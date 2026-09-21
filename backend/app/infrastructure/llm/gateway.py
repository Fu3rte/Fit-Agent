from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn

import anthropic
import openai
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict

from app.application.ports import (
    MODEL_CALL_FAILED_MESSAGE,
    InvalidModelResponse,
    ModelCallFailed,
    ModelConfigurationError,
    ModelGateway,
    TModel,
    TransientModelError,
)
from app.infrastructure.llm.provider_settings import (
    API_ANTHROPIC_MESSAGES,
    API_OPENAI_COMPATIBLE,
    STRUCTURED_OUTPUT_FUNCTION_CALLING_STRICT,
    STRUCTURED_OUTPUT_JSON_SCHEMA,
    ProviderConfig,
    resolve_provider_config,
)
from config import MODEL_REQUEST_TIMEOUT_SECONDS

#: 各 transport → 客户端类：只看 ``api``，base_url 原样传入，不改写任何端点。
_APIS: dict[str, type] = {
    API_OPENAI_COMPATIBLE: ChatOpenAI,
    API_ANTHROPIC_MESSAGES: ChatAnthropic,
}

#: （api, structured_output）→ with_structured_output 原生 kwargs；组合合法性已在 resolve 阶段校验。
# Anthropic 安装版只认 method 形参，额外 kwargs 被忽略，也没有 strict 形参。
_STRUCTURED_KWARGS: dict[tuple[str, str], dict[str, Any]] = {
    (API_OPENAI_COMPATIBLE, STRUCTURED_OUTPUT_JSON_SCHEMA): {
        "method": "json_schema",
        "strict": True,
    },
    (API_OPENAI_COMPATIBLE, STRUCTURED_OUTPUT_FUNCTION_CALLING_STRICT): {
        "method": "function_calling",
        "strict": True,
    },
    (API_ANTHROPIC_MESSAGES, STRUCTURED_OUTPUT_JSON_SCHEMA): {
        "method": "json_schema",
    },
}

#: 可重试的明确瞬时故障类型：SDK 把 httpx 的连接错误、读超时与协议错误包装成 ``APIConnectionError``
#: （``APITimeoutError`` 是其子类）；529 过载由 Anthropic 的显式过载失败给出（不在状态码集合内）。
_RETRYABLE_FAILURE_TYPES: tuple[type[Exception], ...] = (
    openai.APIConnectionError,
    anthropic.APIConnectionError,
    anthropic.OverloadedError,
)

#: 可重试的 HTTP 状态码：429 限流与 500／502／503／504 服务端瞬时故障。
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

#: 状态码判定的唯一适用类型：只有这两类的 ``status_code`` 是 Provider 的 HTTP 状态。
_STATUS_ERROR_TYPES: tuple[type[Exception], ...] = (
    openai.APIStatusError,
    anthropic.APIStatusError,
)

PROBE_SYSTEM_PROMPT = "你是模型连通性检查助手。"
PROBE_USER_PAYLOAD = "只回复 OK，不要输出其它内容。"


class StructuredProbeDetail(BaseModel):
    """探针的嵌套对象：nullable 与 extra=forbid 同时存在，Schema 变形会被拒。"""

    model_config = ConfigDict(extra="forbid")

    label: str


class StructuredProbeResult(BaseModel):
    """探针目标 Schema 的最小形式：一个必填字段、一个 nullable 字段、一个 extra=forbid 嵌套对象。"""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    note: str | None = None
    detail: StructuredProbeDetail


STRUCTURED_PROBE_SYSTEM_PROMPT = "你是模型结构化输出检查助手。"
STRUCTURED_PROBE_USER_PAYLOAD = (
    "按给定 Schema 返回 ok 为 true、note 为 null、detail.label 为 probe 的结果，不要输出其它内容。"
)


def build_chat_client(config: ProviderConfig) -> ChatOpenAI | ChatAnthropic:
    """按 api 选客户端；base_url 原样传入，不改写任何端点、不追加路径。"""
    return _APIS[config.api](
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
        timeout=MODEL_REQUEST_TIMEOUT_SECONDS,
    )


