import asyncio
import copy
import json
from typing import NamedTuple
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agent.tool import run_tool_batch
from app.agent.tools.business import bind_business_tools, business_tool_declarations
from app.agent.tools.plans import plan_tool_declarations
from app.ai.messages import ToolCall
from app.domain.business.models import BusinessContext, PlanProposal, PlanStatusResult
from app.domain.session.models import SendCommand, SendRequest
from test.check_plan_core import CONTENT, profile
from test.check_profile_confirmation import SYSTEM, Fixture, count_rows, new_id, payload
from test.check_workout_service_http import CONTENT as WORKOUT
from test.check_workout_service_http import assistant
from test.regression_support import temporary_root

ROOT = temporary_root("plan-tools") / uuid4().hex
ROOT.mkdir()
NAMES = ["get_current_plan", "get_plan", "list_plans", "prepare_plan", "save_plan", "get_plan_save_status"]
IMPORT_NAMES = ["prepare_plan_import", "prepare_plan_adjustment"]
DECLARED = {item.name: item for item in business_tool_declarations()}
PREPARE_KIND = {"prepare_profile_update": "profile", "prepare_workout": "workout", "prepare_plan": "plan",
                "prepare_plan_import": "plan", "prepare_plan_adjustment": "plan"}
DISPLAY_BINDER = {
    "profile": "bind_display_entry",
    "workout": "bind_workout_display_entry",
    "plan": "bind_plan_display_entry",
}
DISPLAY_MAP = {
    "profile": "list_display_bindings",
    "workout": "list_workout_display_bindings",
    "plan": "list_plan_display_bindings",
}
EXERCISE = CONTENT["days"][0]["exercises"][0]


def body(message):
    assert len(message.content) == 1 and message.content[0].type == "text"
    return json.loads(message.content[0].text)


def invocation(name, arguments):
    return ToolCall(type="toolCall", id=new_id(), name=name, arguments=arguments)


async def ensure_profile(f, session):
    # 画像全局共享：仅在尚未建档时保存版本 1，其余阶段直接复用。
    if (await f.business.get_profile()).version is None:
        await profile(f, session)
    assert (await f.business.get_profile()).version == 1


class Outcome(NamedTuple):
    context: BusinessContext
    messages: list
    nodes: dict
    displays: dict
    registrations: dict
    tools: dict
    source: str
    request_entry_id: str


async def batch(f, session, calls, *, confirmation=None, bind=True):
    """真实消息链：用户节点 -> 助手批次调用 -> 真实工具执行 -> 逐条结果持久化 -> 按业务绑定展示。"""
    outcome = await f.service.accept_send(SendCommand(operation_id=new_id(), session_id=session,
        request=SendRequest(text="确认采纳计划" if confirmation is not None else "处理计划请求")), system_message=SYSTEM)
    run = outcome.run
    if confirmation is not None:
        calls = [invocation("save_plan", {**confirmation, "confirmation_entry_id": run.request_entry_id})]
    source = await f.append(session, run.id, run.request_entry_id,
        assistant(calls[0].name, calls[0].id, calls[0].arguments).model_copy(update={"content": calls}))
    context = f.context(session, run.request_entry_id, source)
    registrations = {"profile": {}, "workout": {}, "plan": {}}
    tools = bind_business_tools(f.business, context, f.call, registrations["profile"],
                                registrations["workout"], registrations["plan"])
    assert {name: tool.definition() for name, tool in tools.items()} == DECLARED
    assert list(tools)[-8:][:6] == NAMES and list(tools)[-2:] == IMPORT_NAMES
    for name in NAMES:
        assert tools[name].execution_mode == ("sequential" if name in {"prepare_plan", "save_plan"} else "parallel")
        assert tools[name].max_output_chars is None
        assert tools[name].prepare_arguments is None
    execution = await run_tool_batch(calls, tools=tools, declared=DECLARED)
    assert execution.failure is None
    captured = {kind: dict(values) for kind, values in registrations.items()}
    nodes, displays = {}, {}
    parent = source
    for message in execution.messages:
        parent = await f.append(session, run.id, parent, message)
        nodes[message.tool_call_id] = parent
        kind = PREPARE_KIND.get(message.tool_name)
        if kind is None or message.is_error:
            continue
        proposal_id = registrations[kind].pop(message.tool_call_id)
        assert proposal_id == body(message)["proposal_id"]
        displays[message.tool_call_id] = parent
        if bind:
            await getattr(f.business, DISPLAY_BINDER[kind])(proposal_id, parent)
    await f.service.finish_run(session, run.id, "completed")
    if bind:
        assert not any(registrations.values())
    return Outcome(context, execution.messages, nodes, displays, captured, tools, source, run.request_entry_id)


