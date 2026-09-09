# Fit-Agent PydanticAI spike：最小 Run 取消语义 harness（离线验证）。
# 验证点：取消后禁止后续模型/工具调用；取消不产生成功提交；
# 不可中断工具收尾（同一个 shielded task）完成后才释放执行槽；取消后 harness 可复用。
# 真实流式取消另需凭据，本轮明确标注未执行。

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import Agent


@dataclass
class RunOutcome:
    status: str  # running | completed | cancelled | failed
    events: list[str] = field(default_factory=list)


class RunHarness:
    """单执行槽 Run 包装：整 Run 取消、成功提交判定、槽释放时序。

    cancel() 取消当前 Run 任务（尽力中断）；drive 在每个节点前检查取消标记，
    禁止启动后续模型/工具；成功提交仅在 drive 正常结束且无取消时判定。
    槽在 inner 任务实际退出后才释放；每个 Run 开始时重置取消标记（可复用）。
    """

    def __init__(self, agent: Agent) -> None:
        self._agent = agent
        self._slot = asyncio.Lock()
        self._cancel_requested = False
        self._inner: asyncio.Future | None = None

    def cancel(self) -> None:
        self._cancel_requested = True
        inner = self._inner
        if inner is not None and not inner.done():
            inner.cancel()

    async def run(
        self,
        prompt: str,
        *,
        message_history: list | None = None,
        outcome: RunOutcome | None = None,
        timeline: list[str] | None = None,
        stream: bool = False,
    ) -> RunOutcome:
        """执行一个 Run。传入外部 outcome 时，状态与事件（含工具事件）记录在同一列表，保证全局时序。
        timeline：与 transport/护栏共享的全局时间线（如 model_request / chunk:N / slot_released），
        用于验证"后续 Run 的模型请求发生在前一 Run 槽释放之后"。
        stream=True 时用 run_stream 真流式驱动（wire 请求 stream:true）。"""
        self._cancel_requested = False  # 复用语义：每个 Run 开始时重置取消标记
        outcome = outcome or RunOutcome(status="running")
        async with self._slot:
            inner = asyncio.ensure_future(self._drive(prompt, message_history, outcome, stream))
            self._inner = inner
            try:
                await inner
            except asyncio.CancelledError:
                pass
            except Exception:
                pass  # 失败也是终态：inner.exception() 已保留，槽照常释放
            finally:
                if not inner.done():  # 兜底：确保底层任务实际退出后再释放槽
                    inner.cancel()
                    try:
                        await inner
                    except asyncio.CancelledError:
                        pass
            if self._cancel_requested:
                outcome.status = "cancelled"
            elif inner.cancelled():
                outcome.status = "cancelled"
            else:
                exc = inner.exception()
                outcome.status = "failed" if exc is not None else "completed"
            outcome.events.append("slot_released")
            if timeline is not None:
                timeline.append("slot_released")
        return outcome

    async def _drive(self, prompt: str, message_history: list | None, outcome: RunOutcome, stream: bool) -> None:
        if stream:
            async with self._agent.run_stream(prompt, message_history=message_history) as result:
                async for _ in result.stream_text():
                    if self._cancel_requested:
                        raise asyncio.CancelledError  # 流中途取消：禁止继续消费/后续调用
        else:
            async with self._agent.iter(prompt, message_history=message_history) as run:
                async for node in run:
                    if self._cancel_requested:
                        raise asyncio.CancelledError  # 禁止后续模型/工具调用
                    _ = node
        if self._cancel_requested:
            raise asyncio.CancelledError
        outcome.events.append("run_drive_finished")


def make_tool(name: str, outcome: RunOutcome, *, uninterruptible_tail: float = 0.0) -> Callable[..., Awaitable[str]]:
    """构造可观测工具；uninterruptible_tail>0 时模拟"已开始且无法安全中断"的收尾：
    收尾在同一个 shielded task 中完成；取消时 await 同一个 task 收尾后重新抛出取消。
    完成事件只记录一次，不存在第二次 sleep/重复完成。"""

    async def tool() -> str:
        outcome.events.append(f"tool_start:{name}")
        if uninterruptible_tail <= 0:
            await asyncio.sleep(0)
            outcome.events.append(f"tool_done:{name}")
            return "done"

        async def tail() -> str:
            await asyncio.sleep(uninterruptible_tail)
            outcome.events.append(f"tool_done:{name}")
            return "tail-done"

        task = asyncio.ensure_future(tail())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # 已开始的不可中断调用：等待同一个 task 实际收尾，再让取消继续传播。
            await task
            raise

    tool.__name__ = name
    tool.__doc__ = f"spike tool {name}"
    return tool


def agent_of(model: Any, tools: list[Callable[..., Awaitable[str]]]) -> Agent:
    return Agent(model, tools=tools, instructions="spike resident instructions")
