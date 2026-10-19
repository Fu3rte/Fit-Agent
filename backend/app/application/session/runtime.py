import asyncio
import logging
from collections.abc import Callable
from math import ceil
from threading import Event, Lock
from time import time
from typing import Literal, Protocol
from uuid import UUID, uuid4
from weakref import WeakValueDictionary

from limits import parse
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter

from app.agent.governance import (
    SESSION_RUN_RATE_LIMIT,
    RunControl,
    RunDeadline,
    configured_run_budget_seconds,
)
from app.agent.tool import ToolExecutionTracker
from app.application.session.steering import SteeringCoordinator

OUTPUT_TOO_LARGE_MESSAGE = "输出内容超过单事件上限，已结束本次运行。"

logger = logging.getLogger(__name__)
active = Lock()
runs_lock = Lock()
accepting_runs = Event()
coordination: set[asyncio.Task] = set()


def track(task: asyncio.Task) -> None:
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
# 同键并发请求串行到受理提交：键随引用消失，不累积。
operation_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
# 新运行受理与会话删除串行，覆盖受理提交至内存登记间的竞态窗口。
session_gates: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def operation_lock(operation_id: str) -> asyncio.Lock:
    return operation_locks.setdefault(operation_id, asyncio.Lock())


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


class RunOutput(Protocol):
    # 事件出口由传输层提供；应用故障封闭只关闭普通输出。
    def publish(self, name: str, data: dict) -> bool: ...

    def publish_terminal(self, name: Literal["done", "error"], data: dict) -> None: ...

    def close_normal(self) -> None: ...


class RunState:
    def __init__(
        self,
        session_id: UUID,
        *,
        create_output: Callable[[UUID, RunControl], RunOutput],
        budget_seconds: float | None = None,
        run_id: UUID | None = None,
    ) -> None:
        self.run_id = run_id if run_id is not None else uuid4()
        self.session_id = session_id
        self.operation_id = ""
        self.request_entry_id = ""
        self.cancel = Event()
        self.control = RunControl(
            deadline=RunDeadline(
                configured_run_budget_seconds()
                if budget_seconds is None
                else budget_seconds
            ),
            cancel=self.cancel,
        )
        self.output = create_output(self.run_id, self.control)
        self.tool_tracker = ToolExecutionTracker()
        self.coordinator: SteeringCoordinator | None = None
        self.task: asyncio.Task | None = None
        self.closed_without_terminal = False
        self.secrets: tuple[str, ...] = ()
        self.origin = "send"
        # 显式装配的调用观测工厂：默认 None 时沿用生产 CacheObserver。
        self.observation_factory: Callable[[], object] | None = None
        # 故障收尾保留真实在途句柄，超时不得作为回滚证据。
        self.fault_reason: str | None = None
        self.inflight_work = None
        self.inflight_tools: tuple = ()
        self.inflight_reads: list[asyncio.Task] = []
        self.diagnostic_tasks: list[asyncio.Task] = []

    def publish(self, name: str, data: dict) -> bool:
        return self.output.publish(name, data)

    def publish_terminal(self, name: Literal["done", "error"], data: dict) -> None:
        self.output.publish_terminal(name, data)

    def disconnect(self) -> None:
        self.control.request_cancel("cancelled")
        if self.coordinator is not None:
            self.coordinator.close(str(self.run_id))

    def mark_fault(self, reason: str, work, tools: tuple) -> None:
        # 故障封闭：不提交成功确认、不释放名额。
        self.fault_reason = reason
        self.inflight_work = work
        self.inflight_tools = tools
        self.closed_without_terminal = True
        self.output.close_normal()


runs: dict[UUID, RunState] = {}