async def check():
    f = await Fixture(ROOT / "tools.db").seeded()
    try:
        session = await f.session("计划工具真实消息链")
        got = await batch(f, session, [invocation("get_current_plan", {}), invocation("list_plans", {})])
        assert body(got.messages[0]) == {"id": None, "content": None} and body(got.messages[1]) == []
        legacy = bind_business_tools(f.business, got.context, f.call, {}, {})
        assert {name: tool.definition() for name, tool in legacy.items()} == DECLARED
        assert [item.name for item in plan_tool_declarations()] == NAMES
        for name in NAMES:
            schema = DECLARED[name].parameters
            assert schema["additionalProperties"] is False
            assert set(schema["properties"]) == {
                "get_current_plan": set(), "list_plans": set(), "get_plan": {"plan_id"},
                "prepare_plan": {"base_profile_version", "base_plan_id", "payload"},
                "save_plan": {"proposal_id", "display_entry_id", "confirmation_entry_id"},
                "get_plan_save_status": {"proposal_id"},
            }[name]
            for identity in ("session_id", "run_id", "request_entry_id", "source_entry_id", "timezone", "business_date"):
                with pytest.raises(ValidationError):
                    got.tools[name].arguments.model_validate({identity: new_id()})
        args = {"base_profile_version": 1, "base_plan_id": None, "payload": copy.deepcopy(CONTENT)}
        got = await batch(f, session, [invocation("prepare_plan", args)])
        assert got.messages[0].is_error and body(got.messages[0])["code"] == "profile_required" and not got.registrations["plan"]
        await ensure_profile(f, session)
        real = f.catalog.all()[0]
        args["payload"]["days"][0]["exercises"][0].update(exercise_id=real.id, name=real.name)
        args["payload"]["notes"] = "依据已保存画像及目录；" * 10000
        prepare = invocation("prepare_plan", args)
        got = await batch(f, session, [invocation("get_plan", {"plan_id": new_id()}), prepare])
        assert got.messages[0].is_error and body(got.messages[0])["code"] == "plan_not_found"
        proposal = body(got.messages[1])
        assert not got.messages[1].is_error and len(got.messages[1].content[0].text) > 50000
        assert proposal["payload"] == args["payload"]
        assert got.registrations["plan"] == {prepare.id: proposal["proposal_id"]}
        snapshot = await f.repository.get_plan_snapshot(proposal["proposal_id"])
        assert snapshot.display_entry_id == got.displays[prepare.id]
        save = {"proposal_id": proposal["proposal_id"], "display_entry_id": got.displays[prepare.id]}
        got = await batch(f, session, [invocation("get_plan_save_status", {"proposal_id": proposal["proposal_id"]})])
        assert body(got.messages[0]) == {"proposal_id": proposal["proposal_id"], "status": "pending", "result": None}
        save_context, messages, _, _, _, _, _, confirmation = await batch(f, session, [], confirmation=save)
        first = body(messages[0])
        assert not messages[0].is_error and first["content"] == args["payload"]
        assert first["created_at"] == first["saved_at"] and "is_current" not in first
        fixed_args = {**save, "confirmation_entry_id": confirmation}
        got = await batch(f, session, [invocation("save_plan", fixed_args),
            invocation("get_plan_save_status", {"proposal_id": proposal["proposal_id"]}),
            invocation("get_current_plan", {}), invocation("get_plan", {"plan_id": first["id"]}), invocation("list_plans", {})])
        messages = got.messages
        assert all(not item.is_error for item in messages)
        assert body(messages[0]) == first and body(messages[1])["result"] == first
        assert body(messages[2]) == {"id": first["id"], "content": first["content"]}
        record = body(messages[3])
        assert record["is_current"] and record["content"] == first["content"] and body(messages[4]) == [record]
        for message in messages:
            assert len(message.content[0].text) > 50000
        invalid = [
            {**args, "base_plan_id": "None"}, {**args, "base_profile_version": "1"},
            {**args, "base_profile_version": True}, {**args, "base_plan_id": "invalid"},
            {key: value for key, value in args.items() if key != "base_plan_id"},
            {**args, "base_plan_id": first["id"], "session_id": session},
        ]
        got = await batch(f, session, [invocation("prepare_plan", value) for value in invalid])
        assert all(message.is_error for message in got.messages) and not got.registrations["plan"]
        incomplete = copy.deepcopy(args)
        incomplete["base_plan_id"] = first["id"]
        incomplete["payload"]["days"][0]["exercises"][0]["sets"] = None
        got = await batch(f, session, [invocation("prepare_plan", incomplete)])
        error = body(got.messages[0])
        assert got.messages[0].is_error and error["code"] == "invalid_business_payload" and not got.registrations["plan"]
        assert error["errors"][0]["path"] == "/payload/days/0/exercises/0/sets"
        other = await f.session("计划工具归属")
        got = await batch(f, other, [invocation("get_plan_save_status", {"proposal_id": proposal["proposal_id"]}),
            invocation("save_plan", fixed_args)])
        assert all(message.is_error and body(message)["code"] == "plan_access_denied" for message in got.messages)
        second_args = {**args, "base_plan_id": first["id"], "payload": {**args["payload"], "notes": "新版本依据"}}
        prepare2 = invocation("prepare_plan", second_args)
        got = await batch(f, session, [prepare2])
        proposal2 = body(got.messages[0])
        _, messages, _, _, _, _, _, confirmation2 = await batch(f, session, [],
            confirmation={"proposal_id": proposal2["proposal_id"], "display_entry_id": got.displays[prepare2.id]})
        second = body(messages[0])
        got = await batch(f, session, [invocation("list_plans", {}), invocation("save_plan", fixed_args)])
        records = body(got.messages[0])
        assert len(records) == 2 and sum(item["is_current"] for item in records) == 1
        assert records[0]["id"] == second["id"] and records[1]["id"] == first["id"]
        assert body(got.messages[1]) == first
        await f.restart()
        got = await batch(f, session, [invocation("get_plan_save_status", {"proposal_id": proposal["proposal_id"]})])
        assert body(got.messages[0])["result"] == first
        base = {"names": NAMES, "versions": [first["id"], second["id"]], "long_json": True,
                "real_catalog_id": real.id, "strict_parameters": True, "batch_registration": True,
                "real_display_confirmation": True, "fixed_result_restart": True}
        return {
            "mixed_batch": await isolated(check_mixed_batch, "mixed"),
            "persistence_before_binding": await isolated(check_persistence_before_binding, "persistence"),
            "strict_arguments": await isolated(check_strict_arguments, "strict"),
            "error_shapes": await isolated(check_error_shapes, "error-shapes"),
            "modification_flow": await isolated(check_modification_flow, "modification"),
            **base,
        }
    finally:
        await f.close()


