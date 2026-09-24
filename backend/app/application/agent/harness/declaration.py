from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from langgraph.graph import MessagesState

from app.application.ports import ModelGateway


class HarnessBudget(Protocol):
    """agent loop 依赖的预算行为契约：模型请求与工具调用各自独立扣减。"""

    def begin_request(self) -> float:
        """登记一次模型请求，返回该次请求可用的超时秒数。"""

    def take_tool_call(self) -> None:
        """登记一次工具调用；耗尽预算的异常由调用方原样向上抛出。"""


@dataclass(frozen=True, slots=True)
class HarnessContext:
    """Harness 运行上下文：只携带 agent loop 通用依赖，业务工具上下文在阶段 3 扩展。"""

    model: ModelGateway
    budget: HarnessBudget
    trace_stage: str = field(default="general", kw_only=True)
    trace_call: Callable[[str, str, str, str, str | None], Awaitable[None]] | None = field(
        default=None, kw_only=True
    )


class HarnessState(MessagesState):
    """Harness 内部状态：消息只存在于当前 Run，不写 WorkflowState。"""
