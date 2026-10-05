from collections.abc import Awaitable, Callable
from concurrent.futures import CancelledError
from inspect import isawaitable
from threading import Event
from typing import TypeAlias

from pydantic import TypeAdapter

from app.ai.context import normalize_context
from app.ai.messages import Message

AgentMessage: TypeAlias = Message

TransformContext: TypeAlias = Callable[
    [list[AgentMessage], Event | None], Awaitable[list[AgentMessage]]
]
ConvertToLlm: TypeAlias = Callable[
    [list[AgentMessage]], list[Message] | Awaitable[list[Message]]
]
_message_list_adapter = TypeAdapter(list[Message])


def _project_messages(messages: list[AgentMessage]) -> list[Message]:
    validated = _message_list_adapter.validate_python(messages, strict=True)
    return _message_list_adapter.validate_python(
        [message.model_dump(exclude_unset=True) for message in validated], strict=True
    )


def convert_to_llm(messages: list[AgentMessage]) -> list[Message]:
    return _project_messages(messages)


async def prepare_message_context(
    messages: list[AgentMessage],
    *,
    transform_context: TransformContext | None = None,
    convert_to_llm: ConvertToLlm = convert_to_llm,
    signal: Event | None = None,
) -> list[Message]:
    def checkpoint() -> None:
        if signal is not None and signal.is_set():
            raise CancelledError("请求上下文处理已取消")

    checkpoint()
    projected = _project_messages(messages)
    checkpoint()
    if transform_context is not None:
        projected = await transform_context(projected, signal)
        checkpoint()
        projected = _project_messages(projected)
        checkpoint()
    converted = convert_to_llm(projected)
    if isawaitable(converted):
        converted = await converted
    checkpoint()
    normalized = normalize_context(converted)
    checkpoint()
    return normalized
