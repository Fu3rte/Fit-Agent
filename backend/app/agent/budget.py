from threading import Lock

# 运行级请求次数上限：普通请求、摘要、恢复重试与 Steering 共用同一预算。
MAX_MODEL_REQUESTS = 64

# 单次请求上下文安全余量：在输出额度之外额外预留，纳入预算检查。
SAFETY_MARGIN_TOKENS = 4096

# 文本字符到 token 的统一估算系数：普通请求与摘要输入共用，向上取整。
CHARS_PER_TOKEN = 3.5


class ModelRequestLimitExceeded(Exception):
    # 本次运行已用尽请求名额：在发送前拒绝并终止运行。
    pass


class RequestBudget:
    # 运行级共享请求预算：同一运行的所有模型请求使用同一实例，独立运行各自创建。
    def __init__(self, limit: int = MAX_MODEL_REQUESTS):
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit 必须为正整数")
        self._limit = limit
        self._used = 0
        self._lock = Lock()

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    def reserve(self) -> int:
        # 发送前检查并占用一个名额：第 limit 次允许，占满后再占用抛出耗尽异常。
        with self._lock:
            if self._used >= self._limit:
                raise ModelRequestLimitExceeded(
                    f"本次运行已达到 {self._limit} 次模型请求上限"
                )
            self._used += 1
            return self._used
