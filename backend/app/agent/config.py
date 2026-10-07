from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.agent.message_context import ConvertToLlm, TransformContext, convert_to_llm
from app.agent.tool import (
    AfterToolCall,
    AgentTool,
    BeforeToolCall,
    CredentialCheck,
    ExecutionMode,
    OnToolUpdate,
)
from app.ai.messages import Message
from app.model_config import ModelConfig


@dataclass
class AgentLoopConfig:
    model: ModelConfig
    max_turns: int
    tool_execution: ExecutionMode = "parallel"
    convert_to_llm: ConvertToLlm = convert_to_llm
    transform_context: TransformContext | None = None
    get_steering_messages: Callable[[], Awaitable[list[Message]]] | None = None
    get_steering_messages_or_close: Callable[[], Awaitable[list[Message]]] | None = None
    on_steering_consumed: Callable[[list[Message]], Awaitable[None]] | None = None
    save_message: Callable[[str, Message], Awaitable[None]] | None = None
    bind_tools: Callable[[str], dict[str, AgentTool]] | None = None
    before_tool_call: BeforeToolCall | None = None
    after_tool_call: AfterToolCall | None = None
    on_tool_update: OnToolUpdate | None = None
    contains_credentials: CredentialCheck | None = None
