import asyncio
import json
from concurrent.futures import CancelledError
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest

from app.agent.tool import run_tool_batch, run_tool_call
from app.agent.tools.business import bind_business_tools, business_tool_declarations
from app.ai.messages import AssistantMessage, TextContent, ToolCall, ToolResultMessage
from app.application.business.service import business_date
from app.domain.business.models import (
    PendingProposalArguments,
    PlanContent,
    PlanImportArguments,
    PlanPendingProposal,
    PlanSaveArguments,
    ProfilePendingProposal,
    ProfileSaveArguments,
    WorkoutPendingProposal,
    WorkoutSaveArguments,
)
from app.domain.session.models import SendCommand, SendRequest
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from test.check_profile_confirmation import (
    SYSTEM,
    USAGE,
    Fixture,
    count_rows,
    envelope,
    message_text,
    new_id,
    now_ms,
    payload,
    propose,
)

ROOT = Path(__file__).resolve().parents[2] / "tmp/agent-pending-proposal" / uuid4().hex
ROOT.mkdir(parents=True)
DECLARED = {item.name: item for item in business_tool_declarations()}
TOOL = "get_pending_proposal"
PLAN_CONTENT = {
    "repeat": None,
    "days": [{"kind": "training", "focus": "推", "exercises": [], "notes": None}],
    "notes": None,
    "suggested_fields": [],
}
LONG_NOTES = "长计划完整返回核验；" * 8000
WORKOUT_CONTENT = {
    "exercises": [{"exercise_id": None, "name": "实际动作", "load_convention": None,
                   "sets": [{"reps": 10, "weight_kg": None, "duration_seconds": None}]}],
    "notes": None,
}


def tool_call(name, arguments, call_id=None):
    return ToolCall(type="toolCall", id=call_id or new_id(), name=name, arguments=arguments)


def assistant(name, call_id, arguments):
    return AssistantMessage(role="assistant", content=[ToolCall(type="toolCall", id=call_id,
        name=name, arguments=arguments)], api="openai-completions", provider="example",
        model="model-1", usage=USAGE, stop_reason="toolUse", timestamp=now_ms())


def assistant_text():
    return AssistantMessage(role="assistant", content=[TextContent(type="text", text="确认保存")],
        api="openai-completions", provider="example", model="model-1", usage=USAGE,
        stop_reason="stop", timestamp=now_ms())


def code_of(message) -> str:
    assert message.is_error, message_text(message)
    return envelope(message)["code"]


async def read(f, session, business_kind, proposal_id, *, signal=None):
    context = f.context(session, new_id(), new_id())
    tools = bind_business_tools(f.business, context, f.call, {}, {}, {})
    assert {name: tool.definition() for name, tool in tools.items()} == DECLARED
    return await run_tool_call(
        tool_call(TOOL, {"business_kind": business_kind, "proposal_id": proposal_id}),
        tools={TOOL: tools[TOOL]}, declared=DECLARED, signal=signal,
    )


async def prepare_via_tool(f, session, tool_name, arguments, binder):
    outcome = await f.service.accept_send(SendCommand(operation_id=new_id(), session_id=session,
        request=SendRequest(text="准备待确认内容")), system_message=SYSTEM)
    run = outcome.run
    call_id = new_id()
    source = await f.append(session, run.id, run.request_entry_id,
                            assistant(tool_name, call_id, arguments))
    context = f.context(session, run.request_entry_id, source)
    tools = bind_business_tools(f.business, context, f.call, {}, {}, {})
    assert {name: tool.definition() for name, tool in tools.items()} == DECLARED
    execution = await run_tool_batch([tool_call(tool_name, arguments, call_id)],
                                     tools=tools, declared=DECLARED)
    assert execution.failure is None
    message = execution.messages[0]
    assert not message.is_error, message_text(message)
    proposal = envelope(message)
    display = await f.append(session, run.id, source, message)
    await binder(proposal["proposal_id"], display)
    await f.service.finish_run(session, run.id, "completed")
    return {"session": session, "run": run.id, "request": run.request_entry_id,
            "source": source, "call": call_id, "display": display,
            "proposal": proposal["proposal_id"], "body": proposal}


