# 运行：在 backend 执行 python -m test.check_trace_audit_fixes
# 或：python -m pytest test/check_trace_audit_fixes.py -q
#
# 本轮四项 Trace 修复的验证代码：全部使用真实实现、真实文件系统与真实 SQLite/内存记录器。
# 模型端点使用本文件内的本地真实 ThreadingHTTPServer（OpenAI 兼容 SSE），不替换客户端。
#
# A. 缓存观测故障隔离（真实文件系统）
# B. 取消状态（真实 RunTrace / 真实 _observed 收尾）
# C. 工具准备阶段取消（run_tool_batch + 真实 turn Span）
# D. 预算拒绝零模型 Span（真实 RequestBudget + 真实本地 HTTP 端点）
import asyncio
import concurrent.futures as futures
import json
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

import pytest
from pydantic import BaseModel

from app.agent.agent_loop import stream_assistant_response
from app.agent.budget import ModelRequestLimitExceeded, RequestBudget
from app.agent.cache_observation import (
    FIRST_CALL,
    NORMAL_CALL,
    CacheObserver,
    ModelCallObservation,
    read_observations,
)
from app.agent.compaction import CompactionPreparation, generate_compaction_summary
from app.agent.config import AgentLoopConfig
from app.agent.telemetry import RunTrace, TraceSpan, open_trace_store, uuid4_canonical
from app.agent.tool import (
    AgentTool,
    AgentToolResult,
    BeforeToolCallResult,
    run_tool_batch,
)
from app.ai.api import openai_completions
from app.ai.messages import (
    AssistantMessage,
    TextContent,
    ToolCall,
    Usage,
    UserMessage,
)
from app.ai.stream import AssistantResponse, _observed, stream
from app.ai.types import ModelSpec
from app.interfaces.http import CredentialFilter
from app.model_config import ModelConfig

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND / "temp" / "trace-audit-fixes"
EVIDENCE = ROOT / "evidence.json"
RESULTS: dict[str, object] = {}


# ---------------------------------------------------------------------------
# 共用构造：真实协议请求、真实用量与真实身份。
# ---------------------------------------------------------------------------
def openai_spec(base_url: str = "https://example.invalid/v1") -> ModelSpec:
    return ModelSpec(
        api="openai-completions",
        provider="test",
        id="test-model",
        base_url=base_url,
        context_window=128000,
        max_tokens=16384,
    )


def usage(i: int = 50, o: int = 8, cr: int = 40, cw: int = 10) -> Usage:
    return Usage(
        input=i, output=o, cache_read=cr, cache_write=cw, total_tokens=i + o + cr + cw
    )


def user(text: str, tick: int = 1) -> UserMessage:
    return UserMessage(role="user", content=text, timestamp=tick)


def assistant_message(text: str) -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=[TextContent(type="text", text=text)],
        api="openai-completions",
        provider="test",
        model="test-model",
        usage=usage(),
        stop_reason="stop",
        timestamp=2,
    )


def request_for(spec: ModelSpec, messages=None) -> dict:
    # 真实适配器生成的最终投影；观测只读取该投影，不改动请求。
    return openai_completions.to_openai_request(
        messages or [user("问")], spec, {"api_key": "k"}
    )


def agent_trace() -> RunTrace:
    return RunTrace(
        session_id=uuid4_canonical(),
        operation_id=uuid4_canonical(),
        run_id=uuid4_canonical(),
    )


def session_records(session: str, directory: Path) -> list[ModelCallObservation]:
    return [
        record
        for record in read_observations(directory)
        if record.session_id == session
    ]


def message_text(message) -> str:
    return "".join(block.text for block in message.content)


def _blocking_dir(name: str) -> Path:
    # 制造“父路径是普通文件”的写入失败位置：<root>/<name>/file.txt/sub。
    root = ROOT / name
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    blocker = root / "file.txt"
    blocker.write_text("blocker", encoding="utf-8")
    return blocker / "sub"


