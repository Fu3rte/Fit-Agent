# 运行：在 backend 执行 python -m test.check_trace_run
import asyncio
import json
import re
import sqlite3
import time
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from app.agent import agent_loop
from app.agent import telemetry as telemetry_module
from app.agent.budget import RequestBudget
from app.agent.config import AgentLoopConfig
from app.agent.telemetry import RunTrace, TraceSpan
from app.ai.messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    Usage,
    UserMessage,
)
from app.interfaces.http import (
    CredentialFilter,
    active,
    app,
    public_content,
    public_text_visible,
)
from app.model_config import load_model_config
from test.check_http import (
    create_session,
    events,
    final_text,
    validate_events,
    wait_idle,
)
from test.regression_support import (
    Server,
    client,
    install_test_model_config,
    patch_default_database,
    patch_trace_database,
    temporary_root,
)

BACKEND = Path(__file__).resolve().parents[1]
ROOT = temporary_root("trace-run")
EVIDENCE = ROOT / "evidence.json"
PLAIN_PROMPT = "只回答 OK，无需使用工具。"
BUSY_PROMPT = "逐行输出从 1 到 100000 的整数，不要省略。无需使用工具。"

RESULTS: dict[str, object] = {}
TRACE_PATH: Path = ROOT / "unset.db"


def read_spans(path: Path, run_id: str) -> list[dict]:
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT * FROM spans WHERE run_id = ? ORDER BY span_id", (run_id,)
        ).fetchall()
    finally:
        connection.close()
    return [{**dict(row), "attributes": json.loads(row["attributes"])} for row in rows]


def trace_roots(path: Path, run_id: str) -> list[dict]:
    # 单根唯一：整条链路只允许一条根记录。
    roots = [row for row in read_spans(path, run_id) if row["parent_span_id"] is None]
    assert len(roots) == 1, roots
    return roots


def trace_root(path: Path, run_id: str) -> dict:
    root = trace_roots(path, run_id)[0]
    assert root["name"] == "agent_run"
    assert root["span_id"] == 1
    assert re.fullmatch(r"[0-9a-f]{32}", root["trace_id"])
    return root


def trace_chain(path: Path, run_id: str) -> list[dict]:
    # 整条链路记录：身份一致、Span 编号唯一且递增、父子关系闭合在同一 Trace 内。
    rows = read_spans(path, run_id)
    assert rows, "链路缺失"
    ids = [row["span_id"] for row in rows]
    assert ids == sorted(ids) and len(set(ids)) == len(ids), ids
    assert ids[0] == 1
    assert len({row["trace_id"] for row in rows}) == 1
    known = set(ids)
    assert all(row["parent_span_id"] in known for row in rows[1:])
    return rows


def run_stream(http, session: str, prompt: str, *, capture_done: bool = False):
    operation_id = str(uuid4())
    capture: dict[str, list] = {}
    with http.stream(
        "POST",
        "/api/agent/run",
        json={"session_id": session, "operation_id": operation_id, "request": prompt},
    ) as response:
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/event-stream")
        run_id = response.headers["X-Run-ID"]
        request_entry_id = response.headers["X-Request-Entry-ID"]
        result = []
        for event in events(response):
            result.append(event)
            if capture_done and event["event"] == "done":
                capture["at_done"] = read_spans(TRACE_PATH, run_id)
    wait_idle()
    return run_id, operation_id, request_entry_id, result, capture.get("at_done")


def run_send(http, session: str, prompt: str, *, capture_done: bool = False):
    run_id, operation_id, entry_id, result, at_done = run_stream(
        http, session, prompt, capture_done=capture_done
    )
    assert result[-1]["event"] == "done" and final_text(result), result[-1]
    validate_events(result)
    return run_id, operation_id, entry_id, at_done


def assistant_message(content) -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=content,
        api="test",
        provider="test",
        model="test",
        usage=Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2),
        stop_reason="stop",
        timestamp=0,
    )


