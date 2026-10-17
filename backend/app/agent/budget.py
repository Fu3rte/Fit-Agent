from threading import Lock

# 运行级请求次数上限：普通请求、摘要、恢复重试与 Steering 共用同一预算。
MAX_MODEL_REQUESTS = 64

# 整个运行共享的额外恢复请求额度：溢出恢复摘要、恢复后的重发与失败重试共同消耗。
MAX_RECOVERY_REQUESTS = 4

# 单个模型步骤的最多尝试次数：首次真实请求加 2 次自动重试。
MAX_MODEL_STEP_ATTEMPTS = 3

# 单次请求上下文安全余量：在输出额度之外额外预留，纳入预算检查。
SAFETY_MARGIN_TOKENS = 4096

# 文本字符到 token 的统一估算系数：普通请求与摘要输入共用，向上取整。
CHARS_PER_TOKEN = 3.5


class ModelRequestLimitExceeded(Exception):
    # 本次运行已用尽请求名额：在发送前拒绝并终止运行。
    pass


class RecoveryBudgetExhausted(Exception):
    # 本次运行的共享恢复额度已用尽：在发送恢复请求前拒绝。
    pass


class RequestBudget:
    # 运行级共享请求预算：同一运行的所有模型请求使用同一实例，独立运行各自创建。
    # 一次真实上游请求只扣一次：普通请求只计总预算，恢复请求同时计恢复额度。
    def __init__(
        self,
        limit: int = MAX_MODEL_REQUESTS,
        *,
        recovery_limit: int = MAX_RECOVERY_REQUESTS,
    ):
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit 必须为正整数")
        if type(recovery_limit) is not int or recovery_limit < 0:
            raise ValueError("recovery_limit 必须为非负整数")
        self._limit = limit
        self._used = 0
        self._recovery_limit = recovery_limit
        self._recovery_used = 0
        self._lock = Lock()

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return self._limit - self._used

    @property
    def recovery_limit(self) -> int:
        return self._recovery_limit

    @property
    def recovery_used(self) -> int:
        with self._lock:
            return self._recovery_used

    @property
    def recovery_remaining(self) -> int:
        with self._lock:
            return self._recovery_limit - self._recovery_used

    def can_reserve(self, *, recovery: bool = False) -> bool:
        # 调用前检查：只读判定，不改变计数；组合检查在同一临界区完成。
        with self._lock:
            return self._available_locked(recovery)

    def ensure_available(self, *, recovery: bool = False) -> None:
        # 发送前预检：按与 reserve 相同的组合规则抛错，但不扣款。
        # 供调用方在创建模型 Span 等可观测结构前拒绝；真正的发送边界继续用 reserve 原子扣除。
        with self._lock:
            self._ensure_available_locked(recovery)

    def reserve(self, *, recovery: bool = False) -> int:
        # 检查并占用：总预算与恢复额度在同一临界区内扣除，避免部分扣除。
        # 总预算耗尽优先于恢复额度耗尽，保留既有超限行为。
        with self._lock:
            self._ensure_available_locked(recovery)
            self._used += 1
            if recovery:
                self._recovery_used += 1
            return self._used

    def _ensure_available_locked(self, recovery: bool) -> None:
        if self._used >= self._limit:
            raise ModelRequestLimitExceeded(
                f"本次运行已达到 {self._limit} 次模型请求上限"
            )
        if recovery and self._recovery_used >= self._recovery_limit:
            raise RecoveryBudgetExhausted(
                f"本次运行已达到 {self._recovery_limit} 次恢复请求额度"
            )

    def _available_locked(self, recovery: bool) -> bool:
        if self._used >= self._limit:
            return False
        return not recovery or self._recovery_used < self._recovery_limit
