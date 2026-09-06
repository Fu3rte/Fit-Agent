"""Tests for agent_core.ai.assistant_retry (plan §8.1 / §11.2).

Contracts: whole-turn retry only for transient errors (quota/billing never
retried), aborted messages terminal, abort during backoff normalized to an
aborted AssistantMessage with errorMessage removed, exponential backoff
``base_delay_ms * 2**(attempt-1)``, callback ordering, and CancelledError
propagation from task cancellation.
"""

import asyncio

from agent_core.ai.assistant_retry import (
    RetryCallbacks,
    RetryPolicy,
    RetrySleepAbortError,
    is_retryable_assistant_error,
    retry_assistant_call,
)
from agent_core.ai.types import AssistantMessage, TextContent, Usage


def make_message(stop_reason: str, error_message: str | None = None, model: str = "m") -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text="partial")],
        api="openai-completions",
        provider="p",
        model=model,
        usage=Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2),
        stop_reason=stop_reason,  # type: ignore[arg-type]
        timestamp=0,
        error_message=error_message,
    )


def make_policy(enabled: bool = True, max_retries: int = 2, base_delay_ms: float = 1.0) -> RetryPolicy:
    return RetryPolicy(enabled=enabled, max_retries=max_retries, base_delay_ms=base_delay_ms)


class ScriptedProducer:
    def __init__(self, responses: list[AssistantMessage]):
        self.responses = list(responses)
        self.calls = 0

    async def __call__(self) -> AssistantMessage:
        self.calls += 1
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)


class Recorder(RetryCallbacks):
    def __init__(self) -> None:
        self.scheduled: list[tuple[int, int, float, str]] = []
        self.attempt_starts: list[bool] = []
        self.finished: list[tuple[bool, int, str | None]] = []

    def on_retry_scheduled(self, attempt, max_attempts, delay_ms, error_message):
        self.scheduled.append((attempt, max_attempts, delay_ms, error_message))

    def on_retry_attempt_start(self):
        self.attempt_starts.append(True)

    def on_retry_finished(self, success, attempt, final_error=None):
        self.finished.append((success, attempt, final_error))


# --- classification ------------------------------------------------------------


def test_retryable_transient_errors():
    for message_text in (
        "provider overloaded",
        "HTTP 429 received",
        "rate limit hit",
        "500 Internal Server Error",
        "connection refused",
        "fetch failed",
        "socket hang up",
        "request timed out",
        "provider returned error",
        "stream ended before message_stop",
    ):
        assert is_retryable_assistant_error(make_message("error", message_text)) is True, message_text


def test_non_retryable_quota_and_billing():
    for message_text in (
        "insufficient_quota: you have exceeded your billing limit",
        "out of budget",
        "quota exceeded",
        "billing failure",
        "GoUsageLimitError",
        "FreeUsageLimitError",
        "Monthly usage limit reached",
        "please add available balance",
    ):
        assert is_retryable_assistant_error(make_message("error", message_text)) is False, message_text


def test_non_retryable_pattern_wins_over_retryable_text():
    # Contains both a non-retryable ("quota exceeded") and retryable ("429") marker
    assert is_retryable_assistant_error(make_message("error", "quota exceeded: 429")) is False


def test_unknown_error_text_is_not_retryable():
    assert is_retryable_assistant_error(make_message("error", "something totally different")) is False


def test_only_error_stop_reason_is_classified():
    assert is_retryable_assistant_error(make_message("stop")) is False
    assert is_retryable_assistant_error(make_message("aborted")) is False
    assert is_retryable_assistant_error(make_message("error", None)) is False


# --- retry_assistant_call -------------------------------------------------------


def test_policy_disabled_returns_first_response():
    producer = ScriptedProducer([make_message("error", "overloaded")])
    result = asyncio.run(retry_assistant_call(producer, None))
    assert result.stop_reason == "error"
    assert producer.calls == 1


def test_disabled_policy_equals_none():
    producer = ScriptedProducer([make_message("error", "overloaded")])
    result = asyncio.run(retry_assistant_call(producer, make_policy(enabled=False, max_retries=5)))
    assert result.stop_reason == "error"
    assert producer.calls == 1


def test_retryable_error_then_success():
    producer = ScriptedProducer([make_message("error", "overloaded"), make_message("stop")])
    recorder = Recorder()
    result = asyncio.run(retry_assistant_call(producer, make_policy(max_retries=2, base_delay_ms=1.0), None, recorder))
    assert result.stop_reason == "stop"
    assert producer.calls == 2
    assert recorder.scheduled == [(1, 2, 1.0, "overloaded")]
    assert recorder.attempt_starts == [True]
    assert recorder.finished == [(True, 1, None)]


def test_exponential_backoff_delays():
    producer = ScriptedProducer([
        make_message("error", "overloaded"),
        make_message("error", "overloaded"),
        make_message("error", "overloaded"),
    ])
    recorder = Recorder()
    asyncio.run(retry_assistant_call(producer, make_policy(max_retries=3, base_delay_ms=100.0), None, recorder))
    assert [s[2] for s in recorder.scheduled] == [100.0, 200.0, 400.0]
    assert [s[0] for s in recorder.scheduled] == [1, 2, 3]
    assert producer.calls == 4
    assert recorder.finished == [(False, 3, "overloaded")]


