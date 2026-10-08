import argparse
import asyncio
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

from app.agent.agent_loop import run_agent_loop
from app.ai.messages import (
    AssistantMessage,
    SystemMessage,
    ToolCall,
    ToolResultMessage,
    text_projection,
)
from app.ai.stream import AssistantResponse
from app.infrastructure.persistence.sqlite import database as database_module
from app.interfaces import http as interface
from test.check_http import events, validate_events, wait_idle
from test.check_plan_core import profile
from test.check_plan_integration import STRICT_FAILURES, check_request_budget
from test.check_profile_confirmation import Fixture
from test.check_workout_service_http import prepare as prepare_workout
from test.regression_support import Server, client, temporary_root

# 验证层级：装配层真实 HTTP/SSE/SQLite/工具与 Agent-loop 装配回归。助手消息逐字取自真实运行已持久化的
# 原文，测试只注入模型响应入口，不合成、不修改任何助手消息字段，因此不构成真实模型 Agent 全流程验收。
# 合法参数的业务流程（准备、展示绑定、确认保存、幂等、取消、失效）在真实工具与服务层验证：
# check_plan_tools.py 与 check_plan_core.py。模型网络请求 0 次。
LEVEL = "装配层：真实 HTTP/SSE/SQLite/工具，逐字回放真实模型消息，0 次模型网络请求"
ROOT = temporary_root("plan-agent-errors") / uuid4().hex
ROOT.mkdir()
PLAN_CALL = "prepare_plan"
SAVING_TOOLS = {"save_plan", "save_profile_update", "save_workout", "update_workout"}
CAPTURED: list[dict] = []
COUNTED: set[str] = set()
ENTERED: list[dict] = []
REPLAYED: list[int] = []
STATE = {"script": [], "index": 0, "round": ""}


def raw_text(content: list) -> str:
    return "".join(block["text"] for block in content if block.get("type") == "text")


def calls_of(message: dict) -> list[dict]:
    return [block for block in message.get("content", []) if block.get("type") == "toolCall"]


def load_real_chain(source: str) -> dict:
    """在真实证据里按结构定位只读并行批次与两次 prepare_plan 严格失败，逐字取出助手消息原文。"""
    path = Path(source).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"缺少真实运行证据数据库：{path}")
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as db:
        rows = db.execute("""SELECT id,session_id,created_at,messages FROM session_entries
                             ORDER BY created_at,id""").fetchall()
    paired: dict[str, list[dict]] = {}
    for _, _, _, raw in rows:
        message = json.loads(raw)[0]
        if message.get("role") == "toolResult":
            paired.setdefault(message["tool_call_id"], []).append(message)
    strict: dict[str, list[tuple]] = {}
    for entry_id, session_id, created_at, raw in rows:
        message = json.loads(raw)[0]
        if message.get("role") != "assistant":
            continue
        calls = calls_of(message)
        if len(calls) != 1 or calls[0]["name"] != PLAN_CALL or calls[0]["arguments"].get("base_plan_id") != "None":
            continue
        if any(item.get("is_error") for item in paired.get(calls[0]["id"], [])):
            strict.setdefault(session_id, []).append((created_at, entry_id))
    if not strict:
        raise AssertionError("真实证据缺少 base_plan_id 严格校验失败的 prepare_plan 助手消息")
    session_id, marks = max(strict.items(), key=lambda item: item[1][-1][0])
    if len(marks) < 2:
        raise AssertionError(f"真实证据会话 {session_id} 只有一次 base_plan_id 严格失败，不足以复现连续失败守卫")
    required = set(marks)
    sequence, errors, contexts = [], {}, []
    for entry_id, entry_session, created_at, raw in rows:
        if entry_session != session_id:
            continue
        message = json.loads(raw)[0]
        if message.get("role") != "assistant":
            continue
        calls = calls_of(message)
        if not calls:
            continue
        names = [call["name"] for call in calls]
        failure = (created_at, entry_id) in required
        batch = len(calls) >= 2 and not [name for name in names
                                        if name.startswith("prepare_") or name in SAVING_TOOLS]
        if not (failure or batch):
            continue
        if batch and errors:
            raise AssertionError("真实证据在严格失败之后又出现只读并行批次，回放顺序需要重新核对")
        if failure:
            call = calls[0]
            errors[call["id"]] = next(raw_text(item["content"]) for item in paired[call["id"]] if item.get("is_error"))
        for call in calls:
            contexts.append({"call_id": call["id"], "name": call["name"], "arguments": call["arguments"]})
        sequence.append(message)
        if len(errors) == 2:
            break
    if len(errors) != 2 or not any(len(calls_of(message)) >= 2 for message in sequence):
        raise AssertionError(f"真实证据链路不完整：批次 {sum(1 for m in sequence if len(calls_of(m)) >= 2)}，"
                             f"严格失败 {len(errors)}")
    results = {}
    for call in contexts:
        for item in paired.get(call["call_id"], []):
            if not item.get("is_error"):
                results[call["call_id"]] = raw_text(item["content"])
    range_calls = [call for call in contexts if call["name"] == "list_workouts"]
    if not range_calls:
        raise AssertionError("真实证据缺少带日期区间的 list_workouts 调用，无法复现最近七日依据读取")
    arguments = range_calls[0]["arguments"]
    if date.fromisoformat(arguments["date_to"]) - date.fromisoformat(arguments["date_from"]) != timedelta(days=6):
        raise AssertionError(f"真实证据的 list_workouts 区间不是 7 自然日：{arguments}")
    return {"session_id": session_id, "sequence": sequence, "errors": errors, "results": results,
            "calls": contexts, "date_from": arguments["date_from"], "date_to": arguments["date_to"]}