async def save_profile(f, info):
    ids = {"session": info["session"], "request": info["confirmation"], "source": info["source"]}
    return await f.business.save_profile_update(
        f.save_context(ids),
        ProfileSaveArguments(proposal_id=info["proposal"], display_entry_id=info["display"],
                             confirmation_entry_id=info["confirmation"]),
    )


async def save_plan(f, session, info):
    outcome = await f.service.accept_send(SendCommand(operation_id=new_id(), session_id=session,
        request=SendRequest(text="确认保存计划")), system_message=SYSTEM)
    run = outcome.run
    source = await f.append(session, run.id, run.request_entry_id, assistant_text())
    context = f.context(session, run.request_entry_id, source)
    arguments = PlanSaveArguments(proposal_id=info["proposal"], display_entry_id=info["display"],
                                  confirmation_entry_id=run.request_entry_id)
    result = await f.business.save_plan(context, arguments)
    await f.service.finish_run(session, run.id, "completed")
    return context, arguments, result


async def save_workout(f, session, info):
    outcome = await f.service.accept_send(SendCommand(operation_id=new_id(), session_id=session,
        request=SendRequest(text="确认保存训练")), system_message=SYSTEM)
    run = outcome.run
    source = await f.append(session, run.id, run.request_entry_id, assistant_text())
    context = f.context(session, run.request_entry_id, source)
    arguments = WorkoutSaveArguments(proposal_id=info["proposal"], display_entry_id=info["display"],
                                     confirmation_entry_id=run.request_entry_id)
    result = await f.business.save_workout(context, arguments)
    await f.service.finish_run(session, run.id, "completed")
    return result


def check_declarations_and_binding():
    names = [item.name for item in business_tool_declarations()]
    assert set(names) == set(DECLARED)
    assert names.index(TOOL) == names.index("get_profile_update_status") + 1
    schema = DECLARED[TOOL].parameters
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"business_kind", "proposal_id"}
    assert set(schema["properties"]) == {"business_kind", "proposal_id"}
    assert schema["properties"]["business_kind"]["enum"] == ["profile", "plan", "workout"]


async def check_profile_reads(f):
    session = await f.session("待确认画像读取")
    info = await propose(f, session, {"profile_id": 1, "base_profile_version": None,
                                      "payload": payload()}, confirmation_text=None)
    snapshot = await f.repository.get_snapshot(info["proposal"])
    assert snapshot.status == "pending" and snapshot.display_entry_id == info["display"]

    message = await read(f, session, "profile", info["proposal"])
    assert not message.is_error, message_text(message)
    result = ProfilePendingProposal.model_validate(envelope(message))
    assert result.business_kind == "profile" and result.status == "pending"
    assert result.proposal_id == info["proposal"]
    assert result.request_entry_id == info["request"]
    assert result.source_entry_id == info["source"]
    assert result.display_entry_id == info["display"]
    assert result.confirmation_entry_id is None
    assert result.profile_id == 1 and result.base_profile_version is None
    assert result.payload.model_dump() == snapshot.payload.model_dump()
    assert set(envelope(message)) == {
        "business_kind", "proposal_id", "status", "payload", "request_entry_id",
        "source_entry_id", "display_entry_id", "confirmation_entry_id",
        "profile_id", "base_profile_version",
    }
    return session, info


async def check_plan_reads(f, version):
    session = await f.session("待确认计划读取")
    long_payload = dict(PLAN_CONTENT, notes=LONG_NOTES)
    info = await prepare_via_tool(f, session, "prepare_plan_import",
        {"base_profile_version": version, "base_plan_id": None, "payload": long_payload},
        f.business.bind_plan_display_entry)
    snapshot = await f.repository.get_plan_snapshot(info["proposal"])
    assert snapshot.status == "pending" and snapshot.display_entry_id == info["display"]
    assert snapshot.base_profile_version == version and snapshot.base_plan_id is None

    message = await read(f, session, "plan", info["proposal"])
    assert not message.is_error, message_text(message)
    assert len(message_text(message)) > 50000
    result = PlanPendingProposal.model_validate(envelope(message))
    assert result.business_kind == "plan" and result.status == "pending"
    assert result.preparation_kind == "import"
    assert result.base_profile_version == version and result.base_plan_id is None
    assert result.display_entry_id == info["display"] and result.confirmation_entry_id is None
    assert result.payload.model_dump() == snapshot.payload.model_dump()
    assert result.payload.notes == LONG_NOTES
    assert set(envelope(message)) == {
        "business_kind", "proposal_id", "status", "payload", "request_entry_id",
        "source_entry_id", "display_entry_id", "confirmation_entry_id",
        "preparation_kind", "base_profile_version", "base_plan_id",
    }
    return session, info


