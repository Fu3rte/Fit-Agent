import asyncio
import json
import sqlite3
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import aiosqlite
import pytest
from pydantic import ValidationError

from app.ai.messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
)
from app.application.session.service import SessionService
from app.domain.business.errors import (
    InvalidBusinessPayload,
    ProfileAccessDenied,
    ProfileConfirmationInvalid,
    ProfileProposalInvalidated,
    ProfileProposalNotFound,
    ProfileSessionNotFound,
    ProfileUpdateProcessing,
    ProfileVersionConflict,
)
from app.domain.business.models import (
    BusinessFieldError,
    ProfileContent,
    ProfileProposal,
    ProfileProposalArguments,
    ProfileSaveArguments,
    ProfileSaveRecord,
    ProfileSaveResult,
    ProfileSnapshot,
    ProfileStatusArguments,
    ProfileStatusResult,
)
from app.domain.session.models import SendCommand, SendRequest, SessionMessageEntry
from app.infrastructure.persistence.sqlite.business_repository import (
    SqliteBusinessRepository,
)
from app.infrastructure.persistence.sqlite.database import (
    _MIGRATIONS,
    SCHEMA_VERSION,
    Database,
    apply_schema,
    open_database,
)
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from test.regression_support import temporary_root

EVIDENCE = temporary_root("profile-snapshot")
USAGE = Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2)
SYSTEM = SystemMessage(role="system", content="系统提示词", tools_added=[], timestamp=100)

FULL_PROFILE = {
    "goal": "增肌",
    "experience": "新手",
    "environment": "健身房",
    "availability": "每周四次",
    "health_notes": None,
    "movement_restrictions": "避免肩部过顶",
    "unavailable_equipment": [],
    "forbidden_exercise_ids": [],
}

BUSINESS_ERRORS = (
    ProfileProposalNotFound,
    ProfileProposalInvalidated,
    ProfileVersionConflict,
    ProfileConfirmationInvalid,
    ProfileAccessDenied,
    ProfileSessionNotFound,
    ProfileUpdateProcessing,
    InvalidBusinessPayload,
)


def stamp() -> int:
    return int(time.time() * 1000)


def assistant_with_call(call_id: str, timestamp: int) -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=[
            ToolCall(
                type="toolCall",
                id=call_id,
                name="prepare_profile_update",
                arguments={"profile_id": 1, "base_profile_version": None},
            )
        ],
        api="openai-completions",
        provider="example",
        model="model-1",
        usage=USAGE,
        stop_reason="toolUse",
        timestamp=timestamp,
    )


def tool_result(call_id: str, payload: dict, timestamp: int) -> ToolResultMessage:
    return ToolResultMessage(
        role="toolResult",
        tool_call_id=call_id,
        tool_name="prepare_profile_update",
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        is_error=False,
        timestamp=timestamp,
    )


async def seed(service: SessionService, session_id: str) -> dict:
    # 一条完整可配对的工具链：用户节点 → 助手工具调用节点 → 工具结果节点 → 后续确认用户节点。
    created, _ = await service.create_session_result(session_id, "画像快照")
    assert created
    first = await service.accept_send(
        SendCommand(
            operation_id=str(uuid4()),
            session_id=session_id,
            request=SendRequest(text="记录我的情况"),
        ),
        system_message=SYSTEM,
    )
    assert first.run is not None
    request_id = first.run.request_entry_id
    now = stamp()
    source_id = str(uuid4())
    await service.append_entry(
        SessionMessageEntry(
            session_id=session_id,
            id=source_id,
            parent_id=request_id,
            run_id=first.run.id,
            type="message",
            messages=[assistant_with_call("call-prepare", now)],
            created_at=now,
        )
    )
    display_id = str(uuid4())
    await service.append_entry(
        SessionMessageEntry(
            session_id=session_id,
            id=display_id,
            parent_id=source_id,
            run_id=first.run.id,
            type="message",
            messages=[tool_result("call-prepare", {"payload": FULL_PROFILE}, now + 1)],
            created_at=now + 1,
        )
    )
    await service.finish_run(session_id, first.run.id, "completed")
    second = await service.accept_send(
        SendCommand(
            operation_id=str(uuid4()),
            session_id=session_id,
            request=SendRequest(text="确认保存这份画像"),
        ),
        system_message=SYSTEM,
    )
    assert second.run is not None
    await service.finish_run(session_id, second.run.id, "completed")
    return {
        "request": request_id,
        "source": source_id,
        "display": display_id,
        "confirmation": second.run.request_entry_id,
    }


