import asyncio
import json
import logging
import os
import re
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from math import ceil
from queue import Empty
from threading import Event, Lock
from time import monotonic, perf_counter, time, time_ns
from traceback import walk_tb
from typing import Annotated, Literal
from uuid import UUID, uuid4
from weakref import WeakValueDictionary

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from limits import parse
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from starlette.datastructures import URL
from starlette.routing import compile_path
from starlette.types import ASGIApp, Receive, Scope, Send

from app.agent.agent_loop import ModelOutputLimitExceeded, run_agent_loop
from app.agent.budget import ModelRequestLimitExceeded, RequestBudget
from app.agent.cache_observation import CacheObserver
from app.agent.compaction import (
    KEEP_RECENT_TOKENS,
    RESERVE_TOKENS,
    CompactionSettings,
    CompactionSummaryError,
    ContextBudgetExceeded,
    NoSummarizableHistory,
    build_effective_context,
    estimate_context_tokens,
    generate_compaction_summary,
    prepare_compaction,
    project_user_message,
)
from app.agent.config import AgentLoopConfig, CompactionHooks, LoopCompactionResult
from app.agent.diagnostic import run_diagnostic
from app.agent.events import AgentEvent
from app.agent.governance import (
    CANCEL_COOPERATIVE_WAIT_SECONDS,
    DIAGNOSTIC_WRITE_WAIT_SECONDS,
    SESSION_RUN_RATE_LIMIT,
    SSE_EVENT_MAX_BYTES,
    SSE_HEARTBEAT_SECONDS,
    SSE_QUEUE_MAX_BYTES,
    SSE_QUEUE_MAX_EVENTS,
    SSE_SEND_WAIT_SECONDS,
    TERMINAL_DRAIN_TOTAL_SECONDS,
    TERMINAL_PERSIST_WAIT_SECONDS,
    TOOL_EXECUTION_SECONDS,
    RunControl,
    RunDeadline,
    RunStopReason,
    configured_run_budget_seconds,
    stop_terminal,
)
from app.agent.message_context import business_context_to_llm
from app.agent.permissions import create_before_tool_call
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.telemetry import RunTrace, TraceSpan, connect_trace_store
from app.agent.tool import CredentialDetectedError, ToolExecutionTracker
from app.agent.tools.business import (
    bind_business_tools,
    business_tool_declarations,
)
from app.agent.tools.files import create_file_tools
from app.ai.context import get_current_system_message
from app.ai.messages import (
    AssistantMessage,
    Message,
    SystemMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    text_projection,
)
from app.ai.model_capabilities import resolve_model_spec
from app.ai.overflow import ContextOverflowError
from app.ai.quota import ProviderQuotaExceeded
from app.ai.types import check_cancelled
from app.application.business.catalog import Catalog
from app.application.business.confirmation import ConfirmationService
from app.application.business.coordination import ReplacementCoordinator
from app.application.business.retrieval_runtime import ActionRetrievalRuntime
from app.application.business.service import (
    BUSINESS_TIMEZONE,
    BusinessService,
    business_date,
)
from app.application.session.attachment_files import AttachmentFiles
from app.application.session.business_context import (
    BusinessBindings,
    collect_message_nodes,
    collect_pending_proposals,
    next_business_context_event,
)
from app.application.session.service import CREDENTIAL_SAFE_MESSAGE, SessionService
from app.application.session.steering import SteeringCoordinator
from app.domain.business.errors import (
    BusinessError,
    ConfirmationNotFound,
    business_http_status,
)
from app.domain.business.models import (
    BusinessContext,
    SaveConfirmationOperation,
    SaveConfirmationRequest,
    WorkoutListArguments,
)
from app.domain.session.attachments import (
    AttachmentError,
    AttachmentInput,
)
from app.domain.session.errors import (
    CredentialDetected,
    EntryNotFound,
    IncompleteToolChain,
    InvalidTargetEntry,
    OperationConflict,
    OperationExpired,
    RunBusy,
    RunClosed,
    RunNotFound,
    SessionConflict,
    SessionMismatch,
    SessionNotFound,
    SteeringConsumptionConflict,
)
from app.domain.session.models import (
    BusinessContextData,
    BusinessContextEntry,
    BusinessContextSnapshot,
    CompactionDetails,
    CompactionEntry,
    EditCommand,
    EditRequest,
    OperationOutcome,
    RegenerateCommand,
    RegenerateRequest,
    RequestContext,
    SendCommand,
    SendRequest,
    SessionEntry,
    SessionMessageEntry,
    SteeringCommand,
    SteeringInput,
    build_business_context_entry,
    build_business_context_message,
    business_baseline_entry_id,
)
from app.domain.session.models import (
    SteeringRequest as SteeringParams,
)
from app.infrastructure.persistence.sqlite.business_repository import (
    SqliteBusinessRepository,
)
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from app.model_config import (
    API_VALUES,
    ModelNotConfigured,
    clear_provider,
    effective_provider,
    load_model_config,
    load_provider_status,
    save_provider,
    saved_api_key,
    validate_http_url,
)

frontend_port = os.environ.get("FIT_AGENT_FRONTEND_PORT", "5173")
if re.fullmatch(r"[0-9]+", frontend_port) is None or not 1 <= int(frontend_port) <= 65535:
    raise ValueError("FIT_AGENT_FRONTEND_PORT 必须是 1–65535 的整数")
FRONTEND_PORT = int(frontend_port)
ALLOWED_HOSTS = {f"{host}:{port}" for host in ("127.0.0.1", "localhost") for port in (8000, FRONTEND_PORT)}
logger = logging.getLogger(__name__)
active = Lock()
runs_lock = Lock()
# 同键并发请求串行到受理提交：落败者等待后命中持久化结果。键随引用消失，不累积。
operation_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def operation_lock(operation_id: str) -> asyncio.Lock:
    return operation_locks.setdefault(operation_id, asyncio.Lock())


# 会话协调入口：新运行受理与会话删除在此串行，覆盖受理提交至内存登记间的竞态窗口。
session_gates: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def session_gate(session_id: str) -> asyncio.Lock:
    return session_gates.setdefault(session_id, asyncio.Lock())


class SessionRunRateLimiter:
    """会话新运行受理额度：移动窗口、内存存储，仅成功受理扣减。"""

    def __init__(self, rate: str = SESSION_RUN_RATE_LIMIT) -> None:
        self._rate = parse(rate)
        self._limiter = MovingWindowRateLimiter(MemoryStorage())

    def available(self, session_id: str) -> bool:
        return self._limiter.test(self._rate, session_id)

    def consume(self, session_id: str) -> bool:
        return self._limiter.hit(self._rate, session_id)

    def remaining(self, session_id: str) -> int:
        return self._limiter.get_window_stats(self._rate, session_id).remaining

    def retry_after(self, session_id: str) -> int:
        # Retry-After 为整数秒；窗口内仍有拒绝时至少返回 1 秒。
        stats = self._limiter.get_window_stats(self._rate, session_id)
        return max(1, ceil(stats.reset_time - time()))


session_run_limits = SessionRunRateLimiter()

# 故障封闭：协作收尾或终态持久化失败后拒绝新运行，直到人工受控重启。
_fault_lock = Lock()
_fault_closed = False


def enter_fault_closure() -> None:
    global _fault_closed
    with _fault_lock:
        _fault_closed = True


def is_fault_closed() -> bool:
    with _fault_lock:
        return _fault_closed


def reset_fault_closure() -> None:
    global _fault_closed
    with _fault_lock:
        _fault_closed = False


# 队列结果：普通输出只有成功入队才继续接收；超限后终止接收并请求取消。
QUEUE_OK = "ok"
QUEUE_CLOSED = "closed"


@dataclass(frozen=True)
class EncodedEvent:
    event: AgentEvent
    payload: bytes

    @property
    def name(self) -> str:
        return self.event.event


class BoundedEventQueue:
    """单运行有界 SSE 队列：普通事件受数量与字节上限约束，终态使用独立槽位。"""

    def __init__(
        self,
        *,
        max_events: int = SSE_QUEUE_MAX_EVENTS,
        max_bytes: int = SSE_QUEUE_MAX_BYTES,
        max_event_bytes: int = SSE_EVENT_MAX_BYTES,
    ) -> None:
        self._lock = Lock()
        self._items: deque[EncodedEvent] = deque()
        self._bytes = 0
        self._accepting = True
        self._terminal: EncodedEvent | None = None
        self._terminal_at: float | None = None
        self.max_events = max_events
        self.max_bytes = max_bytes
        self.max_event_bytes = max_event_bytes

    @property
    def pending_events(self) -> int:
        with self._lock:
            return len(self._items)

    @property
    def pending_bytes(self) -> int:
        with self._lock:
            return self._bytes

    @property
    def accepting(self) -> bool:
        with self._lock:
            return self._accepting

    @property
    def terminal_filled(self) -> bool:
        with self._lock:
            return self._terminal is not None

    def put(self, event: EncodedEvent) -> str:
        size = len(event.payload)
        with self._lock:
            if size > self.max_event_bytes:
                self._accepting = False
                return "output_too_large"
            if not self._accepting:
                return QUEUE_CLOSED
            if (
                len(self._items) >= self.max_events
                or self._bytes + size > self.max_bytes
            ):
                # 队列积压超限：停止接收普通输出并请求取消，生产线程不得等待。
                self._accepting = False
                return "slow_consumer"
            self._items.append(event)
            self._bytes += size
            return QUEUE_OK

    def put_terminal(self, event: EncodedEvent) -> str:
        # 终态槽位不受普通队列容量影响；填充时刻是排空总时限起点。
        size = len(event.payload)
        with self._lock:
            self._accepting = False
            if self._terminal is not None:
                return QUEUE_CLOSED
            if size > self.max_event_bytes:
                return "output_too_large"
            self._terminal = event
            self._terminal_at = monotonic()
            return QUEUE_OK

    def take(self) -> EncodedEvent | None:
        with self._lock:
            if not self._items:
                return None
            event = self._items.popleft()
            self._bytes -= len(event.payload)
            return event

    def empty(self) -> bool:
        with self._lock:
            return not self._items and self._terminal is None

    def get_nowait(self) -> AgentEvent:
        # 既有检查脚本使用的队列读取兼容入口；生产发送方分别走 take()/take_terminal()。
        event = self.take()
        if event is not None:
            return event.event
        with self._lock:
            if self._terminal is not None:
                terminal = self._terminal
                self._terminal = None
                return terminal.event
        raise Empty

    def take_terminal(self) -> EncodedEvent | None:
        with self._lock:
            return self._terminal

    def terminal_remaining(self) -> float | None:
        with self._lock:
            if self._terminal_at is None:
                return None
            return TERMINAL_DRAIN_TOTAL_SECONDS - (monotonic() - self._terminal_at)

    def close_normal(self) -> None:
        with self._lock:
            self._accepting = False


TERMINAL_RUN_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "interrupted"}
)

# 固定公开文案：SSE error 与运行记录使用完全相同的文本。
COMPACTION_FAILED_MESSAGE = "上下文压缩失败，请重新发起请求。"
CONTEXT_BUDGET_EXCEEDED_MESSAGE = "当前模型的上下文预算不足，无法继续执行。"
CONTEXT_OVERFLOW_MESSAGE = "恢复后仍超出容量，无法继续执行。"
MODEL_OUTPUT_LIMIT_MESSAGE = "回答达到输出上限，内容未完成"
PROVIDER_QUOTA_EXCEEDED_MESSAGE = "模型服务账户额度不足，无法继续执行。"
# 治理停止原因的固定公开文案：与运行记录使用完全相同的文本。
MODEL_TIMEOUT_MESSAGE = "模型响应超时，请重新发起请求。"
TOOL_TIMEOUT_MESSAGE = "工具执行超时，已结束本次运行。"
RUN_TIMEOUT_MESSAGE = "本次运行已超时结束。"
SLOW_CONSUMER_MESSAGE = "客户端读取过慢，已结束本次运行。"
OUTPUT_TOO_LARGE_MESSAGE = "输出内容超过单事件上限，已结束本次运行。"
STOP_REASON_MESSAGES: dict[str, str] = {
    "model_timeout": MODEL_TIMEOUT_MESSAGE,
    "tool_timeout": TOOL_TIMEOUT_MESSAGE,
    "run_timeout": RUN_TIMEOUT_MESSAGE,
    "slow_consumer": SLOW_CONSUMER_MESSAGE,
    "output_too_large": OUTPUT_TOO_LARGE_MESSAGE,
}


