"""Tests for agent_core.ai.provider_retry (plan §8.1 / §11.2).

Contracts: SDK-mirror retry classification (x-should-retry, 408/409/429/5xx,
unknown status), Retry-After / Retry-After-MS parsing with server delay cap,
default max_retries=0, abort-event interruption, and clean CancelledError
propagation from asyncio cancellation.
"""

import asyncio
import time

from agent_core.ai.provider_retry import (
    DEFAULT_MAX_RETRY_DELAY_MS,
    AbortError,
    ProviderError,
    ServerRetryDelayError,
    abortable_sleep,
    get_retry_delay_ms,
    is_provider_error,
    is_retryable_provider_error,
    retry_provider_request,
)


def make_error(status: int | None = None, headers: dict[str, str] | None = None, message: str = "boom") -> ProviderError:
    return ProviderError(message, status=status, headers=headers)


# --- classification -----------------------------------------------------------


def test_is_provider_error_nominal():
    assert is_provider_error(ProviderError("x")) is True
    assert is_provider_error(ValueError("x")) is False


def test_x_should_retry_header_wins_over_status():
    assert is_retryable_provider_error(make_error(400, {"X-Should-Retry": "true"})) is True
    assert is_retryable_provider_error(make_error(429, {"x-should-retry": "false"})) is False


def test_unknown_status_is_retryable():
    # TS: status === undefined → retryable (transport-level failures)
    assert is_retryable_provider_error(make_error(None)) is True


def test_retryable_status_codes():
    for status in (408, 409, 429, 500, 502, 503, 504, 599):
        assert is_retryable_provider_error(make_error(status)) is True, status


def test_non_retryable_status_codes():
    for status in (400, 401, 403, 404, 418, 422):
        assert is_retryable_provider_error(make_error(status)) is False, status


# --- delay computation --------------------------------------------------------


def test_retry_after_ms_header():
    assert get_retry_delay_ms(make_error(429, {"retry-after-ms": "250"}), 0, None) == 250.0


def test_retry_after_ms_parses_numeric_prefix_like_js_parsefloat():
    # JS Number.parseFloat("250abc") === 250
    assert get_retry_delay_ms(make_error(429, {"retry-after-ms": "250abc"}), 0, None) == 250.0


def test_retry_after_seconds():
    assert get_retry_delay_ms(make_error(429, {"retry-after": "2"}), 0, None) == 2000.0


def test_retry_after_http_date_in_future():
    from email.utils import formatdate

    headers = {"retry-after": formatdate(time.time() + 10, usegmt=True)}
    delay = get_retry_delay_ms(make_error(429, headers), 0, None)
    assert 9000 < delay <= 10500


def test_retry_after_unparseable_date_falls_back_to_immediate():
    # TS: Date.parse garbage → NaN → setTimeout fires immediately (0ms)
    assert get_retry_delay_ms(make_error(429, {"retry-after": "not a date"}), 0, None) == 0.0


def test_server_delay_over_default_cap_raises():
    try:
        get_retry_delay_ms(make_error(429, {"retry-after-ms": "120000"}), 0, None)
        raise AssertionError("expected ServerRetryDelayError")
    except ServerRetryDelayError as error:
        assert "Server requested 120s retry delay (max: 60s)" in str(error)


def test_server_delay_cap_zero_disables_limit():
    delay = get_retry_delay_ms(make_error(429, {"retry-after-ms": "120000"}), 0, 0)
    assert delay == 120000.0


def test_exponential_backoff_with_jitter_bounds():
    # attempt 0: min(0.5 * 2^0, 8) * 1000 = 500 → jitter ∈ [375, 500)
    for _ in range(20):
        delay = get_retry_delay_ms(make_error(500), 0, None)
        assert 375 <= delay < 500
    # attempt 5: min(0.5 * 2^5, 8) * 1000 = 8000 → jitter ∈ [6000, 8000)
    for _ in range(20):
        delay = get_retry_delay_ms(make_error(500), 5, None)
        assert 6000 <= delay < 8000


def test_default_max_retry_delay_constant():
    assert DEFAULT_MAX_RETRY_DELAY_MS == 60_000


# --- retry_provider_request (async) -------------------------------------------