async def check_workout_reads(f):
    session = await f.session("待确认训练读取")
    day = business_date(now_ms())
    info = await prepare_via_tool(f, session, "prepare_workout",
        {"performed_on": day, "base_workout_id": None, "base_workout_version": None,
         "payload": WORKOUT_CONTENT}, f.business.bind_workout_display_entry)
    snapshot = await f.repository.get_workout_snapshot(info["proposal"])
    assert snapshot.status == "pending" and snapshot.display_entry_id == info["display"]

    message = await read(f, session, "workout", info["proposal"])
    assert not message.is_error, message_text(message)
    result = WorkoutPendingProposal.model_validate(envelope(message))
    assert result.business_kind == "workout" and result.status == "pending"
    assert result.performed_on == day
    assert result.base_workout_id is None and result.base_workout_version is None
    assert result.payload.model_dump() == snapshot.payload.model_dump()
    assert set(envelope(message)) == {
        "business_kind", "proposal_id", "status", "payload", "request_entry_id",
        "source_entry_id", "display_entry_id", "confirmation_entry_id",
        "performed_on", "base_workout_id", "base_workout_version",
    }
    return session, info


async def check_unknown_and_cross_session(f):
    session = await f.session("拒绝读取")
    other = await f.session("跨会话来源")
    info = await propose(f, other, {"profile_id": 1, "base_profile_version": None,
                                    "payload": payload()}, confirmation_text=None)

    assert code_of(await read(f, session, "profile", new_id())) == "profile_proposal_not_found"
    assert code_of(await read(f, session, "plan", new_id())) == "plan_proposal_not_found"
    assert code_of(await read(f, session, "workout", new_id())) == "workout_proposal_not_found"
    # 归属先于状态：跨会话读取以 access_denied 拒绝，归属会话自身读取成功。
    assert code_of(await read(f, session, "profile", info["proposal"])) == "profile_access_denied"
    assert not (await read(f, other, "profile", info["proposal"])).is_error
    assert code_of(await read(f, new_id(), "profile", info["proposal"])) == "session_not_found"


async def check_argument_rejections(f):
    session = await f.session("非法参数拒绝")
    context = f.context(session, new_id(), new_id())
    tools = bind_business_tools(f.business, context, f.call, {}, {}, {})
    before = await count_rows(f.database, "profile_snapshots")
    invalid = [
        {"business_kind": "session", "proposal_id": new_id()},
        {"business_kind": "profile", "proposal_id": "invalid"},
        {"business_kind": "profile", "proposal_id": uuid4().hex},
        {"business_kind": "profile"},
        {"proposal_id": new_id()},
        {"business_kind": "profile", "proposal_id": new_id(), "session_id": new_id()},
    ]
    calls = [tool_call(TOOL, item) for item in invalid]
    execution = await run_tool_batch(calls, tools={TOOL: tools[TOOL]}, declared=DECLARED)
    assert execution.failure is None
    assert [item.tool_call_id for item in execution.messages] == [call.id for call in calls]
    for message in execution.messages:
        assert message.is_error and "参数校验失败" in message_text(message)
    assert await count_rows(f.database, "profile_snapshots") == before