def projection(messages: list) -> dict:
    for message in reversed(messages):
        if isinstance(message, SystemMessage) and message.sections and "business_context" in message.sections:
            return json.loads(message.sections["business_context"])
    raise AssertionError("模型上下文缺少 business_context 投影")


def inspect_context(messages: list) -> None:
    # 本批工具结果在进入上下文前已持久化：先登记失败事实，再在下一模型请求入口执行守卫。
    for message in messages:
        if isinstance(message, ToolResultMessage) and message.tool_name == PLAN_CALL and message.is_error:
            if message.tool_call_id not in COUNTED:
                COUNTED.add(message.tool_call_id)
                STRICT_FAILURES.append(text_projection(message.content))
    calls, results = set(), set()
    for message in messages:
        if isinstance(message, AssistantMessage):
            calls |= {block.id for block in message.content if isinstance(block, ToolCall)}
        elif isinstance(message, ToolResultMessage):
            results.add(message.tool_call_id)
    check_request_budget()
    assert calls <= results, f"存在未配对的助手工具调用：{sorted(calls - results)}"


def scripted(message: dict) -> AssistantResponse:
    assistant = AssistantMessage.model_validate(message)

    async def source():
        partial = assistant.model_copy(update={"content": [], "stop_reason": "pending", "usage": None})
        yield {"type": "start", "partial": partial}
        yield {"type": "done", "reason": assistant.stop_reason, "message": assistant}

    return AssistantResponse(source())


def replayed_stream(model, llm_context, options):
    messages = llm_context["messages"]
    ENTERED.append({"index": len(ENTERED) + 1, "round": STATE["round"]})
    inspect_context(messages)
    if STATE["index"] >= len(STATE["script"]):
        raise AssertionError(f"真实消息脚本已用尽：{STATE['round']}，测试不得合成助手消息")
    REPLAYED.append(len(ENTERED))
    CAPTURED.append({"round": STATE["round"], "entered": len(ENTERED), "messages": messages})
    message = STATE["script"][STATE["index"]]
    STATE["index"] += 1
    return scripted(message)


async def replay_loop(prompts, context, config, emit, signal=None, stream_fn=None):
    return await run_agent_loop(prompts, context, config, emit, signal, stream_fn=replayed_stream)


def turn(http, session, text, round_name):
    STATE.update(index=0, round=round_name)
    with http.stream("POST", "/api/agent/run", json={"session_id": session, "operation_id": str(uuid4()),
                                                    "request": text}) as response:
        assert response.status_code == 200
        request_id = response.headers["X-Request-Entry-ID"]
        wire = list(events(response))
    wait_idle()
    validate_events(wire)
    starts = {item["data"]["tool_call_id"]: item["data"] for item in wire if item["event"] == "tool_start"}
    endings = {item["data"]["tool_call_id"]: item["data"] for item in wire if item["event"] == "tool_execution_end"}
    outputs = []
    for item in wire:
        if item["event"] != "tool_result":
            continue
        data = item["data"]
        ending = endings[data["tool_call_id"]]
        assert ending["content"] == data["content"] and ending["is_error"] == data["is_error"]
        assert "entry_id" not in ending and "entry_id" in data and "parent_id" in data
        start = starts[data["tool_call_id"]]
        outputs.append({**data, "name": start["name"], "arguments": start["arguments"]})
    return request_id, outputs, wire


