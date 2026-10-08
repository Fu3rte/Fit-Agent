import asyncio
import json
from datetime import date, timedelta
from uuid import uuid4

from app.agent.tool import run_tool_batch
from app.agent.tools.bash import create_bash_tool
from app.agent.tools.business import bind_business_tools, business_tool_declarations
from app.ai.messages import ToolCall
from app.domain.business.models import WorkoutListArguments
from app.domain.session.models import SendCommand, SendRequest
from test.check_profile_confirmation import Fixture, SYSTEM, new_id, payload
from test.check_workout_service_http import CONTENT, assistant
from test.regression_support import run_tool, temporary_root

ROOT = temporary_root("workout-tools") / uuid4().hex
ROOT.mkdir()
DECLARED = {item.name: item for item in business_tool_declarations()}


def body(message):
    assert len(message.content) == 1 and message.content[0].type == "text"
    return json.loads(message.content[0].text)


async def batch(f, session, calls):
    outcome = await f.service.accept_send(SendCommand(operation_id=new_id(), session_id=session,
        request=SendRequest(text="执行当前指定业务")), system_message=SYSTEM)
    run = outcome.run
    source = await f.append(session, run.id, run.request_entry_id,
        assistant(calls[0].name, calls[0].id, calls[0].arguments).model_copy(update={"content": calls}))
    context = f.context(session, run.request_entry_id, source)
    profiles, workouts = {}, {}
    tools = bind_business_tools(f.business, context, f.call, profiles, workouts)
    assert {name: tool.definition() for name, tool in tools.items()} == DECLARED
    for name in ("prepare_workout", "save_workout", "update_workout"):
        assert tools[name].execution_mode == "sequential"
    for name in ("get_workout", "list_workouts", "get_workout_save_status"):
        assert tools[name].execution_mode == "parallel"
    assert all(tools[name].max_output_chars is None for name in DECLARED if name != "search_exercises")
    execution = await run_tool_batch(calls, tools=tools, declared=DECLARED)
    assert execution.failure is None
    results = execution.messages
    displays = {}
    parent = source
    for message in results:
        parent = await f.append(session, run.id, parent, message)
        if not message.is_error and message.tool_name == "prepare_workout":
            await f.business.bind_workout_display_entry(workouts.pop(message.tool_call_id), parent)
            displays[message.tool_call_id] = parent
        if not message.is_error and message.tool_name == "prepare_profile_update":
            await f.business.bind_display_entry(profiles.pop(message.tool_call_id), parent)
            displays[message.tool_call_id] = parent
    await f.service.finish_run(session, run.id, "completed")
    assert not profiles and not workouts
    return context, results, displays


def call(name, arguments):
    return ToolCall(type="toolCall", id=new_id(), name=name, arguments=arguments)


