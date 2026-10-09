import asyncio
import copy
import json
import sqlite3
from datetime import date, timedelta
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
    WorkoutContent,
    WorkoutListArguments,
    WorkoutRecord,
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
    count_rows,
    new_id,
    now_ms,
)
from test.check_workout_service_http import assistant
from test.check_workout_storage import make_record, raw, seed

ROOT = Path(__file__).resolve().parents[2] / "tmp/backend-plan-import/checks" / uuid4().hex
ROOT.mkdir(parents=True)
# 调整计划后端专项独立证据目录，与已有录入专项证据分离。
EVIDENCE_ROOT = Path(__file__).resolve().parents[2] / "tmp/backend-plan-adjustment-verification" / uuid4().hex
EVIDENCE_ROOT.mkdir(parents=True)
INCOMPLETE = {"repeat": None, "days": [
    {"kind": "training", "focus": "推", "exercises": [], "notes": None},
    {"kind": "rest", "focus": None, "exercises": [], "notes": None}],
    "notes": None, "suggested_fields": []}
# 完整训练记录口径：多动作、逐组数据、非空重量口径、非空notes；用于最近十条完整性与最新版本核验。
FULL_WORKOUT = {
    "exercises": [
        {"exercise_id": None, "name": "杠铃卧推", "load_convention": "barbell_total",
         "sets": [{"reps": 8, "weight_kg": 40.0, "duration_seconds": None},
                  {"reps": 8, "weight_kg": 42.5, "duration_seconds": None},
                  {"reps": 6, "weight_kg": 45.0, "duration_seconds": None}]},
        {"exercise_id": None, "name": "平板支撑", "load_convention": None,
         "sets": [{"reps": None, "weight_kg": None, "duration_seconds": 60.0}]},
    ],
    "notes": "状态良好，最后一组接近力竭",
}


def full_record(performed_on, notes):
    stamp = now_ms()
    content = {**copy.deepcopy(FULL_WORKOUT), "notes": notes}
    return WorkoutRecord(id=new_id(), performed_on=performed_on, version=1,
                         content=WorkoutContent.model_validate(content),
                         created_at=stamp, updated_at=stamp)


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


async def check_recent_workouts():
    """真实 SQLite 与服务按固定十条参数取数：0/3/10/12 条返回 min(x, 10)，倒序且逐条完整；可信截止日排除未来。"""
    primary = await Fixture(ROOT / "recent10.db").seeded()
    try:
        session = await primary.session("最近十条")
        cutoff = primary.context(session, new_id(), new_id()).business_date
        day = date.fromisoformat(cutoff)
        arguments = WorkoutListArguments(date_from=None, date_to=cutoff, page=1, page_size=10)
        for count in (0, 3, 10, 12):
            f = await Fixture(ROOT / f"recent10-{count}.db").seeded()
            try:
                records = [make_record((day - timedelta(days=i)).isoformat()) for i in range(count)]
                # 逆序插入，排序结果只可能来自服务的 performed_on/ID 倒序，与写入顺序无关。
                for record in reversed(records):
                    await f.repository.insert_workout(record)
                result = await f.business.list_workouts(arguments)
                expected = sorted(records, key=lambda item: (item.performed_on, item.id), reverse=True)[:10]
                assert result.page == 1 and result.page_size == 10
                assert result.total == count and len(result.items) == min(count, 10)
                assert result.items == expected
                # 每条读取返回的全部 items 均为一训练日期的完整最新记录，逐字段等于独立读取。
                for item in result.items:
                    assert await f.business.get_workout(item.id) == item
            finally:
                await f.close()
        # 可信截止日过滤未来记录，绑定工具与服务返回完全一致。
        records = [make_record((day - timedelta(days=i)).isoformat()) for i in range(12)]
        future = make_record((day + timedelta(days=1)).isoformat())
        for record in records + [future]:
            await primary.repository.insert_workout(record)
        service_result = await primary.business.list_workouts(arguments)
        assert service_result.total == 12 and len(service_result.items) == 10
        assert all(item.performed_on <= cutoff for item in service_result.items)
        expected_ids = [item.id for item in sorted(records, key=lambda i: (i.performed_on, i.id), reverse=True)[:10]]
        assert [item.id for item in service_result.items] == expected_ids
        tool_message = await primary.invoke(
            "list_workouts",
            {"date_from": None, "date_to": cutoff, "page": 1, "page_size": 10},
            primary.context(session, new_id(), new_id()),
        )
        assert not tool_message.is_error, tool_message
        body = json.loads(tool_message.content[0].text)
        assert body["total"] == 12 and body["page"] == 1 and body["page_size"] == 10
        assert [item["id"] for item in body["items"]] == expected_ids
    finally:
        await primary.close()


