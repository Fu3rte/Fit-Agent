import asyncio
import copy
import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agent.tool import run_tool_batch
from app.agent.tools.business import bind_business_tools, business_tool_declarations
from app.agent.tools.plan_import import (
    bind_plan_import_tools,
    plan_import_tool_declarations,
)
from app.domain.business.models import PlanAdjustmentProposal, PlanImportProposal
from app.domain.session.models import SendCommand, SendRequest
from app.interfaces import http as interface
from test.check_plan_core import CONTENT, profile
from test.check_profile_confirmation import SYSTEM, Fixture, count_rows, new_id
from test.check_workout_service_http import assistant

ROOT = Path(__file__).resolve().parents[2] / "tmp/backend-plan-import/checks/plan-import-tools" / uuid4().hex
ROOT.mkdir(parents=True)
NAMES = ["prepare_plan_import", "prepare_plan_adjustment"]
DECLARED = {item.name: item for item in plan_import_tool_declarations()}
# 生产声明注册表：稳定声明与批次执行实例声明必须逐项一致。
PRODUCTION = {item.name: item for item in business_tool_declarations()}
INCOMPLETE = {
    "repeat": None,
    "days": [{"kind": "training", "focus": "推", "exercises": [], "notes": None}],
    "notes": None,
    "suggested_fields": [],
}


def invocation(name, arguments):
    from app.ai.messages import ToolCall

    return ToolCall(type="toolCall", id=new_id(), name=name, arguments=arguments)


def body(message):
    assert len(message.content) == 1 and message.content[0].type == "text"
    return json.loads(message.content[0].text)


async def binding(f, session, calls):
    accepted = await f.service.accept_send(
        SendCommand(operation_id=new_id(), session_id=session, request=SendRequest(text="处理计划")),
        system_message=SYSTEM,
    )
    run = accepted.run
    source = await f.append(session, run.id, run.request_entry_id,
        assistant(calls[0].name, calls[0].id, calls[0].arguments).model_copy(update={"content": calls}))
    context = f.context(session, run.request_entry_id, source)
    registrations = {}
    # 生产统一入口：声明注册表与批次执行注册表同源，新增工具按顺序追加并共享 plan_prepared。
    tools = bind_business_tools(f.business, context, f.call, {}, {}, registrations)
    assert list(tools)[-2:] == NAMES
    assert len(tools) == len(PRODUCTION) == len(set(PRODUCTION))
    assert {name: tool.definition() for name, tool in tools.items()} == PRODUCTION
    assert PRODUCTION.keys() >= DECLARED.keys()
    # 持久化后的展示绑定入口覆盖统一注册表中的全部计划域准备工具。
    assert interface.PLAN_PREPARE_TOOLS == {name for name in PRODUCTION if name.startswith("prepare_plan")}
    assert {name: tool.definition() for name, tool in bind_plan_import_tools(
        f.business, context, f.call, {}).items()} == {name: PRODUCTION[name] for name in NAMES}
    for name in NAMES:
        tool = tools[name]
        assert tool.execution_mode == "sequential" and tool.max_output_chars is None
        assert tool.trusted_context.model_dump() == context.model_dump()
        # 参数预处理只做一条明确形式转换：两依据字段精确字符串 "None" 归一为 null，其余原样严格校验。
        converted = {"base_profile_version": "None", "base_plan_id": "None", "payload": copy.deepcopy(INCOMPLETE)}
        assert tool.prepare_arguments(copy.deepcopy(converted)) == {
            "base_profile_version": None, "base_plan_id": None, "payload": INCOMPLETE}
        for kept in ({"base_profile_version": "null", "base_plan_id": "none", "payload": INCOMPLETE},
                     {"base_profile_version": "NONE", "base_plan_id": "", "payload": INCOMPLETE},
                     {"base_profile_version": None, "base_plan_id": " None", "payload": INCOMPLETE}):
            assert tool.prepare_arguments(copy.deepcopy(kept)) == kept
        for identity in ("session_id", "run_id", "request_entry_id", "source_entry_id",
                         "timezone", "business_date"):
            with pytest.raises(ValidationError):
                tool.arguments.model_validate({identity: new_id()})
    # 冻结的身份快照：绑定后源上下文变化不影响已绑定实例，实例字段不可改写。
    probe = bind_plan_import_tools(f.business, context.model_copy(update={"run_id": new_id()}), f.call, {})
    for name in NAMES:
        assert probe[name].trusted_context.run_id != context.run_id
        try:
            probe[name].trusted_context.session_id = new_id()
        except ValidationError:
            pass
        else:
            raise AssertionError("绑定上下文必须冻结")
    return run, source, tools, registrations


