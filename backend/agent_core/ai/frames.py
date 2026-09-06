"""Ported from pi-package/ai/src/utils/assistant-message-frame.ts @ pi snapshot 9841914c71a74d81abe07f751aefd271fd924e63

Compact, replayable assistant-message progress frames (plan §4.3):
- Terminal settlement is intentionally excluded: done/error produce no frame
  and must be persisted separately.
- ``toolcall_checkpoint`` carries raw partial-JSON text progress, NOT verified
  tool arguments; never execute arguments derived from a checkpoint.
- thinking level / thinkingSignature / redacted markers are preserved for
  replay.

The encoder treats ``partial`` as a shared live accumulator and keeps
per-block offsets so already-covered deltas are not replayed. Length
arithmetic uses Python code-point lengths (self-consistent for replay; the
TS original counts UTF-16 units, which only diverges when a provider splits
a surrogate pair across a block start boundary).
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Union

from .partial_json import parse_streaming_json
from .types import AssistantMessage, AssistantMessageEvent, TextContent, ThinkingContent, ToolCall

# ---------------------------------------------------------------------------
# Frame types
# ---------------------------------------------------------------------------


@dataclass
class FrameStart:
    partial: AssistantMessage
    type = "start"

    def to_dict(self) -> dict[str, Any]:
        return {"type": "start", "partial": self.partial.to_dict()}


@dataclass
class FrameTextStart:
    content_index: int
    content: TextContent
    type = "text_start"

    def to_dict(self) -> dict[str, Any]:
        return {"type": "text_start", "contentIndex": self.content_index, "content": self.content.to_dict()}


@dataclass
class FrameTextDelta:
    content_index: int
    delta: str
    type = "text_delta"

    def to_dict(self) -> dict[str, Any]:
        return {"type": "text_delta", "contentIndex": self.content_index, "delta": self.delta}


@dataclass
class FrameTextEnd:
    content_index: int
    content: str
    text_signature: str | None = None
    type = "text_end"

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "type": "text_end",
            "contentIndex": self.content_index,
            "content": self.content,
        }
        if self.text_signature is not None:
            d["textSignature"] = self.text_signature
        return d


@dataclass
class FrameThinkingStart:
    content_index: int
    content: ThinkingContent
    type = "thinking_start"

    def to_dict(self) -> dict[str, Any]:
        return {"type": "thinking_start", "contentIndex": self.content_index, "content": self.content.to_dict()}


@dataclass
class FrameThinkingDelta:
    content_index: int
    delta: str
    type = "thinking_delta"

    def to_dict(self) -> dict[str, Any]:
        return {"type": "thinking_delta", "contentIndex": self.content_index, "delta": self.delta}


@dataclass
class FrameThinkingEnd:
    content_index: int
    content: str
    thinking_signature: str | None = None
    redacted: bool | None = None
    type = "thinking_end"

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "type": "thinking_end",
            "contentIndex": self.content_index,
            "content": self.content,
        }
        if self.thinking_signature is not None:
            d["thinkingSignature"] = self.thinking_signature
        if self.redacted is not None:
            d["redacted"] = self.redacted
        return d


@dataclass
class FrameToolCallStart:
    content_index: int
    tool_call: ToolCall
    type = "toolcall_start"

    def to_dict(self) -> dict[str, Any]:
        return {"type": "toolcall_start", "contentIndex": self.content_index, "toolCall": self.tool_call.to_dict()}


@dataclass
class FrameToolCallCheckpoint:
    content_index: int
    json: str
    type = "toolcall_checkpoint"

    def to_dict(self) -> dict[str, Any]:
        return {"type": "toolcall_checkpoint", "contentIndex": self.content_index, "json": self.json}


@dataclass
class FrameToolCallDelta:
    content_index: int
    delta: str
    type = "toolcall_delta"

    def to_dict(self) -> dict[str, Any]:
        return {"type": "toolcall_delta", "contentIndex": self.content_index, "delta": self.delta}


@dataclass
class FrameToolCallEnd:
    content_index: int
    id: str
    name: str
    arguments: dict[str, Any]
    thought_signature: str | None = None
    namespace: str | None = None
    type = "toolcall_end"

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "type": "toolcall_end",
            "contentIndex": self.content_index,
            "id": self.id,
            "name": self.name,
            "arguments": self.arguments,
        }
        if self.thought_signature is not None:
            d["thoughtSignature"] = self.thought_signature
        if self.namespace is not None:
            d["namespace"] = self.namespace
        return d


AssistantMessageFrame = Union[
    FrameStart,
    FrameTextStart,
    FrameTextDelta,
    FrameTextEnd,
    FrameThinkingStart,
    FrameThinkingDelta,
    FrameThinkingEnd,
    FrameToolCallStart,
    FrameToolCallCheckpoint,
    FrameToolCallDelta,
    FrameToolCallEnd,
]

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _assert_content_index(content_index: int) -> None:
    if isinstance(content_index, bool) or not isinstance(content_index, int) or content_index < 0:
        raise ValueError(f"Invalid assistant message frame contentIndex: {content_index}")


def _serialized_arguments(arguments: dict[str, Any]) -> str:
    try:
        # Compact separators + raw non-ASCII to match JSON.stringify semantics.
        return json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Tool-call arguments are not JSON-serializable") from exc


EMPTY_PARSED_TOOL_ARGUMENTS = _serialized_arguments(parse_streaming_json(""))


def _is_json_prefix(snapshot: Any, current: Any) -> bool:
    if isinstance(snapshot, str):
        return isinstance(current, str) and current.startswith(snapshot)
    if isinstance(snapshot, list):
        return (
            isinstance(current, list)
            and len(snapshot) <= len(current)
            and all(_is_json_prefix(value, current[index]) for index, value in enumerate(snapshot))
        )
    if isinstance(snapshot, dict):
        if not isinstance(current, dict):
            return False
        return all(key in current and _is_json_prefix(value, current[key]) for key, value in snapshot.items())
    # Scalars: Object.is semantics (type-strict equality).
    if snapshot is None:
        return current is None
    if isinstance(snapshot, bool):
        return isinstance(current, bool) and snapshot == current
    if isinstance(snapshot, (int, float)):
        return isinstance(current, (int, float)) and not isinstance(current, bool) and snapshot == current
    return False


def _clone_text_content(content: TextContent) -> TextContent:
    return TextContent(text=content.text, text_signature=content.text_signature)


def _clone_thinking_content(content: ThinkingContent) -> ThinkingContent:
    return ThinkingContent(
        thinking=content.thinking,
        thinking_signature=content.thinking_signature,
        redacted=content.redacted,
    )


def _clone_tool_call(tool_call: ToolCall) -> ToolCall:
    return ToolCall(
        id=tool_call.id,
        name=tool_call.name,
        arguments=copy.deepcopy(tool_call.arguments),
        thought_signature=tool_call.thought_signature,
        namespace=tool_call.namespace,
    )


def _clone_start_message(message: AssistantMessage) -> AssistantMessage:
    return AssistantMessage(
        content=[],
        api=message.api,
        provider=message.provider,
        model=message.model,
        usage=copy.deepcopy(message.usage),
        stop_reason="pending",
        timestamp=message.timestamp,
        response_model=message.response_model,
        response_id=message.response_id,
        provider_thinking_level=message.provider_thinking_level,
        diagnostics=copy.deepcopy(message.diagnostics),
    )


def _event_block(event: Any) -> Any:
    _assert_content_index(event.content_index)
    if event.content_index >= len(event.partial.content):
        raise ValueError(f"{event.type} event has no content block at index {event.content_index}")
    return event.partial.content[event.content_index]


# ---------------------------------------------------------------------------
# Encoder
# ---------------------------------------------------------------------------


@dataclass
class _TextEncoderState:
    kind: str  # "text" | "thinking"
    covered_chars: int
    delta_chars: int


@dataclass
class _ToolCallEncoderState:
    kind: str  # "toolCall"
    caught_up: bool
    catchup_json: str
    snapshot_arguments: str


EncoderBlockState = Union[_TextEncoderState, _ToolCallEncoderState]


class AssistantMessageFrameEncoder:
    """Encodes one assistant stream. ``partial`` remains a shared live
    accumulator; per-block offsets avoid replaying deltas already visible when
    an older queued event is consumed."""

    def __init__(self) -> None:
        self._started = False
        self._terminal = False
        self._blocks: dict[int, EncoderBlockState] = {}

    def encode(self, event: AssistantMessageEvent) -> AssistantMessageFrame | None:
        if self._terminal:
            raise ValueError(f"Assistant message event {event.type} follows a terminal event")

        if event.type == "start":
            if self._started:
                raise ValueError("Assistant message stream contains more than one start event")
            self._started = True
            return FrameStart(partial=_clone_start_message(event.partial))
        if event.type == "done":
            if not self._started:
                raise ValueError("Assistant message done event appears before start")
            self._terminal = True
            return None
        if event.type == "error":
            # Terminal without requiring start (errors may arrive before start).
            self._terminal = True
            return None

        if not self._started:
            raise ValueError(f"Assistant message {event.type} event appears before start")

        if event.type == "text_start":
            content = _event_block(event)
            if not isinstance(content, TextContent):
                raise ValueError(f"text_start event points to {content.type} block at index {event.content_index}")
            self._start_block(
                event.content_index,
                _TextEncoderState(kind="text", covered_chars=len(content.text), delta_chars=0),
            )
            return FrameTextStart(content_index=event.content_index, content=_clone_text_content(content))

        if event.type == "text_delta":
            return self._encode_text_delta(event.content_index, event.delta, "text")

        if event.type == "text_end":
            content = _event_block(event)
            if not isinstance(content, TextContent):
                raise ValueError(f"text_end event points to {content.type} block at index {event.content_index}")
            self._end_block(event.content_index, "text")
            return FrameTextEnd(
                content_index=event.content_index,
                content=event.content,
                text_signature=content.text_signature,
            )

        if event.type == "thinking_start":
            content = _event_block(event)
            if not isinstance(content, ThinkingContent):
                raise ValueError(
                    f"thinking_start event points to {content.type} block at index {event.content_index}"
                )
            self._start_block(
                event.content_index,
                _TextEncoderState(kind="thinking", covered_chars=len(content.thinking), delta_chars=0),
            )
            return FrameThinkingStart(content_index=event.content_index, content=_clone_thinking_content(content))

        if event.type == "thinking_delta":
            return self._encode_text_delta(event.content_index, event.delta, "thinking")

        if event.type == "thinking_end":
            content = _event_block(event)
            if not isinstance(content, ThinkingContent):
                raise ValueError(f"thinking_end event points to {content.type} block at index {event.content_index}")
            self._end_block(event.content_index, "thinking")
            return FrameThinkingEnd(
                content_index=event.content_index,
                content=event.content,
                thinking_signature=content.thinking_signature,
                redacted=content.redacted,
            )

        if event.type == "toolcall_start":
            content = _event_block(event)
            if not isinstance(content, ToolCall):
                raise ValueError(
                    f"toolcall_start event points to {content.type} block at index {event.content_index}"
                )
            snapshot_arguments = _serialized_arguments(content.arguments)
            caught_up = snapshot_arguments == EMPTY_PARSED_TOOL_ARGUMENTS
            self._start_block(
                event.content_index,
                _ToolCallEncoderState(
                    kind="toolCall",
                    caught_up=caught_up,
                    catchup_json="",
                    snapshot_arguments="" if caught_up else snapshot_arguments,
                ),
            )
            return FrameToolCallStart(content_index=event.content_index, tool_call=_clone_tool_call(content))

        if event.type == "toolcall_delta":
            state = self._block(event.content_index, "toolCall")
            assert isinstance(state, _ToolCallEncoderState)
            if state.caught_up:
                if event.delta == "":
                    return None
                return FrameToolCallDelta(content_index=event.content_index, delta=event.delta)
            state.catchup_json += event.delta
            arguments_value = parse_streaming_json(state.catchup_json)
            if _serialized_arguments(arguments_value) != state.snapshot_arguments:
                # Legacy grammar calls include the initial input in toolcall_start, but their
                # JSON delta stream still begins at an empty input. Its parsed arguments can
                # therefore extend, rather than exactly reproduce, the start snapshot.
                snapshot_arguments_value = parse_streaming_json(state.snapshot_arguments)
                if not _is_json_prefix(snapshot_arguments_value, arguments_value):
                    return None
            state.caught_up = True
            state.snapshot_arguments = ""
            json_text = state.catchup_json
            state.catchup_json = ""
            if json_text == "":
                return None
            return FrameToolCallCheckpoint(content_index=event.content_index, json=json_text)

        if event.type == "toolcall_end":
            content = _event_block(event)
            if not isinstance(content, ToolCall):
                raise ValueError(f"toolcall_end event points to {content.type} block at index {event.content_index}")
            tool_call = event.tool_call
            if not isinstance(tool_call, ToolCall):
                raise ValueError(f"toolcall_end event has invalid tool call at index {event.content_index}")
            self._end_block(event.content_index, "toolCall")
            return FrameToolCallEnd(
                content_index=event.content_index,
                id=tool_call.id,
                name=tool_call.name,
                arguments=copy.deepcopy(tool_call.arguments),
                thought_signature=tool_call.thought_signature,
                namespace=tool_call.namespace,
            )

        raise ValueError(f"unexpected assistant message event type: {event.type}")

    def _start_block(self, content_index: int, state: EncoderBlockState) -> None:
        _assert_content_index(content_index)
        if content_index in self._blocks:
            raise ValueError(f"Assistant message block {content_index} starts more than once")
        self._blocks[content_index] = state

    def _block(self, content_index: int, kind: str) -> EncoderBlockState:
        _assert_content_index(content_index)
        state = self._blocks.get(content_index)
        if state is None:
            raise ValueError(f"Assistant message {kind} block {content_index} has not started")
        if state.kind != kind:
            raise ValueError(f"Assistant message block {content_index} is {state.kind}, not {kind}")
        return state

    def _end_block(self, content_index: int, kind: str) -> None:
        self._block(content_index, kind)
        del self._blocks[content_index]

    def _encode_text_delta(self, content_index: int, delta: str, kind: str) -> FrameTextDelta | FrameThinkingDelta | None:
        state = self._block(content_index, kind)
        if isinstance(state, _ToolCallEncoderState):
            raise ValueError("Unreachable text encoder state")
        delta_start = state.delta_chars
        state.delta_chars += len(delta)
        covered = max(0, state.covered_chars - delta_start)
        if covered >= len(delta):
            return None
        uncovered = delta if covered == 0 else delta[covered:]
        if kind == "text":
            return FrameTextDelta(content_index=content_index, delta=uncovered)
        return FrameThinkingDelta(content_index=content_index, delta=uncovered)


# ---------------------------------------------------------------------------
# Reducer
# ---------------------------------------------------------------------------


@dataclass
class _ReducerBlockState:
    kind: str  # "text" | "thinking" | "toolCall"
    ended: bool = False
    json: str = ""


def _append_block(
    message: AssistantMessage,
    states: dict[int, _ReducerBlockState],
    content_index: int,
    block: Any,
    state: _ReducerBlockState,
) -> None:
    _assert_content_index(content_index)
    if content_index != len(message.content):
        reason = "already exists" if content_index < len(message.content) else "would leave a gap"
        raise ValueError(f"Cannot start assistant message block at index {content_index}: {reason}")
    message.content.append(copy.deepcopy(block))
    states[content_index] = state


def _active_block(
    message: AssistantMessage,
    states: dict[int, _ReducerBlockState],
    content_index: int,
    expected_kind: str,
    frame_type: str,
) -> tuple[Any, _ReducerBlockState]:
    _assert_content_index(content_index)
    state = states.get(content_index)
    block = message.content[content_index] if content_index < len(message.content) else None
    if state is None or block is None:
        raise ValueError(f"{frame_type} frame has no started block at index {content_index}")
    if state.kind != expected_kind or block.type != expected_kind:
        raise ValueError(
            f"{frame_type} frame expected {expected_kind} block at index {content_index}, found {block.type}"
        )
    if state.ended:
        raise ValueError(f"{frame_type} frame follows the end of block at index {content_index}")
    return block, state


def reduce_assistant_message_frames(frames: Iterable[AssistantMessageFrame]) -> AssistantMessage | None:
    """Replay compact frames without mutating them. Returns ``None`` when the
    iterable contains no start frame."""
    message: AssistantMessage | None = None
    frame_before_start: str | None = None
    states: dict[int, _ReducerBlockState] = {}

    for frame in frames:
        if isinstance(frame, FrameStart):
            if message is not None:
                raise ValueError("Assistant message frame sequence contains more than one start frame")
            if frame_before_start is not None:
                raise ValueError(f"{frame_before_start} frame appears before the start frame")
            message = copy.deepcopy(frame.partial)
            continue
        if message is None:
            if frame_before_start is None:
                frame_before_start = frame.type
            continue

        if isinstance(frame, FrameTextStart):
            if frame.content.type != "text":
                raise ValueError(f"text_start frame contains {frame.content.type} content")
            _append_block(message, states, frame.content_index, frame.content, _ReducerBlockState(kind="text"))
        elif isinstance(frame, FrameTextDelta):
            block, _ = _active_block(message, states, frame.content_index, "text", frame.type)
            block.text += frame.delta
        elif isinstance(frame, FrameTextEnd):
            block, state = _active_block(message, states, frame.content_index, "text", frame.type)
            block.text = frame.content
            block.text_signature = None
            if frame.text_signature is not None:
                block.text_signature = frame.text_signature
            state.ended = True
        elif isinstance(frame, FrameThinkingStart):
            if frame.content.type != "thinking":
                raise ValueError(f"thinking_start frame contains {frame.content.type} content")
            _append_block(message, states, frame.content_index, frame.content, _ReducerBlockState(kind="thinking"))
        elif isinstance(frame, FrameThinkingDelta):
            block, _ = _active_block(message, states, frame.content_index, "thinking", frame.type)
            block.thinking += frame.delta
        elif isinstance(frame, FrameThinkingEnd):
            block, state = _active_block(message, states, frame.content_index, "thinking", frame.type)
            block.thinking = frame.content
            block.thinking_signature = None
            block.redacted = None
            if frame.thinking_signature is not None:
                block.thinking_signature = frame.thinking_signature
            if frame.redacted is not None:
                block.redacted = frame.redacted
            state.ended = True
        elif isinstance(frame, FrameToolCallStart):
            _append_block(
                message,
                states,
                frame.content_index,
                frame.tool_call,
                _ReducerBlockState(kind="toolCall", json=""),
            )
        elif isinstance(frame, FrameToolCallCheckpoint):
            block, state = _active_block(message, states, frame.content_index, "toolCall", frame.type)
            state.json = frame.json
            block.arguments = parse_streaming_json(frame.json)
        elif isinstance(frame, FrameToolCallDelta):
            block, state = _active_block(message, states, frame.content_index, "toolCall", frame.type)
            state.json += frame.delta
        elif isinstance(frame, FrameToolCallEnd):
            block, state = _active_block(message, states, frame.content_index, "toolCall", frame.type)
            block.id = frame.id
            block.name = frame.name
            block.arguments = copy.deepcopy(frame.arguments)
            block.thought_signature = None
            block.namespace = None
            if frame.thought_signature is not None:
                block.thought_signature = frame.thought_signature
            if frame.namespace is not None:
                block.namespace = frame.namespace
            state.ended = True
        else:
            raise ValueError(f"unexpected assistant message frame type: {getattr(frame, 'type', frame)!r}")

    if message is None:
        return None
    for content_index, state in states.items():
        if state.kind != "toolCall" or state.ended or state.json == "":
            continue
        block = message.content[content_index]
        if not isinstance(block, ToolCall):
            raise ValueError("Unreachable tool-call frame state")
        block.arguments = parse_streaming_json(state.json)

    return message
