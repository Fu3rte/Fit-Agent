from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from threading import Event

from app.agent.budget import RequestBudget
from app.agent.cache_observation import CacheObserver
from app.agent.message_context import ConvertToLlm, TransformContext, convert_to_llm
from app.agent.telemetry import TraceSpan
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
from app.domain.session.models import BusinessContextEntry
from app.model_config import ModelConfig


@dataclass(frozen=True)
class LoopCompactionResult:
    # 提交成功的压缩检查点身份；messages 为提交后可直接发送的模型投影；
    # baseline 为同事务提交的完整新基线节点，供压缩完成后按检查点→基线顺序公开身份。
    entry_id: str
    parent_id: str
    messages: list[Message]
    baseline: BusinessContextEntry | None = None


@dataclass
class CompactionHooks:
    # 每次模型调用前由装配层提供：当前完整请求计数与一次真实压缩提交。
    # compact 接收当前 Compaction Span 作为摘要 model 子 Span 的显式父对象。
    estimate_request: Callable[[], Awaitable[int]]
    compact: Callable[[str, Event | None, TraceSpan], Awaitable[LoopCompactionResult]]
    context_window: int
    reserve_tokens: int


@dataclass
class AgentLoopConfig:
    model: ModelConfig
    max_turns: int
    # 运行开始时固定的模型能力快照；模型与摘要调用共用同一份。
    model_spec: ModelSpec | None = None
    # 运行级共享请求预算；普通请求、摘要及恢复重试共用同一实例，未装配时为 None。
    request_budget: RequestBudget | None = None
    compaction: CompactionHooks | None = None
    tool_execution: ExecutionMode = "parallel"
    convert_to_llm: ConvertToLlm = convert_to_llm
    transform_context: TransformContext | None = None
    get_steering_messages: Callable[[], Awaitable[list[Message]]] | None = None
    get_steering_messages_or_close: Callable[[], Awaitable[list[Message]]] | None = None
    # Steering 输入进入上下文前的模型投影；原始消息保持不变，消费仍按原对象身份。
    project_steering: Callable[[list[Message]], list[Message]] | None = None
    on_steering_consumed: Callable[[list[Message]], Awaitable[None]] | None = None
    save_message: Callable[[str, Message], Awaitable[None]] | None = None
    bind_tools: Callable[[str], dict[str, AgentTool]] | None = None
    before_tool_call: BeforeToolCall | None = None
    after_tool_call: AfterToolCall | None = None
    on_tool_update: OnToolUpdate | None = None
    contains_credentials: CredentialCheck | None = None
    # 逐模型调用缓存观测：普通调用与摘要调用在最终投影处上报；未装配时不做观测。
    observation: CacheObserver | None = None
    # 根上下文：正式运行由入口提供，Agent 循环据此统一内部 Trace 身份；未装配时按无记录上下文处理。
    trace_span: TraceSpan | None = None
