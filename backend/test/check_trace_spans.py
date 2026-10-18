# 运行：在 backend 执行 python -m test.check_trace_spans
import asyncio
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from app.agent.agent_loop import run_compaction
from app.agent.budget import RequestBudget
from app.agent.cache_observation import CacheObserver
from app.agent.compaction import CompactionPreparation, generate_compaction_summary
from app.agent.config import CompactionHooks, LoopCompactionResult
from app.agent.telemetry import RunTrace, TraceSpan, uuid4_canonical
from app.agent.tool import BeforeToolCallResult, run_tool_batch
from app.agent.tools.files import create_file_tools
from app.ai.messages import TextContent, ToolCall, UserMessage
from app.ai.model_capabilities import resolve_model_spec
from app.application.session.credentials import CredentialFilter
from app.interfaces.http import app
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
ROOT = temporary_root("trace-spans")
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


def trace_chain(path: Path, run_id: str) -> list[dict]:
    rows = read_spans(path, run_id)
    assert rows, "链路缺失"
    ids = [row["span_id"] for row in rows]
    assert ids == sorted(ids) and len(set(ids)) == len(ids), ids
    assert ids[0] == 1
    assert len({row["trace_id"] for row in rows}) == 1
    known = set(ids)
    assert sum(1 for row in rows if row["parent_span_id"] is None) == 1
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
        run_id = response.headers["X-Run-ID"]
        result = []
        for event in events(response):
            result.append(event)
            if capture_done and event["event"] == "done":
                capture["at_done"] = read_spans(TRACE_PATH, run_id)
    wait_idle()
    return run_id, operation_id, result, capture.get("at_done")


def drive(coroutine):
    return asyncio.run(coroutine)


def agent_trace(**kwargs) -> RunTrace:
    return RunTrace(
        session_id=uuid4_canonical(),
        operation_id=uuid4_canonical(),
        run_id=uuid4_canonical(),
        **kwargs,
    )


def http_normal_chain(http) -> dict:
    session = str(uuid4())
    create_session(http, session, "spans normal")
    run_id, operation_id, result, at_done = run_stream(
        http, session, PLAIN_PROMPT, capture_done=True
    )
    assert result[-1]["event"] == "done" and final_text(result)
    validate_events(result)
    rows = trace_chain(TRACE_PATH, run_id)
    assert [row["name"] for row in rows] == ["agent_run", "turn", "model"], rows
    assert [row["parent_span_id"] for row in rows] == [None, 1, 2]
    turn, model = rows[1], rows[2]
    assert turn["attributes"] == {}
    assert model["attributes"]["call_kind"] == "normal"
    assert model["attributes"]["stop_reason"] == "stop"
    assert model["status"] == "completed"
    assert model["attributes"]["input_tokens"] is not None
    assert model["attributes"]["first_text_ms"] >= 0
    # 子操作计时闭合在父操作内：model 不晚于 turn 完成，turn 不晚于根。
    assert model["duration_ms"] <= turn["duration_ms"] <= rows[0]["duration_ms"]
    # 收到终态通知时整条链路写入尝试已结束。
    assert at_done is not None and [row["name"] for row in at_done] == [
        "agent_run",
        "turn",
        "model",
    ]
    # 记录器已释放：内存快照清空，Span 编号唯一。
    assert len({row["span_id"] for row in rows}) == 3
    return {
        "run_id": run_id,
        "chain": [[row["span_id"], row["parent_span_id"], row["name"]] for row in rows],
        "model": model["attributes"],
        "at_done_rows": len(at_done),
    }


