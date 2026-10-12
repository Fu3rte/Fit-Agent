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
from test.regression_support import (
    Server,
    client,
    install_test_model_config,
    temporary_root,
)

# 验证层级：装配层真实 HTTP/SSE/SQLite/工具与 Agent-loop 装配回归。助手消息逐字取自真实运行已持久化的
# 原文，测试只注入模型响应入口，不合成、不修改任何助手消息字段，因此不构成真实模型 Agent 全流程验收。
# 新契约：prepare_plan 的空依据精确字符串 "None" 经参数预处理归一为 null，真实生成回合不再被参数校验拒绝。
# 合法参数的业务流程（准备、展示绑定、确认保存、幂等、取消、失效）在真实工具与服务层验证：
# check_plan_tools.py 与 check_plan_core.py。模型网络请求 0 次。
LEVEL = "装配层：真实 HTTP/SSE/SQLite/工具，逐字回放真实模型消息，0 次模型网络请求"
ROOT = temporary_root("plan-agent-errors") / uuid4().hex
ROOT.mkdir()
PLAN_CALL = "prepare_plan"
PLAN_IMPORT = "prepare_plan_import"
SAVING_TOOLS = {"save_plan", "save_profile_update", "save_workout", "update_workout"}
READ_ONLY = {"get_profile", "get_current_plan", "list_plans", "search_exercises",
             "calculate_date", "list_workouts"}
CAPTURED: list[dict] = []
COUNTED: set[str] = set()
ENTERED: list[dict] = []
REPLAYED: list[int] = []
STATE = {"script": [], "index": 0, "round": ""}


def raw_text(content: list) -> str:
    return "".join(block["text"] for block in content if block.get("type") == "text")


def calls_of(message: dict) -> list[dict]:
    return [block for block in message.get("content", []) if block.get("type") == "toolCall"]


def is_batch(message: dict) -> bool:
    calls = calls_of(message)
    return len(calls) >= 2 and not [
        call for call in calls if call["name"].startswith("prepare_") or call["name"] in SAVING_TOOLS
    ]


def load_real_chain(source: str) -> dict:
    """在真实证据里定位首个含连续 prepare_plan 空依据字符串的生成回合，逐字取出助手消息原文与历史错误文本。"""
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
    sessions: dict[str, list[tuple]] = {}
    for entry_id, session_id, created_at, raw in rows:
        message = json.loads(raw)[0]
        if message.get("role") == "assistant":
            sessions.setdefault(session_id, []).append((entry_id, created_at, message))
    chosen = None
    for session_id, assistants in sessions.items():
        failures = []
        for item in assistants:
            calls = calls_of(item[2])
            if len(calls) != 1 or calls[0]["name"] != PLAN_CALL:
                continue
            if calls[0]["arguments"].get("base_plan_id") != "None":
                continue
            if any(res.get("is_error") for res in paired.get(calls[0]["id"], [])):
                failures.append(item)
        if len(failures) >= 2:
            chosen = (session_id, assistants, failures)
            break
    if chosen is None:
        raise AssertionError('真实证据缺少连续 base_plan_id 字符串 "None" 的 prepare_plan 助手消息')
    session_id, assistants, failures = chosen
    marks = {item[0] for item in failures}
    indices = [index for index, item in enumerate(assistants) if item[0] in marks]
    start, end = min(indices), max(indices)
    while start > 0 and is_batch(assistants[start - 1][2]):
        start -= 1
    while end + 1 < len(assistants):
        end += 1
        if not calls_of(assistants[end][2]):
            break
    sequence = [item[2] for item in assistants[start:end + 1]]
    if not any(is_batch(message) for message in sequence):
        raise AssertionError("真实证据生成回合缺少只读并行批次")
    if calls_of(sequence[-1]) or sequence[-1].get("stop_reason") != "stop":
        raise AssertionError("真实证据生成回合缺少可终止回放的最终消息")
    errors = {}
    for item in failures:
        call = calls_of(item[2])[0]
        errors[call["id"]] = next(
            raw_text(res["content"]) for res in paired[call["id"]] if res.get("is_error"))
    results, calls = {}, []
    for message in sequence:
        for call in calls_of(message):
            calls.append({"call_id": call["id"], "name": call["name"], "arguments": call["arguments"]})
            for res in paired.get(call["id"], []):
                if not res.get("is_error"):
                    results[call["id"]] = raw_text(res["content"])
    return {"session_id": session_id, "sequence": sequence, "errors": errors,
            "results": results, "calls": calls}


