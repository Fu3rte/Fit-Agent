"""生产 Provider 接入：DeepSeek OpenAI 兼容端点上的 ``deepseek-flash``（S4-01 接缝）。

三件事，不多做：

1. 模型 id 必须来自 :mod:`runtime.models` 目录（未知 id 拒绝，不推断别名）。
2. SDK 客户端 **禁用隐式重试**（``max_retries=0``）：框架与适配层必须能看见每一次实际请求，
   SDK 自身静默重发会产生不可见请求与不可见费用（08 8.6「禁止多层隐式重试叠加」）。
   离线证据见 ``tests/test_stage4_sdk_retries_offline.py``（本地假服务端断言恰好一次发送）。
3. 显式传框架模型 profile（窗口 1,000,000、思考支持与默认行为由目录覆盖，不依赖框架名推断）。
   思考模式（2026-09-12 用户拍板）：保持 Provider 默认**开启**，但不把它写成能力限制——
   本模块不提供按 Run 的思考开关，也不传任何 thinking 参数或关闭参数；隐藏推理不写入消息
   或产品／SSE 输出。

Harness 参数（单次请求 120 秒／Run 300 秒、输出上限、预算）的加载、硬边界校验与 Run 期冻结
归 S4-05；本模块只提供 ``timeout_seconds`` 透传，不读配置文件、不读环境变量、不碰业务库。
凭据只作为参数传入（生产取用路径为 Provider 配置仓储，归 S4-04）；本模块不打印、不记录它。
"""

from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.providers.deepseek import DeepSeekProvider

from runtime.models import (
    DEEPSEEK_FLASH,
    ModelSpec,
    require_supported_model_id,
    resolve_model_profile,
)


def build_openai_client(
    api_key: str,
    *,
    spec: ModelSpec = DEEPSEEK_FLASH,
    timeout_seconds: float | None = None,
) -> AsyncOpenAI:
    """构造 SDK 客户端：``max_retries=0``，端点取目录（单一来源）。

    ``timeout_seconds`` 为 None 时用 SDK 默认值；生产时限（单次请求 120 秒）由 S4-05
    从冻结参数传入，本模块不预置数值、不自行放宽。
    """
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("api_key 必须是非空字符串（本模块不回显、不记录其内容）")
    if timeout_seconds is None:
        return AsyncOpenAI(api_key=api_key, base_url=spec.base_url, max_retries=0)
    return AsyncOpenAI(
        api_key=api_key, base_url=spec.base_url, max_retries=0, timeout=timeout_seconds
    )


def build_model(
    api_key: str,
    *,
    model_id: str = DEEPSEEK_FLASH.model_id,
    timeout_seconds: float | None = None,
) -> OpenAIChatModel:
    """按目录构造框架模型对象；未知模型 id 抛 :class:`runtime.models.UnknownModelId`。"""
    spec: ModelSpec = require_supported_model_id(model_id)
    profile: ModelProfile = resolve_model_profile(model_id)
    client = build_openai_client(api_key, spec=spec, timeout_seconds=timeout_seconds)
    return OpenAIChatModel(
        model_id,
        provider=DeepSeekProvider(openai_client=client),
        profile=profile,
    )