def snapshot(ids: dict, proposal_id: str | None = None, **overrides) -> ProfileSnapshot:
    data = {
        "proposal_id": proposal_id or str(uuid4()),
        "session_id": overrides.pop("session_id", ids["session"]),
        "request_entry_id": ids["request"],
        "source_entry_id": ids["source"],
        "profile_id": 1,
        "base_profile_version": None,
        "payload": ProfileContent.model_validate(FULL_PROFILE),
        "display_entry_id": None,
        "confirmation_entry_id": None,
        "status": "pending",
        "created_at": stamp(),
    }
    data.update(overrides)
    return ProfileSnapshot.model_validate(data)


def save_record(ids: dict, proposal_id: str, version: int, saved_at: int) -> ProfileSaveRecord:
    result = ProfileSaveResult(
        proposal_id=proposal_id,
        profile_id=1,
        version=version,
        content=ProfileContent.model_validate(FULL_PROFILE),
        saved_at=saved_at,
    )
    return ProfileSaveRecord(
        proposal_id=proposal_id,
        session_id=ids["session"],
        profile_id=1,
        display_entry_id=ids["display"],
        confirmation_entry_id=ids["confirmation"],
        result=result,
        saved_at=saved_at,
    )


async def rejects(label: str, awaitable) -> str:
    try:
        await awaitable
    except BaseException as error:
        return f"{label}:{type(error).__name__}"
    raise AssertionError(f"{label} 未按预期拒绝")


_SNAPSHOT_ORDER = (
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
)
_SNAPSHOT_INSERT = (
    "INSERT INTO profile_snapshots ({}) VALUES ({})".format(
        ", ".join(_SNAPSHOT_ORDER), ", ".join("?" for _ in _SNAPSHOT_ORDER)
    )
)


def snapshot_row(ids: dict, proposal_id: str, **overrides) -> tuple:
    # 直接构造持久化行，用于验证数据库自身的约束与触发器。
    row = {
        "proposal_id": proposal_id,
        "session_id": ids["session"],
        "request_entry_id": ids["request"],
        "source_entry_id": ids["source"],
        "profile_id": 1,
        "base_profile_version": None,
        "payload": json.dumps(FULL_PROFILE, ensure_ascii=False),
        "display_entry_id": None,
        "confirmation_entry_id": None,
        "status": "pending",
        "created_at": stamp(),
    }
    row.update(overrides)
    return tuple(row[column] for column in _SNAPSHOT_ORDER)


async def raw(database: Database, sql: str, parameters: tuple = ()) -> None:
    async def run(connection: aiosqlite.Connection) -> None:
        cursor = await connection.execute(sql, parameters)
        await cursor.close()

    await database.read(run)


async def open_pair(
    root: Path,
) -> tuple[Database, SqliteBusinessRepository, SqliteSessionRepository]:
    database = await open_database(root / f"{uuid4().hex}.db")
    return database, SqliteBusinessRepository(database), SqliteSessionRepository(database)


async def check_migration(root: Path) -> dict:
    fresh = root / "fresh.db"
    database = await open_database(fresh)
    await database.close()
    version = await _user_version(fresh)
    reopened = await open_database(fresh)
    await reopened.close()
    legacy = root / "legacy.db"
    connection = await _connect(legacy)
    for target in (1, 2, 3):
        await apply_schema(connection, _MIGRATIONS[target].read_text(encoding="utf-8"), target)
    await connection.close()
    upgraded = await open_database(legacy)
    tables = await _tables(await _connect(legacy))
    await upgraded.close()
    assert version == SCHEMA_VERSION == 6
    assert {"profile_snapshots", "profile_save_records"} <= tables
    assert not {"confirmations", "confirmation_receipts"} & tables
    guarded = root / "guarded.db"
    connection = await _connect(guarded)
    await connection.execute("PRAGMA foreign_keys = ON")
    for target in (1, 2, 3, 4):
        await apply_schema(connection, _MIGRATIONS[target].read_text(encoding="utf-8"), target)
    await connection.execute(
        "INSERT INTO confirmation_receipts VALUES (?, ?, ?)",
        (str(uuid4()), json.dumps({"version": 1}), stamp()),
    )
    await connection.commit()
    await connection.close()
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        await open_database(guarded)
    connection = await _connect(guarded)
    assert (await (await connection.execute("PRAGMA user_version")).fetchone())[0] == 4
    assert (await (await connection.execute("SELECT COUNT(*) FROM confirmation_receipts")).fetchone())[0] == 1
    await connection.close()
    return {"schema_version": SCHEMA_VERSION, "reopened": True, "upgraded_tables": sorted(tables), "nonempty_legacy_migration_rejected": True}