async def check_training_fact_lifecycle():
    """待确认提案固定使用准备时的训练事实；采用新事实及修改式确认须重新准备并使旧 pending 失效、重新确认。"""
    f = await Fixture(ROOT / "lifecycle.db").seeded()
    try:
        session = await f.session("调整事实时效")
        await profile(f, session)
        version = (await f.business.get_profile()).version
        base_context, base_save = await prepare(f, session, "import", version=version, base=None)
        first = await f.business.save_plan(base_context, base_save)
        content_x = copy.deepcopy(INCOMPLETE)
        content_x["notes"] = "依据2026年训练记录第3组8次调整重量"
        prepare_context, prepare_save = await prepare(f, session, "adjustment", version=version,
                                                       base=first.id, content=content_x)
        snapshot = await f.repository.get_plan_snapshot(prepare_save.proposal_id)
        assert snapshot.status == "pending" and snapshot.payload.model_dump() == content_x
        assert snapshot.base_plan_id == first.id
        # 确认前新增训练：原提案内容与状态保持原值。
        workout = make_record(f.context(session, new_id(), new_id()).business_date)
        await f.repository.insert_workout(workout)
        after = await f.repository.get_plan_snapshot(prepare_save.proposal_id)
        assert after.status == "pending" and after.payload.model_dump() == content_x
        assert (await f.business.get_plan_save_status(prepare_context, prepare_save.proposal_id)).status == "pending"
        # 沿用原提案确认保存使用准备时固化的内容，忽略确认后新增的训练。
        saved_x = await f.business.save_plan(prepare_context, prepare_save)
        assert saved_x.content.model_dump() == content_x
        assert (await f.business.get_current_plan()).id == saved_x.id
        assert await f.repository.get_workout(workout.id) == workout
        # 采用新事实重新准备，旧 pending 快照失效，需对新提案再次确认。
        content_y = copy.deepcopy(INCOMPLETE)
        content_y["notes"] = "第一次整理的新提案"
        context_y, save_y = await prepare(f, session, "adjustment", version=version, base=saved_x.id, content=content_y)
        content_z = copy.deepcopy(INCOMPLETE)
        content_z["notes"] = "采用新增训练事实后的重新准备"
        context_z, save_z = await prepare(f, session, "adjustment", version=version, base=saved_x.id, content=content_z)
        assert (await f.repository.get_plan_snapshot(save_y.proposal_id)).status == "invalidated"
        assert (await f.repository.get_plan_snapshot(save_z.proposal_id)).status == "pending"
        await assert_rejected("旧pending失效", f.business.save_plan(context_y, save_y), "plan_proposal_invalidated")
        saved_z = await f.business.save_plan(context_z, save_z)
        assert saved_z.content.model_dump() == content_z
        assert (await f.business.get_current_plan()).id == saved_z.id
        assert await f.repository.get_workout(workout.id) == workout
        plans = await f.business.list_plans()
        assert len(plans) == 3 and sum(item.is_current for item in plans) == 1
    finally:
        await f.close()


