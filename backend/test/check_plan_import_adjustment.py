import asyncio
import copy
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import aiosqlite
import pytest
from pydantic import ValidationError

from app.ai.messages import TextContent, ToolResultMessage
from app.domain.business.errors import BusinessError, PlanConfirmationInvalid
from app.domain.business.models import (
    PlanAdjustmentArguments,
    PlanAdjustmentProposal,
    PlanImportArguments,
    PlanImportProposal,
    PlanSaveArguments,
    PlanSnapshot,
)
from app.domain.session.models import SendCommand, SendRequest
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
from test.check_plan_core import CONTENT, profile
from test.check_profile_confirmation import (
    SYSTEM,
    Fixture,
    assert_rejected,
    new_id,
    now_ms,
)
from test.check_workout_service_http import assistant
from test.check_workout_storage import make_record, raw, seed

ROOT = Path(__file__).resolve().parents[2] / "tmp/backend-plan-import/checks" / uuid4().hex
ROOT.mkdir(parents=True)
INCOMPLETE = {"repeat": None, "days": [
    {"kind": "training", "focus": "推", "exercises": [], "notes": None},
    {"kind": "rest", "focus": None, "exercises": [], "notes": None}],
    "notes": None, "suggested_fields": []}


def check_schema():
    for model in (PlanImportArguments, PlanAdjustmentArguments):
        valid = {"base_profile_version": None, "base_plan_id": None, "payload": INCOMPLETE}
        model.model_validate(valid)
        for key in valid:
            with pytest.raises(ValidationError):
                model.model_validate({k: v for k, v in valid.items() if k != key})
        for update in ({"preparation_kind": "generation"}, {"base_profile_version": True},
                       {"base_profile_version": "1"}, {"base_profile_version": 0},
                       {"base_plan_id": uuid4().hex}):
            with pytest.raises(ValidationError):
                model.model_validate({**valid, **update})
    for model, kind in ((PlanImportProposal, "import"), (PlanAdjustmentProposal, "adjustment")):
        values = {**valid, "proposal_id": new_id(), "preparation_kind": kind}
        model.model_validate(values)
        with pytest.raises(ValidationError):
            model.model_validate({**values, "preparation_kind": "generation"})
        with pytest.raises(ValidationError):
            model.model_validate({k: v for k, v in values.items() if k != "preparation_kind"})


async def prepare(f, session, kind="import", version=None, base=None, content=None, display_kind=None, extra=False):
    accepted = await f.service.accept_send(SendCommand(operation_id=new_id(), session_id=session,
        request=SendRequest(text="整理计划")), system_message=SYSTEM)
    run = accepted.run
    model = PlanImportArguments if kind == "import" else PlanAdjustmentArguments
    args = model(base_profile_version=version, base_plan_id=base, payload=INCOMPLETE if content is None else content)
    call = new_id()
    tool = "prepare_plan_" + kind
    source = await f.append(session, run.id, run.request_entry_id, assistant(tool, call, args.model_dump()))
    display = None
    try:
        proposal = await getattr(f.business, tool)(f.context(session, run.request_entry_id, source), args)
        data = proposal.model_dump()
        if extra:
            data["extra"] = True
        display = await f.append(session, run.id, source, ToolResultMessage(role="toolResult",
            tool_name=tool if display_kind is None else display_kind, tool_call_id=call,
            content=[TextContent(type="text", text=json.dumps(data))], is_error=False, timestamp=now_ms()))
        await f.business.bind_plan_display_entry(proposal.proposal_id, display)
    except BusinessError as error:
        if display is None:
            await f.append(session, run.id, source, ToolResultMessage(role="toolResult",
                tool_name=tool, tool_call_id=call, content=[TextContent(type="text", text=json.dumps(error.detail()))],
                is_error=True, timestamp=now_ms()))
        raise
    finally:
        await f.service.finish_run(session, run.id, "completed")
    snapshot = await f.repository.get_plan_snapshot(proposal.proposal_id)
    assert snapshot.preparation_kind == kind and snapshot.base_profile_version == version
    assert snapshot.payload == args.payload
    accepted = await f.service.accept_send(SendCommand(operation_id=new_id(), session_id=session,
        request=SendRequest(text="确认保存")), system_message=SYSTEM)
    run = accepted.run
    save = PlanSaveArguments(proposal_id=proposal.proposal_id, display_entry_id=display,
                             confirmation_entry_id=run.request_entry_id)
    saving = assistant("save_plan", new_id(), save.model_dump()).model_copy(update={
        "content": [TextContent(type="text", text="处理用户确认")], "stop_reason": "stop"})
    source = await f.append(session, run.id, run.request_entry_id, saving)
    await f.service.finish_run(session, run.id, "completed")
    return f.context(session, run.request_entry_id, source), save


