"""运行治理共享契约：截止时间、取消上下文、首个停止原因与终态映射。

只定义模型执行保护、HTTP 治理与前端恢复任务共同依赖的基础类型与纯函数；
执行机制（模型重试循环、工具取消、队列与故障封闭接线）由对应任务接入。
"""

from __future__ import annotations

import os
from threading import Event, Lock
from time import monotonic
from typing import Callable, Literal, TypeAlias

# 运行级固定时间预算（秒）。整个运行包含重试等待与上下文压缩。
RUN_EXECUTION_BUDGET_SECONDS = 300.0
# 取消后的协作收尾等待：超过时记录 worker_stuck 并保持运行占用。
CANCEL_COOPERATIVE_WAIT_SECONDS = 10.0
# 终态持久化等待：从调用 finish 提交终态时起计。
TERMINAL_PERSIST_WAIT_SECONDS = 10.0
# 终态排空总时限：从独立终态槽位填充时起计，包含剩余普通事件与终态发送。
TERMINAL_DRAIN_TOTAL_SECONDS = 10.0
# 诊断写入上限：数据库阻塞时不得无限占用主事件循环。
DIAGNOSTIC_WRITE_WAIT_SECONDS = 1.0

# 新运行受理额度：同一会话每分钟最多 10 次成功受理。
SESSION_RUN_RATE_LIMIT = "10/minute"

# SSE 有界队列与发送边界。字节按最终 SSE 的 UTF-8 编码计量。
SSE_QUEUE_MAX_EVENTS = 256
SSE_QUEUE_MAX_BYTES = 1024 * 1024
SSE_EVENT_MAX_BYTES = 1024 * 1024
SSE_HEARTBEAT_SECONDS = 15.0
SSE_SEND_WAIT_SECONDS = 10.0


def configured_run_budget_seconds() -> float:
    """运行预算的集中配置入口；测试可用环境变量注入缩短的预算。"""
    raw = os.environ.get("FIT_AGENT_RUN_BUDGET_SECONDS")
    if raw is None:
        return RUN_EXECUTION_BUDGET_SECONDS
    value = float(raw)
    if value <= 0:
        raise ValueError("运行预算必须为正数")
    return value

# 模型与工具执行边界。子调用取自身时限与运行剩余预算的较小值。
HTTP_CONNECT_SECONDS = 10.0
HTTP_POOL_SECONDS = 5.0
HTTP_WRITE_SECONDS = 10.0
HTTP_READ_SECONDS = 60.0
MODEL_FIRST_INCREMENT_SECONDS = 60.0
MODEL_INCREMENT_INTERVAL_SECONDS = 60.0
MODEL_ATTEMPT_SECONDS = 180.0
TOOL_EXECUTION_SECONDS = 30.0

# 全抖动退避上限：第一次 0–2 秒，第二次 0–4 秒；Retry-After 作为最小等待。
RETRY_BACKOFF_CAPS = (2.0, 4.0)
# 单次退避等待上限：超过剩余运行预算或该上限时结束恢复。
RETRY_MAX_WAIT_SECONDS = 30.0

# 首个有效停止原因：决定运行终态，只接受首个有效值。
RunStopReason: TypeAlias = Literal[
    "model_timeout",
    "tool_timeout",
    "run_timeout",
    "cancelled",
    "slow_consumer",
    "output_too_large",
]

# 诊断原因：执行尚未退出或终态持久化异常时记录，不单独映射终态。
RunDiagnosticReason: TypeAlias = Literal["worker_stuck", "terminal_persist_failed"]

# 诊断原因全集：持久化入口按此校验，避免写入未登记诊断。
DIAGNOSTIC_REASONS: frozenset[str] = frozenset(
    {"worker_stuck", "terminal_persist_failed"}
)

# 停止原因可映射的终态：超时与输出超限为 failed，取消类为 cancelled。
RunStopStatus: TypeAlias = Literal["failed", "cancelled"]