# ===========================================================================
# A. 缓存观测故障隔离
# ===========================================================================
def test_read_parse_failure_isolated_and_chain_continues() -> None:
    # 读取/解析失败：构造 CacheObserver 不抛出，比较链按空继续，首个调用 category == "first"。
    directory = ROOT / "a-read"
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=True)
    session = uuid4_canonical()
    path = directory / f"{session}.jsonl"
    path.write_text(
        '{"observation_id": "broken" 这不是合法 JSON\n第二行同样损坏\n',
        encoding="utf-8",
    )

    observer = CacheObserver(
        session_id=session, run_id=uuid4_canonical(), directory=directory
    )
    assert observer.chains == {}

    spec = openai_spec()
    handle = observer.start_call(NORMAL_CALL, message_id="m1")
    handle.observe(request_for(spec), spec)
    handle.finish(usage(), "stop")

    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    record = ModelCallObservation.model_validate(json.loads(lines[-1]))
    assert record.category == FIRST_CALL
    RESULTS["A_read_failure"] = {
        "chains_after_load": 0,
        "first_call_category": record.category,
    }


def test_write_failure_isolated() -> None:
    # 写入失败：directory 指向“父路径是普通文件”的位置，observe + finish 均不抛出。
    directory = _blocking_dir("a-write")
    session = uuid4_canonical()
    observer = CacheObserver(
        session_id=session, run_id=uuid4_canonical(), directory=directory
    )
    spec = openai_spec()
    handle = observer.start_call(NORMAL_CALL)
    handle.observe(request_for(spec), spec)
    handle.finish(usage(), "stop")  # 不得抛出
    assert handle.finished
    assert not directory.exists()
    RESULTS["A_write_failure"] = {"finished": handle.finished, "directory_exists": False}


def test_persist_failure_still_reports_model_span() -> None:
    # 持久化失败后仍补充模型 Span：status == completed 且真实用量写入。
    directory = _blocking_dir("a-span")
    trace = agent_trace()
    spec = openai_spec()
    observed_usage = usage(100, 8, 40, 10)

    def body(root_span: TraceSpan) -> None:
        span = root_span.open(
            "model",
            {"provider": spec.provider, "model": spec.id, "call_kind": NORMAL_CALL},
        )
        observer = CacheObserver(
            session_id=uuid4_canonical(), run_id=trace.run_id, directory=directory
        )
        handle = observer.start_call(NORMAL_CALL, span=span)
        handle.observe(request_for(spec), spec)
        handle.finish(observed_usage, "stop")  # persist 失败，仍须向 Span 补充结局
        span.settle()

    trace.start_span("agent_run", {}, body)
    model = next(row for row in trace.records() if row.name.value == "model")
    assert model.status.value == "completed"
    assert model.attributes["stop_reason"] == "stop"
    assert model.attributes["input_tokens"] == 100
    assert model.attributes["output_tokens"] == 8
    assert model.attributes["cache_read_tokens"] == 40
    assert model.attributes["cache_write_tokens"] == 10
    assert not directory.exists()

    # 真实 SQLite 往返：同一批记录写入观测库后读回，结局与用量保持一致。
    async def round_trip():
        store = await open_trace_store(ROOT / f"{uuid4().hex}.db")
        try:
            await store.save(trace.records())
            return await store.read_trace(trace.trace_id)
        finally:
            await store.close()

    persisted = asyncio.run(round_trip())
    persisted_model = next(row for row in persisted if row.name.value == "model")
    assert persisted_model.status.value == "completed"
    assert persisted_model.attributes["input_tokens"] == 100
    RESULTS["A_persist_failure_span"] = {
        "status": model.status.value,
        "stop_reason": model.attributes["stop_reason"],
        "input_tokens": model.attributes["input_tokens"],
        "sqlite_round_trip_status": persisted_model.status.value,
    }


def test_business_and_observation_failure_together() -> None:
    # 业务与观测同时失败：callback 内先做一次写失败的观测，再抛原始业务异常对象。
    directory = _blocking_dir("a-both")
    trace = agent_trace()
    spec = openai_spec()
    original = RuntimeError("业务原始异常对象")
    caught: list[BaseException] = []

    def body(root_span: TraceSpan) -> None:
        def model_body(model_span: TraceSpan) -> None:
            observer = CacheObserver(
                session_id=uuid4_canonical(), run_id=trace.run_id, directory=directory
            )
            handle = observer.start_call(NORMAL_CALL)
            handle.observe(request_for(spec), spec)
            handle.finish(usage(), "stop")  # 观测写入失败被隔离
            raise original

        root_span.start_span(
            "model",
            {"provider": spec.provider, "model": spec.id, "call_kind": NORMAL_CALL},
            model_body,
        )

    try:
        trace.start_span("agent_run", {}, body)
    except BaseException as error:  # noqa: BLE001 - 核对异常对象身份
        caught.append(error)
    assert len(caught) == 1 and caught[0] is original
    model = next(row for row in trace.records() if row.name.value == "model")
    assert model.status.value == "failed"
    assert not directory.exists()
    RESULTS["A_both_failure"] = {
        "same_exception_identity": caught[0] is original,
        "model_status": model.status.value,
        "observation_isolated": True,
    }