def http_model_failure(http, config) -> dict:
    from app.interfaces.http_api.routes import agent as http_module

    original = http_module.load_model_config
    bad = config.model_copy(update={"base_url": "http://127.0.0.1:1/v1"})
    http_module.load_model_config = lambda: bad
    try:
        session = str(uuid4())
        create_session(http, session, "spans failure")
        run_id, _, result, _ = run_stream(http, session, PLAIN_PROMPT)
        assert result[-1]["event"] == "error", result[-1]
    finally:
        http_module.load_model_config = original
    rows = trace_chain(TRACE_PATH, run_id)
    model = next(row for row in rows if row["name"] == "model")
    turn = next(row for row in rows if row["name"] == "turn")
    # 请求失败与消费失败都按真实结局结算：模型 failed，缺失用量保持省略。
    assert model["status"] == "failed" and turn["status"] == "failed"
    assert "input_tokens" not in model["attributes"]
    return {"model_status": model["status"], "turn_status": turn["status"]}


def http_output_limit(http) -> dict:
    import app.agent.agent_loop as agent_loop

    original = agent_loop.DEFAULT_MAX_OUTPUT_TOKENS
    agent_loop.DEFAULT_MAX_OUTPUT_TOKENS = 16
    try:
        session = str(uuid4())
        create_session(http, session, "spans output limit")
        run_id, _, result, _ = run_stream(http, session, BUSY_PROMPT)
        assert result[-1]["event"] == "error", result[-1]
    finally:
        agent_loop.DEFAULT_MAX_OUTPUT_TOKENS = original
    rows = trace_chain(TRACE_PATH, run_id)
    root = rows[0]
    model = next(row for row in rows if row["name"] == "model")
    # 合法 length 响应：模型 completed 且 stop_reason=length；Agent 输出上限为业务失败。
    assert root["status"] == "failed" and root["error_code"] == "model_output_limit_exceeded"
    assert model["status"] == "completed"
    assert model["attributes"]["stop_reason"] == "length"
    return {
        "root_status": root["status"],
        "model_status": model["status"],
        "model_stop_reason": model["attributes"]["stop_reason"],
    }


def http_cancel(http) -> dict:
    session = str(uuid4())
    create_session(http, session, "spans cancel")
    with http.stream(
        "POST",
        "/api/agent/run",
        json={"session_id": session, "operation_id": str(uuid4()), "request": BUSY_PROMPT},
    ) as response:
        assert response.status_code == 200, response.text
        run_id = response.headers["X-Run-ID"]
        # 消费到第一个公开正文后断开：模型请求已开始。
        for event in events(response):
            if event["event"] == "message_update" and any(
                block["type"] == "text" and block["text"] for block in event["data"]["content"]
            ):
                break
    wait_idle()
    run = http.get(f"/api/sessions/{session}/runs/{run_id}").json()
    rows = trace_chain(TRACE_PATH, run_id)
    root = rows[0]
    model = next(row for row in rows if row["name"] == "model")
    assert root["status"] == run["status"]
    assert model["status"] in {"cancelled", "failed"}
    assert "first_text_ms" in root["attributes"]
    return {"run_status": run["status"], "model_status": model["status"]}


def setup_workspace_file(session_id: str, name: str, content: str) -> str:
    from app.domain.session.attachments import TMP_ROOT

    directory = TMP_ROOT / "sessions" / session_id / "workspace"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(content, encoding="utf-8")
    return f"sessions/{session_id}/workspace/{name}"