async def isolated(phase, name):
    # 每个阶段使用独立隔离数据库，避免依赖其他阶段遗留的当前计划与快照。
    f = await Fixture(ROOT / f"{name}.db").seeded()
    try:
        return await phase(f)
    finally:
        await f.close()


async def check_mixed_batch(f):
    """批次内成功、失败及三类业务调用保持逐调用对应；跨类展示绑定拒绝（工具与 harness 层）。"""
    session = await f.session("计划混合批次")
    await ensure_profile(f, session)
    day = f.context(session, new_id(), new_id()).business_date
    real = f.catalog.all()[0]
    plan_content = copy.deepcopy(CONTENT)
    plan_content["days"][0]["exercises"][0].update(exercise_id=real.id, name=real.name)
    legal = {"base_profile_version": 1, "base_plan_id": None, "payload": plan_content}
    calls = [
        invocation("get_current_plan", {}),
        invocation("prepare_plan", legal),
        invocation("prepare_plan", {**legal, "payload": {**plan_content, "days": [
            {**plan_content["days"][0], "exercises": [{**EXERCISE, "sets": None, "exercise_id": None}]}]}}),
        invocation("save_plan", {"proposal_id": new_id(), "display_entry_id": new_id(), "confirmation_entry_id": new_id()}),
        invocation("get_plan_save_status", {"proposal_id": new_id()}),
        invocation("prepare_profile_update", {"profile_id": 1, "base_profile_version": 1, "payload": payload(goal="力量")}),
        invocation("prepare_workout", {"performed_on": day, "base_workout_id": None, "base_workout_version": None,
                                       "payload": WORKOUT}),
        invocation("get_plan", {"plan_id": new_id()}),
    ]
    before = await count_rows(f.database, "plan_snapshots")
    got = await batch(f, session, calls, bind=False)
    assert [item.tool_call_id for item in got.messages] == [item.id for item in calls]
    assert [item.tool_name for item in got.messages] == [item.name for item in calls]
    assert [item.role for item in got.messages] == ["toolResult"] * len(calls)
    assert [item.is_error for item in got.messages] == [False, False, True, True, True, False, False, True]
    assert got.registrations == {
        "profile": {calls[5].id: body(got.messages[5])["proposal_id"]},
        "workout": {calls[6].id: body(got.messages[6])["proposal_id"]},
        "plan": {calls[1].id: body(got.messages[1])["proposal_id"]},
    }
    assert body(got.messages[2])["code"] == "invalid_business_payload"
    assert body(got.messages[2])["errors"] == [
        {"path": "/payload/days/0/exercises/0/sets", "message": "生成动作必须明确组数。"}]
    for index, code in [(3, "plan_proposal_not_found"), (4, "plan_proposal_not_found"), (7, "plan_not_found")]:
        assert body(got.messages[index]) == {"code": code, "message": got.messages[index].content[0].text and body(got.messages[index])["message"]}
        assert set(body(got.messages[index])) == {"code", "message"}
    assert await count_rows(f.database, "plan_snapshots") == before + 1

    plan_proposal = body(got.messages[1])["proposal_id"]
    rejected = {}
    for label, target in [
        ("助手来源节点", got.source),
        ("失败准备结果节点", got.nodes[calls[2].id]),
        ("训练准备结果节点", got.nodes[calls[6].id]),
        ("画像准备结果节点", got.nodes[calls[5].id]),
        ("查询结果节点", got.nodes[calls[0].id]),
    ]:
        rejected[label] = await rejection_message(f.business.bind_plan_display_entry(plan_proposal, target))
    assert [item["code"] for item in rejected.values()] == ["plan_confirmation_invalid"] * len(rejected)

    for index, kind in [(1, "plan"), (5, "profile"), (6, "workout")]:
        await getattr(f.business, DISPLAY_BINDER[kind])(
            got.registrations[kind][calls[index].id], got.nodes[calls[index].id])
    maps = {kind: await getattr(f.business, method)(session) for kind, method in DISPLAY_MAP.items()}
    assert maps["plan"] == {got.nodes[calls[1].id]: plan_proposal}
    profile_proposal = got.registrations["profile"][calls[5].id]
    assert maps["profile"].get(got.nodes[calls[5].id]) == profile_proposal
    assert maps["workout"] == {got.nodes[calls[6].id]: got.registrations["workout"][calls[6].id]}
    keys = [set(item) for item in maps.values()]
    assert not keys[0] & keys[1] and not keys[0] & keys[2] and not keys[1] & keys[2]
    return {"mixed_batch_calls": len(calls), "per_kind_registration": True,
            "cross_kind_display_rejected": sorted(rejected), "display_maps_disjoint": True}


