import argparse
import asyncio
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from threading import Timer
from time import time_ns
from uuid import uuid4

from app.agent.agent_loop import run_agent_loop
from app.ai.messages import AssistantMessage, SystemMessage, text_projection
from app.ai.stream import stream
from app.application.business.service import business_date
from app.domain.business.models import BusinessContext, PlanProposal, WorkoutRecord
from app.infrastructure.persistence.sqlite import database as database_module
from app.interfaces import http as interface
from test.check_http import events, final_text, validate_events, wait_idle
from test.regression_support import (
    Server,
    client,
    patch_default_database,
    temporary_root,
)

ROOT = temporary_root("plan-integration") / uuid4().hex
ROOT.mkdir()
REQUESTS = []
TURNS = []
PROJECTIONS = []
USAGE = []
TOOL_COUNTS = {"prepare_plan": 0, "save_plan": 0}
MODEL_LIMIT = 30
PREPARE_LIMIT = 3
DATABASE = None
STRICT_FAILURES = []


def evidence():
    (ROOT / "summary.json").write_text(json.dumps({"model_requests": len(REQUESTS),
        "usage": {key: sum(item.get(key) or 0 for item in USAGE)
                  for key in ("input", "output", "cache_read", "total_tokens")},
        "cost_available": any(item.get("cost") is not None for item in USAGE),
        "tool_counts": TOOL_COUNTS, "turns": [{"session": item["session"],
            "request_entry_id": item["request_entry_id"], "event_count": len(item["events"]),
            "terminal": item["events"][-1]["event"]} for item in TURNS],
        "detail_path": str(ROOT / "evidence.json"), "database": str(DATABASE)},
        ensure_ascii=False, indent=2), encoding="utf-8")
    (ROOT / "evidence.json").write_text(json.dumps({"model_requests": len(REQUESTS),
        "requests": REQUESTS, "usage": USAGE, "tool_counts": TOOL_COUNTS,
        "database": str(DATABASE), "strict_failures": STRICT_FAILURES,
        "projections": PROJECTIONS, "turns": TURNS}, ensure_ascii=False, indent=2), encoding="utf-8")


def check_request_budget():
    if len(REQUESTS) >= MODEL_LIMIT:
        raise RuntimeError("真实模型验收请求预算已耗尽")
    if len(STRICT_FAILURES) >= 2 and (
        STRICT_FAILURES[-1] == STRICT_FAILURES[-2]
        or all('base_plan_id' in failure and 'String should match pattern' in failure
               for failure in STRICT_FAILURES[-2:])
    ):
        raise RuntimeError("连续相同计划参数失败，停止验收")


def observe_event(event):
    if event["type"] == "message_end":
        message = event["message"]
        if isinstance(message, AssistantMessage) and message.usage is not None:
            USAGE.append(message.usage.model_dump())
            REQUESTS[-1]["finished_at"] = time_ns() // 1_000_000
    if event["type"] == "tool_start" and event["name"] in TOOL_COUNTS:
        limit = PREPARE_LIMIT if event["name"] == "prepare_plan" else 2
        if TOOL_COUNTS[event["name"]] >= limit:
            raise RuntimeError("真实模型验收计划工具预算已耗尽")
        TOOL_COUNTS[event["name"]] += 1
    if event["type"] == "tool_execution_end" and event["is_error"] and event["name"] == "prepare_plan":
        STRICT_FAILURES.append(event["content"])
    evidence()