# 停止原因 -> (终态, 公开错误码)。用户取消与浏览器断连沿用既有公开取消契约。
STOP_TERMINAL: dict[RunStopReason, tuple[RunStopStatus, str]] = {
    "model_timeout": ("failed", "model_timeout"),
    "tool_timeout": ("failed", "tool_timeout"),
    "run_timeout": ("failed", "run_timeout"),
    "cancelled": ("cancelled", "cancelled"),
    "slow_consumer": ("cancelled", "slow_consumer"),
    "output_too_large": ("failed", "output_too_large"),
}


def stop_terminal(reason: RunStopReason) -> tuple[RunStopStatus, str]:
    return STOP_TERMINAL[reason]


class RunDeadline:
    """运行截止时间（单调时钟）。

    截止值属于进程内状态：不落库、不跨进程共享。构造后只读，主事件循环与工作
    线程可无锁读取；子调用预算取自身上限与剩余运行预算的较小值。
    """

    def __init__(
        self,
        seconds: float = RUN_EXECUTION_BUDGET_SECONDS,
        *,
        clock: Callable[[], float] = monotonic,
        started_at: float | None = None,
    ) -> None:
        if seconds <= 0:
            raise ValueError("运行预算必须为正数")
        self._clock = clock
        self._seconds = float(seconds)
        self._started_at = clock() if started_at is None else started_at

    @property
    def seconds(self) -> float:
        return self._seconds

    @property
    def started_at(self) -> float:
        return self._started_at

    @property
    def expires_at(self) -> float:
        return self._started_at + self._seconds

    def remaining(self) -> float:
        return max(0.0, self.expires_at - self._clock())

    def expired(self) -> bool:
        return self._clock() >= self.expires_at

    def effective(self, ceiling: float) -> float:
        return min(ceiling, self.remaining())


class StopDecision:
    """首个有效停止原因：加锁裁决，只写入一次，后续写入不覆盖。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._reason: RunStopReason | None = None
        self._stopped_at: float | None = None

    def request(self, reason: RunStopReason) -> bool:
        # 返回 True 表示本次写入生效；False 表示已有原因，保持原值。
        with self._lock:
            if self._reason is not None:
                return False
            self._reason = reason
            if self._stopped_at is None:
                self._stopped_at = monotonic()
            return True

    def note_stopped(self) -> None:
        # 取消信号可能由既有代码直接置位；首次观察时补记共享收尾起点。
        with self._lock:
            if self._stopped_at is None:
                self._stopped_at = monotonic()

    @property
    def reason(self) -> RunStopReason | None:
        with self._lock:
            return self._reason

    @property
    def stopped_at(self) -> float | None:
        with self._lock:
            return self._stopped_at


class RunControl:
    """运行控制上下文：取消信号、截止时间与首个停止原因。

    同步规则：取消沿用 threading.Event，跨主事件循环与工作线程安全读写；
    截止时间为受控只读值；停止原因由 StopDecision 加锁裁决。request_cancel
    先记录首个原因再置位信号，保证观察者读到信号时原因已确定。
    """

    def __init__(
        self,
        *,
        deadline: RunDeadline | None = None,
        cancel: Event | None = None,
        stop: StopDecision | None = None,
    ) -> None:
        self.cancel = cancel if cancel is not None else Event()
        self.deadline = deadline
        self.stop = stop if stop is not None else StopDecision()

    @property
    def cancelled(self) -> bool:
        return self.cancel.is_set()

    def request_cancel(self, reason: RunStopReason = "cancelled") -> bool:
        recorded = self.stop.request(reason)
        self.cancel.set()
        return recorded

    def note_stop(self) -> None:
        self.stop.note_stopped()

    @property
    def stopped_at(self) -> float | None:
        return self.stop.stopped_at

    def cooperative_deadline(self) -> float | None:
        # 共享收尾截止时间：从首次停止触发时刻起算，各层级不得重新起算。
        started = self.stop.stopped_at
        return None if started is None else started + CANCEL_COOPERATIVE_WAIT_SECONDS

    def remaining(self) -> float | None:
        return None if self.deadline is None else self.deadline.remaining()