async def rejection_message(awaitable) -> dict:
    try:
        await awaitable
    except Exception as error:
        return error.detail()
    raise AssertionError("展示绑定未被拒绝")


async def check_persistence_before_binding(f):
    """prepare 结果先持久化再绑定实际展示节点；未持久化节点与未绑定快照均不能保存。"""
    session = await f.session("计划先持久化后绑定")
    await ensure_profile(f, session)
    real = f.catalog.all()[0]
    content = copy.deepcopy(CONTENT)
    content["days"][0]["exercises"][0].update(exercise_id=real.id, name=real.name)
    call = invocation("prepare_plan", {"base_profile_version": 1, "base_plan_id": None, "payload": content})
    got = await batch(f, session, [call], bind=False)
    proposal_id = body(got.messages[0])["proposal_id"]
    snapshot = await f.repository.get_plan_snapshot(proposal_id)
    assert snapshot.display_entry_id is None and snapshot.status == "pending"
    unpersisted = await rejection_message(f.business.bind_plan_display_entry(proposal_id, new_id()))
    assert "当前消息路径" in unpersisted["message"]
    arguments = {"proposal_id": proposal_id, "display_entry_id": new_id(), "confirmation_entry_id": got.request_entry_id}
    got2 = await batch(f, session, [invocation("save_plan", arguments)])
    assert got2.messages[0].is_error and body(got2.messages[0])["code"] == "plan_confirmation_invalid"
    assert (await f.repository.get_plan_snapshot(proposal_id)).display_entry_id is None
    display = got.nodes[call.id]
    await f.business.bind_plan_display_entry(proposal_id, display)
    assert (await f.repository.get_plan_snapshot(proposal_id)).display_entry_id == display
    stored = await f.service.get_entry(session, display)
    assert stored.messages[0] == got.messages[0] and stored.parent_id == got.source
    return {"unpersisted_display_rejected": True, "unbound_save_rejected": True,
            "result_persisted_before_binding": True}


