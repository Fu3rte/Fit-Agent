import asyncio
import copy
import json
import sqlite3
from concurrent.futures import CancelledError
from datetime import date, timedelta
from threading import Event
from uuid import uuid4

import aiosqlite
import pytest
from pydantic import ValidationError

from app.ai.messages import TextContent, ToolResultMessage
from app.domain.business.errors import BusinessError, PlanConfirmationInvalid
from app.domain.business.models import (
    CurrentPlan,
    PlanContent,
    PlanDay,
    PlanExercise,
    PlanGetArguments,
    PlanProposalArguments,
    PlanRecord,
    PlanSaveArguments,
    PlanSaveRecord,
    PlanSaveResult,
    PlanSnapshot,
    PlanStatusArguments,
    PlanStatusResult,
    ProfileContent,
    WorkoutListArguments,
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
from test.check_business_profile import _paused_save, _regenerate, _StatementSync
from test.check_profile_confirmation import (
    SYSTEM,
    Fixture,
    assert_rejected,
    count_rows,
    new_id,
    now_ms,
    payload,
    propose,
)
from test.check_workout_service_http import assistant
from test.check_workout_service_http import prepare as prepare_workout
from test.check_workout_storage import make_record, raw, seed
from test.regression_support import temporary_root

ROOT = temporary_root("plan-core") / uuid4().hex
ROOT.mkdir()
EXERCISE = {"exercise_id": None, "name": "用户指定动作", "sets": 3, "reps": 10,
            "duration_seconds": None, "weight_kg": None, "load_convention": None, "rest_seconds": None}
CONTENT = {"repeat": True, "days": [
    {"kind": "training", "focus": "全身", "exercises": [EXERCISE], "notes": "目录外动作需核实；按动作规范选重"},
    {"kind": "rest", "focus": None, "exercises": [], "notes": None}],
    "notes": "依据已保存画像；结合恢复安排", "suggested_fields": ["/days", "/repeat", "/notes"]}


def check_models():
    cases = 0
    for field, valid, invalid in (
        ("sets", [None, 1], [0, -1, True, "3", 3.0]),
        ("reps", [None, 10], [0, -1, True, "10", 10.0]),
        ("duration_seconds", [None, 1, 1.5], [0, -1, True, "10", float("inf"), float("nan")]),
        ("weight_kg", [None, 0, 1.5], [-1, True, "10", float("inf"), float("nan")]),
        ("rest_seconds", [None, 0, 1.5], [-1, True, "10", float("inf"), float("nan")]),
    ):
        for value in valid:
            PlanExercise.model_validate({**EXERCISE, field: value, "load_convention": "added_weight"})
            cases += 1
        for value in invalid:
            with pytest.raises(ValidationError):
                PlanExercise.model_validate({**EXERCISE, field: value, "load_convention": "added_weight"})
            cases += 1
    for key in EXERCISE:
        with pytest.raises(ValidationError):
            PlanExercise.model_validate({k: v for k, v in EXERCISE.items() if k != key})
    for update in ({"extra": 1}, {"name": " "}, {"exercise_id": " "}, {"weight_kg": 0}, {"load_convention": "unknown"}):
        with pytest.raises(ValidationError):
            PlanExercise.model_validate({**EXERCISE, **update})
    for convention in ("per_implement", "barbell_total", "machine_display", "plates_total", "per_side", "added_weight", "assistance_weight"):
        PlanExercise.model_validate({**EXERCISE, "weight_kg": 0, "load_convention": convention})
    incomplete = {**CONTENT, "repeat": None, "days": [{"kind": "training", "focus": None, "exercises": [], "notes": None}]}
    PlanContent.model_validate(incomplete)
    for update in ({"days": []}, {"repeat": 1}, {"repeat": "true"}, {"extra": 1}, {"suggested_fields": [1]}):
        with pytest.raises(ValidationError):
            PlanContent.model_validate({**CONTENT, **update})
    with pytest.raises(ValidationError):
        PlanDay.model_validate({**CONTENT["days"][0], "kind": "rest"})
    for model, values in (
        (PlanGetArguments, {"plan_id": new_id()}),
        (PlanStatusArguments, {"proposal_id": new_id()}),
        (PlanSaveArguments, {"proposal_id": new_id(), "display_entry_id": new_id(), "confirmation_entry_id": new_id()}),
        (PlanProposalArguments, {"base_profile_version": 1, "base_plan_id": None, "payload": CONTENT}),
    ):
        model.model_validate(values)
        for key in values:
            with pytest.raises(ValidationError):
                model.model_validate({k: v for k, v in values.items() if k != key})
        with pytest.raises(ValidationError):
            model.model_validate({**values, "session_id": new_id()})
    for bad in [True, 0, -1, "1", 1.0, None]:
        with pytest.raises(ValidationError):
            PlanProposalArguments(base_profile_version=bad, base_plan_id=None, payload=CONTENT)
    for bad in ["invalid", uuid4().hex, "{" + new_id() + "}"]:
        with pytest.raises(ValidationError):
            PlanGetArguments(plan_id=bad)
    result = PlanSaveResult(proposal_id=new_id(), id=new_id(), content=CONTENT, created_at=10, saved_at=10)
    for status in ["pending", "processing", "invalidated", "conflicted"]:
        PlanStatusResult(proposal_id=result.proposal_id, status=status, result=None)
        with pytest.raises(ValidationError):
            PlanStatusResult(proposal_id=result.proposal_id, status=status, result=result)
    PlanStatusResult(proposal_id=result.proposal_id, status="saved", result=result)
    with pytest.raises(ValidationError):
        PlanStatusResult(proposal_id=new_id(), status="saved", result=result)
    with pytest.raises(ValidationError):
        PlanStatusResult(proposal_id=result.proposal_id, status="saved", result=None)
    with pytest.raises(ValidationError):
        PlanSaveResult(**{**result.model_dump(), "saved_at": 11})
    with pytest.raises(ValidationError):
        CurrentPlan(id=new_id(), content=None)
    assert "is_current" not in result.model_dump()
    return {"numeric_cases": cases, "incomplete_storage_content": True, "strict_required_fields": True}


async def prepare(f, session, *, content=None, base_profile_version=1, base_plan_id=None, tamper=False, direct_snapshot=False):
    accepted = await f.service.accept_send(SendCommand(operation_id=new_id(), session_id=session,
        request=SendRequest(text="生成完整计划")), system_message=SYSTEM)
    run = accepted.run
    args = PlanProposalArguments(base_profile_version=base_profile_version,
                                 base_plan_id=base_plan_id, payload=content if content is not None else CONTENT)
    call_id = new_id()
    source = await f.append(session, run.id, run.request_entry_id, assistant("prepare_plan", call_id, args.model_dump()))
    context = f.context(session, run.request_entry_id, source)
    display = None
    try:
        if direct_snapshot:
            snapshot = PlanSnapshot(**args.model_dump(), preparation_kind="generation", proposal_id=new_id(), session_id=session,
                request_entry_id=run.request_entry_id, source_entry_id=source, status="pending", created_at=now_ms())
            async with f.repository.transaction():
                await f.repository.insert_plan_snapshot(snapshot)
            proposal = f.business._plan_proposal(snapshot)
        else:
            proposal = await f.business.prepare_plan(context, args)
        displayed = proposal.model_copy(update={"base_profile_version": 99}) if tamper else proposal
        display = await f.append(session, run.id, source, ToolResultMessage(role="toolResult", tool_name="prepare_plan",
            tool_call_id=call_id, content=[TextContent(type="text", text=displayed.model_dump_json())],
            is_error=False, timestamp=now_ms()))
        await f.business.bind_plan_display_entry(proposal.proposal_id, display)
    except BusinessError as error:
        if display is None:
            await f.append(session, run.id, source, ToolResultMessage(role="toolResult", tool_name="prepare_plan",
                tool_call_id=call_id, content=[TextContent(type="text", text=json.dumps(error.detail(), ensure_ascii=False))],
                is_error=True, timestamp=now_ms()))
        raise
    finally:
        await f.service.finish_run(session, run.id, "completed")
    accepted = await f.service.accept_send(SendCommand(operation_id=new_id(), session_id=session,
        request=SendRequest(text="确认采纳计划")), system_message=SYSTEM)
    run = accepted.run
    arguments = PlanSaveArguments(proposal_id=proposal.proposal_id, display_entry_id=display,
                                  confirmation_entry_id=run.request_entry_id)
    saving = assistant("save_plan", new_id(), arguments.model_dump()).model_copy(update={
        "content": [TextContent(type="text", text="处理用户确认")], "stop_reason": "stop",
    })
    source = await f.append(session, run.id, run.request_entry_id, saving)
    await f.service.finish_run(session, run.id, "completed")
    return f.context(session, run.request_entry_id, source), arguments


async def profile(f, session, version=None, **updates):
    p = await propose(f, session, {"profile_id": 1, "base_profile_version": version, "payload": payload(**updates)})
    return p, await f.service_save(p)


async def check_service():
    f = await Fixture(ROOT / "service.db").seeded()
    try:
        session = await f.session("计划生成")
        assert (await f.business.get_current_plan()).model_dump() == {"id": None, "content": None}
        assert await f.business.list_plans() == []
        await assert_rejected("缺画像", prepare(f, session), "profile_required")
        p, saved_profile = await profile(f, session)
        c, a = await prepare(f, session)
        assert (await f.business.get_plan_save_status(c, a.proposal_id)).status == "pending"
        assert (await f.business.list_plan_display_bindings(session))[a.display_entry_id] == a.proposal_id
        await assert_rejected("错误展示", f.business.save_plan(c, a.model_copy(update={"display_entry_id": new_id()})), "plan_confirmation_invalid")
        await assert_rejected("错误角色", f.business.save_plan(c, a.model_copy(update={"confirmation_entry_id": c.source_entry_id})), "plan_confirmation_invalid")
        await assert_rejected("错误时序", f.business.save_plan(c, a.model_copy(update={"confirmation_entry_id": (await f.repository.get_plan_snapshot(a.proposal_id)).request_entry_id})), "plan_confirmation_invalid")
        signal = Event()
        signal.set()
        with pytest.raises(CancelledError):
            await f.business.save_plan(c, a, signal)
        first = await f.business.save_plan(c, a)
        assert first.content == PlanContent.model_validate(CONTENT) and first.created_at == first.saved_at
        assert await f.business.save_plan(c, a) == first
        assert (await f.business.get_plan(first.id)).is_current
        assert (await f.business.list_plan_confirmation_bindings(session))[a.confirmation_entry_id] == a.proposal_id
        assert (await f.business.get_plan_save_status(c, a.proposal_id)).result == first
        other = await f.session("其他会话")
        wrong = c.model_copy(update={"session_id": other})
        await assert_rejected("归属", f.business.save_plan(wrong, a), "plan_access_denied")
        await assert_rejected("归属状态", f.business.get_plan_save_status(wrong, a.proposal_id), "plan_access_denied")
        await assert_rejected("未知版本", f.business.get_plan(new_id()), "plan_not_found")
        await assert_rejected("未知快照", f.business.get_plan_save_status(c, new_id()), "plan_proposal_not_found")
        await assert_rejected("准备计划冲突", prepare(f, other), "plan_version_conflict")
        await assert_rejected("准备画像冲突", prepare(f, other, base_profile_version=2, base_plan_id=first.id), "profile_version_conflict")
        c2, a2 = await prepare(f, other, base_plan_id=first.id, content={**CONTENT, "notes": "新版本"})
        stale_session = await f.session("并发旧依据")
        cs, ass = await prepare(f, stale_session, base_plan_id=first.id)
        outcomes = await asyncio.gather(f.business.save_plan(c2, a2), f.business.save_plan(c2, a2), return_exceptions=True)
        second = await f.business.save_plan(c2, a2)
        for result in outcomes:
            if isinstance(result, Exception):
                assert result.code == "plan_save_processing"
            else:
                assert result == second
        await assert_rejected("保存计划冲突", f.business.save_plan(cs, ass), "plan_version_conflict")
        assert (await f.business.get_plan_save_status(cs, ass.proposal_id)).status == "conflicted"
        assert not (await f.business.get_plan(first.id)).is_current
        assert (await f.business.get_plan(second.id)).is_current
        assert await f.business.save_plan(c, a) == first
        plans = await f.business.list_plans()
        assert len(plans) == 2 and sum(item.is_current for item in plans) == 1
        assert [(x.created_at, x.id) for x in plans] == sorted([(x.created_at, x.id) for x in plans], reverse=True)
        assert (await f.business.get_profile()).version == 1
        cp, ap = await prepare(f, session, base_plan_id=second.id)
        await profile(f, session, 1, health_notes="补充信息")
        await assert_rejected("保存画像冲突", f.business.save_plan(cp, ap), "profile_version_conflict")
        assert (await f.business.get_plan_save_status(cp, ap.proposal_id)).status == "conflicted"
        ci, ai = await prepare(f, session, base_profile_version=2, base_plan_id=second.id)
        cn, an = await prepare(f, session, base_profile_version=2, base_plan_id=second.id)
        await assert_rejected("旧pending", f.business.save_plan(ci, ai), "plan_proposal_invalidated")
        assert (await f.business.get_plan_save_status(cn, an.proposal_id)).status == "pending"
        with pytest.raises(PlanConfirmationInvalid):
            await prepare(f, session, base_profile_version=2, base_plan_id=second.id, tamper=True)
        await f.restart()
        assert await f.business.save_plan(c, a) == first
        assert (await f.business.get_plan_save_status(c2, a2.proposal_id)).result == second
        return {"versions": 2, "current": second.id, "fixed_original": first.id, "profile_conflict": True,
                "ownership": True, "invalidation": True, "strict_display": True, "concurrent_single_save": True}
    finally:
        await f.close()


async def check_constraints_and_cross_business():
    f = await Fixture(ROOT / "constraints.db").seeded()
    try:
        session = await f.session()
        p, _ = await profile(f, session)
        exercise = f.catalog.all()[0]
        real = copy.deepcopy(CONTENT)
        real["days"][0]["exercises"][0].update(exercise_id=exercise.id, name=exercise.name)
        invalids = []
        for field in ["sets", "reps"]:
            content = copy.deepcopy(CONTENT)
            content["days"][0]["exercises"][0][field] = None
            invalids.append((content, "/payload/days/0/exercises/0/" + field))
        content = copy.deepcopy(CONTENT)
        content["days"][0]["exercises"] = []
        invalids.append((content, "/payload/days/0/exercises"))
        content = copy.deepcopy(CONTENT)
        content["days"][0]["exercises"][0]["exercise_id"] = "unknown"
        invalids.append((content, "/payload/days/0/exercises/0/exercise_id"))
        for content, path in invalids:
            error = await assert_rejected("生成约束", prepare(f, session, content=content), "invalid_business_payload")
            assert path in [item["path"] for item in error["errors"]]
        valid_duration = copy.deepcopy(real)
        valid_duration["days"][0]["exercises"][0].update(reps=None, duration_seconds=30)
        c, a = await prepare(f, session, content=valid_duration)
        await assert_rejected("画像确认授权计划", f.business.save_plan(c, a.model_copy(update={"confirmation_entry_id": p["confirmation"]})), "plan_confirmation_invalid")
        day = c.business_date
        wc, wa, _ = await prepare_workout(f, session, day)
        wr = await f.business.save_workout(wc, wa)
        await assert_rejected("训练确认授权计划", f.business.save_plan(c, a.model_copy(update={"confirmation_entry_id": wa.confirmation_entry_id})), "plan_confirmation_invalid")
        result = await f.business.save_plan(c, a)
        pp = await propose(f, session, {"profile_id": 1, "base_profile_version": 1, "payload": payload()})
        await assert_rejected("计划确认授权画像", f.service_save(pp, confirmation=a.confirmation_entry_id), "profile_confirmation_invalid")
        await f.service_save(pp)
        wc2, wa2, _ = await prepare_workout(f, session, day, wr)
        await assert_rejected("计划确认授权训练", f.business.update_workout(wc2, wa2.model_copy(update={"confirmation_entry_id": a.confirmation_entry_id})), "workout_confirmation_invalid")
        # pending/processing 的跨业务确认关联在共享事务内检查。
        pc, pa = await prepare(f, session, base_profile_version=2, base_plan_id=result.id, content=real)
        async with f.repository.transaction():
            await f.business._begin_plan_save(pc, pa)
        await assert_rejected("processing", f.business.save_plan(pc, pa), "plan_save_processing")
        await assert_rejected("processing计划确认授权训练", f.business.update_workout(wc2, wa2.model_copy(update={"confirmation_entry_id": pa.confirmation_entry_id})), "workout_confirmation_invalid")
        await f.restart()
        assert await f.repository.recover_interrupted_plan_saves() == 1
        assert await f.repository.recover_interrupted_plan_saves() == 0
        recovered = await f.business.save_plan(pc, pa)
        await profile(f, session, 2, forbidden_exercise_ids=[exercise.id])
        error = await assert_rejected("禁用动作", prepare(f, session, base_profile_version=3, base_plan_id=recovered.id, content=real), "invalid_business_payload")
        assert error["errors"][0]["path"] == "/payload/days/0/exercises/0/exercise_id"
        await profile(f, session, 3, unavailable_equipment=[exercise.equipment])
        await assert_rejected("不可用器械", prepare(f, session, base_profile_version=4, base_plan_id=recovered.id, content=real), "invalid_business_payload")
        assert await f.business.get_workout(wr.id) == await f.repository.get_workout(wr.id)
        assert (await f.repository.get_workout_save_record(wa.proposal_id)).result == wr
        # 首次保存重新校验目录与完整度，使用当前库中不可修改快照的真实内容。
        for content, expected_path in [*invalids, (real, "/payload/days/0/exercises/0/exercise_id")]:
            bc, ba = await prepare(f, session, base_profile_version=4, base_plan_id=recovered.id,
                                   content=content, direct_snapshot=True)
            error = await assert_rejected("首次保存重验", f.business.save_plan(bc, ba), "invalid_business_payload")
            assert expected_path in [item["path"] for item in error["errors"]]
            assert (await f.business.get_current_plan()).id == recovered.id
            assert await f.repository.get_plan_save_record(ba.proposal_id) is None
        return {"real_catalog_id": exercise.id, "equipment": exercise.equipment, "cross_business_directions": 4,
                "processing_binding_protected": True, "recovered_once": True, "workout_preserved": wr.id}
    finally:
        await f.close()


async def check_migration_and_store():
    path = ROOT / "migration.db"
    connection = await aiosqlite.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    await connection.execute("PRAGMA foreign_keys = ON")
    for version in range(1, 7):
        await apply_schema(connection, _MIGRATIONS[version].read_text(encoding="utf-8"), version)
    db = Database(connection)
    repo = SqliteBusinessRepository(db)
    ids = await seed(db)
    workout = make_record("2026-06-01")
    from app.application.business.catalog import Catalog
    async with repo.transaction():
        await repo.save_profile(ProfileContent.model_validate(payload()), 1, now_ms())
        await repo.insert_workout(workout)
        await repo.replace_exercises(Catalog.load().all())
    tables = ["profile", "workouts", "exercises", "sessions", "session_entries", "session_runs", "profile_snapshots", "profile_save_records", "workout_snapshots", "workout_save_records"]
    async def rows(database, table):
        cursor = await database.connection.execute(f"SELECT * FROM {table} ORDER BY 1")
        values = [tuple(row) for row in await cursor.fetchall()]
        await cursor.close()
        return values
    before = {table: await rows(db, table) for table in tables}
    await db.close()
    db = await open_database(path)
    repo = SqliteBusinessRepository(db)
    try:
        assert (await rows(db, "pragma_user_version")) == [(SCHEMA_VERSION,)]
        for table in tables:
            assert await rows(db, table) == before[table]
        content = PlanContent.model_validate(CONTENT)
        record = PlanRecord(id=new_id(), is_current=True, content=content, created_at=now_ms())
        snap = PlanSnapshot(preparation_kind="generation", proposal_id=new_id(), session_id=ids["session"], request_entry_id=ids["request"],
            source_entry_id=ids["source"], base_profile_version=1, base_plan_id=None, payload=content,
            status="pending", created_at=now_ms())
        await repo.insert_plan_snapshot(snap)
        for column, value in [("payload", "{}"), ("base_profile_version", 2), ("base_plan_id", new_id())]:
            with pytest.raises(sqlite3.IntegrityError):
                await raw(db, f"UPDATE plan_snapshots SET {column} = ? WHERE proposal_id = ?", (value, snap.proposal_id))
        with pytest.raises(sqlite3.IntegrityError):
            await repo.bind_plan_display_entry(snap.proposal_id, ids["source"])
        await repo.bind_plan_display_entry(snap.proposal_id, ids["display"])
        with pytest.raises(sqlite3.IntegrityError):
            await repo.begin_plan_save(snap.proposal_id, ids["display"])
        await repo.begin_plan_save(snap.proposal_id, ids["confirmation"])
        second_snap = snap.model_copy(update={"proposal_id": new_id(), "display_entry_id": ids["display"]})
        await repo.insert_plan_snapshot(second_snap)
        with pytest.raises(sqlite3.IntegrityError):
            await repo.begin_plan_save(second_snap.proposal_id, ids["confirmation"])
        saved = PlanSaveResult(proposal_id=snap.proposal_id, id=record.id, content=content,
                               created_at=record.created_at, saved_at=record.created_at)
        fixed = PlanSaveRecord(proposal_id=snap.proposal_id, session_id=ids["session"], display_entry_id=ids["display"],
            confirmation_entry_id=ids["confirmation"], result=saved, saved_at=saved.saved_at)
        with pytest.raises(RuntimeError, match="事务中断"):
            async with repo.transaction():
                await repo.insert_plan(record)
                await repo.complete_plan_save(fixed)
                raise RuntimeError("事务中断")
        assert await repo.get_current_plan() is None and await repo.get_plan_save_record(snap.proposal_id) is None
        assert (await repo.get_plan_snapshot(snap.proposal_id)).status == "processing"
        async with repo.transaction():
            await repo.insert_plan(record)
            await repo.complete_plan_save(fixed)
        with pytest.raises(sqlite3.IntegrityError):
            await raw(db, "UPDATE plans SET content = '{}' WHERE id = ?", (record.id,))
        with pytest.raises(sqlite3.IntegrityError):
            await raw(db, "UPDATE plan_save_records SET result = '{}' WHERE proposal_id = ?", (snap.proposal_id,))
        with pytest.raises(sqlite3.IntegrityError):
            await raw(db, "INSERT INTO plans VALUES (?, 1, ?, ?)", (new_id(), content.model_dump_json(), now_ms()))
        with pytest.raises(sqlite3.IntegrityError):
            await repo.insert_plan(record)
        assert await repo.get_current_plan() == record
        with pytest.raises(RuntimeError, match="事务中断"):
            async with repo.transaction():
                await repo.insert_plan(record.model_copy(update={"id": new_id()}))
                raise RuntimeError("事务中断")
        assert await repo.get_current_plan() == record
        history = record.model_copy(update={"id": new_id(), "is_current": False})
        await repo.insert_plan(history)
        assert [x.id for x in await repo.list_plans()] == sorted([record.id, history.id], reverse=True)
        assert await repo.find_plan_save_record_by_confirmation(ids["session"], ids["confirmation"]) == fixed
        assert await repo.find_plan_snapshot_by_confirmation(ids["session"], ids["confirmation"]) is not None
        await repo.delete_plan_snapshots_for_entries(ids["session"], {ids["display"]})
        await repo.delete_plan_snapshots_for_session(ids["session"])
        assert await repo.get_plan_snapshot(snap.proposal_id) is None
        assert await repo.get_plan_save_record(snap.proposal_id) == fixed
        assert await repo.get_workout(workout.id) == workout
        with pytest.raises(sqlite3.OperationalError):
            await apply_schema(db.connection, "CREATE TABLE migration_guard (id INTEGER); SELECT * FROM absent_table;", 8)
        assert await rows(db, "plans")
        await db.close()
        db = await open_database(path)
        repo = SqliteBusinessRepository(db)
        assert await repo.get_plan_save_record(snap.proposal_id) == fixed
        assert await repo.get_current_plan() == record
        assert await rows(db, "profile") == before["profile"]
        cursor = await db.connection.execute("PRAGMA foreign_key_check")
        assert await cursor.fetchall() == []
        await cursor.close()
        return {"migration": f"6→{SCHEMA_VERSION}", "catalog_rows_preserved": len(before["exercises"]), "tables_preserved": tables,
                "atomic_rollback": True, "unique_current": True, "immutable_history": True, "reopen_verified": True}
    finally:
        await db.close()


async def check_replacement_and_delete():
    f = await Fixture(ROOT / "replacement.db").seeded()
    try:
        session = await f.session()
        await profile(f, session)
        c, a = await prepare(f, session)
        snap = await f.repository.get_plan_snapshot(a.proposal_id)
        sync = _StatementSync("COMMIT", nth=2)
        async with _paused_save(f, sync, lambda: f.business.save_plan(c, a)) as task:
            async with f.replacements.register(session):
                replacement = asyncio.create_task(_regenerate(f, session, snap.request_entry_id))
                sync.release.set()
                outcome = await replacement
                await f.service.finish_run(session, outcome.run.id, "completed")
        await assert_rejected("替换优先", task, "plan_proposal_not_found")
        assert await f.business.list_plans() == []
        c, a = await prepare(f, session)
        first = await f.business.save_plan(c, a)
        outcome = await _regenerate(f, session, a.confirmation_entry_id)
        await f.service.finish_run(session, outcome.run.id, "completed")
        assert await f.business.save_plan(c, a) == first
        assert (await f.business.list_plan_confirmation_bindings(session))[a.confirmation_entry_id] == a.proposal_id
        snap = await f.repository.get_plan_snapshot(a.proposal_id)
        async with f.replacements.register(session):
            outcome = await _regenerate(f, session, snap.request_entry_id)
            await f.service.finish_run(session, outcome.run.id, "completed")
        assert await f.repository.get_plan_snapshot(a.proposal_id) is None
        assert await f.business.save_plan(c, a) == first
        assert (await f.business.get_plan_save_status(c, a.proposal_id)).result == first
        await f.service.delete_session(session)
        assert await f.repository.get_plan_save_record(a.proposal_id) is not None
        assert (await f.business.get_current_plan()).id == first.id
        await assert_rejected("删除后", f.business.get_plan_save_status(c, a.proposal_id), "session_not_found")
        return {"replacement_first": "plan_proposal_not_found", "commit_first_retained": True,
                "regenerated_confirmation_locates_original": True, "session_delete_retains_plan_and_fixed_result": True}
    finally:
        await f.close()


async def read_tables(database, statements):
    values = {}
    for statement in statements:
        async def read(connection, statement=statement):
            cursor = await connection.execute(statement, ())
            rows = [tuple(item) for item in await cursor.fetchall()]
            await cursor.close()
            return rows
        values[statement] = await database.read(read)
    return values


async def check_preservation_and_cancellation():
    """计划保存对画像、全部训练记录及保存幂等记录的保持；取消与中断不产生版本；重启后完整核对。"""
    preserved = [
        "SELECT * FROM profile ORDER BY version",
        "SELECT * FROM workouts ORDER BY id",
        "SELECT * FROM workout_save_records ORDER BY proposal_id",
        "SELECT * FROM exercises ORDER BY id",
    ]
    f = await Fixture(ROOT / "preservation.db").seeded()
    try:
        session = await f.session("计划保持与取消")
        assert await count_rows(f.database, "plan_snapshots") == 0
        assert await count_rows(f.database, "plans") == 0
        assert await count_rows(f.database, "plan_save_records") == 0
        await profile(f, session)
        day = f.context(session, new_id(), new_id()).business_date
        workouts = []
        for offset in range(3):
            performed = (date.fromisoformat(day) - timedelta(days=offset)).isoformat()
            workout_context, workout_arguments, _ = await prepare_workout(f, session, performed)
            workouts.append((await f.business.save_workout(workout_context, workout_arguments)).model_dump(
                exclude={"proposal_id", "saved_at"}))
        baseline = await read_tables(f.database, preserved)
        c, a = await prepare(f, session)
        signal = Event()
        signal.set()
        with pytest.raises(CancelledError):
            await f.business.save_plan(c, a, signal)
        assert await count_rows(f.database, "plans") == 0
        assert await count_rows(f.database, "plan_save_records") == 0
        assert (await f.repository.get_plan_snapshot(a.proposal_id)).status == "pending"
        first = await f.business.save_plan(c, a)
        assert await count_rows(f.database, "plans") == 1
        assert await read_tables(f.database, preserved) == baseline

        c2, a2 = await prepare(f, session, base_plan_id=first.id, content={**CONTENT, "notes": "第二版"})
        c3, a3 = await prepare(f, session, base_plan_id=first.id, content={**CONTENT, "notes": "第三版"})
        assert (await f.repository.get_plan_snapshot(a2.proposal_id)).status == "invalidated"
        second = await f.business.save_plan(c3, a3)
        assert (await f.business.get_current_plan()).id == second.id
        assert len(await f.business.list_plans()) == 2
        assert sum(item.is_current for item in await f.business.list_plans()) == 1
        assert await read_tables(f.database, preserved) == baseline
        displays = {
            "profile": await f.business.list_display_bindings(session),
            "workout": await f.business.list_workout_display_bindings(session),
            "plan": await f.business.list_plan_display_bindings(session),
        }
        keys = [set(item) for item in displays.values()]
        assert not keys[0] & keys[1] and not keys[0] & keys[2] and not keys[1] & keys[2]
        # 失效快照的展示节点退出待确认集合，不能再被用作保存授权。
        invalidated_display = (await f.repository.get_plan_snapshot(a2.proposal_id)).display_entry_id
        assert invalidated_display not in displays["plan"]
        confirmations = {
            "profile": await f.business.list_confirmation_bindings(session),
            "workout": await f.business.list_workout_confirmation_bindings(session),
            "plan": await f.business.list_plan_confirmation_bindings(session),
        }
        shared = [set(item) for item in confirmations.values()]
        assert not shared[0] & shared[1] and not shared[0] & shared[2] and not shared[1] & shared[2]
        assert confirmations["plan"][a3.confirmation_entry_id] == a3.proposal_id

        f = await f.restart()
        assert await f.business.save_plan(c, a) == first
        assert (await f.business.get_plan_save_status(c, a.proposal_id)).result == first
        await assert_rejected("失效快照重放", f.business.save_plan(c2, a2), "plan_proposal_invalidated")
        assert (await f.business.get_plan_save_status(c2, a2.proposal_id)).status == "invalidated"
        assert await read_tables(f.database, preserved) == baseline
        assert (await f.repository.get_plan_save_record(a.proposal_id)).result == first
        assert (await f.repository.get_plan_save_record(a3.proposal_id)).result == second
        stored = await f.business.list_workouts(WorkoutListArguments(page_size=100))
        assert {item.model_dump()["id"]: item.model_dump() for item in stored.items} == {item["id"]: item for item in workouts}
        assert stored.total == len(workouts)
        async with f.database.connection.execute("PRAGMA foreign_key_check") as cursor:
            assert await cursor.fetchall() == []
        async with f.database.connection.execute("PRAGMA integrity_check") as cursor:
            assert [row[0] for row in await cursor.fetchall()] == ["ok"]
        return {"cancellation_writes_nothing": True, "workouts_preserved": len(workouts),
                "profile_and_catalog_preserved": True, "invalidated_display_excluded": True,
                "binding_maps_disjoint": True, "restart_fixed_results": [first.id, second.id],
                "sqlite_fk_and_integrity": True}
    finally:
        await f.close()


def check():
    evidence = {"models": check_models(), "migration_store": asyncio.run(check_migration_and_store()),
                "service": asyncio.run(check_service()), "constraints": asyncio.run(check_constraints_and_cross_business()),
                "replacement_delete": asyncio.run(check_replacement_and_delete()),
                "preservation": asyncio.run(check_preservation_and_cancellation())}
    (ROOT / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"PASS: plan strict models; migration 6→{SCHEMA_VERSION}; preserved profile/workout/catalog/session; SQLite rollback/current/history; real message binding; "
          "save/version/conflict/idempotency; cross-business confirmation; recovery/replacement/delete; cancellation writes nothing; "
          "profile/workouts/catalog preserved across plan saves and restart; binding maps disjoint; SQLite FK and integrity")
    print("Evidence:", ROOT)


if __name__ == "__main__":
    check()
