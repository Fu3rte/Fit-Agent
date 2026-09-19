from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from config import MODEL_REQUEST_TIMEOUT_SECONDS
from provider_settings import (
    ModelConfigurationError as ModelConfigurationError,
)
from provider_settings import ProviderConfig, resolve_model_credentials

#: 一次模型调用的注入契约：系统提示词 ＋ 用户载荷文本 → 原始响应文本。
ModelCall = Callable[[str, str], Awaitable[str]]

TModel = TypeVar("TModel", bound=BaseModel)


class InvalidModelResponse(ValueError):
    """模型响应不是合法 JSON 或不符合目标 Schema：运行错误，不消耗修订次数。"""


class ModelCallFailed(ValueError):
    """Provider／SDK 模型调用失败：只用固定文本，不回显端点、模型名、密钥或堆栈。"""


#: :class:`ModelCallFailed` 的可见文本：产品提示 ＋ 本次 Run 没有计划写入。
MODEL_CALL_FAILED_MESSAGE = "模型调用失败：本次运行未产生计划写入，请稍后重试"


#: 连通性探测的固定提示词：单次最小调用，不进 Run 预算。
PROBE_SYSTEM_PROMPT = "你是模型连通性检查助手。"
PROBE_USER_PAYLOAD = "只回复 OK，不要输出其它内容。"


def build_chat_model(
    data_dir: Path, override: ProviderConfig | None = None
) -> ChatOpenAI:
    """按 provider.json 优先、空字段回落 MODEL_* 创建模型对象（每次调用重新 resolve）。"""
    api_key, base_url, model = resolve_model_credentials(data_dir, override)
    return ChatOpenAI(
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout=MODEL_REQUEST_TIMEOUT_SECONDS,
    )


def openai_compatible_model_call(
    data_dir: Path, override: ProviderConfig | None = None
) -> ModelCall:
    """创建 OpenAI 兼容的模型调用入口：系统提示词 ＋ 用户载荷 → 响应文本。"""
    chat = build_chat_model(data_dir, override)

    async def call(system_prompt: str, user_payload: str) -> str:
        response = await chat.ainvoke(
            [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_payload),
            ]
        )
        content = response.content
        if not isinstance(content, str):
            raise InvalidModelResponse("模型响应不是纯文本：无法解析为统一 Schema")
        return content

    return call


async def probe_model_call(
    data_dir: Path, override: ProviderConfig | None = None
) -> None:
    """一次性连通性探测：固定提示词单次请求，失败向上抛。"""
    call = openai_compatible_model_call(data_dir, override)
    await call(PROBE_SYSTEM_PROMPT, PROBE_USER_PAYLOAD)


def parse_model_json(text: str, model_type: type[TModel]) -> TModel:
    """响应文本 → 目标 Schema 对象（未声明字段一律拒绝）；形状不符即 :class:`InvalidModelResponse`。"""
    try:
        return model_type.model_validate_json(text)
    except ValueError as exc:
        raise InvalidModelResponse(
            f"模型响应不是合法 {model_type.__name__}：{exc}"
        ) from exc
