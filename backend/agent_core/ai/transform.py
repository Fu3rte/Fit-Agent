"""Ported from pi-package/ai/src/api/transform-messages.ts @ pi snapshot
9841914c71a74d81abe07f751aefd271fd924e63

Cross-model message transformation (plan pre-prj/pi-python-harness-plan.md
§6.3 / §11.2). Pure functions:

- same-model identity = provider + api + model-id triple equality
- signed thinking blocks survive same-model replay (even with empty thinking
  text, e.g. OpenAI encrypted reasoning); plain thinking degrades to plain
  text cross-model (no <thinking> tags); redacted thinking is dropped
  cross-model
- thoughtSignature is removed from tool calls cross-model
- tool-call IDs are normalized per target provider through the optional
  ``normalize_tool_call_id`` callback (cross-model only); toolResult IDs are
  remapped through the same map
- error/aborted assistant messages are skipped entirely
- orphaned tool calls get a synthetic error toolResult with the fixed text
  "No result provided"

If an older pi README describes this differently, transform-messages.ts is
authoritative.
"""

from __future__ import annotations

import dataclasses
import time
from typing import Callable

from .types import (
    AssistantContent,
    AssistantMessage,
    ImageContent,
    Message,
    Model,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)

NON_VISION_USER_IMAGE_PLACEHOLDER = "(image omitted: model does not support images)"
NON_VISION_TOOL_IMAGE_PLACEHOLDER = "(tool image omitted: model does not support images)"

NormalizeToolCallId = Callable[[str, Model, AssistantMessage], str]