def run(coro):
    return asyncio.run(coro)


def test_default_max_retries_is_zero():
    calls = []

    async def request():
        calls.append(1)
        raise ProviderError("nope", status=429)

    try:
        run(retry_provider_request(request))
        raise AssertionError("expected ProviderError")
    except ProviderError:
        pass
    assert len(calls) == 1  # no retry on the default policy


def test_retries_transient_errors_until_success():
    calls = []

    async def request():
        calls.append(1)
        if len(calls) < 3:
            raise ProviderError("slow down", status=429, headers={"retry-after-ms": "1"})
        return "ok"

    assert run(retry_provider_request(request, max_retries=5)) == "ok"
    assert len(calls) == 3


def test_non_retryable_error_raises_immediately():
    calls = []

    async def request():
        calls.append(1)
        raise ProviderError("bad key", status=401)

    try:
        run(retry_provider_request(request, max_retries=5))
        raise AssertionError("expected ProviderError")
    except ProviderError as error:
        assert error.status == 401
    assert len(calls) == 1


def test_x_should_retry_true_retries_even_on_400():
    calls = []

    async def request():
        calls.append(1)
        if len(calls) < 2:
            raise ProviderError("weird", status=400, headers={"x-should-retry": "true"})
        return "ok"

    assert run(retry_provider_request(request, max_retries=2)) == "ok"
    assert len(calls) == 2


def test_plain_exception_is_not_wrapped_or_retried():
    calls = []

    async def request():
        calls.append(1)
        raise ValueError("not a provider error")

    try:
        run(retry_provider_request(request, max_retries=3))
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    assert len(calls) == 1


def test_abort_event_set_before_call_aborts_without_calling():
    # TS contract: the abort check happens on the error path; a succeeding request
    # returns normally. Here the first request fails retryably, so the set event
    # converts the failure into AbortError.
    calls = []
    abort = asyncio.Event()
    abort.set()

    async def request():
        calls.append(1)
        raise ProviderError("slow down", status=429, headers={"retry-after-ms": "1"})

    try:
        run(retry_provider_request(request, max_retries=3, abort=abort))
        raise AssertionError("expected AbortError")
    except AbortError as error:
        assert str(error) == "Request aborted"
    assert len(calls) == 1


def test_succeeding_request_ignores_abort_event():
    # TS parity: no exception raised → no abort check on the success path.
    abort = asyncio.Event()
    abort.set()

    async def request():
        return "ok"

    assert run(retry_provider_request(request, abort=abort)) == "ok"


def test_abort_event_set_after_failure_interrupts_backoff():
    calls = []
    abort = asyncio.Event()

    async def request():
        calls.append(1)
        raise ProviderError("slow down", status=429, headers={"retry-after-ms": "5000"})

    async def set_later():
        await asyncio.sleep(0.02)
        abort.set()

    async def scenario():
        asyncio.ensure_future(set_later())
        started = time.monotonic()
        try:
            await retry_provider_request(request, max_retries=3, abort=abort)
            return None
        except AbortError:
            return time.monotonic() - started

    elapsed = run(scenario())
    assert elapsed is not None and elapsed < 1.0  # did not wait the full 5s
    assert len(calls) == 1


def test_cancellation_propagates_cleanly():
    calls = []

    async def request():
        calls.append(1)
        raise ProviderError("slow down", status=429, headers={"retry-after-ms": "5000"})

    async def scenario():
        task = asyncio.ensure_future(retry_provider_request(request, max_retries=3))
        await asyncio.sleep(0.02)  # now inside the backoff sleep
        task.cancel()
        try:
            await task
            return "no-cancel"
        except asyncio.CancelledError:
            return "cancelled"

    assert run(scenario()) == "cancelled"
    assert len(calls) == 1


def test_abortable_sleep_already_set_event():
    async def scenario():
        event = asyncio.Event()
        event.set()
        try:
            await abortable_sleep(1000, event)
            return "slept"
        except AbortError:
            return "aborted"

    assert run(scenario()) == "aborted"


def test_abortable_sleep_completes_without_abort():
    async def scenario():
        started = time.monotonic()
        await abortable_sleep(10)
        return time.monotonic() - started

    assert run(scenario()) >= 0.005