async def _connect(path: Path) -> aiosqlite.Connection:
    return await aiosqlite.connect(path, isolation_level=None)


async def _user_version(path: Path) -> int:
    connection = await _connect(path)
    cursor = await connection.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    await connection.close()
    return int(row[0])


async def _tables(connection: aiosqlite.Connection) -> set[str]:
    cursor = await connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    rows = await cursor.fetchall()
    return {row[0] for row in rows}


async def check_store(root: Path) -> dict:
    database, business, sessions = await open_pair(root)
    service = SessionService(sessions)
    other_database, _, other_sessions = await open_pair(root)
    other_service = SessionService(other_sessions)
    try:
        session_id = str(uuid4())
        ids = await seed(service, session_id)
        ids["session"] = session_id
        other_session_id = str(uuid4())
        await seed(other_service, other_session_id)
        proposal_id = str(uuid4())
        async with business.transaction():
            await business.insert_snapshot(snapshot(ids, proposal_id))
        stored = await business.get_snapshot(proposal_id)
        assert stored == snapshot(ids, proposal_id, created_at=stored.created_at)

        rejections = [
            await rejects(
                "重复 proposal_id",
                business.insert_snapshot(snapshot(ids, proposal_id)),
            ),
            await rejects(
                "跨会话消息节点",
                business.insert_snapshot(snapshot(ids, session_id=other_session_id)),
            ),
            await rejects(
                "会话不存在",
                business.insert_snapshot(snapshot(ids, session_id=str(uuid4()))),
            ),
            await rejects(
                "请求节点为助手",
                business.insert_snapshot(snapshot(ids, request_entry_id=ids["source"])),
            ),
            await rejects(
                "来源节点为用户",
                business.insert_snapshot(snapshot(ids, source_entry_id=ids["request"])),
            ),
            await rejects(
                "非单用户目标画像",
                raw(database, _SNAPSHOT_INSERT, snapshot_row(ids, str(uuid4()), profile_id=2)),
            ),
            await rejects(
                "依据版本为零",
                raw(
                    database,
                    _SNAPSHOT_INSERT,
                    snapshot_row(ids, str(uuid4()), base_profile_version=0),
                ),
            ),
            await rejects(
                "未知保存状态",
                raw(
                    database,
                    _SNAPSHOT_INSERT,
                    snapshot_row(ids, str(uuid4()), status="committed"),
                ),
            ),
            await rejects(
                "saved 缺少绑定",
                raw(database, _SNAPSHOT_INSERT, snapshot_row(ids, str(uuid4()), status="saved")),
            ),
            await rejects(
                "确认绑定缺少展示绑定",
                raw(
                    database,
                    _SNAPSHOT_INSERT,
                    snapshot_row(
                        ids, str(uuid4()), confirmation_entry_id=ids["confirmation"]
                    ),
                ),
            ),
            await rejects(
                "内容非 JSON 对象",
                raw(
                    database,
                    _SNAPSHOT_INSERT,
                    snapshot_row(ids, str(uuid4()), payload="[]"),
                ),
            ),
            await rejects(
                "修改快照内容",
                raw(
                    database,
                    "UPDATE profile_snapshots SET payload = ? WHERE proposal_id = ?",
                    ('{"goal": "力量"}', proposal_id),
                ),
            ),
            await rejects(
                "修改依据版本",
                raw(
                    database,
                    "UPDATE profile_snapshots SET base_profile_version = 2 "
                    "WHERE proposal_id = ?",
                    (proposal_id,),
                ),
            ),
            await rejects(
                "修改归属会话",
                raw(
                    database,
                    "UPDATE profile_snapshots SET session_id = ? WHERE proposal_id = ?",
                    (other_session_id, proposal_id),
                ),
            ),
            await rejects(
                "展示绑定指向助手节点",
                business.bind_display_entry(proposal_id, ids["source"]),
            ),
            await rejects(
                "确认绑定指向工具结果节点",
                business.begin_save(proposal_id, ids["display"]),
            ),
        ]
        assert all("IntegrityError" in item for item in rejections), rejections

        async with business.transaction():
            await business.bind_display_entry(proposal_id, ids["display"])
        bound = await business.get_snapshot(proposal_id)
        assert bound.display_entry_id == ids["display"] and bound.status == "pending"
        assert await business.find_snapshot_by_confirmation(
            session_id, ids["confirmation"]
        ) is None

        async with business.transaction():
            await business.begin_save(proposal_id, ids["confirmation"])
        started = await business.get_snapshot(proposal_id)
        assert started.status == "processing"
        assert started.confirmation_entry_id == ids["confirmation"]
        located = await business.find_snapshot_by_confirmation(
            session_id, ids["confirmation"]
        )
        assert located is not None and located.proposal_id == proposal_id

        # 重复绑定原 proposal_id 幂等：同一确认节点保持唯一关联。
        async with business.transaction():
            await business.begin_save(proposal_id, ids["confirmation"])
        idempotent = await business.find_snapshot_by_confirmation(
            session_id, ids["confirmation"]
        )
        assert idempotent is not None and idempotent.proposal_id == proposal_id

        second_id = str(uuid4())
        async with business.transaction():
            await business.insert_snapshot(snapshot(ids, second_id))
            await business.bind_display_entry(second_id, ids["display"])
        second_rejected = await rejects(
            "同一确认节点绑定其他快照",
            _bind_second(business, second_id, ids["confirmation"]),
        )
        assert "IntegrityError" in second_rejected
        kept = await business.find_snapshot_by_confirmation(
            session_id, ids["confirmation"]
        )
        assert kept is not None and kept.proposal_id == proposal_id
        assert (await business.get_snapshot(second_id)).confirmation_entry_id is None

        invalidated = str(uuid4())
        async with business.transaction():
            await business.insert_snapshot(snapshot(ids, invalidated))
            await business.set_snapshot_status(invalidated, "invalidated")
            await business.set_snapshot_status(invalidated, "conflicted")
        assert (await business.get_snapshot(invalidated)).status == "conflicted"
        return {
            "rejections": [*rejections, second_rejected],
            "binding": [bound.status, started.status],
            "located": proposal_id,
            "stored": stored.model_dump(),
        }
    finally:
        await other_database.close()
        await database.close()


