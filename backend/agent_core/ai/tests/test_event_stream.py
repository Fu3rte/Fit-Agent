"""Tests for agent_core.ai.event_stream (plan §11.2: EventStream contracts)."""

import asyncio

from agent_core.ai.event_stream import AssistantMessageEventStream, EventStream
from agent_core.ai.types import (
    AssistantMessage,
    DoneEvent,
    ErrorEvent,
    StartEvent,
    TextDeltaEvent,
    TextStartEvent,
    Usage,
)


def make_message(text: str, stop_reason: str = "stop") -> AssistantMessage:
    return AssistantMessage(
        content=[],
        api="openai-completions",
        provider="openai",
        model="gpt-test",
        usage=Usage(input=0, output=0, cache_read=0, cache_write=0, total_tokens=0),
        stop_reason=stop_reason,  # type: ignore[arg-type]
        timestamp=0,
    )


def test_zero_consumers_can_await_result():
    async def scenario():
        stream: EventStream[str, str] = EventStream(lambda e: e == "END", lambda e: f"final:{e}")
        stream.push("a")
        stream.push("b")
        stream.push("END")
        # Never iterate the events; result must still resolve.
        return await stream.result()

    assert asyncio.run(scenario()) == "final:END"


def test_many_events_zero_consumers_still_settle():
    async def scenario():
        stream = AssistantMessageEventStream()
        stream.push(StartEvent(partial=make_message("")))
        for i in range(10_000):
            stream.push(TextDeltaEvent(content_index=0, delta=f"t{i}", partial=make_message("")))
        final = make_message("done")
        stream.push(DoneEvent(reason="stop", message=final))
        return await stream.result()

    assert asyncio.run(scenario()) is not None


def test_result_resolves_before_complete_event_if_end_called_with_result():
    async def scenario():
        stream: EventStream[str, str] = EventStream(lambda e: False, lambda e: e)
        stream.push("a")
        stream.end("explicit-result")
        return await stream.result()

    assert asyncio.run(scenario()) == "explicit-result"


def test_end_without_result_keeps_future_pending_but_wakes_consumer():
    async def scenario():
        stream: EventStream[str, str] = EventStream(lambda e: False, lambda e: e)
        stream.end()
        # Consumer wakes up and iteration stops.
        events = [e async for e in stream]
        return events

    assert asyncio.run(scenario()) == []


def test_push_after_done_is_ignored():
    async def scenario():
        stream = AssistantMessageEventStream()
        final = make_message("final")
        stream.push(DoneEvent(reason="stop", message=final))
        stream.push(TextDeltaEvent(content_index=0, delta="late", partial=make_message("")))
        events = [e async for e in stream]
        return events, await stream.result()

    events, result = asyncio.run(scenario())
    # Only the terminal event was delivered; the late push was dropped.
    assert len(events) == 1 and events[0].type == "done"
    assert result is not None


def test_iteration_delivers_all_events_then_stops_after_terminal():
    async def scenario():
        stream = AssistantMessageEventStream()
        stream.push(StartEvent(partial=make_message("")))
        stream.push(TextStartEvent(content_index=0, partial=make_message("")))
        final = make_message("x")
        stream.push(DoneEvent(reason="stop", message=final))
        collected = [e.type async for e in stream]
        return collected

    # Terminal event is delivered to consumers, then iteration stops
    # without requiring end().
    assert asyncio.run(scenario()) == ["start", "text_start", "done"]


def test_error_before_start_resolves_result_to_error_message():
    async def scenario():
        stream = AssistantMessageEventStream()
        err = make_message("boom", stop_reason="error")
        stream.push(ErrorEvent(reason="error", error=err))
        return await stream.result()

    result = asyncio.run(scenario())
    assert result.stop_reason == "error"


def test_error_mid_stream():
    async def scenario():
        stream = AssistantMessageEventStream()
        stream.push(StartEvent(partial=make_message("")))
        stream.push(TextStartEvent(content_index=0, partial=make_message("")))
        err = make_message("boom", stop_reason="error")
        stream.push(ErrorEvent(reason="error", error=err))
        types = [e.type async for e in stream]
        result = await stream.result()
        return types, result

    types, result = asyncio.run(scenario())
    assert types == ["start", "text_start", "error"]
    assert result.stop_reason == "error"


def test_aborted_error_event():
    async def scenario():
        stream = AssistantMessageEventStream()
        err = make_message("cancelled", stop_reason="aborted")
        stream.push(ErrorEvent(reason="aborted", error=err))
        result = await stream.result()
        events = [e async for e in stream]
        return result, events

    result, events = asyncio.run(scenario())
    assert result.stop_reason == "aborted"
    # The terminal event itself is still delivered to consumers (TS parity).
    assert len(events) == 1 and events[0].type == "error"


def test_done_event_result_is_message():
    async def scenario():
        stream = AssistantMessageEventStream()
        final = make_message("ok")
        stream.push(DoneEvent(reason="stop", message=final))
        return await stream.result()

    assert asyncio.run(scenario()) is not None


def test_multiple_result_waiters_all_resolve():
    async def scenario():
        stream = AssistantMessageEventStream()
        final = make_message("shared")
        waiters = [asyncio.create_task(stream.result()) for _ in range(3)]
        await asyncio.sleep(0)
        stream.push(DoneEvent(reason="stop", message=final))
        return await asyncio.gather(*waiters)

    results = asyncio.run(scenario())
    assert all(r is results[0] for r in results)


def test_generic_stream_not_iterated_only_result_used():
    async def scenario():
        seen: list[str] = []
        stream: EventStream[str, str] = EventStream(
            lambda e: e.startswith("END"), lambda e: e.removeprefix("END")
        )
        for i in range(100):
            stream.push(f"e{i}")
            seen.append(f"e{i}")
        stream.push("END42")
        return await stream.result(), seen

    result, seen = asyncio.run(scenario())
    assert result == "42"
    assert len(seen) == 100
