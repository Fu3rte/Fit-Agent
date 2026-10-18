import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from queue import Empty
from threading import Lock
from time import monotonic
from typing import Literal
from uuid import UUID

from fastapi import Request
from fastapi.responses import StreamingResponse

from app.agent.events import AgentEvent
from app.agent.governance import (
    SSE_EVENT_MAX_BYTES,
    SSE_HEARTBEAT_SECONDS,
    SSE_QUEUE_MAX_BYTES,
    SSE_QUEUE_MAX_EVENTS,
    SSE_SEND_WAIT_SECONDS,
    TERMINAL_DRAIN_TOTAL_SECONDS,
    RunControl,
)
from app.application.session.runtime import OUTPUT_TOO_LARGE_MESSAGE
from app.application.session.runtime import RunState as ApplicationRunState

QUEUE_OK = "ok"
QUEUE_CLOSED = "closed"
SSE_HEARTBEAT = b": keep-alive\n\n"


def encode(event: AgentEvent) -> str:
    # 最终 SSE 编码是唯一的字节计量口径：包含事件名、JSON 转义、封装与追加字段。
    return (
        f"event: {event.event}\n"
        f"data: {json.dumps(event.data, ensure_ascii=False, allow_nan=False)}\n\n"
    )


@dataclass(frozen=True)
class EncodedEvent:
    event: AgentEvent
    payload: bytes

    @property
    def name(self) -> str:
        return self.event.event


class BoundedEventQueue:
    """单运行有界 SSE 队列：普通事件受数量与字节上限约束，终态使用独立槽位。"""

    def __init__(
        self,
        *,
        max_events: int = SSE_QUEUE_MAX_EVENTS,
        max_bytes: int = SSE_QUEUE_MAX_BYTES,
        max_event_bytes: int = SSE_EVENT_MAX_BYTES,
    ) -> None:
        self._lock = Lock()
        self._items: deque[EncodedEvent] = deque()
        self._bytes = 0
        self._accepting = True
        self._terminal: EncodedEvent | None = None
        self._terminal_at: float | None = None
        self.max_events = max_events
        self.max_bytes = max_bytes
        self.max_event_bytes = max_event_bytes

    @property
    def pending_events(self) -> int:
        with self._lock:
            return len(self._items)

    @property
    def pending_bytes(self) -> int:
        with self._lock:
            return self._bytes

    @property
    def accepting(self) -> bool:
        with self._lock:
            return self._accepting

    @property
    def terminal_filled(self) -> bool:
        with self._lock:
            return self._terminal is not None

    def put(self, event: EncodedEvent) -> str:
        size = len(event.payload)
        with self._lock:
            if size > self.max_event_bytes:
                self._accepting = False
                return "output_too_large"
            if not self._accepting:
                return QUEUE_CLOSED
            if (
                len(self._items) >= self.max_events
                or self._bytes + size > self.max_bytes
            ):
                # 队列积压超限：停止接收普通输出并请求取消，生产线程不得等待。
                self._accepting = False
                return "slow_consumer"
            self._items.append(event)
            self._bytes += size
            return QUEUE_OK

    def put_terminal(self, event: EncodedEvent) -> str:
        # 终态槽位不受普通队列容量影响；填充时刻是排空总时限起点。
        size = len(event.payload)
        with self._lock:
            self._accepting = False
            if self._terminal is not None:
                return QUEUE_CLOSED
            if size > self.max_event_bytes:
                return "output_too_large"
            self._terminal = event
            self._terminal_at = monotonic()
            return QUEUE_OK

    def take(self) -> EncodedEvent | None:
        with self._lock:
            if not self._items:
                return None
            event = self._items.popleft()
            self._bytes -= len(event.payload)
            return event

    def empty(self) -> bool:
        with self._lock:
            return not self._items and self._terminal is None

    def get_nowait(self) -> AgentEvent:
        # 既有检查脚本使用的队列读取兼容入口；生产发送方分别走 take()/take_terminal()。
        event = self.take()
        if event is not None:
            return event.event
        with self._lock:
            if self._terminal is not None:
                terminal = self._terminal
                self._terminal = None
                return terminal.event
        raise Empty

    def take_terminal(self) -> EncodedEvent | None:
        with self._lock:
            return self._terminal

    def terminal_remaining(self) -> float | None:
        with self._lock:
            if self._terminal_at is None:
                return None
            return TERMINAL_DRAIN_TOTAL_SECONDS - (monotonic() - self._terminal_at)

    def close_normal(self) -> None:
        with self._lock:
            self._accepting = False