async def check_recent_workouts_completeness():
    """固定十条取数的完整性与最新版本：真实 SQLite/仓库/服务/生产绑定工具，完整逐字段对象一致。"""
    f = await Fixture(EVIDENCE_ROOT / "recent-full.db").seeded()
    try:
        session = await f.session("最近十条完整性")
        cutoff = f.context(session, new_id(), new_id()).business_date
        day = date.fromisoformat(cutoff)
        arguments = WorkoutListArguments(date_from=None, date_to=cutoff, page=1, page_size=10)
        records = [full_record((day - timedelta(days=i)).isoformat(), notes=f"第{i}天完整记录")
                   for i in range(12)]
        # 逆序写入：返回排序只可能来自 performed_on、id 倒序，与写入顺序无关。
        for record in reversed(records):
            await f.repository.insert_workout(record)
        # 同一天的最新版本：更新既有记录，记录表只保留最新完整内容且版本递增。
        assert await f.repository.update_workout(records[0].id, 1,
            WorkoutContent.model_validate({**copy.deepcopy(FULL_WORKOUT), "notes": "更新后的最新记录"}),
            records[0].updated_at + 10)
        latest = await f.repository.get_workout(records[0].id)
        assert latest.version == 2 and latest.created_at == records[0].created_at
        assert latest.updated_at == records[0].updated_at + 10
        expected = sorted([latest, *records[1:]],
                          key=lambda item: (item.performed_on, item.id), reverse=True)[:10]
        result = await f.business.list_workouts(arguments)
        assert result.page == 1 and result.page_size == 10 and result.total == 12
        assert result.items == expected
        # 每条为完整最新记录：与实际 get_workout 完整对象逐字段相等。
        for item in result.items:
            assert await f.business.get_workout(item.id) == item
        top = result.items[0]
        assert top.id == latest.id and top.version == 2 and top.updated_at == latest.updated_at
        assert len(top.content.exercises) == 2
        assert top.content.exercises[0].load_convention == "barbell_total"
        assert [group.weight_kg for group in top.content.exercises[0].sets] == [40.0, 42.5, 45.0]
        assert [group.reps for group in top.content.exercises[0].sets] == [8, 8, 6]
        assert top.content.exercises[1].sets[0].duration_seconds == 60.0
        assert top.content.notes == "更新后的最新记录"
        # 截止日包含边界：未来记录被排除，total 保持截止日内实际总数。
        future = full_record((day + timedelta(days=1)).isoformat(), notes="未来记录")
        await f.repository.insert_workout(future)
        after = await f.business.list_workouts(arguments)
        assert after.total == 12 and len(after.items) == 10
        assert all(item.performed_on <= cutoff for item in after.items)
        assert future.id not in {item.id for item in after.items}
        # 生产绑定工具与服务返回完全一致。
        tool_message = await f.invoke("list_workouts",
            {"date_from": None, "date_to": cutoff, "page": 1, "page_size": 10},
            f.context(session, new_id(), new_id()))
        assert not tool_message.is_error, tool_message
        assert json.loads(tool_message.content[0].text) == after.model_dump()
        return {"total": after.total, "items": len(after.items), "latest_version": top.version,
                "boundary_inclusive": True}
    finally:
        await f.close()


async def check_weight_convention():
    """调整边界重量口径：重量有值（包括0）必须明确有效口径，无效口径在工具参数层被拒绝。"""
    f = await Fixture(EVIDENCE_ROOT / "weight-convention.db").seeded()
    try:
        session = await f.session("调整重量口径")
        await profile(f, session)
        version = (await f.business.get_profile()).version
        context = f.context(session, new_id(), new_id())

        def arguments(weight, convention):
            content = copy.deepcopy(CONTENT)
            content["days"][0]["exercises"][0].update(weight_kg=weight, load_convention=convention)
            return {"base_profile_version": version, "base_plan_id": None, "payload": content}

        before = await count_rows(f.database, "plan_snapshots")
        rejected = []
        for weight, convention in ((0, None), (2.5, None), (None, "unknown"), (0, "unknown")):
            message = await f.invoke("prepare_plan_adjustment", arguments(weight, convention), context)
            assert message.is_error, (weight, convention)
            assert "参数校验失败" in message.content[0].text
            rejected.append([weight, convention])
        assert await count_rows(f.database, "plan_snapshots") == before

        accepted = []
        for weight, convention in ((0, "added_weight"), (2.5, "barbell_total"), (None, "plates_total")):
            _, save = await prepare(f, session, "adjustment", version, base=None,
                                    content=arguments(weight, convention)["payload"])
            snapshot = await f.repository.get_plan_snapshot(save.proposal_id)
            exercise = snapshot.payload.days[0].exercises[0]
            assert exercise.weight_kg == weight and exercise.load_convention == convention
            accepted.append([exercise.weight_kg, exercise.load_convention])
        return {"rejected": rejected, "accepted": accepted}
    finally:
        await f.close()


async def prepare_adjustment_result(f, session, version, base):
    """真实回合：用户请求节点 -> 助手工具调用来源节点 -> 真实调整准备工具，不绑定展示，用于绑定拒绝核验。"""
    accepted = await f.service.accept_send(SendCommand(operation_id=new_id(), session_id=session,
        request=SendRequest(text="整理计划")), system_message=SYSTEM)
    run = accepted.run
    args = PlanAdjustmentArguments(base_profile_version=version, base_plan_id=base, payload=INCOMPLETE)
    call = new_id()
    source = await f.append(session, run.id, run.request_entry_id,
                            assistant("prepare_plan_adjustment", call, args.model_dump()))
    proposal = await f.business.prepare_plan_adjustment(
        f.context(session, run.request_entry_id, source), args)
    return run, source, call, proposal