async def batch(f, session, calls):
    run, source, tools, registrations = await binding(f, session, calls)
    execution = await run_tool_batch(calls, tools=tools, declared=PRODUCTION)
    assert execution.failure is None, execution.failure
    assert [message.tool_call_id for message in execution.messages] == [call.id for call in calls]
    parent = source
    for message in execution.messages:
        parent = await f.append(session, run.id, parent, message)
        if not message.is_error:
            assert registrations[message.tool_call_id] == body(message)["proposal_id"]
            await f.business.bind_plan_display_entry(registrations[message.tool_call_id], parent)
            stored = await f.service.get_entry(session, parent)
            assert stored.messages[0] == message
    await f.service.finish_run(session, run.id, "completed")
    return execution.messages, registrations


async def saved_chain(f, session, name, arguments):
    """统一注册表与真实 harness：准备结果持久化后绑定展示，后续真实确认节点保存并核对幂等。"""
    call = invocation(name, arguments)
    run, source, tools, registrations = await binding(f, session, [call])
    execution = await run_tool_batch([call], tools=tools, declared=PRODUCTION)
    assert execution.failure is None
    prepared = execution.messages[0]
    assert not prepared.is_error, prepared
    proposal = body(prepared)
    assert registrations == {call.id: proposal["proposal_id"]}
    display = await f.append(session, run.id, source, prepared)
    assert (await f.service.get_entry(session, display)).messages[0] == prepared
    await f.business.bind_plan_display_entry(proposal["proposal_id"], display)
    snapshot = await f.repository.get_plan_snapshot(proposal["proposal_id"])
    assert snapshot.display_entry_id == display and snapshot.status == "pending"
    assert snapshot.preparation_kind == proposal["preparation_kind"]
    await f.service.finish_run(session, run.id, "completed")

    accepted = await f.service.accept_send(
        SendCommand(operation_id=new_id(), session_id=session, request=SendRequest(text="确认保存这份计划")),
        system_message=SYSTEM,
    )
    save_run = accepted.run
    save_args = {"proposal_id": proposal["proposal_id"], "display_entry_id": display,
                 "confirmation_entry_id": save_run.request_entry_id}
    save_call = invocation("save_plan", save_args)
    save_source = await f.append(session, save_run.id, save_run.request_entry_id,
        assistant("save_plan", save_call.id, save_args))
    # 保存批次绑定自身可信上下文，登记集合沿用准备批次的同一对象。
    save_tools = bind_business_tools(
        f.business, f.context(session, save_run.request_entry_id, save_source), f.call, {}, {}, registrations
    )
    assert {tool_name: tool.definition() for tool_name, tool in save_tools.items()} == PRODUCTION
    execution = await run_tool_batch([save_call], tools=save_tools, declared=PRODUCTION)
    assert execution.failure is None
    saved_message = execution.messages[0]
    assert saved_message.tool_call_id == save_call.id and not saved_message.is_error, saved_message
    saved = body(saved_message)
    assert set(saved) == {"proposal_id", "id", "content", "created_at", "saved_at"}
    assert saved["proposal_id"] == proposal["proposal_id"] and saved["content"] == arguments["payload"]
    assert saved["created_at"] == saved["saved_at"]
    node = await f.append(session, save_run.id, save_source, saved_message)
    assert (await f.service.get_entry(session, node)).messages[0] == saved_message
    assert (await f.business.get_current_plan()).id == saved["id"]

    repeat, status_call = invocation("save_plan", save_args), invocation(
        "get_plan_save_status", {"proposal_id": proposal["proposal_id"]})
    execution = await run_tool_batch([repeat, status_call], tools=save_tools, declared=PRODUCTION)
    assert execution.failure is None
    assert [item.tool_call_id for item in execution.messages] == [repeat.id, status_call.id]
    assert not any(item.is_error for item in execution.messages)
    assert body(execution.messages[0]) == saved
    assert body(execution.messages[1]) == {"proposal_id": proposal["proposal_id"], "status": "saved", "result": saved}
    snapshot = await f.repository.get_plan_snapshot(proposal["proposal_id"])
    assert snapshot.status == "saved" and snapshot.confirmation_entry_id == save_run.request_entry_id
    assert sum(item.is_current for item in await f.business.list_plans()) == 1
    await f.service.finish_run(session, save_run.id, "completed")
    return saved


