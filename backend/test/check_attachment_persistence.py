import asyncio
import base64
import json
import sqlite3
from functools import partial
from pathlib import Path
from uuid import uuid4

import aiosqlite
import pytest
from pydantic import ValidationError

from app.ai.messages import UserMessage
from app.application.session.attachment_files import AttachmentFiles
from app.application.session.service import _reject_credentials
from app.domain.session.attachments import (
    AttachmentError,
    AttachmentMetadata,
    attachment_storage_ref,
)
from app.domain.session.models import (
    Session,
    SessionMessageEntry,
    SessionRun,
    SteeringInput,
)
from app.infrastructure.persistence.sqlite.database import (
    _MIGRATIONS,
    SCHEMA_VERSION,
    Database,
    apply_schema,
    open_database,
)
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from test import regression_support

ROOT = Path(__file__).resolve().parents[2] / "tmp/backend-plan-import/checks" / uuid4().hex
ROOT.mkdir(parents=True)
regression_support.TEMP_ROOT = ROOT


def identifier():
    return str(uuid4())


async def rows(db, sql, parameters=()):
    async def read(connection):
        cursor = await connection.execute(sql, parameters)
        result = [tuple(row) for row in await cursor.fetchall()]
        await cursor.close()
        return result
    return await db.read(read)


async def execute(db, sql, parameters=()):
    async def write(connection):
        cursor = await connection.execute(sql, parameters)
        await cursor.close()
    await db.read(write)


def metadata(session, *, file_name="计划.MD", attachment_id=None):
    attachment_id = identifier() if attachment_id is None else attachment_id
    return AttachmentMetadata(attachment_id=attachment_id, session_id=session, file_name=file_name,
        size_bytes=10, storage_ref=attachment_storage_ref(session, attachment_id, file_name), created_at=1000)


async def seed(repo):
    session, entry, run, steering = [identifier() for _ in range(4)]
    user = UserMessage(role="user", content="保存原始输入", timestamp=1000)
    async with repo.transaction():
        await repo.insert_session(Session(id=session, title="附件持久化", active_leaf_id=None,
            created_at=1000, updated_at=1000))
        await repo.insert_entry(SessionMessageEntry(session_id=session, id=entry, parent_id=None,
            run_id=None, type="message", messages=[user], created_at=1000))
        await repo.insert_run(SessionRun(session_id=session, id=run, request_entry_id=entry,
            last_entry_id=None, status="running", started_at=1000, finished_at=None,
            error_code=None, error_message=None))
        await repo.insert_steering(SteeringInput(session_id=session, id=steering, run_id=run,
            message=user, status="pending", entry_id=None, reason=None, created_at=1000, updated_at=1000))
    return session, entry, run, steering