def compaction_settings() -> CompactionSettings:
    # 生产预算固定为 reserve 16384、keep 20000；环境变量仅供测试注入缩小或调整触发条件的预算。
    reserve = os.environ.get("FIT_AGENT_COMPACTION_RESERVE_TOKENS")
    keep = os.environ.get("FIT_AGENT_COMPACTION_KEEP_RECENT_TOKENS")
    return CompactionSettings(
        reserve_tokens=int(reserve) if reserve is not None else RESERVE_TOKENS,
        keep_recent_tokens=int(keep) if keep is not None else KEEP_RECENT_TOKENS,
    )


class CredentialFilter:
    def __init__(self, secrets):
        self._secrets = tuple(secret for secret in secrets if secret)
        self._hold = max((len(secret) for secret in self._secrets), default=0) - 1

    def contains(self, value) -> bool:
        if isinstance(value, str):
            return any(secret in value for secret in self._secrets)
        if isinstance(value, list):
            return any(self.contains(item) for item in value)
        if isinstance(value, dict):
            return any(
                self.contains(key) or self.contains(item)
                for key, item in value.items()
            )
        return False

    def tail_safe(self, text: str) -> str:
        if self._hold <= 0:
            return text
        return text[: max(0, len(text) - self._hold)]


_NO_CREDENTIALS = CredentialFilter(())


def path_uuid(value: str) -> str:
    # 路径身份只接受标准带连字符 UUID；紧凑、URN 及花括号形式按非法请求拒绝。
    if not isinstance(value, str):
        raise ValueError("身份必须为字符串")
    canonical = str(UUID(value))
    if value.lower() != canonical:
        raise ValueError("路径身份必须为标准带连字符的 UUID")
    return canonical


