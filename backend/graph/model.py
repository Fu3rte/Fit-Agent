import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict

from config import MODEL_REQUEST_TIMEOUT_SECONDS
from provider_settings import (
    API_ANTHROPIC_MESSAGES,
    API_OPENAI_COMPATIBLE,
    STRUCTURED_OUTPUT_FUNCTION_CALLING_STRICT,
    STRUCTURED_OUTPUT_JSON_SCHEMA,
    ProviderConfig,
    resolve_provider_config,
)

TModel = TypeVar("TModel", bound=BaseModel)

#: 文本形态模型调用：返回模型文本，供看板解释、打卡摘要与知识问答等无 Schema 的答复使用。
ModelCall = Callable[[str, str], Awaitable[str]]

#: 结构化形态模型调用：Schema 由所选结构化输出机制约束，直接返回 Pydantic 实例。
StructuredModelCall = Callable[[str, str, type[TModel]], Awaitable[TModel]]

#: 工具调用形态模型调用：原生消息序列与 offered 工具 tuple → 含 ``tool_calls`` 的 AIMessage。
ToolModelCall = Callable[
    [Sequence[BaseMessage], Sequence[BaseTool]], Awaitable[AIMessage]
]

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


@dataclass(frozen=True, slots=True)
class ModelGateway:
    """唯一模型入口的三个形态：每次调用都重新解析 Provider 配置。"""

    text: ModelCall
    structured: StructuredModelCall
    tools: ToolModelCall


class InvalidModelResponse(ValueError):
    """模型响应不是合法文本或不符合目标 Schema。"""


class ModelCallFailed(ValueError):
    """Provider／SDK 模型调用失败。"""


MODEL_CALL_FAILED_MESSAGE = "模型调用失败：本次运行未产生计划写入，请稍后重试"


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


def dump_model_payload(payload: Mapping[str, Any]) -> str:
    """载荷 → 模型输入文本：日期等非 JSON 原生值按文本写出，排序固定便于复现。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