def _raise_contains(_payload) -> bool:
    raise RuntimeError("凭据检查失败")


def test_credential_boundary_blocks_file() -> None:
    # 凭据边界：contains 命中时该会话文件不产生；contains 抛错同样按拒绝处理。
    directory = ROOT / "a-cred"
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=True)
    session = "secret-model-session"
    spec = ModelSpec(
        api="openai-completions",
        provider="test",
        id="secret-model",
        base_url="https://example.invalid/v1",
        context_window=128000,
        max_tokens=16384,
    )
    guard = CredentialFilter(("secret-model",))
    observer = CacheObserver(
        session_id=session,
        run_id=uuid4_canonical(),
        directory=directory,
        contains=guard.contains,
    )
    handle = observer.start_call(NORMAL_CALL)
    handle.observe(request_for(spec), spec)
    handle.finish(usage(), "stop")
    assert not (directory / f"{session}.jsonl").exists()

    raising_session = "raising-session"
    raising = CacheObserver(
        session_id=raising_session,
        run_id=uuid4_canonical(),
        directory=directory,
        contains=_raise_contains,
    )
    raising_handle = raising.start_call(NORMAL_CALL)
    raising_handle.observe(request_for(spec), spec)
    raising_handle.finish(usage(), "stop")
    assert not (directory / f"{raising_session}.jsonl").exists()
    RESULTS["A_credential_boundary"] = {
        "hit_blocked": True,
        "raising_blocked": True,
    }


# ===========================================================================
# B. 取消状态
# ===========================================================================
def test_automatic_status_recognizes_cancellations() -> None:
    def outcome(raiser):
        trace = agent_trace()
        caught: list[BaseException] = []

        def callback(_span):
            if raiser is not None:
                raise raiser()
            return "ok"

        try:
            result = trace.start_span("agent_run", {}, callback)
        except BaseException as error:  # noqa: BLE001
            caught.append(error)
            result = None
        rows = trace.records()
        assert len(rows) == 1, rows
        return rows[0].status.value, result, caught

    assert outcome(lambda: asyncio.CancelledError())[0] == "cancelled"
    assert outcome(lambda: futures.CancelledError())[0] == "cancelled"
    assert outcome(lambda: RuntimeError("boom"))[0] == "failed"
    status, result, caught = outcome(None)
    assert status == "completed" and result == "ok" and caught == []
    RESULTS["B_automatic_status"] = {
        "asyncio_cancelled": "cancelled",
        "futures_cancelled": "cancelled",
        "runtime_error": "failed",
        "normal": "completed",
    }


def _stream_source(state, *, block: bool, error: BaseException | None):
    async def generator():
        try:
            yield {"type": "start", "message": assistant_message("开始")}
            if block:
                await state["gate"].wait()
            if error is not None:
                raise error
        finally:
            state["cleanup"].append("source_closed")

    return generator()


