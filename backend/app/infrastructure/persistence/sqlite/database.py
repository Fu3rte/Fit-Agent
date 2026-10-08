import asyncio
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TypeVar

import aiosqlite

from app.domain.business.errors import CommitVetoed

T = TypeVar("T")

# 提交否决判据：由连接线程在 COMMIT 执行期间同步调用，返回真值表示本次提交让位。
CommitVeto = Callable[[], bool]

SCHEMA_VERSION = 6

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_INITIAL_SCHEMA = _MIGRATIONS_DIR / "001_initial.sql"
_DELETE_SCHEMA = _MIGRATIONS_DIR / "002_delete_and_invalidation.sql"
_BUSINESS_SCHEMA = _MIGRATIONS_DIR / "003_business.sql"
_PROFILE_SNAPSHOT_SCHEMA = _MIGRATIONS_DIR / "004_profile_snapshot.sql"

# 按目标版本应用的迁移脚本：新库从头依次应用，旧库只应用缺失版本。
_MIGRATIONS: dict[int, Path] = {
    1: _INITIAL_SCHEMA,
    2: _DELETE_SCHEMA,
    3: _BUSINESS_SCHEMA,
    4: _PROFILE_SNAPSHOT_SCHEMA,
    5: _MIGRATIONS_DIR / "005_remove_confirmation_cards.sql",
    6: _MIGRATIONS_DIR / "006_workouts.sql",
}

# 当前版本的结构基线：表 -> 列集合。
_TABLE_COLUMNS: dict[str, frozenset[str]] = {
    "sessions": frozenset(
        {"id", "title", "active_leaf_id", "created_at", "updated_at"}
    ),
    "session_entries": frozenset(
        {"session_id", "id", "parent_id", "run_id", "type", "messages", "created_at"}
    ),
    "session_runs": frozenset(
        {
            "session_id",
            "id",
            "request_entry_id",
            "last_entry_id",
            "status",
            "started_at",
            "finished_at",
            "error_code",
            "error_message",
        }
    ),
    "steering_inputs": frozenset(
        {
            "session_id",
            "id",
            "run_id",
            "message",
            "status",
            "entry_id",
            "reason",
            "created_at",
            "updated_at",
        }
    ),
    "session_operations": frozenset(
        {
            "operation_id",
            "session_id",
            "kind",
            "request",
            "run_id",
            "steering_id",
            "created_at",
        }
    ),
    "session_operation_invalidations": frozenset({"operation_id", "session_id"}),
    "profile": frozenset({"id", "version", "content", "updated_at"}),
    "exercises": frozenset(
        {
            "id",
            "name",
            "body_part",
            "equipment",
            "target",
            "muscle_group",
            "secondary_muscles",
            "load_convention",
            "steps",
        }
    ),
    "profile_snapshots": frozenset(
        {
            "proposal_id",
            "session_id",
            "request_entry_id",
            "source_entry_id",
            "profile_id",
            "base_profile_version",
            "payload",
            "display_entry_id",
            "confirmation_entry_id",
            "status",
            "created_at",
        }
    ),
    "profile_save_records": frozenset(
        {
            "proposal_id",
            "session_id",
            "profile_id",
            "display_entry_id",
            "confirmation_entry_id",
            "result",
            "saved_at",
        }
    ),
    "workouts": frozenset({"id", "performed_on", "version", "content", "created_at", "updated_at"}),
    "workout_snapshots": frozenset({
        "proposal_id", "session_id", "request_entry_id", "source_entry_id", "performed_on",
        "base_workout_id", "base_workout_version", "payload", "display_entry_id",
        "confirmation_entry_id", "status", "created_at",
    }),
    "workout_save_records": frozenset({
        "proposal_id", "session_id", "display_entry_id", "confirmation_entry_id", "result", "saved_at",
    }),
}

# 当前版本中允许为 NULL 的列。
_NULLABLE_COLUMNS = frozenset({
    ("sessions", "active_leaf_id"),
    ("session_entries", "parent_id"),
    ("session_entries", "run_id"),
    ("session_runs", "last_entry_id"),
    ("session_runs", "finished_at"),
    ("session_runs", "error_code"),
    ("session_runs", "error_message"),
    ("steering_inputs", "entry_id"),
    ("steering_inputs", "reason"),
    ("session_operations", "steering_id"),
    ("exercises", "load_convention"),
    ("profile_snapshots", "base_profile_version"),
    ("profile_snapshots", "display_entry_id"),
    ("profile_snapshots", "confirmation_entry_id"),
    ("workout_snapshots", "base_workout_id"),
    ("workout_snapshots", "base_workout_version"),
    ("workout_snapshots", "display_entry_id"),
    ("workout_snapshots", "confirmation_entry_id"),
})