PathUUID = Annotated[str, BeforeValidator(path_uuid)]


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: PathUUID
    title: str = Field(strict=True, min_length=1, max_length=32000)

    @field_validator("title")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title 必须包含非空文本")
        return value


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: PathUUID
    operation_id: PathUUID
    request: str = Field(max_length=32000)
    attachments: list[AttachmentInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_content(self):
        if not self.request.strip() and not self.attachments:
            raise ValueError("请求必须包含文字或附件")
        return self


class EditPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: PathUUID
    operation_id: PathUUID
    target_entry_id: PathUUID
    request: str = Field(max_length=32000)
    attachments: list[AttachmentInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_content(self):
        if "attachments" in self.model_fields_set and not self.request.strip() and not self.attachments:
            raise ValueError("请求必须包含文字或附件")
        return self


class RegeneratePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: PathUUID
    operation_id: PathUUID
    target_entry_id: PathUUID


class SteeringRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: PathUUID
    operation_id: PathUUID
    message: str = Field(max_length=32000)
    attachments: list[AttachmentInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_content(self):
        if not self.message.strip() and not self.attachments:
            raise ValueError("请求必须包含文字或附件")
        return self


class WithdrawRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: PathUUID


class SaveConfirmationWire(BaseModel):
    # §7.2：确认请求恰好四字段。确认节点、业务正文、基础版本、训练记录 ID、
    # 保存工具名、运行 ID 及文字确认内容一律由后端从已绑定快照取得，客户端不得提交。
    model_config = ConfigDict(extra="forbid", strict=True)
    operation_id: PathUUID
    business_kind: Literal["profile", "plan", "workout"]
    proposal_id: PathUUID
    display_entry_id: PathUUID


class ProviderWriteBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    api: str
    base_url: str
    model: str
    api_key: str
    provider: str = ""

    @field_validator("api")
    @classmethod
    def registered(cls, value: str) -> str:
        if value not in API_VALUES:
            raise ValueError("仅支持 openai-completions 与 anthropic-messages")
        return value

    @field_validator("base_url")
    @classmethod
    def http_url(cls, value: str) -> str:
        return validate_http_url(value)

    @field_validator("model", "api_key")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("字段必须包含非空白内容")
        return value


class RunState:
    def __init__(self, session_id: UUID, *, budget_seconds: float | None = None):
        self.run_id = uuid4()
        self.session_id = session_id
        self.operation_id = ""
        self.request_entry_id = ""
        self.cancel = Event()
        # 有界输出队列：所有生产者共用同一入口，终态使用独立槽位。
        self.events = BoundedEventQueue()
        # 运行控制上下文：与 state.cancel 共用同一取消信号，另携带单调截止时间与首个停止原因；
        # 主事件循环与工作线程通过该对象共享运行级治理状态。
        self.control = RunControl(
            deadline=RunDeadline(
                configured_run_budget_seconds()
                if budget_seconds is None
                else budget_seconds
            ),
            cancel=self.cancel,
        )
        # 运行级在途工具执行证据：与执行线程看到同一实例。
        self.tool_tracker = ToolExecutionTracker()
        self.coordinator: SteeringCoordinator | None = None
        self.task: asyncio.Task | None = None
        self.closed_without_terminal = False
        self.secrets: tuple[str, ...] = ()
        # 运行来源：send / edit / regenerate；观测据此标注编辑与分支等已知事件。
        self.origin = "send"
        # 故障证据：收尾超时后保留真实在途句柄与原因，供人工恢复核对。
        self.fault_reason: str | None = None
        self.inflight_work = None
        self.inflight_tools: tuple = ()
        # 有界协调读取的底层任务：等待超时后继续保留，禁止把超时当作回滚证据。
        self.inflight_reads: list[asyncio.Task] = []
        # 故障诊断写入异步进行：数据库等待不得再次阻塞主事件循环或收尾。
        self.diagnostic_tasks: list[asyncio.Task] = []
        self._state_lock = Lock()
        self._last_tool_call_id: str | None = None

    def publish(self, name: str, data: dict) -> bool:
        # 生产线程与主事件循环共用入口：编码后计量，超限不阻塞、不拆分业务快照。
        with self._state_lock:
            if name == "tool_start":
                self._last_tool_call_id = data.get("tool_call_id")
            elif name == "tool_result":
                self._last_tool_call_id = None
        event = AgentEvent(name, {"run_id": str(self.run_id), **data})
        outcome = self.events.put(
            EncodedEvent(event, encode(event).encode("utf-8"))
        )
        if outcome == "output_too_large":
            self.control.request_cancel("output_too_large")
        elif outcome == "slow_consumer":
            self.control.request_cancel("slow_consumer")
        return outcome == QUEUE_OK

    def publish_terminal(self, name: Literal["done", "error"], data: dict) -> None:
        # 仅终态持久化成功后调用；独立槽位在普通队列已满时仍可登记。
        if name == "error":
            with self._state_lock:
                tool_call_id = self._last_tool_call_id
            data = {"tool_call_id": tool_call_id, **data}
        event = AgentEvent(name, {"run_id": str(self.run_id), **data})
        outcome = self.events.put_terminal(
            EncodedEvent(event, encode(event).encode("utf-8"))
        )
        if outcome != "output_too_large":
            return
        # 终态帧本身超过单事件上限：发布固定最小错误，不公开部分内容。
        fallback = AgentEvent("error", {
            "run_id": str(self.run_id),
            "status": "failed",
            "code": "output_too_large",
            "message": OUTPUT_TOO_LARGE_MESSAGE,
            "tool_call_id": None,
        })
        self.events.put_terminal(
            EncodedEvent(fallback, encode(fallback).encode("utf-8"))
        )

    def disconnect(self) -> None:
        self.control.request_cancel("cancelled")
        if self.coordinator is not None:
            self.coordinator.close(str(self.run_id))

    def mark_fault(self, reason: str, work, tools: tuple) -> None:
        # 故障封闭：不提交成功确认、不释放名额；保留真实在途句柄供重启核对。
        self.fault_reason = reason
        self.inflight_work = work
        self.inflight_tools = tools
        self.closed_without_terminal = True
        self.events.close_normal()


runs: dict[UUID, RunState] = {}


def retrieval_secret() -> str | None:
    # 检索密钥凭据保护：直接读环境变量，不触发检索配置校验、不打印，未配置时为空。
    value = os.environ.get("DASHSCOPE_API_KEY")
    return value if value and value.strip() else None


def active_secrets(session_id: str | None = None) -> tuple[str, ...]:
    # 合并当前保存的凭据与在途运行快照：清除或更换 Key 后，仍在使用的凭据继续参与检查。
    current = saved_api_key()
    with runs_lock:
        states = list(runs.values())
    held = [
        secret
        for state in states
        if session_id is None or str(state.session_id) == str(session_id)
        for secret in state.secrets
    ]
    return tuple(secret for secret in (current, retrieval_secret(), *held) if secret)


@asynccontextmanager
async def lifespan(app: FastAPI):
    reset_fault_closure()
    database = await open_database()
    service = SessionService(SqliteSessionRepository(database))
    await service.recover_interrupted()
    business_repository = SqliteBusinessRepository(database)
    catalog = Catalog.load()
    replacements = ReplacementCoordinator()
    # 检索资源按需初始化：此处仅创建持有者，缺省配置或 Milvus 不可用只影响后续检索请求。
    retrieval = ActionRetrievalRuntime()
    business = BusinessService(business_repository, catalog, service, replacements, retrieval)
    service.attach_snapshots(business)
    confirmations = ConfirmationService(business_repository, service, business)
    # 中断的保存在事务未提交时已随数据库回滚；恢复完成前不接收任何保存请求。
    await business_repository.recover_interrupted_saves()
    await business_repository.recover_interrupted_workout_saves()
    await business_repository.recover_interrupted_plan_saves()
    # 确认对账排在业务保存恢复之后：核对处理中操作的保存记录，恢复 saved 或登记中断失败。
    await confirmations.recover_interrupted()
    async with business_repository.transaction():
        await business_repository.replace_exercises(catalog.all())
    # 观测连接在主事件循环获取：开库故障隔离为 None，业务服务沿用既有生命周期。
    trace_store = await connect_trace_store()
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    app.state.trace_store = trace_store
    app.state.session_service = service
    app.state.steering = SteeringCoordinator(service)
    app.state.business = business
    app.state.confirmations = confirmations
    app.state.database = database
    app.state.replacements = replacements
    app.state.retrieval = retrieval
    app.state.loop = loop
    app.state.executor = executor
    app.state.coordination = set()
    app.state.accepting_runs = True
    try:
        yield
    finally:
        # 停止接收新运行，向已登记运行发出取消信号并关闭 Steering 接收入口。
        app.state.accepting_runs = False
        with runs_lock:
            states = list(runs.values())
        for state in states:
            state.control.request_cancel("cancelled")
            if state.coordinator is not None:
                state.coordinator.close(str(state.run_id))
        # 异步排空收尾任务，覆盖与受理交错后刚登记的任务，期间主事件循环保持可调度。
        while True:
            with runs_lock:
                tasks = list(app.state.coordination)
            if not tasks:
                break
            await asyncio.gather(*tasks, return_exceptions=True)
        await loop.run_in_executor(None, executor.shutdown, True)
        # 运行与取消收尾排空后再在所属循环关闭检索客户端；随后关闭观测与业务库。
        await retrieval.close()
        if trace_store is not None:
            await trace_store.close()
        await database.close()


app = FastAPI(lifespan=lifespan)
NO_STORE = {"Cache-Control": "no-store"}


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, error: RequestValidationError):
    code = "invalid_request"
    if any(item["type"] == "attachment_format_invalid" or (
        item["type"] == "value_error" and "attachments" in item["loc"]
        and item["loc"][-1] == "data_base64"
    ) for item in error.errors()):
        code = "attachment_format_invalid"
    return JSONResponse(status_code=422, content={
        "detail": {"code": code, "message": "请求字段不合法。"},
    }, headers=NO_STORE)


@app.exception_handler(HTTPException)
async def http_error(request: Request, error: HTTPException):
    headers = {**NO_STORE, **(error.headers or {})}
    return JSONResponse({"detail": error.detail}, error.status_code, headers=headers)


@app.exception_handler(BusinessError)
async def business_error(request: Request, error: BusinessError):
    detail = error.detail()
    if CredentialFilter(active_secrets()).contains(detail):
        return JSONResponse({"detail": {
            "code": "credential_detected", "message": CREDENTIAL_SAFE_MESSAGE,
        }}, 422, headers=NO_STORE)
    return JSONResponse({"detail": detail}, error.http_status, headers=NO_STORE)


def stored_attachment_read_error(error: BaseException) -> bool:
    codes = {
        SqliteSessionRepository.get_attachments.__code__,
        SqliteSessionRepository.list_entry_attachments.__code__,
        SqliteSessionRepository.list_steering_attachments.__code__,
    }
    return any(frame.f_code in codes for frame, _ in walk_tb(error.__traceback__))


@app.exception_handler(ValueError)
async def attachment_metadata_error(request: Request, error: ValueError):
    if not stored_attachment_read_error(error):
        raise error
    return JSONResponse({"detail": {
        "code": "internal_error", "message": "服务内部错误。",
    }}, 500, headers=NO_STORE)


@app.exception_handler(AttachmentError)
async def attachment_error(request: Request, error: AttachmentError):
    code = error.code
    if code == "attachment_format_invalid" and (
        stored_attachment_read_error(error)
        or any(frame.f_code is AttachmentFiles.read.__code__ for frame, _ in walk_tb(error.__traceback__))
    ):
        code = "internal_error"
    status = {
        "invalid_request": 422, "attachment_format_invalid": 422,
        "attachment_size_exceeded": 413, "attachment_not_found": 404,
        "attachment_access_denied": 403, "attachment_conflict": 409,
        "internal_error": 500,
    }[code]
    return JSONResponse({"detail": {"code": code, "message": "附件操作失败。"}}, status, headers=NO_STORE)


@app.exception_handler(OSError)
@app.exception_handler(UnicodeDecodeError)
async def attachment_native_error(request: Request, error: OSError | UnicodeDecodeError):
    frames = {frame.f_code for frame, _ in walk_tb(error.__traceback__)}
    boundary = AttachmentFiles.read.__code__ in frames
    if isinstance(error, OSError):
        boundary = boundary or bool(frames & {
            AttachmentFiles.write.__code__, AttachmentFiles.prepare.__code__,
            AttachmentFiles.remove_created.__code__,
        })
    if not boundary:
        raise error
    return JSONResponse({"detail": {
        "code": "internal_error", "message": "服务内部错误。",
    }}, 500, headers=NO_STORE)


@app.exception_handler(Exception)
async def internal_error(request: Request, error: Exception):
    return JSONResponse(status_code=500, content={
        "detail": {"code": "internal_error", "message": "服务内部错误。"},
    }, headers=NO_STORE)


@app.exception_handler(ModelNotConfigured)
async def model_not_configured(request: Request, error: ModelNotConfigured):
    return JSONResponse(status_code=409, content={
        "detail": {
            "code": "model_not_configured",
            "message": "尚未配置可用的模型，无法发起新的运行。",
        },
    }, headers=NO_STORE)


def reject(
    status: int,
    code: str,
    message: str,
    *,
    reason: str | None = None,
    headers: dict[str, str] | None = None,
) -> None:
    detail = {"code": code, "message": message}
    if reason is not None:
        detail["reason"] = reason
    raise HTTPException(status, detail, headers=headers)


def reject_operation_expired() -> None:
    reject(409, "operation_conflict", "该操作已失效。", reason="operation_expired")


async def reject_if_expired(
    service: SessionService, operation_id: str, session_id: str
) -> None:
    if await service.get_operation_invalidation(operation_id) == session_id:
        reject_operation_expired()


def encode(event: AgentEvent) -> str:
    # 最终 SSE 编码是唯一的字节计量口径：包含事件名、JSON 转义、封装与追加字段。
    return (
        f"event: {event.event}\n"
        f"data: {json.dumps(event.data, ensure_ascii=False, allow_nan=False)}\n\n"
    )


def public_content(message: AssistantMessage, secrets: CredentialFilter, truncate: bool) -> list[dict]:
    def text(value: str) -> str:
        return secrets.tail_safe(value) if truncate else value

    content = []
    for index, block in enumerate(message.content):
        if isinstance(block, TextContent):
            content.append({
                "content_index": index,
                "type": "text",
                "text": text(block.text),
            })
        elif isinstance(block, ThinkingContent) and not block.redacted:
            content.append({
                "content_index": index,
                "type": "thinking",
                "thinking": text(block.thinking),
            })
        elif isinstance(block, ToolCall):
            content.append({
                "content_index": index,
                "type": "tool_call",
                "tool_call_id": block.id,
                "name": block.name,
                "arguments": block.arguments,
            })
    return content


def public_text_visible(content: list[dict]) -> bool:
    # 首正文判定以公开投影为准：原始正文非空但公开 text 为空时不构成可见正文。
    return any(block["type"] == "text" and block["text"] for block in content)


def public_tool_execution_end(event: dict) -> dict:
    # 内部完成事件到公开字段的映射：content 沿用工具结果的文本投影，不携带持久化节点身份。
    return {
        "tool_call_id": event["tool_call_id"],
        "tool_name": event["name"],
        "content": event["content"],
        "is_error": event["is_error"],
    }


def public_tool_execution_update(
    tool_call_id: str, tool_name: str, content: list, is_error: bool
) -> dict:
    # 进度快照：content 使用与最终结果一致的文本投影，前端直接替换；不携带持久化节点身份。
    return {
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "content": text_projection(content),
        "is_error": is_error,
    }


def public_message(message: Message) -> dict:
    if isinstance(message, SystemMessage):
        return {"role": "system"}
    if isinstance(message, UserMessage):
        return {
            "role": "user",
            "attachments": [],
            "text": text_projection(message.content),
            "timestamp": message.timestamp,
        }
    if isinstance(message, AssistantMessage):
        return {
            "role": "assistant",
            "content": public_content(message, _NO_CREDENTIALS, False),
            "stop_reason": message.stop_reason,
            "timestamp": message.timestamp,
        }
    if isinstance(message, ToolResultMessage):
        return {
            "role": "toolResult",
            "tool_call_id": message.tool_call_id,
            "tool_name": message.tool_name,
            "content": text_projection(message.content),
            "is_error": message.is_error,
            "timestamp": message.timestamp,
        }
    raise ValueError("未知消息角色")


def attachment_object(metadata) -> dict:
    return {
        "attachment_id": metadata.attachment_id, "file_name": metadata.file_name,
        "size_bytes": metadata.size_bytes, "created_at": metadata.created_at,
    }


def history_entry(entry: SessionEntry) -> dict:
    # 公开投影按节点类型白名单：压缩节点与内部资料节点只公开身份与父子关系，摘要、用量、
    # 系统状态、业务资料及内部附件路径保留在后端，不能直接 model_dump 作为公开响应。
    public = {
        "type": entry.type,
        "entry_id": entry.id,
        "parent_id": entry.parent_id,
        "run_id": entry.run_id,
        "created_at": entry.created_at,
    }
    if isinstance(entry, (CompactionEntry, BusinessContextEntry)):
        return public
    message = public_message(entry.messages[0])
    if message["role"] == "user":
        message["attachments"] = [attachment_object(item) for item in entry.attachments]
    return {**public, "message": message}


def history_steering(steering: SteeringInput) -> dict:
    return {
        "session_id": steering.session_id,
        "run_id": steering.run_id,
        "steering_id": steering.id,
        "text": text_projection(steering.message.content),
        "attachments": [attachment_object(item) for item in steering.attachments],
        "timestamp": steering.message.timestamp,
        "status": steering.status,
        "entry_id": steering.entry_id,
        "reason": steering.reason,
        "created_at": steering.created_at,
        "updated_at": steering.updated_at,
    }


def session_object(session) -> dict:
    return {
        "session_id": session.id,
        "title": session.title,
        "active_leaf_id": session.active_leaf_id,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


def run_object(run) -> dict:
    return {
        "session_id": run.session_id,
        "run_id": run.id,
        "request_entry_id": run.request_entry_id,
        "last_entry_id": run.last_entry_id,
        "status": run.status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "error_code": run.error_code,
        "error_message": run.error_message,
        "stop_reason": run.stop_reason,
    }


def steering_object(steering) -> dict:
    return {
        "attachments": [attachment_object(item) for item in steering.attachments],
        "session_id": steering.session_id,
        "run_id": steering.run_id,
        "steering_id": steering.id,
        "status": steering.status,
        "entry_id": steering.entry_id,
        "reason": steering.reason,
        "created_at": steering.created_at,
        "updated_at": steering.updated_at,
    }


def send_result(operation_id: str, run) -> dict:
    return {
        "operation_id": operation_id,
        "session_id": run.session_id,
        "run_id": run.id,
        "request_entry_id": run.request_entry_id,
        "status": run.status,
    }


async def check_boundary(request: Request) -> None:
    hosts = request.headers.getlist("host")
    origins = request.headers.getlist("origin")
    if len(hosts) != 1 or hosts[0] not in ALLOWED_HOSTS:
        reject(403, "host_forbidden", "Host 不允许")
    if origins:
        if len(origins) != 1:
            reject(403, "origin_forbidden", "Origin 不允许")
        origin = URL(origins[0])
        local = re.fullmatch(r"(?:localhost|127\.0\.0\.1):([0-9]{1,5})", origin.netloc)
        if (local is None or not 1 <= int(local[1]) <= 65535
                or origins[0] != f"http://{origin.netloc}"):
            reject(403, "origin_forbidden", "Origin 不允许")


class AttachmentBodyLimit:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.steering_path = compile_path("/api/agent/runs/{run_id}/steering")[0]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        protected = scope["type"] == "http" and scope["method"] == "POST" and (
            scope["path"] in ("/api/agent/run", "/api/agent/edit")
            or self.steering_path.fullmatch(scope["path"]) is not None
        )
        if not protected:
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        try:
            await check_boundary(request)
        except HTTPException as error:
            await JSONResponse({"detail": error.detail}, error.status_code, headers=NO_STORE)(scope, receive, send)
            return
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 1048576:
                await JSONResponse({"detail": {
                    "code": "request_size_exceeded", "message": "请求体大小超过限制。",
                }}, 413, headers=NO_STORE)(scope, receive, send)
                return
            body.extend(chunk)
        delivered = False

        async def buffered_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, buffered_receive, send)


app.add_middleware(AttachmentBodyLimit)


@app.get("/api/provider", dependencies=[Depends(check_boundary)])
async def get_provider():
    return JSONResponse(load_provider_status().model_dump(), headers=NO_STORE)


@app.put("/api/provider", dependencies=[Depends(check_boundary)])
async def put_provider(payload: ProviderWriteBody):
    status = save_provider(
        api=payload.api,
        base_url=payload.base_url,
        model=payload.model,
        provider=payload.provider,
        api_key=payload.api_key,
    )
    return JSONResponse(status.model_dump(), headers=NO_STORE)


@app.delete("/api/provider", dependencies=[Depends(check_boundary)])
async def delete_provider():
    return JSONResponse(clear_provider().model_dump(), headers=NO_STORE)


@app.post("/api/provider/test", dependencies=[Depends(check_boundary)])
async def test_provider(payload: ProviderWriteBody):
    result = await run_diagnostic(
        api=payload.api,
        base_url=payload.base_url,
        model=payload.model,
        provider=effective_provider(payload.api, payload.provider),
        api_key=payload.api_key,
    )
    return JSONResponse(result, headers=NO_STORE)


async def require_run(service: SessionService, run_id: str, session_id: str) -> None:
    # 数据库为运行存在与会话归属的权威，重启后原键重试仍可定位。
    run = await service.find_run_any(str(run_id))
    if run is None:
        reject(404, "run_not_found", "运行不存在。")
    if run.session_id != str(session_id):
        reject(409, "session_mismatch", "运行与会话不匹配。")


def retire(state: RunState) -> None:
    # 唯一收尾入口只释放一次：重复回调不得再次归还全局名额。
    with runs_lock:
        removed = runs.pop(state.run_id, None)
    if removed is not None:
        active.release()


# 计划域准备工具：成功结果持久化后均按 plan_prepared 登记绑定展示节点。
PLAN_PREPARE_TOOLS = {"prepare_plan", "prepare_plan_import", "prepare_plan_adjustment"}


def model_visible_entries(
    path: list[SessionEntry], visible: set[str]
) -> list[SessionMessageEntry]:
    # message_nodes 白名单：只接纳模型可见集合内的真实消息节点。内部资料节点与压缩
    # 结构节点即使参与祖先链读取，其身份也不会成为资料来源。
    return [
        entry
        for entry in path
        if isinstance(entry, SessionMessageEntry) and entry.id in visible
    ]


def execute(
    run,
    session_id: str,
    branch: list[Message],
    tools: dict,
    state: RunState,
    service: SessionService,
    business: BusinessService,
    loop,
    model,
    secrets: tuple[str, ...],
    root: TraceSpan,
) -> str:
    coordinator = state.coordinator
    guard = CredentialFilter(secrets)
    # 逐模型调用缓存观测：普通调用、摘要子调用与恢复重试均按真实请求记录，身份绑定当前运行。
    observer = CacheObserver(
        session_id=session_id, run_id=run.id, contains=guard.contains
    )
    if state.origin == "edit":
        observer.note_event("edit")
    elif state.origin == "regenerate":
        observer.note_event("branch")
    position = {"id": run.request_entry_id}
    parents: dict[str, str] = {}
    # 可信业务身份与日期：request_entry_id 随 Steering 消费更新，日期取该用户节点
    # 的真实 created_at，处理期间固定；每批按 source_entry_id 绑定。
    request_entry = {"id": run.request_entry_id}
    business_day = {"value": ""}
    # 准备调用按业务独立登记；结果节点提交后才绑定展示。
    prepared: dict[str, str] = {}
    workout_prepared: dict[str, str] = {}
    plan_prepared: dict[str, str] = {}

    def call(coro):
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    def read_business_date(entry_id: str) -> str:
        entry = call(service.get_entry(session_id, entry_id))
        return business_date(entry.created_at)

    business_day["value"] = read_business_date(request_entry["id"])
    # 运行开始时固定一次模型能力快照与压缩预算，供当前运行与摘要调用共用；请求预算每次运行独立创建。
    spec = resolve_model_spec(model)
    settings = compaction_settings()
    budget = RequestBudget()

    def business_context(source_entry_id: str) -> BusinessContext:
        return BusinessContext(
            timezone=BUSINESS_TIMEZONE,
            business_date=business_day["value"],
            session_id=session_id,
            run_id=run.id,
            request_entry_id=request_entry["id"],
            source_entry_id=source_entry_id,
        )

    def execution_registry(source_entry_id: str) -> dict:
        return {
            **tools,
            **bind_business_tools(
                business, business_context(source_entry_id), call, prepared,
                workout_prepared, plan_prepared
            ),
        }

    def announce_business_context(entry: BusinessContextEntry) -> None:
        # 身份通知复用公开白名单：只通知已成功提交的真实节点，业务资料与内部字段保持私有。
        data = {
            "type": "custom_message",
            "entry_id": entry.id,
            "parent_id": entry.parent_id,
            "run_id": entry.run_id,
            "created_at": entry.created_at,
        }
        if guard.contains(data):
            raise CredentialDetectedError()
        state.publish("custom_message", data)

    async def read_request_state() -> tuple[list[Message], BusinessContextData | None]:
        # 主循环读取：当前有效模型投影与本次模型调用前将追加的资料；只读，不提交、不通知。
        projection = await service.get_projection(session_id, position["id"])
        visible = set(projection.context.source_entry_ids)
        nodes = collect_message_nodes(
            model_visible_entries(projection.path, visible),
            BusinessBindings(
                profile_confirmations=await business.list_confirmation_bindings(session_id),
                workout_confirmations=await business.list_workout_confirmation_bindings(session_id),
                plan_confirmations=await business.list_plan_confirmation_bindings(session_id),
                profile_displays=await business.list_display_bindings(session_id),
                workout_displays=await business.list_workout_display_bindings(session_id),
                plan_displays=await business.list_plan_display_bindings(session_id),
            ),
        )
        proposals = collect_pending_proposals(
            await business.list_pending_references(session_id),
            {entry.id for entry in projection.path},
        )
        details = await next_business_context_event(
            path=projection.path,
            request=RequestContext(
                timezone=BUSINESS_TIMEZONE,
                business_date=business_day["value"],
                request_entry_id=request_entry["id"],
            ),
            nodes=nodes,
            proposals=proposals,
            removal_reason=business.removed_proposal_reason,
        )
        return projection.context.messages, details

    async def append_business_context() -> BusinessContextEntry | None:
        # 下一次模型请求前比较真实资料并提交；提交失败或凭据命中时抛错，调用方不得发送身份通知。
        _, details = await read_request_state()
        if details is None:
            return None
        node = build_business_context_entry(
            session_id=session_id,
            id=str(uuid4()),
            parent_id=position["id"],
            run_id=run.id,
            details=details,
            created_at=time_ns() // 1_000_000,
        )
        outcome = await service.append_business_context(node, credentials=secrets)
        if outcome.credential_detected:
            raise CredentialDetectedError()
        return outcome.entry

    async def transform_context(messages: list[Message], signal) -> list[Message]:
        # 每次模型调用前从真实持久化分支重放完整有效资料序列：必要资料提交成功才通知身份并同步
        # 真实执行位置，使后续助手节点的 parent_id 指向最新资料节点；同批工具结果与 Steering 消费
        # 通知均先于本批资料通知。不依赖会遗漏隐藏节点的内存消息列表，每条资料只进入本次请求一次。
        committed = call(append_business_context())
        if committed is not None:
            announce_business_context(committed)
            position["id"] = committed.id
            observer.note_event("business_context")
        projection = call(service.get_projection(session_id, position["id"]))
        return list(projection.context.messages)

    async def save_message(node_id: str, message: Message) -> None:
        parent_id = position["id"]

        async def save() -> object:
            return await service.append_entry(
                SessionMessageEntry(
                    session_id=session_id,
                    id=node_id,
                    parent_id=parent_id,
                    run_id=run.id,
                    type="message",
                    messages=[message],
                    created_at=time_ns() // 1_000_000,
                    # 明确上下文溢出的失败尝试：持久化模型投影省略标记。
                    model_omitted=(
                        isinstance(message, AssistantMessage)
                        and message.context_overflow
                    ),
                ),
                credentials=secrets,
            )

        outcome = asyncio.run_coroutine_threadsafe(save(), loop).result()
        if outcome.credential_detected:
            raise CredentialDetectedError()
        parents[node_id] = parent_id
        position["id"] = node_id
        if (
            isinstance(message, ToolResultMessage)
            and message.tool_name in {*PLAN_PREPARE_TOOLS, "prepare_profile_update", "prepare_workout"}
            and not message.is_error
        ):
            # 成功结果必须对应本批登记；关联缺失直接终止运行。
            if message.tool_name == "prepare_profile_update":
                call(business.bind_display_entry(prepared.pop(message.tool_call_id), node_id))
            elif message.tool_name == "prepare_workout":
                call(business.bind_workout_display_entry(
                    workout_prepared.pop(message.tool_call_id), node_id
                ))
            else:
                call(business.bind_plan_display_entry(
                    plan_prepared.pop(message.tool_call_id), node_id
                ))

    async def get_steering_messages() -> list[Message]:
        return call(coordinator.take(run.id))

    async def get_steering_messages_or_close() -> list[Message]:
        return call(coordinator.take_or_close(run.id))

    def project_steering(messages: list[Message]) -> list[Message]:
        # 统一附件投影：Steering 输入在进入模型请求前表达附件读取引用。
        groups = call(coordinator.attachments_for(run.id, messages))
        return [
            project_user_message(message, group)
            for message, group in zip(messages, groups)
        ]

    async def on_steering_consumed(messages: list[Message]) -> None:
        last_id = call(coordinator.consume(run.id, messages))
        if last_id is not None:
            position["id"] = last_id
            # Steering 消费后：后续批次的 request_entry_id 与业务日期使用新用户节点。
            request_entry["id"] = last_id
            business_day["value"] = read_business_date(last_id)

    async def emit(event) -> None:
        kind = event["type"]
        if kind in {"message_start", "message_update", "message_end"}:
            message = event["message"]
            if guard.contains(message.model_dump()):
                raise CredentialDetectedError()
            data = {
                "message_id": event["message_id"],
                "content": public_content(message, guard, kind != "message_end"),
            }
            if kind == "message_update":
                update = event["assistant_event"]
                index = update["content_index"]
                block = message.content[index]
                if isinstance(block, ThinkingContent) and block.redacted:
                    return
                data.update(content_index=index, update_type=update["type"])
            elif kind == "message_end":
                data["entry_id"] = event["message_id"]
                data["parent_id"] = parents.get(event["message_id"])
                data["stop_reason"] = message.stop_reason
            if public_text_visible(data["content"]):
                # 首个公开非空正文进入队列边界：按受理起点记录一次，事件内容与顺序不变。
                root.record_first_text()
            state.publish(kind, data)
        elif kind == "tool_start":
            data = {
                "tool_call_id": event["tool_call_id"],
                "name": event["name"],
                "arguments": event["arguments"],
            }
            if guard.contains(data):
                raise CredentialDetectedError()
            state.publish(kind, data)
        elif kind == "tool_result":
            data = {
                "tool_call_id": event["tool_call_id"],
                "content": event["content"],
                "is_error": event["is_error"],
                "entry_id": event["message_id"],
                "parent_id": parents.get(event["message_id"]),
            }
            if guard.contains(data):
                raise CredentialDetectedError()
            state.publish(kind, data)
        elif kind == "tool_execution_end":
            # 单工具结果定稿：经现有凭据检查后按实际完成顺序公开；不携带 entry_id、parent_id。
            data = public_tool_execution_end(event)
            if guard.contains(data):
                raise CredentialDetectedError()
            state.publish(kind, data)
        elif kind == "compaction_start":
            # 阶段开始：只公开压缩身份与原因，不含摘要、系统状态与用量。
            state.publish(kind, {
                "compaction_id": event["compaction_id"],
                "reason": event["reason"],
            })
        elif kind == "compaction_end":
            # 检查点提交成功后公开真实节点身份，与开始事件的压缩身份一致。
            state.publish(kind, {
                "compaction_id": event["compaction_id"],
                "entry_id": event["entry_id"],
                "parent_id": event["parent_id"],
            })
        elif kind == "custom_message":
            # 压缩新基线身份通知：复用资料节点五字段白名单，凭据命中不得公开。
            data = {
                "type": "custom_message",
                "entry_id": event["entry_id"],
                "parent_id": event["parent_id"],
                "run_id": event["run_id"],
                "created_at": event["created_at"],
            }
            if guard.contains(data):
                raise CredentialDetectedError()
            state.publish(kind, data)

    def on_tool_update(tool_call_id: str, tool_name: str, result) -> None:
        # 单工具进度快照：中间态，可被完成事件与保存确认覆盖；凭据在 harness 通知前拦截。
        state.publish(
            "tool_execution_update",
            public_tool_execution_update(
                tool_call_id, tool_name, result.content, result.is_error
            ),
        )

    def request_messages() -> list[Message]:
        # 即将发送的完整请求：当前有效模型投影加本次模型调用前将追加的资料，与实际发送同口径。
        messages, details = call(read_request_state())
        if details is None:
            return messages
        return [
            *messages,
            business_context_to_llm(
                build_business_context_message(
                    details=details, created_at=time_ns() // 1_000_000
                )
            ),
        ]

    async def estimate_request() -> int:
        # 完整计数入口：来源与投影一致性由投影读取保证，本次将追加的资料与实际发送同口径计入。
        return estimate_context_tokens(request_messages()).tokens

    async def build_compaction_baseline(
        path: list[SessionEntry], entry: CompactionEntry
    ) -> BusinessContextEntry:
        # 压缩新基线：按拟提交检查点形成的压缩后有效投影重新采集可见真实消息来源，
        # 待确认提案按完整当前分支核验（被摘要的真实来源仍受控保留）；强制完整 Snapshot。
        prospective = build_effective_context([*path, entry])
        visible = set(prospective.source_entry_ids)
        nodes = collect_message_nodes(
            model_visible_entries([*path, entry], visible),
            BusinessBindings(
                profile_confirmations=await business.list_confirmation_bindings(session_id),
                workout_confirmations=await business.list_workout_confirmation_bindings(session_id),
                plan_confirmations=await business.list_plan_confirmation_bindings(session_id),
                profile_displays=await business.list_display_bindings(session_id),
                workout_displays=await business.list_workout_display_bindings(session_id),
                plan_displays=await business.list_plan_display_bindings(session_id),
            ),
        )
        proposals = collect_pending_proposals(
            await business.list_pending_references(session_id),
            {item.id for item in path},
        )
        snapshot = BusinessContextSnapshot(
            mode="snapshot",
            request=RequestContext(
                timezone=BUSINESS_TIMEZONE,
                business_date=business_day["value"],
                request_entry_id=request_entry["id"],
            ),
            message_nodes=list(nodes.values()),
            pending_proposals=list(proposals.values()),
        )
        return build_business_context_entry(
            session_id=session_id,
            id=business_baseline_entry_id(entry.id),
            parent_id=entry.id,
            run_id=run.id,
            details=snapshot,
            created_at=time_ns() // 1_000_000,
        )

    async def compact(compaction_id: str, signal, span) -> LoopCompactionResult:
        # 事务外生成真实摘要与用量并重建完整新基线，再经同一事务入口原子提交；cancelled 在提交前生效。
        check_cancelled(signal)
        projection = call(service.get_projection(session_id, position["id"]))
        preparation = prepare_compaction(
            projection.path, spec.context_window, spec.max_tokens, settings
        )
        tokens_before = estimate_context_tokens(request_messages()).tokens
        # 摘要 model 子 Span 挂在当前 Compaction 下，与检查点提交同属一次压缩操作。
        result = await generate_compaction_summary(
            preparation, spec, api_key=secrets[0], settings=settings, signal=signal,
            deadline=state.control.deadline,
            run_control=state.control,
            budget=budget, observation=observer, compaction_id=compaction_id, parent=span,
        )
        check_cancelled(signal)
        system_state = get_current_system_message(projection.context.messages)
        if system_state is None:
            raise RuntimeError("压缩检查点缺少稳定系统状态")
        entry = CompactionEntry(
            session_id=session_id,
            id=compaction_id,
            parent_id=position["id"],
            run_id=run.id,
            type="compaction",
            summary=result.summary,
            first_kept_entry_id=preparation.first_kept_entry_id,
            tokens_before=tokens_before,
            usage=result.usage,
            system_message=system_state,
            details=(
                CompactionDetails(attachments=preparation.attachments)
                if preparation.attachments
                else None
            ),
            created_at=time_ns() // 1_000_000,
        )
        baseline = call(build_compaction_baseline(projection.path, entry))
        outcome = call(service.commit_compaction(
            entry, baseline, prepared_position=entry.parent_id, credentials=secrets
        ))
        if outcome.credential_detected:
            raise CredentialDetectedError()
        parents[outcome.entry.id] = outcome.entry.parent_id
        parents[outcome.baseline.id] = outcome.entry.id
        # 压缩提交后标注已知事件：供检查点之后的首次真实调用记录重置原因。
        observer.note_event("compaction")
        # 执行位置与刷新投影使用新基线节点；后续助手节点 parent_id、工具与 Steering 追加位置随之一致。
        position["id"] = outcome.baseline.id
        refreshed = call(service.get_projection(session_id, outcome.baseline.id))
        return LoopCompactionResult(
            entry_id=outcome.entry.id,
            parent_id=outcome.entry.parent_id,
            messages=refreshed.context.messages,
            baseline=outcome.baseline,
        )

    loop_config = AgentLoopConfig(
        model=model,
        max_turns=64,
        model_spec=spec,
        request_budget=budget,
        deadline=state.control.deadline,
        run_control=state.control,
        tool_tracker=state.tool_tracker,
        tool_timeout_seconds=TOOL_EXECUTION_SECONDS,
        compaction=CompactionHooks(
            estimate_request=estimate_request,
            compact=compact,
            context_window=spec.context_window,
            reserve_tokens=settings.reserve_tokens,
        ),
        transform_context=transform_context,
        get_steering_messages=get_steering_messages,
        get_steering_messages_or_close=get_steering_messages_or_close,
        project_steering=project_steering,
        on_steering_consumed=on_steering_consumed,
        save_message=save_message,
        bind_tools=execution_registry,
        before_tool_call=create_before_tool_call(),
        on_tool_update=on_tool_update,
        contains_credentials=guard.contains,
        observation=observer,
        trace_span=root,
    )
    # 稳定声明注册表：业务工具实例仅用于声明校验，实际执行按批重新绑定。
    context = {
        "messages": branch[:-1],
        "tools": execution_registry(run.request_entry_id),
    }
    messages = asyncio.run(
        run_agent_loop([branch[-1]], context, loop_config, emit, state.cancel)
    )
    check_cancelled(state.cancel)
    final_message = next(
        message for message in reversed(messages)
        if isinstance(message, AssistantMessage) and message.stop_reason == "stop"
    )
    return final_message.stop_reason


def publish_terminal(state: RunState, run, stop_reason: str | None) -> None:
    # 公开终态由已提交运行记录决定，与数据库终态保持一致；仅终态持久化成功后调用。
    if run.status == "completed":
        state.publish_terminal("done", {"status": "completed", "stop_reason": stop_reason})
        return
    cancelled = run.status == "cancelled"
    state.publish_terminal("error", {
        "status": run.status,
        "code": run.error_code or ("cancelled" if cancelled else "execution_failed"),
        "message": run.error_message
        or ("执行已取消。" if cancelled else "执行失败，请重新发起请求。"),
    })


def terminal_decision(
    failure: BaseException | None,
    cancelled: bool,
    stop_reason: RunStopReason | None = None,
) -> tuple[str, str | None, str | None]:
    # 凭据命中优先于取消与停止原因；否则停止原因裁决终态，再按取消、失败、完成的顺序兜底。
    if isinstance(failure, CredentialDetectedError):
        return "failed", "credential_detected", CREDENTIAL_SAFE_MESSAGE
    if stop_reason is not None:
        status, code = stop_terminal(stop_reason)
        message = STOP_REASON_MESSAGES.get(code)
        if message is None:
            message = "执行已取消。" if status == "cancelled" else "执行失败，请重新发起请求。"
        return status, code, message
    if isinstance(failure, CompactionSummaryError):
        return "failed", "compaction_failed", COMPACTION_FAILED_MESSAGE
    if isinstance(failure, (ContextBudgetExceeded, NoSummarizableHistory)):
        return "failed", "context_budget_exceeded", CONTEXT_BUDGET_EXCEEDED_MESSAGE
    if isinstance(failure, ModelRequestLimitExceeded):
        return "failed", "model_request_limit_exceeded", str(failure)
    if isinstance(failure, ProviderQuotaExceeded):
        return "failed", "provider_quota_exceeded", PROVIDER_QUOTA_EXCEEDED_MESSAGE
    if isinstance(failure, ModelOutputLimitExceeded):
        return "failed", "model_output_limit_exceeded", MODEL_OUTPUT_LIMIT_MESSAGE
    if cancelled:
        return "cancelled", "cancelled", "执行已取消。"
    if isinstance(failure, ContextOverflowError):
        return "failed", "context_overflow", CONTEXT_OVERFLOW_MESSAGE
    if failure is not None:
        return "failed", "execution_failed", "执行失败，请重新发起请求。"
    return "completed", None, None


# 故障封闭哨兵：收尾超出共享预算或终态持久化失败，不发布终态、不释放名额。
FAULT_CLOSED = object()


def _consume_future(future) -> None:
    # 迟到的底层结果只取回异常，禁止再次发布终态或释放名额。
    with suppress(Exception):
        if not future.cancelled():
            future.exception()


async def _record_diagnostic(
    service: SessionService, session_id: str, run_id: str, reason: str
) -> None:
    # 诊断写入受短上限约束；数据库阻塞时后台重试，仍不阻塞主事件循环或收尾。
    for attempt in range(3):
        try:
            await asyncio.wait_for(
                service.record_run_diagnostic(session_id, run_id, reason),
                timeout=DIAGNOSTIC_WRITE_WAIT_SECONDS,
            )
            return
        except TimeoutError:
            if attempt == 2:
                logger.error("运行诊断写入超时（%s）", reason)
                return
            await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error("运行诊断写入失败（%s/%s）", reason, type(error).__name__)
            return


# 有界协调读取超时哨兵：底层任务继续保留，实际结果仍可在之后核对。
READ_TIMEOUT = object()


def _schedule_diagnostic(
    state: RunState,
    service: SessionService,
    session_id: str,
    run_id: str,
    reason: str,
) -> None:
    # 故障状态先落地；诊断写入异步进行，数据库等待不得阻塞收尾或主事件循环。
    task = asyncio.create_task(
        _record_diagnostic(service, session_id, run_id, reason)
    )
    state.diagnostic_tasks.append(task)
    task.add_done_callback(_consume_future)


async def _bounded_read(
    state: RunState, awaitable: Awaitable, timeout: float
) -> object:
    # 投影与收尾状态读取共用有界协调：超时返回哨兵，不取消底层任务、不回滚。
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=max(0.0, timeout))
    except BaseException:
        state.inflight_reads.append(task)
        task.add_done_callback(_consume_future)
        raise
    if task in done:
        return task.result()
    state.inflight_reads.append(task)
    task.add_done_callback(_consume_future)
    return READ_TIMEOUT


def _finalization_read_budget(_state: RunState) -> float:
    # 终态提交前的收尾读取使用独立短上限；不与执行收尾预算重复起算，也不无限等待。
    return TERMINAL_PERSIST_WAIT_SECONDS


async def _watch_control(
    state: RunState, coordinator: SteeringCoordinator, run_id: str
) -> None:
    # 主事件循环监控同一运行截止时间；停止信号一到即关闭 Steering 新输入。
    deadline = state.control.deadline
    while not state.control.cancelled:
        if deadline is not None and deadline.expired():
            state.control.request_cancel("run_timeout")
            break
        wait = 0.05 if deadline is None else min(0.05, max(0.001, deadline.remaining()))
        await asyncio.sleep(wait)
    state.control.note_stop()
    coordinator.close(run_id)


async def _fault_close(
    state: RunState,
    root: TraceSpan,
    coordinator: SteeringCoordinator,
    service: SessionService,
    session_id: str,
    run_id: str,
    reason: str,
    work,
    concurrent_future,
) -> object:
    # 保留运行占用并进入故障封闭：不发布成功确认、不释放名额。
    state.mark_fault(reason, concurrent_future, state.tool_tracker.pending())
    # 故障状态先落地；诊断写入异步进行，避免数据库等待再次阻塞收尾。
    _schedule_diagnostic(state, service, session_id, run_id, reason)
    root.set_status("failed", reason)
    coordinator.close(run_id)
    enter_fault_closure()
    if work is not None and not work.done():
        work.add_done_callback(_consume_future)
    logger.error("运行进入故障封闭（%s/%s）", run_id, reason)
    return FAULT_CLOSED


async def _wait_tools(state: RunState) -> bool:
    # 共享收尾预算：从首次停止时刻起算，任务二工具层已消耗的等待不重复计时。
    cooperative_deadline = state.control.cooperative_deadline()
    if cooperative_deadline is None:
        cooperative_deadline = monotonic() + CANCEL_COOPERATIVE_WAIT_SECONDS
    remaining = max(0.0, cooperative_deadline - monotonic())
    # wait_all 是阻塞等待：放入线程执行，保持主事件循环可调度。
    return await asyncio.to_thread(state.tool_tracker.wait_all, remaining)


async def coordinate(
    run,
    state: RunState,
    session_id: str,
    tools: dict,
    model,
    secrets: tuple[str, ...],
    service: SessionService,
    business: BusinessService,
    coordinator: SteeringCoordinator,
    executor: ThreadPoolExecutor,
    loop,
    trace: RunTrace,
    store,
) -> None:
    # 主事件循环持有的独立收尾任务：始终拥有已受理运行的终态所有权。
    # 根 callback 覆盖业务执行与终态提交；提交完成后依次结算根、写入 Trace、发布终态。
    failure: BaseException | None = None
    stop_reason: str | None = None

    async def settle(root: TraceSpan):
        nonlocal failure, stop_reason
        watcher = asyncio.create_task(
            _watch_control(state, coordinator, run.id)
        )
        concurrent_future = None
        future = None
        try:
            try:
                # 投影读取纳入运行截止时间与共享协作预算；数据库持锁不得越过两项时限。
                projection = await _bounded_read(
                    state,
                    service.get_projection(session_id, run.request_entry_id),
                    state.control.deadline.remaining()
                    + CANCEL_COOPERATIVE_WAIT_SECONDS,
                )
                if projection is READ_TIMEOUT:
                    # 底层投影任务保留在 state.inflight_reads，等待超时不作为回滚证据。
                    state.control.request_cancel("run_timeout")
                    return await _fault_close(
                        state, root, coordinator, service, session_id, run.id,
                        "terminal_persist_failed", None, None,
                    )
                branch = projection.context.messages
                concurrent_future = executor.submit(
                    execute, run, session_id, branch, tools, state, service, business,
                    loop, model, secrets, root,
                )
                future = asyncio.wrap_future(concurrent_future)
            except BaseException as error:
                failure = error

            if future is not None:
                # 主循环持续观察工作 future 的真实退出；停止后超过共享收尾预算即故障封闭。
                while True:
                    done, _ = await asyncio.wait({future}, timeout=0.05)
                    if done:
                        break
                    if state.control.cancelled:
                        state.control.note_stop()
                        deadline = state.control.cooperative_deadline()
                        if deadline is not None and monotonic() >= deadline:
                            return await _fault_close(
                                state, root, coordinator, service, session_id, run.id,
                                "worker_stuck", future, concurrent_future,
                            )
                try:
                    stop_reason = future.result()
                except BaseException as error:
                    failure = error

            if not await _wait_tools(state):
                return await _fault_close(
                    state, root, coordinator, service, session_id, run.id,
                    "worker_stuck", future, concurrent_future,
                )

            cancelled = isinstance(failure, CancelledError) or state.cancel.is_set()
            # 首个有效停止原因（若有）裁决终态；仅取消信号而无停止原因时沿用既有取消契约。
            recorded_stop = state.control.stop.reason
            status, code, message = terminal_decision(failure, cancelled, recorded_stop)

            try:
                # 收尾读取同样纳入共享协作预算；超时保留底层读取任务并故障封闭。
                current = await _bounded_read(
                    state,
                    service.get_run(session_id, run.id),
                    _finalization_read_budget(state),
                )
            except BaseException:
                return await _fault_close(
                    state, root, coordinator, service, session_id, run.id,
                    "terminal_persist_failed", future, concurrent_future,
                )
            if current is READ_TIMEOUT:
                return await _fault_close(
                    state, root, coordinator, service, session_id, run.id,
                    "terminal_persist_failed", future, concurrent_future,
                )
            if current.status in TERMINAL_RUN_STATUSES:
                # 已有终态保持原值，仍执行收尾以关闭接收入口并丢弃剩余输入。
                status, code, message = (
                    current.status,
                    current.error_code,
                    current.error_message,
                )

            # 终态持久化等待从实际调用 finish 提交时起计；超时不回滚、不再次提交。
            finish_task = asyncio.create_task(coordinator.finish(
                session_id,
                run.id,
                status,
                error_code=code,
                error_message=message,
                stop_reason=recorded_stop,
            ))
            done, _ = await asyncio.wait(
                {finish_task}, timeout=TERMINAL_PERSIST_WAIT_SECONDS
            )
            if not done:
                finish_task.add_done_callback(_consume_future)
                return await _fault_close(
                    state, root, coordinator, service, session_id, run.id,
                    "terminal_persist_failed", future, concurrent_future,
                )
            try:
                outcome = finish_task.result()
            except BaseException:
                return await _fault_close(
                    state, root, coordinator, service, session_id, run.id,
                    "terminal_persist_failed", future, concurrent_future,
                )
            # 业务终态提交成功后才显式设置根状态；读取或提交失败时根按自动失败结算。
            root_status = {"completed": "completed", "cancelled": "cancelled"}.get(
                status, "failed"
            )
            root.set_status(root_status, code)
            return outcome
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher

    try:
        outcome = await trace.start_span("agent_run", {}, settle)
    except BaseException:
        # 收尾读取或终态提交失败：根已记为 failed，尝试保存诊断，不发布已持久化终态确认，
        # 保留数据库真实状态及尚未完成收尾的运行占用。
        state.mark_fault("terminal_persist_failed", None, state.tool_tracker.pending())
        _schedule_diagnostic(
            state, service, session_id, run.id, "terminal_persist_failed"
        )
        coordinator.close(str(run.id))
        enter_fault_closure()
        await trace.finish(store)
        raise
    await trace.finish(store)
    if outcome is FAULT_CLOSED:
        # 故障封闭路径已关闭 SSE 并保留占用，禁止发布终态或释放名额。
        return
    publish_terminal(state, outcome.run, stop_reason)
    retire(state)


def track(application: FastAPI, task: asyncio.Task) -> None:
    coordination = application.state.coordination
    coordination.add(task)

    def finalized(completed: asyncio.Task) -> None:
        coordination.discard(completed)
        if completed.cancelled():
            return
        error = completed.exception()
        if error is not None:
            # 仅记录受控诊断字段；禁止异常正文与 traceback 进入日志。
            logger.error(
                "运行收尾任务异常（%s），保留数据库真实状态。",
                type(error).__name__,
            )

    task.add_done_callback(finalized)


SSE_HEARTBEAT = b": keep-alive\n\n"


def stream(request: Request, state: RunState) -> AsyncIterator[bytes]:
    async def generate() -> AsyncIterator[bytes]:
        last_emit = monotonic()
        while True:
            # 故障封闭：保留占用但立即关闭 SSE，不发布任何终态确认。
            if state.closed_without_terminal:
                return
            if await request.is_disconnected():
                state.disconnect()
                return
            if state.events.terminal_filled:
                remaining = state.events.terminal_remaining()
                if remaining is not None and remaining <= 0:
                    return
            event = state.events.take()
            if event is None and state.events.terminal_filled:
                event = state.events.take_terminal()
            if event is not None:
                yield event.payload
                last_emit = monotonic()
                if event.name in {"done", "error"}:
                    return
                continue
            now = monotonic()
            idle = now - last_emit
            if idle >= SSE_HEARTBEAT_SECONDS:
                # 心跳不刷新运行预算，只遵守同一发送时限。
                yield SSE_HEARTBEAT
                last_emit = monotonic()
            else:
                await asyncio.sleep(
                    min(0.02, max(0.001, SSE_HEARTBEAT_SECONDS - idle))
                )

    return generate()


class RunStream(StreamingResponse):
    def __init__(self, state: RunState, body: AsyncIterator[bytes]):
        super().__init__(body, media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Session-ID": str(state.session_id),
            "X-Operation-ID": state.operation_id,
            "X-Run-ID": str(state.run_id),
            "X-Request-Entry-ID": state.request_entry_id,
        })
        self.state = state

    async def __call__(self, scope, receive, send):
        # 在真实 ASGI send 边界施加单次发送时限；终态排空期间动态取剩余总时限的较小值。
        async def bounded_send(message):
            pending = asyncio.ensure_future(send(message))
            started = monotonic()
            try:
                while True:
                    timeout = SSE_SEND_WAIT_SECONDS - (monotonic() - started)
                    remaining = self.state.events.terminal_remaining()
                    if remaining is not None:
                        timeout = min(timeout, remaining)
                    if timeout <= 0:
                        raise TimeoutError("SSE 发送超过时限")
                    done, _ = await asyncio.wait(
                        {pending}, timeout=min(0.05, timeout)
                    )
                    if done:
                        await pending
                        return
            finally:
                if not pending.done():
                    pending.cancel()

        try:
            await super().__call__(scope, receive, bounded_send)
        finally:
            # 断连回调只发出取消信号并关闭 Steering；已提交终态与名额由 coordinate 独占管理。
            self.state.disconnect()


