from collections.abc import AsyncGenerator, Callable
from concurrent.futures import CancelledError
from dataclasses import dataclass
from threading import Event
from typing import Literal, NotRequired, Protocol, TypeAlias, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from app.agent.budget import MAX_MODEL_STEP_ATTEMPTS, RequestBudget
from app.agent.governance import (
    HTTP_CONNECT_SECONDS,
    HTTP_POOL_SECONDS,
    HTTP_READ_SECONDS,
    HTTP_WRITE_SECONDS,
    MODEL_ATTEMPT_SECONDS,
    MODEL_FIRST_INCREMENT_SECONDS,
    MODEL_INCREMENT_INTERVAL_SECONDS,
    RETRY_BACKOFF_CAPS,
    RETRY_MAX_WAIT_SECONDS,
    RunControl,
    RunDeadline,
)
from app.ai.messages import AssistantMessage, Message, ToolCall, Usage


@dataclass(frozen=True)
class ModelTimeouts:
    # 模型请求分项时限：适配器按自身 HTTP 上限与运行剩余预算取较小值。
    http_connect_seconds: float = HTTP_CONNECT_SECONDS
    http_pool_seconds: float = HTTP_POOL_SECONDS
    http_write_seconds: float = HTTP_WRITE_SECONDS
    http_read_seconds: float = HTTP_READ_SECONDS
    first_increment_seconds: float = MODEL_FIRST_INCREMENT_SECONDS
    increment_interval_seconds: float = MODEL_INCREMENT_INTERVAL_SECONDS
    attempt_seconds: float = MODEL_ATTEMPT_SECONDS


DEFAULT_MODEL_TIMEOUTS = ModelTimeouts()


@dataclass(frozen=True)
class RetryPolicy:
    # 公共模型消费边界的网络恢复策略：全抖动上限、单次等待上限与最大尝试次数。
    backoff_caps: tuple[float, ...] = RETRY_BACKOFF_CAPS
    max_wait_seconds: float = RETRY_MAX_WAIT_SECONDS
    max_attempts: int = MAX_MODEL_STEP_ATTEMPTS


DEFAULT_RETRY_POLICY = RetryPolicy()


class ModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    api: str
    provider: str
    id: str
    base_url: str
    context_window: int = Field(gt=0)
    max_tokens: int = Field(gt=0)
    headers: dict[str, str] | None = None


class CallObservation(Protocol):
    # 逐次真实调用的观测句柄：适配器在最终投影处上报请求，在结局处上报用量与状态。
    @property
    def finished(self) -> bool: ...

    def observe(self, request: dict, model: ModelSpec) -> None: ...

    def finish(self, usage: Usage | None, status: str) -> None: ...

    def fail(self, aborted: bool, error_info: dict | None = None) -> None: ...

    def record_first_text(self) -> None: ...


class LlmContext(TypedDict):
    messages: list[Message]


class StreamOptions(TypedDict):
    api_key: str
    signal: NotRequired[Event | None]
    # 运行剩余预算：适配器按自身 HTTP 分项上限与剩余运行预算取较小值；未装配时不限制。
    deadline: NotRequired[RunDeadline | None]
    # 共享请求预算：公共模型消费边界在每次真实发送前检查并扣除，重试同样计数。
    budget: NotRequired[RequestBudget | None]
    # 本次模型请求是否属于恢复额度：恢复后的重发与溢出恢复摘要为 True。
    recovery: NotRequired[bool]
    # 内部消费（摘要）：事件先整体缓冲，失败尝试可丢弃后恢复，不按公开消息边界关闭重试窗口。
    internal: NotRequired[bool]
    # 运行控制：超时终止时记录首个停止原因并发出同一取消信号。
    run_control: NotRequired[RunControl | None]
    # 可注入时间边界与网络恢复策略：生产使用默认值，测试使用缩短预算。
    timeouts: NotRequired[ModelTimeouts | None]
    retry_policy: NotRequired[RetryPolicy | None]
    max_tokens: NotRequired[int | None]
    temperature: NotRequired[float | None]
    headers: NotRequired[dict[str, str] | None]
    # 单次真实调用的观测句柄；由 observation_factory 在每次尝试内创建。
    observation: NotRequired[CallObservation | None]
    # 逐次真实请求的观测工厂：每次尝试创建独立记录，失败尝试不被后续成功覆盖。
    observation_factory: NotRequired[Callable[[], CallObservation | None] | None]


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
