import json
import sqlite3
from contextlib import AbstractAsyncContextManager

import aiosqlite

from app.domain.business.models import (
    CatalogExercise,
    ProfileContent,
    ProfileProposalStatus,
    ProfileRecord,
    ProfileSaveRecord,
    ProfileSnapshot,
    WorkoutContent,
    WorkoutListArguments,
    WorkoutListResult,
    WorkoutProposalStatus,
    WorkoutRecord,
    WorkoutSaveRecord,
    WorkoutSnapshot,
)
from app.infrastructure.persistence.sqlite.database import CommitVeto, Database

_PROFILE_COLUMNS = "version, content, updated_at"
_SNAPSHOT_COLUMNS = (
    "proposal_id, session_id, request_entry_id, source_entry_id, profile_id, "
    "base_profile_version, payload, display_entry_id, confirmation_entry_id, status, "
    "created_at"
)
_SAVE_RECORD_COLUMNS = (
    "proposal_id, session_id, profile_id, display_entry_id, confirmation_entry_id, "
    "result, saved_at"
)


_WORKOUT_COLUMNS = "id, performed_on, version, content, created_at, updated_at"
_WORKOUT_SNAPSHOT_COLUMNS = (
    "proposal_id, session_id, request_entry_id, source_entry_id, performed_on, "
    "base_workout_id, base_workout_version, payload, display_entry_id, "
    "confirmation_entry_id, status, created_at"
)
_WORKOUT_SAVE_COLUMNS = (
    "proposal_id, session_id, display_entry_id, confirmation_entry_id, result, saved_at"
)


def _row_to_workout(row: sqlite3.Row) -> WorkoutRecord:
    data = dict(row)
    data["content"] = json.loads(data["content"])
    return WorkoutRecord.model_validate(data)


def _row_to_workout_snapshot(row: sqlite3.Row) -> WorkoutSnapshot:
    data = dict(row)
    data["payload"] = json.loads(data["payload"])
    return WorkoutSnapshot.model_validate(data)


def _row_to_workout_save_record(row: sqlite3.Row) -> WorkoutSaveRecord:
    data = dict(row)
    data["result"] = json.loads(data["result"])
    return WorkoutSaveRecord.model_validate(data)


def _placeholders(count: int) -> str:
    return ", ".join("?" for _ in range(count))


def _dump_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _row_to_profile(row: sqlite3.Row) -> ProfileRecord:
    return ProfileRecord(
        version=row["version"],
        content=ProfileContent.model_validate(json.loads(row["content"])),
        updated_at=row["updated_at"],
    )


def _row_to_snapshot(row: sqlite3.Row) -> ProfileSnapshot:
    data = dict(row)
    data["payload"] = json.loads(data["payload"])
    return ProfileSnapshot.model_validate(data)


def _row_to_save_record(row: sqlite3.Row) -> ProfileSaveRecord:
    data = dict(row)
    data["result"] = json.loads(data["result"])
    return ProfileSaveRecord.model_validate(data)


class SqliteBusinessRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def transaction(self, veto: CommitVeto | None = None) -> AbstractAsyncContextManager[None]:
        return self._database.transaction_scope(veto)

    async def _write(self, sql: str, parameters: tuple) -> int:
        async def run(connection: aiosqlite.Connection) -> int:
            cursor = await connection.execute(sql, parameters)
            count = cursor.rowcount
            await cursor.close()
            return count

        return await self._database.read(run)

    async def _fetch_one(
        self, sql: str, parameters: tuple
    ) -> sqlite3.Row | None:
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

    async def get_profile(self) -> ProfileRecord | None:
        row = await self._fetch_one(
            f"SELECT {_PROFILE_COLUMNS} FROM profile WHERE id = 1", ()
        )
        return None if row is None else _row_to_profile(row)

    async def save_profile(
        self, content: ProfileContent, version: int, updated_at: int
    ) -> None:
        await self._write(
            "INSERT INTO profile (id, version, content, updated_at) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET version = excluded.version, "
            "content = excluded.content, updated_at = excluded.updated_at",
            (version, _dump_json(content.model_dump()), updated_at),
        )

    async def insert_snapshot(self, snapshot: ProfileSnapshot) -> None:
        await self._write(
            "INSERT INTO profile_snapshots "
            "(proposal_id, session_id, request_entry_id, source_entry_id, profile_id, "
            "base_profile_version, payload, display_entry_id, confirmation_entry_id, "
            "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot.proposal_id,
                snapshot.session_id,
                snapshot.request_entry_id,
                snapshot.source_entry_id,
                snapshot.profile_id,
                snapshot.base_profile_version,
                _dump_json(snapshot.payload.model_dump()),
                snapshot.display_entry_id,
                snapshot.confirmation_entry_id,
                snapshot.status,
                snapshot.created_at,
            ),
        )

    async def get_snapshot(self, proposal_id: str) -> ProfileSnapshot | None:
        row = await self._fetch_one(
            f"SELECT {_SNAPSHOT_COLUMNS} FROM profile_snapshots WHERE proposal_id = ?",
            (proposal_id,),
        )
        return None if row is None else _row_to_snapshot(row)

    async def list_snapshots(self, session_id: str) -> list[ProfileSnapshot]:
        rows = await self._fetch_all(
            f"SELECT {_SNAPSHOT_COLUMNS} FROM profile_snapshots "
            "WHERE session_id = ? ORDER BY created_at, proposal_id",
            (session_id,),
        )
        return [_row_to_snapshot(row) for row in rows]

    async def find_snapshot_by_confirmation(
        self, session_id: str, confirmation_entry_id: str
    ) -> ProfileSnapshot | None:
        row = await self._fetch_one(
            f"SELECT {_SNAPSHOT_COLUMNS} FROM profile_snapshots "
            "WHERE session_id = ? AND confirmation_entry_id = ?",
            (session_id, confirmation_entry_id),
        )
        return None if row is None else _row_to_snapshot(row)

    async def bind_display_entry(
        self, proposal_id: str, display_entry_id: str
    ) -> None:
        await self._write(
            "UPDATE profile_snapshots SET display_entry_id = ? WHERE proposal_id = ?",
            (display_entry_id, proposal_id),
        )

    # 保存执行前的关联持久化：确认节点与 proposal_id 的绑定先于保存事务提交。
    async def begin_save(self, proposal_id: str, confirmation_entry_id: str) -> None:
        await self._write(
            "UPDATE profile_snapshots SET confirmation_entry_id = ?, "
            "status = 'processing' WHERE proposal_id = ?",
            (confirmation_entry_id, proposal_id),
        )

    async def set_snapshot_status(
        self, proposal_id: str, status: ProfileProposalStatus
    ) -> None:
        await self._write(
            "UPDATE profile_snapshots SET status = ? WHERE proposal_id = ?",
            (status, proposal_id),
        )

    # 同会话、同目标画像的旧待确认快照一次性失效，保持本次快照不变。
    async def invalidate_pending(
        self, session_id: str, profile_id: int, keep_proposal_id: str
    ) -> None:
        await self._write(
            "UPDATE profile_snapshots SET status = 'invalidated' "
            "WHERE session_id = ? AND profile_id = ? AND status = 'pending' "
            "AND proposal_id <> ?",
            (session_id, profile_id, keep_proposal_id),
        )

    # saved 状态与固定保存结果在同一调用内落地，原子性由所属事务保证。
    async def complete_save(self, record: ProfileSaveRecord) -> None:
        await self._write(
            "UPDATE profile_snapshots SET status = 'saved' WHERE proposal_id = ?",
            (record.proposal_id,),
        )
        await self._write(
            "INSERT INTO profile_save_records "
            "(proposal_id, session_id, profile_id, display_entry_id, "
            "confirmation_entry_id, result, saved_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                record.proposal_id,
                record.session_id,
                record.profile_id,
                record.display_entry_id,
                record.confirmation_entry_id,
                _dump_json(record.result.model_dump()),
                record.saved_at,
            ),
        )

    async def get_save_record(self, proposal_id: str) -> ProfileSaveRecord | None:
        row = await self._fetch_one(
            f"SELECT {_SAVE_RECORD_COLUMNS} FROM profile_save_records "
            "WHERE proposal_id = ?",
            (proposal_id,),
        )
        return None if row is None else _row_to_save_record(row)

    async def list_save_records(self, session_id: str) -> list[ProfileSaveRecord]:
        rows = await self._fetch_all(
            f"SELECT {_SAVE_RECORD_COLUMNS} FROM profile_save_records "
            "WHERE session_id = ? ORDER BY saved_at, proposal_id",
            (session_id,),
        )
        return [_row_to_save_record(row) for row in rows]

    async def find_save_record_by_confirmation(
        self, session_id: str, confirmation_entry_id: str
    ) -> ProfileSaveRecord | None:
        row = await self._fetch_one(
            f"SELECT {_SAVE_RECORD_COLUMNS} FROM profile_save_records "
            "WHERE session_id = ? AND confirmation_entry_id = ?",
            (session_id, confirmation_entry_id),
        )
        return None if row is None else _row_to_save_record(row)

    async def delete_snapshots_for_entries(
        self, session_id: str, entry_ids: set[str]
    ) -> None:
        if not entry_ids:
            return
        ids = tuple(entry_ids)
        marks = _placeholders(len(ids))
        await self._write(
            "DELETE FROM profile_snapshots WHERE session_id = ? AND ("
            f"request_entry_id IN ({marks}) OR source_entry_id IN ({marks}) "
            f"OR display_entry_id IN ({marks}) OR confirmation_entry_id IN ({marks})"
            ")",
            (session_id, *ids, *ids, *ids, *ids),
        )

    async def delete_snapshots_for_session(self, session_id: str) -> None:
        await self._write(
            "DELETE FROM profile_snapshots WHERE session_id = ?", (session_id,)
        )

    # 重启恢复：未提交的保存操作在数据库回滚后停留 processing，统一恢复为 pending。
    async def recover_interrupted_saves(self) -> int:
        return await self._write(
            "UPDATE profile_snapshots SET status = 'pending' "
            "WHERE status = 'processing'",
            (),
        )

    async def get_workout(self, workout_id: str) -> WorkoutRecord | None:
        row = await self._fetch_one(
            f"SELECT {_WORKOUT_COLUMNS} FROM workouts WHERE id = ?", (workout_id,)
        )
        return None if row is None else _row_to_workout(row)

    async def get_workout_by_date(self, performed_on: str) -> WorkoutRecord | None:
        row = await self._fetch_one(
            f"SELECT {_WORKOUT_COLUMNS} FROM workouts WHERE performed_on = ?", (performed_on,)
        )
        return None if row is None else _row_to_workout(row)

    async def list_workouts(self, arguments: WorkoutListArguments) -> WorkoutListResult:
        clauses: list[str] = []
        parameters: list[object] = []
        for column, operator, value in (
            ("performed_on", ">=", arguments.date_from),
            ("performed_on", "<=", arguments.date_to),
        ):
            if value is not None:
                clauses.append(f"{column} {operator} ?")
                parameters.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        # total 与分页内容使用同一事务快照。
        async with self.transaction():
            count = await self._fetch_one("SELECT COUNT(*) FROM workouts" + where, tuple(parameters))
            assert count is not None
            total = int(count[0])
            offset = (arguments.page - 1) * arguments.page_size
            if offset >= total:
                return WorkoutListResult(
                    items=[], page=arguments.page, page_size=arguments.page_size, total=total,
                )
            rows = await self._fetch_all(
                f"SELECT {_WORKOUT_COLUMNS} FROM workouts" + where
                + " ORDER BY performed_on DESC, id DESC LIMIT ? OFFSET ?",
                (*parameters, arguments.page_size, offset),
            )
            return WorkoutListResult(
                items=[_row_to_workout(row) for row in rows], page=arguments.page,
                page_size=arguments.page_size, total=total,
            )

    async def insert_workout(self, record: WorkoutRecord) -> None:
        if record.version != 1 or record.created_at != record.updated_at:
            raise ValueError("首次训练记录版本必须为 1，创建与更新时间必须一致。")
        await self._write(
            f"INSERT INTO workouts ({_WORKOUT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
            (record.id, record.performed_on, record.version,
             _dump_json(record.content.model_dump()), record.created_at, record.updated_at),
        )

    async def update_workout(
        self, workout_id: str, base_version: int, content: WorkoutContent, updated_at: int
    ) -> bool:
        count = await self._write(
            "UPDATE workouts SET content = ?, version = version + 1, updated_at = ? "
            "WHERE id = ? AND version = ?",
            (_dump_json(content.model_dump()), updated_at, workout_id, base_version),
        )
        return count == 1

    async def insert_workout_snapshot(self, snapshot: WorkoutSnapshot) -> None:
        await self._write(
            f"INSERT INTO workout_snapshots ({_WORKOUT_SNAPSHOT_COLUMNS}) "
            f"VALUES ({_placeholders(12)})",
            (snapshot.proposal_id, snapshot.session_id, snapshot.request_entry_id,
             snapshot.source_entry_id, snapshot.performed_on, snapshot.base_workout_id,
             snapshot.base_workout_version, _dump_json(snapshot.payload.model_dump()),
             snapshot.display_entry_id, snapshot.confirmation_entry_id,
             snapshot.status, snapshot.created_at),
        )

    async def get_workout_snapshot(self, proposal_id: str) -> WorkoutSnapshot | None:
        row = await self._fetch_one(
            f"SELECT {_WORKOUT_SNAPSHOT_COLUMNS} FROM workout_snapshots WHERE proposal_id = ?",
            (proposal_id,),
        )
        return None if row is None else _row_to_workout_snapshot(row)

    async def list_workout_snapshots(self, session_id: str) -> list[WorkoutSnapshot]:
        rows = await self._fetch_all(
            f"SELECT {_WORKOUT_SNAPSHOT_COLUMNS} FROM workout_snapshots "
            "WHERE session_id = ? ORDER BY created_at, proposal_id", (session_id,),
        )
        return [_row_to_workout_snapshot(row) for row in rows]

    async def find_workout_snapshot_by_confirmation(
        self, session_id: str, confirmation_entry_id: str
    ) -> WorkoutSnapshot | None:
        row = await self._fetch_one(
            f"SELECT {_WORKOUT_SNAPSHOT_COLUMNS} FROM workout_snapshots "
            "WHERE session_id = ? AND confirmation_entry_id = ?",
            (session_id, confirmation_entry_id),
        )
        return None if row is None else _row_to_workout_snapshot(row)

    async def bind_workout_display_entry(self, proposal_id: str, display_entry_id: str) -> None:
        await self._write(
            "UPDATE workout_snapshots SET display_entry_id = ? WHERE proposal_id = ?",
            (display_entry_id, proposal_id),
        )

    async def begin_workout_save(self, proposal_id: str, confirmation_entry_id: str) -> None:
        await self._write(
            "UPDATE workout_snapshots SET confirmation_entry_id = ?, status = 'processing' "
            "WHERE proposal_id = ?", (confirmation_entry_id, proposal_id),
        )

    async def set_workout_snapshot_status(self, proposal_id: str, status: WorkoutProposalStatus) -> None:
        await self._write(
            "UPDATE workout_snapshots SET status = ? WHERE proposal_id = ?", (status, proposal_id),
        )

    async def invalidate_pending_workouts(
        self, session_id: str, performed_on: str, keep_proposal_id: str
    ) -> None:
        await self._write(
            "UPDATE workout_snapshots SET status = 'invalidated' "
            "WHERE session_id = ? AND performed_on = ? AND status = 'pending' AND proposal_id <> ?",
            (session_id, performed_on, keep_proposal_id),
        )

    # 调用方事务保证记录写入、状态与固定结果原子提交。
    async def complete_workout_save(self, record: WorkoutSaveRecord) -> None:
        await self._write(
            "UPDATE workout_snapshots SET status = 'saved' WHERE proposal_id = ?", (record.proposal_id,),
        )
        await self._write(
            f"INSERT INTO workout_save_records ({_WORKOUT_SAVE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
            (record.proposal_id, record.session_id, record.display_entry_id,
             record.confirmation_entry_id, _dump_json(record.result.model_dump()), record.saved_at),
        )

    async def get_workout_save_record(self, proposal_id: str) -> WorkoutSaveRecord | None:
        row = await self._fetch_one(
            f"SELECT {_WORKOUT_SAVE_COLUMNS} FROM workout_save_records WHERE proposal_id = ?", (proposal_id,),
        )
        return None if row is None else _row_to_workout_save_record(row)

    async def list_workout_save_records(self, session_id: str) -> list[WorkoutSaveRecord]:
        rows = await self._fetch_all(
            f"SELECT {_WORKOUT_SAVE_COLUMNS} FROM workout_save_records "
            "WHERE session_id = ? ORDER BY saved_at, proposal_id", (session_id,),
        )
        return [_row_to_workout_save_record(row) for row in rows]

    async def find_workout_save_record_by_confirmation(
        self, session_id: str, confirmation_entry_id: str
    ) -> WorkoutSaveRecord | None:
        row = await self._fetch_one(
            f"SELECT {_WORKOUT_SAVE_COLUMNS} FROM workout_save_records "
            "WHERE session_id = ? AND confirmation_entry_id = ?", (session_id, confirmation_entry_id),
        )
        return None if row is None else _row_to_workout_save_record(row)

    async def delete_workout_snapshots_for_entries(self, session_id: str, entry_ids: set[str]) -> None:
        if not entry_ids:
            return
        ids = tuple(entry_ids)
        marks = _placeholders(len(ids))
        await self._write(
            "DELETE FROM workout_snapshots WHERE session_id = ? AND ("
            f"request_entry_id IN ({marks}) OR source_entry_id IN ({marks}) "
            f"OR display_entry_id IN ({marks}) OR confirmation_entry_id IN ({marks}))",
            (session_id, *ids, *ids, *ids, *ids),
        )

    async def delete_workout_snapshots_for_session(self, session_id: str) -> None:
        await self._write("DELETE FROM workout_snapshots WHERE session_id = ?", (session_id,))

    async def recover_interrupted_workout_saves(self) -> int:
        return await self._write(
            "UPDATE workout_snapshots SET status = 'pending' WHERE status = 'processing'", (),
        )

    async def replace_exercises(self, exercises: list[CatalogExercise]) -> None:
        await self._write("DELETE FROM exercises", ())
        for exercise in exercises:
            await self._write(
                "INSERT INTO exercises "
                "(id, name, body_part, equipment, target, muscle_group, "
                "secondary_muscles, load_convention, steps) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    exercise.id,
                    exercise.name,
                    exercise.body_part,
                    exercise.equipment,
                    exercise.target,
                    exercise.muscle_group,
                    _dump_json(exercise.secondary_muscles),
                    exercise.load_convention,
                    _dump_json(exercise.steps.model_dump()),
                ),
            )

    async def count_exercises(self) -> int:
        row = await self._fetch_one("SELECT COUNT(*) FROM exercises", ())
        assert row is not None
        return int(row[0])