async def check_repository():
    path = ROOT / "repository.db"
    db = await open_database(path)
    repo = SqliteSessionRepository(db)
    try:
        session, entry, run, steering = await seed(repo)
        other, other_entry, _, other_steering = await seed(repo)
        first, second, foreign = metadata(session), metadata(session, file_name="计划.txt"), metadata(other)
        async with repo.transaction():
            for item in (first, second, foreign):
                await repo.insert_attachment(item)
            assert await repo.get_attachments([first.attachment_id, foreign.attachment_id, identifier()]) == {
                first.attachment_id: first, foreign.attachment_id: foreign}
            await repo.bind_entry_attachments(session, entry, [second.attachment_id, first.attachment_id])
            await repo.bind_steering_attachments(session, steering, [first.attachment_id, second.attachment_id])
        assert await repo.get_attachments([]) == {}
        assert await repo.list_entry_attachments(session, entry) == [second, first]
        assert await repo.list_steering_attachments(session, steering) == [first, second]
        for column, value in (("attachment_id", identifier()), ("session_id", other), ("file_name", "new.txt"),
                              ("size_bytes", 0), ("storage_ref", "D:/host.txt"), ("created_at", 2000)):
            with pytest.raises(sqlite3.IntegrityError):
                await execute(db, f"UPDATE session_attachments SET {column}=? WHERE attachment_id=?",
                              (value, first.attachment_id))
        with pytest.raises(sqlite3.IntegrityError):
            await repo.insert_attachment(metadata(other, file_name="different.txt", attachment_id=first.attachment_id))
        with pytest.raises(sqlite3.IntegrityError):
            await repo.insert_attachment(metadata(identifier()))
        for size in (-1, 100001):
            item = metadata(session)
            with pytest.raises(sqlite3.IntegrityError):
                await execute(db, "INSERT INTO session_attachments VALUES (?, ?, ?, ?, ?, ?)",
                    (item.attachment_id, session, item.file_name, size, item.storage_ref, item.created_at))
        with pytest.raises(ValueError):
            await repo.insert_attachment(first.model_copy(update={"storage_ref": "D:/host.txt"}))
        with pytest.raises(ValidationError):
            await repo.insert_attachment(first.model_copy(update={"size_bytes": True}))
        for table, parent_column, parent in (("session_entry_attachments", "entry_id", entry),
                                             ("steering_input_attachments", "steering_id", steering)):
            for attachment, position in ((first.attachment_id, 2), (foreign.attachment_id, 0),
                                         (foreign.attachment_id, -1), (identifier(), 2)):
                with pytest.raises(sqlite3.IntegrityError):
                    await execute(db, f"INSERT INTO {table} VALUES (?, ?, ?, ?)",
                                  (session, parent, attachment, position))
            third = metadata(session)
            await repo.insert_attachment(third)
            for position in (0, -1):
                with pytest.raises(sqlite3.IntegrityError):
                    await execute(db, f"INSERT INTO {table} VALUES (?, ?, ?, ?)",
                                  (session, parent, third.attachment_id, position))
            for missing_parent in (identifier(), other_entry if parent_column == "entry_id" else other_steering):
                with pytest.raises(sqlite3.IntegrityError):
                    await execute(db, f"INSERT INTO {table} VALUES (?, ?, ?, 0)",
                                  (session, missing_parent, third.attachment_id))
            with pytest.raises(sqlite3.IntegrityError):
                await execute(db, f"UPDATE {table} SET attachment_id=? WHERE session_id=? AND {parent_column}=? AND position=0",
                              (foreign.attachment_id, session, parent))
        # 角色守卫覆盖真实助手、工具与系统消息。
        from test.check_session_database import PARSED
        for role in ("assistant", "toolResult", "system"):
            role_entry = identifier()
            await repo.insert_entry(SessionMessageEntry(session_id=session, id=role_entry, parent_id=None,
                run_id=None, type="message", messages=[PARSED[role]], created_at=1000))
            with pytest.raises(sqlite3.IntegrityError):
                await repo.bind_entry_attachments(session, role_entry, [first.attachment_id])
            with pytest.raises(sqlite3.IntegrityError):
                await execute(db, "UPDATE session_entry_attachments SET entry_id=? WHERE session_id=? AND entry_id=?",
                              (role_entry, session, entry))
        new_entry = identifier()
        await repo.insert_entry(SessionMessageEntry(session_id=session, id=new_entry, parent_id=None,
            run_id=run, type="message", messages=[UserMessage(role="user", content="引用原文", timestamp=1000)], created_at=1000))
        await repo.bind_entry_attachments(session, new_entry, [first.attachment_id, second.attachment_id])
        consumed = (await repo.get_steering(session, steering)).model_copy(update={
            "status": "consumed", "entry_id": new_entry, "updated_at": 1001})
        await repo.update_steering(consumed)
        assert await repo.list_entry_attachments(session, new_entry) == await repo.list_steering_attachments(session, steering)
        retained_steering = []
        for status, reason in (("withdrawn", None), ("discarded", "interrupted")):
            extra = identifier()
            retained_steering.append(extra)
            await repo.insert_steering(consumed.model_copy(update={"id": extra, "status": "pending", "entry_id": None}))
            await repo.bind_steering_attachments(session, extra, [first.attachment_id])
            await repo.update_steering(consumed.model_copy(update={"id": extra, "status": status,
                "entry_id": None, "reason": reason}))
            assert await repo.list_steering_attachments(session, extra) == [first]
        rolled_back = metadata(session)
        with pytest.raises(RuntimeError, match="回滚"):
            async with repo.transaction():
                await repo.insert_attachment(rolled_back)
                await repo.bind_entry_attachments(session, entry, [])
                await repo.delete_steering(session, [steering])
                raise RuntimeError("回滚")
        assert await repo.get_attachments([rolled_back.attachment_id]) == {}
        assert await repo.list_steering_attachments(session, steering) == [first, second]
        duplicate_entry = identifier()
        await repo.insert_entry(SessionMessageEntry(session_id=session, id=duplicate_entry, parent_id=None,
            run_id=None, type="message", messages=[consumed.message], created_at=1000))
        with pytest.raises(sqlite3.IntegrityError):
            await repo.bind_entry_attachments(session, duplicate_entry, [first.attachment_id, first.attachment_id])
        assert await repo.list_entry_attachments(session, duplicate_entry) == []
        duplicate_steering = identifier()
        await repo.insert_steering(consumed.model_copy(update={"id": duplicate_steering,
            "status": "pending", "entry_id": None}))
        with pytest.raises(sqlite3.IntegrityError):
            await repo.bind_steering_attachments(session, duplicate_steering,
                [first.attachment_id, first.attachment_id])
        assert await repo.list_steering_attachments(session, duplicate_steering) == []
        retained_steering.append(duplicate_steering)
        await db.close()
        db = await open_database(path)
        repo = SqliteSessionRepository(db)
        assert await repo.list_entry_attachments(session, entry) == [second, first]
        async with repo.transaction():
            await repo.defer_foreign_keys()
            await repo.delete_entries(session, [entry])
            await repo.delete_steering(session, [steering, *retained_steering])
            await repo.delete_runs(session, [run])
            await repo.delete_entries(session, [new_entry])
        assert await repo.list_entry_attachments(session, entry) == []
        assert await repo.list_steering_attachments(session, steering) == []
        assert await repo.get_attachments([first.attachment_id]) == {first.attachment_id: first}
        await repo.bind_entry_attachments(session, duplicate_entry, [first.attachment_id])
        async with repo.transaction():
            await repo.defer_foreign_keys()
            await repo.delete_session(session)
        assert await repo.get_attachments([first.attachment_id, second.attachment_id]) == {}
        assert await repo.get_attachments([foreign.attachment_id]) == {foreign.attachment_id: foreign}
        assert await repo.get_session(other) is not None
        assert await rows(db, "PRAGMA foreign_key_check") == []
        assert await rows(db, "PRAGMA integrity_check") == [("ok",)]
    finally:
        await db.close()