@app.post("/api/sessions", dependencies=[Depends(check_boundary)])
async def create_session(payload: CreateSessionRequest):
    service: SessionService = app.state.session_service
    credentials = active_secrets()
    try:
        created, session = await service.create_session_result(
            str(payload.session_id), payload.title, credentials=credentials
        )
    except CredentialDetected:
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    except SessionConflict:
        reject(409, "session_conflict", "会话标题冲突。")
    return JSONResponse(session_object(session), status_code=201 if created else 200)


@app.get("/api/sessions", dependencies=[Depends(check_boundary)])
async def list_sessions():
    service: SessionService = app.state.session_service
    guard = CredentialFilter(active_secrets())
    sessions = await service.list_sessions()
    ordered = sorted(
        sessions, key=lambda session: (session.updated_at, session.id), reverse=True
    )
    payload = {"sessions": [session_object(session) for session in ordered]}
    if guard.contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


async def drain_session_runs(session_id: str) -> None:
    # 对目标会话的运行发出协作取消，等待执行任务与持久化收尾真正退出后返回。
    with runs_lock:
        states = [
            state for state in runs.values() if state.session_id == UUID(session_id)
        ]
    for state in states:
        state.disconnect()
    await asyncio.gather(*(state.task for state in states))


@app.delete("/api/sessions/{session_id}", dependencies=[Depends(check_boundary)])
async def delete_session(session_id: PathUUID, request: Request):
    service: SessionService = request.app.state.session_service
    replacements: ReplacementCoordinator = request.app.state.replacements
    if CredentialFilter(active_secrets(session_id)).contains(session_id):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)

    async def remove() -> None:
        # 串行新运行受理并登记删除意图，等待目标执行与收尾退出后执行删除事务。
        async with session_gate(session_id), replacements.register(session_id):
            await drain_session_runs(session_id)
            await service.delete_session(session_id)

    task = asyncio.get_running_loop().create_task(remove())
    track(request.app, task)
    # 删除任务的生命周期独立于 HTTP 连接：断连取消不影响服务端继续完成删除。
    await asyncio.shield(task)
    return JSONResponse({"session_id": session_id, "deleted": True}, headers=NO_STORE)