async def _bind_second(business, proposal_id: str, confirmation_entry_id: str) -> None:
    async with business.transaction():
        await business.begin_save(proposal_id, confirmation_entry_id)


async def check_atomic_save(root: Path) -> dict:
    database, business, sessions = await open_pair(root)
    service = SessionService(sessions)
    try:
        session_id = str(uuid4())
        ids = await seed(service, session_id)
        ids["session"] = session_id
        proposal_id = str(uuid4())
        saved_at = stamp()
        async with business.transaction():
            await business.insert_snapshot(snapshot(ids, proposal_id))
            await business.bind_display_entry(proposal_id, ids["display"])
            await business.begin_save(proposal_id, ids["confirmation"])

        async def failed_save() -> None:
            async with business.transaction():
                await business.save_profile(
                    ProfileContent.model_validate(FULL_PROFILE), 1, saved_at
                )
                await business.complete_save(
                    save_record(ids, proposal_id, 1, saved_at)
                )
                raise RuntimeError("模拟保存事务提交前中断")

        aborted = await rejects("保存事务中断", failed_save())
        assert "RuntimeError" in aborted
        assert await business.get_profile() is None
        assert await business.get_save_record(proposal_id) is None
        assert (await business.get_snapshot(proposal_id)).status == "processing"

        recovered = await business.recover_interrupted_saves()
        after_recovery = await business.get_snapshot(proposal_id)
        untouched = str(uuid4())
        async with business.transaction():
            await business.insert_snapshot(snapshot(ids, untouched))
            await business.set_snapshot_status(untouched, "invalidated")
        recovered_twice = await business.recover_interrupted_saves()
        assert recovered == 1 and recovered_twice == 0
        assert after_recovery.status == "pending"
        assert after_recovery.confirmation_entry_id == ids["confirmation"]
        assert (await business.get_snapshot(untouched)).status == "invalidated"

        async with business.transaction():
            await business.begin_save(proposal_id, ids["confirmation"])
            await business.save_profile(
                ProfileContent.model_validate(FULL_PROFILE), 1, saved_at
            )
            await business.complete_save(save_record(ids, proposal_id, 1, saved_at))
        profile = await business.get_profile()
        record = await business.get_save_record(proposal_id)
        saved = await business.get_snapshot(proposal_id)
        assert profile.version == 1 and saved.status == "saved"
        status = ProfileStatusResult(
            proposal_id=proposal_id, status="saved", result=record.result
        )
        assert ProfileStatusResult.model_validate(status.model_dump()) == status
        assert record.result.saved_at == saved_at and record.saved_at == saved_at
        assert record.result.content.model_dump() == FULL_PROFILE
        located = await business.find_save_record_by_confirmation(
            session_id, ids["confirmation"]
        )
        assert located is not None and located.proposal_id == proposal_id
        assert await business.find_save_record_by_confirmation(
            str(uuid4()), ids["confirmation"]
        ) is None

        conflicting = await rejects(
            "重复固定结果",
            business.complete_save(save_record(ids, proposal_id, 2, saved_at + 1)),
        )
        assert "IntegrityError" in conflicting
        drifted = await rejects(
            "固定结果与原保存时间漂移",
            raw(
                database,
                "UPDATE profile_save_records SET saved_at = ? WHERE proposal_id = ?",
                (saved_at + 1, proposal_id),
            ),
        )
        assert "IntegrityError" in drifted
        return {
            "recovery": [recovered, recovered_twice],
            "rollback": [aborted],
            "saved": [saved.status, record.saved_at, profile.version],
            "record_guard": [conflicting, drifted],
        }
    finally:
        await database.close()