async def shared_registry(f, session, version):
    """生成与录入准备共用运行级登记集合；批次含顺序工具时整批顺序执行，后建快照使旧 pending 失效。"""
    current = (await f.business.get_current_plan()).id
    real = f.catalog.all()[0]
    content = copy.deepcopy(CONTENT)
    content["days"][0]["exercises"][0].update(exercise_id=real.id, name=real.name)
    calls = [
        invocation("get_current_plan", {}),
        invocation("prepare_plan", {"base_profile_version": version, "base_plan_id": current, "payload": content}),
        invocation("prepare_plan_import", {"base_profile_version": version, "base_plan_id": current,
                                           "payload": copy.deepcopy(INCOMPLETE)}),
    ]
    run, source, tools, registrations = await binding(f, session, calls)
    assert tools["prepare_plan"].execution_mode == "sequential"
    finalized = []

    async def record(outcome) -> None:
        finalized.append(outcome.message.tool_call_id)

    execution = await run_tool_batch(calls, tools=tools, declared=PRODUCTION, on_tool_finalized=record)
    assert execution.failure is None
    assert [item.tool_call_id for item in execution.messages] == [call.id for call in calls]
    assert finalized == [call.id for call in calls]
    assert all(not item.is_error for item in execution.messages), [item.content[0].text for item in execution.messages]
    assert body(execution.messages[0])["id"] == current
    generation, imported = body(execution.messages[1]), body(execution.messages[2])
    assert set(generation) == {"proposal_id", "base_profile_version", "base_plan_id", "payload"}
    assert set(imported) == set(generation) | {"preparation_kind"}
    assert imported["preparation_kind"] == "import" and imported["payload"] == INCOMPLETE
    assert registrations == {calls[1].id: generation["proposal_id"], calls[2].id: imported["proposal_id"]}
    assert (await f.repository.get_plan_snapshot(generation["proposal_id"])).status == "invalidated"
    assert (await f.repository.get_plan_snapshot(imported["proposal_id"])).status == "pending"
    parent = source
    for message in execution.messages:
        parent = await f.append(session, run.id, parent, message)
    await f.service.finish_run(session, run.id, "completed")
    return imported["proposal_id"]


