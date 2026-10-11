from collections.abc import AsyncGenerator, Callable
from concurrent.futures import CancelledError
from threading import Event
from typing import Literal, NotRequired, TypeAlias, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from app.ai.messages import AssistantMessage, Message, ToolCall


class ModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    api: str
    provider: str
    id: str
    base_url: str
    context_window: int = Field(gt=0)
    max_tokens: int = Field(gt=0)
    headers: dict[str, str] | None = None


class LlmContext(TypedDict):
    messages: list[Message]


class StreamOptions(TypedDict):
    api_key: str
    signal: NotRequired[Event | None]
    max_tokens: NotRequired[int | None]
    temperature: NotRequired[float | None]
    headers: NotRequired[dict[str, str] | None]


DoneReason: TypeAlias = Literal["stop", "length", "toolUse", "error", "aborted"]


class StartEvent(TypedDict):
    type: Literal["start"]
    partial: AssistantMessage


class TextStartEvent(TypedDict):
    type: Literal["text_start"]
    content_index: int
    partial: AssistantMessage


class TextDeltaEvent(TypedDict):
    type: Literal["text_delta"]
    content_index: int
    delta: str
    partial: AssistantMessage


class TextEndEvent(TypedDict):
    type: Literal["text_end"]
    content_index: int
    content: str
    partial: AssistantMessage


class ThinkingStartEvent(TypedDict):
    type: Literal["thinking_start"]
    content_index: int
    partial: AssistantMessage


class ThinkingDeltaEvent(TypedDict):
    type: Literal["thinking_delta"]
    content_index: int
    delta: str
    partial: AssistantMessage


class ThinkingEndEvent(TypedDict):
    type: Literal["thinking_end"]
    content_index: int
    content: str
    partial: AssistantMessage


class ToolCallStartEvent(TypedDict):
    type: Literal["toolcall_start"]
    content_index: int
    partial: AssistantMessage


class ToolCallDeltaEvent(TypedDict):
    type: Literal["toolcall_delta"]
    content_index: int
    delta: str
    partial: AssistantMessage


class ToolCallEndEvent(TypedDict):
    type: Literal["toolcall_end"]
    content_index: int
    tool_call: ToolCall
    partial: AssistantMessage


class DoneEvent(TypedDict):
    type: Literal["done"]
    reason: DoneReason
    message: AssistantMessage


AssistantStreamEvent: TypeAlias = (
    StartEvent
    | TextStartEvent
    | TextDeltaEvent
    | TextEndEvent
    | ThinkingStartEvent
    | ThinkingDeltaEvent
    | ThinkingEndEvent
    | ToolCallStartEvent
    | ToolCallDeltaEvent
    | ToolCallEndEvent
    | DoneEvent
)

StreamFunction: TypeAlias = Callable[
    [ModelSpec, LlmContext, StreamOptions],
    AsyncGenerator[AssistantStreamEvent, None],
]


def check_cancelled(signal: Event | None) -> None:
    if signal is not None and signal.is_set():
        raise CancelledError("执行已取消")
