"""Golden-contract tests for agent_core.ai.types (plan §4.1 / §11.2).

Focus: Usage optional-field serialization differences (both-missing /
one-side / explicit-0), camelCase wire key mapping, no JSON null for optional
fields, and message/content/event round-trips.
"""

import json

from agent_core.ai.types import (
    AssistantContent,
    AssistantMessage,
    DeferredHandle,
    DoneEvent,
    ErrorEvent,
    ImageContent,
    TextContent,
    TextDeltaEvent,
    ThinkingContent,
    ToolCall,
    ToolCallEndEvent,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserMessage,
    message_from_dict,
    usage_from_dict,
)


def make_usage(
    *,
    input: int = 100,
    output: int = 50,
    cache_read: int = 10,
    cache_write: int = 20,
    total_tokens: int = 170,
    reasoning: int | None = None,
    cache_write_1h: int | None = None,
    cost: UsageCost | None = None,
) -> Usage:
    return Usage(
        input=input,
        output=output,
        cache_read=cache_read,
        cache_write=cache_write,
        total_tokens=total_tokens,
        reasoning=reasoning,
        cache_write_1h=cache_write_1h,
        cost=cost if cost is not None else UsageCost(),
    )


def make_assistant(
    *,
    content: list[AssistantContent] | None = None,
    api: str = "openai-completions",
    provider: str = "deepseek",
    model: str = "deepseek-chat",
    usage: Usage | None = None,
    stop_reason: str = "stop",
    timestamp: int = 1700000000000,
    **extra,
) -> AssistantMessage:
    msg = AssistantMessage(
        content=list(content) if content is not None else [TextContent(text="hi")],
        api=api,
        provider=provider,
        model=model,
        usage=usage if usage is not None else make_usage(),
        stop_reason=stop_reason,  # type: ignore[arg-type]
        timestamp=timestamp,
    )
    for key, value in extra.items():
        setattr(msg, key, value)
    return msg


class TestUsageOptionalFieldSerialization:
    def test_both_optional_missing_keys_absent(self):
        d = make_usage().to_dict()
        assert "reasoning" not in d
        assert "cacheWrite1h" not in d
        assert "reasoning" not in json.dumps(d)
        assert "cacheWrite1h" not in json.dumps(d)

    def test_one_side_present_only_that_key_emitted(self):
        d = make_usage(reasoning=7).to_dict()
        assert d["reasoning"] == 7
        assert "cacheWrite1h" not in d

        d2 = make_usage(cache_write_1h=3).to_dict()
        assert d2["cacheWrite1h"] == 3
        assert "reasoning" not in d2

    def test_explicit_zero_is_preserved(self):
        d = make_usage(reasoning=0, cache_write_1h=0).to_dict()
        assert d["reasoning"] == 0
        assert d["cacheWrite1h"] == 0

    def test_no_json_null_anywhere(self):
        msg = make_assistant().to_dict()
        assert "null" not in json.dumps(msg)

    def test_usage_subsets_not_double_counted_in_total_tokens(self):
        # reasoning is a subset of output; cacheWrite1h a subset of cacheWrite.
        # total_tokens must be the provider-reported value, never re-derived by
        # adding the subsets again.
        u = Usage(input=100, output=50, cache_read=10, cache_write=20, total_tokens=170, reasoning=50, cache_write_1h=20)
        assert u.to_dict()["totalTokens"] == 170

    def test_camel_case_keys(self):
        d = make_usage().to_dict()
        assert list(d) == ["input", "output", "cacheRead", "cacheWrite", "totalTokens", "cost"]

    def test_usage_round_trip(self):
        u = Usage(
            input=100,
            output=50,
            cache_read=10,
            cache_write=20,
            total_tokens=170,
            reasoning=0,
            cache_write_1h=5,
            cost=UsageCost(input=0.1, output=0.2, cache_read=0.01, cache_write=0.02, total=0.33),
        )
        assert usage_from_dict(u.to_dict()) == u
        # optional-absent round trip
        u2 = make_usage()
        assert usage_from_dict(u2.to_dict()) == u2