# 版本 1 的外键基线：表 -> {(被引用表, {(子列, 父列), ...})}。
_FOREIGN_KEYS: dict[str, set[tuple[str, frozenset[tuple[str, str]]]]] = {
    "sessions": {
        (
            "session_entries",
            frozenset({("id", "session_id"), ("active_leaf_id", "id")}),
        ),
    },
    "session_entries": {
        ("sessions", frozenset({("session_id", "id")})),
        (
            "session_entries",
            frozenset({("session_id", "session_id"), ("parent_id", "id")}),
        ),
        (
            "session_runs",
            frozenset({("session_id", "session_id"), ("run_id", "id")}),
        ),
    },
    "session_runs": {
        ("sessions", frozenset({("session_id", "id")})),
        (
            "session_entries",
            frozenset({("session_id", "session_id"), ("request_entry_id", "id")}),
        ),
        (
            "session_entries",
            frozenset({("session_id", "session_id"), ("last_entry_id", "id")}),
        ),
    },
    "steering_inputs": {
        ("sessions", frozenset({("session_id", "id")})),
        (
            "session_runs",
            frozenset({("session_id", "session_id"), ("run_id", "id")}),
        ),
        (
            "session_entries",
            frozenset({("session_id", "session_id"), ("entry_id", "id")}),
        ),
    },
    "session_operations": {
        ("sessions", frozenset({("session_id", "id")})),
        (
            "session_runs",
            frozenset({("session_id", "session_id"), ("run_id", "id")}),
        ),
        (
            "steering_inputs",
            frozenset({("session_id", "session_id"), ("steering_id", "id")}),
        ),
    },
    "session_operation_invalidations": {
        ("sessions", frozenset({("session_id", "id")})),
    },
    "profile": set(),
    "exercises": set(),
    "profile_snapshots": {
        ("sessions", frozenset({("session_id", "id")})),
        (
            "session_entries",
            frozenset({("session_id", "session_id"), ("request_entry_id", "id")}),
        ),
        (
            "session_entries",
            frozenset({("session_id", "session_id"), ("source_entry_id", "id")}),
        ),
        (
            "session_entries",
            frozenset({("session_id", "session_id"), ("display_entry_id", "id")}),
        ),
        (
            "session_entries",
            frozenset(
                {("session_id", "session_id"), ("confirmation_entry_id", "id")}
            ),
        ),
    },
    "profile_save_records": set(),
    "workouts": set(),
    "workout_snapshots": {
        ("sessions", frozenset({("session_id", "id")})),
        ("session_entries", frozenset({("session_id", "session_id"), ("request_entry_id", "id")})),
        ("session_entries", frozenset({("session_id", "session_id"), ("source_entry_id", "id")})),
        ("session_entries", frozenset({("session_id", "session_id"), ("display_entry_id", "id")})),
        ("session_entries", frozenset({("session_id", "session_id"), ("confirmation_entry_id", "id")})),
    },
    "workout_save_records": set(),
}

_INDEXES = frozenset({
    "idx_session_entries_parent",
    "idx_session_entries_run",
    "idx_steering_inputs_run",
    "idx_steering_inputs_entry",
    "idx_session_operations_session",
    "idx_profile_snapshots_pending",
    "idx_profile_snapshots_confirmation",
    "idx_profile_save_records_confirmation",
    "idx_workouts_performed_on",
    "idx_workout_snapshots_pending",
    "idx_workout_snapshots_confirmation",
    "idx_workout_save_records_confirmation",
})

