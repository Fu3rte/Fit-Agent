"""唯一 aiosqlite 连接 + asyncio.Lock 串行化 + 事务入口，WAL/外键/busy_timeout（07 7.1）。

- FastAPI 生命周期内保持一个连接，不使用连接池；生命周期装配见 api/app.py。
- 所有数据库访问（包括读取和单语句写入）经同一个 asyncio.Lock 串行化，
  防止共享连接上的读取插入未提交事务。该锁只保证数据库串行访问，
  不冒充全局 Agent Run 互斥（那归 08 章 / Stage 4）。
- ``isolation_level=None``（autocommit）：事务边界完全由 :meth:`Database.transaction`
  的显式 BEGIN/COMMIT/ROLLBACK 控制，杜绝隐式事务。
- 本模块不持有业务 SQL：语句常量与参数化查询都在 repo（README 硬规则 3
  "SQL 只在 repo"）；repo 经 :meth:`under_lock` / :meth:`transaction` 在锁内执行。
- 事务只包含数据库操作；事务体内禁止模型请求、工具执行或 SSE 推送，
  也禁止再次获取锁（锁不可重入，嵌套会死锁）。
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar

import aiosqlite

from storage.migrations import run_migrations

BUSY_TIMEOUT_MS = 5000

T = TypeVar("T")


def require_outer_transaction(conn: aiosqlite.Connection, what: str) -> None:
    """断言 ``conn`` 来自 :meth:`Database.transaction`：本层不自行 BEGIN/COMMIT。

    只接受外层事务连接的 repo 方法（档案读写、动作引用读取、草稿提交凭据写入）用它
    守住契约：不在事务内即编程错误，既避免在持锁区间内再取锁（锁不可重入，会死锁），
    也避免留下「部分写入 + 由调用方补提交」的半事务形态（stage2.md §2）。``what``
    只用于错误消息定位。
    """
    if not conn.in_transaction:
        raise RuntimeError(
            f"{what}必须复用外层事务（Database.transaction()）：本层不自行 BEGIN/COMMIT"
        )


async def _await_settled(
    statement: Coroutine[Any, Any, object],
) -> tuple[bool, BaseException | None]:
    """等待一段串行化操作（语句 / 取锁 + 语句）settle，其间到达的取消只记账。

    aiosqlite 语句一旦排入后台线程队列就必然执行，取消调用方并不能撤销它；所以
    BEGIN/ROLLBACK 与压制栅栏都必须等其 settle（含等锁阶段）后才能继续。等待由独立
    Task 承接，调用方被取消（含反复取消）不会打断它。返回 ``(等待期间是否被取消,
    该操作自身异常)``，由调用方决定何时恢复取消传播。
    """
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


# pragma_value 只放行固定名单内的查询；语句为字面量，不允许拼接（S0-02 可查询验证）。
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

        取消安全（S0-02）：等待期内的取消只记账，close settle 后才传播——否则
        close() 已上抛而工作线程仍会经 call_soon_threadsafe 向（随后关闭的）事件
        循环投递结果。连接先摘除（``_conn = None``），保证退出后 ``is_open`` 为 False。
        """
        conn = self._conn
        if conn is None:
            return
        self._conn = None
        cancelled, close_error = await _await_settled(conn.close())
        # 底层关闭 settle 之后才传播取消；取消优先，close 自身异常挂在 __cause__ 上不被吞。
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
        """在唯一锁保护下执行一个数据库操作回调（07 7.1：读取与单语句写入同样不绕锁）。

        回调只允许做数据库操作（读或单语句写入）；多语句原子性必须走
        :meth:`transaction`。回调内禁止再获取锁（锁不可重入）。
        """
        conn = self._require_open()
        async with self._lock:
            return await operation(conn)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """多语句事务：从 BEGIN 到 COMMIT/ROLLBACK 全程持有唯一锁（07 7.1）。

        事务体只使用 yield 出的连接做数据库操作；异常（含 CancelledError）一律回滚并
        重新抛出，不遗留开放事务或被占用的锁。BEGIN 交给独立 Task 并 shield 等待：
        在 BEGIN 等待点被取消时，仍在持锁状态下等它 settle（后台线程可能随后才真正
        开启事务），再按 in_transaction 回滚，最后才恢复取消传播（S0-03）。
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
