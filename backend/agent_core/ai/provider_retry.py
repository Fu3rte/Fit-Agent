"""Ported from pi-package/ai/src/utils/provider-retry.ts @ pi snapshot 9841914c71a74d81abe07f751aefd271fd924e63

Request-level provider retry: reproduces the retry policy of the pinned
OpenAI/Anthropic SDKs while making the backoff sleep interruptible. Callers
must invoke any SDK with ``max_retries=0`` and wrap the request with
:func:`retry_provider_request` (plan §8.1: default ``max_retries=0``).

Python mapping of TS concepts:
- TS ``AbortSignal`` → an :class:`asyncio.Event` (``abort``). The sleep is also
  cancellable by plain asyncio task cancellation; ``CancelledError`` always
  propagates (never swallowed, plan §8.4).
- TS ``Headers`` → a plain string-keyed mapping; lookup is case-insensitive
  like ``Headers.get``.
- TS generic ``Error`` raised on oversized server-requested delays →
  :class:`ServerRetryDelayError` (dedicated class, same message shape).
"""

from __future__ import annotations

import asyncio
import math
import random
import re
from collections.abc import Awaitable, Callable, Mapping
from email.utils import parsedate_to_datetime
from typing import Any, TypeGuard, TypeVar

DEFAULT_MAX_RETRY_DELAY_MS = 60_000

T = TypeVar("T")


class AbortError(Exception):
    """TS: an ``Error`` with ``name = "AbortError"`` ("Request aborted")."""


class ServerRetryDelayError(Exception):
    """TS: plain ``Error`` raised when the server-requested retry delay exceeds the cap."""


class ProviderError(Exception):
    """TS ProviderError: an error carrying an HTTP ``status`` and response ``headers``.

    Transport/HTTP client adapters should wrap their native exceptions into this
    type (or provide an equivalent with ``status``/``headers`` attributes) so the
    classifier can see them. TS duck-types on the two attributes; here the type
    is nominal.
    """

    def __init__(self, message: str, status: int | None = None, headers: Mapping[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.headers = headers


def is_provider_error(error: Any) -> TypeGuard[ProviderError]:
    # TS duck-types: error instanceof Error && "status" in error && "headers" in error,
    # with status number|undefined and headers Headers|undefined. The Python port is
    # nominal (ProviderError) — the attribute shape is enforced by the class.
    return isinstance(error, ProviderError)


def _get_header(headers: Mapping[str, str] | None, name: str) -> str | None:
    if headers is None:
        return None
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def is_retryable_provider_error(error: ProviderError) -> bool:
    """Mirrors the pinned OpenAI/Anthropic SDK retry policy; review when either SDK is upgraded."""
    should_retry = _get_header(error.headers, "x-should-retry")
    if should_retry == "true":
        return True
    if should_retry == "false":
        return False

    if error.status is None:
        return True
    return error.status == 408 or error.status == 409 or error.status == 429 or error.status >= 500


def _parse_float_prefix(value: str) -> float | None:
    # JS Number.parseFloat accepts a leading numeric prefix and ignores trailing
    # garbage ("250abc" -> 250); Python float() does not. Match JS semantics.
    match = re.match(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?", value.strip())
    if match is None:
        return None
    return float(match.group(0))


def validate_server_retry_delay_ms(
    delay_ms: float,
    max_retry_delay_ms: int | None,
    provider_error_message: str,
) -> float:
    max_delay_ms = max_retry_delay_ms if max_retry_delay_ms is not None else DEFAULT_MAX_RETRY_DELAY_MS
    if max_delay_ms > 0 and delay_ms > max_delay_ms:
        raise ServerRetryDelayError(
            f"Server requested {math.ceil(delay_ms / 1000)}s retry delay (max: {math.ceil(max_delay_ms / 1000)}s). "
            f"{provider_error_message}"
        )
    return delay_ms


def get_retry_delay_ms(error: ProviderError, retry_index: int, max_retry_delay_ms: int | None) -> float:
    retry_after_ms = _get_header(error.headers, "retry-after-ms")
    if retry_after_ms:
        value = _parse_float_prefix(retry_after_ms)
        if value is not None:
            return validate_server_retry_delay_ms(value, max_retry_delay_ms, str(error))

    retry_after = _get_header(error.headers, "retry-after")
    if retry_after:
        seconds = _parse_float_prefix(retry_after)
        if seconds is not None:
            delay_ms = seconds * 1000
        else:
            # TS: Date.parse(retryAfter) - Date.now(). An unparseable date yields NaN,
            # which survives the cap check and makes setTimeout fire immediately (0ms).
            try:
                when = parsedate_to_datetime(retry_after)
                import time

                delay_ms = when.timestamp() * 1000 - time.time() * 1000
            except (TypeError, ValueError):
                delay_ms = 0.0
        return validate_server_retry_delay_ms(delay_ms, max_retry_delay_ms, str(error))

    exponential_delay = min(0.5 * 2**retry_index, 8) * 1000
    return exponential_delay * (1 - random.random() * 0.25)


async def abortable_sleep(ms: float, abort: asyncio.Event | None = None) -> None:
    """Sleep ``ms`` milliseconds; raise :class:`AbortError` if ``abort`` is set.

    Also cancellable by plain asyncio task cancellation — ``CancelledError``
    propagates unchanged (plan §8.4: never swallow cancellation).
    """
    if abort is not None and abort.is_set():
        raise AbortError("Request aborted")

    delay_s = max(0.0, ms) / 1000.0  # TS: Math.max(0, ms); setTimeout treats <0 as 0
    if abort is None:
        await asyncio.sleep(delay_s)
        return

    waiter = asyncio.ensure_future(abort.wait())
    try:
        done, _ = await asyncio.wait({waiter}, timeout=delay_s)
    except asyncio.CancelledError:
        waiter.cancel()
        raise
    if waiter in done:
        raise AbortError("Request aborted")
    waiter.cancel()


async def retry_provider_request(
    request: Callable[[], Awaitable[T]],
    max_retries: int = 0,
    max_retry_delay_ms: int | None = None,
    abort: asyncio.Event | None = None,
) -> T:
    """Reproduce the retry behavior used by the OpenAI and Anthropic SDKs while
    making their backoff sleep interruptible.

    Their built-in retry timers ignore the request abort signal, so callers must
    invoke the SDK with ``max_retries: 0`` and wrap the request with this helper.
    Provider-requested delays above ``max_retry_delay_ms`` fail immediately
    (60 seconds by default); set it to zero to disable the limit.
    """
    retries_remaining = max_retries

    while True:
        try:
            # Each retry is a fresh SDK request, so X-Stainless-Retry-Count remains zero.
            return await request()
        except Exception as error:
            if abort is not None and abort.is_set():
                raise AbortError("Request aborted") from error
            if retries_remaining <= 0 or not is_provider_error(error):
                raise
            if not is_retryable_provider_error(error):
                raise

            retry_index = max_retries - retries_remaining
            retries_remaining -= 1
            await abortable_sleep(get_retry_delay_ms(error, retry_index, max_retry_delay_ms), abort)