def tool_task_cancellation() -> dict:
    from threading import Event

    session = str(uuid4())
    tools = create_file_tools(session)
    declared = {name: tool.definition() for name, tool in tools.items()}
    path = setup_workspace_file(session, "cancel.txt", "真实取消测试\n")
    results = {}

    class HookCancelled(asyncio.CancelledError):
        pass

    async def scenario(stage, mode, with_signal):
        trace = agent_trace()
        entered = asyncio.Event()
        completed = asyncio.Event()
        gate = asyncio.Event()
        signal = Event() if with_signal else None
        original = HookCancelled() if stage == "subclass" else RuntimeError("完成通知失败")

        async def before(context, signal):
            if stage == "parallel":
                return None
            entered.set()
            if stage == "subclass":
                raise original
            if stage == "failure":
                return None
            if stage == "cooperative":
                return None
            await gate.wait()

        async def after(context, signal):
            if context.tool_call.id == "waiting":
                entered.set()
                await gate.wait()
            return None

        async def finalized(result):
            if stage == "failure":
                raise original
            if result.message.tool_call_id == "done":
                completed.set()

        async def started(call):
            if stage == "cooperative" and call.id == "trigger":
                signal.set()

        async def body(root):
            turn = root.open("turn", {})
            ids = (["done", "waiting"] if stage == "parallel" else
                   ["waiting", "trigger"] if stage == "cooperative" else ["waiting"])
            task = asyncio.create_task(run_tool_batch(
                [ToolCall(type="toolCall", id=identifier, name="read", arguments={"path": path})
                 for identifier in ids],
                tools=tools, declared=declared, before_tool_call=before,
                after_tool_call=after if stage == "parallel" else None,
                on_tool_start=started, on_tool_finalized=finalized, signal=signal,
                execution_mode=mode, telemetry_span=turn,
            ))
            await asyncio.wait_for(entered.wait(), 10)
            if stage == "parallel":
                await asyncio.wait_for(completed.wait(), 10)
            if stage in {"prepare", "parallel"}:
                task.cancel()
            batch = await task
            assert [message.tool_call_id for message in batch.messages] == (
                ["done"] if stage == "parallel" else []
            )
            if stage in {"prepare", "parallel", "subclass"}:
                assert isinstance(batch.failure, asyncio.CancelledError)
            if batch.failure is not None:
                turn.settle(batch.failure)
                raise batch.failure
            turn.set_status("cancelled")
            turn.settle()
            root.set_status("cancelled")
            return batch

        failure = None
        try:
            batch = await trace.start_span("agent_run", {}, body)
        except (asyncio.CancelledError, RuntimeError) as error:
            failure = error
            batch = None
        rows = trace.records()
        statuses = {row.attributes["tool_call_id"]: row.status.value
                    for row in rows if row.name.value == "tool"}
        expected = "failed" if stage == "failure" else "cancelled"
        assert statuses["waiting"] == expected, statuses
        assert all(row.status.value == expected for row in rows[:2]), rows
        assert all(row.finished_at <= rows[1].finished_at <= rows[0].finished_at
                   for row in rows[2:]), rows
        if stage == "parallel":
            assert statuses["done"] == "completed", statuses
        if stage in {"subclass", "failure"}:
            assert failure is original
        elif stage != "cooperative":
            assert isinstance(failure, asyncio.CancelledError), failure
        else:
            assert failure is None and batch.failure is None and batch.messages == []
        results[f"{stage}-{mode}-signal={with_signal}"] = {
            "failure": type(failure).__name__ if failure else None,
            "tool_statuses": statuses,
            "parent_statuses": [row.status.value for row in rows[:2]],
        }

    for mode in ("parallel", "sequential"):
        for with_signal in (False, True):
            drive(scenario("prepare", mode, with_signal))
    for with_signal in (False, True):
        drive(scenario("parallel", "parallel", with_signal))
    drive(scenario("subclass", "parallel", True))
    drive(scenario("failure", "parallel", True))
    drive(scenario("cooperative", "parallel", True))
    return results


