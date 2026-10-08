import asyncio
import json
from concurrent.futures import CancelledError
from datetime import date, timedelta
from threading import Event
from uuid import uuid4

import pytest

from app.ai.messages import AssistantMessage, TextContent, ToolCall, ToolResultMessage
from app.domain.business.errors import WorkoutConfirmationInvalid
from app.domain.business.models import (
    WorkoutContent, WorkoutListArguments, WorkoutProposalArguments, WorkoutSaveArguments,
)
from app.domain.session.models import SendCommand, SendRequest
from app.infrastructure.persistence.sqlite import database as database_module
from app.interfaces.http import app
from test.check_business_profile import _StatementSync, _paused_save, _regenerate
from test.check_profile_confirmation import (
    Fixture, SYSTEM, USAGE, assert_rejected, new_id, now_ms, payload, propose,
)
from test.regression_support import Server, client, temporary_root

ROOT = temporary_root("workout-service-http") / uuid4().hex
ROOT.mkdir()
CONTENT = {"exercises": [{"exercise_id": None, "name": "实际动作", "load_convention": None,
                           "sets": [{"reps": 10, "weight_kg": None, "duration_seconds": None}]}],
           "notes": None}


def assistant(name, call_id, arguments):
    return AssistantMessage(role="assistant", content=[ToolCall(type="toolCall", id=call_id,
        name=name, arguments=arguments)], api="openai-completions", provider="example",
        model="model-1", usage=USAGE, stop_reason="toolUse", timestamp=now_ms())


async def prepare(f, session, performed_on, base=None, content=None, *, tamper=False):
    outcome = await f.service.accept_send(SendCommand(operation_id=new_id(), session_id=session,
        request=SendRequest(text="整理实际训练")), system_message=SYSTEM)
    run = outcome.run
    args = WorkoutProposalArguments(performed_on=performed_on,
        base_workout_id=None if base is None else base.id,
        base_workout_version=None if base is None else base.version,
        payload=WorkoutContent.model_validate(content or CONTENT))
    call_id = new_id()
    source = await f.append(session, run.id, run.request_entry_id,
                            assistant("prepare_workout", call_id, args.model_dump()))
    context = f.context(session, run.request_entry_id, source)
    proposal = await f.business.prepare_workout(context, args)
    displayed = proposal.model_copy(update={"payload": proposal.payload.model_copy(
        update={"notes": "展示变更"})}) if tamper else proposal
    message = ToolResultMessage(role="toolResult", tool_call_id=call_id, tool_name="prepare_workout",
        content=[TextContent(type="text", text=displayed.model_dump_json())], is_error=False,
        timestamp=now_ms())
    display = await f.append(session, run.id, source, message)
    await f.business.bind_workout_display_entry(proposal.proposal_id, display)
    await f.service.finish_run(session, run.id, "completed")
    outcome = await f.service.accept_send(SendCommand(operation_id=new_id(), session_id=session,
        request=SendRequest(text="确认保存训练")), system_message=SYSTEM)
    run = outcome.run
    saving_source = await f.append(session, run.id, run.request_entry_id,
        AssistantMessage(role="assistant", content=[TextContent(type="text", text="保存已确认内容")],
            api="openai-completions", provider="example", model="model-1", usage=USAGE,
            stop_reason="stop", timestamp=now_ms()))
    await f.service.finish_run(session, run.id, "completed")
    context = f.context(session, run.request_entry_id, saving_source)
    arguments = WorkoutSaveArguments(proposal_id=proposal.proposal_id,
        display_entry_id=display, confirmation_entry_id=run.request_entry_id)
    return context, arguments, run.id


async def finish(f, context, run):
    await f.service.finish_run(context.session_id, run, "completed")