def exercise_update(**overrides):
    return {**copy.deepcopy(CONTENT), "days": [{**copy.deepcopy(CONTENT["days"][0]),
        "exercises": [{**EXERCISE, **overrides}]}]}


async def check_strict_arguments(f):
    """严格输入：JSON null 合法；字符串 None/null、非法 UUID、次数范围字符串、缺字段与额外字段拒绝。"""
    session = await f.session("计划严格参数")
    await ensure_profile(f, session)
    real = f.catalog.all()[0]
    base_content = exercise_update(exercise_id=real.id, name=real.name)
    legal = {"base_profile_version": 1, "base_plan_id": None, "payload": base_content}

    legal_cases = [
        ("次数字符串改为合法整数", legal),
        ("reps 为空时使用时长", {**legal, "payload": exercise_update(reps=None, duration_seconds=30,
            exercise_id=real.id, name=real.name)}),
        ("重量为零且口径明确", {**legal, "payload": exercise_update(weight_kg=0, load_convention="barbell_total",
            rest_seconds=0, exercise_id=real.id, name=real.name)}),
        ("目录外动作保持 ID 为空", {**legal, "payload": exercise_update()}),
        ("循环方式未知", {**legal, "payload": {**base_content, "repeat": None}}),
        ("建议来源为空数组", {**legal, "payload": {**base_content, "suggested_fields": []}}),
        ("训练日后接休息日", {**legal, "payload": {**base_content, "days": [base_content["days"][0],
            {"kind": "rest", "focus": None, "exercises": [], "notes": None}]}}),
    ]
    accepted = {}
    for label, arguments in legal_cases:
        got = await batch(f, session, [invocation("prepare_plan", arguments)])
        assert not got.messages[0].is_error, (label, got.messages[0].content[0].text)
        proposal = PlanProposal.model_validate_json(got.messages[0].content[0].text)
        assert proposal.payload.model_dump() == arguments["payload"]
        assert proposal.base_plan_id is None and proposal.base_profile_version == 1
        accepted[label] = proposal.proposal_id
    assert len(accepted) == 7

    schema_cases = {
        "base_plan_id 字符串 None": {**legal, "base_plan_id": "None"},
        "base_plan_id 字符串 null": {**legal, "base_plan_id": "null"},
        "base_plan_id 空字符串": {**legal, "base_plan_id": " "},
        "base_plan_id 非标准 UUID": {**legal, "base_plan_id": uuid4().hex},
        "base_plan_id 花括号 UUID": {**legal, "base_plan_id": "{" + str(uuid4()) + "}"},
        "base_plan_id 缺失": {key: value for key, value in legal.items() if key != "base_plan_id"},
        "画像版本字符串": {**legal, "base_profile_version": "1"},
        "画像版本布尔": {**legal, "base_profile_version": True},
        "画像版本零": {**legal, "base_profile_version": 0},
        "画像版本浮点": {**legal, "base_profile_version": 1.0},
        "画像版本缺失": {key: value for key, value in legal.items() if key != "base_profile_version"},
        "额外身份字段": {**legal, "session_id": session},
        "payload 缺失": {key: value for key, value in legal.items() if key != "payload"},
        "次数字符串": {**legal, "payload": exercise_update(reps="8")},
        "次数范围字符串": {**legal, "payload": exercise_update(reps="8-12")},
        "次数区间带空格": {**legal, "payload": exercise_update(reps=" 8")},
        "次数布尔": {**legal, "payload": exercise_update(reps=True)},
        "次数零": {**legal, "payload": exercise_update(reps=0)},
        "次数浮点": {**legal, "payload": exercise_update(reps=8.0)},
        "组数范围字符串": {**legal, "payload": exercise_update(sets="2-3")},
        "时长字符串": {**legal, "payload": exercise_update(duration_seconds="30")},
        "时长为零": {**legal, "payload": exercise_update(duration_seconds=0)},
        "重量字符串": {**legal, "payload": exercise_update(weight_kg="20")},
        "重量为负": {**legal, "payload": exercise_update(weight_kg=-1)},
        "休息时长负值": {**legal, "payload": exercise_update(rest_seconds=-1)},
        "未知重量口径": {**legal, "payload": exercise_update(weight_kg=20)},
        "口径取值未知": {**legal, "payload": exercise_update(load_convention="unknown")},
        "动作名为空白": {**legal, "payload": exercise_update(name=" ")},
        "动作字段缺失": {**legal, "payload": {**base_content, "days": [{**base_content["days"][0],
            "exercises": [{key: value for key, value in EXERCISE.items() if key != "sets"}]}]}},
        "动作额外字段": {**legal, "payload": {**base_content, "days": [{**base_content["days"][0],
            "exercises": [{**EXERCISE, "tempo": "2-0-1"}]}]}},
        "训练日额外字段": {**legal, "payload": {**base_content, "days": [{**base_content["days"][0], "extra": 1}]}},
        "休息日包含动作": {**legal, "payload": {**base_content, "days": [{**base_content["days"][0], "kind": "rest"}]}},
        "日期列表为空": {**legal, "payload": {**base_content, "days": []}},
        # 契约要求 suggested_fields 为 JSON Pointer 路径；非指针取值的拒绝用例在 check_plan_pointer_gap.py。
        "建议来源非字符串": {**legal, "payload": {**base_content, "suggested_fields": [1]}},
        "内容额外字段": {**legal, "payload": {**base_content, "version": 1}},
    }
    before = await count_rows(f.database, "plan_snapshots")
    got = await batch(f, session, [invocation("prepare_plan", value) for value in schema_cases.values()])
    rows = {}
    for (label, _), message in zip(schema_cases.items(), got.messages):
        assert message.is_error, label
        text = message.content[0].text
        assert message.role == "toolResult" and len(message.content) == 1
        assert text.startswith('工具 "prepare_plan" 参数校验失败'), (label, text[:120])
        assert "收到的参数" in text
        rows[label] = text
    assert await count_rows(f.database, "plan_snapshots") == before
    assert '"type": "null"' in rows["base_plan_id 字符串 None"]
    assert "String should match pattern" in rows["base_plan_id 字符串 None"]
    assert "payload.days.0.exercises.0.reps" in rows["次数字符串"]
    assert "payload.days.0.exercises.0.reps" in rows["次数范围字符串"]
    assert "payload.base_profile_version" not in rows["画像版本字符串"]
    assert "base_profile_version" in rows["画像版本字符串"]
    assert "payload.days.0" in rows["休息日包含动作"]

    business_cases = {
        "组数为空": (exercise_update(sets=None), "/payload/days/0/exercises/0/sets"),
        "次数与时长同时为空": (exercise_update(reps=None), "/payload/days/0/exercises/0/reps"),
        "目录外动作 ID": (exercise_update(exercise_id="unknown"), "/payload/days/0/exercises/0/exercise_id"),
        "动作 ID 字符串 None": (exercise_update(exercise_id="None"), "/payload/days/0/exercises/0/exercise_id"),
        "动作 ID 字符串 null": (exercise_update(exercise_id="null"), "/payload/days/0/exercises/0/exercise_id"),
        "训练日无动作": ({**base_content, "days": [{**base_content["days"][0], "exercises": []}]},
                          "/payload/days/0/exercises"),
    }
    got = await batch(f, session, [invocation("prepare_plan", {**legal, "payload": value}) for value, _ in business_cases.values()])
    assert await count_rows(f.database, "plan_snapshots") == before
    for (label, (_, path)), message in zip(business_cases.items(), got.messages):
        assert message.is_error, label
        detail = body(message)
        assert detail["code"] == "invalid_business_payload", (label, detail)
        assert path in [item["path"] for item in detail["errors"]], (label, detail["errors"])
        assert all(set(item) == {"path", "message"} for item in detail["errors"])
    return {"legal_accepted": sorted(accepted), "schema_rejected": sorted(rows),
            "business_rejected": sorted(business_cases), "no_snapshot_written": True}


