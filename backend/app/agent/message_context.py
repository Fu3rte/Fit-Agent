from collections.abc import Awaitable, Callable
from concurrent.futures import CancelledError
from inspect import isawaitable
from threading import Event
from typing import Annotated, TypeAlias

from pydantic import Field, TypeAdapter

from app.agent.compaction_summary import (
    COMPACTION_SUMMARY_PREFIX,
    COMPACTION_SUMMARY_SUFFIX,
    CompactionSummaryMessage,
)
from app.ai.context import normalize_context
from app.ai.messages import Message, TextContent, UserMessage

AgentMessage: TypeAlias = Annotated[
    Message | CompactionSummaryMessage,
    Field(discriminator="role"),
]

TransformContext: TypeAlias = Callable[
    [list[AgentMessage], Event | None], Awaitable[list[AgentMessage]]
]
ConvertToLlm: TypeAlias = Callable[
    [list[AgentMessage]], list[Message] | Awaitable[list[Message]]
]
_message_list_adapter = TypeAdapter(list[Message])
_agent_list_adapter = TypeAdapter(list[AgentMessage])


def _project_messages(messages: list[Message]) -> list[Message]:
    validated = _message_list_adapter.validate_python(messages, strict=True)
    return _message_list_adapter.validate_python(
        [message.model_dump(exclude_unset=True) for message in validated], strict=True
    )


def _project_agent_messages(messages: list[AgentMessage]) -> list[AgentMessage]:
    validated = _agent_list_adapter.validate_python(messages, strict=True)
    return _agent_list_adapter.validate_python(
        [message.model_dump(exclude_unset=True) for message in validated], strict=True
    )


def _summary_to_user_message(message: CompactionSummaryMessage) -> Message:
    # 摘要转换为带历史摘要标记的标准 UserMessage，保持历史数据身份而非真实用户确认节点。
    return UserMessage(
        role="user",
        content=[
            TextContent(
                type="text",
                text=(
                    COMPACTION_SUMMARY_PREFIX
                    + message.summary
                    + COMPACTION_SUMMARY_SUFFIX
                ),
            )
        ],
        timestamp=message.timestamp,
    )


def convert_to_llm(messages: list[AgentMessage]) -> list[Message]:
    projected = _project_agent_messages(messages)
    return _project_messages(
        [
            _summary_to_user_message(message)
            if isinstance(message, CompactionSummaryMessage)
            else message
            for message in projected
        ]
    )


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
    projected = _project_agent_messages(messages)
    checkpoint()
    if transform_context is not None:
        projected = await transform_context(projected, signal)
        checkpoint()
        projected = _project_agent_messages(projected)
        checkpoint()
    converted = convert_to_llm(projected)
    if isawaitable(converted):
        converted = await converted
    checkpoint()
    normalized = normalize_context(converted)
    checkpoint()
    return normalized