def _run_stream_scenario(kind: str) -> tuple[str, str, list[str]]:
    # 真实异步生成器 source：产出一个 start 事件后阻塞在 asyncio.Event（或抛出指定异常）。
    state: dict = {"cleanup": [], "gate": asyncio.Event()}
    trace = agent_trace()
    spec = openai_spec()
    session = uuid4_canonical()
    directory = ROOT / "b-stream"
    directory.mkdir(parents=True, exist_ok=True)
    box: dict = {}

    async def body(root_span: TraceSpan) -> None:
        span = root_span.open(
            "model",
            {"provider": spec.provider, "model": spec.id, "call_kind": NORMAL_CALL},
        )
        observer = CacheObserver(
            session_id=session, run_id=trace.run_id, directory=directory
        )
        handle = observer.start_call(NORMAL_CALL, span=span)
        handle.observe(request_for(spec), spec)
        box["handle"] = handle
        error = {
            "future_cancelled": futures.CancelledError(),
            "runtime_error": RuntimeError("boom"),
        }.get(kind)
        source = _stream_source(state, block=(kind == "cancel_task"), error=error)
        response = AssistantResponse(_observed(source, handle, None))

        if kind == "cancel_task":
            async def consume_all() -> None:
                async for _event in response:
                    pass

            task = asyncio.create_task(consume_all())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        elif kind == "early_aclose":
            iterator = response.__aiter__()
            await iterator.__anext__()
            await response.aclose()
        else:
            try:
                async for _event in response:
                    pass
            except BaseException:  # noqa: BLE001 - 消费端观测到真实异常
                pass
        span.settle()

    asyncio.run(trace.start_span("agent_run", {}, body))
    model = next(row for row in trace.records() if row.name.value == "model")
    records = session_records(session, directory)
    assert len(records) == 1 and box["handle"].finished
    return records[0].status, model.status.value, state["cleanup"]


def test_stream_settle_cancelled_and_failed() -> None:
    cancel_status, cancel_span, cancel_cleanup = _run_stream_scenario("cancel_task")
    assert cancel_status == "aborted" and cancel_span == "cancelled"
    assert cancel_cleanup == ["source_closed"]

    future_status, future_span, _ = _run_stream_scenario("future_cancelled")
    assert future_status == "aborted" and future_span == "cancelled"

    error_status, error_span, error_cleanup = _run_stream_scenario("runtime_error")
    assert error_status == "failed" and error_span == "failed"
    assert error_cleanup == ["source_closed"]

    close_status, close_span, close_cleanup = _run_stream_scenario("early_aclose")
    assert close_status == "failed" and close_span == "failed"
    assert close_cleanup == ["source_closed"]

    RESULTS["B_stream_settle"] = {
        "cancel_task": {"record": cancel_status, "span": cancel_span},
        "future_cancelled": {"record": future_status, "span": future_span},
        "runtime_error": {"record": error_status, "span": error_span},
        "early_aclose": {"record": close_status, "span": close_span},
    }


# ===========================================================================
# C. 工具准备阶段取消
# ===========================================================================
class EchoArgs(BaseModel):
    text: str


def _echo_execute(_tool_call_id, arguments, _signal, _on_update) -> AgentToolResult:
    return AgentToolResult([TextContent(type="text", text=f"echo:{arguments.text}")])


def _boom_execute(_tool_call_id, _arguments, _signal, _on_update) -> AgentToolResult:
    raise RuntimeError("工具执行失败")


ECHO = AgentTool(name="echo", description="回显", arguments=EchoArgs, execute=_echo_execute)
BOOM = AgentTool(name="boom", description="失败工具", arguments=EchoArgs, execute=_boom_execute)


def _tool_call(identifier: str, arguments: dict, name: str = "echo") -> ToolCall:
    return ToolCall(type="toolCall", id=identifier, name=name, arguments=arguments)


def run_batch(tool, tool_calls, *, before=None, signal=None, mode="parallel"):
    trace = agent_trace()

    async def body(root_span: TraceSpan):
        turn = root_span.open("turn", {})
        batch = await run_tool_batch(
            tool_calls,
            tools={tool.name: tool},
            declared={tool.name: tool.definition()},
            before_tool_call=before,
            signal=signal,
            execution_mode=mode,
            telemetry_span=turn,
        )
        turn.settle()
        return batch

    batch = asyncio.run(trace.start_span("agent_run", {}, body))
    tool_rows = [row for row in trace.records() if row.name.value == "tool"]
    return batch, tool_rows


def test_prepare_stage_cancellation_settles_tool_span_cancelled() -> None:
    signal = Event()

    async def before(_context, sig):
        sig.set()  # before_tool_call 期间置位协作取消
        return None

    batch, tool_rows = run_batch(
        ECHO,
        [_tool_call("c1", {"text": "x"})],
        before=before,
        signal=signal,
    )
    assert len(tool_rows) == 1
    assert tool_rows[0].status.value == "cancelled"
    assert len(batch.messages) == 1
    message = batch.messages[0]
    assert message_text(message) == "Operation aborted"
    assert message.is_error is True
    RESULTS["C_prepare_cancel"] = {
        "span_status": tool_rows[0].status.value,
        "message_text": message_text(message),
        "is_error": message.is_error,
    }


