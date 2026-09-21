"""唯一 aiosqlite 连接 + asyncio.Lock 串行化 + 事务入口，WAL/外键/busy_timeout（07 7.1）。"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar

import aiosqlite

from app.infrastructure.database.migrations import run_migrations

BUSY_TIMEOUT_MS = 5000

T = TypeVar("T")


def require_outer_transaction(conn: aiosqlite.Connection, what: str) -> None:
    """断言 ``conn`` 来自 :meth:`Database.transaction`：本层不自行 BEGIN/COMMIT。"""
    if not conn.in_transaction:
        raise RuntimeError(
            f"{what}必须复用外层事务（Database.transaction()）：本层不自行 BEGIN/COMMIT"
        )


async def _await_settled(
    statement: Coroutine[Any, Any, object],
) -> tuple[bool, BaseException | None]:
    """等待一段串行化操作（语句 / 取锁 + 语句）settle，其间到达的取消只记账。"""
    task = asyncio.ensure_future(statement)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if task.cancelled():
        return True, None
    return cancelled, task.exception()


_PRAGMA_QUERIES: dict[str, str] = {
    "journal_mode": "PRAGMA journal_mode",
    "foreign_keys": "PRAGMA foreign_keys",
    "busy_timeout": "PRAGMA busy_timeout",
    "user_version": "PRAGMA user_version",
}


class Database:
    """单连接 + 单锁的 SQLite 访问入口；连接对象不对外暴露（07 7.1）。"""

    def __init__(self, path: str | Path, migrations_dir: str | Path | None = None):
        self._path = Path(path)
        self._migrations_dir = (
            Path(migrations_dir) if migrations_dir is not None else None
        )
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    def _require_open(self) -> aiosqlite.Connection:
        conn = self._conn
        if conn is None:
            raise RuntimeError("数据库未打开：先调用 open()")
        return conn

    async def open(self) -> None:
        """打开唯一连接并初始化 PRAGMA；重复 open 视为编程错误。"""
        if self._conn is not None:
            raise RuntimeError("数据库已打开，禁止重复 open")
        conn = await aiosqlite.connect(self._path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        try:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.execute("PRAGMA busy_timeout=5000")
        except BaseException:
            await conn.close()
            raise
        self._conn = conn

    async def close(self) -> None:
        """关闭连接；不遗留本应用持有的连接，之后可再次 open（重开/停服检查）。

        取消安全（S0-02）：等待期内的取消只记账，close settle 后才传播。
        """
        conn = self._conn
        if conn is None:
            return
        self._conn = None
        cancelled, close_error = await _await_settled(conn.close())
        # 取消优先，close 自身异常挂在 __cause__ 上不被吞。
        if cancelled:
            raise asyncio.CancelledError from close_error
        if close_error is not None:
            raise close_error

    async def migrate(self) -> int:
        """执行编号迁移（07 7.2）。迁移失败抛异常：启动流程据此拒绝对外服务。"""
        conn = self._require_open()
        async with self._lock:
            return await run_migrations(conn, self._migrations_dir)

    async def under_lock(
        self, operation: Callable[[aiosqlite.Connection], Awaitable[T]]
    ) -> T:
        """在唯一锁保护下执行一个数据库操作回调（读取与单语句写入同样不绕锁）。"""
        conn = self._require_open()
        async with self._lock:
            return await operation(conn)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """多语句事务：从 BEGIN 到 COMMIT/ROLLBACK 全程持有唯一锁。

    异常（含 CancelledError）一律回滚并重新抛出，不遗留开放事务或被占用的锁。
    """
        conn = self._require_open()
        async with self._lock:
            cancelled, begin_error = await _await_settled(conn.execute("BEGIN"))
            try:
                if begin_error is not None:
                    raise begin_error
                if cancelled:
                    raise asyncio.CancelledError
                yield conn
                await conn.commit()
            except BaseException:
                if conn.in_transaction:
                    # 回滚同样要跑完：取消不能把开放事务留给下一个持有者。
                    await _await_settled(conn.rollback())
                raise

    async def pragma_value(self, name: str) -> str | int | None:
        """查询固定名单内的 PRAGMA 当前值（连接配置可查询验证，S0-02）。"""
        sql = _PRAGMA_QUERIES.get(name)
        if sql is None:
            raise ValueError(f"不允许查询 PRAGMA {name!r}")
        conn = self._require_open()
        async with self._lock, conn.execute(sql) as cursor:
            row = await cursor.fetchone()
        return None if row is None else row[0]