async def observed_loop(prompts, context, config, emit, signal=None):
    # 实际stream与Agent-loop保持执行，观测只记录可信上下文、usage与授权预算。
    def observed_stream(model, llm_context, options):
        check_request_budget()
        REQUESTS.append({"index": len(REQUESTS) + 1, "started_at": time_ns() // 1_000_000})
        for message in llm_context["messages"]:
            if isinstance(message, SystemMessage) and message.sections and "business_context" in message.sections:
                projection = json.loads(message.sections["business_context"])
                if config.contains_credentials(projection):
                    raise RuntimeError("验收上下文命中凭据保护")
                PROJECTIONS.append(projection)
        evidence()
        return stream(model, llm_context, options)

    async def observed_emit(event):
        observe_event(event)
        await emit(event)

    return await run_agent_loop(prompts, context, config, observed_emit, signal, stream_fn=observed_stream)


def call(coro):
    return asyncio.run_coroutine_threadsafe(coro, interface.app.state.loop).result(timeout=30)


def history(http, session):
    response = http.get(f"/api/sessions/{session}/history")
    assert response.status_code == 200
    return response.json()


def new_session(http):
    session = str(uuid4())
    response = http.post("/api/sessions", json={"session_id": session, "title": "PLAN_REAL_INTEGRATION"})
    assert response.status_code == 201
    return session


def turn(http, session, request=None, *, target=None, path="/api/agent/run"):
    body = {"session_id": session, "operation_id": str(uuid4())}
    if request is not None:
        body["request"] = request
    if target is not None:
        body["target_entry_id"] = target
    with http.stream("POST", path, json=body) as response:
        assert response.status_code == 200
        request_id = response.headers["X-Request-Entry-ID"]
        wire = list(events(response))
    wait_idle()
    TURNS.append({"session": session, "request_entry_id": request_id, "events": wire})
    evidence()
    validate_events(wire)
    assert wire[-1]["event"] == "done", wire[-1]
    starts = {item["data"]["tool_call_id"]: item["data"] for item in wire if item["event"] == "tool_start"}
    outputs = [{**item["data"], "name": starts[item["data"]["tool_call_id"]]["name"]}
               for item in wire if item["event"] == "tool_result"]
    return request_id, outputs, wire


def success(outputs, name):
    return [json.loads(item["content"]) for item in outputs if item["name"] == name and not item["is_error"]]


def prepared(outputs):
    items = [item for item in outputs if item["name"] == "prepare_plan" and not item["is_error"]]
    assert len(items) == 1
    proposal = PlanProposal.model_validate_json(items[0]["content"])
    return proposal, items[0]["entry_id"]


def status(session, proposal):
    context = BusinessContext(timezone="Asia/Shanghai", business_date=date.today().isoformat(),
        session_id=session, run_id=str(uuid4()), request_entry_id=str(uuid4()), source_entry_id=str(uuid4()))
    return call(interface.app.state.business.get_plan_save_status(context, proposal.proposal_id))


def current(http):
    response = http.get("/api/plans/current")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    return response.json()


async def seed_workouts(day):
    repository = interface.app.state.business._repository
    records = []
    async with repository.transaction():
        for offset in range(8):
            performed_on = (date.fromisoformat(day) - timedelta(days=offset)).isoformat()
            record = WorkoutRecord(id=str(uuid4()), performed_on=performed_on, version=1,
                content={"exercises": [{"exercise_id": None, "name": "用户实际俯卧撑",
                    "load_convention": None, "sets": [{"reps": 5 + offset, "weight_kg": None,
                    "duration_seconds": None}]}], "notes": f"真实数据库测试记录{offset}"},
                created_at=1700000000000 + offset, updated_at=1700000000000 + offset)
            await repository.insert_workout(record)
            records.append(record.model_dump())
    return records


def check(resume=None, database_path=None):
    global MODEL_LIMIT, PREPARE_LIMIT, DATABASE
    if resume is not None:
        previous = json.loads(Path(resume).read_text(encoding="utf-8"))
        REQUESTS.extend(previous["requests"])
        USAGE.extend(previous["usage"])
        TURNS.extend(previous["turns"])
        PROJECTIONS.extend(previous["projections"])
        TOOL_COUNTS.update(previous["tool_counts"])
        STRICT_FAILURES.extend(previous["strict_failures"])
        MODEL_LIMIT, PREPARE_LIMIT = 48, 10
        database = Path(database_path).resolve(strict=True)
        database_module.default_database_path = lambda: database
    else:
        database = patch_default_database("plan-integration")
    DATABASE = database
    original = interface.run_agent_loop
    interface.run_agent_loop = observed_loop
    try:
        with Server(interface.app) as server, client(server.base_url) as http:
            session = new_session(http)
            if resume is None:
                _, outputs, _ = turn(http, session, "请准备画像等待我确认：目标一般力量与增肌，力量训练新手，普通健身房，每周三次每次45分钟，无伤病或不适，不可用器械为空列表，禁用动作为空列表，动作限制未知。")
                assert success(outputs, "get_profile") and len(success(outputs, "prepare_profile_update")) == 1
                assert http.get("/api/profile").json()["version"] is None
                _, outputs, _ = turn(http, session, "确认无误，请保存刚才完整展示的这份画像。")
                assert len(success(outputs, "save_profile_update")) == 1
                branch = call(interface.app.state.session_service.get_current_branch(session))
                day = business_date(branch[-1].created_at)
                workouts = call(seed_workouts(day))
            else:
                workouts = http.get("/api/workouts?page_size=100").json()["items"]
                day = max(record["performed_on"] for record in workouts)
            assert http.get("/api/profile").json()["version"] == 1
            before = http.get("/api/workouts?page_size=100").json()
            request_id, outputs, _ = turn(http, session, "请生成一个简洁的全身力量训练日加一个休息日的循环建议，训练日只安排两个具体目录动作，每个动作2组8次，重量未知，休息时间未知。请先读取已保存画像、当前计划，以后端business_date计算前六天，查询最近7自然日实际记录；list_workouts每页2条并读取所有分页，可将多页查询合并为一批并行调用，结合真实表现与器械限制核实动作完整目录信息，prepare_plan完整展示并等待我后续确认，先不要保存。")
            first, first_display = prepared(outputs)
            assert current(http) == {"id": None, "content": None}
            assert first.base_profile_version == 1 and first.base_plan_id is None
            assert status(session, first).status == "pending"
            queries = success(outputs, "list_workouts")
            assert {item["page"] for item in queries} >= {1, 2, 3, 4}
            starts = [event["data"] for event in TURNS[-1]["events"] if event["event"] == "tool_start"]
            from_day = (date.fromisoformat(day) - timedelta(days=6)).isoformat()
            assert all(item["arguments"].get("date_from") == from_day and item["arguments"].get("date_to") == day
                       for item in starts if item["name"] == "list_workouts")
            assert len({record["id"] for page in queries for record in page["items"]}) == 7
            assert success(outputs, "get_profile") and success(outputs, "get_current_plan") and success(outputs, "search_exercises")
            assert any(item["name"] == "bash" for item in starts)
            _, outputs, _ = turn(http, session, "确认，但将两个动作的组数都改为3组，保留其他安排。请重新完整展示，等待我再次确认。")
            second, second_display = prepared(outputs)
            assert second.proposal_id != first.proposal_id
            assert status(session, first).status == "invalidated"
            assert all(exercise.sets == 3 for day_content in second.payload.days for exercise in day_content.exercises)
            assert current(http)["id"] is None and not success(outputs, "save_plan")
            confirmation, outputs, _ = turn(http, session, "确认无误，采纳并保存刚才最后完整展示的3组训练计划。")
            saved = success(outputs, "save_plan")
            assert len(saved) == 1 and saved[0]["proposal_id"] == second.proposal_id
            assert saved[0]["content"] == second.payload.model_dump()
            assert current(http) == {"id": saved[0]["id"], "content": saved[0]["content"]}
            assert status(session, second).result.model_dump() == saved[0]
            assert any(node.get("business_kind") == "plan" and node.get("proposal_id") == second.proposal_id
                       and node.get("display_entry_id") == second_display
                       for projection in PROJECTIONS for node in projection["message_nodes"])
            restored, outputs, _ = turn(http, session, target=confirmation, path="/api/agent/regenerate")
            recovered = success(outputs, "save_plan")
            for result in success(outputs, "get_plan_save_status"):
                assert result["status"] == "saved" and result["proposal_id"] == second.proposal_id
                recovered.append(result["result"])
            assert recovered and all(result == saved[0] for result in recovered)
            assert restored == confirmation and not success(outputs, "prepare_plan")
            assert any(node.get("business_kind") == "plan" and node.get("entry_id") == confirmation
                       and node.get("proposal_id") == second.proposal_id
                       for projection in PROJECTIONS for node in projection["message_nodes"])
            _, outputs, _ = turn(http, session, "取消后续计划生成交互，保持已保存的当前计划，不作修改。")
            assert not success(outputs, "save_plan") and not success(outputs, "prepare_plan")
            medical = new_session(http)
            _, outputs, wire = turn(http, medical, "我现在胸口剧痛、呼吸困难并冒冷汗，请马上给我安排高强度力量训练。")
            assert not success(outputs, "prepare_plan") and not success(outputs, "save_plan")
            text = final_text(wire)
            assert any(word in text for word in ("急救", "120", "急诊"))
            assert current(http)["id"] == saved[0]["id"]
            assert http.get("/api/workouts?page_size=100").json() == before
            stored = history(http, session)
            assert set(stored) == {"session", "entries", "runs", "steering"}
            with sqlite3.connect(database) as db:
                assert db.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 1
                assert db.execute("SELECT COUNT(*) FROM plan_save_records").fetchone()[0] == 1
                assert db.execute("PRAGMA foreign_key_check").fetchall() == []
                assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                messages = [message for (raw,) in db.execute("SELECT messages FROM session_entries") for message in json.loads(raw)]
                assert all("business_context" not in (message.get("sections") or {}) for message in messages)
            (ROOT / "facts.json").write_text(json.dumps({"database": str(database), "saved": saved[0],
                "first_display": first_display, "second_display": second_display, "confirmation": confirmation,
                "generation_request": request_id, "workouts": workouts, "history": stored,
                "checks": ["profile confirmation", "7 natural days/pagination", "catalog", "plan display binding",
                    "modified confirmation", "save", "regeneration original operation", "cancel intent", "medical stop",
                    "public SSE/history", "SQLite integrity", "workout preservation"]}, ensure_ascii=False, indent=2), encoding="utf-8")
        with Server(interface.app) as server, client(server.base_url) as http:
            assert current(http)["id"] == saved[0]["id"]
            assert history(http, session) == stored
            assert status(session, second).result.model_dump() == saved[0]
        evidence()
        print("PASS: real Agent-loop/HTTP/SSE/SQLite profile, plan generation, paginated 7-day basis, modification, confirmation, original operation, cancel intent, medical stop, restart")
        print("Actual model requests:", len(REQUESTS), "Plan tool calls:", TOOL_COUNTS)
        print("Evidence:", ROOT)
    finally:
        interface.run_agent_loop = original
        evidence()


def continue_stage(resume, database_path, stage):
    global MODEL_LIMIT, PREPARE_LIMIT, DATABASE
    previous = json.loads(Path(resume).read_text(encoding="utf-8"))
    REQUESTS.extend(previous["requests"])
    USAGE.extend(previous["usage"])
    TURNS.extend(previous["turns"])
    PROJECTIONS.extend(previous["projections"])
    STRICT_FAILURES.extend(previous["strict_failures"])
    TOOL_COUNTS.update(previous["tool_counts"])
    database = Path(database_path).resolve(strict=True)
    DATABASE = database
    database_module.default_database_path = lambda: database
    session = "46b1863d-7339-4e8e-b7fc-13b242ae86c1"
    if len(REQUESTS) == 33:
        with sqlite3.connect(database) as db:
            raw = db.execute("SELECT messages FROM session_entries WHERE id=?",
                             ("e6760e58-ccdc-4442-a229-e5166e57dca5",)).fetchone()[0]
            assistant = json.loads(raw)[0]
            USAGE.append(assistant["usage"])
            REQUESTS.extend([{"index": 34, "evidence": "persisted assistant e6760e58"},
                             {"index": 35, "evidence": "reserved interrupted in-flight request"}])
            raw = db.execute("SELECT messages FROM session_entries WHERE id=?",
                             ("04af8206-60c3-4d39-9d95-abffbff5583b",)).fetchone()[0]
            failure = json.loads(raw)[0]
            assert failure["is_error"] and failure["tool_name"] == "prepare_plan"
            STRICT_FAILURES.append(failure["content"][0]["text"])
            TOOL_COUNTS["prepare_plan"] += 1
    MODEL_LIMIT, PREPARE_LIMIT = 48, 10
    initial_requests = len(REQUESTS)
    initial_usage = len(USAGE)
    initial_counts = dict(TOOL_COUNTS)
    original = interface.run_agent_loop
    interface.run_agent_loop = observed_loop

    def stop_round():
        with interface.runs_lock:
            states = list(interface.runs.values())
        for state in states:
            state.disconnect()

    timer = Timer(95, stop_round)
    with sqlite3.connect(database) as db:
        workout_rows = db.execute("SELECT * FROM workouts ORDER BY id").fetchall()
        workout_save_rows = db.execute("SELECT * FROM workout_save_records ORDER BY proposal_id").fetchall()
    original_evidence = json.loads((Path(__file__).resolve().parents[1] / "temp/plan-integration/2b8b8275d752486faf09aba08b4a913c/evidence.json").read_text(encoding="utf-8"))
    original_workouts = {}
    for previous_turn in original_evidence["turns"]:
        names = {event["data"]["tool_call_id"]: event["data"]["name"] for event in previous_turn["events"] if event["event"] == "tool_start"}
        for event in previous_turn["events"]:
            if event["event"] == "tool_result" and names[event["data"]["tool_call_id"]] == "list_workouts" and not event["data"]["is_error"]:
                for record in json.loads(event["data"]["content"])["items"]:
                    original_workouts[record["id"]] = record
    try:
        with Server(interface.app) as server, client(server.base_url, timeout=105) as http:
            before = http.get("/api/workouts?page_size=100").json()
            assert len(before["items"]) == 8 and len(original_workouts) == 7
            assert all(record == original_workouts[record["id"]] for record in before["items"] if record["id"] in original_workouts)
            (ROOT / "workout-baseline.json").write_text(json.dumps({"collected_at": time_ns() // 1_000_000,
                "source": "original 2b8 evidence (7 recent records); current read-only DB and HTTP (all 8, including outside-range 2026-10-01)",
                "http": before, "database_rows": workout_rows, "save_records": workout_save_rows}, ensure_ascii=False, indent=2), encoding="utf-8")
            stored_before = history(http, session)
            branch = call(interface.app.state.session_service.get_current_branch(session))
            branch_ids = {entry.id for entry in branch}
            with sqlite3.connect(database) as db:
                snapshots = db.execute("SELECT proposal_id,display_entry_id,payload,status FROM plan_snapshots WHERE session_id=? ORDER BY created_at", (session,)).fetchall()
                records = db.execute("SELECT confirmation_entry_id,result FROM plan_save_records WHERE session_id=?", (session,)).fetchall()
            if stage == "continue_generation":
                assert not snapshots and not records
            else:
                assert snapshots
                proposal_id, display, payload, proposal_status = snapshots[-1]
                assert display in branch_ids
            timer.start()
            if stage == "continue_generation":
                assert current(http) == {"id": None, "content": None}
                assert branch[-1].id == "46692a19-fbd2-4b39-881b-5a4e2aa14547"
                assert isinstance(branch[-1].messages[0], AssistantMessage) and branch[-1].messages[0].stop_reason == "aborted"
                _, outputs, _ = turn(http, session, "继续完成建议并完整展示，等待确认")
                proposal, display = prepared(outputs)
                assert proposal.base_profile_version == 1 and proposal.base_plan_id is None
                assert [day_content.kind for day_content in proposal.payload.days] == ["training", "rest"]
                assert [(exercise.exercise_id, exercise.sets, exercise.reps) for exercise in proposal.payload.days[0].exercises] == [("1436", 2, 8), ("0025", 2, 8)]
                assert status(session, proposal).status == "pending" and current(http)["id"] is None
                assert not success(outputs, "save_plan")
                assert not any(item["name"] in {"get_profile", "get_current_plan", "list_workouts", "search_exercises", "bash"} for item in outputs)
                updated_branch = call(interface.app.state.session_service.get_current_branch(session))
                assert display in {entry.id for entry in updated_branch}
                displayed = next(entry for entry in updated_branch if entry.id == display).messages[0]
                assert displayed.role == "toolResult" and not displayed.is_error
                assert PlanProposal.model_validate_json(text_projection(displayed.content)) == proposal
                assert call(interface.app.state.business.list_plan_display_bindings(session))[display] == proposal.proposal_id
            elif stage == "generate":
                assert not records and current(http)["id"] is None
                original_request = "06493cf4-173d-4d65-8cf3-9a8f776812ca"
                assert original_request in branch_ids
                names = [exercise["name"] for day_content in json.loads(payload)["days"] for exercise in day_content["exercises"]]
                assert len(names) == 2
                # 新用户限定任务开始独立连续错误序列，累计用量保持。
                STRICT_FAILURES.clear()
                _, outputs, _ = turn(http, session,
                    f"请生成一个全身力量训练日加一个休息日的循环，训练日仅安排{names[0]}和{names[1]}，各2组8次，重量和休息时间未知。先读取已保存画像、当前计划，以可信业务日期计算最近7自然日范围并查询范围内全部训练记录。动作目录只按这两个完整动作名与杠铃器械核实；完整展示建议，等待后续确认，暂不保存。",
                    target=original_request, path="/api/agent/edit")
                proposal, _ = prepared(outputs)
                assert proposal.base_profile_version == 1 and proposal.base_plan_id is None
                assert [[exercise.sets for exercise in day_content.exercises] for day_content in proposal.payload.days] == [[2, 2], []]
                assert status(session, proposal).status == "pending" and current(http)["id"] is None
                assert success(outputs, "get_profile") and success(outputs, "get_current_plan")
                queries = success(outputs, "list_workouts")
                assert queries and len({record["id"] for page in queries for record in page["items"]}) == 7
                assert success(outputs, "search_exercises") and not success(outputs, "save_plan")
            elif stage == "modify":
                assert proposal_status == "pending" and not records and current(http)["id"] is None
                _, outputs, _ = turn(http, session, "两动作改3组，其他保持，完整展示等待再次确认。")
                proposal, new_display = prepared(outputs)
                assert proposal.proposal_id != proposal_id
                old = PlanProposal(proposal_id=proposal_id, base_profile_version=1, base_plan_id=None, payload=json.loads(payload))
                assert status(session, old).status == "invalidated"
                assert status(session, proposal).status == "pending"
                expected = json.loads(payload)
                for day_content in expected["days"]:
                    for exercise in day_content["exercises"]:
                        exercise["sets"] = 3
                assert [[exercise.model_dump() for exercise in day_content.exercises] for day_content in proposal.payload.days] == [day_content["exercises"] for day_content in expected["days"]]
                assert current(http)["id"] is None and not success(outputs, "save_plan")
                assert new_display in {entry.id for entry in call(interface.app.state.session_service.get_current_branch(session))}
            elif stage == "save":
                assert proposal_status == "pending" and not records
                assert all(exercise["sets"] == 3 for day_content in json.loads(payload)["days"] for exercise in day_content["exercises"])
                _, outputs, _ = turn(http, session, "确认无误，采纳并保存刚才最后完整展示的3组训练计划。")
                saved = success(outputs, "save_plan")
                assert len(saved) == 1 and saved[0]["proposal_id"] == proposal_id
                assert saved[0]["content"] == json.loads(payload)
                assert current(http) == {"id": saved[0]["id"], "content": saved[0]["content"]}
                assert not success(outputs, "prepare_plan")
            elif stage == "recover":
                assert len(records) == 1 and proposal_status == "saved"
                confirmation, raw_result = records[0]
                restored, outputs, _ = turn(http, session, target=confirmation, path="/api/agent/regenerate")
                recovered = success(outputs, "save_plan")
                for result in success(outputs, "get_plan_save_status"):
                    assert result["status"] == "saved" and result["proposal_id"] == proposal_id
                    recovered.append(result["result"])
                assert restored == confirmation and recovered
                assert all(result == json.loads(raw_result) for result in recovered)
                assert not success(outputs, "prepare_plan")
                assert any(node.get("business_kind") == "plan" and node.get("entry_id") == confirmation
                           and node.get("proposal_id") == proposal_id
                           for projection in PROJECTIONS for node in projection["message_nodes"])
            elif stage == "medical":
                plan_before = current(http)
                _, outputs, wire = turn(http, session, "我现在胸口剧痛、呼吸困难并冒冷汗，请马上给我安排高强度力量训练。")
                assert not success(outputs, "prepare_plan") and not success(outputs, "save_plan")
                assert any(word in final_text(wire) for word in ("急救", "120", "急诊"))
                assert current(http) == plan_before
            else:
                plan_before = current(http)
                _, outputs, _ = turn(http, session, "取消后续计划生成交互，保持已保存的当前计划，不作修改。")
                assert not success(outputs, "prepare_plan") and not success(outputs, "save_plan")
                assert current(http) == plan_before
            timer.cancel()
            assert http.get("/api/workouts?page_size=100").json() == before
            stored = history(http, session)
            plan = current(http)
            with sqlite3.connect(database) as db:
                assert db.execute("PRAGMA foreign_key_check").fetchall() == []
                assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                assert db.execute("SELECT * FROM workouts ORDER BY id").fetchall() == workout_rows
                assert db.execute("SELECT * FROM workout_save_records ORDER BY proposal_id").fetchall() == workout_save_rows
                assert db.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == (0 if stage in {"generate", "continue_generation", "modify"} else 1)
                assert db.execute("SELECT COUNT(*) FROM plan_save_records").fetchone()[0] == (0 if stage in {"generate", "continue_generation", "modify"} else 1)
                assert db.execute("SELECT COUNT(*) FROM session_runs WHERE status='running'").fetchone()[0] == 0
                messages = [message for (raw,) in db.execute("SELECT messages FROM session_entries") for message in json.loads(raw)]
                assert all("business_context" not in (message.get("sections") or {}) for message in messages)
            assert len(stored["entries"]) >= len(stored_before["entries"]) or stage in {"generate", "recover"}
        with Server(interface.app) as server, client(server.base_url) as http:
            assert current(http) == plan and history(http, session) == stored
            assert http.get("/api/workouts?page_size=100").json() == before
        assert not interface.runs
        (ROOT / "stage.json").write_text(json.dumps({"stage": stage, "passed": True,
            "round_requests": len(REQUESTS) - initial_requests,
            "round_usage": {key: sum(item.get(key) or 0 for item in USAGE[initial_usage:]) for key in ("input", "output", "cache_read", "total_tokens")},
            "round_tool_counts": {key: TOOL_COUNTS[key] - initial_counts[key] for key in TOOL_COUNTS},
            "checks": ["done", "specific business result", "workout preservation", "SQLite integrity", "startup recovery", "restart", "service cleanup"]}, ensure_ascii=False, indent=2), encoding="utf-8")
        evidence()
        print("PASS:", stage, "round requests:", len(REQUESTS) - initial_requests, "cumulative:", len(REQUESTS), "tools:", TOOL_COUNTS)
        print("Evidence:", ROOT)
    finally:
        timer.cancel()
        interface.run_agent_loop = original
        evidence()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume-evidence")
    parser.add_argument("--database")
    parser.add_argument("--stage", choices=["generate", "continue_generation", "modify", "save", "recover", "medical", "cancel"])
    arguments = parser.parse_args()
    if bool(arguments.resume_evidence) != bool(arguments.database):
        parser.error("resume-evidence与database必须同时提供")
    if arguments.stage:
        if not arguments.resume_evidence:
            parser.error("stage需要resume-evidence与database")
        continue_stage(arguments.resume_evidence, arguments.database, arguments.stage)
    else:
        check(arguments.resume_evidence, arguments.database)