async def check_business():
    f = await Fixture(ROOT / "business.db").seeded()
    try:
        session = await f.session()
        c, a = await prepare(f, session)
        await assert_rejected("展示前请求授权", f.business.save_plan(c, a.model_copy(update={
            "confirmation_entry_id": (await f.repository.get_plan_snapshot(a.proposal_id)).request_entry_id})),
            "plan_confirmation_invalid")
        first = await f.business.save_plan(c, a)
        assert first.content.model_dump() == INCOMPLETE
        assert await f.business.save_plan(c, a) == first
        workout = make_record("2026-06-01")
        await f.repository.insert_workout(workout)
        partial = copy.deepcopy(CONTENT)
        partial["days"][0]["exercises"][0].update(sets=None, reps=None)
        c2, a2 = await prepare(f, session, "adjustment", base=first.id, content=partial)
        second = await f.business.save_plan(c2, a2)
        assert second.content.model_dump() == partial
        assert await f.repository.get_workout(workout.id) == workout
        oldc, olda = await prepare(f, session, base=second.id)
        newc, newa = await prepare(f, session, "adjustment", base=second.id)
        await assert_rejected("失效", f.business.save_plan(oldc, olda), "plan_proposal_invalidated")
        await assert_rejected("跨计划确认", f.business.save_plan(newc, newa.model_copy(update={
            "confirmation_entry_id": a.confirmation_entry_id})), "plan_confirmation_invalid")
        invalid = copy.deepcopy(partial)
        invalid["days"][0]["exercises"][0]["exercise_id"] = "unknown"
        await assert_rejected("准备失败保留pending", prepare(f, session, base=second.id, content=invalid),
                              "invalid_business_payload")
        assert (await f.business.get_plan_save_status(newc, newa.proposal_id)).status == "pending"
        profile_proposal, _ = await profile(f, session)
        await assert_rejected("跨业务确认", f.business.save_plan(newc, newa.model_copy(update={
            "confirmation_entry_id": profile_proposal["confirmation"]})), "plan_confirmation_invalid")
        await assert_rejected("null画像变为已保存", f.business.save_plan(newc, newa), "profile_version_conflict")
        await assert_rejected("已存在画像null依据", prepare(f, session, base=second.id), "profile_version_conflict")
        assert await f.business.save_plan(c, a) == first
        assert (await f.business.get_plan_save_status(c, a.proposal_id)).result == first
        for kind in ("import", "adjustment"):
            exercise = f.catalog.all()[0]
            real = copy.deepcopy(partial)
            real["days"][0]["exercises"][0]["exercise_id"] = exercise.id
            await profile(f, session, (await f.business.get_profile()).version,
                          forbidden_exercise_ids=[exercise.id])
            version = (await f.business.get_profile()).version
            await assert_rejected("已知限制", prepare(f, session, kind, version, second.id, real), "invalid_business_payload")
            unknown = copy.deepcopy(real)
            unknown["days"][0]["exercises"][0]["exercise_id"] = "unknown"
            await assert_rejected("目录", prepare(f, session, kind, version, second.id, unknown), "invalid_business_payload")
            await profile(f, session, version, unavailable_equipment=[exercise.equipment])
            version += 1
            await assert_rejected("器械限制", prepare(f, session, kind, version, second.id, real), "invalid_business_payload")
            with pytest.raises(PlanConfirmationInvalid):
                await prepare(f, await f.session(), kind, version, second.id, display_kind="prepare_plan")
            with pytest.raises(ValidationError):
                await prepare(f, session, kind, version, second.id, extra=True)
        version = (await f.business.get_profile()).version
        other = await f.session()
        stale, stalea = await prepare(f, other, version=version, base=second.id)
        current, currenta = await prepare(f, session, "adjustment", version, second.id)
        third = await f.business.save_plan(current, currenta)
        await assert_rejected("当前计划冲突", f.business.save_plan(stale, stalea), "plan_version_conflict")
        pc, pa = await prepare(f, session, version=version, base=third.id)
        await profile(f, session, version, health_notes="已补充")
        await assert_rejected("画像版本冲突", f.business.save_plan(pc, pa), "profile_version_conflict")
        assert await f.business.save_plan(c, a) == first
        assert sum(p.is_current for p in await f.business.list_plans()) == 1
        await f.restart()
        assert await f.business.save_plan(c, a) == first
        assert await f.repository.get_workout(workout.id) == workout
    finally:
        await f.close()