def test_tool_prepare_and_execution_outcomes() -> None:
    # 普通参数校验失败 → failed
    validation_batch, validation_rows = run_batch(
        ECHO, [_tool_call("v1", {})]
    )
    assert validation_rows[0].status.value == "failed"
    assert validation_batch.messages[0].is_error is True

    # 权限 block → failed
    async def reject(_context, _signal):
        return BeforeToolCallResult(block=True, reason="策略拒绝")

    blocked_batch, blocked_rows = run_batch(
        ECHO, [_tool_call("b1", {"text": "x"})], before=reject, signal=Event()
    )
    assert blocked_rows[0].status.value == "failed"
    assert blocked_batch.messages[0].is_error is True

    # 工具执行错误 → failed
    error_batch, error_rows = run_batch(
        BOOM, [_tool_call("e1", {"text": "x"}, name="boom")]
    )
    assert error_rows[0].status.value == "failed"
    assert error_batch.messages[0].is_error is True

    # 正常执行 → completed
    ok_batch, ok_rows = run_batch(ECHO, [_tool_call("o1", {"text": "x"})])
    assert ok_rows[0].status.value == "completed"
    assert ok_batch.messages[0].is_error is False
    RESULTS["C_tool_outcomes"] = {
        "validation_failure": validation_rows[0].status.value,
        "permission_block": blocked_rows[0].status.value,
        "execution_error": error_rows[0].status.value,
        "normal": ok_rows[0].status.value,
    }


# ===========================================================================
# D. 预算拒绝零模型 Span
# ===========================================================================
def _unused_stream_fn(*_args, **_kwargs) -> AssistantResponse:
    raise AssertionError("预算拒绝时不应发起模型请求")


def test_budget_rejection_no_model_span_normal_request() -> None:
    trace = agent_trace()
    spec = openai_spec()
    budget = RequestBudget(limit=1)
    budget.reserve()  # 先占满
    model_config = ModelConfig(
        api="openai-completions",
        base_url=spec.base_url,
        model=spec.id,
        provider=spec.provider,
        api_key="k",
    )
    config = AgentLoopConfig(
        model=model_config, max_turns=4, model_spec=spec, request_budget=budget
    )

    async def body(root_span: TraceSpan) -> None:
        turn = root_span.open("turn", {})
        context = {"messages": [user("go")], "tools": {}}

        async def emit(_event) -> None:
            return None

        with pytest.raises(ModelRequestLimitExceeded):
            await stream_assistant_response(
                context, config, None, "node-1", emit, _unused_stream_fn, turn
            )
        turn.settle()

    asyncio.run(trace.start_span("agent_run", {}, body))
    rows = trace.records()
    assert [row.name.value for row in rows] == ["agent_run", "turn"]
    assert all(row.name.value != "model" for row in rows)
    assert budget.used == 1
    RESULTS["D_budget_normal"] = {
        "nodes": [row.name.value for row in rows],
        "used": budget.used,
    }


def test_budget_rejection_no_model_span_summary() -> None:
    trace = agent_trace()
    spec = openai_spec()
    budget = RequestBudget(limit=1)
    budget.reserve()
    preparation = CompactionPreparation(
        first_kept_entry_id="kept-1",
        messages_to_summarize=[user("历史内容" * 20)],
        turn_prefix_messages=[],
        is_split_turn=False,
        tokens_before=0,
        previous_summary=None,
        attachments=[],
    )

    async def body(root_span: TraceSpan) -> None:
        compaction = root_span.open("compaction", {"reason": "threshold"})
        with pytest.raises(ModelRequestLimitExceeded):
            await generate_compaction_summary(
                preparation, spec, api_key="k", budget=budget, parent=compaction
            )
        compaction.settle()

    asyncio.run(trace.start_span("agent_run", {}, body))
    rows = trace.records()
    assert [row.name.value for row in rows] == ["agent_run", "compaction"]
    assert all(row.name.value != "model" for row in rows)
    assert budget.used == 1
    RESULTS["D_budget_summary"] = {
        "nodes": [row.name.value for row in rows],
        "used": budget.used,
    }


