"""最小模型入口：三个模型环境变量的校验与一个 OpenAI 兼容调用入口（stage4.md §3.7、§5.3）。

- **只在环境变量里取配置**：``MODEL_API_KEY``／``MODEL_BASE_URL``／``MODEL_MODEL`` 三者缺一即
  :class:`ModelConfigurationError`；取值不回显、不写日志、不进 State／业务库／checkpoint。
- **一个具体入口，不是基础设施**：:func:`openai_compatible_model_call` 返回节点接收的 callable
  （系统提示词 ＋ 用户载荷 → 响应文本），:func:`build_chat_model` 暴露同一个 ``ChatOpenAI`` 对象；
  不建 Planner／Evaluator 基类、Agent 工厂或 Provider 注册中心。
- **单次请求超时 60 秒**（决策 8B）：在模型对象上固定；180 秒 Run 时限与最多 5 次请求由 Graph
  运行上下文 ``graph.nodes.ModelRequestBudget`` 持有。
- **响应文本 → 目标 Schema** 的解析在 :func:`parse_model_json`：非法结构是运行错误，不消耗修订次数、
  不创建 rejected 计划。

注入点：节点构造期传入 :data:`ModelCall`，测试用固定替身即可在无 API Key 的情况下驱动整条计划链路。
"""

import os
from collections.abc import Awaitable, Callable
from typing import TypeVar

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from config import (
    MODEL_API_KEY_ENV,
    MODEL_BASE_URL_ENV,
    MODEL_MODEL_ENV,
    MODEL_REQUEST_TIMEOUT_SECONDS,
)

#: 一次模型调用的注入契约：系统提示词 ＋ 用户载荷文本 → 原始响应文本。
ModelCall = Callable[[str, str], Awaitable[str]]

TModel = TypeVar("TModel", bound=BaseModel)


class ModelConfigurationError(ValueError):
    """模型配置缺失：三个环境变量缺一即配置错误，不落默认值、不猜端点。"""


class InvalidModelResponse(ValueError):
    """模型响应不是合法 JSON 或不符合目标 Schema：运行错误，不消耗修订次数。"""


def require_model_env(name: str) -> str:
    """读取一个模型环境变量；缺失或空白即 :class:`ModelConfigurationError`（不暴露其它取值）。"""
    value = os.environ.get(name, "").strip()
    if not value:
        raise ModelConfigurationError(f"缺少模型配置环境变量：{name}")
    return value


def build_chat_model() -> ChatOpenAI:
    """按环境变量创建一个具体的 OpenAI 兼容模型对象（单次请求超时固定 60 秒）。"""
    return ChatOpenAI(
        api_key=require_model_env(MODEL_API_KEY_ENV),
        base_url=require_model_env(MODEL_BASE_URL_ENV),
        model=require_model_env(MODEL_MODEL_ENV),
        timeout=MODEL_REQUEST_TIMEOUT_SECONDS,
    )


def openai_compatible_model_call() -> ModelCall:
    """创建 OpenAI 兼容的模型调用入口：系统提示词 ＋ 用户载荷 → 响应文本。

    生产接线由 Stage 5 把它注入生成计划子图；本函数自己不做超时兜底（超时在模型对象上），
    也不记录提示词或响应全文。
    """

    chat = build_chat_model()

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
