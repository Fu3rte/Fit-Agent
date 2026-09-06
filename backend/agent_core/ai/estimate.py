"""Ported from pi-package/ai/src/utils/estimate.ts @ pi snapshot 9841914c71a74d81abe07f751aefd271fd924e63

Context token estimation: prefer the most recent applicable assistant usage
block, estimate only the trailing messages after it; fall back to pure
char-count estimation (~4 chars/token) when no usage is available.

Fidelity notes:
- TS ``string.length`` counts UTF-16 code units, while Python ``len`` counts
  Unicode code points. For differential parity (plan §7.1) every length
  computation here goes through :func:`utf16_length`.
- ``JSON.stringify`` output is compact (no spaces) and keeps non-ASCII
  characters raw, so ``_safe_json_stringify`` uses ``ensure_ascii=False`` and
  ``separators=(",", ":")``.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Sequence

from .types import AssistantMessage, Context, ImageContent, Message, TextContent, Tool, Usage

CHARS_PER_TOKEN = 4
ESTIMATED_IMAGE_CHARS = 4800


@dataclass
class ContextUsageEstimate:
    """TS ContextUsageEstimate (non-wire type; snake_case per plan §4.1)."""

    tokens: int
    """Estimated total context tokens."""
    usage_tokens: int
    """Tokens reported by the most recent applicable assistant usage block."""
    trailing_tokens: int
    """Estimated tokens after the most recent applicable assistant usage block."""
    last_usage_index: int | None
    """Index of the applicable message that provided usage, or None when none exists."""


def utf16_length(text: str) -> int:
    """Length of ``text`` in UTF-16 code units (TS ``string.length`` semantics).

    Code points above BMP (emoji, rare CJK) count as 2; everything else (including
    lone surrogates) counts as 1.
    """
    return len(text) + sum(1 for ch in text if ord(ch) > 0xFFFF)


def _ceil_div(chars: int, divisor: int) -> int:
    # Equivalent to Math.ceil(chars / divisor) for the integer ranges involved.
    return -(-chars // divisor)


def calculate_context_tokens(usage: Usage) -> int:
    # TS: usage.totalTokens || input + output + cacheRead + cacheWrite
    # (0/falsy totalTokens falls back to the sum).
    return usage.total_tokens or (
        usage.input + usage.output + usage.cache_read + usage.cache_write
    )


def _safe_json_stringify(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return "[unserializable]"


def _estimate_text_and_image_content_chars(content: str | Sequence[TextContent | ImageContent]) -> int:
    if isinstance(content, str):
        return utf16_length(content)

    chars = 0
    for block in content:
        chars += utf16_length(block.text) if block.type == "text" else ESTIMATED_IMAGE_CHARS
    return chars


def estimate_text_tokens(text: str) -> int:
    return _ceil_div(utf16_length(text), CHARS_PER_TOKEN)


def estimate_text_and_image_content_tokens(content: str | Sequence[TextContent | ImageContent]) -> int:
    return _ceil_div(_estimate_text_and_image_content_chars(content), CHARS_PER_TOKEN)


def estimate_message_tokens(message: Message) -> int:
    if message.role == "user":
        return estimate_text_and_image_content_tokens(message.content)  # type: ignore[arg-type]
    if message.role == "toolResult":
        return estimate_text_and_image_content_tokens(message.content)  # type: ignore[arg-type]

    chars = 0
    for block in message.content:
        if block.type == "text":
            chars += utf16_length(block.text)
        elif block.type == "thinking":
            chars += utf16_length(block.thinking)
        else:
            chars += utf16_length(block.name) + utf16_length(_safe_json_stringify(block.arguments))
    return _ceil_div(chars, CHARS_PER_TOKEN)


def _get_last_assistant_usage_info(messages: Sequence[Message]) -> tuple[Usage, int] | None:
    latest_prefix_timestamp = float("-inf")
    usage_info: tuple[Usage, int] | None = None

    for i, message in enumerate(messages):
        if message.role == "assistant":
            assistant = message
            # A newer prefix message was inserted after this response (for example, a
            # compaction summary), so its usage cannot describe the current prefix.
            usage_applies_to_prefix = assistant.timestamp >= latest_prefix_timestamp
            if (
                usage_applies_to_prefix
                and assistant.stop_reason != "aborted"
                and assistant.stop_reason != "error"
                and calculate_context_tokens(assistant.usage) > 0
            ):
                usage_info = (assistant.usage, i)
        latest_prefix_timestamp = max(latest_prefix_timestamp, message.timestamp)

    return usage_info


def _estimate_messages(messages: Sequence[Message]) -> ContextUsageEstimate:
    usage_info = _get_last_assistant_usage_info(messages)
    if usage_info is not None:
        usage, index = usage_info
        usage_tokens = calculate_context_tokens(usage)
        trailing_tokens = sum(estimate_message_tokens(m) for m in messages[index + 1 :])
        return ContextUsageEstimate(
            tokens=usage_tokens + trailing_tokens,
            usage_tokens=usage_tokens,
            trailing_tokens=trailing_tokens,
            last_usage_index=index,
        )

    tokens = sum(estimate_message_tokens(m) for m in messages)
    return ContextUsageEstimate(
        tokens=tokens, usage_tokens=0, trailing_tokens=tokens, last_usage_index=None
    )


def _estimate_tools_tokens(tools: Sequence[Tool] | None) -> int:
    if not tools:
        return 0
    return estimate_text_tokens(_safe_json_stringify([tool.to_dict() for tool in tools]))


def estimate_context_tokens(context: Context | Sequence[Message]) -> ContextUsageEstimate:
    # TS isMessageArray checks Array.isArray; Context is a dataclass, never a list.
    if not isinstance(context, Context):
        return _estimate_messages(list(context))

    estimate = _estimate_messages(context.messages)
    if estimate.last_usage_index is not None:
        added_names: set[str] = set()
        for message in context.messages[estimate.last_usage_index + 1 :]:
            if message.role == "toolResult" and message.added_tool_names:
                added_names.update(message.added_tool_names)
        added_tool_tokens = _estimate_tools_tokens(
            [tool for tool in (context.tools or []) if tool.name in added_names]
        )
        return ContextUsageEstimate(
            tokens=estimate.tokens + added_tool_tokens,
            usage_tokens=estimate.usage_tokens,
            trailing_tokens=estimate.trailing_tokens + added_tool_tokens,
            last_usage_index=estimate.last_usage_index,
        )

    prefix_tokens = (
        (estimate_text_tokens(context.system_prompt) if context.system_prompt else 0)
        + _estimate_tools_tokens(context.tools)
    )
    return ContextUsageEstimate(
        tokens=estimate.tokens + prefix_tokens,
        usage_tokens=estimate.usage_tokens,
        trailing_tokens=estimate.trailing_tokens + prefix_tokens,
        last_usage_index=estimate.last_usage_index,
    )