async def check_migration_and_files():
    from app.domain.business.models import (
        PlanContent,
        PlanRecord,
        PlanSaveRecord,
        PlanSaveResult,
        PlanSnapshot,
        ProfileContent,
    )
    from app.infrastructure.persistence.sqlite.business_repository import (
        SqliteBusinessRepository,
    )
    from test.check_plan_core import CONTENT
    from test.check_profile_snapshot_store import FULL_PROFILE
    from test.check_workout_storage import make_record
    from test.check_workout_storage import seed as business_seed

    path = ROOT / "schema8.db"
    connection = await aiosqlite.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    await connection.execute("PRAGMA foreign_keys=ON")
    for version in range(1, 9):
        await apply_schema(connection, _MIGRATIONS[version].read_text(encoding="utf-8"), version)
    db = Database(connection)
    business = SqliteBusinessRepository(db)
    repo = SqliteSessionRepository(db)
    files = AttachmentFiles(ROOT, check_credentials=partial(_reject_credentials, credentials=()))
    try:
        ids = await business_seed(db)
        async with business.transaction():
            await business.save_profile(ProfileContent.model_validate(FULL_PROFILE), 1, 1000)
            await business.insert_workout(make_record("2026-06-01"))
            content = PlanContent.model_validate(CONTENT)
            current = PlanRecord(id=identifier(), is_current=True, content=content, created_at=1000)
            await business.insert_plan(current)
            await business.insert_plan(current.model_copy(update={"id": identifier(), "is_current": False}))
            for kind in ("generation", "import", "adjustment"):
                for status in ("pending", "processing", "saved", "invalidated", "conflicted"):
                    snapshot = PlanSnapshot(proposal_id=identifier(), session_id=ids["session"],
                        request_entry_id=ids["request"], source_entry_id=ids["source"], preparation_kind=kind,
                        base_profile_version=1 if kind == "generation" else None, base_plan_id=None,
                        payload=content, status=status, created_at=1000, display_entry_id=ids["display"],
                        confirmation_entry_id=ids["confirmation"] if status in ("processing", "saved") else None)
                    # 每条处理/保存快照使用真实独立确认节点。
                    if status in ("processing", "saved"):
                        node = identifier()
                        await repo.insert_entry(SessionMessageEntry(session_id=ids["session"], id=node,
                            parent_id=ids["confirmation"], run_id=None, type="message", created_at=1000,
                            messages=[UserMessage(role="user", content="确认", timestamp=1000)]))
                        snapshot = snapshot.model_copy(update={"confirmation_entry_id": node})
                    await business.insert_plan_snapshot(snapshot)
                    if status == "saved":
                        result = PlanSaveResult(proposal_id=snapshot.proposal_id, id=current.id, content=content,
                            created_at=1000, saved_at=1000)
                        await business.complete_plan_save(PlanSaveRecord(proposal_id=snapshot.proposal_id,
                            session_id=ids["session"], display_entry_id=ids["display"],
                            confirmation_entry_id=snapshot.confirmation_entry_id, result=result, saved_at=1000))
        tables = [row[0] for row in await rows(db, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]

        # 压缩能力为 session_entries 增列；迁移保真只比对原有消息列。
        def snapshot_sql(table: str) -> str:
            columns = (
                "session_id, id, parent_id, run_id, type, messages, created_at"
                if table == "session_entries"
                else "*"
            )
            return f"SELECT {columns} FROM {table} ORDER BY 1, 2"

        before = {table: await rows(db, snapshot_sql(table)) for table in tables}
        assert await rows(db, "PRAGMA user_version") == [(8,)]
        data = "\ufeff# 原计划\r\n推拉腿\n".encode("utf-8")
        attachment = identifier()
        prepared = files.prepare(ids["session"], [{"kind": "upload", "attachment_id": attachment,
            "file_name": "计划.MD", "data_base64": base64.b64encode(data).decode("ascii")}], existing={})[0]
        stored = files.write(ids["session"], prepared, created_at=1000)
        workspace = ROOT / "tmp/sessions" / ids["session"] / "workspace/work.txt"
        workspace.parent.mkdir(parents=True)
        workspace.write_bytes(b"workspace original\r\n")
        await db.close()
        db = await open_database(path)
        repo = SqliteSessionRepository(db)
        business = SqliteBusinessRepository(db)
        assert await rows(db, "PRAGMA user_version") == [(SCHEMA_VERSION,)]
        for table in tables:
            assert await rows(db, snapshot_sql(table)) == before[table], table
        async with repo.transaction():
            await repo.insert_attachment(stored)
            await repo.bind_entry_attachments(ids["session"], ids["request"], [attachment])
        assert files.read(ids["session"], (await repo.get_attachments([attachment]))[attachment]).encode("utf-8") == data
        assert files.prepare(ids["session"], [{"kind": "reference", "attachment_id": attachment}],
            existing=await repo.get_attachments([attachment])) == (stored,)
        other, _, _, _ = await seed(repo)
        with pytest.raises(AttachmentError, match="不属于"):
            files.prepare(other, [{"kind": "reference", "attachment_id": attachment}],
                existing=await repo.get_attachments([attachment]))
        with pytest.raises(RuntimeError, match="回滚"):
            async with repo.transaction():
                await repo.defer_foreign_keys()
                await business.delete_plan_snapshots_for_session(ids["session"])
                await repo.delete_session(ids["session"])
                raise RuntimeError("回滚")
        assert await repo.list_entry_attachments(ids["session"], ids["request"]) == [stored]
        async with repo.transaction():
            await repo.defer_foreign_keys()
            await business.delete_plan_snapshots_for_session(ids["session"])
            await repo.delete_session(ids["session"])
        for table in ("profile", "plans", "workouts", "profile_save_records", "plan_save_records", "workout_save_records"):
            assert await rows(db, f"SELECT * FROM {table} ORDER BY 1, 2") == before[table]
        assert (ROOT / stored.storage_ref).read_bytes() == data
        assert workspace.read_bytes() == b"workspace original\r\n"
        assert await repo.get_attachments([attachment]) == {}
        assert await rows(db, "PRAGMA foreign_key_check") == []
        assert await rows(db, "PRAGMA integrity_check") == [("ok",)]
        await db.close()
        db = await open_database(path)
        assert (ROOT / stored.storage_ref).read_bytes() == data
        assert workspace.read_bytes() == b"workspace original\r\n"
    finally:
        await db.close()


async def check_schema_guards():
    # 用真实迁移建立受损终态，分别核验每项结构事实。
    source = _MIGRATIONS[9].read_text(encoding="utf-8")
    for name, old, new in (
        ("null", "file_name TEXT NOT NULL", "file_name TEXT"),
        ("type", "size_bytes INTEGER NOT NULL", "size_bytes REAL NOT NULL"),
        ("primary", "attachment_id TEXT NOT NULL PRIMARY KEY", "attachment_id TEXT NOT NULL UNIQUE"),
        ("cascade", "REFERENCES sessions(id) ON DELETE CASCADE", "REFERENCES sessions(id)"),
        ("check", "CHECK (position >= 0)", "CHECK (position >= -1)"),
        ("position_unique", "UNIQUE (session_id, entry_id, position),", ""),
    ):
        path = ROOT / f"damaged-{name}.db"
        connection = await aiosqlite.connect(path, isolation_level=None)
        for version in range(1, 9):
            await apply_schema(connection, _MIGRATIONS[version].read_text(encoding="utf-8"), version)
        await apply_schema(connection, source.replace(old, new), SCHEMA_VERSION)
        await connection.close()
        with pytest.raises(RuntimeError):
            await open_database(path)
    for name, sql in (("trigger", "DROP TRIGGER session_entry_attachments_user_update"),
        ("trigger_body", "DROP TRIGGER session_attachments_immutable_update; CREATE TRIGGER session_attachments_immutable_update BEFORE UPDATE ON session_attachments BEGIN SELECT 1; END;"),
        ("unique", "DROP TABLE steering_input_attachments; CREATE TABLE steering_input_attachments (session_id TEXT NOT NULL, steering_id TEXT NOT NULL, attachment_id TEXT NOT NULL, position INTEGER NOT NULL, PRIMARY KEY(session_id, steering_id, attachment_id), FOREIGN KEY(session_id, steering_id) REFERENCES steering_inputs(session_id,id), FOREIGN KEY(session_id,attachment_id) REFERENCES session_attachments(session_id,attachment_id)) STRICT;")):
        path = ROOT / f"damaged-{name}.db"
        db = await open_database(path)
        await db.connection.executescript(sql)
        await db.close()
        with pytest.raises(RuntimeError):
            await open_database(path)
    path = ROOT / "bad-row.db"
    db = await open_database(path)
    repo = SqliteSessionRepository(db)
    try:
        session, entry, _, steering = await seed(repo)
        item = metadata(session)
        await repo.insert_attachment(item)
        await repo.bind_entry_attachments(session, entry, [item.attachment_id])
        await repo.bind_steering_attachments(session, steering, [item.attachment_id])
        await execute(db, "DROP TRIGGER session_attachments_immutable_update")
        await execute(db, "PRAGMA ignore_check_constraints=ON")
        await execute(db, "UPDATE session_attachments SET storage_ref='/host/secret' WHERE attachment_id=?", (item.attachment_id,))
        with pytest.raises(ValueError):
            await repo.get_attachments([item.attachment_id])
        with pytest.raises(ValueError):
            await repo.list_entry_attachments(session, entry)
        await execute(db, "PRAGMA foreign_keys=OFF")
        await execute(db, "DELETE FROM session_attachments WHERE attachment_id=?", (item.attachment_id,))
        with pytest.raises(ValidationError):
            await repo.list_entry_attachments(session, entry)
        with pytest.raises(ValidationError):
            await repo.list_steering_attachments(session, steering)
    finally:
        await db.close()


async def check():
    await check_repository()
    await check_migration_and_files()
    await check_schema_guards()
    evidence = {"schema": SCHEMA_VERSION, "repository": "passed", "migration8": "passed",
        "real_files_retained": True, "schema_guards": "passed", "foreign_keys": [], "integrity": "ok"}
    (ROOT / "attachment-persistence-evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print("PASS: attachment metadata/order/reference/constraints; entry/Steering/session deletion; rollback/reopen; real 8→9 preservation; original/workspace bytes retained; schema guards; FK/integrity")
    print("Evidence:", ROOT)


if __name__ == "__main__":
    asyncio.run(check())