async def check_cleanup(root: Path) -> dict:
    database, business, sessions = await open_pair(root)
    service = SessionService(sessions)
    try:
        session_id = str(uuid4())
        ids = await seed(service, session_id)
        ids["session"] = session_id
        proposal_id = str(uuid4())
        saved_at = stamp()
        async with business.transaction():
            await business.insert_snapshot(snapshot(ids, proposal_id))
            await business.bind_display_entry(proposal_id, ids["display"])
            await business.begin_save(proposal_id, ids["confirmation"])
            await business.save_profile(ProfileContent.model_validate(FULL_PROFILE), 1, saved_at)
            await business.complete_save(save_record(ids, proposal_id, 1, saved_at))

        blocked = await rejects(
            "绑定节点未清理即删除",
            sessions.delete_entries(session_id, [ids["display"]]),
        )
        assert "IntegrityError" in blocked

        pending_id = str(uuid4())
        async with business.transaction():
            await business.insert_snapshot(snapshot(ids, pending_id))
        async with sessions.transaction():
            await sessions.defer_foreign_keys()
            await business.delete_snapshots_for_session(session_id)
            await sessions.delete_session(session_id)
        assert await business.get_snapshot(proposal_id) is None
        assert await business.get_snapshot(pending_id) is None
        record = await business.get_save_record(proposal_id)
        profile = await business.get_profile()
        located = await business.find_save_record_by_confirmation(
            session_id, ids["confirmation"]
        )
        assert record is not None and profile is not None
        assert located is not None and located.proposal_id == proposal_id
        assert await business.find_snapshot_by_confirmation(
            session_id, ids["confirmation"]
        ) is None
        assert record.result.version == 1 and record.saved_at == saved_at
        return {
            "blocked": blocked,
            "survived_record": record.proposal_id,
            "survived_profile_version": profile.version,
            "snapshots_left": await _session_proposals(business, session_id),
        }
    finally:
        await database.close()