async def check_service(path):
    f = await Fixture(path).seeded()
    try:
        day = f.context(new_id(), new_id(), new_id()).business_date
        session = await f.session("训练新增")
        c, a, run = await prepare(f, session, day)
        assert (await f.business.get_workout_save_status(c, a.proposal_id)).status == "pending"
        assert (await f.business.list_workout_display_bindings(session))[a.display_entry_id] == a.proposal_id
        first = await f.business.save_workout(c, a)
        assert first.version == 1
        assert await f.business.save_workout(c, a) == first
        assert (await f.business.list_workout_confirmation_bindings(session))[a.confirmation_entry_id] == a.proposal_id
        await finish(f, c, run)
        other = await f.session("跨会话更新")
        c2, a2, run2 = await prepare(f, other, day, first, {**CONTENT, "notes": "新内容"})
        third = await f.session("并发旧依据")
        c3, a3, run3 = await prepare(f, third, day, first)
        second = await f.business.update_workout(c2, a2)
        assert second.id == first.id and second.version == 2 and second.created_at == first.created_at
        await assert_rejected("跨会话冲突", f.business.update_workout(c3, a3), "workout_version_conflict")
        assert (await f.business.get_workout_save_status(c3, a3.proposal_id)).status == "conflicted"
        assert (await f.business.get_workout_save_status(c, a.proposal_id)).result == first
        assert (await f.business.get_workout(first.id)).version == 2
        await assert_rejected("归属", f.business.get_workout_save_status(c3, a.proposal_id), "workout_access_denied")
        await finish(f, c2, run2)
        await finish(f, c3, run3)
        tomorrow = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        future = WorkoutProposalArguments(performed_on=tomorrow, base_workout_id=None,
            base_workout_version=None, payload=WorkoutContent.model_validate(CONTENT))
        await assert_rejected("未来日期", f.business.prepare_workout(c, future), "invalid_business_payload")
        invalid_id = WorkoutProposalArguments(performed_on=day, base_workout_id=first.id,
            base_workout_version=2, payload=WorkoutContent.model_validate({"exercises": [
                {**CONTENT["exercises"][0], "exercise_id": "missing"}], "notes": None}))
        await assert_rejected("目录ID", f.business.prepare_workout(c, invalid_id), "invalid_business_payload")
        yesterday = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
        recover_session = await f.session("恢复保存")
        cr, ar, rr = await prepare(f, recover_session, yesterday)
        await assert_rejected("工具类型", f.business.update_workout(cr, ar), "workout_confirmation_invalid")
        async with f.repository.transaction():
            await f.business._begin_workout_save(cr, ar, updating=False)
        await assert_rejected("processing", f.business.save_workout(cr, ar), "workout_save_processing")
        await f.restart()
        assert await f.repository.recover_interrupted_workout_saves() == 1
        assert await f.repository.recover_interrupted_workout_saves() == 0
        recovered = await f.business.save_workout(cr, ar)
        await finish(f, cr, rr)
        await f.service.delete_session(recover_session)
        assert await f.repository.get_workout_snapshot(ar.proposal_id) is None
        assert await f.repository.get_workout_save_record(ar.proposal_id) is not None
        assert await f.business.get_workout(recovered.id)
        await assert_rejected("删除后访问", f.business.get_workout_save_status(cr, ar.proposal_id), "session_not_found")
        # 已完成训练确认节点拒绝画像绑定，使用真实画像准备及消息路径。
        p = await propose(f, session, {"profile_id": 1, "base_profile_version": None, "payload": payload()})
        await assert_rejected("训练确认不可授权画像", f.service_save(p, confirmation=a.confirmation_entry_id),
                              "profile_confirmation_invalid")
        # 已完成画像确认节点拒绝训练绑定。
        saved_profile = await f.service_save(p)
        cp, ap, rp = await prepare(f, session, (date.fromisoformat(day) - timedelta(days=2)).isoformat())
        ap = ap.model_copy(update={"confirmation_entry_id": p["confirmation"]})
        await assert_rejected("画像确认不可授权训练", f.business.save_workout(cp, ap), "workout_confirmation_invalid")
        await finish(f, cp, rp)
        assert saved_profile.version == 1
        assert (await f.business.list_workouts(WorkoutListArguments())).total == 2
        return {"id": first.id, "latest": second.model_dump(), "fixed": first.model_dump(),
                "recovered": recovered.model_dump(), "day": day, "session": session}
    finally:
        await f.close()