@app.get("/api/profile", dependencies=[Depends(check_boundary)])
async def get_profile():
    business: BusinessService = app.state.business
    guard = CredentialFilter(active_secrets())
    response = await business.get_profile()
    payload = response.model_dump()
    if guard.contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


class WorkoutQuery(WorkoutListArguments):
    model_config = ConfigDict(extra="forbid", strict=False, allow_inf_nan=False)


def check_workout_query(request: Request, allowed: set[str]) -> None:
    keys = list(request.query_params.keys())
    if any(key not in allowed or len(request.query_params.getlist(key)) != 1 for key in keys):
        reject(422, "invalid_request", "请求字段不合法。")


def workout_response(response) -> JSONResponse:
    payload = response.model_dump()
    if CredentialFilter(active_secrets()).contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers=NO_STORE)


@app.get("/api/exercises/equipment", dependencies=[Depends(check_boundary)])
async def get_exercise_equipment(request: Request):
    check_workout_query(request, set())
    payload = request.app.state.business.exercise_equipment()
    if CredentialFilter(active_secrets()).contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers=NO_STORE)


@app.get("/api/plans/current", dependencies=[Depends(check_boundary)])
async def get_current_plan(request: Request):
    check_workout_query(request, set())
    return workout_response(await request.app.state.business.get_current_plan())