async def check():
    f = await Fixture(ROOT / "tools.db").seeded()
    try:
        session = await f.session("录入调整工具")
        for name, model, kind in zip(NAMES, (PlanImportProposal, PlanAdjustmentProposal), ("import", "adjustment")):
            args = {"base_profile_version": None, "base_plan_id": None, "payload": copy.deepcopy(INCOMPLETE)}
            args["payload"]["notes"] = "用户提供的完整依据；" * 10000
            messages, registrations = await batch(f, session, [invocation(name, args)])
            message = messages[0]
            assert not message.is_error and len(message.content[0].text) > 50000
            proposal = model.model_validate_json(message.content[0].text)
            assert proposal.preparation_kind == kind and proposal.payload.model_dump() == args["payload"]
            assert proposal.base_profile_version is None and proposal.base_plan_id is None
            assert len(registrations) == 1
            schema = DECLARED[name].parameters
            assert schema["additionalProperties"] is False
            assert set(schema["required"]) == {"base_profile_version", "base_plan_id", "payload"}
            assert set(schema["properties"]) == set(args)
            invalid = [{k: v for k, v in args.items() if k != key} for key in args]
            invalid += [{**args, **update} for update in (
                {"base_profile_version": "null"}, {"base_profile_version": "NONE"},
                {"base_profile_version": "1"}, {"base_profile_version": True},
                {"base_profile_version": 0}, {"base_profile_version": 1.0},
                {"base_plan_id": "null"}, {"base_plan_id": "none"}, {"base_plan_id": ""},
                {"base_plan_id": uuid4().hex},
                {"base_plan_id": "invalid"}, {"preparation_kind": "generation"},
            )]
            invalid += [{**args, identity: new_id()} for identity in (
                "session_id", "run_id", "request_entry_id", "source_entry_id", "timezone", "business_date")]
            partial = copy.deepcopy(CONTENT)
            for value in ("3", True, 0, 3.0):
                changed = copy.deepcopy(partial)
                changed["days"][0]["exercises"][0]["sets"] = value
                invalid.append({**args, "payload": changed})
            before = await count_rows(f.database, "plan_snapshots")
            messages, registrations = await batch(f, session, [invocation(name, value) for value in invalid])
            assert all(message.is_error and "参数校验失败" in message.content[0].text for message in messages)
            assert not registrations and await count_rows(f.database, "plan_snapshots") == before
            # 两依据字段的精确字符串 "None" 经预处理成为 null 依据，创建成功且登记快照。
            messages, registrations = await batch(f, session,
                [invocation(name, {**args, "base_profile_version": "None", "base_plan_id": "None"})])
            prepared = body(messages[0])
            assert not messages[0].is_error and prepared["base_profile_version"] is None
            assert prepared["base_plan_id"] is None and registrations == {messages[0].tool_call_id: prepared["proposal_id"]}
            unknown = copy.deepcopy(partial)
            unknown["days"][0]["exercises"][0].update(sets=None, reps=None)
            messages, _ = await batch(f, session, [invocation(name, {**args, "payload": unknown})])
            assert not messages[0].is_error and body(messages[0])["payload"] == unknown
            unknown["days"][0]["exercises"][0]["exercise_id"] = "unknown"
            messages, registrations = await batch(f, session, [invocation(name, {**args, "payload": unknown})])
            detail = body(messages[0])
            assert messages[0].is_error and not registrations
            assert detail["code"] == "invalid_business_payload"
            assert set(detail) == {"code", "message", "errors"}
            assert detail["errors"] == [{"path": "/payload/days/0/exercises/0/exercise_id", "message": "动作 ID 必须存在于动作目录中。"}]
        # 无画像依据的不完整计划可录入并保存：null 依据要求保存时画像仍不存在。
        imported_saved = await saved_chain(f, await f.session("无画像录入保存链"), "prepare_plan_import",
            {"base_profile_version": None, "base_plan_id": None, "payload": copy.deepcopy(INCOMPLETE)})
        adjusted_saved = await saved_chain(f, await f.session("无画像调整保存链"), "prepare_plan_adjustment",
            {"base_profile_version": None, "base_plan_id": imported_saved["id"],
             "payload": copy.deepcopy(INCOMPLETE)})
        assert adjusted_saved["id"] != imported_saved["id"]
        assert (await f.business.get_current_plan()).id == adjusted_saved["id"]
        assert await count_rows(f.database, "plans") == 2
        await profile(f, session)
        for name in NAMES:
            args = {"base_profile_version": None, "base_plan_id": None, "payload": INCOMPLETE}
            messages, registrations = await batch(f, session, [invocation(name, args)])
            assert messages[0].is_error and body(messages[0])["code"] == "profile_version_conflict"
            assert set(body(messages[0])) == {"code", "message"} and not registrations
            args["base_profile_version"] = (await f.business.get_profile()).version
            args["base_plan_id"] = (await f.business.get_current_plan()).id
            messages, _ = await batch(f, session, [invocation(name, args)])
            assert not messages[0].is_error and body(messages[0])["base_profile_version"] == args["base_profile_version"]
            messages, registrations = await batch(f, session, [invocation(name, {**args, "base_plan_id": new_id()})])
            assert messages[0].is_error and body(messages[0])["code"] == "plan_version_conflict" and not registrations
        assert await count_rows(f.database, "plans") == 2
        await shared_registry(f, await f.session("统一登记与顺序批次"), (await f.business.get_profile()).version)
        from test.check_plan_import_adjustment import prepare

        context, save = await prepare(f, session, version=(await f.business.get_profile()).version,
                                      base=(await f.business.get_current_plan()).id)
        saved = await f.business.save_plan(context, save)
        for name in NAMES:
            args = {"base_profile_version": (await f.business.get_profile()).version,
                    "base_plan_id": saved.id, "payload": INCOMPLETE}
            messages, _ = await batch(f, session, [invocation(name, args)])
            assert not messages[0].is_error and body(messages[0])["base_plan_id"] == saved.id
            assert (await f.business.get_current_plan()).id == saved.id
            assert await count_rows(f.database, "plans") == 3
        already = (await f.business.get_current_plan()).id
        version = (await f.business.get_profile()).version
        for name in NAMES:
            chain_saved = await saved_chain(f, await f.session(f"{name}保存链"), name,
                {"base_profile_version": version, "base_plan_id": (await f.business.get_current_plan()).id,
                 "payload": copy.deepcopy(INCOMPLETE)})
            assert (await f.business.get_current_plan()).id == chain_saved["id"]
            assert chain_saved["id"] != already
            already = chain_saved["id"]
        assert await count_rows(f.database, "plans") == 5
        assert sum(item.is_current for item in await f.business.list_plans()) == 1
        print("PASS: unified production registry declarations and bindings; frozen identity; real SQLite/catalog/messages; both kinds; complete JSON; strict fields/null/types/UUID; registrations shared with generation; sequential batch ordering; persist then display bind; confirm save and idempotent status; no-profile import save")
        print("Evidence:", ROOT)
    finally:
        await f.close()


if __name__ == "__main__":
    asyncio.run(check())
