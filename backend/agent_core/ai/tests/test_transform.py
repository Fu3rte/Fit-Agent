"""Tests for ai/transform.py (port of pi transform-messages.ts).

Each contract point from plan §6.3/§11.2 gets at least one case.
"""

from __future__ import annotations

import dataclasses
from typing import Any, cast

from agent_core.ai.transform import (
    NON_VISION_TOOL_IMAGE_PLACEHOLDER,
    NON_VISION_USER_IMAGE_PLACEHOLDER,
    transform_messages,
)
from agent_core.ai.types import (
    AssistantMessage,
    ImageContent,
    Model,
    ModelCost,
    StopReason,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)

USAGE = Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2)

MODEL = Model(
    id="m1",
    name="M1",
    api="anthropic-messages",
    provider="p1",
    base_url="https://api.example.com",
    reasoning=True,
    input=["text", "image"],
    cost=ModelCost(input=1.0, output=2.0, cache_read=0.1, cache_write=1.25),
    context_window=128_000,
    max_tokens=4096,
)


def assistant(
    content: Any,
    *,
    provider: str = "p1",
    api: str = "anthropic-messages",
    model: str = "m1",
    stop_reason: StopReason = "stop",
    timestamp: int = 1,
) -> AssistantMessage:
    return AssistantMessage(
        content=content,
        api=api,
        provider=provider,
        model=model,
        usage=USAGE,
        stop_reason=stop_reason,
        timestamp=timestamp,
    )


def tool_result(call_id: str, name: str = "tool", timestamp: int = 2) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=call_id,
        tool_name=name,
        content=[TextContent(text="ok")],
        is_error=False,
        timestamp=timestamp,
    )


def call(content: Any) -> UserMessage:
    """User message with list content."""
    return UserMessage(content=content, timestamp=3)


def text_blocks(message: Any) -> list[str]:
    """Extract .text from a message's content blocks (all must be TextContent)."""
    assert isinstance(message, (UserMessage, ToolResultMessage))
    assert isinstance(message.content, list)
    texts = []
    for block in message.content:
        assert isinstance(block, TextContent)
        texts.append(block.text)
    return texts


def test_same_model_identity_requires_provider_api_model_triple():
    signed = ThinkingContent(thinking="why", thinking_signature="sig")

    # All three fields equal -> thinking kept as a thinking block.
    out = transform_messages([assistant([signed])], MODEL)
    assert isinstance(out[0], AssistantMessage)
    assert isinstance(out[0].content[0], ThinkingContent)

    # Any one of the triple differing -> degraded to plain text.
    variants: list[dict[str, Any]] = [
        {"provider": "pX"},
        {"api": "openai-completions"},
        {"model": "mX"},
    ]
    for kwargs in variants:
        msg = assistant([signed], **kwargs)
        out = transform_messages([msg], MODEL)
        assert isinstance(out[0], AssistantMessage), kwargs
        block = out[0].content[0]
        assert isinstance(block, TextContent), kwargs
        assert block.text == "why", kwargs


def test_same_model_signed_thinking_preserved_even_with_empty_text():
    # OpenAI encrypted reasoning: empty thinking text with a signature.
    signed_empty = ThinkingContent(thinking="", thinking_signature="enc-payload")

    out = transform_messages([assistant([signed_empty])], MODEL)
    assert isinstance(out[0], AssistantMessage)
    assert out[0].content == [signed_empty]

    # Cross-model the same block is meaningless: dropped.
    out = transform_messages([assistant([signed_empty], model="mX")], MODEL)
    assert isinstance(out[0], AssistantMessage)
    assert out[0].content == []


def test_plain_thinking_cross_model_becomes_plain_text_without_tags():
    thinking = ThinkingContent(thinking="  secret plan  ")
    out = transform_messages([assistant([thinking], model="mX")], MODEL)
    assert isinstance(out[0], AssistantMessage)
    block = out[0].content[0]
    assert isinstance(block, TextContent)
    assert block.text == "  secret plan  "
    assert "<thinking>" not in block.text