@app.get("/api/plans", dependencies=[Depends(check_boundary)])
async def list_plans(request: Request):
    check_workout_query(request, set())
    payload = [record.model_dump() for record in await request.app.state.business.list_plans()]
    if CredentialFilter(active_secrets()).contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers=NO_STORE)


@app.get("/api/plans/{plan_id}", dependencies=[Depends(check_boundary)])
async def get_plan(plan_id: PathUUID, request: Request):
    check_workout_query(request, set())
    return workout_response(await request.app.state.business.get_plan(plan_id))


@app.get("/api/workouts", dependencies=[Depends(check_boundary)])
async def list_workouts(request: Request, query: Annotated[WorkoutQuery, Query()]):
    check_workout_query(request, {"date_from", "date_to", "page", "page_size"})
    arguments = WorkoutListArguments.model_validate(query.model_dump())
    return workout_response(await request.app.state.business.list_workouts(arguments))


@app.get("/api/workouts/{workout_id}", dependencies=[Depends(check_boundary)])
async def get_workout(workout_id: PathUUID, request: Request):
    check_workout_query(request, set())
    return workout_response(await request.app.state.business.get_workout(workout_id))


@app.get("/api/sessions/{session_id}/attachments/{attachment_id}", dependencies=[Depends(check_boundary)])
async def get_attachment(session_id: PathUUID, attachment_id: PathUUID, request: Request):
    check_workout_query(request, set())
    credentials = active_secrets(session_id)
    if CredentialFilter(credentials).contains({"session_id": session_id, "attachment_id": attachment_id}):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    try:
        metadata, text = await request.app.state.session_service.get_attachment_content(
            session_id, attachment_id, credentials=credentials,
        )
    except SessionNotFound:
        reject(404, "session_not_found", "会话不存在。")
    except CredentialDetected:
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    payload = {**attachment_object(metadata), "text": text}
    if CredentialFilter(credentials).contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers=NO_STORE)