def projection(messages: list) -> dict:
    for message in reversed(messages):
        if isinstance(message, SystemMessage) and message.sections and "business_context" in message.sections:
            return json.loads(message.sections["business_context"])
    raise AssertionError("模型上下文缺少 business_context 投影")


def inspect_context(messages: list) -> None:
    # 本批工具结果在进入上下文前已持久化：先登记计划参数失败事实（新契约下不再出现），再在模型请求入口执行守卫。
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


def plan_snapshot_rows(database: Path) -> list[tuple]:
    with sqlite3.connect(database) as db:
        return db.execute("SELECT preparation_kind,base_plan_id,display_entry_id FROM plan_snapshots "
                          "ORDER BY created_at,proposal_id").fetchall()


async def seed_state(database: Path, chain: dict) -> dict:
    """真实服务与真实工具建档；证据含最近训练查询时，按查询区间补种实际训练记录。"""
    f = await Fixture(database).seeded()
    try:
        session = await f.session("装配层真实生成链")
        await profile(f, session)
        saved = await f.business.get_profile()
        ranges = [call for call in chain["calls"] if call["name"] == "list_workouts"]
        records = []
        if ranges:
            anchor = date.fromisoformat(ranges[0]["arguments"]["date_to"])
            for offset in range(3):
                performed = (anchor - timedelta(days=offset)).isoformat()
                context, arguments, _ = await prepare_workout(f, session, performed)
                records.append(await f.business.save_workout(context, arguments))
        return {"profile": saved.model_dump(), "workouts": [record.model_dump() for record in records]}
    finally:
        await f.close()


def direct_guard_trip(historical: dict) -> dict:
    """守卫直测：连续两次相同真实严格失败文本必须停止下一模型请求，守卫实现保持不变。"""
    assert historical, "缺少真实历史失败文本"
    sample = next(iter(historical.values()))
    STRICT_FAILURES.clear()
    STRICT_FAILURES.extend([sample, sample])
    try:
        check_request_budget()
    except RuntimeError as error:
        assert "连续相同计划参数失败" in str(error), str(error)
    else:
        raise AssertionError("连续相同计划参数失败未触发请求预算守卫")
    STRICT_FAILURES.clear()
    return {"sample": sample, "consecutive_strict_failures": 2, "guard": "trip"}