def tool_spans() -> dict:
    session_id = str(uuid4())
    tools = create_file_tools(session_id)
    declared = {tool.name: tool.definition() for tool in tools.values()}
    path = setup_workspace_file(session_id, "note.txt", "行一\n行二\n")

    def call(identifier: str, name: str, arguments: dict) -> ToolCall:
        return ToolCall(type="toolCall", id=identifier, name=name, arguments=arguments)

    # 真并行：两个真实 read 调用共享同一 Turn 父 Span，各自独立编号。
    parallel = agent_trace()

    async def parallel_body(root: TraceSpan):
        turn = root.open("turn", {})
        batch = await run_tool_batch(
            [call("c1", "read", {"path": path}), call("c2", "read", {"path": path})],
            tools=tools,
            declared=declared,
            execution_mode="parallel",
            telemetry_span=turn,
        )
        turn.set_status("completed")
        turn.settle()
        return batch

    batch = drive(parallel.start_span("agent_run", {}, parallel_body))
    assert len(batch.messages) == 2 and not any(item.is_error for item in batch.messages)
    records = parallel.records()
    assert [row.name.value for row in records] == ["agent_run", "turn", "tool", "tool"]
    assert [row.parent_span_id for row in records] == [None, 1, 2, 2]
    tool_rows = [row for row in records if row.name.value == "tool"]
    assert len({row.span_id for row in tool_rows}) == 2
    for row in tool_rows:
        assert row.status.value == "completed"
        assert row.attributes["tool_name"] == "read"
        assert row.attributes["tool_call_id"] in {"c1", "c2"}
        assert row.attributes["execution_ms"] >= 0

    # 准备阶段拒绝：block 决策不进入执行，execution_ms 保持未知。
    blocked = agent_trace()

    async def blocked_body(root: TraceSpan):
        turn = root.open("turn", {})

        async def reject(context, signal):
            return BeforeToolCallResult(block=True, reason="策略拒绝")

        batch = await run_tool_batch(
            [call("b1", "read", {"path": path})],
            tools=tools,
            declared=declared,
            before_tool_call=reject,
            telemetry_span=turn,
        )
        turn.set_status("completed")
        turn.settle()
        return batch

    drive(blocked.start_span("agent_run", {}, blocked_body))
    blocked_tool = next(row for row in blocked.records() if row.name.value == "tool")
    assert blocked_tool.status.value == "failed"
    assert blocked_tool.attributes["tool_name"] == "read"
    assert "execution_ms" not in blocked_tool.attributes

    # 返回错误与后处理耗时：真实 read 失败 + 真实 after_tool_call 覆盖。
    erroring = agent_trace()

    async def error_body(root: TraceSpan):
        turn = root.open("turn", {})

        async def after(context, signal):
            from app.agent.tool import AfterToolCallResult

            return AfterToolCallResult(
                content=[TextContent(type="text", text="后处理结果")]
            )

        batch = await run_tool_batch(
            [call("e1", "read", {"path": f"sessions/{session_id}/workspace/missing.txt"})],
            tools=tools,
            declared=declared,
            after_tool_call=after,
            telemetry_span=turn,
        )
        turn.set_status("completed")
        turn.settle()
        return batch

    drive(erroring.start_span("agent_run", {}, error_body))
    error_tool = next(row for row in erroring.records() if row.name.value == "tool")
    assert error_tool.status.value == "failed"
    assert error_tool.attributes["execution_ms"] >= 0
    assert error_tool.attributes["postprocess_ms"] >= 0

    # 未核实名称：工具不在注册表，tool_name 保持 null 且请求仍然收尾。
    unknown = agent_trace()

    async def unknown_body(root: TraceSpan):
        turn = root.open("turn", {})
        batch = await run_tool_batch(
            [call("u1", "ghost_tool", {})],
            tools=tools,
            declared=declared,
            telemetry_span=turn,
        )
        turn.set_status("completed")
        turn.settle()
        return batch

    drive(unknown.start_span("agent_run", {}, unknown_body))
    unknown_tool = next(row for row in unknown.records() if row.name.value == "tool")
    assert unknown_tool.status.value == "failed"
    assert "tool_name" not in unknown_tool.attributes
    assert unknown_tool.attributes["tool_call_id"] == "u1"
    return {
        "parallel": [
            [row.span_id, row.parent_span_id, row.attributes.get("execution_ms")]
            for row in tool_rows
        ],
        "blocked": blocked_tool.attributes,
        "error": error_tool.attributes,
        "unknown": unknown_tool.attributes,
    }