async def check():
    f = await Fixture(ROOT / "tools.db").seeded()
    try:
        session = await f.session("实际工具新增")
        day = f.context(session, new_id(), new_id()).business_date
        args = {"performed_on": day, "base_workout_id": None, "base_workout_version": None,
                "payload": CONTENT}
        prepare = call("prepare_workout", args)
        profile = call("prepare_profile_update", {"profile_id": 1, "base_profile_version": None,
                                                  "payload": payload()})
        _, results, displays = await batch(f, session, [call("list_workouts", {}), profile, prepare])
        proposal = body(results[-1])
        assert all(not result.is_error for result in results)
        assert (await f.business.list_workouts(WorkoutListArguments())).total == 0
        snapshot = await f.repository.get_workout_snapshot(proposal["proposal_id"])
        assert snapshot.payload.model_dump() == CONTENT
        assert snapshot.display_entry_id == displays[prepare.id]
        save_args = {"proposal_id": proposal["proposal_id"], "display_entry_id": displays[prepare.id],
                     "confirmation_entry_id": new_id()}
        # 确认节点采用真实新用户节点，在执行前构建保存调用。
        async def save(session, name, save_args):
            outcome = await f.service.accept_send(SendCommand(operation_id=new_id(), session_id=session,
                request=SendRequest(text="确认保存指定训练快照")), system_message=SYSTEM)
            request = outcome.run.request_entry_id
            arguments = {**save_args, "confirmation_entry_id": request}
            invocation = call(name, arguments)
            source = await f.append(session, outcome.run.id, request,
                                    assistant(name, invocation.id, arguments))
            context = f.context(session, request, source)
            tools = bind_business_tools(f.business, context, f.call, {}, {})
            from app.agent.tool import run_tool_call
            result = await run_tool_call(invocation, tools=tools, declared=DECLARED)
            await f.append(session, outcome.run.id, source, result)
            await f.service.finish_run(session, outcome.run.id, "completed")
            return result

        first = body(await save(session, "save_workout", save_args))
        assert first["version"] == 1
        _, results, _ = await batch(f, session, [call("get_workout", {"workout_id": first["id"]}),
            call("get_workout_save_status", {"proposal_id": proposal["proposal_id"]})])
        assert body(results[0])["version"] == 1 and body(results[1])["result"] == first
        other = await f.session("跨会话更新")
        stale = await f.session("并发旧依据")
        update_args = {**args, "base_workout_id": first["id"], "base_workout_version": 1,
                       "payload": {**CONTENT, "notes": "跨会话实际更新"}}
        async def update_proposal(session):
            invocation = call("prepare_workout", update_args)
            _, messages, nodes = await batch(f, session, [invocation])
            return {"proposal_id": body(messages[0])["proposal_id"],
                    "display_entry_id": nodes[invocation.id]}
        a2, a3 = await update_proposal(other), await update_proposal(stale)
        second = body(await save(other, "update_workout", a2))
        assert second["id"] == first["id"] and second["version"] == 2
        rejected = await save(stale, "update_workout", a3)
        assert rejected.is_error and body(rejected)["code"] == "workout_version_conflict"
        _, results, _ = await batch(f, session, [call("get_workout_save_status", {"proposal_id": proposal["proposal_id"]}),
            call("get_workout", {"workout_id": first["id"]}),
            call("list_workouts", {"session_id": session}),
            call("get_workout", {"workout_id": new_id()})])
        assert body(results[0])["result"] == first and body(results[1])["version"] == 2
        assert results[2].is_error and results[3].is_error
        assert body(results[3])["code"] == "workout_not_found"
        tomorrow = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        _, results, _ = await batch(f, session, [call("prepare_workout", {**args, "performed_on": tomorrow})])
        assert results[0].is_error and body(results[0])["code"] == "invalid_business_payload"
        compatibility_session = await f.session("精确基础None参数")
        compatibility_day = (date.fromisoformat(day) - timedelta(days=5)).isoformat()
        original = call("prepare_workout", {**args, "performed_on": compatibility_day,
            "base_workout_id": "None", "base_workout_version": "None",
            "payload": {**CONTENT, "notes": "None"}})
        _, results, _ = await batch(f, compatibility_session, [original])
        assert not results[0].is_error
        assert body(results[0])["base_workout_id"] is None and body(results[0])["base_workout_version"] is None
        assert body(results[0])["payload"]["notes"] == "None"
        assert original.arguments["base_workout_id"] == original.arguments["base_workout_version"] == "None"
        invalid = [
            {**args, "base_workout_id": "null", "base_workout_version": "null"},
            {**args, "base_workout_id": "", "base_workout_version": ""},
            {**args, "base_workout_id": 0, "base_workout_version": 0},
            {**args, "base_workout_id": first["id"], "base_workout_version": "2"},
            {key: value for key, value in args.items() if key != "base_workout_id"},
            {**args, "payload": {**CONTENT, "exercises": [{**CONTENT["exercises"][0], "exercise_id": "None"}]}},
        ]
        _, rejected_args, _ = await batch(f, compatibility_session,
            [call("prepare_workout", item) for item in invalid])
        assert all(message.is_error for message in rejected_args)
        await f.restart()
        assert (await f.business.get_workout(first["id"])).version == 2
        await f.service.delete_session(other)
        assert await f.repository.get_workout_snapshot(a2["proposal_id"]) is None
        assert await f.repository.get_workout_save_record(a2["proposal_id"]) is not None
        return {"first": first, "second": second, "conflict": body(rejected), "restart": "passed"}
    finally:
        await f.close()


def main():
    evidence = asyncio.run(check())
    dates = []
    bash = create_bash_tool()
    for day, offset, expected in [("2026-01-01", 1, "2025-12-31"),
                                  ("2024-03-01", 1, "2024-02-29"),
                                  ("2026-03-01", 3, "2026-02-26")]:
        command = ("../.venv/Scripts/python.exe -c 'from datetime import date,timedelta; "
                   f'print((date.fromisoformat("{day}")-timedelta(days={offset})).isoformat())' + "'")
        result = run_tool(bash, {"command": command})
        assert not result.is_error and result.content[0].text.strip() == expected
        dates.append({"base": day, "offset": offset, "result": expected})
    evidence["actual_bash_dates"] = dates
    (ROOT / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS: six actual tools/Harness; mixed snapshot bindings; insert/update/conflict; original status; strict identity; restart/delete; actual bash dates:", ROOT)


if __name__ == "__main__":
    main()