def test_blank_thinking_dropped_even_same_model_without_signature():
    blank = ThinkingContent(thinking="   ")
    out = transform_messages([assistant([blank])], MODEL)
    assert isinstance(out[0], AssistantMessage)
    assert out[0].content == []


def test_redacted_thinking_dropped_cross_model_kept_same_model():
    redacted = ThinkingContent(thinking="opaque", thinking_signature="enc", redacted=True)

    out = transform_messages([assistant([redacted])], MODEL)
    assert isinstance(out[0], AssistantMessage)
    assert out[0].content == [redacted]

    out = transform_messages([assistant([redacted], model="mX")], MODEL)
    assert isinstance(out[0], AssistantMessage)
    assert out[0].content == []


def test_thought_signature_removed_cross_model_kept_same_model():
    tool_call = ToolCall(id="call_1", name="get", arguments={"a": 1}, thought_signature="gsig")

    out = transform_messages([assistant([tool_call])], MODEL)
    assert isinstance(out[0], AssistantMessage)
    block = out[0].content[0]
    assert isinstance(block, ToolCall)
    assert block.thought_signature == "gsig"

    out = transform_messages([assistant([tool_call], model="mX")], MODEL)
    assert isinstance(out[0], AssistantMessage)
    block = out[0].content[0]
    assert isinstance(block, ToolCall)
    assert block.thought_signature is None


def test_text_block_cross_model_drops_text_signature():
    text = TextContent(text="hello", text_signature="meta")

    out = transform_messages([assistant([text])], MODEL)
    assert isinstance(out[0], AssistantMessage)
    assert out[0].content[0] is text

    out = transform_messages([assistant([text], model="mX")], MODEL)
    assert isinstance(out[0], AssistantMessage)
    block = out[0].content[0]
    assert isinstance(block, TextContent)
    assert block.text == "hello"
    assert block.text_signature is None


def test_error_and_aborted_assistant_filtered_entirely():
    content: Any = [TextContent(text="partial"), ToolCall(id="call_1", name="t", arguments={})]
    for reason in ("error", "aborted"):
        out = transform_messages([assistant(content, stop_reason=reason)], MODEL)
        assert out == [], reason


def test_orphan_tool_call_gets_synthetic_error_result():
    msg = assistant([ToolCall(id="call_1", name="lookup", arguments={"q": "x"})])
    out = transform_messages([msg], MODEL)

    assert len(out) == 2
    synthetic = out[1]
    assert isinstance(synthetic, ToolResultMessage)
    assert synthetic.tool_call_id == "call_1"
    assert synthetic.tool_name == "lookup"
    assert synthetic.is_error is True
    assert isinstance(synthetic.content[0], TextContent)
    assert synthetic.content[0].text == "No result provided"


def test_trailing_orphan_tool_calls_synthesized_at_end():
    out = transform_messages(
        [
            call([TextContent(text="go")]),
            assistant(
                [
                    ToolCall(id="a", name="t", arguments={}),
                    ToolCall(id="b", name="t", arguments={}),
                ]
            ),
            tool_result("a"),
        ],
        MODEL,
    )

    kinds = [(m.role, getattr(m, "tool_call_id", None)) for m in out]
    assert kinds == [
        ("user", None),
        ("assistant", None),
        ("toolResult", "a"),
        ("toolResult", "b"),  # synthetic
    ]
    assert isinstance(out[3], ToolResultMessage)
    assert out[3].is_error is True


def test_user_message_interrupts_tool_flow_with_synthetic_results():
    out = transform_messages(
        [
            assistant([ToolCall(id="call_1", name="t", arguments={})]),
            call([TextContent(text="nevermind")]),
        ],
        MODEL,
    )

    assert [m.role for m in out] == ["assistant", "toolResult", "user"]
    assert isinstance(out[1], ToolResultMessage)
    assert out[1].tool_call_id == "call_1"
    assert out[1].is_error is True