def build_model_gateway(
    data_dir: Path, override: ProviderConfig | None = None
) -> ModelGateway:
    config = resolve_provider_config(data_dir, override)
    chat = build_chat_client(config)
    structured_kwargs = _STRUCTURED_KWARGS[(config.api, config.structured_output)]

    async def text(system_prompt: str, user_payload: str) -> str:
        response = await chat.ainvoke(
            [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_payload),
            ]
        )
        content = response.content
        if not isinstance(content, str):
            raise InvalidModelResponse("模型响应不是纯文本：无法作为可见文本使用")
        return content

    async def structured(
        system_prompt: str, user_payload: str, schema: type[TModel]
    ) -> TModel:
        runnable = chat.with_structured_output(schema, **structured_kwargs)
        result = await runnable.ainvoke(
            [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_payload),
            ]
        )
        # Provider 未发起 tool call 时 with_structured_output 返回 None：不把非目标 Schema 实例
        # 交给调用方，否则下游按字段取值会崩溃成属性错误。
        if not isinstance(result, schema):
            raise InvalidModelResponse("模型响应不是目标 Schema 的实例：Provider 未返回结构化结果")
        return result

    async def tools(
        messages: Sequence[BaseMessage], offered_tools: Sequence[BaseTool]
    ) -> AIMessage:
        # 与 text／structured 共用同一个 chat client；工具调用只读标准 AIMessage.tool_calls。
        response = await chat.bind_tools(tuple(offered_tools)).ainvoke(messages)
        if not isinstance(response, AIMessage):
            raise InvalidModelResponse("模型响应不是 AIMessage：无法读取原生工具调用")
        return response

    return ModelGateway(text=text, structured=structured, tools=tools)


def _is_transient_failure(error: BaseException) -> bool:
    """失败是否属于可重试的瞬时故障：只看 ``__cause__`` 链，取消与产品错误都不算。"""
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, _RETRYABLE_FAILURE_TYPES):
            return True
        if (
            isinstance(current, _STATUS_ERROR_TYPES)
            and current.status_code in _RETRYABLE_STATUS_CODES
        ):
            return True
        current = current.__cause__
    return False


def _raise_model_call_error(error: BaseException) -> NoReturn:
    """Provider 调用失败的唯一分类点：瞬时故障转 :class:`TransientModelError`，其余转固定文本。"""
    if _is_transient_failure(error):
        raise TransientModelError(MODEL_CALL_FAILED_MESSAGE) from error
    raise ModelCallFailed(MODEL_CALL_FAILED_MESSAGE) from error


def build_dynamic_model_gateway(data_dir: Path) -> ModelGateway:
    """唯一模型入口：每次调用重新 resolve 配置；配置错误原样抛出，其余失败按瞬时／非瞬时分类。"""

    async def text(system_prompt: str, user_payload: str) -> str:
        try:
            gateway = build_model_gateway(data_dir)
            return await gateway.text(system_prompt, user_payload)
        except ModelConfigurationError:
            raise
        except Exception as error:
            _raise_model_call_error(error)

    async def structured(
        system_prompt: str, user_payload: str, schema: type[TModel]
    ) -> TModel:
        try:
            gateway = build_model_gateway(data_dir)
            return await gateway.structured(system_prompt, user_payload, schema)
        except ModelConfigurationError:
            raise
        except Exception as error:
            _raise_model_call_error(error)

    async def tools(
        messages: Sequence[BaseMessage], offered_tools: Sequence[BaseTool]
    ) -> AIMessage:
        try:
            gateway = build_model_gateway(data_dir)
            return await gateway.tools(messages, offered_tools)
        except ModelConfigurationError:
            raise
        except InvalidModelResponse:
            # 响应不是 AIMessage 属模型响应错误，不是 Provider 调用失败：原样上抛给 Harness。
            raise
        except Exception as error:
            _raise_model_call_error(error)

    return ModelGateway(text=text, structured=structured, tools=tools)


async def probe_model_call(
    data_dir: Path, override: ProviderConfig | None = None
) -> None:
    """连通性探针：一次普通文本调用（连接、鉴权与模型名）。"""
    gateway = build_model_gateway(data_dir, override)
    await gateway.text(PROBE_SYSTEM_PROMPT, PROBE_USER_PAYLOAD)


async def probe_structured_model_call(
    data_dir: Path, override: ProviderConfig | None = None
) -> None:
    """结构化能力探针：用所选机制发一次真实结构化调用，Schema 变形与拒绝都在此暴露。"""
    gateway = build_model_gateway(data_dir, override)
    await gateway.structured(
        STRUCTURED_PROBE_SYSTEM_PROMPT,
        STRUCTURED_PROBE_USER_PAYLOAD,
        StructuredProbeResult,
    )
