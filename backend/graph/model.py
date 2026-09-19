"""最小模型入口：模型配置的校验与一个 OpenAI 兼容调用入口（stage4.md §3.7、§5.3）。

- **配置取值两级来源**：``provider_settings.resolve_model_credentials`` 先读数据目录
  ``provider.json``，非空字段优先；空字段回落 ``MODEL_API_KEY``／``MODEL_BASE_URL``／
  ``MODEL_MODEL`` 环境变量；两者皆空即 ``provider_settings.ModelConfigurationError``。
- **一个具体入口，不是基础设施**：:func:`openai_compatible_model_call` 返回节点接收的 callable；
  不建 Agent 工厂或 Provider 注册中心。
- **单次请求超时 60 秒**（决策 8B）：在模型对象上固定。

注入点：节点构造期传入 :class:`ModelCall`；``data_dir`` 由 app 装配注入。
"""

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
from provider_settings import resolve_model_credentials

#: 一次模型调用的注入契约：系统提示词 ＋ 用户载荷文本 → 原始响应文本。
ModelCall = Callable[[str, str], Awaitable[str]]

TModel = TypeVar("TModel", bound=BaseModel)


class InvalidModelResponse(ValueError):
    """模型响应不是合法 JSON 或不符合目标 Schema：运行错误，不消耗修订次数。"""


class ModelCallFailed(ValueError):
    """Provider／SDK 模型调用失败：只用固定文本，不回显端点、模型名、密钥或堆栈。"""


#: :class:`ModelCallFailed` 的可见文本：产品提示 ＋ 本次 Run 没有计划写入。
MODEL_CALL_FAILED_MESSAGE = "模型调用失败：本次运行未产生计划写入，请稍后重试"


def build_chat_model(data_dir: Path) -> ChatOpenAI:
    """按 provider.json 优先、空字段回落 MODEL_* 创建模型对象（每次调用重新 resolve）。"""
    api_key, base_url, model = resolve_model_credentials(data_dir)
    return ChatOpenAI(
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout=MODEL_REQUEST_TIMEOUT_SECONDS,
    )


def openai_compatible_model_call(data_dir: Path) -> ModelCall:
    """创建 OpenAI 兼容的模型调用入口：系统提示词 ＋ 用户载荷 → 响应文本。"""
    chat = build_chat_model(data_dir)

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


def parse_model_json(text: str, model_type: type[TModel]) -> TModel:
    """响应文本 → 目标 Schema 对象（未声明字段一律拒绝）；形状不符即 :class:`InvalidModelResponse`。"""
    try:
        return model_type.model_validate_json(text)
    except ValueError as exc:
        raise InvalidModelResponse(
            f"模型响应不是合法 {model_type.__name__}：{exc}"
        ) from exc