async def check_constraints():
    f = await Fixture(ROOT / "constraints.db").seeded()
    try:
        day = f.context(new_id(), new_id(), new_id()).business_date
        session = await f.session("内容与绑定约束")
        exercise_id = next(iter(f.catalog.exercise_ids))
        profile = await propose(f, session, {"profile_id": 1, "base_profile_version": None,
            "payload": payload(forbidden_exercise_ids=[exercise_id])})
        await f.service_save(profile)
        actual = {"exercises": [{**CONTENT["exercises"][0], "exercise_id": exercise_id,
            "sets": [], "load_convention": None}], "notes": "真实受限动作"}
        c, a, _ = await prepare(f, session, day, content=actual)
        c2, a2, _ = await prepare(f, session, day, content=actual)
        assert (await f.business.get_workout_save_status(c, a.proposal_id)).status == "invalidated"
        await assert_rejected("旧pending失效", f.business.save_workout(c, a), "workout_proposal_invalidated")
        yesterday = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
        c3, a3, _ = await prepare(f, session, yesterday)
        assert (await f.business.get_workout_save_status(c2, a2.proposal_id)).status == "pending"
        await assert_rejected("保存未来日期", f.business.save_workout(
            c2.model_copy(update={"business_date": yesterday}), a2), "invalid_business_payload")
        assert (await f.business.get_workout_save_status(c2, a2.proposal_id)).status == "pending"
        wrong = a2.model_copy(update={"display_entry_id": a3.display_entry_id})
        await assert_rejected("错误展示绑定", f.business.save_workout(c2, wrong), "workout_confirmation_invalid")
        wrong = a2.model_copy(update={"confirmation_entry_id": c2.source_entry_id})
        await assert_rejected("确认角色", f.business.save_workout(c2, wrong), "workout_confirmation_invalid")
        signal = Event()
        signal.set()
        with pytest.raises(CancelledError):
            await f.business.save_workout(c2, a2, signal)
        assert (await f.business.get_workout_save_status(c2, a2.proposal_id)).status == "pending"
        # 同一快照的真实并发调用返回固定结果或processing，始终单次写入。
        results = await asyncio.gather(f.business.save_workout(c2, a2),
            f.business.save_workout(c2, a2), return_exceptions=True)
        saved = await f.business.save_workout(c2, a2)
        for result in results:
            if isinstance(result, Exception):
                assert result.code == "workout_save_processing"
            else:
                assert result == saved
        assert saved.version == 1 and saved.content.exercises[0].exercise_id == exercise_id
        assert (await f.business.list_workouts(WorkoutListArguments())).total == 1
        stale = WorkoutProposalArguments(performed_on=day, base_workout_id=None,
            base_workout_version=None, payload=WorkoutContent.model_validate(CONTENT))
        await assert_rejected("准备依据冲突", f.business.prepare_workout(c3, stale), "workout_version_conflict")
        with pytest.raises(WorkoutConfirmationInvalid):
            await prepare(f, session, yesterday, tamper=True)
        return {"restricted_exercise_saved": exercise_id, "concurrent_version": saved.version,
                "cancelled_status": "pending", "invalidation": "same_session_same_date"}
    finally:
        await f.close()