async def _session_proposals(business, session_id: str) -> list[str]:
    return [item.proposal_id for item in await business.list_snapshots(session_id)]


NULL_PROFILE = dict.fromkeys(FULL_PROFILE)


def schema_cases() -> list[tuple[str, object, dict, bool]]:
    proposal_id = str(uuid4())
    entry_id = str(uuid4())
    other_id = str(uuid4())
    proposal = {
        "profile_id": 1,
        "base_profile_version": None,
        "payload": FULL_PROFILE,
    }
    saved_result = {
        "proposal_id": proposal_id,
        "profile_id": 1,
        "version": 1,
        "content": FULL_PROFILE,
        "saved_at": 1_700_000_000_000,
    }
    save_arguments = {
        "proposal_id": proposal_id,
        "display_entry_id": entry_id,
        "confirmation_entry_id": other_id,
    }

    def status(value: str, result: object = None) -> dict:
        return {"proposal_id": proposal_id, "status": value, "result": result}

    return [
        ("prepare_arguments", ProfileProposalArguments, proposal, True),
        ("prepare_arguments", ProfileProposalArguments, {**proposal, "base_profile_version": 3}, True),
        ("prepare_arguments", ProfileProposalArguments, {**proposal, "base_profile_version": 2, "payload": NULL_PROFILE}, True),
        ("prepare_arguments", ProfileProposalArguments, {**proposal, "payload": NULL_PROFILE}, False),
        ("prepare_arguments", ProfileProposalArguments, {"profile_id": 1, "base_profile_version": None}, False),
        ("prepare_arguments", ProfileProposalArguments, {**proposal, "session_id": entry_id}, False),
        ("prepare_arguments", ProfileProposalArguments, {**proposal, "profile_id": "2"}, False),
        ("prepare_arguments", ProfileProposalArguments, {**proposal, "profile_id": "1"}, False),
        ("prepare_arguments", ProfileProposalArguments, {**proposal, "profile_id": True}, False),
        ("prepare_arguments", ProfileProposalArguments, {**proposal, "profile_id": 2}, False),
        ("prepare_arguments", ProfileProposalArguments, {**proposal, "profile_id": 0}, False),
        ("prepare_arguments", ProfileProposalArguments, {**proposal, "base_profile_version": 0}, False),
        ("prepare_arguments", ProfileProposalArguments, {**proposal, "base_profile_version": "1"}, False),
        ("prepare_arguments", ProfileProposalArguments, {**proposal, "base_profile_version": 1.5}, False),
        ("prepare_arguments", ProfileProposalArguments, {**proposal, "base_profile_version": True}, False),
        ("prepare_arguments", ProfileProposalArguments, {**proposal, "payload": {**FULL_PROFILE, "goal": "   "}}, False),
        ("prepare_arguments", ProfileProposalArguments, {**proposal, "payload": {**FULL_PROFILE, "goal": 5}}, False),
        ("prepare_arguments", ProfileProposalArguments, {**proposal, "payload": {**FULL_PROFILE, "forbidden_exercise_ids": ["0001", "0001"]}}, False),
        ("prepare_arguments", ProfileProposalArguments, {**proposal, "payload": {**FULL_PROFILE, "forbidden_exercise_ids": ["0001", "  "]}}, False),
        ("prepare_arguments", ProfileProposalArguments, {**proposal, "payload": {**FULL_PROFILE, "unavailable_equipment": "barbell"}}, False),
        ("prepare_arguments", ProfileProposalArguments, {**proposal, "payload": {**FULL_PROFILE, "age": None}}, False),
        ("prepare_arguments", ProfileProposalArguments, {**proposal, "payload": {k: v for k, v in FULL_PROFILE.items() if k != "goal"}}, False),
        ("prepare_result", ProfileProposal, {"proposal_id": proposal_id, **proposal}, True),
        ("prepare_result", ProfileProposal, proposal, False),
        ("prepare_result", ProfileProposal, {"proposal_id": "p-1", **proposal}, False),
        ("prepare_result", ProfileProposal, {"proposal_id": proposal_id.replace("-", ""), **proposal}, False),
        ("save_arguments", ProfileSaveArguments, save_arguments, True),
        ("save_arguments", ProfileSaveArguments, {"proposal_id": proposal_id, "display_entry_id": entry_id}, False),
        ("save_arguments", ProfileSaveArguments, {**save_arguments, "session_id": entry_id}, False),
        ("save_arguments", ProfileSaveArguments, {**save_arguments, "confirmation_entry_id": entry_id.replace("-", "")}, False),
        ("save_result", ProfileSaveResult, saved_result, True),
        ("save_result", ProfileSaveResult, {**saved_result, "version": 0}, False),
        ("save_result", ProfileSaveResult, {**saved_result, "version": "1"}, False),
        ("save_result", ProfileSaveResult, {**saved_result, "saved_at": 0}, False),
        ("save_result", ProfileSaveResult, {**saved_result, "content": None}, False),
        ("save_result", ProfileSaveResult, {**saved_result, "profile_id": 2}, False),
        ("save_result", ProfileSaveResult, {**saved_result, "profile_id": "1"}, False),
        ("save_result", ProfileSaveResult, {k: v for k, v in saved_result.items() if k != "proposal_id"}, False),
        ("status_arguments", ProfileStatusArguments, {"proposal_id": proposal_id}, True),
        ("status_arguments", ProfileStatusArguments, {"proposal_id": proposal_id, "session_id": entry_id}, False),
        ("status_arguments", ProfileStatusArguments, {}, False),
        ("status_result", ProfileStatusResult, status("pending"), True),
        ("status_result", ProfileStatusResult, status("processing"), True),
        ("status_result", ProfileStatusResult, status("invalidated"), True),
        ("status_result", ProfileStatusResult, status("conflicted"), True),
        ("status_result", ProfileStatusResult, status("saved", saved_result), True),
        ("status_result", ProfileStatusResult, status("saved"), False),
        ("status_result", ProfileStatusResult, status("pending", saved_result), False),
        ("status_result", ProfileStatusResult, status("saved", {**saved_result, "proposal_id": other_id}), False),
        ("status_result", ProfileStatusResult, status("committed"), False),
        ("status_result", ProfileStatusResult, {**status("pending"), "session_id": entry_id}, False),
    ]


