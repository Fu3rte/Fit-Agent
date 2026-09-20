import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import AsyncToolCallWrapper, ToolCallRequest
from langgraph.types import Command

from harness.bounded import DEFAULT_MAX_BYTES, DEFAULT_MAX_LINES, bound_text
from harness.cache import CachedToolResult, ToolResultCache
from harness.declaration import HarnessContext


class ToolResultContractError(RuntimeError):
    """wrapper 的结果不是 ToolMessage，或其 content 不是字符串。"""


LOGGER = logging.getLogger(__name__)


def build_tool_call_wrapper(
    *,
    timeout_seconds: float,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_lines: int = DEFAULT_MAX_LINES,
    cache: ToolResultCache | None = None,
) -> AsyncToolCallWrapper:
    """构造 ToolNode 的 awrap_tool_call：扣工具预算 → 缓存查找 → 单工具超时 → execute → 结果校验与定界。

    ``cache`` 为 None 时只走阶段 1／5 路径（不查缓存、不写缓存），行为与阶段 5 完全一致。
    只有成功的 ToolMessage 进入缓存：错误、超时、取消与数据库异常都不写缓存。
    """

    async def wrapper(
        request: ToolCallRequest,
        execute: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        context: HarnessContext = request.runtime.context
        context.budget.take_tool_call()
        started = time.perf_counter()
        key = None if cache is None else await cache.key_for(request)
        if key is not None:
            hit = cache.get(key)
            if hit is not None:
                _log_observation(
                    request, hit, started=started, cache_hit=True
                )
                return hit.to_message(request)
        async with asyncio.timeout(timeout_seconds):
            result = await execute(request)
        if not isinstance(result, ToolMessage):
            raise ToolResultContractError(
                f"工具 {request.tool_call['name']} 的执行结果不是 ToolMessage：{type(result).__name__}"
            )
        if not isinstance(result.content, str):
            raise ToolResultContractError(
                f"工具 {request.tool_call['name']} 的 ToolMessage.content 不是字符串："
                f"{type(result.content).__name__}"
            )
        bounded = bound_text(result.content, max_bytes=max_bytes, max_lines=max_lines)
        observation = CachedToolResult(
            content=bounded.text,
            artifact=result.artifact,
            status=result.status,
            truncated=bounded.truncated,
            result_bytes=len(bounded.text.encode("utf-8")),
        )
        if key is not None and result.status == "success":
            cache.put(key, observation)
        _log_observation(request, observation, started=started, cache_hit=False)
        if not bounded.truncated:
            return result
        return result.model_copy(update={"content": bounded.text})

    return wrapper


def _log_observation(
    request: ToolCallRequest,
    observation: CachedToolResult,
    *,
    started: float,
    cache_hit: bool,
) -> None:
    """内部观察量：只记工具名、耗时与计数，不含画像、训练记录与用户原始字段。"""
    LOGGER.info(
        "tool call name=%s duration_ms=%.1f cache_hit=%s truncated=%s result_bytes=%d",
        request.tool_call["name"],
        (time.perf_counter() - started) * 1000.0,
        cache_hit,
        observation.truncated,
        observation.result_bytes,
    )