async def check_replacement():
    f = await Fixture(ROOT / "replacement.db").seeded()
    try:
        day = f.context(new_id(), new_id(), new_id()).business_date
        session = await f.session("提交替换交错")
        c, a, run = await prepare(f, session, day)
        snapshot = await f.repository.get_workout_snapshot(a.proposal_id)
        sync = _StatementSync("COMMIT", nth=2)
        async with _paused_save(f, sync, lambda: f.business.save_workout(c, a)) as task:
            async with f.replacements.register(session):
                replacement = asyncio.create_task(_regenerate(f, session, snapshot.request_entry_id))
                sync.release.set()
                outcome = await replacement
                await f.service.finish_run(session, outcome.run.id, "completed")
        await assert_rejected("替换优先", task, "workout_proposal_not_found")
        assert (await f.business.list_workouts(WorkoutListArguments())).total == 0
        assert await f.repository.get_workout_save_record(a.proposal_id) is None
        c, a, run = await prepare(f, session, day)
        saved = await f.business.save_workout(c, a)
        snapshot = await f.repository.get_workout_snapshot(a.proposal_id)
        async with f.replacements.register(session):
            outcome = await _regenerate(f, session, snapshot.request_entry_id)
            await f.service.finish_run(session, outcome.run.id, "completed")
        assert await f.repository.get_workout_snapshot(a.proposal_id) is None
        assert await f.business.save_workout(c, a) == saved
        assert (await f.business.list_workout_confirmation_bindings(session))[a.confirmation_entry_id] == a.proposal_id
        assert (await f.business.get_workout_save_status(c, a.proposal_id)).result == saved
        return {"replacement_first": "workout_proposal_not_found", "commit_first_version": saved.version}
    finally:
        await f.close()


def check_http(path, evidence):
    database_module.default_database_path = lambda: path
    with Server(app) as server, client(server.base_url) as http:
        response = http.get("/api/workouts")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["page_size"] == 10 and body["total"] == 2
        assert body["items"][0]["version"] == 2
        response = http.get("/api/workouts/" + evidence["id"])
        assert response.status_code == 200 and response.json()["content"]["notes"] == "新内容"
        for query in ["page=0", "page=true", "page=1.5", "page_size=101", "date_from=2026-02-30",
                      "date_from=2026-12-31&date_to=2026-01-01", "unknown=1", "page=1&page=2"]:
            response = http.get("/api/workouts?" + query)
            assert response.status_code == 422, (query, response.text)
            assert response.headers["cache-control"] == "no-store"
        assert http.get("/api/workouts/not-a-uuid").status_code == 422
        assert http.get("/api/workouts/" + new_id()).json()["detail"]["code"] == "workout_not_found"
        assert http.get("/api/workouts/" + evidence["id"] + "?unknown=1").status_code == 422
        exact = http.get("/api/workouts", params={"date_from": evidence["day"], "date_to": evidence["day"]})
        assert exact.json()["total"] == 1
        assert http.get("/api/workouts?page=10").json()["items"] == []
        assert http.get("/api/workouts", headers={"Host": "invalid.example"}).status_code == 403
        # 真实数据库的合法JSON、非法业务schema经读取产生500。
        async def corrupt():
            repository = app.state.business._repository
            async with repository.transaction():
                await repository._write("UPDATE workouts SET content = ? WHERE id = ?",
                                        ('{"exercises": [], "notes": null}', evidence["id"]))
        asyncio.run_coroutine_threadsafe(corrupt(), app.state.loop).result()
        response = http.get("/api/workouts/" + evidence["id"])
        assert response.status_code == 500 and response.json()["detail"]["code"] == "internal_error"
        assert response.headers["cache-control"] == "no-store"
        # 隔离测试库在真实事务中恢复，供独立复核。
        async def restore():
            repository = app.state.business._repository
            async with repository.transaction():
                await repository._write("UPDATE workouts SET content = ? WHERE id = ?",
                    (json.dumps(evidence["latest"]["content"]), evidence["id"]))
        asyncio.run_coroutine_threadsafe(restore(), app.state.loop).result()


def check():
    path = ROOT / "service.db"
    evidence = asyncio.run(check_service(path))
    evidence["replacement"] = asyncio.run(check_replacement())
    evidence["constraints"] = asyncio.run(check_constraints())
    check_http(path, evidence)
    (ROOT / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS: real workout service insert/update/conflict/idempotency/catalog/date/bindings/recovery/cleanup/cross-business confirmation; real HTTP query/422/404/500/no-store/boundary")
    print("Evidence:", ROOT)


if __name__ == "__main__":
    check()