async def check_error_shapes(f):
    """六个工具的错误形状：业务错误为完整 PlanBusinessErrorWire JSON，harness 失败保留原始文本，身份与角色不变。"""
    session = await f.session("计划错误形状")
    await ensure_profile(f, session)
    real = f.catalog.all()[0]
    content = exercise_update(exercise_id=real.id, name=real.name)
    legal = {"base_profile_version": 1, "base_plan_id": None, "payload": content}
    got = await batch(f, session, [invocation("prepare_plan", legal)])
    proposal_id = body(got.messages[0])["proposal_id"]
    display = got.displays[got.messages[0].tool_call_id]
    stray = new_id()
    unknown = new_id()
    cases = [
        (invocation("get_plan", {"plan_id": unknown}), "business", "plan_not_found"),
        (invocation("get_plan", {"plan_id": "None"}), "harness", "String should match pattern"),
        (invocation("save_plan", {"proposal_id": "None", "display_entry_id": display,
                                  "confirmation_entry_id": stray}), "harness", "proposal_id"),
        (invocation("save_plan", {"proposal_id": proposal_id, "display_entry_id": uuid4().hex,
                                  "confirmation_entry_id": stray}), "harness", "display_entry_id"),
        (invocation("save_plan", {"proposal_id": unknown, "display_entry_id": display,
                                  "confirmation_entry_id": stray}), "business", "plan_proposal_not_found"),
        (invocation("get_plan_save_status", {"proposal_id": "not-a-uuid"}), "harness", "proposal_id"),
        (invocation("get_plan_save_status", {"proposal_id": unknown}), "business", "plan_proposal_not_found"),
        (invocation("prepare_plan", {**legal, "payload": {**content, "days": []}}), "harness", "days"),
    ]
    got2 = await batch(f, session, [item[0] for item in cases], bind=False)
    business, harness = [], []
    for (call, kind, marker), message in zip(cases, got2.messages):
        assert message.tool_call_id == call.id and message.tool_name == call.name
        assert message.role == "toolResult" and message.is_error and len(message.content) == 1
        text = message.content[0].text
        assert marker in text, (call.name, text[:200])
        if kind == "business":
            detail = body(message)
            assert detail["code"] == marker and set(detail) == {"code", "message"}
            assert isinstance(detail["message"], str) and detail["message"]
            business.append(marker)
        else:
            assert not text.lstrip().startswith("{")
            assert "收到的参数" in text
            harness.append(call.name)
    assert sorted(business) == ["plan_not_found", "plan_proposal_not_found", "plan_proposal_not_found"]
    assert len(harness) == 5

    # 确认节点不在当前消息路径时拒绝保存；失败的准备结果不进入展示绑定表。
    got3 = await batch(f, session, [invocation("save_plan", {"proposal_id": proposal_id,
        "display_entry_id": display, "confirmation_entry_id": stray})], bind=False)
    assert got3.messages[0].is_error and body(got3.messages[0])["code"] == "plan_confirmation_invalid"
    failed = await batch(f, session, [invocation("prepare_plan", {**legal, "base_plan_id": "None"})], bind=False)
    assert failed.messages[0].is_error and not failed.registrations["plan"]
    bindings = await f.business.list_plan_display_bindings(session)
    assert bindings == {display: proposal_id}
    assert failed.nodes[failed.messages[0].tool_call_id] not in bindings
    return {"business_error_codes": sorted(business), "harness_text_errors": harness,
            "confirmation_path_rejected": True, "failed_display_unbound": True}


