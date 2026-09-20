"""tool_cache_revisions 表的手写 SQL：namespace revision 的事务内自增与只读读取。"""

import aiosqlite

from storage.db import Database, require_outer_transaction

#: 首批 namespace 与四个只读工具实际读取的业务表一一对应（exercises 由 schema_version 承担）。
CACHE_NAMESPACES: tuple[str, ...] = ("plans", "workouts", "metrics")

_UPDATE_REVISION = (
    "UPDATE tool_cache_revisions SET revision = revision + 1 WHERE namespace = ?"
)
_SELECT_REVISION = (
    "SELECT revision FROM tool_cache_revisions WHERE namespace = ?"
)


async def _read_all_revisions(conn: aiosqlite.Connection) -> dict[str, int]:
    async with conn.execute(
        "SELECT namespace, revision FROM tool_cache_revisions"
    ) as cursor:
        rows = await cursor.fetchall()
    return {str(row["namespace"]): int(row["revision"]) for row in rows}


class ToolCacheRevisionsRepo:
    """``tool_cache_revisions`` 的读写：bump 复用业务写事务，读取走唯一锁。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def read_all(self) -> dict[str, int]:
        """当前全部 namespace revision（Harness 执行工具前读取）。"""
        return await self._db.under_lock(_read_all_revisions)

    async def bump_in_transaction(
        self, conn: aiosqlite.Connection, namespace: str
    ) -> int:
        """在业务写事务内把该 namespace 的 revision 加一，返回新值；不自行 BEGIN／COMMIT。"""
        if namespace not in CACHE_NAMESPACES:
            raise ValueError(f"未知的缓存 namespace：{namespace!r}")
        require_outer_transaction(conn, f"{namespace} revision bump")
        cursor = await conn.execute(_UPDATE_REVISION, (namespace,))
        try:
            changed = cursor.rowcount
        finally:
            await cursor.close()
        if changed != 1:
            raise RuntimeError(
                f"revision bump 未命中唯一行：{namespace}（受影响 {changed} 行）"
            )
        async with conn.execute(_SELECT_REVISION, (namespace,)) as read:
            row = await read.fetchone()
        if row is None:
            raise RuntimeError(f"revision bump 后读回失败：{namespace}")
        return int(row["revision"])