def _openai_success_payload() -> bytes:
    chunks = [
        {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "完成"},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
        {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test-model",
            "choices": [],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 8,
                "total_tokens": 108,
                "prompt_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 10},
            },
        },
    ]
    payload = bytearray()
    for chunk in chunks:
        payload += f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
    payload += b"data: [DONE]\n\n"
    return bytes(payload)


class LocalOpenAIServer:
    # 本地真实 OpenAI 兼容流式端点：统计实际 HTTP 请求数，返回带真实 usage 的 SSE。
    def __init__(self) -> None:
        self.requests = 0
        self._payload = _openai_success_payload()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                outer.requests += 1
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(outer._payload)))
                self.end_headers()
                self.wfile.write(outer._payload)
                self.wfile.flush()

            def log_message(self, *_args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def __enter__(self) -> "LocalOpenAIServer":
        self._thread.start()
        return self

    def __exit__(self, *_error) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


def test_budget_counts_each_request_once() -> None:
    with LocalOpenAIServer() as server:
        spec = openai_spec(server.base_url)
        model_config = ModelConfig(
            api="openai-completions",
            base_url=server.base_url,
            model=spec.id,
            provider=spec.provider,
            api_key="k",
        )
        budget = RequestBudget()
        trace = agent_trace()
        # 真实缓存观测：确认每次请求只观测一次并补充真实用量到模型 Span。
        records_dir = ROOT / "d-count" / uuid4_canonical()
        records_dir.mkdir(parents=True, exist_ok=True)
        observer = CacheObserver(
            session_id=uuid4_canonical(), run_id=trace.run_id, directory=records_dir
        )
        config = AgentLoopConfig(
            model=model_config,
            max_turns=4,
            model_spec=spec,
            request_budget=budget,
            observation=observer,
        )

        async def body(root_span: TraceSpan) -> None:
            for index in range(2):
                turn = root_span.open("turn", {})
                context = {"messages": [user("问")], "tools": {}}

                async def emit(_event) -> None:
                    return None

                await stream_assistant_response(
                    context, config, None, f"n{index}", emit, stream, turn
                )
                turn.settle()

        asyncio.run(trace.start_span("agent_run", {}, body))
        model_rows = [row for row in trace.records() if row.name.value == "model"]
        # 每次实际请求只占一次：used 与实际 HTTP 请求数相等且均为 2。
        assert budget.used == server.requests == 2
        assert len(model_rows) == 2
        assert all(row.status.value == "completed" for row in model_rows)
        assert all(row.attributes["input_tokens"] == 50 for row in model_rows)
        assert all(row.attributes["cache_read_tokens"] == 40 for row in model_rows)
        persisted = read_observations(records_dir)
        assert len(persisted) == 2
        assert [record.call_index for record in persisted] == [1, 2]
        RESULTS["D_budget_counting"] = {
            "used": budget.used,
            "http_requests": server.requests,
            "model_spans": len(model_rows),
            "observations": len(persisted),
            "input_tokens": model_rows[0].attributes["input_tokens"],
        }


def check() -> None:
    if ROOT.exists():
        shutil.rmtree(ROOT)
    ROOT.mkdir(parents=True, exist_ok=True)
    test_read_parse_failure_isolated_and_chain_continues()
    test_write_failure_isolated()
    test_persist_failure_still_reports_model_span()
    test_business_and_observation_failure_together()
    test_credential_boundary_blocks_file()
    test_automatic_status_recognizes_cancellations()
    test_stream_settle_cancelled_and_failed()
    test_prepare_stage_cancellation_settles_tool_span_cancelled()
    test_tool_prepare_and_execution_outcomes()
    test_budget_rejection_no_model_span_normal_request()
    test_budget_rejection_no_model_span_summary()
    test_budget_counts_each_request_once()
    EVIDENCE.write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "Trace 审计修复：缓存观测读取解析与写入故障隔离、持久化失败仍补充模型 Span、"
        "业务与观测双重失败异常身份与隔离、凭据命中与检查抛错均拒绝落盘、"
        "两类取消自动状态与普通异常与正常、流收尾取消 aborted 与模型 Span cancelled、"
        "提前关闭与普通异常 failed、工具准备阶段取消 Span cancelled 且文案不变、"
        "参数校验/权限/执行错误 failed 与正常 completed、"
        "普通请求与摘要请求预算拒绝零模型 Span、真实 HTTP 端点每请求只占一次通过"
    )


if __name__ == "__main__":
    check()