async def check_path_and_binding_rejections(f):
    session = await f.session("路径与绑定拒绝")
    unbound = await propose(f, session, {"profile_id": 1, "base_profile_version": None,
                                         "payload": payload()}, bind=False, confirmation_text=None)
    assert (await f.repository.get_snapshot(unbound["proposal"])).display_entry_id is None
    assert code_of(await read(f, session, "profile", unbound["proposal"])) == "profile_confirmation_invalid"

    # 路径外：真实提案与节点仍在库中，会话叶子回退到请求节点，来源与展示节点脱离当前有效路径。
    outside = await propose(f, session, {"profile_id": 1, "base_profile_version": None,
                                         "payload": payload()}, confirmation_text=None)
    sessions = SqliteSessionRepository(f.database)
    stored = await sessions.get_session(session)
    async with sessions.transaction():
        await sessions.update_session(
            stored.model_copy(update={"active_leaf_id": outside["request"]}))
    assert code_of(await read(f, session, "profile", outside["proposal"])) == "profile_confirmation_invalid"

    # 展示结果内容与快照不一致：真实准备后绕过服务绑定，指向篡改结果。
    outcome = await f.service.accept_send(SendCommand(operation_id=new_id(), session_id=session,
        request=SendRequest(text="准备计划")), system_message=SYSTEM)
    run = outcome.run
    call_id = new_id()
    arguments = PlanImportArguments(base_profile_version=None, base_plan_id=None,
                                    payload=PlanContent.model_validate(PLAN_CONTENT))
    source = await f.append(session, run.id, run.request_entry_id,
                            assistant("prepare_plan_import", call_id, arguments.model_dump()))
    proposal = await f.business.prepare_plan_import(
        f.context(session, run.request_entry_id, source), arguments)
    tampered_body = dict(proposal.model_dump(), payload=dict(PLAN_CONTENT, notes="篡改内容"))
    tampered = ToolResultMessage(role="toolResult", tool_call_id=call_id,
        tool_name="prepare_plan_import", content=[TextContent(type="text", text=json.dumps(
            tampered_body, ensure_ascii=False))], is_error=False, timestamp=now_ms())
    display = await f.append(session, run.id, source, tampered)
    async with f.repository.transaction():
        await f.repository.bind_plan_display_entry(proposal.proposal_id, display)
    await f.service.finish_run(session, run.id, "completed")
    assert code_of(await read(f, session, "plan", proposal.proposal_id)) == "plan_confirmation_invalid"


async def check_status_rejections(f):
    session = await f.session("非待确认状态拒绝")

    first = await propose(f, session, {"profile_id": 1, "base_profile_version": None,
                                       "payload": payload()}, confirmation_text=None)
    await propose(f, session, {"profile_id": 1, "base_profile_version": None,
                               "payload": payload(goal="减脂")}, confirmation_text=None)
    assert (await f.repository.get_snapshot(first["proposal"])).status == "invalidated"
    assert code_of(await read(f, session, "profile", first["proposal"])) == "profile_proposal_invalidated"

    processing = await propose(f, session, {"profile_id": 1, "base_profile_version": None,
                                            "payload": payload()}, confirmation_text=None)
    confirmation = await f.user_turn(session, "确认处理中的画像")
    async with f.repository.transaction():
        await f.repository.begin_save(processing["proposal"], confirmation)
    assert code_of(await read(f, session, "profile", processing["proposal"])) == "profile_update_processing"

    conflicted = await propose(f, session, {"profile_id": 1, "base_profile_version": None,
                                            "payload": payload()}, confirmation_text=None)
    async with f.repository.transaction():
        await f.repository.set_snapshot_status(conflicted["proposal"], "conflicted")
    assert code_of(await read(f, session, "profile", conflicted["proposal"])) == "profile_version_conflict"

    saved = await propose(f, session, {"profile_id": 1, "base_profile_version": None,
                                       "payload": payload()})
    result = await save_profile(f, saved)
    assert result.version >= 1
    assert code_of(await read(f, session, "profile", saved["proposal"])) == "proposal_already_saved"
    status = await f.business.get_profile_update_status(
        f.save_context({"session": session, "request": saved["confirmation"],
                        "source": saved["source"]}), saved["proposal"])
    assert status.status == "saved" and status.result == result

    plan_session = await f.session("计划已保存拒绝")
    plan = await prepare_via_tool(f, plan_session, "prepare_plan_import",
        {"base_profile_version": result.version, "base_plan_id": None,
         "payload": dict(PLAN_CONTENT)}, f.business.bind_plan_display_entry)
    _, plan_arguments, plan_result = await save_plan(f, plan_session, plan)
    repeated = await f.business.save_plan(
        f.context(plan_session, new_id(), new_id()), plan_arguments)
    assert repeated == plan_result
    assert code_of(await read(f, plan_session, "plan", plan["proposal"])) == "proposal_already_saved"

    workout_session = await f.session("训练已保存拒绝")
    day = business_date(now_ms())
    workout = await prepare_via_tool(f, workout_session, "prepare_workout",
        {"performed_on": day, "base_workout_id": None, "base_workout_version": None,
         "payload": WORKOUT_CONTENT}, f.business.bind_workout_display_entry)
    await save_workout(f, workout_session, workout)
    assert code_of(await read(f, workout_session, "workout", workout["proposal"])) == "proposal_already_saved"
    return plan_session, plan


