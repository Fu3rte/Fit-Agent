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
- 绑定参数含凭据的写入必须在 :meth:`Database.parameter_echo_suppressed` 内进行，
  否则驱动层 DEBUG 日志会把绑定参数（含明文 Key）渲染进日志（10.3 绝对禁止）。
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar

import aiosqlite

from storage.migrations import run_migrations

BUSY_TIMEOUT_MS = 5000

# 在 DEBUG 级别把「语句 + 其绑定参数」渲染成日志文本的第三方 logger。
# aiosqlite 的连接工作线程对每条排队语句打 LOG.debug("executing %s", partial(...))，
# 而 functools.partial 的 repr 包含实参元组：明文凭据作为绑定参数时即在此泄露。
_PARAMETER_ECHOING_LOGGERS = ("aiosqlite",)

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


def _logger_state(name: str) -> tuple[int, bool]:
    logger = logging.getLogger(name)
    return (logger.level, logger.disabled)


def _restore_logger_state(name: str, state: tuple[int, bool]) -> None:
    logger = logging.getLogger(name)
    logger.setLevel(state[0])
    logger.disabled = state[1]


# 压制状态按 logger 名引用计数（语义见 parameter_echo_suppressed）；计数与级别变更
# 只发生在事件循环线程上（单一 uvicorn worker，见 11 章），因此无需加锁。
_ECHO_SUPPRESSION_COUNT: dict[str, int] = {}
_ECHO_SAVED_STATE: dict[str, tuple[int, bool]] = {}


def _suppress_parameter_echo(name: str) -> None:
    count = _ECHO_SUPPRESSION_COUNT.get(name, 0)
    if count == 0:
        _ECHO_SAVED_STATE[name] = _logger_state(name)
        logging.getLogger(name).setLevel(logging.WARNING)
    _ECHO_SUPPRESSION_COUNT[name] = count + 1


def _release_parameter_echo(name: str) -> None:
    count = _ECHO_SUPPRESSION_COUNT[name] - 1
    if count == 0:
        del _ECHO_SUPPRESSION_COUNT[name]
        _restore_logger_state(name, _ECHO_SAVED_STATE.pop(name))
    else:
        _ECHO_SUPPRESSION_COUNT[name] = count


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

    @asynccontextmanager
    async def parameter_echo_suppressed(self) -> AsyncIterator[None]:
        """把参数回显型 logger 临时抬到 WARNING，覆盖一段含凭据绑定参数的写入。

        10.3 绝对禁止完整 Key 进日志：本应用 logger 从不打印 SQL/参数，唯一泄露面是
        aiosqlite 在 DEBUG 下把绑定参数写进 "executing %s"。故只抬级别（WARNING 以上
        照常输出，不丢生产诊断），并在 finally 无条件恢复进入前的原状态（含 NOTSET
        继承与 disabled）。

        退出前在锁内排一条无参数语句作栅栏：工作线程按队列顺序处理，而后继语句
        settle 即证明前一条的日志点已过（aiosqlite 先 set_result 再打 "completed"
        日志，故 await 返回不等于日志已产生）。栅栏的等待同样用 :func:`_await_settled`
        屏蔽取消——外层在这段挂起中被取消时不得提前恢复 logger，否则凭据语句的尾部
        DEBUG 日志正好落在已解压制的窗口里；恢复原状态后才重抛取消。

        并发边界（07 7.1 单连接 + 单锁）：锁串行化语句但不串行化压制窗口，故按 logger
        名引用计数（只在事件循环线程变更），先退出的窗口不会替仍开着的窗口恢复级别。
        代价：压制是进程级 logger 状态，同窗口内其他 aiosqlite 连接（如测试的第二个库）
        的 DEBUG 回显一并被压制——只丢诊断，不影响正确性。
        约束：必须包在 :meth:`transaction` / :meth:`under_lock` 外层（退出时要取一次
        锁），持锁区间内嵌套会因锁不可重入而死锁。
        """
        for name in _PARAMETER_ECHOING_LOGGERS:
            _suppress_parameter_echo(name)
        try:
            yield
        finally:
            # 先等栅栏 settle 再恢复级别（取消只记账），见上文取消保护。
            cancelled, drain_error = await _await_settled(
                self._drain_statement_logging()
            )
            for name in _PARAMETER_ECHOING_LOGGERS:
                _release_parameter_echo(name)
            # 状态恢复完成后才传播取消；取消优先，栅栏异常挂在 __cause__ 上不被吞。
            if cancelled:
                raise asyncio.CancelledError from drain_error
            if drain_error is not None:
                raise drain_error

    async def _drain_statement_logging(self) -> None:
        """排一条无参数语句，确认工作线程已越过前一条语句的日志点（栅栏）。

        连接已关闭时直接返回（没有后台线程会再产生日志）。调用方用屏蔽取消的独立
        Task 等待本方法，故含取锁在内的任何挂起点都不会被外层取消打断。
        """
        if not self.is_open:
            return

        async def op(conn: aiosqlite.Connection) -> None:
            await conn.execute("SELECT 1")

        await self.under_lock(op)

    async def pragma_value(self, name: str) -> str | int | None:
        """查询固定名单内的 PRAGMA 当前值（连接配置可查询验证，S0-02）。"""
        sql = _PRAGMA_QUERIES.get(name)
        if sql is None:
            raise ValueError(f"不允许查询 PRAGMA {name!r}")
        conn = self._require_open()
        async with self._lock, conn.execute(sql) as cursor:
            row = await cursor.fetchone()
        return None if row is None else row[0]