async def check_display_binding_rejections():
    """展示绑定：结果节点身份、内容、路径与准备快照不一致时按契约拒绝调整计划保存授权。"""
    f = await Fixture(EVIDENCE_ROOT / "display-binding.db").seeded()
    try:
        base_session = await f.session("调整展示绑定基线")
        await profile(f, base_session)
        version = (await f.business.get_profile()).version
        bootstrap, bootstrap_save = await prepare(f, base_session, "import", version=version, base=None)
        base_plan = await f.business.save_plan(bootstrap, bootstrap_save)

        rejected = []
        for label, field, override in (
            ("调用标识不匹配", "tool_call_id", new_id()),
            ("工具名称不匹配", "tool_name", "prepare_plan"),
            ("错误结果节点", "is_error", True),
            ("展示内容不一致", "notes", "被篡改的展示内容"),
        ):
            session = await f.session(label)
            run, source, call, proposal = await prepare_adjustment_result(f, session, version, base_plan.id)
            body = proposal.model_dump()
            message = ToolResultMessage(role="toolResult", tool_name="prepare_plan_adjustment", tool_call_id=call,
                content=[TextContent(type="text", text=json.dumps(body))], is_error=False,
                timestamp=now_ms())
            if field == "notes":
                body["payload"]["notes"] = override
                message = message.model_copy(update={"content": [TextContent(type="text", text=json.dumps(body))]})
            elif field == "is_error":
                message = message.model_copy(update={"is_error": override})
            else:
                message = message.model_copy(update={field: override})
            display = await f.append(session, run.id, source, message)
            try:
                await assert_rejected(label, f.business.bind_plan_display_entry(proposal.proposal_id, display),
                                      "plan_confirmation_invalid")
            finally:
                await f.service.finish_run(session, run.id, "completed")
            assert (await f.repository.get_plan_snapshot(proposal.proposal_id)).display_entry_id is None
            rejected.append(label)
        # 非法来源：展示节点不在当前消息路径，或使用用户请求节点。
        for label, pick in (("节点不在路径", lambda run, source: new_id()),
                            ("来源为用户节点", lambda run, source: run.request_entry_id)):
            session = await f.session(label)
            run, source, _, proposal = await prepare_adjustment_result(f, session, version, base_plan.id)
            try:
                await assert_rejected(label, f.business.bind_plan_display_entry(proposal.proposal_id, pick(run, source)),
                                      "plan_confirmation_invalid")
            finally:
                await f.service.finish_run(session, run.id, "completed")
            assert (await f.repository.get_plan_snapshot(proposal.proposal_id)).display_entry_id is None
            rejected.append(label)
        return {"rejected": rejected}
    finally:
        await f.close()


async def check_payload_consistency():
    """准备payload、持久化展示节点payload与最终保存content一致，且固定保存结果可复核。"""
    f = await Fixture(EVIDENCE_ROOT / "payload-consistency.db").seeded()
    try:
        session = await f.session("调整payload一致性")
        content = copy.deepcopy(CONTENT)
        content["notes"] = "调整说明：第三组重量提升；依据2026-06-01训练记录第3组8次"
        context, save = await prepare(f, session, "adjustment", base=None, content=content)
        snapshot = await f.repository.get_plan_snapshot(save.proposal_id)
        display = await f.service.get_entry(session, save.display_entry_id)
        displayed = json.loads(display.messages[0].content[0].text)
        assert displayed["proposal_id"] == save.proposal_id
        saved = await f.business.save_plan(context, save)
        assert saved.content.model_dump() == snapshot.payload.model_dump() == displayed["payload"]
        assert saved.content.notes == content["notes"]
        status = await f.business.get_plan_save_status(context, save.proposal_id)
        assert status.status == "saved" and status.result == saved
        assert (await f.business.get_current_plan()).id == saved.id
        return {"proposal_id": save.proposal_id, "plan_id": saved.id}
    finally:
        await f.close()


async def check():
    check_schema()
    await check_business()
    await check_migration()
    await check_recent_workouts()
    await check_training_fact_lifecycle()
    evidence = {
        "recent_completeness": await check_recent_workouts_completeness(),
        "weight_convention": await check_weight_convention(),
        "display_binding": await check_display_binding_rejections(),
        "payload_consistency": await check_payload_consistency(),
    }
    (EVIDENCE_ROOT / "evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"PASS: strict import/adjustment schema; unknown values; real display/confirmation; nullable/version/plan conflicts; limits; immutable kinds; fixed saves; history; recent-10 fixed query min(x,10) descending full records trusted cutoff; prep-time training facts immutable and re-prepare invalidates old pending; v7→{SCHEMA_VERSION} preservation; recent-10 full latest record and inclusive boundary; adjustment weight convention; display-binding identity/path/content rejection; prepare-display-save payload consistency")
    print("Evidence:", ROOT)
    print("Adjustment evidence:", EVIDENCE_ROOT)


if __name__ == "__main__":
    asyncio.run(check())