async def check_cancellation(f):
    session = await f.session("取消传播")
    version = (await f.business.get_profile()).version
    info = await propose(f, session, {"profile_id": 1, "base_profile_version": version,
                                      "payload": payload()}, confirmation_text=None)
    arguments = PendingProposalArguments(business_kind="profile", proposal_id=info["proposal"])
    cancelled = Event()
    cancelled.set()
    with pytest.raises(CancelledError):
        await f.business.get_pending_proposal(
            f.context(session, new_id(), new_id()), arguments, signal=cancelled)
    # 取消先于读取：会话不存在时也以取消收尾。
    with pytest.raises(CancelledError):
        await f.business.get_pending_proposal(
            f.context(new_id(), new_id(), new_id()), arguments, signal=cancelled)
    aborted = await read(f, session, "profile", info["proposal"], signal=cancelled)
    assert aborted.is_error and "Operation aborted" in message_text(aborted)


async def check_readonly_invariant(f):
    session = await f.session("只读不变量")
    version = (await f.business.get_profile()).version
    info = await propose(f, session, {"profile_id": 1, "base_profile_version": version,
                                      "payload": payload()}, confirmation_text=None)
    tables = ("profile_snapshots", "plan_snapshots", "workout_snapshots", "profile",
              "plans", "workouts", "session_entries")
    before = {name: await count_rows(f.database, name) for name in tables}
    before_status = (await f.repository.get_snapshot(info["proposal"])).status

    for _ in range(3):
        message = await read(f, session, "profile", info["proposal"])
        assert not message.is_error, message_text(message)
    assert code_of(await read(f, session, "profile", new_id())) == "profile_proposal_not_found"

    after = {name: await count_rows(f.database, name) for name in tables}
    assert after == before, (before, after)
    assert (await f.repository.get_snapshot(info["proposal"])).status == before_status
    return before, after


async def check():
    f = await Fixture(ROOT / "pending.db").seeded()
    try:
        check_declarations_and_binding()
        await check_argument_rejections(f)
        profile_session, _ = await check_profile_reads(f)
        plan_session, _ = await check_plan_reads(f, (await f.business.get_profile()).version)
        workout_session, _ = await check_workout_reads(f)
        await check_unknown_and_cross_session(f)
        await check_path_and_binding_rejections(f)
        saved_plan_session, saved_plan = await check_status_rejections(f)
        await check_cancellation(f)
        before, after = await check_readonly_invariant(f)

        # 已保存提案的幂等记录在会话删除后保留，读取改为拒绝。
        assert (await f.repository.get_plan_snapshot(saved_plan["proposal"])).status == "saved"
        await f.service.delete_session(saved_plan_session)
        assert await f.repository.get_plan_snapshot(saved_plan["proposal"]) is None
        assert await f.repository.get_plan_save_record(saved_plan["proposal"]) is not None
        (ROOT / "evidence.json").write_text(json.dumps({
            "tables": before, "after": after,
            "sessions": [profile_session, plan_session, workout_session],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print("PASS: get_pending_proposal 三类读取、长计划完整返回、非法参数/未知/跨会话/路径外拒绝、"
              "非 pending 与展示未绑定/不一致拒绝、取消传播、声明注册调用一致、只读不变量、"
              "既有准备保存状态幂等保存回归;", ROOT)
    finally:
        await f.close()


if __name__ == "__main__":
    asyncio.run(check())
