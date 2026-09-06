"""Tests for agent_core.ai.estimate (plan §7.1 / §11.2).

Differential contracts: UTF-16 code-unit length counting (Chinese, emoji,
surrogate pairs), usage-preferred estimation with trailing estimate, prefix
invalidation by newer-timestamp messages, and Context-level tool prefix /
addedToolNames accounting.
"""

import json
import math

from agent_core.ai.estimate import (
    CHARS_PER_TOKEN,
    calculate_context_tokens,
    estimate_context_tokens,
    estimate_message_tokens,
    estimate_text_tokens,
    utf16_length,
)
from agent_core.ai.types import (
    AssistantMessage,
    Context,
    ImageContent,
    TextContent,
    ThinkingContent,
    Tool,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)


def make_assistant(
    usage: Usage,
    timestamp: int,
    stop_reason: str = "stop",
    content: list | None = None,
) -> AssistantMessage:
    return AssistantMessage(
        content=content if content is not None else [TextContent(text="ok")],
        api="openai-completions",
        provider="p",
        model="m",
        usage=usage,
        stop_reason=stop_reason,  # type: ignore[arg-type]
        timestamp=timestamp,
    )


def make_tool_result(timestamp: int, added_tool_names: list[str] | None = None) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id="c1",
        tool_name="t",
        content=[TextContent(text="r")],
        is_error=False,
        timestamp=timestamp,
        added_tool_names=added_tool_names,
    )


# --- UTF-16 code-unit counting (TS string.length semantics) -----------------


def test_utf16_length_basic():
    assert utf16_length("") == 0
    assert utf16_length("abc") == 3
    # BMP CJK: 1 unit per code point (Python len agrees here)
    assert utf16_length("中文") == 2


def test_utf16_length_surrogate_pairs():
    # Non-BMP emoji count as 2 units each (Python len counts 1)
    assert utf16_length("\U0001F44D") == 2  # 👍
    assert utf16_length("a\U0001F44Db") == 4
    # Family emoji: 3 non-BMP + 2 ZWJ = 3*2 + 2*1 = 8 units
    assert utf16_length("\U0001F468\u200D\U0001F469\u200D\U0001F467") == 8
    # Mathematical alphanumerics are non-BMP too
    assert utf16_length("\U0001D552") == 2


def test_utf16_length_matches_utf16_le_byte_length():
    for s in ("hello", "中文", "\U0001F44D mixed 中文", "\U0001F1E8\U0001F1F3"):
        assert utf16_length(s) == len(s.encode("utf-16-le")) // 2


# --- token estimation --------------------------------------------------------


def test_estimate_text_tokens_ceil():
    assert estimate_text_tokens("a" * 4) == 1
    assert estimate_text_tokens("a" * 5) == 2
    # 4 CJK chars = 4 UTF-16 units = 1 token
    assert estimate_text_tokens("中" * 4) == 1
    assert estimate_text_tokens("中" * 5) == 2


def test_calculate_context_tokens_prefers_total_tokens():
    usage = Usage(input=1, output=2, cache_read=3, cache_write=4, total_tokens=500)
    assert calculate_context_tokens(usage) == 500


def test_calculate_context_tokens_zero_total_falls_back_to_sum():
    usage = Usage(input=1, output=2, cache_read=3, cache_write=4, total_tokens=0)
    # TS: usage.totalTokens || sum — 0 is falsy
    assert calculate_context_tokens(usage) == 10


def test_estimate_message_tokens_user_string_and_image():
    assert estimate_message_tokens(UserMessage(content="a" * 8, timestamp=1)) == 2
    # Image content estimates at ESTIMATED_IMAGE_CHARS / CHARS_PER_TOKEN = 1200
    assert estimate_message_tokens(
        UserMessage(content=[TextContent(text="ab"), ImageContent(data="Zg==", mime_type="image/png")], timestamp=1)
    ) == math.ceil((2 + 4800) / CHARS_PER_TOKEN)


def test_estimate_message_tokens_assistant_blocks():
    message = make_assistant(
        usage=Usage(input=0, output=0, cache_read=0, cache_write=0, total_tokens=0),
        timestamp=1,
        content=[
            TextContent(text="a" * 8),  # 8 units
            ThinkingContent(thinking="b" * 4),  # 4 units
            ToolCall(id="c1", name="get_weather", arguments={"city": "北京"}),  # 11 + 13 units
        ],
    )
    # {"city":"北京"} compact = 13 UTF-16 units; name = 11 units
    assert utf16_length(json.dumps({"city": "北京"}, ensure_ascii=False, separators=(",", ":"))) == 13
    assert estimate_message_tokens(message) == math.ceil((8 + 4 + 11 + 13) / CHARS_PER_TOKEN)


# --- usage-preferred messages estimation -------------------------------------


