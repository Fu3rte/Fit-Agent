import json
import sqlite3
from contextlib import AbstractAsyncContextManager

import aiosqlite

from app.domain.session.models import (
    Session,
    SessionMessageEntry,
    SessionOperation,
    SessionRun,
    SteeringInput,
)
from app.infrastructure.persistence.sqlite.database import Database

_ENTRY_COLUMNS = "session_id, id, parent_id, run_id, type, messages, created_at"
_RUN_COLUMNS = (
    "session_id, id, request_entry_id, last_entry_id, status, started_at, "
    "finished_at, error_code, error_message"
)
_STEERING_COLUMNS = (
    "session_id, id, run_id, message, status, entry_id, reason, created_at, "
    "updated_at"
)
_OPERATION_COLUMNS = (
    "operation_id, session_id, kind, request, run_id, steering_id, created_at"
)
_SESSION_COLUMNS = "id, title, active_leaf_id, created_at, updated_at"
# 会话删除时清除聊天关联表；画像快照由业务存储在同一事务内处理，已完成保存幂等记录保留。
_SESSION_CHILD_TABLES = (
    "steering_inputs",
    "session_operations",
    "session_operation_invalidations",
    "session_runs",
    "session_entries",
)


def _placeholders(count: int) -> str:
    return ", ".join("?" for _ in range(count))


def _dump_messages(entry: SessionMessageEntry) -> str:
    return json.dumps(
        [message.model_dump(exclude_unset=True) for message in entry.messages],
        ensure_ascii=False,
        allow_nan=False,
    )


def _row_to_session(row: sqlite3.Row) -> Session:
    return Session.model_validate(dict(row))


def _row_to_entry(row: sqlite3.Row) -> SessionMessageEntry:
    data = dict(row)
    data["messages"] = json.loads(data["messages"])
    return SessionMessageEntry.model_validate(data)


def _row_to_run(row: sqlite3.Row) -> SessionRun:
    return SessionRun.model_validate(dict(row))


def _row_to_steering(row: sqlite3.Row) -> SteeringInput:
    data = dict(row)
    data["message"] = json.loads(data["message"])
    return SteeringInput.model_validate(data)


def _row_to_operation(row: sqlite3.Row) -> SessionOperation:
    data = dict(row)
    data["request"] = json.loads(data["request"])
    return SessionOperation.model_validate(data)


class SqliteSessionRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def transaction(self) -> AbstractAsyncContextManager[None]:
        return self._database.transaction_scope()

    async def defer_foreign_keys(self) -> None:
        # 在受理事务内先删除再补齐，外键检查延迟到 COMMIT，事务结束自动关闭。
        await self._write("PRAGMA defer_foreign_keys = ON", ())

    async def _write(self, sql: str, parameters: tuple) -> None:
        async def run(connection: aiosqlite.Connection) -> None:
            cursor = await connection.execute(sql, parameters)
            await cursor.close()

        await self._database.read(run)

    async def _fetch_one(self, sql: str, parameters: tuple) -> sqlite3.Row | None:
        async def run(connection: aiosqlite.Connection) -> sqlite3.Row | None:
            cursor = await connection.execute(sql, parameters)
            row = await cursor.fetchone()
            await cursor.close()
            return row

        return await self._database.read(run)

    async def _fetch_all(self, sql: str, parameters: tuple) -> list[sqlite3.Row]:
        async def run(connection: aiosqlite.Connection) -> list[sqlite3.Row]:
            cursor = await connection.execute(sql, parameters)
            rows = await cursor.fetchall()
            await cursor.close()
            return rows

        return await self._database.read(run)

    async def insert_session(self, session: Session) -> None:
        await self._write(
            "INSERT INTO sessions (id, title, active_leaf_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                session.id,
                session.title,
                session.active_leaf_id,
                session.created_at,
                session.updated_at,
            ),
        )

    async def update_session(self, session: Session) -> None:
        await self._write(
            "UPDATE sessions SET title = ?, active_leaf_id = ?, updated_at = ? "
            "WHERE id = ?",
            (
                session.title,
                session.active_leaf_id,
                session.updated_at,
                session.id,
            ),
        )

    async def get_session(self, session_id: str) -> Session | None:
        row = await self._fetch_one(
            f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE id = ?", (session_id,)
        )
        return None if row is None else _row_to_session(row)

    async def list_sessions(self) -> list[Session]:
        rows = await self._fetch_all(
            f"SELECT {_SESSION_COLUMNS} FROM sessions ORDER BY created_at, id", ()
        )
        return [_row_to_session(row) for row in rows]

    async def delete_session(self, session_id: str) -> None:
        for table in _SESSION_CHILD_TABLES:
            await self._write(
                f"DELETE FROM {table} WHERE session_id = ?", (session_id,)
            )
        await self._write("DELETE FROM sessions WHERE id = ?", (session_id,))

    async def insert_entry(self, entry: SessionMessageEntry) -> None:
        await self._write(
            "INSERT INTO session_entries "
            "(session_id, id, parent_id, run_id, type, messages, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                entry.session_id,
                entry.id,
                entry.parent_id,
                entry.run_id,
                entry.type,
                _dump_messages(entry),
                entry.created_at,
            ),
        )

    async def _delete_rows(self, session_id: str, table: str, ids: list[str]) -> None:
        if not ids:
            return
        await self._write(
            f"DELETE FROM {table} WHERE session_id = ? "
            f"AND id IN ({_placeholders(len(ids))})",
            (session_id, *ids),
        )

    async def delete_entries(self, session_id: str, entry_ids: list[str]) -> None:
        await self._delete_rows(session_id, "session_entries", entry_ids)

    async def get_entry(
        self, session_id: str, entry_id: str
    ) -> SessionMessageEntry | None:
        row = await self._fetch_one(
            f"SELECT {_ENTRY_COLUMNS} FROM session_entries "
            "WHERE session_id = ? AND id = ?",
            (session_id, entry_id),
        )
        return None if row is None else _row_to_entry(row)

    async def list_entries(self, session_id: str) -> list[SessionMessageEntry]:
        rows = await self._fetch_all(
            f"SELECT {_ENTRY_COLUMNS} FROM session_entries WHERE session_id = ? "
            "ORDER BY created_at, id",
            (session_id,),
        )
        return [_row_to_entry(row) for row in rows]

    async def insert_run(self, run: SessionRun) -> None:
        await self._write(
            "INSERT INTO session_runs "
            "(session_id, id, request_entry_id, last_entry_id, status, started_at, "
            "finished_at, error_code, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run.session_id,
                run.id,
                run.request_entry_id,
                run.last_entry_id,
                run.status,
                run.started_at,
                run.finished_at,
                run.error_code,
                run.error_message,
            ),
        )

    async def delete_runs(self, session_id: str, run_ids: list[str]) -> None:
        await self._delete_rows(session_id, "session_runs", run_ids)

    async def update_run(self, run: SessionRun) -> None:
        await self._write(
            "UPDATE session_runs SET request_entry_id = ?, last_entry_id = ?, "
            "status = ?, started_at = ?, finished_at = ?, error_code = ?, "
            "error_message = ? WHERE session_id = ? AND id = ?",
            (
                run.request_entry_id,
                run.last_entry_id,
                run.status,
                run.started_at,
                run.finished_at,
                run.error_code,
                run.error_message,
                run.session_id,
                run.id,
            ),
        )

    async def get_run(self, session_id: str, run_id: str) -> SessionRun | None:
        row = await self._fetch_one(
            f"SELECT {_RUN_COLUMNS} FROM session_runs "
            "WHERE session_id = ? AND id = ?",
            (session_id, run_id),
        )
        return None if row is None else _row_to_run(row)

    async def get_run_by_id(self, run_id: str) -> SessionRun | None:
        row = await self._fetch_one(
            f"SELECT {_RUN_COLUMNS} FROM session_runs WHERE id = ?", (run_id,)
        )
        return None if row is None else _row_to_run(row)

    async def list_runs(self, session_id: str) -> list[SessionRun]:
        rows = await self._fetch_all(
            f"SELECT {_RUN_COLUMNS} FROM session_runs WHERE session_id = ? "
            "ORDER BY started_at, id",
            (session_id,),
        )
        return [_row_to_run(row) for row in rows]

    async def find_running_run(self) -> SessionRun | None:
        row = await self._fetch_one(
            f"SELECT {_RUN_COLUMNS} FROM session_runs WHERE status = 'running' "
            "ORDER BY started_at, id LIMIT 1",
            (),
        )
        return None if row is None else _row_to_run(row)

    async def list_running_runs(self) -> list[SessionRun]:
        rows = await self._fetch_all(
            f"SELECT {_RUN_COLUMNS} FROM session_runs WHERE status = 'running' "
            "ORDER BY started_at, id",
            (),
        )
        return [_row_to_run(row) for row in rows]

    async def insert_steering(self, steering: SteeringInput) -> None:
        await self._write(
            "INSERT INTO steering_inputs "
            "(session_id, id, run_id, message, status, entry_id, reason, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                steering.session_id,
                steering.id,
                steering.run_id,
                json.dumps(
                    steering.message.model_dump(exclude_unset=True),
                    ensure_ascii=False,
                    allow_nan=False,
                ),
                steering.status,
                steering.entry_id,
                steering.reason,
                steering.created_at,
                steering.updated_at,
            ),
        )

    async def delete_steering(self, session_id: str, steering_ids: list[str]) -> None:
        await self._delete_rows(session_id, "steering_inputs", steering_ids)

    async def update_steering(self, steering: SteeringInput) -> None:
        await self._write(
            "UPDATE steering_inputs SET status = ?, entry_id = ?, reason = ?, "
            "updated_at = ? WHERE session_id = ? AND id = ?",
            (
                steering.status,
                steering.entry_id,
                steering.reason,
                steering.updated_at,
                steering.session_id,
                steering.id,
            ),
        )

    async def get_steering(
        self, session_id: str, steering_id: str
    ) -> SteeringInput | None:
        row = await self._fetch_one(
            f"SELECT {_STEERING_COLUMNS} FROM steering_inputs "
            "WHERE session_id = ? AND id = ?",
            (session_id, steering_id),
        )
        return None if row is None else _row_to_steering(row)

    async def list_steering(
        self, session_id: str, run_id: str
    ) -> list[SteeringInput]:
        rows = await self._fetch_all(
            f"SELECT {_STEERING_COLUMNS} FROM steering_inputs "
            "WHERE session_id = ? AND run_id = ? ORDER BY created_at, id",
            (session_id, run_id),
        )
        return [_row_to_steering(row) for row in rows]

    async def list_pending_steering(
        self, session_id: str, run_id: str
    ) -> list[SteeringInput]:
        rows = await self._fetch_all(
            f"SELECT {_STEERING_COLUMNS} FROM steering_inputs "
            "WHERE session_id = ? AND run_id = ? AND status = 'pending' "
            "ORDER BY created_at, id",
            (session_id, run_id),
        )
        return [_row_to_steering(row) for row in rows]

    async def insert_operation(self, operation: SessionOperation) -> None:
        await self._write(
            "INSERT INTO session_operations "
            "(operation_id, session_id, kind, request, run_id, steering_id, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                operation.operation_id,
                operation.session_id,
                operation.kind,
                json.dumps(
                    operation.request, ensure_ascii=False, allow_nan=False
                ),
                operation.run_id,
                operation.steering_id,
                operation.created_at,
            ),
        )

    async def delete_operations(self, operation_ids: list[str]) -> None:
        if not operation_ids:
            return
        await self._write(
            f"DELETE FROM session_operations "
            f"WHERE operation_id IN ({_placeholders(len(operation_ids))})",
            tuple(operation_ids),
        )

    async def list_operations(self, session_id: str) -> list[SessionOperation]:
        rows = await self._fetch_all(
            f"SELECT {_OPERATION_COLUMNS} FROM session_operations "
            "WHERE session_id = ? ORDER BY created_at, operation_id",
            (session_id,),
        )
        return [_row_to_operation(row) for row in rows]

    async def get_operation(self, operation_id: str) -> SessionOperation | None:
        row = await self._fetch_one(
            f"SELECT {_OPERATION_COLUMNS} FROM session_operations "
            "WHERE operation_id = ?",
            (operation_id,),
        )
        return None if row is None else _row_to_operation(row)

    async def insert_operation_invalidation(
        self, operation_id: str, session_id: str
    ) -> None:
        await self._write(
            "INSERT INTO session_operation_invalidations (operation_id, session_id) "
            "VALUES (?, ?)",
            (operation_id, session_id),
        )

    async def get_operation_invalidation(self, operation_id: str) -> str | None:
        row = await self._fetch_one(
            "SELECT session_id FROM session_operation_invalidations "
            "WHERE operation_id = ?",
            (operation_id,),
        )
        return None if row is None else row[0]