def compaction_spans() -> dict:
    # 真实模型摘要：Compaction Span 下历史摘要与任务前缀摘要分别建立 model 子 Span。
    model = install_test_model_config()
    spec = resolve_model_spec(model)
    spec = spec.model_copy(update={"base_url": model.base_url})
    results: dict[str, object] = {}
    for reason in ("threshold", "overflow"):
        trace = agent_trace()
        events_seen: list[str] = []
        observer = CacheObserver(
            session_id=uuid4_canonical(),
            run_id=uuid4_canonical(),
            directory=ROOT / "cache",
        )

        async def estimate_request() -> int:
            return 0

        async def compact(compaction_id, signal, span):
            preparation = CompactionPreparation(
                first_kept_entry_id="kept-1",
                messages_to_summarize=[UserMessage(role="user", content="历史" * 60, timestamp=1)],
                turn_prefix_messages=[UserMessage(role="user", content="前缀" * 60, timestamp=2)],
                is_split_turn=True,
                tokens_before=0,
                previous_summary=None,
                attachments=[],
            )
            await generate_compaction_summary(
                preparation,
                spec,
                api_key=model.api_key,
                budget=RequestBudget(),
                observation=observer,
                compaction_id=compaction_id,
                parent=span,
            )
            return LoopCompactionResult(
                entry_id=compaction_id,
                parent_id="parent-entry",
                messages=[],
            )

        async def emit(event) -> None:
            events_seen.append(event["type"])

        hooks = CompactionHooks(
            estimate_request=estimate_request,
            compact=compact,
            context_window=spec.context_window,
            reserve_tokens=16384,
        )

        async def body(root: TraceSpan) -> None:
            await run_compaction({"messages": [], "tools": {}}, hooks, None, emit, reason, root)

        drive(trace.start_span("agent_run", {}, body))
        records = trace.records()
        assert [row.name.value for row in records] == [
            "agent_run",
            "compaction",
            "model",
            "model",
        ], records
        assert [row.parent_span_id for row in records] == [None, 1, 2, 2]
        compaction = records[1]
        assert compaction.attributes["reason"] == reason
        assert compaction.status.value == "completed"
        kinds = sorted(
            row.attributes["call_kind"] for row in records if row.name.value == "model"
        )
        assert kinds == ["history_summary", "turn_prefix_summary"], kinds
        for row in records[2:]:
            assert row.attributes["input_tokens"] is not None
            assert row.status.value == "completed"
        results[reason] = {
            "events": events_seen,
            "compaction_reason": compaction.attributes["reason"],
            "model_kinds": kinds,
            "model_usage": [row.attributes.get("input_tokens") for row in records[2:]],
        }
    return results