def test_assistant_without_tool_calls_flushes_pending_orphans():
    # TS quirk: an intervening assistant message (no tool calls) flushes the
    # pending orphan with a synthetic result; the late real result still passes
    # through after it.
    out = transform_messages(
        [
            assistant([ToolCall(id="call_1", name="t", arguments={})], timestamp=1),
            assistant([TextContent(text="hm")], timestamp=2),
            tool_result("call_1"),
        ],
        MODEL,
    )

    assert [m.role for m in out] == ["assistant", "toolResult", "assistant", "toolResult"]
    assert isinstance(out[1], ToolResultMessage)
    assert out[1].is_error is True
    assert isinstance(out[3], ToolResultMessage)
    assert out[3].is_error is False


def test_tool_result_remap_only_when_normalized_id_differs():
    seen_ids: list[str] = []

    def normalize(tool_call_id: str, model: Model, source: AssistantMessage) -> str:
        seen_ids.append(tool_call_id)
        return f"norm_{tool_call_id}"

    out = transform_messages(
        [
            assistant([ToolCall(id="call_1", name="t", arguments={})], model="mX"),
            tool_result("call_1"),
            tool_result("unknown_id"),
        ],
        MODEL,
        normalize_tool_call_id=normalize,
    )

    assert isinstance(out[0], AssistantMessage)
    # The tool call was rewritten...
    call_block = out[0].content[0]
    assert isinstance(call_block, ToolCall)
    assert call_block.id == "norm_call_1"
    # ...and the matching toolResult follows the same mapping.
    assert isinstance(out[1], ToolResultMessage)
    assert out[1].tool_call_id == "norm_call_1"
    # An unrelated toolResult passes through untouched.
    assert isinstance(out[2], ToolResultMessage)
    assert out[2].tool_call_id == "unknown_id"
    assert seen_ids == ["call_1"]


def test_tool_call_id_not_normalized_for_same_model():
    def normalize(tool_call_id: str, model: Model, source: AssistantMessage) -> str:
        raise AssertionError("normalize callback must not run for same-model messages")

    msg = assistant([ToolCall(id="call_1", name="t", arguments={})])
    out = transform_messages(
        [msg, tool_result("call_1")], MODEL, normalize_tool_call_id=normalize
    )
    assert isinstance(out[0], AssistantMessage)
    call_block = out[0].content[0]
    assert isinstance(call_block, ToolCall)
    assert call_block.id == "call_1"
    assert isinstance(out[1], ToolResultMessage)
    assert out[1].tool_call_id == "call_1"


def test_non_vision_model_downgrades_images_with_placeholder():
    text_only_model = dataclasses.replace(MODEL, input=["text"])
    image = ImageContent(data="base64data", mime_type="image/png")

    out = transform_messages(
        [
            call([image, image, TextContent(text="hi"), image]),
            tool_result("call_1"),
        ],
        text_only_model,
    )
    user_msg = out[0]
    assert isinstance(user_msg, UserMessage)
    # Consecutive images collapse into a single placeholder.
    assert text_blocks(user_msg) == [
        NON_VISION_USER_IMAGE_PLACEHOLDER,
        "hi",
        NON_VISION_USER_IMAGE_PLACEHOLDER,
    ]


def test_tool_result_images_downgraded_with_tool_placeholder():
    text_only_model = dataclasses.replace(MODEL, input=["text"])
    image = ImageContent(data="base64data", mime_type="image/png")
    tr = ToolResultMessage(
        tool_call_id="call_1",
        tool_name="t",
        content=[image, TextContent(text="result")],
        is_error=False,
        timestamp=2,
    )
    out = transform_messages([tr], text_only_model)
    assert text_blocks(out[0]) == [NON_VISION_TOOL_IMAGE_PLACEHOLDER, "result"]


def test_vision_model_keeps_images_untouched():
    image = ImageContent(data="base64data", mime_type="image/png")
    user = call([image])
    out = transform_messages([user], MODEL)
    assert out[0] is user


def test_null_content_normalized_to_empty_list():
    broken = dataclasses.replace(assistant([TextContent(text="x")]), content=cast(Any, None))
    broken_user = dataclasses.replace(call([TextContent(text="x")]), content=cast(Any, None))
    out = transform_messages([broken, broken_user], MODEL)
    assert isinstance(out[0], AssistantMessage)
    assert out[0].content == []
    assert isinstance(out[1], UserMessage)
    assert out[1].content == []