def counts(database: Path) -> dict:
    with sqlite3.connect(database) as db:
        return {
            "plan_snapshots": db.execute("SELECT COUNT(*) FROM plan_snapshots").fetchone()[0],
            "plans": db.execute("SELECT COUNT(*) FROM plans").fetchone()[0],
            "plan_save_records": db.execute("SELECT COUNT(*) FROM plan_save_records").fetchone()[0],
            "running": db.execute("SELECT COUNT(*) FROM session_runs WHERE status='running'").fetchone()[0],
            "failed": db.execute("SELECT COUNT(*) FROM session_runs WHERE status='failed'").fetchone()[0],
        }


def branch_entries(database: Path, session: str) -> list[dict]:
    with sqlite3.connect(database) as db:
        leaf = db.execute("SELECT active_leaf_id FROM sessions WHERE id=?", (session,)).fetchone()[0]
        rows = []
        current = leaf
        while current is not None:
            row = db.execute("SELECT id,parent_id,messages FROM session_entries WHERE id=?", (current,)).fetchone()
            rows.append({"id": row[0], "parent_id": row[1], "message": json.loads(row[2])[0]})
            current = row[1]
        return rows[::-1]


def pairing(database: Path, session: str) -> tuple[set, set]:
    calls, results = set(), set()
    for item in branch_entries(database, session):
        message = item["message"]
        if message.get("role") == "assistant":
            calls |= {block["id"] for block in message.get("content", []) if block.get("type") == "toolCall"}
        elif message.get("role") == "toolResult":
            results.add(message["tool_call_id"])
    return calls, results


def preserved_rows(database: Path) -> dict:
    tables = ["profile", "workouts", "workout_save_records", "workout_snapshots", "exercises",
              "profile_snapshots", "profile_save_records"]
    with sqlite3.connect(database) as db:
        return {table: db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in tables}  # noqa: S608


async def seed_state(database: Path, chain: dict) -> dict:
    """真实服务与真实工具建档并保存最近七日范围内的实际训练记录，供逐字回放的只读调用读取。"""
    f = await Fixture(database).seeded()
    try:
        session = await f.session("装配层真实错误链")
        await profile(f, session)
        saved = await f.business.get_profile()
        anchor = date.fromisoformat(chain["date_to"])
        records = []
        for offset in range(3):
            performed = (anchor - timedelta(days=offset)).isoformat()
            context, arguments, _ = await prepare_workout(f, session, performed)
            records.append(await f.business.save_workout(context, arguments))
        assert chain["date_from"] <= records[-1].performed_on <= chain["date_to"]
        return {"profile": saved.model_dump(), "workouts": [record.model_dump() for record in records]}
    finally:
        await f.close()