_TRIGGERS = frozenset({
    "session_entries_parent_exists_insert",
    "session_runs_request_entry_user_insert",
    "session_runs_request_entry_user_update",
    "session_runs_last_entry_in_run_insert",
    "session_runs_last_entry_in_run_update",
    "steering_inputs_consumed_entry_insert",
    "steering_inputs_consumed_entry_update",
    "session_operations_steering_run_insert",
    "session_operations_steering_run_update",
    "session_entries_immutable_update",
    "profile_snapshots_request_entry_user_insert",
    "profile_snapshots_source_entry_assistant_insert",
    "profile_snapshots_display_entry_tool_result",
    "profile_snapshots_confirmation_entry_user",
    "profile_snapshots_immutable_update",
    "workout_snapshots_request_entry_user_insert",
    "workout_snapshots_source_entry_assistant_insert",
    "workout_snapshots_display_entry_tool_result_insert",
    "workout_snapshots_display_entry_tool_result",
    "workout_snapshots_confirmation_entry_user_insert",
    "workout_snapshots_confirmation_entry_user",
    "workout_snapshots_immutable_update",
    "workout_save_records_immutable_update",
})


def default_database_path() -> Path:
    return Path(__file__).resolve().parents[4] / "data" / "fit-agent.db"


class Database:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection
        self._lock = asyncio.Lock()
        self._veto: CommitVeto | None = None
        self._owner: ContextVar[asyncio.Task | None] = ContextVar(
            f"session_database_transaction_{id(self)}", default=None
        )

    @property
    def connection(self) -> aiosqlite.Connection:
        return self._connection

    def _owns_transaction(self) -> bool:
        # 只有持有事务的当前 Task 可以绕过协调锁；子任务继承的 ContextVar 不生效。
        current = asyncio.current_task()
        return current is not None and self._owner.get() is current

    async def read(
        self, operation: Callable[[aiosqlite.Connection], Awaitable[T]]
    ) -> T:
        if self._owns_transaction():
            return await operation(self._connection)
        async with self._lock:
            return await operation(self._connection)

    @asynccontextmanager
    async def transaction_scope(self, veto: CommitVeto | None = None) -> AsyncIterator[None]:
        if self._owns_transaction():
            yield
            return
        async with self._lock:
            token = self._owner.set(asyncio.current_task())
            try:
                await self._statement("BEGIN")
                yield
                await self._commit(veto)
            except BaseException:
                await self._rollback()
                raise
            finally:
                self._owner.reset(token)

    def _checking(self) -> int:
        # progress 回调运行在连接线程：只读取纯 Python 状态，不得进入事件循环。
        veto = self._veto
        return 1 if veto is not None and veto() else 0

    async def _commit(self, veto: CommitVeto | None) -> None:
        if veto is None:
            await self._statement("COMMIT")
            return
        # 意图登记与本次提交在 COMMIT 语句的实际执行瞬间由连接线程裁决：
        # 否决使 COMMIT 中断且事务保持未提交，落地后才登记的意图按提交在先处理。
        self._veto = veto
        try:
            await self._connection.set_progress_handler(self._checking, 1)
            try:
                await self._statement("COMMIT")
            except sqlite3.OperationalError as error:
                # 断开钩子必须先于 ROLLBACK 语句，否则回滚同样被打断。
                if self._connection.in_transaction:
                    raise CommitVetoed() from error
                raise
        finally:
            await self._connection.set_progress_handler(None, 0)
            self._veto = None

    async def _statement(self, sql: str) -> None:
        # 排队语句必须在连接线程落地后才传播取消；重复取消不得打断等待。
        pending = asyncio.ensure_future(self._connection.execute(sql))
        cancelled = False
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                cancelled = True
                task = asyncio.current_task()
                if task is not None:
                    task.uncancel()
            except BaseException:
                break
        failure: BaseException | None = None
        if pending.done() and not pending.cancelled():
            failure = pending.exception()
        if cancelled:
            raise asyncio.CancelledError()
        if failure is not None:
            raise failure

    async def _rollback(self) -> None:
        if self._connection.in_transaction:
            await self._statement("ROLLBACK")

    async def close(self) -> None:
        await self._connection.close()