def check(source: str) -> dict:
    install_test_model_config()
    chain = load_real_chain(source)
    database = temporary_root("plan-agent-errors") / f"{uuid4().hex}.db"
    seeded = asyncio.run(seed_state(database, chain))
    STATE["script"] = chain["sequence"]
    prepare_calls = [call for call in chain["calls"] if call["name"] == PLAN_CALL]
    import_calls = [call for call in chain["calls"] if call["name"] == PLAN_IMPORT]
    expected_names = [call["name"] for call in chain["calls"]]
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
            request_id, outputs, wire = turn(http, session, "先读取真实依据，再依据真实画像准备完整生成建议。",
                                             "real_chain")

            assert wire[-1]["event"] == "done", wire[-1]
            assert wire[-1]["data"]["status"] == "completed"
            assert [item["name"] for item in outputs] == expected_names, (
                [item["name"] for item in outputs], expected_names)
            assert not any(item["is_error"] for item in outputs), [
                (item["name"], item["content"][:160]) for item in outputs if item["is_error"]]
            assert [item["tool_call_id"] for item in outputs] == [call["call_id"] for call in chain["calls"]]
            assert len(ENTERED) == len(REPLAYED) == len(chain["sequence"]), (ENTERED, REPLAYED)
            assert REPLAYED == list(range(1, len(chain["sequence"]) + 1))
            by_call = {item["tool_call_id"]: item for item in outputs}

            read_only = [call for call in chain["calls"] if call["name"] in READ_ONLY]
            assert all(not by_call[call["call_id"]]["is_error"] for call in read_only)
            profile_calls = [call for call in read_only if call["name"] == "get_profile"]
            assert json.loads(by_call[profile_calls[0]["call_id"]]["content"]) == seeded["profile"]
            current_calls = [call for call in read_only if call["name"] == "get_current_plan"]
            assert json.loads(by_call[current_calls[0]["call_id"]]["content"]) == {"id": None, "content": None}
            for call in read_only:
                if call["name"] == "get_profile":
                    continue
                assert call["call_id"] in chain["results"], (call["name"], "缺少历史成功结果")
                assert by_call[call["call_id"]]["content"] == chain["results"][call["call_id"]]

            # 新契约：真实回合里的 prepare_plan 空依据字符串现在成功，生成快照依据为 null。
            proposals = []
            for call in prepare_calls:
                got = by_call[call["call_id"]]
                assert not got["is_error"], got["content"]
                assert got["arguments"]["base_plan_id"] == "None"
                detail = json.loads(got["content"])
                assert set(detail) == {"proposal_id", "base_profile_version", "base_plan_id", "payload"}
                assert detail["base_plan_id"] is None and detail["base_profile_version"] == 1
                proposals.append(detail["proposal_id"])
            for call in import_calls:
                detail = json.loads(by_call[call["call_id"]]["content"])
                assert detail["preparation_kind"] == "import" and detail["base_plan_id"] is None
                proposals.append(detail["proposal_id"])
            snapshots = plan_snapshot_rows(database)
            kinds = [row[0] for row in snapshots]
            assert kinds.count("generation") == len(prepare_calls), kinds
            assert sorted(kinds) == sorted(["generation"] * len(prepare_calls) + ["import"] * len(import_calls))
            assert all(row[1] is None for row in snapshots), "生成与录入快照依据均为 null"
            assert all(row[2] is not None for row in snapshots), "每个快照结果持久化后绑定真实展示节点"
            assert counts(database) == {"plan_snapshots": len(snapshots), "plans": 0,
                                       "plan_save_records": 0, "running": 0, "failed": 0}

            assistants = [item["message"] for item in branch_entries(database, session)
                          if item["message"].get("role") == "assistant"]
            assert assistants == chain["sequence"], "持久化助手消息与真实原文不一致"
            calls, results = pairing(database, session)
            assert calls == results == {call["call_id"] for call in chain["calls"]}
            for table, rows in preserved_rows(database).items():
                assert rows == before[table], table

            history = http.get(f"/api/sessions/{session}/history").json()
            assert set(history) == {
                "session", "entries", "runs", "steering", "confirmations", "pending_proposals",
            }
            assert history["confirmations"] == []
            assert http.get("/api/plans/current").json() == {"id": None, "content": None}
            assert http.get("/api/plans").json() == []
            run = next(iter(history["runs"]))
            assert run["status"] == "completed" and run.get("error_code") is None
            for call in [*prepare_calls, *import_calls]:
                entry = next(item for item in history["entries"]
                             if item["entry_id"] == by_call[call["call_id"]]["entry_id"])
                assert entry["message"]["role"] == "toolResult" and entry["message"]["is_error"] is False
                assert entry["message"]["tool_name"] == call["name"]
                assert entry["message"]["tool_call_id"] == call["call_id"]
            plan_nodes = [node for node in projection(CAPTURED[-1]["messages"])["message_nodes"]
                          if node.get("business_kind") == "plan"]
            assert plan_nodes, "成功生成结果须在后端绑定为 plan 展示节点"
            assert {node["proposal_id"] for node in plan_nodes} <= set(proposals), plan_nodes
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

        guard = direct_guard_trip(chain["errors"])
        evidence.update({
            "replayed_session": chain["session_id"],
            "replayed_calls": [{"name": call["name"], "chars": len(by_call[call["call_id"]]["content"])}
                               for call in chain["calls"]],
            "verbatim_assistant_messages": len(chain["sequence"]),
            "prepare_plan_generations": len(proposals),
            "plan_snapshots": [{"preparation_kind": row[0], "base_plan_id": row[1],
                                "display_bound": row[2] is not None} for row in snapshots],
            "guard_direct": guard,
            "no_saves": counts(database), "seeded_profile": seeded["profile"],
            "seeded_workouts": [record["id"] for record in seeded["workouts"]],
            "checks": ["verbatim real assistant replay", "real parallel read-only batch and historical results",
                       "prepare_plan empty-basis string accepted and normalized to JSON null",
                       "generation snapshots bound to real display nodes", "import tool unaffected",
                       "zero saves and no plan writes", "tool pairing complete", "assistant messages persisted verbatim",
                       "restart", "request budget guard still trips on real strict failure text",
                       "sqlite fk and integrity"]})
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
    print("Replayed real calls:", [item["name"] for item in evidence["replayed_calls"]])
    print("Generation snapshots:", len(evidence["plan_snapshots"]),
          "| guard direct:", evidence["guard_direct"]["guard"], "| model network requests: 0")
    print("Evidence:", ROOT)


if __name__ == "__main__":
    main()