class TestMessageCamelCaseWire:
    def test_assistant_message_keys(self):
        msg = make_assistant(
            provider_thinking_level="high",
            error_message="boom",
            raw_stop_reason="max_tokens",
            response_model="deepseek-v3",
            response_id="resp_1",
            end_turn=True,
        )
        d = msg.to_dict()
        assert d["stopReason"] == "stop"
        assert d["providerThinkingLevel"] == "high"
        assert d["errorMessage"] == "boom"
        assert d["rawStopReason"] == "max_tokens"
        assert d["responseModel"] == "deepseek-v3"
        assert d["responseId"] == "resp_1"
        assert d["endTurn"] is True
        # optional fields stay absent, never null
        assert "deferred" not in d and "diagnostics" not in d

    def test_tool_result_message_keys(self):
        msg = ToolResultMessage(
            tool_call_id="call_1",
            tool_name="get_weather",
            content=[TextContent(text="22C")],
            is_error=False,
            timestamp=1700000000001,
        )
        d = msg.to_dict()
        assert d["role"] == "toolResult"
        assert d["toolCallId"] == "call_1"
        assert d["toolName"] == "get_weather"
        assert d["isError"] is False
        assert "details" not in d and "usage" not in d and "addedToolNames" not in d

    def test_user_message_string_content(self):
        msg = UserMessage(content="hello", timestamp=1700000000000)
        assert msg.to_dict() == {"role": "user", "content": "hello", "timestamp": 1700000000000}

    def test_user_message_block_content(self):
        msg = UserMessage(
            content=[TextContent(text="look"), ImageContent(data="b64==", mime_type="image/png")],
            timestamp=1700000000000,
        )
        d = msg.to_dict()
        assert d["content"][0] == {"type": "text", "text": "look"}
        assert d["content"][1] == {"type": "image", "data": "b64==", "mimeType": "image/png"}


class TestContentBlocks:
    def test_text_content(self):
        d = TextContent(text="x", text_signature="sig").to_dict()
        assert d == {"type": "text", "text": "x", "textSignature": "sig"}
        plain = TextContent(text="x").to_dict()
        assert "textSignature" not in plain

    def test_thinking_content(self):
        d = ThinkingContent(thinking="t", thinking_signature="s", redacted=True).to_dict()
        assert d == {"type": "thinking", "thinking": "t", "thinkingSignature": "s", "redacted": True}
        # absent optional -> omitted
        d2 = ThinkingContent(thinking="t").to_dict()
        assert d2 == {"type": "thinking", "thinking": "t"}

    def test_tool_call(self):
        d = ToolCall(id="c1", name="f", arguments={"a": 1}, thought_signature="g").to_dict()
        assert d["type"] == "toolCall"
        assert d["thoughtSignature"] == "g"
        assert "namespace" not in d


class TestMessageRoundTrip:
    def test_assistant_full_round_trip(self):
        msg = make_assistant(
            content=[
                ThinkingContent(thinking="hmm", thinking_signature="", redacted=False),
                TextContent(text="answer"),
                ToolCall(id="c1", name="f", arguments={"x": [1, 2]}, thought_signature="gs"),
            ],
            deferred=DeferredHandle(provider="p", model_id="m", api="a", id="tok"),
        )
        restored = message_from_dict(msg.to_dict())
        assert restored == msg

    def test_tool_result_round_trip(self):
        msg = ToolResultMessage(
            tool_call_id="c1",
            tool_name="f",
            content=[TextContent(text="r")],
            is_error=True,
            timestamp=5,
            details={"k": "v"},
            usage=make_usage(),
            added_tool_names=["t2"],
        )
        assert message_from_dict(msg.to_dict()) == msg

    def test_unknown_role_raises(self):
        try:
            message_from_dict({"role": "system"})
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for unknown role")


class TestEventProtocol:
    def _partial(self) -> AssistantMessage:
        return make_assistant(content=[])

    def test_text_delta_camel_case(self):
        ev = TextDeltaEvent(content_index=0, delta="abc", partial=self._partial())
        d = ev.to_dict()
        assert d["type"] == "text_delta"
        assert d["contentIndex"] == 0
        assert d["delta"] == "abc"

    def test_toolcall_end_key(self):
        tc = ToolCall(id="c1", name="f", arguments={})
        ev = ToolCallEndEvent(content_index=1, tool_call=tc, partial=self._partial())
        d = ev.to_dict()
        assert d["toolCall"]["id"] == "c1"

    def test_done_event(self):
        msg = make_assistant(stop_reason="toolUse")
        d = DoneEvent(reason="toolUse", message=msg).to_dict()
        assert d == {"type": "done", "reason": "toolUse", "message": msg.to_dict()}

    def test_error_event(self):
        msg = make_assistant(stop_reason="error", error_message="x")
        d = ErrorEvent(reason="error", error=msg).to_dict()
        assert d["type"] == "error"
        assert d["error"]["stopReason"] == "error"
