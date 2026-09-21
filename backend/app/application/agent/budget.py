import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

from app.application.ports import (
    ModelGateway,
    TModel,
    TransientModelError,
    dump_model_payload,
)
from config import (
    GRAPH_RUN_TIMEOUT_SECONDS,
    MAX_MODEL_REQUESTS_PER_RUN,
    MAX_TOOL_CALLS_PER_RUN,
    MODEL_REQUEST_TIMEOUT_SECONDS,
)


class ModelRequestBudgetExceeded(RuntimeError):
    """一次 Run 的模型请求预算已用尽（次数上限或 Run 时限）。"""


class ToolCallBudgetExceeded(RuntimeError):
    """一次 Run 的工具调用预算已用尽（次数上限或 Run 时限）。"""


@dataclass(slots=True)
class ModelRequestBudget:
    """一次 Run 的模型请求与工具调用预算：两者共享同一 ``started_at`` 与 Run 总时限。"""

    max_requests: int = MAX_MODEL_REQUESTS_PER_RUN
    run_timeout_seconds: float = GRAPH_RUN_TIMEOUT_SECONDS
    request_timeout_seconds: float = MODEL_REQUEST_TIMEOUT_SECONDS
    started_at: float = field(default_factory=time.monotonic)
    used: int = 0
    max_tool_calls: int = MAX_TOOL_CALLS_PER_RUN
    tool_calls: int = 0

    def remaining_run_seconds(self) -> float:
        """本 Run 的剩余时限（秒）。"""
        return self.run_timeout_seconds - (time.monotonic() - self.started_at)

    def take_tool_call(self) -> None:
        """登记一次工具调用；次数上限或 Run 时限已用尽即明确失败，调用方不执行工具。"""
        if self.tool_calls >= self.max_tool_calls:
            raise ToolCallBudgetExceeded(
                f"单次 Run 最多 {self.max_tool_calls} 次工具调用，已用尽（不消耗模型请求次数）"
            )
        if self.remaining_run_seconds() <= 0:
            raise ToolCallBudgetExceeded(
                f"单次 Run 的 {self.run_timeout_seconds} 秒时限已用尽（不消耗模型请求次数）"
            )
        self.tool_calls += 1

    def begin_request(self) -> float:
        """登记一次模型请求，返回该次请求可用的超时秒数（不超过 Run 剩余时限）。"""
        if self.used >= self.max_requests:
            raise ModelRequestBudgetExceeded(
                f"单次 Run 最多 {self.max_requests} 次模型请求，已用尽（不消耗修订次数）"
            )
        remaining = self.remaining_run_seconds()
        if remaining <= 0:
            raise ModelRequestBudgetExceeded(
                f"单次 Run 的 {self.run_timeout_seconds} 秒模型请求时限已用尽（不消耗修订次数）"
            )
        self.used += 1
        return min(self.request_timeout_seconds, remaining)


#: 瞬时失败后的固定重试延迟（秒）；重试只发生一次。
MODEL_RETRY_DELAY_SECONDS = 1.0

TResult = TypeVar("TResult")


def _is_transient_failure(error: BaseException) -> bool:
    """失败是否属于可重试的瞬时故障：只识别网关映射出的 :class:`TransientModelError`。"""
    return isinstance(error, TransientModelError)


async def _request_with_retry(
    request: Callable[[], Awaitable[TResult]], budget: ModelRequestBudget
) -> TResult:
    """模型请求的唯一物理尝试循环：瞬时失败固定延迟后重试一次，其余失败立即上抛。

    每个物理尝试各登记一次请求预算，重试同样占用次数与 Run 剩余时限；取消是
    ``BaseException``，不进入本分支，绝不被当作瞬时失败重试。
    """
    attempt = 0
    while True:
        timeout = budget.begin_request()
        try:
            async with asyncio.timeout(timeout):
                return await request()
        except Exception as error:
            if attempt > 0 or not _is_transient_failure(error):
                raise
            attempt += 1
            await asyncio.sleep(MODEL_RETRY_DELAY_SECONDS)


async def request_model(
    model: ModelGateway,
    system_prompt: str,
    payload: Mapping[str, Any],
    budget: ModelRequestBudget,
) -> str:
    """一次文本模型请求：先扣请求预算，再在剩余时限内调用注入的 callable，瞬时失败重试一次。"""
    user_payload = dump_model_payload(payload)
    return await _request_with_retry(
        lambda: model.text(system_prompt, user_payload), budget
    )


async def request_structured_model(
    model: ModelGateway,
    system_prompt: str,
    payload: Mapping[str, Any],
    budget: ModelRequestBudget,
    schema: type[TModel],
) -> TModel:
    """一次结构化模型请求：与文本请求共享同一份预算、超时与重试，Schema 由 Provider 约束。"""
    user_payload = dump_model_payload(payload)
    return await _request_with_retry(
        lambda: model.structured(system_prompt, user_payload, schema), budget
    )