def unit_first_text_and_identity() -> dict:
    # 纯数据边界：记录器语义（首次写入一次、受理起点、迟到更新无效、白名单与凭据边界）。
    trace = RunTrace(
        session_id=str(uuid4()),
        operation_id=str(uuid4()),
        run_id=str(uuid4()),
        accepted_at=(time.time_ns() // 1_000_000) - 100,
        accepted_perf=perf_counter() - 0.1,
    )
    seen: dict[str, str] = {}

    def root_body(root: TraceSpan) -> str:
        seen["trace_id"] = root.trace_id
        root.record_first_text()
        time.sleep(0.2)
        # 第二次调用必须被忽略：取值停留在第一次。
        root.record_first_text()
        return "value"

    assert trace.start_span("agent_run", {}, root_body) == "value"
    record = trace.records()[0]
    assert seen["trace_id"] == trace.trace_id
    assert record.attributes["first_text_ms"] >= 90
    assert record.attributes["first_text_ms"] < 250

    unknown = RunTrace(
        session_id=str(uuid4()), operation_id=str(uuid4()), run_id=str(uuid4())
    )
    unknown.start_span("agent_run", {}, lambda root: "value")
    assert unknown.records()[0].attributes == {}

    nested = RunTrace(
        session_id=str(uuid4()), operation_id=str(uuid4()), run_id=str(uuid4())
    )

    def nested_body(root: TraceSpan) -> str:
        # 首正文属性只落在白名单内 Span：turn 属性为空对象，写入被拒绝。
        return root.start_span("turn", {}, lambda child: (child.record_first_text(), "x")[1])

    nested.start_span("agent_run", {}, nested_body)
    assert nested.records()[0].attributes == {}
    assert nested.records()[1].attributes == {}

    no_record = TraceSpan(trace)
    no_record.record_first_text()
    assert no_record.trace_id == trace.trace_id

    guarded_session = str(uuid4())
    guarded = RunTrace(
        session_id=guarded_session,
        operation_id=str(uuid4()),
        run_id=str(uuid4()),
        contains=CredentialFilter((guarded_session,)).contains,
    )
    guarded.start_span("agent_run", {}, lambda root: (root.record_first_text(), "value")[1])
    assert guarded.records() == []

    return {
        "first_text_ms": round(record.attributes["first_text_ms"], 3),
        "unknown_attributes": unknown.records()[0].attributes,
        "turn_attributes": nested.records()[1].attributes,
        "credential_records": len(guarded.records()),
    }


def unit_first_text_public_boundary() -> dict:
    # 纯数据边界：调用实际安全投影与记录器，按公开队列判定逐边界驱动。
    secret = "sk-" + "x" * 60
    guard = CredentialFilter((secret,))
    short = assistant_message([TextContent(type="text", text="OK")])
    long = assistant_message([TextContent(type="text", text="答案" * 200)])
    empty = assistant_message([TextContent(type="text", text="")])
    thinking = assistant_message([ThinkingContent(type="thinking", thinking="推理")])
    tool = assistant_message(
        [ToolCall(type="toolCall", id="call-1", name="echo", arguments={"text": "hi"})]
    )
    hit = assistant_message([TextContent(type="text", text=f"凭据 {secret}")])

    # 原始正文非空而安全投影正文为空：公开判定不成立，原始正文判定会在此提前记录。
    raw_present = any(isinstance(block, TextContent) and block.text for block in short.content)
    assert raw_present
    assert public_content(short, guard, True) == [
        {"content_index": 0, "type": "text", "text": ""}
    ]
    assert public_text_visible(public_content(short, guard, True)) is False
    # 短正文在 message_end 首次完整公开时才构成可见正文。
    assert public_text_visible(public_content(short, guard, False)) is True
    # 正文超过安全保留长度后，截断投影已非空。
    assert public_text_visible(public_content(long, guard, True)) is True
    # 空正文、思考与工具调用不构成正文。
    for message in (empty, thinking, tool):
        assert public_text_visible(public_content(message, guard, True)) is False
        assert public_text_visible(public_content(message, guard, False)) is False
    # 凭据命中由既有安全检查在记录前阻断。
    assert guard.contains(hit.model_dump()) is True

    trace = RunTrace(
        session_id=str(uuid4()),
        operation_id=str(uuid4()),
        run_id=str(uuid4()),
        accepted_perf=perf_counter() - 0.05,
    )

    def root_body(root: TraceSpan) -> str:
        def note(message, truncate: bool) -> None:
            if public_text_visible(public_content(message, guard, truncate)):
                root.record_first_text()

        for message in (empty, thinking, tool, short):
            note(message, True)
        time.sleep(0.3)
        note(short, False)
        time.sleep(0.3)
        note(long, True)
        return "value"

    assert trace.start_span("agent_run", {}, root_body) == "value"
    record = trace.records()[0]
    # 首次测量来自 message_end 的完整公开（≈ 50ms + 300ms），后续事件不改写。
    assert 250 <= record.attributes["first_text_ms"] < 500
    return {
        "raw_present": raw_present,
        "short_update_visible": public_text_visible(public_content(short, guard, True)),
        "short_end_visible": public_text_visible(public_content(short, guard, False)),
        "long_update_visible": public_text_visible(public_content(long, guard, True)),
        "thinking_visible": public_text_visible(public_content(thinking, guard, True)),
        "tool_visible": public_text_visible(public_content(tool, guard, True)),
        "empty_visible": public_text_visible(public_content(empty, guard, True)),
        "credential_hit_blocked": guard.contains(hit.model_dump()),
        "first_text_ms": round(record.attributes["first_text_ms"], 3),
    }


def unit_standalone_loop() -> dict:
    config = load_model_config()
    captured: list = []

    async def emit(event) -> None:
        captured.append(event)

    def drive(trace_span):
        loop_config = AgentLoopConfig(
            model=config, max_turns=1, request_budget=RequestBudget(), trace_span=trace_span
        )
        context = {
            "messages": [
                SystemMessage(
                    role="system", content="只回答完成。", tools_added=[], timestamp=0
                )
            ],
            "tools": {},
        }
        prompt = [
            UserMessage(
                role="user",
                content="只回答 OK，无需使用工具。",
                timestamp=time.time_ns() // 1_000_000,
            )
        ]
        asyncio.run(agent_loop.run_agent_loop(prompt, context, loop_config, emit))

    drive(None)
    standalone_id = captured[0]["trace_id"]
    assert captured[0]["type"] == "trace_start"
    assert re.fullmatch(r"[0-9a-f]{32}", standalone_id)

    captured.clear()
    trace = RunTrace(
        session_id=str(uuid4()), operation_id=str(uuid4()), run_id=str(uuid4())
    )
    drive(TraceSpan(trace))
    assert captured[0]["trace_id"] == trace.trace_id
    assert trace.records() == []
    return {
        "standalone_trace_id": standalone_id,
        "provided_trace_id": trace.trace_id,
        "matches_provided": captured[0]["trace_id"] == trace.trace_id,
    }


def check_normal(http) -> dict:
    session = str(uuid4())
    create_session(http, session, "trace normal")
    run_id, operation_id, _, at_done = run_send(
        http, session, PLAIN_PROMPT, capture_done=True
    )
    run = http.get(f"/api/sessions/{session}/runs/{run_id}").json()
    root = trace_root(TRACE_PATH, run_id)
    assert root["session_id"] == session
    assert root["operation_id"] == operation_id
    assert root["run_id"] == run_id
    assert root["status"] == "completed" and root["error_code"] is None
    assert run["status"] == "completed"
    assert run["started_at"] <= root["started_at"]
    assert run["finished_at"] <= root["finished_at"]
    assert root["duration_ms"] >= 0
    assert set(root["attributes"]) == {"first_text_ms"}
    assert root["attributes"]["first_text_ms"] >= 0
    # 链路：agent_run 根下依次为 turn 与普通 model；父子关系与计数分别验证。
    rows = trace_chain(TRACE_PATH, run_id)
    assert [row["name"] for row in rows] == ["agent_run", "turn", "model"], rows
    assert [row["parent_span_id"] for row in rows] == [None, 1, 2]
    assert len({row["trace_id"] for row in rows}) == 1
    model = rows[2]
    assert model["status"] == "completed"
    assert model["attributes"]["provider"] and model["attributes"]["model"]
    assert model["attributes"]["call_kind"] == "normal"
    assert model["attributes"]["stop_reason"] == "stop"
    assert model["attributes"]["input_tokens"] is not None
    assert model["attributes"]["first_text_ms"] >= 0
    # 终态通知发出时，观测写入尝试已经结束：收到 done 时整条 Trace 已在库内。
    assert at_done is not None
    assert [row["name"] for row in at_done] == ["agent_run", "turn", "model"]
    assert all(row["status"] == "completed" for row in at_done if row["name"] != "model")
    assert at_done[0]["status"] == "completed"

    # 重复受理复用原业务结果，保持原执行次数，不产生第二条 Trace。
    retry = http.post(
        "/api/agent/run",
        json={
            "session_id": session,
            "operation_id": operation_id,
            "request": PLAIN_PROMPT,
        },
    )
    assert retry.status_code == 200
    assert retry.headers["content-type"].startswith("application/json")
    assert retry.json()["run_id"] == run_id
    assert len(read_spans(TRACE_PATH, run_id)) == 3
    assert len(trace_roots(TRACE_PATH, run_id)) == 1
    return {
        "run_id": run_id,
        "root": root,
        "at_done_rows": len(at_done),
        "repeat_run_id": retry.json()["run_id"],
    }


def check_edit_regenerate(http) -> dict:
    session = str(uuid4())
    create_session(http, session, "trace edit regenerate")
    send_run, send_op, user1, _ = run_send(http, session, PLAIN_PROMPT)

    edit_op = str(uuid4())
    with http.stream(
        "POST",
        "/api/agent/edit",
        json={
            "session_id": session,
            "operation_id": edit_op,
            "target_entry_id": user1,
            "request": "只回答完成，无需使用工具。",
        },
    ) as response:
        assert response.status_code == 200, response.text
        edit_run = response.headers["X-Run-ID"]
        new_user = response.headers["X-Request-Entry-ID"]
        assert list(events(response))[-1]["event"] == "done"
    wait_idle()
    edit_root = trace_root(TRACE_PATH, edit_run)
    assert edit_root["operation_id"] == edit_op and edit_root["status"] == "completed"
    assert edit_run != send_run
    assert trace_root(TRACE_PATH, send_run)["operation_id"] == send_op

    regen_op = str(uuid4())
    with http.stream(
        "POST",
        "/api/agent/regenerate",
        json={
            "session_id": session,
            "operation_id": regen_op,
            "target_entry_id": new_user,
        },
    ) as response:
        assert response.status_code == 200, response.text
        regen_run = response.headers["X-Run-ID"]
        assert list(events(response))[-1]["event"] == "done"
    wait_idle()
    regen_root = trace_root(TRACE_PATH, regen_run)
    assert regen_root["operation_id"] == regen_op and regen_root["status"] == "completed"
    assert len({send_run, edit_run, regen_run}) == 3
    return {
        "send_run": send_run,
        "edit_run": edit_run,
        "regen_run": regen_run,
        "edit_trace_id": edit_root["trace_id"],
        "regen_trace_id": regen_root["trace_id"],
    }


def check_steering(http) -> dict:
    session = str(uuid4())
    create_session(http, session, "trace steering")
    steered = False
    with http.stream(
        "POST",
        "/api/agent/run",
        json={"session_id": session, "operation_id": str(uuid4()), "request": BUSY_PROMPT},
    ) as response:
        assert response.status_code == 200, response.text
        run_id = response.headers["X-Run-ID"]
        for event in events(response):
            if event["event"] == "message_update" and not steered:
                accepted = http.post(
                    f"/api/agent/runs/{run_id}/steering",
                    json={
                        "session_id": session,
                        "operation_id": str(uuid4()),
                        "message": "补充说明，请尽快结束。",
                    },
                )
                assert accepted.status_code == 200, accepted.text
                steered = True
                break
    wait_idle()
    assert steered
    root = trace_root(TRACE_PATH, run_id)
    assert root["run_id"] == run_id
    assert root["status"] in {"completed", "failed", "cancelled"}
    return {"run_id": run_id, "steered": steered, "root_status": root["status"]}


def check_cancel_after_text(http) -> dict:
    session = str(uuid4())
    create_session(http, session, "trace cancel after text")
    with http.stream(
        "POST",
        "/api/agent/run",
        json={"session_id": session, "operation_id": str(uuid4()), "request": BUSY_PROMPT},
    ) as response:
        assert response.status_code == 200, response.text
        run_id = response.headers["X-Run-ID"]
        for event in events(response):
            # 断连发生在首个公开非空正文之后。
            if event["event"] == "message_update" and any(
                block["type"] == "text" and block["text"] for block in event["data"]["content"]
            ):
                break
    wait_idle()
    run = http.get(f"/api/sessions/{session}/runs/{run_id}").json()
    assert run["status"] in {"cancelled", "failed"}, run
    root = trace_root(TRACE_PATH, run_id)
    assert root["status"] == run["status"]
    # 取消前已公开正文：保留该次真实测量。
    assert "first_text_ms" in root["attributes"]
    return {
        "run_id": run_id,
        "run_status": run["status"],
        "root_status": root["status"],
        "first_text_ms": root["attributes"]["first_text_ms"],
    }


def check_cancel_before_text(http) -> dict:
    session = str(uuid4())
    create_session(http, session, "trace cancel before text")
    with http.stream(
        "POST",
        "/api/agent/run",
        json={"session_id": session, "operation_id": str(uuid4()), "request": BUSY_PROMPT},
    ) as response:
        assert response.status_code == 200, response.text
        run_id = response.headers["X-Run-ID"]
        # 拿到响应头即断开：模型尚未产出任何公开正文。
    wait_idle()
    run = http.get(f"/api/sessions/{session}/runs/{run_id}").json()
    assert run["status"] in {"cancelled", "failed"}, run
    root = trace_root(TRACE_PATH, run_id)
    assert root["status"] == run["status"]
    # 始终没有公开正文：首正文属性保持未知。
    assert "first_text_ms" not in root["attributes"]
    return {"run_id": run_id, "run_status": run["status"], "root_status": root["status"]}


def check_model_failure(http, config) -> dict:
    import app.interfaces.http as http_module

    original = http_module.load_model_config
    bad = config.model_copy(update={"base_url": "http://127.0.0.1:1/v1"})
    http_module.load_model_config = lambda: bad
    try:
        session = str(uuid4())
        create_session(http, session, "trace failure")
        run_id, _, _, result, _ = run_stream(http, session, PLAIN_PROMPT)
        assert result[-1]["event"] == "error", result[-1]
    finally:
        http_module.load_model_config = original
    run = http.get(f"/api/sessions/{session}/runs/{run_id}").json()
    assert run["status"] == "failed" and run["error_code"] == "execution_failed", run
    root = trace_root(TRACE_PATH, run_id)
    assert root["status"] == "failed" and root["error_code"] == "execution_failed"
    # 始终没有公开正文：首正文属性保持未知。
    assert "first_text_ms" not in root["attributes"]
    # 请求失败的真实结局：模型 Span 记 failed，缺失用量保持省略。
    model = next(row for row in trace_chain(TRACE_PATH, run_id) if row["name"] == "model")
    assert model["status"] == "failed"
    assert "input_tokens" not in model["attributes"]
    return {"run_id": run_id, "run_status": run["status"], "root": root, "model": model}


def check_output_limit(http) -> dict:
    original = agent_loop.DEFAULT_MAX_OUTPUT_TOKENS
    agent_loop.DEFAULT_MAX_OUTPUT_TOKENS = 16
    try:
        session = str(uuid4())
        create_session(http, session, "trace output limit")
        run_id, _, _, result, _ = run_stream(http, session, BUSY_PROMPT)
        assert result[-1]["event"] == "error", result[-1]
    finally:
        agent_loop.DEFAULT_MAX_OUTPUT_TOKENS = original
    run = http.get(f"/api/sessions/{session}/runs/{run_id}").json()
    assert run["status"] == "failed" and run["error_code"] == "model_output_limit_exceeded", run
    root = trace_root(TRACE_PATH, run_id)
    assert root["status"] == "failed" and root["error_code"] == "model_output_limit_exceeded"
    # 合法 length 响应：模型 Span 记 completed 且 stop_reason 为 length；Agent 输出上限为业务失败。
    model = next(row for row in trace_chain(TRACE_PATH, run_id) if row["name"] == "model")
    assert model["status"] == "completed" and model["attributes"]["stop_reason"] == "length"
    return {
        "run_id": run_id,
        "run_status": run["status"],
        "root_status": root["status"],
        "model_status": model["status"],
        "model_stop_reason": model["attributes"]["stop_reason"],
    }


def check_observation_open_failure() -> dict:
    blocked = ROOT / "blocked"
    blocked.mkdir(parents=True, exist_ok=True)
    (blocked / "occupies.db").write_text("占用", encoding="utf-8")
    original = telemetry_module.trace_database_path
    telemetry_module.trace_database_path = lambda: blocked / "occupies.db" / "trace.db"
    try:
        server = Server(app)
        with server, client(server.base_url) as http:
            assert app.state.trace_store is None
            session = str(uuid4())
            create_session(http, session, "trace open failure")
            run_id, _, _, _ = run_send(http, session, PLAIN_PROMPT)
            run = http.get(f"/api/sessions/{session}/runs/{run_id}").json()
            assert run["status"] == "completed"
            assert not app.state.trace_store
        assert not (blocked / "occupies.db" / "trace.db").exists()
        return {"run_status": "completed", "store": None}
    finally:
        telemetry_module.trace_database_path = original


def check_shutdown(store) -> dict:
    closed = False
    try:
        asyncio.run(store._connection.execute("SELECT 1"))
    except Exception as error:
        closed = "no active connection" in str(error)
    assert closed
    assert len(app.state.coordination) == 0
    return {"connection_closed": closed, "coordination": 0}


def check() -> None:
    install_test_model_config()
    patch_default_database("trace-run")
    production_trace = telemetry_module.trace_database_path()
    production_existed = production_trace.exists()
    global TRACE_PATH
    TRACE_PATH = patch_trace_database("trace-run")

    # unit_* 为纯数据边界证据，http_* 为真实 HTTP 链路证据。
    RESULTS["unit_first_text"] = unit_first_text_and_identity()
    RESULTS["unit_first_text_public"] = unit_first_text_public_boundary()
    RESULTS["unit_standalone_loop"] = unit_standalone_loop()
    config = load_model_config()

    server = Server(app)
    with server, client(server.base_url) as http:
        RESULTS["http_normal"] = check_normal(http)
        RESULTS["http_edit_regenerate"] = check_edit_regenerate(http)
        RESULTS["http_steering"] = check_steering(http)
        RESULTS["http_model_failure"] = check_model_failure(http, config)
        RESULTS["http_output_limit"] = check_output_limit(http)
        RESULTS["http_cancel_after_text"] = check_cancel_after_text(http)
        RESULTS["http_cancel_before_text"] = check_cancel_before_text(http)
        store = app.state.trace_store
        assert store is not None
        assert not active.locked()
    RESULTS["http_shutdown"] = check_shutdown(store)
    RESULTS["http_observation_open_failure"] = check_observation_open_failure()
    # 观测库生产默认路径零改动：本次运行只写入隔离目录下的观测库。
    assert production_trace == BACKEND / "data" / "trace.db"
    assert production_trace.exists() == production_existed

    EVIDENCE.write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(
        "Trace 运行接线：受理起点与运行身份、根终态与观测写入顺序、重复请求零重复执行、"
        "编辑/重新生成各自新 Trace、Steering 归属当前运行、断连取消根收尾、"
        "执行失败与输出上限的真实业务结局、首正文按公开队列边界只记一次与未知值、"
        "纯数据边界的公开投影判定、开库故障隔离、观测连接随服务关闭释放、"
        "独立循环沿用无记录上下文、生产数据路径零改动通过"
    )


if __name__ == "__main__":
    check()
