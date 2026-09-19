import json
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, TypeVar

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from config import MODEL_REQUEST_TIMEOUT_SECONDS
from provider_settings import (
    ModelConfigurationError as ModelConfigurationError,
)
from provider_settings import ProviderConfig, resolve_model_credentials

ModelCall = Callable[[str, str], Awaitable[str]]

TModel = TypeVar("TModel", bound=BaseModel)


class InvalidModelResponse(ValueError):
    """模型响应不是合法 JSON 或不符合目标 Schema。"""


class ModelCallFailed(ValueError):
    """Provider／SDK 模型调用失败。"""


MODEL_CALL_FAILED_MESSAGE = "模型调用失败：本次运行未产生计划写入，请稍后重试"


PROBE_SYSTEM_PROMPT = "你是模型连通性检查助手。"
PROBE_USER_PAYLOAD = "只回复 OK，不要输出其它内容。"


def build_chat_model(
    data_dir: Path, override: ProviderConfig | None = None
) -> ChatOpenAI:
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
    call = openai_compatible_model_call(data_dir, override)
    await call(PROBE_SYSTEM_PROMPT, PROBE_USER_PAYLOAD)


def parse_model_json(text: str, model_type: type[TModel]) -> TModel:
    try:
        return model_type.model_validate_json(text)
    except ValueError as exc:
        raise InvalidModelResponse(
            f"模型响应不是合法 {model_type.__name__}：{exc}"
        ) from exc


def dump_model_payload(payload: Mapping[str, Any]) -> str:
    """载荷 → 模型输入文本：日期等非 JSON 原生值按文本写出，排序固定便于复现。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