def test_exhausted_retries_return_final_error():
    producer = ScriptedProducer([make_message("error", "overloaded")])
    recorder = Recorder()
    result = asyncio.run(retry_assistant_call(producer, make_policy(max_retries=2, base_delay_ms=1.0), None, recorder))
    assert result.stop_reason == "error"
    assert result.error_message == "overloaded"
    assert producer.calls == 3
    assert recorder.finished == [(False, 2, "overloaded")]


def test_non_retryable_error_fails_fast():
    producer = ScriptedProducer([make_message("error", "insufficient_quota")])
    recorder = Recorder()
    result = asyncio.run(retry_assistant_call(producer, make_policy(max_retries=5, base_delay_ms=1.0), None, recorder))
    assert result.stop_reason == "error"
    assert result.error_message == "insufficient_quota"
    assert producer.calls == 1
    assert recorder.scheduled == []
    assert recorder.finished == []  # no retry ever scheduled → no finished callback


def test_aborted_message_is_terminal_and_never_retried():
    producer = ScriptedProducer([make_message("aborted")])
    recorder = Recorder()
    result = asyncio.run(retry_assistant_call(producer, make_policy(max_retries=5, base_delay_ms=1.0), None, recorder))
    assert result.stop_reason == "aborted"
    assert producer.calls == 1
    assert recorder.finished == []


def test_abort_during_backoff_normalizes_to_aborted_message():
    producer = ScriptedProducer([make_message("error", "overloaded", model="original-model")])
    recorder = Recorder()
    signal = asyncio.Event()

    async def set_later():
        await asyncio.sleep(0.02)
        signal.set()

    async def scenario():
        asyncio.ensure_future(set_later())
        return await retry_assistant_call(
            producer, make_policy(max_retries=2, base_delay_ms=10_000.0), signal, recorder
        )

    result = asyncio.run(scenario())
    assert result.stop_reason == "aborted"
    assert result.error_message is None  # TS removes errorMessage from the aborted copy
    assert result.model == "original-model"
    assert producer.calls == 1
    assert recorder.finished == [(False, 1, "overloaded")]


def test_abort_before_backoff_normalizes_immediately():
    producer = ScriptedProducer([make_message("error", "overloaded")])
    signal = asyncio.Event()
    signal.set()

    async def scenario():
        return await retry_assistant_call(producer, make_policy(max_retries=2, base_delay_ms=10_000.0), signal)

    result = asyncio.run(scenario())
    assert result.stop_reason == "aborted"
    assert result.error_message is None
    assert producer.calls == 1


def test_task_cancellation_propagates_not_normalized():
    producer = ScriptedProducer([make_message("error", "overloaded")])

    async def scenario():
        task = asyncio.ensure_future(
            retry_assistant_call(producer, make_policy(max_retries=2, base_delay_ms=10_000.0), None)
        )
        await asyncio.sleep(0.02)  # producer failed, now inside the backoff sleep
        task.cancel()
        try:
            await task
            return "no-cancel"
        except asyncio.CancelledError:
            return "cancelled"

    assert asyncio.run(scenario()) == "cancelled"
    assert producer.calls == 1


def test_async_and_sync_callbacks_both_supported():
    events: list[str] = []

    # Plain class (not a RetryCallbacks subclass): the dataclass __init__ would
    # shadow methods with None fields.
    class AsyncRecorder:
        async def on_retry_scheduled(self, attempt, max_attempts, delay_ms, error_message):
            events.append("scheduled")

        async def on_retry_attempt_start(self):
            events.append("start")

        async def on_retry_finished(self, success, attempt, final_error=None):
            events.append("finished")

    producer = ScriptedProducer([make_message("error", "overloaded"), make_message("stop")])
    result = asyncio.run(
        retry_assistant_call(producer, make_policy(max_retries=1, base_delay_ms=1.0), None, AsyncRecorder())
    )
    assert result.stop_reason == "stop"
    assert events == ["scheduled", "start", "finished"]


def test_sync_callbacks_supported():
    events: list[str] = []

    class SyncCallbacks:
        def on_retry_scheduled(self, attempt, max_attempts, delay_ms, error_message):
            events.append("scheduled")

        def on_retry_attempt_start(self):
            events.append("start")

        def on_retry_finished(self, success, attempt, final_error=None):
            events.append("finished")

    producer = ScriptedProducer([make_message("error", "overloaded"), make_message("stop")])
    result = asyncio.run(
        retry_assistant_call(producer, make_policy(max_retries=1, base_delay_ms=1.0), None, SyncCallbacks())
    )
    assert result.stop_reason == "stop"
    assert events == ["scheduled", "start", "finished"]


def test_retry_sleep_abort_error_shape():
    assert str(RetrySleepAbortError()) == "Aborted"