# Exact JS String.prototype.trim character set (ES WhiteSpace + LineTerminators,
# including U+FEFF which Python str.strip() would keep). Used so the
# "blank thinking text" decision matches the TS source exactly.
_JS_TRIM_CHARS = (
    "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
    "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)


def _js_trim_blank(text: str) -> bool:
    """True when JS ``text.trim() === ""``."""
    return text.strip(_JS_TRIM_CHARS) == ""


def _now_ms() -> int:
    """TS ``Date.now()`` — Unix epoch milliseconds."""
    return int(time.time() * 1000)


def _replace_images_with_placeholder(
    content: list[TextContent | ImageContent], placeholder: str
) -> list[TextContent]:
    result: list[TextContent] = []
    previous_was_placeholder = False

    for block in content:
        if isinstance(block, ImageContent):
            if not previous_was_placeholder:
                result.append(TextContent(text=placeholder))
            previous_was_placeholder = True
            continue

        result.append(block)
        previous_was_placeholder = isinstance(block, TextContent) and block.text == placeholder

    return result


def _downgrade_unsupported_images(messages: list[Message], model: Model) -> list[Message]:
    if "image" in model.input:
        return messages

    out: list[Message] = []
    for msg in messages:
        if isinstance(msg, UserMessage) and isinstance(msg.content, list):
            out.append(
                dataclasses.replace(
                    msg,
                    content=_replace_images_with_placeholder(
                        msg.content, NON_VISION_USER_IMAGE_PLACEHOLDER
                    ),
                )
            )
            continue
        if isinstance(msg, ToolResultMessage):
            out.append(
                dataclasses.replace(
                    msg,
                    content=_replace_images_with_placeholder(
                        msg.content, NON_VISION_TOOL_IMAGE_PLACEHOLDER
                    ),
                )
            )
            continue
        out.append(msg)
    return out


def transform_messages(
    messages: list[Message],
    model: Model,
    normalize_tool_call_id: NormalizeToolCallId | None = None,
) -> list[Message]:
    """TS ``transformMessages(messages, model, normalizeToolCallId?)``.

    Normalize tool call ID for cross-provider compatibility. OpenAI Responses
    API generates IDs that are 450+ chars with special characters like ``|``;
    Anthropic APIs require IDs matching ``^[a-zA-Z0-9_-]+$`` (max 64 chars).
    """
    # Build a map of original tool call IDs to normalized IDs
    tool_call_id_map: dict[str, str] = {}

    # Normalize null content from untyped callers (custom tools, hand-built
    # histories, old session files) so downstream code can rely on the type
    # contract.
    normalized_messages: list[Message] = []
    for msg in messages:
        if getattr(msg, "content", None) is None:
            normalized_messages.append(dataclasses.replace(msg, content=[]))
        else:
            normalized_messages.append(msg)

    image_aware_messages = _downgrade_unsupported_images(normalized_messages, model)

    # First pass: transform messages (unsupported image downgrade, thinking
    # blocks, tool call ID normalization)
    transformed: list[Message] = []
    for msg in image_aware_messages:
        # User messages pass through unchanged
        if isinstance(msg, UserMessage):
            transformed.append(msg)
            continue

        # Handle toolResult messages - normalize toolCallId if we have a mapping
        if isinstance(msg, ToolResultMessage):
            normalized_id = tool_call_id_map.get(msg.tool_call_id)
            if normalized_id and normalized_id != msg.tool_call_id:
                transformed.append(dataclasses.replace(msg, tool_call_id=normalized_id))
            else:
                transformed.append(msg)
            continue

        # Assistant messages need transformation check
        if isinstance(msg, AssistantMessage):
            is_same_model = (
                msg.provider == model.provider
                and msg.api == model.api
                and msg.model == model.id
            )

            transformed_content: list[AssistantContent] = []
            for block in msg.content:
                if isinstance(block, ThinkingContent):
                    if block.redacted:
                        # Redacted thinking is opaque encrypted content, only
                        # valid for the same model. Drop it for cross-model to
                        # avoid API errors.
                        if is_same_model:
                            transformed_content.append(block)
                        continue
                    # For same model: keep thinking blocks with signatures
                    # (needed for replay) even if the thinking text is empty
                    # (OpenAI encrypted reasoning)
                    if is_same_model and block.thinking_signature:
                        transformed_content.append(block)
                        continue
                    # Skip empty thinking blocks, convert others to plain text
                    if not block.thinking or _js_trim_blank(block.thinking):
                        continue
                    if is_same_model:
                        transformed_content.append(block)
                        continue
                    transformed_content.append(TextContent(text=block.thinking))
                elif isinstance(block, TextContent):
                    if is_same_model:
                        transformed_content.append(block)
                    else:
                        transformed_content.append(TextContent(text=block.text))
                elif isinstance(block, ToolCall):
                    tool_call = block

                    if not is_same_model and tool_call.thought_signature:
                        tool_call = dataclasses.replace(tool_call, thought_signature=None)

                    if not is_same_model and normalize_tool_call_id is not None:
                        normalized_id = normalize_tool_call_id(tool_call.id, model, msg)
                        if normalized_id != tool_call.id:
                            tool_call_id_map[tool_call.id] = normalized_id
                            tool_call = dataclasses.replace(tool_call, id=normalized_id)

                    transformed_content.append(tool_call)
                else:
                    transformed_content.append(block)

            transformed.append(dataclasses.replace(msg, content=transformed_content))
            continue

        transformed.append(msg)

    # Second pass: insert synthetic empty tool results for orphaned tool calls
    # This preserves thinking signatures and satisfies API requirements
    result: list[Message] = []
    pending_tool_calls: list[ToolCall] = []
    existing_tool_result_ids: set[str] = set()

    def insert_synthetic_tool_results() -> None:
        nonlocal pending_tool_calls, existing_tool_result_ids
        if pending_tool_calls:
            for tc in pending_tool_calls:
                if tc.id not in existing_tool_result_ids:
                    result.append(
                        ToolResultMessage(
                            tool_call_id=tc.id,
                            tool_name=tc.name,
                            content=[TextContent(text="No result provided")],
                            is_error=True,
                            timestamp=_now_ms(),
                        )
                    )
            pending_tool_calls = []
            existing_tool_result_ids = set()

    for msg in transformed:
        if isinstance(msg, AssistantMessage):
            # If we have pending orphaned tool calls from a previous assistant,
            # insert synthetic results now
            insert_synthetic_tool_results()

            # Skip errored/aborted assistant messages entirely.
            # These are incomplete turns that shouldn't be replayed:
            # - May have partial content (reasoning without message, incomplete
            #   tool calls)
            # - Replaying them can cause API errors (e.g. OpenAI "reasoning
            #   without following item")
            # - The model should retry from the last valid state
            if msg.stop_reason == "error" or msg.stop_reason == "aborted":
                continue

            # Track tool calls from this assistant message
            tool_calls = [b for b in msg.content if isinstance(b, ToolCall)]
            if tool_calls:
                pending_tool_calls = tool_calls
                existing_tool_result_ids = set()

            result.append(msg)
        elif isinstance(msg, ToolResultMessage):
            existing_tool_result_ids.add(msg.tool_call_id)
            result.append(msg)
        elif isinstance(msg, UserMessage):
            # User message interrupts tool flow - insert synthetic results for
            # orphaned calls
            insert_synthetic_tool_results()
            result.append(msg)
        else:
            result.append(msg)

    # If the conversation ends with unresolved tool calls, synthesize results now.
    insert_synthetic_tool_results()

    return result