def fault_isolation_and_identity() -> dict:
    # 独立无记录调用：无 Trace 身份，Span 操作全部为空且 callback 仍恰好执行一次。
    no_record = TraceSpan()
    assert no_record.trace_id == ""
    calls: list[int] = []
    assert no_record.start_span("turn", {}, lambda span: calls.append(1)) is None
    assert calls == [1]
    opened = no_record.open("tool", {"tool_call_id": "x"})
    opened.set_attributes({"tool_name": "echo"})
    opened.set_status("completed")
    opened.settle()
    assert no_record.start_span("turn", {}, lambda span: span.start_span("model", {}, lambda s: "v")) == "v"

    # 属性白名单：非法开始属性导致该 Span 不记录，业务 callback 仍执行且返回值不变。
    whitelist = agent_trace()
    values: list[str] = []

    def body(root: TraceSpan) -> str:
        values.append(
            root.start_span("model", {"provider": None, "model": "m", "call_kind": "normal"}, lambda s: "no-provider")
        )
        values.append(root.start_span("tool", {"tool_call_id": "t1", "unexpected": 1}, lambda s: "bad-attr"))
        return "root-value"

    assert whitelist.start_span("agent_run", {}, body) == "root-value"
    assert values == ["no-provider", "bad-attr"]
    assert [row.name.value for row in whitelist.records()] == ["agent_run"]

    # 原始业务异常传播：模型 Span 记 failed，异常对象身份不变，详情不进记录。
    error = RuntimeError("业务原始异常正文")
    failing = agent_trace()

    def boom(root: TraceSpan) -> None:
        def model_body(span: TraceSpan) -> None:
            raise error

        root.start_span("model", {"provider": "p", "model": "m", "call_kind": "normal"}, model_body)

    try:
        failing.start_span("agent_run", {}, boom)
    except RuntimeError as caught:
        assert caught is error
    else:
        raise AssertionError("业务异常未传播")
    dumped = json.dumps([row.model_dump() for row in failing.records()])
    assert "业务原始异常正文" not in dumped

    # 凭据命中：模型 Span 命中已知凭据时连同子树被拒绝，其余链路保持完整。
    secret = "sk-" + "y" * 40
    guarded = agent_trace(contains=CredentialFilter((secret,)).contains)

    def guarded_body(root: TraceSpan) -> None:
        root.start_span("turn", {}, lambda turn: "clean")

        def leaky(turn: TraceSpan) -> None:
            turn.start_span("model", {"provider": secret, "model": "m", "call_kind": "normal"}, lambda s: "leak")

        root.start_span("turn", {}, leaky)

    guarded.start_span("agent_run", {}, guarded_body)
    kept = [(row.span_id, row.name.value) for row in guarded.records()]
    # 命中凭据的 model 记录被拒绝并剔除其子树；其余链路保持完整。
    assert kept == [(1, "agent_run"), (2, "turn"), (3, "turn")], kept
    assert all(name != "model" for _, name in kept)
    assert secret not in json.dumps([row.model_dump() for row in guarded.records()])

    # 结算故障隔离：非法受理计时导致观测记录被拒，业务结局保持原样。
    broken = agent_trace(accepted_perf="非法计时边界")

    def still_ok(root: TraceSpan) -> str:
        return root.start_span("turn", {}, lambda turn: "turn-value")

    assert broken.start_span("agent_run", {}, still_ok) == "turn-value"
    assert broken.records() == []

    return {
        "no_record_trace_id": no_record.trace_id,
        "whitelist_values": values,
        "whitelist_kept": [row.name.value for row in whitelist.records()],
        "exception_propagated": True,
        "credential_kept": kept,
        "settlement_fault_records": len(broken.records()),
    }


def check() -> None:
    install_test_model_config()
    patch_default_database("trace-spans")
    global TRACE_PATH
    TRACE_PATH = patch_trace_database("trace-spans")
    config = load_model_config()

    RESULTS["faults"] = fault_isolation_and_identity()
    RESULTS["compaction"] = compaction_spans()
    RESULTS["tools"] = tool_spans()
    RESULTS["tool_task_cancellation"] = tool_task_cancellation()

    server = Server(app)
    with server, client(server.base_url) as http:
        RESULTS["http_normal"] = http_normal_chain(http)
        RESULTS["http_model_failure"] = http_model_failure(http, config)
        RESULTS["http_output_limit"] = http_output_limit(http)
        RESULTS["http_cancel"] = http_cancel(http)

    EVIDENCE.write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(
        "Trace 子操作自检：根/Turn/模型/工具/压缩父子关系、整链身份与唯一编号、"
        "终态与释放、终态通知前完成写入、模型首正文与真实用量与未知字段、"
        "length 完成与输出上限失败、失败与取消、并行工具与准备拒绝与错误与后处理、"
        "threshold 与 overflow 与两类摘要、故障隔离与原始异常与凭据白名单、"
        "独立无记录调用通过"
    )


if __name__ == "__main__":
    check()
