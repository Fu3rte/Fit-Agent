"""Ported from pi-package/ai/src/utils/retry.ts @ pi snapshot 9841914c71a74d81abe07f751aefd271fd924e63

Whole-assistant-turn retry: bounded attempts with exponential backoff
(``base_delay_ms * 2**(attempt-1)``) on transient provider/transport errors.
Quota/billing/subscription-limit errors are never retried; aborted messages are
terminal and never retried (plan §8.1).

Python mapping of TS concepts:
- TS ``AbortSignal`` → an :class:`asyncio.Event` (``signal``). Backoff sleeps are
  also cancellable by plain asyncio task cancellation; ``CancelledError``
  propagates unchanged (plan §8.4).
- TS callbacks may be sync or async (``void | Promise<void>``); both are awaited.
- ``on_retry_finished`` is always invoked with 3 positional args in Python
  (``success, attempt, final_error``); TS omits the third when undefined.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from .types import AssistantMessage


def _build_pattern(patterns: tuple[str, ...]) -> re.Pattern[str]:
    # TS: new RegExp(patterns.join("|"), "i")
    return re.compile("|".join(patterns), re.IGNORECASE)


_NON_RETRYABLE_PROVIDER_LIMIT_ERROR_PATTERN = _build_pattern(
    (
        # OpenCode Go/free-tier limits returned as 429 JSON error types by OpenCode's
        # Zen API. These are subscription/account limits, not transient throttles.
        r"GoUsageLimitError",
        r"FreeUsageLimitError",
        # OpenCode Go subscription-limit text asks users to enable available-balance
        # usage after rolling/weekly/monthly limits are reached.
        r"Monthly usage limit reached",
        r"available balance",
        # Generic quota/budget/billing exhaustion. `insufficient_quota` is OpenAI's
        # quota/billing error code; the other strings cover common gateway wording.
        r"insufficient_quota",
        r"out of budget",
        r"quota exceeded",
        r"billing",
    )
)

_RETRYABLE_PROVIDER_ERROR_PATTERN = _build_pattern(
    (
        # Generic provider load, HTTP status, and server-side transient failures.
        r"overloaded",
        r"rate.?limit",
        r"too many requests",
        r"429",
        r"500",
        r"502",
        r"503",
        r"504",
        r"524",
        r"service.?unavailable",
        r"server.?error",
        r"internal.?error",
        # Wrapper/provider text for transient upstream failures, including OpenRouter
        # "Provider returned error" responses (#2264).
        r"provider.?returned.?error",
        r"exceeded request buffer limit while retrying upstream",
        # Network, proxy, and fetch transport failures. This includes OpenAI Codex
        # raw-fetch failures such as "upstream connect", "connection refused", and
        # "reset before headers" (#733), plus OpenRouter connection drops (#3317).
        r"network.?error",
        r"connection.?error",
        r"connection.?refused",
        r"connection.?lost",
        r"other side closed",
        r"fetch failed",
        r"getaddrinfo",
        r"ENOTFOUND",
        r"EAI_AGAIN",
        r"upstream.?connect",
        r"reset before headers",
        r"socket hang up",
        r"socket connection was closed",
        r"timed? out",
        r"timeout",
        r"terminated",
        # WebSocket transports can report close/error text instead of HTTP/fetch text.
        r"websocket.?closed",
        r"websocket.?error",
        # Premature stream endings from SDKs and transports. Anthropic can throw
        # "stream ended without ..." and "Anthropic stream ended before message_stop"
        # (#4433); Bedrock/Smithy can throw an HTTP/2 no-response error (#3594).
        r"ended without",
        r"stream ended before message_stop",
        r"stream ended before a terminal response event",
        r"http2 request did not get a response",
        # Provider-requested retry delay cap failures should flow through the outer
        # retry policy so callers can surface/abort the backoff (#1123).
        r"retry delay",
        # Explicit retry guidance emitted mid-stream by OpenAI Responses and Bedrock
        # stream exceptions (#6019).
        r"you can retry your request",
        r"try your request again",
        r"please retry your request",
        # gRPC based providers (e.g. NVIDIA NIM)
        r"ResourceExhausted",
    )
)


@dataclass
class RetryPolicy:
    """Retry policy: bounded attempts with exponential backoff (``base_delay_ms * 2**(attempt-1)``)."""

    enabled: bool
    max_retries: int
    """Max retry attempts (0 = no retries). The initial call never counts as a retry."""
    base_delay_ms: float
    """Base delay in ms. Per-attempt delay is ``base_delay_ms * 2**(attempt-1)`` before jitter."""


SyncOrAsyncCallback = Callable[..., "Awaitable[None] | None"]


@dataclass
class RetryCallbacks:
    """Optional callbacks emitted by :func:`retry_assistant_call` around each retry.

    Each callback may be sync or async; its return value is awaited when awaitable.
    ``on_retry_finished`` is called with ``(success, attempt, final_error)``.
    """

    on_retry_scheduled: SyncOrAsyncCallback | None = None
    """Emitted before the backoff sleep of each retry attempt (1-indexed):
    ``(attempt, max_attempts, delay_ms, error_message)``."""
    on_retry_attempt_start: SyncOrAsyncCallback | None = None
    """Emitted after the backoff sleep, immediately before the retried call starts."""
    on_retry_finished: SyncOrAsyncCallback | None = None
    """Emitted once when the loop ends: success if a later call completed normally:
    ``(success, attempt, final_error | None)``."""


class RetryCallbacksLike(Protocol):
    """Structural type for the ``callbacks`` argument (TS ``RetryCallbacks`` is an
    interface): any object exposing the three optional callbacks satisfies it.

    Declared as read-only properties: :func:`retry_assistant_call` only reads the
    callbacks, and a mutable-attribute protocol would structurally reject
    implementations that define them as methods (a concrete method signature
    cannot accept assignment of ``SyncOrAsyncCallback | None``)."""

    @property
    def on_retry_scheduled(self) -> SyncOrAsyncCallback | None: ...

    @property
    def on_retry_attempt_start(self) -> SyncOrAsyncCallback | None: ...

    @property
    def on_retry_finished(self) -> SyncOrAsyncCallback | None: ...


class RetrySleepAbortError(Exception):
    def __init__(self) -> None:
        super().__init__("Aborted")


async def _invoke(callback: SyncOrAsyncCallback | None, *args: object) -> None:
    if callback is None:
        return
    result = callback(*args)
    if inspect.isawaitable(result):
        await result


async def _sleep(ms: float, signal: asyncio.Event | None = None) -> None:
    if signal is not None and signal.is_set():
        raise RetrySleepAbortError()
    delay_s = max(0.0, ms) / 1000.0
    if signal is None:
        await asyncio.sleep(delay_s)
        return
    waiter = asyncio.ensure_future(signal.wait())
    try:
        done, _ = await asyncio.wait({waiter}, timeout=delay_s)
    except asyncio.CancelledError:
        waiter.cancel()
        raise
    if waiter in done:
        raise RetrySleepAbortError()
    waiter.cancel()


def is_retryable_assistant_error(message: AssistantMessage) -> bool:
    """Classify whether a failed assistant message looks like a transient provider
    or transport error, so callers can decide if the last assistant turn should be
    restarted.

    This does not implement retry policy. Callers should first handle context
    overflow separately, then apply their own retry budget, backoff, and reporting
    before restarting the assistant turn.
    """
    if message.stop_reason != "error" or not message.error_message:
        return False
    error_message = message.error_message
    if _NON_RETRYABLE_PROVIDER_LIMIT_ERROR_PATTERN.search(error_message):
        return False
    return bool(_RETRYABLE_PROVIDER_ERROR_PATTERN.search(error_message))


async def retry_assistant_call(
    produce: Callable[[], Awaitable[AssistantMessage]],
    policy: RetryPolicy | None,
    signal: asyncio.Event | None = None,
    callbacks: RetryCallbacksLike | None = None,
) -> AssistantMessage:
    """Run a single assistant-producing call with bounded retry on transient errors.

    Behavior:
    - A successful response is returned immediately. Aborts are terminal and never
      retried, but reported as unsuccessful if they happen after a retry was
      scheduled. Aborts during the backoff sleep are normalized to an aborted
      ``AssistantMessage`` too, so callers do not need to care when cancellation
      happened.
    - A non-retryable error (per :func:`is_retryable_assistant_error`, including
      quota/billing exhaustion) is returned immediately so deterministic errors
      fail fast.
    - Otherwise retries up to ``max_retries`` times with exponential backoff,
      emitting ``on_retry_scheduled`` before each sleep, ``on_retry_attempt_start``
      after each sleep before the retried call starts, and ``on_retry_finished``
      once at the end (whether the loop ends in success, exhausted retries, or an
      aborted backoff).

    When ``policy`` is None or disabled, the first response is returned unchanged
    (equivalent to calling ``produce()`` directly).
    """
    max_attempts = policy.max_retries if policy is not None and policy.enabled else 0
    scheduled = callbacks.on_retry_scheduled if callbacks is not None else None
    attempt_start = callbacks.on_retry_attempt_start if callbacks is not None else None
    finished = callbacks.on_retry_finished if callbacks is not None else None

    attempt = 0
    last_retry: tuple[int, str] | None = None  # (attempt, error_message)
    while True:
        response = await produce()

        # Abort: terminal but not successful. Never retry an aborted message.
        if response.stop_reason == "aborted":
            if last_retry is not None:
                await _invoke(finished, False, last_retry[0], None)
            return response

        # Success: non-error, non-abort responses return as-is.
        if response.stop_reason != "error":
            if last_retry is not None:
                await _invoke(finished, True, last_retry[0], None)
            return response

        # Non-retryable, or budget exhausted: return the final error message.
        if attempt >= max_attempts or not is_retryable_assistant_error(response):
            if last_retry is not None:
                await _invoke(finished, False, last_retry[0], response.error_message)
            return response

        attempt += 1
        last_retry = (attempt, response.error_message or "Unknown error")
        assert policy is not None  # max_attempts > 0 implies an enabled policy
        delay_ms = policy.base_delay_ms * 2 ** (attempt - 1)
        await _invoke(scheduled, attempt, max_attempts, delay_ms, last_retry[1])

        # Normalize aborts during retry backoff to the same AssistantMessage shape as
        # provider stream aborts, so callers do not need to care when cancellation happened.
        try:
            await _sleep(delay_ms, signal)
        except RetrySleepAbortError:
            await _invoke(finished, False, attempt, last_retry[1])
            # TS: const { errorMessage: _, ...rest } = response; stopReason = "aborted"
            response.error_message = None
            response.stop_reason = "aborted"
            return response
        await _invoke(attempt_start)