async def open_database(path: Path | None = None) -> Database:
    resolved = Path(path) if path is not None else default_database_path()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    connection = await aiosqlite.connect(resolved, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        await _enable_foreign_keys(connection)
        version = await _read_user_version(connection)
        if version == SCHEMA_VERSION:
            await _verify_schema(connection)
        elif version == 0:
            existing = await _application_tables(connection)
            if existing:
                raise RuntimeError(
                    f"数据库 user_version 为 0 但已存在应用表：{existing}"
                )
            await _migrate(connection, 1, SCHEMA_VERSION)
        elif 0 < version < SCHEMA_VERSION:
            await _migrate(connection, version + 1, SCHEMA_VERSION)
        else:
            raise RuntimeError(f"不支持的数据库 schema 版本：{version}")
    except BaseException:
        await connection.close()
        raise
    return Database(connection)


async def _migrate(
    connection: aiosqlite.Connection, from_version: int, to_version: int
) -> None:
    for target in range(from_version, to_version + 1):
        script = _MIGRATIONS[target].read_text(encoding="utf-8")
        await apply_schema(connection, script, target)


async def apply_schema(
    connection: aiosqlite.Connection, script: str, version: int
) -> None:
    try:
        await connection.executescript(
            f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;"
        )
    except BaseException:
        if connection.in_transaction:
            await connection.execute("ROLLBACK")
        raise


async def _enable_foreign_keys(connection: aiosqlite.Connection) -> None:
    await connection.execute("PRAGMA foreign_keys = ON")
    row = await _fetch_one(connection, "PRAGMA foreign_keys")
    if row is None or row[0] != 1:
        raise RuntimeError("无法开启外键约束 PRAGMA foreign_keys = ON")


async def _read_user_version(connection: aiosqlite.Connection) -> int:
    row = await _fetch_one(connection, "PRAGMA user_version")
    if row is None:
        raise RuntimeError("无法读取 PRAGMA user_version")
    return int(row[0])


async def _application_tables(connection: aiosqlite.Connection) -> list[str]:
    rows = await _fetch_all(
        connection,
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
    )
    return [row[0] for row in rows]


async def _verify_schema(connection: aiosqlite.Connection) -> None:
    tables = await _application_tables(connection)
    if tables != sorted(_TABLE_COLUMNS):
        raise RuntimeError(f"版本 {SCHEMA_VERSION} 的表集合不兼容：{tables}")
    for table, columns in _TABLE_COLUMNS.items():
        if not await _table_is_strict(connection, table):
            raise RuntimeError(f"表 {table} 不是 STRICT 表")
        info = await _fetch_all(connection, f"PRAGMA table_info({table})")
        if {row[1] for row in info} != columns:
            raise RuntimeError(f"表 {table} 的列不兼容")
        nullable = {(table, row[1]) for row in info if not row[3]}
        if nullable != {column for column in _NULLABLE_COLUMNS if column[0] == table}:
            raise RuntimeError(f"表 {table} 的可空列不兼容")
        if await _foreign_keys(connection, table) != _FOREIGN_KEYS[table]:
            raise RuntimeError(f"表 {table} 的外键不兼容")
    indexes = await _object_names(connection, "index")
    if not _INDEXES <= indexes:
        raise RuntimeError(f"索引缺失：{sorted(_INDEXES - indexes)}")
    triggers = await _object_names(connection, "trigger")
    if not _TRIGGERS <= triggers:
        raise RuntimeError(f"触发器缺失：{sorted(_TRIGGERS - triggers)}")


async def _table_is_strict(connection: aiosqlite.Connection, table: str) -> bool:
    row = await _fetch_one(
        connection, "SELECT strict FROM pragma_table_list WHERE name = ?", (table,)
    )
    return row is not None and row[0] == 1


async def _foreign_keys(
    connection: aiosqlite.Connection, table: str
) -> set[tuple[str, frozenset[tuple[str, str]]]]:
    rows = await _fetch_all(connection, f"PRAGMA foreign_key_list({table})")
    grouped: dict[tuple[str, int], set[tuple[str, str]]] = {}
    for row in rows:
        grouped.setdefault((row[2], row[0]), set()).add((row[3], row[4]))
    return {(target, frozenset(pairs)) for (target, _id), pairs in grouped.items()}


async def _object_names(connection: aiosqlite.Connection, kind: str) -> set[str]:
    rows = await _fetch_all(
        connection,
        "SELECT name FROM sqlite_master WHERE type = ? ORDER BY name",
        (kind,),
    )
    return {row[0] for row in rows}


async def _fetch_one(
    connection: aiosqlite.Connection, sql: str, parameters: tuple = ()
) -> tuple | None:
    cursor = await connection.execute(sql, parameters)
    row = await cursor.fetchone()
    await cursor.close()
    return row


async def _fetch_all(
    connection: aiosqlite.Connection, sql: str, parameters: tuple = ()
) -> list[tuple]:
    cursor = await connection.execute(sql, parameters)
    rows = await cursor.fetchall()
    await cursor.close()
    return rows
