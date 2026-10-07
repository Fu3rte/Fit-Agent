import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class ReplacementCoordinator:
    """会话内容替换与确认提交共用的同一协调入口。

    编辑/重新生成受理在确认事务执行期间登记意图，登记不等待提交；确认事务的
    COMMIT 在实际执行瞬间按 `pending_count` 裁决，存在意图时提交被否决并回滚，
    待替换受理后重新读取快照及校验，已落地提交保持有效。
    """

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._pending: dict[str, int] = {}

    def pending_count(self, session_id: str) -> int:
        # 供连接线程在 COMMIT 执行期间同步读取：只暴露纯 Python 状态。
        return self._pending.get(session_id, 0)

    @asynccontextmanager
    async def register(self, session_id: str) -> AsyncIterator[None]:
        async with self._condition:
            self._pending[session_id] = self._pending.get(session_id, 0) + 1
            self._condition.notify_all()
        try:
            yield
        finally:
            async with self._condition:
                remaining = self._pending.get(session_id, 0) - 1
                if remaining > 0:
                    self._pending[session_id] = remaining
                else:
                    self._pending.pop(session_id, None)
                self._condition.notify_all()

    async def wait_clear(self, session_id: str) -> None:
        async with self._condition:
            while self._pending.get(session_id):
                await self._condition.wait()