def test_estimate_messages_prefers_last_applicable_usage():
    messages = [
        UserMessage(content="x" * 100, timestamp=1),
        make_assistant(Usage(input=60, output=40, cache_read=0, cache_write=0, total_tokens=100), timestamp=2),
        UserMessage(content="a" * 8, timestamp=3),  # trailing 2 tokens
    ]
    estimate = estimate_context_tokens(messages)
    assert estimate.usage_tokens == 100
    assert estimate.trailing_tokens == 2
    assert estimate.tokens == 102
    assert estimate.last_usage_index == 1


def test_estimate_messages_skips_aborted_and_error_usage():
    messages = [
        make_assistant(Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=999), timestamp=1, stop_reason="error", content=[]),
        make_assistant(Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=999), timestamp=2, stop_reason="aborted", content=[]),
        UserMessage(content="a" * 8, timestamp=3),
    ]
    estimate = estimate_context_tokens(messages)
    assert estimate.last_usage_index is None
    # Pure fallback: only the user message carries estimable content (8 units = 2 tokens)
    assert estimate.tokens == 2


def test_estimate_messages_usage_invalidated_by_newer_prefix_message():
    # A compaction-summary style user message with a NEWER timestamp inserted
    # before the assistant response invalidates its usage (plan §7.1).
    messages = [
        UserMessage(content="prefix " * 30, timestamp=300),
        make_assistant(Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=500), timestamp=100),
        UserMessage(content="a" * 8, timestamp=50),
    ]
    estimate = estimate_context_tokens(messages)
    assert estimate.last_usage_index is None


def test_estimate_messages_usage_applies_when_timestamps_equal():
    messages = [
        UserMessage(content="x", timestamp=200),
        make_assistant(Usage(input=0, output=0, cache_read=0, cache_write=0, total_tokens=42), timestamp=200),
    ]
    estimate = estimate_context_tokens(messages)
    assert estimate.last_usage_index == 1
    assert estimate.usage_tokens == 42


def test_estimate_messages_zero_total_usage_ignored():
    messages = [
        make_assistant(Usage(input=0, output=0, cache_read=0, cache_write=0, total_tokens=0), timestamp=1),
        UserMessage(content="a" * 8, timestamp=2),
    ]
    estimate = estimate_context_tokens(messages)
    assert estimate.last_usage_index is None


# --- Context-level estimation -------------------------------------------------


def test_estimate_context_prefix_without_usage():
    tools = [Tool(name="x", description="d", parameters={"type": "object"})]
    context = Context(messages=[UserMessage(content="a" * 8, timestamp=1)], system_prompt="s" * 8, tools=tools)
    estimate = estimate_context_tokens(context)
    expected_tools_json = json.dumps([t.to_dict() for t in tools], ensure_ascii=False, separators=(",", ":"))
    # Compact separators and raw non-ASCII (TS JSON.stringify parity)
    assert "\\u" not in expected_tools_json
    expected = 2 + math.ceil(utf16_length(expected_tools_json) / CHARS_PER_TOKEN) + 2
    assert estimate.tokens == expected
    assert estimate.usage_tokens == 0
    assert estimate.trailing_tokens == expected
    assert estimate.last_usage_index is None


def test_estimate_context_added_tool_names_after_usage():
    new_tool = Tool(name="new_tool", description="d", parameters={"type": "object"})
    other_tool = Tool(name="other_tool", description="d", parameters={"type": "object"})
    context = Context(
        messages=[
            make_assistant(Usage(input=0, output=0, cache_read=0, cache_write=0, total_tokens=100), timestamp=1),
            make_tool_result(timestamp=2, added_tool_names=["new_tool"]),
        ],
        tools=[new_tool, other_tool],
    )
    estimate = estimate_context_tokens(context)
    expected_added_json = json.dumps([new_tool.to_dict()], ensure_ascii=False, separators=(",", ":"))
    expected_added = math.ceil(utf16_length(expected_added_json) / CHARS_PER_TOKEN)
    assert estimate.usage_tokens == 100
    # trailing = toolResult message text (1 unit → 1 token) + the added tool's JSON
    assert estimate.trailing_tokens == 1 + expected_added
    assert estimate.tokens == 100 + 1 + expected_added


def test_estimate_context_with_usage_ignores_system_prompt_and_all_tools():
    tools = [Tool(name="t", description="d" * 500, parameters={"type": "object"})]
    context = Context(
        messages=[make_assistant(Usage(input=0, output=0, cache_read=0, cache_write=0, total_tokens=100), timestamp=1)],
        system_prompt="s" * 500,
        tools=tools,
    )
    estimate = estimate_context_tokens(context)
    assert estimate.tokens == 100  # only the usage; prefix is not double counted


def test_estimate_context_message_list_shortcut():
    assert estimate_context_tokens([UserMessage(content="a" * 8, timestamp=1)]).tokens == 2
