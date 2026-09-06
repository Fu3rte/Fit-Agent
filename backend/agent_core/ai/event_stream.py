"""Ported from pi-package/ai/src/utils/event-stream.ts @ pi snapshot 9841914c71a74d81abe07f751aefd271fd924e63

Generic event stream for async iteration (plan §4.2):
- ``push()`` is synchronous and never blocks on consumers; the internal
  ``asyncio.Queue`` is unbounded (never swap in a bounded queue — a slow or
  absent consumer must not stall the producer).
- The final result lives in an independent future, separate from the event
  queue. Even with zero event consumers, ``await result()`` resolves once a
  complete event arrives or ``end()`` is called. done/error both settle it.
- Consumer iteration never drives execution; the low-level queue is for
  observation only.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Generic, TypeVar

from .types import AssistantMessage, AssistantMessageEvent, DoneEvent, ErrorEvent

E = TypeVar("E")
R = TypeVar("R")

_END = object()
"""Sentinel appended to the queue by end(); wakes idle consumers."""


class EventStream(Generic[E, R]):
    """TS EventStream<T, R>.

    The final result is cached in ``_result`` so late ``result()`` callers
    resolve without depending on the future being created; the future itself
    is created lazily inside a running loop (TS creates the promise in the
    constructor, which Python cannot do outside an event loop).
    """

    def __init__(
        self,
        is_complete: Callable[[E], bool],
        extract_result: Callable[[E], R],
    ) -> None:
        self._is_complete = is_complete
        self._extract_result = extract_result
        self._queue: asyncio.Queue = asyncio.Queue()  # unbounded
        self._done = False
        self._end_signalled = False
        self._result: R | None = None
        self._result_set = False
        self._final: asyncio.Future[R] | None = None

    def _ensure_final(self) -> None:
        if self._final is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self._final = loop.create_future()

    def _settle(self, result: R) -> None:
        self._result = result
        self._result_set = True
        self._ensure_final()
        if self._final is not None and not self._final.done():
            self._final.set_result(result)

    def push(self, event: E) -> None:
        """Synchronous, non-blocking delivery (TS: array push / waiter wakeup)."""
        if self._done:
            return

        if self._is_complete(event):
            self._done = True
            self._settle(self._extract_result(event))

        # Deliver to waiting consumer or queue it.
        self._queue.put_nowait(event)

    def end(self, result: R | None = None) -> None:
        """TS end(result?). ``result=None`` means "no final result provided"
        (TS undefined); the future is only resolved when a result is given."""
        self._done = True
        if result is not None:
            self._settle(result)
        # Notify consumers that we're done.
        if not self._end_signalled:
            self._end_signalled = True
            self._queue.put_nowait(_END)

    def __aiter__(self) -> "EventStream[E, R]":
        return self

    async def __anext__(self) -> E:
        if self._queue.empty() and self._done:
            # TS: queue drained and done -> iterator returns.
            raise StopAsyncIteration
        item = await self._queue.get()
        if item is _END:
            raise StopAsyncIteration
        return item

    async def result(self) -> R:
        """TS result(): the independent final-result promise."""
        if self._result_set:
            return self._result  # type: ignore[return-value]
        self._ensure_final()
        if self._final is None:
            raise RuntimeError("EventStream.result() awaited outside an event loop")
        return await self._final


def _is_assistant_message_terminal(event: AssistantMessageEvent) -> bool:
    return isinstance(event, (DoneEvent, ErrorEvent))


def _extract_assistant_message_result(event: AssistantMessageEvent) -> AssistantMessage:
    if isinstance(event, DoneEvent):
        return event.message
    if isinstance(event, ErrorEvent):
        return event.error
    raise ValueError("Unexpected event type for final result")


class AssistantMessageEventStream(EventStream[AssistantMessageEvent, AssistantMessage]):
    """TS AssistantMessageEventStream: terminal events are `done` and `error`;
    the extracted result is the settled AssistantMessage in both cases (the
    error event carries an AssistantMessage, not an exception)."""

    def __init__(self) -> None:
        super().__init__(_is_assistant_message_terminal, _extract_assistant_message_result)


def create_assistant_message_event_stream() -> AssistantMessageEventStream:
    """Factory for AssistantMessageEventStream (for use in extensions)."""
    return AssistantMessageEventStream()