class HttpRunOutput:
    def __init__(self, run_id: UUID, control: RunControl) -> None:
        self.run_id = run_id
        self.control = control
        self.events = BoundedEventQueue()
        self._state_lock = Lock()
        self._last_tool_call_id: str | None = None

    def close_normal(self) -> None:
        self.events.close_normal()

    def publish(self, name: str, data: dict) -> bool:
        # 生产线程与主事件循环共用入口：编码后计量，超限不阻塞、不拆分业务快照。
        with self._state_lock:
            if name == "tool_start":
                self._last_tool_call_id = data.get("tool_call_id")
            elif name == "tool_result":
                self._last_tool_call_id = None
        event = AgentEvent(name, {"run_id": str(self.run_id), **data})
        outcome = self.events.put(
            EncodedEvent(event, encode(event).encode("utf-8"))
        )
        if outcome == "output_too_large":
            self.control.request_cancel("output_too_large")
        elif outcome == "slow_consumer":
            self.control.request_cancel("slow_consumer")
        return outcome == QUEUE_OK

    def publish_terminal(self, name: Literal["done", "error"], data: dict) -> None:
        # 仅终态持久化成功后调用；独立槽位在普通队列已满时仍可登记。
        if name == "error":
            with self._state_lock:
                tool_call_id = self._last_tool_call_id
            data = {"tool_call_id": tool_call_id, **data}
        event = AgentEvent(name, {"run_id": str(self.run_id), **data})
        outcome = self.events.put_terminal(
            EncodedEvent(event, encode(event).encode("utf-8"))
        )
        if outcome != "output_too_large":
            return
        # 终态帧本身超过单事件上限：发布固定最小错误，不公开部分内容。
        fallback = AgentEvent("error", {
            "run_id": str(self.run_id),
            "status": "failed",
            "code": "output_too_large",
            "message": OUTPUT_TOO_LARGE_MESSAGE,
            "tool_call_id": None,
        })
        self.events.put_terminal(
            EncodedEvent(fallback, encode(fallback).encode("utf-8"))
        )


class RunState(ApplicationRunState):
    output: HttpRunOutput

    def __init__(
        self, session_id: UUID, *, budget_seconds: float | None = None,
        run_id: UUID | None = None,
    ):
        super().__init__(
            session_id, create_output=HttpRunOutput, budget_seconds=budget_seconds,
            run_id=run_id,
        )

    @property
    def events(self) -> BoundedEventQueue:
        return self.output.events


def stream(request: Request, state: RunState) -> AsyncIterator[bytes]:
    async def generate() -> AsyncIterator[bytes]:
        last_emit = monotonic()
        while True:
            # 故障封闭：保留占用但立即关闭 SSE，不发布任何终态确认。
            if state.closed_without_terminal:
                return
            if await request.is_disconnected():
                state.disconnect()
                return
            if state.events.terminal_filled:
                remaining = state.events.terminal_remaining()
                if remaining is not None and remaining <= 0:
                    return
            event = state.events.take()
            if event is None and state.events.terminal_filled:
                event = state.events.take_terminal()
            if event is not None:
                yield event.payload
                last_emit = monotonic()
                if event.name in {"done", "error"}:
                    return
                continue
            now = monotonic()
            idle = now - last_emit
            if idle >= SSE_HEARTBEAT_SECONDS:
                # 心跳不刷新运行预算，只遵守同一发送时限。
                yield SSE_HEARTBEAT
                last_emit = monotonic()
            else:
                await asyncio.sleep(
                    min(0.02, max(0.001, SSE_HEARTBEAT_SECONDS - idle))
                )

    return generate()


class RunStream(StreamingResponse):
    def __init__(self, state: RunState, body: AsyncIterator[bytes]):
        super().__init__(body, media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Session-ID": str(state.session_id),
            "X-Operation-ID": state.operation_id,
            "X-Run-ID": str(state.run_id),
            "X-Request-Entry-ID": state.request_entry_id,
        })
        self.state = state

    async def __call__(self, scope, receive, send):
        # 在真实 ASGI send 边界施加单次发送时限；终态排空期间动态取剩余总时限的较小值。
        async def bounded_send(message):
            pending = asyncio.ensure_future(send(message))
            started = monotonic()
            try:
                while True:
                    timeout = SSE_SEND_WAIT_SECONDS - (monotonic() - started)
                    remaining = self.state.events.terminal_remaining()
                    if remaining is not None:
                        timeout = min(timeout, remaining)
                    if timeout <= 0:
                        raise TimeoutError("SSE 发送超过时限")
                    done, _ = await asyncio.wait(
                        {pending}, timeout=min(0.05, timeout)
                    )
                    if done:
                        await pending
                        return
            finally:
                if not pending.done():
                    pending.cancel()

        try:
            await super().__call__(scope, receive, bounded_send)
        finally:
            # 断连回调只发出取消信号并关闭 Steering；已提交终态与名额由 coordinate 独占管理。
            self.state.disconnect()