def check(source: str) -> dict:
    chain = load_real_chain(source)
    database = temporary_root("plan-agent-errors") / f"{uuid4().hex}.db"
    seeded = asyncio.run(seed_state(database, chain))
    STATE["script"] = chain["sequence"]
    expected_names = [call["name"] for call in chain["calls"]]
    expected_errors = [call["call_id"] in chain["errors"] for call in chain["calls"]]
    database_module.default_database_path = lambda: database
    original = interface.run_agent_loop
    interface.run_agent_loop = replay_loop
    before = preserved_rows(database)
    evidence: dict = {"level": LEVEL, "source": str(Path(source).resolve()), "database": str(database)}
    try:
        with Server(interface.app) as server, client(server.base_url) as http:
            session = str(uuid4())
            assert http.post("/api/sessions", json={"session_id": session,
                                                    "title": "PLAN_SSE_REAL_REPLAY"}).status_code == 201
            assert counts(database) == {"plan_snapshots": 0, "plans": 0, "plan_save_records": 0,
                                       "running": 0, "failed": 0}
            request_id, outputs, wire = turn(http, session, "先读取真实依据，再依据依据准备完整建议。", "real_chain")

            trip = wire[-1]
            assert trip["event"] == "error" and trip["data"]["code"] == "execution_failed", trip
            assert [item["name"] for item in outputs] == expected_names, (
                [item["name"] for item in outputs], expected_names)
            assert [item["is_error"] for item in outputs] == expected_errors, (
                [(item["name"], item["is_error"]) for item in outputs], expected_errors)
            assert [item["tool_call_id"] for item in outputs] == [call["call_id"] for call in chain["calls"]]
            assert len(ENTERED) == len(REPLAYED) + 1, (ENTERED, REPLAYED)
            assert REPLAYED == list(range(1, len(chain["sequence"]) + 1))
            by_call = {item["tool_call_id"]: item for item in outputs}

            read_only = [call for call in chain["calls"] if call["call_id"] not in chain["errors"]]
            assert all(not by_call[call["call_id"]]["is_error"] for call in read_only)
            profile_calls = [call for call in read_only if call["name"] == "get_profile"]
            assert json.loads(by_call[profile_calls[0]["call_id"]]["content"]) == seeded["profile"]
            current_calls = [call for call in read_only if call["name"] == "get_current_plan"]
            assert json.loads(by_call[current_calls[0]["call_id"]]["content"]) == {"id": None, "content": None}
            assert by_call[current_calls[0]["call_id"]]["content"] == chain["results"][current_calls[0]["call_id"]]
            bash_calls = [call for call in read_only if call["name"] == "bash"]
            assert bash_calls and by_call[bash_calls[0]["call_id"]]["content"] == chain["results"][bash_calls[0]["call_id"]]
            assert by_call[bash_calls[0]["call_id"]]["content"].strip() == f"{chain['date_from']} {chain['date_to']}"
            catalog_calls = [call for call in read_only if call["name"] == "search_exercises"]
            assert catalog_calls and all(by_call[call["call_id"]]["content"] == chain["results"][call["call_id"]]
                                         for call in catalog_calls)
            workout_calls = [call for call in read_only if call["name"] == "list_workouts"]
            assert workout_calls and all(by_call[call["call_id"]]["arguments"]["date_from"] == chain["date_from"]
                                         and by_call[call["call_id"]]["arguments"]["date_to"] == chain["date_to"]
                                         for call in workout_calls)
            page = json.loads(by_call[workout_calls[0]["call_id"]]["content"])
            assert page["total"] == len(seeded["workouts"])
            stored_items = {item["id"]: item for item in page["items"]}
            for record in seeded["workouts"]:
                item = stored_items[record["id"]]
                assert {key: item[key] for key in ("performed_on", "version", "content")} == {
                    key: record[key] for key in ("performed_on", "version", "content")}

            failure_calls = [call for call in chain["calls"] if call["call_id"] in chain["errors"]]
            assert [call["call_id"] for call in failure_calls] == list(chain["errors"])
            for call in failure_calls:
                got = by_call[call["call_id"]]
                assert got["name"] == PLAN_CALL and got["is_error"]
                assert got["content"] == chain["errors"][call["call_id"]]
                assert got["content"].startswith('工具 "prepare_plan" 参数校验失败')
                assert "String should match pattern" in got["content"] and '"type": "null"' in got["content"]
                assert '"base_plan_id": "None"' in got["content"]
                persisted = [item["message"] for item in branch_entries(database, session)
                             if item["message"].get("tool_call_id") == call["call_id"]]
                assert len(persisted) == 1
                assert persisted[0]["role"] == "toolResult" and persisted[0]["is_error"] is True
                assert persisted[0]["tool_name"] == PLAN_CALL
                assert raw_text(persisted[0]["content"]) == got["content"]
                assert got["arguments"] == call["arguments"]

            # 第一次严格失败结果持久化后，循环继续向模型发起请求；真实模型再次给出同样的非法参数。
            assert len(STRICT_FAILURES) == 2 and STRICT_FAILURES[0] == STRICT_FAILURES[1] == chain["errors"][
                failure_calls[1]["call_id"]]
            second_context = CAPTURED[len(chain["sequence"]) - 1]["messages"]
            assert any(isinstance(item, ToolResultMessage) and item.is_error
                       and item.tool_call_id == failure_calls[0]["call_id"]
                       and text_projection(item.content) == chain["errors"][failure_calls[0]["call_id"]]
                       for item in second_context)

            assistants = [item["message"] for item in branch_entries(database, session)
                          if item["message"].get("role") == "assistant"]
            assert assistants == chain["sequence"], "持久化助手消息与真实原文不一致"
            assert counts(database) == {"plan_snapshots": 0, "plans": 0, "plan_save_records": 0,
                                       "running": 0, "failed": 1}
            calls, results = pairing(database, session)
            assert calls == results == {call["call_id"] for call in chain["calls"]}
            assert len(calls) == len(chain["calls"])
            assert preserved_rows(database) == before

            history = http.get(f"/api/sessions/{session}/history").json()
            assert set(history) == {"session", "entries", "runs", "steering"}
            assert http.get("/api/plans/current").json() == {"id": None, "content": None}
            assert http.get("/api/plans").json() == []
            failed_run = next(run for run in history["runs"] if run["status"] == "failed")
            assert failed_run["error_code"] == "execution_failed"
            for call in failure_calls:
                entry = next(item for item in history["entries"] if item["entry_id"] == by_call[call["call_id"]]["entry_id"])
                assert entry["parent_id"] == by_call[call["call_id"]]["parent_id"]
                assert entry["message"]["role"] == "toolResult" and entry["message"]["is_error"] is True
                assert entry["message"]["tool_name"] == PLAN_CALL
                assert entry["message"]["content"] == chain["errors"][call["call_id"]]
                assert entry["message"]["tool_call_id"] == call["call_id"]
            projection_nodes = [node for node in projection(CAPTURED[-1]["messages"])["message_nodes"]
                                if node.get("business_kind") == "plan"]
            assert projection_nodes == []
            request_nodes = [node for node in projection(CAPTURED[-1]["messages"])["message_nodes"]
                             if node["role"] == "user"]
            assert [node["entry_id"] for node in request_nodes] == [request_id]
            assert projection(CAPTURED[-1]["messages"])["request_entry_id"] == request_id
        with Server(interface.app) as server, client(server.base_url) as http:
            restarted = http.get(f"/api/sessions/{session}/history").json()
            assert restarted == history
            assert http.get("/api/plans/current").json() == {"id": None, "content": None}
        assert not interface.runs
        with sqlite3.connect(database) as db:
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert db.execute("SELECT COUNT(*) FROM plans WHERE is_current=1").fetchone()[0] == 0
            assert db.execute("SELECT version FROM profile").fetchone()[0] == 1
            messages = [json.loads(raw)[0] for (raw,) in db.execute("SELECT messages FROM session_entries")]
            assert all("business_context" not in (item.get("sections") or {}) for item in messages)
        evidence.update({
            "replayed_session": chain["session_id"], "read_range": [chain["date_from"], chain["date_to"]],
            "replayed_calls": [{"name": call["name"], "is_error": call["call_id"] in chain["errors"],
                                "chars": len(by_call[call["call_id"]]["content"])} for call in chain["calls"]],
            "verbatim_assistant_messages": len(chain["sequence"]),
            "guard_trip": {"entered": len(ENTERED), "replayed": len(REPLAYED),
                           "model_requests_allowed": len(REPLAYED), "blocked_next_request": 1,
                           "consecutive_strict_failures": len(STRICT_FAILURES), "terminal": trip["data"]},
            "no_plan_writes": counts(database), "seeded_profile": seeded["profile"],
            "seeded_workouts": [record["id"] for record in seeded["workouts"]],
            "checks": ["verbatim real assistant replay", "parallel read-only batch through real tools",
                       "real business date range and catalog results", "strict failure toolResult identity",
                       "error content persisted and published unchanged", "model continues after first error",
                       "guard blocks next model request after whole batch", "no unpaired assistant tool call",
                       "no snapshot plan or save record written", "profile workout catalog rows preserved",
                       "failed run terminal", "restart", "service cleanup", "sqlite fk and integrity"]})
        return evidence
    finally:
        interface.run_agent_loop = original
        STRICT_FAILURES.clear()
        COUNTED.clear()
        (ROOT / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str),
                                            encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-database", required=True)
    arguments = parser.parse_args()
    evidence = check(arguments.source_database)
    print("PASS:", evidence["level"])
    print("Replayed real calls:", [(item["name"], item["is_error"]) for item in evidence["replayed_calls"]])
    print("Guard:", evidence["guard_trip"], "| model network requests: 0")
    print("Evidence:", ROOT)


if __name__ == "__main__":
    main()
