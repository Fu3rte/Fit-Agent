from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from src.agent.message_context import ConvertToLlm, TransformContext, convert_to_llm
from src.ai.messages import Message
from src.model_config import ModelConfig


@dataclass
class AgentLoopConfig:
    model: ModelConfig
    max_turns: int
    convert_to_llm: ConvertToLlm = convert_to_llm
    transform_context: TransformContext | None = None
    get_steering_messages: Callable[[], Awaitable[list[Message]]] | None = None
    get_steering_messages_or_close: Callable[[], Awaitable[list[Message]]] | None = None
    on_steering_consumed: Callable[[list[Message]], Awaitable[None]] | None = None