@app.get(
    "/api/sessions/{session_id}/history",
    dependencies=[Depends(check_boundary)],
)
async def get_history(session_id: PathUUID, request: Request):
    check_workout_query(request, set())
    service: SessionService = app.state.session_service
    confirmations: ConfirmationService = app.state.confirmations
    database = app.state.database
    guard = CredentialFilter(active_secrets(session_id))
    # 消息、会话、运行、Steering 与确认扩展在同一数据库读取快照内取得：
    # 会话与业务仓库共用同一 Database，同事务对当前 Task 可重入。
    try:
        async with database.transaction_scope():
            try:
                history = await service.get_session_history(str(session_id))
            except SessionNotFound:
                reject(404, "session_not_found", "会话不存在。")
            extension = await confirmations.history(str(session_id))
    except SessionNotFound:
        reject(404, "session_not_found", "会话不存在。")
    # 投影前先检查完整原始记录：系统内容、隐藏块、签名、嵌套元数据、身份字段及确认结果与错误。
    if guard.contains(
        {
            "session": history.session.model_dump(),
            "entries": [entry.model_dump() for entry in history.entries],
            "runs": [run.model_dump() for run in history.runs],
            "steering": [item.model_dump() for item in history.steering],
            "confirmations": [item.model_dump() for item in extension.confirmations],
            "pending_proposals": [_pending_reference(item) for item in extension.pending_proposals],
        }
    ):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    payload = {
        "session": session_object(history.session),
        "entries": [history_entry(entry) for entry in history.entries],
        "runs": [run_object(run) for run in history.runs],
        "steering": [history_steering(item) for item in history.steering],
        "confirmations": [confirmation_operation_object(item) for item in extension.confirmations],
        "pending_proposals": [_pending_reference(item) for item in extension.pending_proposals],
    }
    if guard.contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


def _pending_reference(reference) -> dict:
    # PendingProposalReference 为 dataclass；按 §7.6 字段白名单投影。
    return {
        "business_kind": reference.business_kind,
        "proposal_id": reference.proposal_id,
        "status": reference.status,
        "request_entry_id": reference.request_entry_id,
        "source_entry_id": reference.source_entry_id,
        "display_entry_id": reference.display_entry_id,
        "confirmation_entry_id": reference.confirmation_entry_id,
    }


def confirmation_operation_object(operation: SaveConfirmationOperation) -> dict:
    # §7.3 公开投影：保留 result 与 error 的必填 null 及业务结果内的合法 null；
    # 无字段错误明细时省略 error.errors，避免前端把 null 当作明细数组拒绝解析。
    payload = operation.model_dump()
    error = payload["error"]
    if error is not None and error["errors"] is None:
        del error["errors"]
    return payload


def _confirmation_object(operation: SaveConfirmationOperation, status_code: int) -> JSONResponse:
    payload = confirmation_operation_object(operation)
    if CredentialFilter(active_secrets(operation.session_id)).contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, status_code=status_code, headers=NO_STORE)


def _confirmation_error_response(operation: SaveConfirmationOperation) -> JSONResponse:
    # failed 操作按真实 error 返回既有 detail 结构；仅内容校验错误携带 errors，状态沿用业务定义。
    error = operation.error
    detail: dict = {"code": error.code, "message": error.message}
    if error.code == "invalid_business_payload":
        detail["errors"] = [item.model_dump() for item in (error.errors or [])]
    if CredentialFilter(active_secrets(operation.session_id)).contains(detail):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse({"detail": detail}, status_code=business_http_status(error.code), headers=NO_STORE)


def _confirmation_post_response(operation: SaveConfirmationOperation) -> JSONResponse:
    if operation.status == "failed":
        return _confirmation_error_response(operation)
    return _confirmation_object(operation, 200 if operation.status == "saved" else 202)


@app.post(
    "/api/sessions/{session_id}/confirmations",
    dependencies=[Depends(check_boundary)],
)
async def create_confirmation(session_id: PathUUID, payload: SaveConfirmationWire, request: Request):
    check_workout_query(request, set())
    confirmations: ConfirmationService = request.app.state.confirmations
    session = str(session_id)
    operation_id = str(payload.operation_id)
    req = SaveConfirmationRequest(
        operation_id=operation_id,
        business_kind=payload.business_kind,
        proposal_id=str(payload.proposal_id),
        display_entry_id=str(payload.display_entry_id),
    )
    # 观测依赖显式传入：使用主事件循环已有 TraceStore 与现有凭据检查，不新增逐请求观测连接。
    trace_store = request.app.state.trace_store
    contains = CredentialFilter(active_secrets(session)).contains

    async def execute_new():
        # 后台执行：受理即登记处理中操作并直接调用业务保存；客户端断连不打断收尾，active 恰好释放一次。
        try:
            return await confirmations.confirm(
                session, req, trace_store=trace_store, contains=contains
            )
        finally:
            active.release()

    async with operation_lock(operation_id), session_gate(session):
        # 同键幂等查询先于新操作占用检查：已登记操作复用原对象，处理中同键不误判为新操作。
        try:
            registered = await confirmations.get(session, operation_id)
        except ConfirmationNotFound:
            registered = None
        if registered is not None:
            # 复用路径不新增记录器或根 Span：同键操作沿用原结果，观测边界保持不变。
            return _confirmation_post_response(
                await confirmations.confirm(
                    session, req, trace_store=trace_store, contains=contains
                )
            )
        if not request.app.state.accepting_runs:
            reject(409, "run_busy", "服务正在关闭，暂不接受新确认。")
        if not active.acquire(blocking=False):
            reject(409, "run_busy", "已有任务正在执行。")
        task = asyncio.get_running_loop().create_task(execute_new())
        track(request.app, task)
        # shield 令断连取消只关闭响应，后台任务继续完成原操作并持久化终态。
        return _confirmation_post_response(await asyncio.shield(task))


@app.get(
    "/api/sessions/{session_id}/confirmations/{operation_id}",
    dependencies=[Depends(check_boundary)],
)
async def get_confirmation(session_id: PathUUID, operation_id: PathUUID, request: Request):
    check_workout_query(request, set())
    confirmations: ConfirmationService = request.app.state.confirmations
    # 查询只读：不触发保存、不创建确认节点；未登记或会话不存在沿用服务的 404 错误。
    operation = await confirmations.get(str(session_id), str(operation_id))
    return _confirmation_object(operation, 200)


async def resolve_existing(
    service: SessionService,
    operation_id: str,
    session_id: str,
    kind: str,
    request: object,
) -> OperationOutcome | None:
    # 先按 operation_id 查重：已失效编号返回冲突，已受理操作返回持久化原结果。
    try:
        outcome = await service.resolve_operation(
            operation_id, session_id, kind, request,
            credentials=active_secrets(session_id),
        )
        if outcome is not None and CredentialFilter(active_secrets(session_id)).contains(outcome.model_dump()):
            reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
        return outcome
    except CredentialDetected:
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    except OperationExpired:
        reject_operation_expired()
    except OperationConflict:
        reject(409, "operation_conflict", "操作请求冲突。")
    except SessionMismatch:
        reject(409, "session_mismatch", "运行与会话不匹配。")


async def launch_run(
    request: Request,
    *,
    session_id: str,
    operation_id: str,
    origin: str,
    accept: Callable[..., Awaitable[OperationOutcome]],
):
    service: SessionService = request.app.state.session_service
    coordinator: SteeringCoordinator = request.app.state.steering
    loop = asyncio.get_running_loop()
    # 故障封闭与关闭状态先于额度检查：恢复依赖人工受控重启。
    if is_fault_closed():
        reject(503, "service_unavailable", "服务正在恢复，暂不接受新运行。")
    if not request.app.state.accepting_runs:
        reject(409, "run_busy", "服务正在关闭，暂不接受新运行。")

    # 参数与会话存在性校验先于名额与额度。
    try:
        await service.get_session(session_id)
    except SessionNotFound:
        reject(404, "session_not_found", "会话不存在。")

    # 同会话运行冲突沿用既有 409；不同会话继续竞争全局名额。
    if await service.find_running_run_for_session(session_id) is not None:
        reject(409, "run_busy", "已有任务正在执行。")

    # 会话频率额度：只检查不扣减；受理成功后才扣一次。
    if not session_run_limits.available(session_id):
        retry_after = session_run_limits.retry_after(session_id)
        reject(
            429,
            "rate_limited",
            "请求过于频繁，请稍后重试。",
            headers={"Retry-After": str(retry_after)},
        )

    model = load_model_config()
    # 运行凭据快照覆盖模型密钥与检索密钥；两者均参与消息、Trace 与结果的凭据检查。
    secrets = tuple(secret for secret in (model.api_key, retrieval_secret()) if secret)
    tools = create_file_tools(session_id)
    disconnected = await request.is_disconnected()

    # 全局名额在同一主事件循环临界区检查并占用；不排队，失败直接 503。
    if not active.acquire(blocking=False):
        reject(503, "capacity_exceeded", "当前已有任务执行中，请稍后重试。")

    slot_owned = True
    accepted_run = None
    try:
        outcome = await accept(service, tools, secrets)
        if not outcome.created:
            # 幂等重放不启动执行、不扣额度；防御性归还预占名额。
            active.release()
            slot_owned = False
            return JSONResponse(send_result(operation_id, outcome.run))

        accepted_run = outcome.run
        run = outcome.run
        # 受理成功扣减一次会话额度；同会话受理由 session_gate 串行。
        if not session_run_limits.consume(session_id):
            logger.error("会话运行额度扣减失败（%s）", session_id)

        # 新运行受理成功：捕获受理起点并复用真实业务身份，建立唯一记录器与 agent_run 根 Span。
        trace = RunTrace(
            session_id=session_id,
            operation_id=operation_id,
            run_id=run.id,
            contains=CredentialFilter(secrets).contains,
            accepted_at=time_ns() // 1_000_000,
            accepted_perf=perf_counter(),
        )
        state = RunState(UUID(session_id))
        state.run_id = UUID(run.id)
        state.secrets = secrets
        state.operation_id = operation_id
        state.origin = origin
        state.request_entry_id = run.request_entry_id
        state.coordinator = coordinator
        coordinator.open(
            session_id, run.id, lambda data: state.publish("steering_status", data)
        )
        task = loop.create_task(coordinate(
            run, state, session_id, tools, model, secrets,
            service, request.app.state.business, coordinator,
            request.app.state.executor, loop, trace, request.app.state.trace_store,
        ))
        # 运行登记与独立收尾任务登记同关闭快照互斥，确保已受理运行必被等待。
        # 运行登记后立即移交名额所有权；收尾任务异常也不得回落到预占路径。
        state.task = task
        with runs_lock:
            runs[state.run_id] = state
        slot_owned = False
        with runs_lock:
            track(request.app, task)
            closing = not request.app.state.accepting_runs
        # 断连或关闭只设置同一取消信号并关闭接收入口，收尾任务继续完成收尾。
        if disconnected or closing:
            state.control.request_cancel("cancelled")
            coordinator.close(run.id)
        return RunStream(state, stream(request, state))
    except OperationExpired:
        if slot_owned:
            active.release()
        reject_operation_expired()
    except CredentialDetected:
        if slot_owned:
            active.release()
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    except RunBusy:
        if slot_owned:
            active.release()
        reject(409, "run_busy", "已有任务正在执行。")
    except InvalidTargetEntry:
        if slot_owned:
            active.release()
        reject(409, "invalid_target_entry", "目标节点必须是用户消息。")
    except EntryNotFound:
        if slot_owned:
            active.release()
        reject(404, "entry_not_found", "指定会话中找不到目标节点。")
    except IncompleteToolChain:
        if slot_owned:
            active.release()
        reject(409, "incomplete_tool_chain", "执行分支存在未配对的工具调用。")
    except OperationConflict:
        if slot_owned:
            active.release()
        reject(409, "operation_conflict", "操作请求冲突。")
    except SessionMismatch:
        if slot_owned:
            active.release()
        reject(409, "session_mismatch", "运行与会话不匹配。")
    except SessionNotFound:
        if slot_owned:
            active.release()
        reject(404, "session_not_found", "会话不存在。")
    except BaseException:
        if slot_owned:
            try:
                if accepted_run is not None:
                    # 受理记录已提交：尽力落为可追踪失败，随后归还预占名额。
                    with suppress(Exception):
                        await service.finish_run(
                            session_id,
                            accepted_run.id,
                            "failed",
                            error_code="execution_failed",
                            error_message="执行失败，请重新发起请求。",
                        )
            finally:
                active.release()
        raise


