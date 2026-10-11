from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from threading import Event

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
from app.ai.types import ModelSpec
from app.model_config import ModelConfig


@dataclass(frozen=True)
class LoopCompactionResult:
    # 提交成功的压缩检查点身份；messages 为提交后可直接发送的模型投影。
    entry_id: str
    parent_id: str
    messages: list[Message]


@dataclass
class CompactionHooks:
    # 每次模型调用前由装配层提供：当前完整请求计数与一次真实压缩提交。
    estimate_request: Callable[[], Awaitable[int]]
    compact: Callable[[str, Event | None], Awaitable[LoopCompactionResult]]
    context_window: int
    reserve_tokens: int


@dataclass
class AgentLoopConfig:
    model: ModelConfig
    max_turns: int
    # 运行开始时固定的模型能力快照；模型与摘要调用共用同一份。
    model_spec: ModelSpec | None = None
    compaction: CompactionHooks | None = None
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
