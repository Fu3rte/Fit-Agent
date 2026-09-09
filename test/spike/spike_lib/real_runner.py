# Fit-Agent PydanticAI spike：唯一真实调用入口。
# 所有真实模型调用必须经本模块构建（FeeGuardTransport + max_retries=0 + max_tokens=256）。
# 在真实调用路径上直接构造 Agent/Model/OpenAI client 绕过本入口，即违反费用护栏契约。
# API Key 只经环境变量/参数传入进程，不写入源码、日志或证据文件。

from __future__ import annotations

from collections.abc import Callable

import httpx2 as httpx  # pyright: ignore[reportMissingImports]  # venv 内依赖：运行时+真 pyright 均可解析；仅扫描器 import 误报
from openai import AsyncOpenAI  # pyright: ignore[reportMissingImports]  # 同上
from pydantic_ai import Agent  # pyright: ignore[reportMissingImports]  # 同上
from pydantic_ai.models.openai import (  # pyright: ignore[reportMissingImports]  # 同上
    OpenAIChatModel,
)
from pydantic_ai.providers.deepseek import (  # pyright: ignore[reportMissingImports]  # 同上
    DeepSeekProvider,
)

from spike_lib.fee_guard import MAX_TOKENS_LIMIT, FeeGuard
from spike_lib.guard_transport import FeeGuardTransport

DEEPSEEK_BASE_URL = (
    "https://api.deepseek.com"  # OpenAI 兼容端点（用户已拍板，本轮唯一真实 Provider）
)


def build_spike_agent(
    model_name: str,
    *,
    guard: FeeGuard,
    api_key: str,
    tools: list | None = None,
    instructions: str = "",
    inner_transport: httpx.AsyncBaseTransport | None = None,
    captured: list | None = None,
    on_event: Callable[[str], None] | None = None,
    base_url: str = DEEPSEEK_BASE_URL,
) -> Agent:
    """构建受护栏约束的 Agent（真实调用入口；测试以桩 transport 注入 inner_transport）。"""
    transport = FeeGuardTransport(
        inner=inner_transport
        if inner_transport is not None
        else httpx.AsyncHTTPTransport(),
        guard=guard,
        captured=captured,
        on_event=on_event,
    )
    client = httpx.AsyncClient(transport=transport)
    # SDK 重试必须为 0：默认 DEFAULT_MAX_RETRIES=2 会绕过单次预算语义。
    openai_client = AsyncOpenAI(
        base_url=base_url,
        api_key=api_key,
        max_retries=0,
        http_client=client,
    )
    provider = DeepSeekProvider(openai_client=openai_client)
    model = OpenAIChatModel(model_name, provider=provider)
    # 框架级重试同样不加：每次 wire 请求都经 FeeGuardTransport 独立预留/结算，
    # 任何层级的自动重试都会形成新的 wire 请求并被逐次记账（守门见 guard_transport）。
    return Agent(
        model,
        tools=tools if tools is not None else [],
        instructions=instructions,
        model_settings={"max_tokens": MAX_TOKENS_LIMIT},
    )


def read_api_key_from_env(env_name: str = "DEEPSEEK_API_KEY") -> str:
    """从环境变量读取 Key；缺失即拒绝（不提示值、不落盘）。"""
    import os

    value = os.getenv(env_name)
    if not value:
        raise RuntimeError(
            f"环境变量 {env_name} 未配置；真实调用拒绝启动（Key 只经环境变量传入）"
        )
    return value