def check_models() -> dict:
    verdicts = []
    for name, model, payload, expected in schema_cases():
        try:
            model.model_validate(payload)
            accepted = True
        except ValidationError:
            accepted = False
        assert accepted is expected, (name, payload, expected)
        verdicts.append({"parser": name, "payload": payload, "accepted": accepted})
    field_error = BusinessFieldError(path="/payload/goal", message="字段取值不合法。")
    codes: dict[str, dict] = {}
    for error in BUSINESS_ERRORS:
        instance = (
            InvalidBusinessPayload([field_error])
            if error is InvalidBusinessPayload
            else error()
        )
        codes[error.code] = instance.detail()
    assert codes["invalid_business_payload"]["errors"] == [field_error.model_dump()]
    assert all(
        "errors" not in detail
        for code, detail in codes.items()
        if code != "invalid_business_payload"
    )
    assert all({"code", "message"} <= set(detail) for detail in codes.values())
    assert set(codes) == {
        "profile_proposal_not_found",
        "profile_proposal_invalidated",
        "profile_update_processing",
        "profile_version_conflict",
        "profile_confirmation_invalid",
        "profile_access_denied",
        "session_not_found",
        "invalid_business_payload",
    }
    return {"cases": verdicts, "codes": sorted(codes)}


def check() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    evidence: dict = {}
    with TemporaryDirectory(dir=EVIDENCE, ignore_cleanup_errors=True) as directory:
        root = Path(directory)
        evidence["migration"] = asyncio.run(check_migration(root))
        evidence["store"] = asyncio.run(check_store(root))
        evidence["atomic_save"] = asyncio.run(check_atomic_save(root))
        evidence["cleanup"] = asyncio.run(check_cleanup(root))
    evidence["models"] = check_models()
    (EVIDENCE / "profile-snapshot.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(
        "PASS: 迁移与结构校验、快照唯一与归属外键、节点角色触发器、内容不可修改、"
        "展示与确认绑定、确认节点唯一绑定、原子保存与回滚、重启恢复、"
        "会话删除保留幂等记录、三工具 schema 与八项错误码"
    )


if __name__ == "__main__":
    check()