async def check_modification_flow(f):
    """修改式确认创建新快照并使旧 pending 失效；修改请求本身不产生保存。"""
    session = await f.session("计划修改式确认")
    await ensure_profile(f, session)
    real = f.catalog.all()[0]
    content = exercise_update(exercise_id=real.id, name=real.name)
    legal = {"base_profile_version": 1, "base_plan_id": None, "payload": content}
    got = await batch(f, session, [invocation("prepare_plan", legal)])
    first = body(got.messages[0])
    first_display = got.displays[got.messages[0].tool_call_id]
    assert await count_rows(f.database, "plans") == 0
    assert (await f.business.get_current_plan()).model_dump() == {"id": None, "content": None}
    modified = {**legal, "payload": {**content, "days": [{**content["days"][0],
        "exercises": [{**content["days"][0]["exercises"][0], "sets": 3}]}]}}
    got = await batch(f, session, [invocation("prepare_plan", modified)])
    second = body(got.messages[0])
    second_display = got.displays[got.messages[0].tool_call_id]
    assert second["proposal_id"] != first["proposal_id"]
    assert [item.status for item in [await f.repository.get_plan_snapshot(first["proposal_id"]),
                                     await f.repository.get_plan_snapshot(second["proposal_id"])]] == ["invalidated", "pending"]
    assert await count_rows(f.database, "plans") == 0
    assert (await f.business.list_plan_display_bindings(session)) == {second_display: second["proposal_id"]}
    got = await batch(f, session, [], confirmation={"proposal_id": first["proposal_id"], "display_entry_id": first_display})
    assert got.messages[0].is_error and body(got.messages[0])["code"] == "plan_proposal_invalidated"
    assert await count_rows(f.database, "plans") == 0
    got = await batch(f, session, [invocation("save_plan", {"proposal_id": second["proposal_id"],
        "display_entry_id": first_display, "confirmation_entry_id": got.request_entry_id})])
    assert got.messages[0].is_error and body(got.messages[0])["code"] == "plan_confirmation_invalid"
    assert await count_rows(f.database, "plans") == 0
    got = await batch(f, session, [], confirmation={"proposal_id": second["proposal_id"], "display_entry_id": second_display})
    saved = body(got.messages[0])
    assert not got.messages[0].is_error and saved["proposal_id"] == second["proposal_id"]
    assert saved["content"] == modified["payload"]
    assert await count_rows(f.database, "plans") == 1
    assert (await f.business.get_current_plan()).id == saved["id"]
    assert PlanStatusResult.model_validate(body((await batch(f, session, [invocation(
        "get_plan_save_status", {"proposal_id": first["proposal_id"]})])).messages[0])).status == "invalidated"
    return {"new_snapshot_on_modification": second["proposal_id"], "old_pending_invalidated": first["proposal_id"],
            "modification_saved_nothing": True, "old_display_rejected": True, "independent_confirmation_saved": saved["id"]}


def main():
    evidence = asyncio.run(check())
    (ROOT / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS: six plan tools; strict stable declarations; real SQLite/catalog/message chain; full JSON; registration; ownership; fixed idempotency; restart;")
    print("      mixed business batch correspondence; cross-kind display rejection; persist-then-bind; strict null/UUID/reps-range inputs;")
    print("      business and harness error shapes; modification creating new snapshot without saving. Evidence:", ROOT)


if __name__ == "__main__":
    main()