@app.post("/api/agent/run", dependencies=[Depends(check_boundary)])
async def run(payload: RunRequest, request: Request):
    check_workout_query(request, set())
    service: SessionService = request.app.state.session_service
    session_id = str(payload.session_id)
    operation_id = str(payload.operation_id)
    send_request = SendRequest(
        text=payload.request, attachments=payload.attachments,
    )

    async def accept(svc: SessionService, tools: dict, secrets: tuple[str, ...]):
        system_message = SystemMessage(
            role="system", content=SYSTEM_PROMPT,
            tools_added=[
                tool.definition() for tool in tools.values()
            ] + business_tool_declarations(),
            timestamp=time_ns() // 1_000_000,
        )
        return await svc.accept_send(
            SendCommand(
                operation_id=operation_id,
                session_id=session_id,
                request=send_request,
            ),
            system_message=system_message,
            credentials=secrets,
        )

    async with operation_lock(operation_id), session_gate(session_id):
        existing = await resolve_existing(
            service, operation_id, session_id, "send", send_request
        )
        if existing is not None:
            return JSONResponse(send_result(operation_id, existing.run))
        return await launch_run(
            request, session_id=session_id, operation_id=operation_id,
            origin="send", accept=accept,
        )


@app.post("/api/agent/edit", dependencies=[Depends(check_boundary)])
async def edit(payload: EditPayload, request: Request):
    check_workout_query(request, set())
    service: SessionService = request.app.state.session_service
    session_id = str(payload.session_id)
    operation_id = str(payload.operation_id)
    edit_request = EditRequest(
        target_entry_id=str(payload.target_entry_id), text=payload.request,
        **({"attachments": payload.attachments} if "attachments" in payload.model_fields_set else {}),
    )

    async def accept(svc: SessionService, tools: dict, secrets: tuple[str, ...]):
        replacements: ReplacementCoordinator = request.app.state.replacements
        # 内容替换登记会话替换意图：确认提交在事务提交前检查该意图并让位。
        async with replacements.register(session_id):
            return await svc.accept_edit(
                EditCommand(
                    operation_id=operation_id,
                    session_id=session_id,
                    request=edit_request,
                ),
                credentials=secrets,
            )

    async with operation_lock(operation_id), session_gate(session_id):
        existing = await resolve_existing(
            service, operation_id, session_id, "edit", edit_request
        )
        if existing is not None:
            return JSONResponse(send_result(operation_id, existing.run))
        return await launch_run(
            request, session_id=session_id, operation_id=operation_id,
            origin="edit", accept=accept,
        )


@app.post("/api/agent/regenerate", dependencies=[Depends(check_boundary)])
async def regenerate(payload: RegeneratePayload, request: Request):
    check_workout_query(request, set())
    service: SessionService = request.app.state.session_service
    session_id = str(payload.session_id)
    operation_id = str(payload.operation_id)
    regenerate_request = RegenerateRequest(
        target_entry_id=str(payload.target_entry_id)
    )

    async def accept(svc: SessionService, tools: dict, secrets: tuple[str, ...]):
        replacements: ReplacementCoordinator = request.app.state.replacements
        async with replacements.register(session_id):
            return await svc.accept_regenerate(
                RegenerateCommand(
                    operation_id=operation_id,
                    session_id=session_id,
                    request=regenerate_request,
                ),
                credentials=secrets,
            )

    async with operation_lock(operation_id), session_gate(session_id):
        existing = await resolve_existing(
            service, operation_id, session_id, "regenerate", regenerate_request
        )
        if existing is not None:
            return JSONResponse(send_result(operation_id, existing.run))
        return await launch_run(
            request, session_id=session_id, operation_id=operation_id,
            origin="regenerate", accept=accept,
        )


@app.post("/api/agent/runs/{run_id}/steering", dependencies=[Depends(check_boundary)])
async def steering(run_id: PathUUID, payload: SteeringRequest, request: Request):
    check_workout_query(request, set())
    coordinator: SteeringCoordinator = app.state.steering
    service: SessionService = app.state.session_service
    steering_request = SteeringParams(target_run_id=run_id, text=payload.message, attachments=payload.attachments)
    existing = await resolve_existing(
        service, payload.operation_id, payload.session_id, "steering", steering_request,
    )
    if existing is None:
        await require_run(service, run_id, payload.session_id)
    credentials = active_secrets(str(payload.session_id))
    try:
        created, steering_input = await coordinator.accept(
            str(run_id),
            SteeringCommand(
                operation_id=str(payload.operation_id),
                session_id=str(payload.session_id),
                request=steering_request,
            ),
            credentials=credentials,
        )
    except RunClosed:
        reject(409, "run_closed", "运行已关闭。")
    except OperationExpired:
        reject_operation_expired()
    except OperationConflict:
        reject(409, "operation_conflict", "操作请求冲突。")
    except SessionMismatch:
        reject(409, "session_mismatch", "运行与会话不匹配。")
    except SessionNotFound:
        reject(404, "run_not_found", "运行不存在。")
    except CredentialDetected:
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    if CredentialFilter(credentials).contains(steering_input.model_dump()):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return {
        "operation_id": str(payload.operation_id),
        "session_id": str(payload.session_id),
        "run_id": str(run_id),
        "steering_id": steering_input.id,
        "created": created,
        "status": "accepted" if created else steering_input.status,
        "entry_id": steering_input.entry_id,
        "reason": steering_input.reason,
    }


@app.post(
    "/api/agent/runs/{run_id}/steering/{steering_id}/withdraw",
    dependencies=[Depends(check_boundary)],
)
async def withdraw(run_id: PathUUID, steering_id: PathUUID, payload: WithdrawRequest, request: Request):
    check_workout_query(request, set())
    coordinator: SteeringCoordinator = app.state.steering
    await require_run(app.state.session_service, run_id, payload.session_id)
    try:
        withdrawal = await coordinator.withdraw(
            str(run_id), str(payload.session_id), str(steering_id)
        )
    except SteeringConsumptionConflict:
        reject(409, "steering_consumption_conflict", "目标输入消费已开始。")
    except SessionMismatch:
        reject(409, "session_mismatch", "运行与会话不匹配。")
    except SessionNotFound:
        reject(404, "steering_not_found", "输入不存在。")
    steering_input = withdrawal.steering
    if CredentialFilter(active_secrets(str(payload.session_id))).contains(steering_input.model_dump()):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return {
        "session_id": str(payload.session_id),
        "run_id": str(run_id),
        "steering_id": steering_input.id,
        "status": steering_input.status,
        "entry_id": steering_input.entry_id,
        "reason": steering_input.reason,
    }


@app.get(
    "/api/sessions/{session_id}/operations/{operation_id}",
    dependencies=[Depends(check_boundary)],
)
async def get_operation(session_id: PathUUID, operation_id: PathUUID, request: Request):
    check_workout_query(request, set())
    service: SessionService = app.state.session_service
    try:
        outcome = await service.get_operation_outcome(session_id, operation_id)
    except OperationExpired:
        reject_operation_expired()
    except SessionNotFound:
        reject(404, "session_not_found", "会话不存在。")
    guard = CredentialFilter(active_secrets(session_id))
    if guard.contains({"session_id": session_id, "operation_id": operation_id, "outcome": outcome.model_dump() if outcome is not None else None}):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    payload = {
        "operation_id": operation_id,
        "session_id": session_id,
        "accepted": outcome is not None,
        "kind": outcome.operation.kind if outcome is not None else None,
        "run": run_object(outcome.run) if outcome is not None else None,
        "steering": steering_object(outcome.steering) if outcome is not None and outcome.steering is not None else None,
    }
    if guard.contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers=NO_STORE)


@app.get(
    "/api/sessions/{session_id}/runs/{run_id}",
    dependencies=[Depends(check_boundary)],
)
async def get_run(session_id: PathUUID, run_id: PathUUID, request: Request):
    check_workout_query(request, set())
    service: SessionService = app.state.session_service
    try:
        await service.get_session(str(session_id))
    except SessionNotFound:
        reject(404, "session_not_found", "会话不存在。")
    try:
        run = await service.get_run(str(session_id), str(run_id))
    except SessionNotFound:
        reject(404, "run_not_found", "运行不存在。")
    payload = run_object(run)
    if CredentialFilter(active_secrets(session_id)).contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers=NO_STORE)


@app.get(
    "/api/sessions/{session_id}/runs",
    dependencies=[Depends(check_boundary)],
)
async def list_session_runs(session_id: PathUUID, request: Request):
    check_workout_query(request, set())
    service: SessionService = app.state.session_service
    guard = CredentialFilter(active_secrets(session_id))
    try:
        stored = await service.list_runs(session_id)
    except SessionNotFound:
        reject(404, "session_not_found", "会话不存在。")
    payload = {
        "session_id": session_id,
        "runs": [run_object(run) for run in stored],
    }
    if guard.contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.get(
    "/api/sessions/{session_id}/runs/{run_id}/steering",
    dependencies=[Depends(check_boundary)],
)
async def list_run_steering(session_id: PathUUID, run_id: PathUUID, request: Request):
    check_workout_query(request, set())
    service: SessionService = app.state.session_service
    guard = CredentialFilter(active_secrets(session_id))
    try:
        stored = await service.list_steering(session_id, run_id)
    except RunNotFound:
        reject(404, "run_not_found", "运行不存在。")
    except SessionNotFound:
        reject(404, "session_not_found", "会话不存在。")
    # 投影前先检查完整原始记录：输入正文、内容块签名及嵌套元数据。
    if guard.contains([item.model_dump() for item in stored]):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    payload = {
        "session_id": session_id,
        "run_id": run_id,
        "steering": [history_steering(item) for item in stored],
    }
    if guard.contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})