async def check_migration():
    path = ROOT / "v7.db"
    connection = await aiosqlite.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    await connection.execute("PRAGMA foreign_keys=ON")
    for version in range(1, 8):
        await apply_schema(connection, _MIGRATIONS[version].read_text(encoding="utf-8"), version)
    db = Database(connection)
    saved_id = None
    try:
        for status in ("pending", "processing", "saved", "invalidated", "conflicted"):
            ids = await seed(db)
            proposal = new_id()
            await raw(db, "INSERT INTO plan_snapshots VALUES (?, ?, ?, ?, 1, NULL, ?, ?, ?, ?, ?)",
                      (proposal, ids["session"], ids["request"], ids["source"], json.dumps(CONTENT),
                       ids["display"], ids["confirmation"], status, now_ms()))
            if status == "saved":
                saved_id = new_id()
                stamp = now_ms()
                await raw(db, "INSERT INTO plans VALUES (?, 1, ?, ?)", (saved_id, json.dumps(CONTENT), stamp))
                result = {"proposal_id": proposal, "id": saved_id, "content": CONTENT,
                          "created_at": stamp, "saved_at": stamp}
                await raw(db, "INSERT INTO plan_save_records VALUES (?, ?, ?, ?, ?, ?)",
                          (proposal, ids["session"], ids["display"], ids["confirmation"], json.dumps(result), stamp))
        async def rows(table):
            cursor = await db.connection.execute(f"SELECT * FROM {table} ORDER BY 1")
            result = [dict(row) for row in await cursor.fetchall()]
            await cursor.close()
            return result
        tables = ("plans", "plan_save_records", "workouts", "profile_snapshots", "profile_save_records",
                  "workout_snapshots", "workout_save_records", "sessions", "session_entries")
        before = {table: await rows(table) for table in tables}
        snapshots = await rows("plan_snapshots")
        await db.close()
        db = await open_database(path)
        cursor = await db.connection.execute("PRAGMA user_version")
        assert (await cursor.fetchone())[0] == SCHEMA_VERSION
        await cursor.close()
        for table in tables:
            assert await rows(table) == before[table]
        assert await rows("plan_snapshots") == [{**row, "preparation_kind": "generation"} for row in snapshots]
        repo = SqliteBusinessRepository(db)
        assert (await repo.get_current_plan()).id == saved_id
        for kind in ("generation", "import", "adjustment"):
            values = {**snapshots[0], "proposal_id": new_id(), "confirmation_entry_id": None,
                      "status": "pending", "preparation_kind": kind,
                      "base_profile_version": 1 if kind == "generation" else None}
            snapshot = PlanSnapshot.model_validate({**values, "payload": json.loads(values["payload"])})
            await repo.insert_plan_snapshot(snapshot)
            assert await repo.get_plan_snapshot(snapshot.proposal_id) == snapshot
            with pytest.raises(sqlite3.IntegrityError):
                await raw(db, "UPDATE plan_snapshots SET preparation_kind='adjustment' WHERE proposal_id=?", (snapshot.proposal_id,))
        with pytest.raises(sqlite3.IntegrityError):
            await raw(db, "INSERT INTO plan_snapshots (proposal_id, session_id, request_entry_id, source_entry_id, preparation_kind, payload, status, created_at) VALUES (?, ?, ?, ?, 'generation', '{}', 'pending', ?)",
                      (new_id(), ids["session"], ids["request"], ids["source"], now_ms()))
        cursor = await db.connection.execute("PRAGMA foreign_key_check")
        assert await cursor.fetchall() == []
        await cursor.close()
        await db.close()
        db = await open_database(path)
    finally:
        await db.close()


async def check():
    check_schema()
    await check_business()
    await check_migration()
    print(f"PASS: strict import/adjustment schema; unknown values; real display/confirmation; nullable/version/plan conflicts; limits; immutable kinds; fixed saves; history; v7→{SCHEMA_VERSION} preservation")
    print("Evidence:", ROOT)


if __name__ == "__main__":
    asyncio.run(check())
