"""任务三定向验证：HTTP 准入、有界 SSE、唯一收尾与故障封闭。

真实链路：真实 FastAPI/uvicorn、HTTPX、本地 loopback 模型 SSE、真实 SQLite。
直连 coordinator 的用例只替换工作 future 的执行体（阻塞/延迟），用于验证
收尾所有权、共享预算与故障封闭；不伪造业务成功或模型结果。
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from uuid import UUID, uuid4

import httpx
import pytest
from openai._streaming import SSEDecoder
from pydantic import BaseModel, TypeAdapter

import app.agent.governance as governance
import app.agent.tool as tool_module
import app.ai.api.openai_completions as openai_completions
import app.ai.stream as ai_stream
from app.agent.events import AgentEvent
from app.agent.tool import AgentTool, AgentToolResult
from app.ai.messages import TextContent
from app.ai.types import ModelTimeouts
from app.application.session import coordination as coordination_module
from app.application.session import execution as execution_module
from app.application.session.coordination import terminal_decision
from app.application.session.runtime import RunState as ApplicationRunState
from app.application.session.service import SessionService
from app.interfaces import http as http_module
from app.interfaces.http_api import stream as stream_module
from app.interfaces.http_api.routes import agent as agent_routes
from app.interfaces.http_api.stream import (
    BoundedEventQueue,
    EncodedEvent,
    HttpRunOutput,
    RunState,
    RunStream,
    encode,
    stream,
)
from test.regression_support import Server, client, install_test_model_config

REPO_ROOT = Path(__file__).resolve().parents[2]
TMP_ROOT = REPO_ROOT / "tmp" / "task3-tests"
TMP_ROOT.mkdir(parents=True, exist_ok=True)

OPENAI_SUCCESS_CHUNKS = [
    {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test",
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
        "model": "test",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    },
    {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test",
        "choices": [],
        "usage": {
            "prompt_tokens": 15,
            "completion_tokens": 6,
            "total_tokens": 21,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    },
]


def _chunk(text: str) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test",
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": text},
                "finish_reason": None,
            }
        ],
    }


def _tool_call_chunks(name: str, arguments: dict) -> list[dict]:
    return [
        {
            "id": "chatcmpl-tool",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": name, "arguments": ""},
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-tool",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {
                                    "arguments": json.dumps(arguments, ensure_ascii=False)
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-tool",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test",
            "choices": [
                {"index": 0, "delta": {}, "finish_reason": "tool_calls"}
            ],
        },
        {
            "id": "chatcmpl-tool",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test",
            "choices": [],
            "usage": {
                "prompt_tokens": 15,
                "completion_tokens": 6,
                "total_tokens": 21,
                "prompt_tokens_details": {"cached_tokens": 0},
            },
        },
    ]


class ToolArgs(BaseModel):
    text: str = "x"


class ModelServer:
    """本地真实 OpenAI 兼容 SSE 端点；场景只改变响应时序与内容。"""

    def __init__(self, scenario: str = "success") -> None:
        self.scenario = scenario
        self._lock = Lock()
        self.requests = 0
        self.started = Event()
        self.stop = Event()
        self.tool_name = "slow"
        self.tool_arguments: dict = {"text": "x"}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                with outer._lock:
                    outer.requests += 1
                outer.started.set()
                try:
                    outer.respond(self)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    self.close_connection = True

            def log_message(self, *_args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def __enter__(self) -> "ModelServer":
        self._thread.start()
        return self

    def __exit__(self, *_error) -> None:
        self.stop.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def _write_openai(self, handler, chunks) -> None:
        for chunk in chunks:
            handler.wfile.write(
                f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
            )
            handler.wfile.flush()
        handler.wfile.write(b"data: [DONE]\n\n")
        handler.wfile.flush()

    def respond(self, handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()
        if self.scenario == "tool_once":
            if self.requests == 1:
                self._write_openai(
                    handler,
                    _tool_call_chunks(self.tool_name, self.tool_arguments),
                )
            else:
                self._write_openai(handler, OPENAI_SUCCESS_CHUNKS)
            return
        if self.scenario == "stall":
            while not self.stop.wait(0.02):
                pass
            return
        if self.scenario == "stall_after_start":
            handler.wfile.write(
                f"data: {json.dumps(_chunk('开始'), ensure_ascii=False)}\n\n".encode()
            )
            handler.wfile.flush()
            while not self.stop.wait(0.02):
                pass
            return
        if self.scenario == "slow_success":
            time.sleep(0.5)
            self._write_openai(handler, OPENAI_SUCCESS_CHUNKS)
            return
        if self.scenario == "huge":
            handler.wfile.write(
                f"data: {json.dumps(_chunk('x' * (1024 * 1024 + 128)), ensure_ascii=False)}\n\n".encode()
            )
            handler.wfile.flush()
            self._write_openai(handler, OPENAI_SUCCESS_CHUNKS[1:])
            return
        self._write_openai(handler, OPENAI_SUCCESS_CHUNKS)


def patch_runtime(tmp_path: Path) -> tuple[Path, Path]:
    import app.agent.telemetry as telemetry
    import app.infrastructure.persistence.sqlite.database as database_module

    database_path = tmp_path / f"{uuid4().hex}.db"
    trace_path = tmp_path / f"trace-{uuid4().hex}.db"
    database_module.default_database_path = lambda: database_path
    telemetry.trace_database_path = lambda: trace_path
    return database_path, trace_path


@contextmanager
def running_app(model: ModelServer, tmp_path: Path):
    database_path, _ = patch_runtime(tmp_path)
    os.environ["FIT_AGENT_TEST_MODEL_API"] = "openai-completions"
    os.environ["FIT_AGENT_TEST_MODEL_BASE_URL"] = model.base_url
    os.environ["FIT_AGENT_TEST_MODEL_ID"] = "test-model"
    os.environ["FIT_AGENT_TEST_MODEL_API_KEY"] = "test-key"
    os.environ["FIT_AGENT_TEST_MODEL_PROVIDER"] = "local"
    install_test_model_config(root=tmp_path / f"config-{uuid4().hex}")
    with Server(http_module.app) as server:
        with client(server.base_url) as http:
            yield http, database_path


@pytest.fixture(autouse=True)
def clean_http_state():
    yield
    with http_module.runs_lock:
        states = list(http_module.runs.values())
        http_module.runs.clear()
    for state in states:
        state.events.close_normal()
        state.closed_without_terminal = True
    while http_module.active.locked():
        http_module.active.release()
    http_module.reset_fault_closure()
    for name in (
        "FIT_AGENT_RUN_BUDGET_SECONDS",
    ):
        os.environ.pop(name, None)


def create_session(http: httpx.Client, session_id: str) -> None:
    response = http.post(
        "/api/sessions", json={"session_id": session_id, "title": "任务三"}
    )
    assert response.status_code in {200, 201}, response.text


def read_events(response) -> list[dict]:
    return [
        {"event": item.event, "data": TypeAdapter(dict).validate_json(item.data)}
        for item in SSEDecoder().iter_bytes(response.iter_bytes())
    ]


def wait_idle(timeout: float = 15.0) -> None:
    end = time.monotonic() + timeout
    while http_module.active.locked() and time.monotonic() < end:
        time.sleep(0.01)
    assert not http_module.active.locked(), "工作线程未释放运行占用"
    with http_module.runs_lock:
        assert not http_module.runs


def run_body(session_id: str, operation_id: str, text: str = "只回复完成。") -> dict:
    return {
        "session_id": session_id,
        "operation_id": operation_id,
        "request": text,
    }


def post_stream(http: httpx.Client, body: dict) -> tuple[int, list[dict], str | None]:
    status, events, headers = post_stream_path(http, "/api/agent/run", body)
    return status, events, headers.get("X-Run-ID")


def post_stream_path(
    http: httpx.Client, path: str, body: dict
) -> tuple[int, list[dict], httpx.Headers]:
    with http.stream("POST", path, json=body) as response:
        if response.status_code != 200:
            return response.status_code, [], httpx.Headers()
        return response.status_code, read_events(response), response.headers


def db_run(database_path: Path, run_id: str):
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "SELECT * FROM session_runs WHERE id = ?", (run_id,)
        ).fetchone()


# ---------------------------------------------------------------------------
# 有界队列与发送边界
# ---------------------------------------------------------------------------


def _event(name: str, payload: bytes) -> EncodedEvent:
    return EncodedEvent(AgentEvent(name, {"run_id": "x"}), payload)


def test_queue_event_count_and_byte_boundaries() -> None:
    queue = BoundedEventQueue(max_events=256, max_bytes=10_000, max_event_bytes=10_000)
    for index in range(256):
        assert queue.put(_event("message_update", str(index).encode())) == "ok"
    assert queue.pending_events == 256
    assert queue.put(_event("message_update", b"overflow")) == "slow_consumer"
    assert queue.accepting is False

    exact_bytes = BoundedEventQueue(
        max_events=10, max_bytes=100, max_event_bytes=100
    )
    assert exact_bytes.put(_event("message_update", b"x" * 100)) == "ok"
    assert exact_bytes.pending_bytes == 100
    assert exact_bytes.put(_event("message_update", b"x")) == "slow_consumer"

    exact_event = BoundedEventQueue(
        max_events=10, max_bytes=10_000, max_event_bytes=100
    )
    assert exact_event.put(_event("message_update", b"x" * 100)) == "ok"
    assert exact_event.put(_event("message_update", b"x" * 101)) == "output_too_large"


def test_terminal_slot_is_independent_and_starts_drain_clock(monkeypatch) -> None:
    queue = BoundedEventQueue(max_events=2, max_bytes=100, max_event_bytes=100)
    assert queue.put(_event("message_update", b"1")) == "ok"
    assert queue.put(_event("message_update", b"2")) == "ok"
    assert queue.put(_event("message_update", b"3")) == "slow_consumer"
    assert queue.put_terminal(_event("done", b"done")) == "ok"
    assert queue.terminal_filled
    assert queue.take().payload == b"1"
    assert queue.take().payload == b"2"
    assert queue.take() is None
    assert queue.take_terminal().payload == b"done"
    monkeypatch.setattr(stream_module, "TERMINAL_DRAIN_TOTAL_SECONDS", 10.0)
    queue2 = BoundedEventQueue()
    assert queue2.put_terminal(_event("done", b"done")) == "ok"
    first = queue2.terminal_remaining()
    assert first is not None and 9.0 < first <= 10.0
    time.sleep(0.02)
    assert queue2.terminal_remaining() < first


def test_application_runtime_uses_http_output_without_owning_queue() -> None:
    state = ApplicationRunState(
        uuid4(), create_output=HttpRunOutput, budget_seconds=30.0
    )
    output = state.output
    assert isinstance(output, HttpRunOutput)
    assert output.control is state.control
    assert state.control.cancel is state.cancel
    assert state.publish("tool_start", {"tool_call_id": "call-1"})
    assert output.events.take().event.data["run_id"] == str(state.run_id)
    state.publish_terminal("error", {"status": "failed"})
    assert output.events.take_terminal().event.data["tool_call_id"] == "call-1"

    state.disconnect()
    assert state.cancel.is_set()
    state.mark_fault("worker_stuck", state.cancel, (state.tool_tracker,))
    assert state.closed_without_terminal
    assert state.inflight_work is state.cancel
    assert state.inflight_tools == (state.tool_tracker,)
    assert not output.events.accepting
    assert output.events.terminal_filled


def test_single_event_oversize_records_output_too_large() -> None:
    state = RunState(uuid4(), budget_seconds=30.0)
    state.publish("message_update", {"blob": "x" * (1024 * 1024)})
    assert state.control.stop.reason == "output_too_large"
    assert state.events.empty()
    assert terminal_decision(None, False, "output_too_large")[:2] == (
        "failed",
        "output_too_large",
    )


def test_utf8_bytes_include_json_escapes_and_tool_call_id() -> None:
    state = RunState(uuid4(), budget_seconds=30.0)
    state.publish(
        "tool_execution_end",
        {
            "tool_call_id": "调用-1",
            "tool_name": "读",
            "content": '内容 "引号"\n第二行',
            "is_error": False,
        },
    )
    assert state.events.pending_events == 1
    event = state.events.take()
    expected = encode(event.event).encode("utf-8")
    assert event.payload == expected
    assert b"tool_call_id" in expected
    assert state.events.pending_bytes == 0

    state.publish_terminal(
        "error",
        {"status": "cancelled", "code": "cancelled", "message": "执行已取消。"},
    )
    terminal = state.events.take_terminal()
    assert terminal is not None
    assert b"tool_call_id" in terminal.payload


def test_heartbeat_and_terminal_drain(monkeypatch) -> None:
    class FakeRequest:
        async def is_disconnected(self) -> bool:
            return False

    monkeypatch.setattr(stream_module, "SSE_HEARTBEAT_SECONDS", 0.02)
    state = RunState(uuid4(), budget_seconds=30.0)

    async def collect() -> list[bytes]:
        generator = stream(FakeRequest(), state)
        return [await asyncio.wait_for(anext(generator), 1.0) for _ in range(2)]

    assert asyncio.run(collect()) == [
        stream_module.SSE_HEARTBEAT,
        stream_module.SSE_HEARTBEAT,
    ]

    monkeypatch.setattr(stream_module, "TERMINAL_DRAIN_TOTAL_SECONDS", 0.05)
    state.events.put_terminal(_event("done", b'event: done\ndata: {}\n\n'))
    time.sleep(0.1)

    async def expired() -> None:
        generator = stream(FakeRequest(), state)
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(generator), 1.0)

    asyncio.run(expired())


def test_send_timeout_is_enforced_at_asgi_boundary(monkeypatch) -> None:
    monkeypatch.setattr(stream_module, "SSE_SEND_WAIT_SECONDS", 0.05)
    state = RunState(uuid4(), budget_seconds=30.0)

    async def hidden_send(_message) -> None:
        await asyncio.Event().wait()

    async def never_receive():
        await asyncio.Event().wait()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 80),
        "scheme": "http",
    }

    async def call() -> None:
        runner = RunStream(state, stream(type("R", (), {"is_disconnected": lambda self: _false()})(), state))
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(runner(scope, never_receive, hidden_send), 1.0)
        assert time.monotonic() - started < 0.5

    async def _false() -> bool:
        return False

    asyncio.run(call())
    assert state.control.cancelled


# ---------------------------------------------------------------------------
# HTTP 准入与运行结束
# ---------------------------------------------------------------------------


def test_normal_run_persists_terminal_before_done_and_releases_slot() -> None:
    with ModelServer("success") as model, running_app(model, TMP_ROOT) as (http, db_path):
        session_id = str(uuid4())
        create_session(http, session_id)
        status, events, run_id = post_stream(
            http, run_body(session_id, str(uuid4()))
        )
        assert status == 200 and run_id is not None
        assert events[-1]["event"] == "done"
        assert events[-1]["data"]["status"] == "completed"
        row = db_run(db_path, run_id)
        assert row["status"] == "completed"
        assert row["finished_at"] is not None
        wait_idle()
        assert model.requests == 1


def test_same_operation_concurrent_accepts_once_and_charges_once() -> None:
    with ModelServer("slow_success") as model, running_app(model, TMP_ROOT) as (http, db_path):
        session_id = str(uuid4())
        create_session(http, session_id)
        body = run_body(session_id, str(uuid4()))
        base_url = str(http.base_url)

        def submit():
            with client(base_url) as local:
                return post_stream(local, body)

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = [future.result() for future in [pool.submit(submit), pool.submit(submit)]]
        assert model.requests == 1
        streams = [item for item in outcomes if item[1]]
        replays = [item for item in outcomes if not item[1]]
        assert len(streams) == 1 and len(replays) == 1
        assert replays[0][0] == 200
        with sqlite3.connect(db_path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM session_runs WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
        assert count == 1
        assert http_module.session_run_limits.remaining(session_id) == 9
        wait_idle()


def test_capacity_rejects_other_session_with_503_and_keeps_rate() -> None:
    with ModelServer("stall") as model, running_app(model, TMP_ROOT) as (http, _):
        busy_session = str(uuid4())
        other_session = str(uuid4())
        create_session(http, busy_session)
        create_session(http, other_session)
        with http.stream(
            "POST", "/api/agent/run",
            json=run_body(busy_session, str(uuid4())),
        ) as response:
            assert response.status_code == 200
            assert model.started.wait(5)
            before = http_module.session_run_limits.remaining(other_session)
            rejected = http.post(
                "/api/agent/run", json=run_body(other_session, str(uuid4()))
            )
            assert rejected.status_code == 503
            assert rejected.json()["detail"]["code"] == "capacity_exceeded"
            # 容量拒绝不扣频率额度。
            assert http_module.session_run_limits.remaining(other_session) == before
        wait_idle()


def test_same_session_conflict_keeps_409() -> None:
    with ModelServer("stall") as model, running_app(model, TMP_ROOT) as (http, _):
        session_id = str(uuid4())
        create_session(http, session_id)
        with http.stream(
            "POST", "/api/agent/run",
            json=run_body(session_id, str(uuid4())),
        ) as response:
            assert response.status_code == 200
            assert model.started.wait(5)
            conflict = http.post(
                "/api/agent/run", json=run_body(session_id, str(uuid4()))
            )
            assert conflict.status_code == 409
            assert conflict.json()["detail"]["code"] == "run_busy"
        wait_idle()


def test_rate_limit_429_retry_after_and_replay_bypasses() -> None:
    with ModelServer("success") as model, running_app(model, TMP_ROOT) as (http, _):
        session_id = str(uuid4())
        create_session(http, session_id)
        first_body = run_body(session_id, str(uuid4()))
        first_run = None
        for index in range(10):
            body = first_body if index == 0 else run_body(session_id, str(uuid4()))
            status, events, run_id = post_stream(http, body)
            assert status == 200, (index, status)
            assert events[-1]["event"] == "done"
            if index == 0:
                first_run = run_id
        wait_idle()
        assert http_module.session_run_limits.remaining(session_id) == 0
        limited = http.post(
            "/api/agent/run", json=run_body(session_id, str(uuid4()))
        )
        assert limited.status_code == 429
        assert int(limited.headers["Retry-After"]) >= 1
        assert limited.json()["detail"]["code"] == "rate_limited"
        replay = http.post("/api/agent/run", json=first_body)
        assert replay.status_code == 200
        assert replay.json()["run_id"] == first_run

        # 容量已满且本会话频率已满时，同键重放仍返回原受理结果。
        model.scenario = "slow_success"
        other_session = str(uuid4())
        create_session(http, other_session)
        with http.stream(
            "POST", "/api/agent/run",
            json=run_body(other_session, str(uuid4())),
        ) as busy:
            assert busy.status_code == 200
            replay_while_busy = http.post("/api/agent/run", json=first_body)
            assert replay_while_busy.status_code == 200
            assert replay_while_busy.json()["run_id"] == first_run
            busy.close()
        wait_idle()
        model.scenario = "success"


def test_accept_failure_does_not_charge_rate() -> None:
    with ModelServer("success") as model, running_app(model, TMP_ROOT) as (http, _):
        session_id = str(uuid4())
        create_session(http, session_id)
        before = http_module.session_run_limits.remaining(session_id)
        rejected = http.post(
            "/api/agent/edit",
            json={
                "session_id": session_id,
                "operation_id": str(uuid4()),
                "target_entry_id": str(uuid4()),
                "request": "不存在目标",
            },
        )
        assert rejected.status_code == 404
        assert http_module.session_run_limits.remaining(session_id) == before
        # 受理失败后的成功重试只扣一次。
        status, events, _ = post_stream(http, run_body(session_id, str(uuid4())))
        assert status == 200 and events[-1]["event"] == "done"
        assert http_module.session_run_limits.remaining(session_id) == before - 1


def test_edit_and_regenerate_share_admission_bucket() -> None:
    with ModelServer("success") as model, running_app(model, TMP_ROOT) as (http, _):
        session_id = str(uuid4())
        create_session(http, session_id)
        status, events, headers = post_stream_path(
            http, "/api/agent/run", run_body(session_id, str(uuid4()))
        )
        assert status == 200 and events[-1]["event"] == "done"
        request_entry_id = headers["X-Request-Entry-ID"]
        assert http_module.session_run_limits.remaining(session_id) == 9

        edit_body = {
            "session_id": session_id,
            "operation_id": str(uuid4()),
            "target_entry_id": request_entry_id,
            "request": "编辑后的输入",
        }
        status, events, headers = post_stream_path(
            http, "/api/agent/edit", edit_body
        )
        assert status == 200 and events[-1]["event"] == "done"
        assert http_module.session_run_limits.remaining(session_id) == 8
        edited_entry_id = headers["X-Request-Entry-ID"]

        history = http.get(f"/api/sessions/{session_id}/history").json()
        target = next(
            entry["entry_id"]
            for entry in history["entries"]
            if entry.get("message", {}).get("role") == "user"
            and entry["entry_id"] == edited_entry_id
        )
        regenerate_body = {
            "session_id": session_id,
            "operation_id": str(uuid4()),
            "target_entry_id": target,
        }
        status, events, _ = post_stream_path(
            http, "/api/agent/regenerate", regenerate_body
        )
        assert status == 200 and events[-1]["event"] == "done"
        assert http_module.session_run_limits.remaining(session_id) == 7


def test_confirmation_rejected_while_run_holds_slot_without_registration() -> None:
    with ModelServer("stall") as model, running_app(model, TMP_ROOT) as (http, _):
        session_id = str(uuid4())
        create_session(http, session_id)
        with http.stream(
            "POST", "/api/agent/run",
            json=run_body(session_id, str(uuid4())),
        ) as response:
            assert response.status_code == 200
            assert model.started.wait(5)
            operation_id = str(uuid4())
            rejected = http.post(
                f"/api/sessions/{session_id}/confirmations",
                json={
                    "operation_id": operation_id,
                    "business_kind": "profile",
                    "proposal_id": str(uuid4()),
                    "display_entry_id": str(uuid4()),
                },
            )
            assert rejected.status_code == 409
            assert rejected.json()["detail"]["code"] == "run_busy"
            queried = http.get(
                f"/api/sessions/{session_id}/confirmations/{operation_id}"
            )
            assert queried.status_code == 404
        wait_idle()


def test_run_timeout_persists_failed_run_timeout(monkeypatch) -> None:
    with ModelServer("stall") as model, running_app(model, TMP_ROOT) as (http, db_path):
        monkeypatch.setenv("FIT_AGENT_RUN_BUDGET_SECONDS", "0.3")
        session_id = str(uuid4())
        create_session(http, session_id)
        status, events, run_id = post_stream(
            http, run_body(session_id, str(uuid4()), "阻塞运行")
        )
        assert status == 200
        assert events[-1]["event"] == "error"
        assert events[-1]["data"]["code"] == "run_timeout"
        row = db_run(db_path, run_id)
        assert row["status"] == "failed"
        assert row["error_code"] == "run_timeout"
        assert row["stop_reason"] == "run_timeout"
        wait_idle()


def test_disconnect_cancels_and_operation_query_finds_run() -> None:
    with ModelServer("stall_after_start") as model, running_app(model, TMP_ROOT) as (http, db_path):
        session_id = str(uuid4())
        create_session(http, session_id)
        operation_id = str(uuid4())
        with http.stream(
            "POST", "/api/agent/run",
            json=run_body(session_id, operation_id),
        ) as response:
            run_id = response.headers["X-Run-ID"]
            events = []
            for event in SSEDecoder().iter_bytes(response.iter_bytes()):
                events.append(event.event)
                if event.event == "message_start":
                    break
        assert "message_start" in events
        wait_idle()
        row = db_run(db_path, run_id)
        assert row["status"] == "cancelled"
        assert row["stop_reason"] == "cancelled"
        queried = http.get(f"/api/sessions/{session_id}/operations/{operation_id}")
        assert queried.status_code == 200
        body = queried.json()
        assert body["accepted"] is True
        assert body["run"]["run_id"] == run_id


def test_stop_closes_steering_and_settles_accepted_input() -> None:
    with ModelServer("stall_after_start") as model, running_app(model, TMP_ROOT) as (http, _):
        session_id = str(uuid4())
        create_session(http, session_id)
        with http.stream(
            "POST", "/api/agent/run",
            json=run_body(session_id, str(uuid4())),
        ) as response:
            run_id = response.headers["X-Run-ID"]
            # 保持迭代器存活，避免 break 触发生成器关闭并提前断连。
            iterator = SSEDecoder().iter_bytes(response.iter_bytes())
            while True:
                event = next(iterator)
                if event.event == "message_start":
                    break
            accepted = http.post(
                f"/api/agent/runs/{run_id}/steering",
                json={
                    "session_id": session_id,
                    "operation_id": str(uuid4()),
                    "message": "补充输入",
                },
            )
            assert accepted.status_code == 200, accepted.text
            steering_id = accepted.json()["steering_id"]
        wait_idle()
        stored = http.get(f"/api/sessions/{session_id}/runs/{run_id}/steering").json()
        target = next(item for item in stored["steering"] if item["steering_id"] == steering_id)
        assert target["status"] in {"consumed", "discarded"}
        late = http.post(
            f"/api/agent/runs/{run_id}/steering",
            json={
                "session_id": session_id,
                "operation_id": str(uuid4()),
                "message": "迟到输入",
            },
        )
        assert late.status_code == 409


def test_output_too_large_http_fails_and_keeps_run_record() -> None:
    with ModelServer("huge") as model, running_app(model, TMP_ROOT) as (http, db_path):
        session_id = str(uuid4())
        create_session(http, session_id)
        status, events, run_id = post_stream(
            http, run_body(session_id, str(uuid4()), "输出超限")
        )
        assert status == 200
        assert events[-1]["event"] == "error"
        assert events[-1]["data"]["status"] == "failed"
        assert events[-1]["data"]["code"] == "output_too_large"
        row = db_run(db_path, run_id)
        assert row["status"] == "failed"
        assert row["stop_reason"] == "output_too_large"
        wait_idle()


# ---------------------------------------------------------------------------
# 真实 Agent Loop + 真实工具：协作/非协作退出、超时与故障封闭
# ---------------------------------------------------------------------------


def _patch_short_cooperative(monkeypatch) -> None:
    monkeypatch.setattr(coordination_module, "CANCEL_COOPERATIVE_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(governance, "CANCEL_COOPERATIVE_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(tool_module, "CANCEL_COOPERATIVE_WAIT_SECONDS", 0.2)


def _install_tool(monkeypatch, tool: AgentTool) -> None:
    monkeypatch.setattr(
        agent_routes, "create_file_tools", lambda _session_id: {tool.name: tool}
    )


def _wait_fault_state(run_id: str, timeout: float = 5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with http_module.runs_lock:
            state = http_module.runs.get(UUID(run_id))
        if state is not None and state.closed_without_terminal:
            return state
        time.sleep(0.01)
    raise AssertionError("运行未进入故障封闭")


def _wait_db_diagnostic(
    db_path: Path, run_id: str, reason: str, timeout: float = 8.0
):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        row = db_run(db_path, run_id)
        if row is not None and row["stop_diagnostic"] == reason:
            return row
        time.sleep(0.02)
    raise AssertionError("故障诊断未落库")


def _wait_until(predicate, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("等待条件超时")


def test_http_noncooperative_tool_keeps_slot_and_records_worker_stuck(
    monkeypatch,
) -> None:
    _patch_short_cooperative(monkeypatch)
    started, release = Event(), Event()

    def blocking(_tool_call_id, _params, _signal, _on_update):
        started.set()
        release.wait(30)
        return AgentToolResult([TextContent(type="text", text="released")])

    tool = AgentTool(
        "slow", "slow", ToolArgs, blocking, execution_mode="sequential"
    )
    _install_tool(monkeypatch, tool)
    model = ModelServer("tool_once")
    state = None
    with model, running_app(model, TMP_ROOT) as (http, db_path):
        try:
            monkeypatch.setenv("FIT_AGENT_RUN_BUDGET_SECONDS", "8.0")
            session_id = str(uuid4())
            create_session(http, session_id)
            status, events, run_id = post_stream(
                http, run_body(session_id, str(uuid4()), "执行工具")
            )
            assert status == 200
            _wait_until(lambda: started.is_set(), 5.0)
            assert started.is_set()
            assert all(item["event"] not in {"done", "error"} for item in events)
            state = _wait_fault_state(run_id)
            assert state.fault_reason == "worker_stuck"
            assert state.closed_without_terminal is True
            handles = state.tool_tracker.snapshot()
            assert len(handles) == 1
            assert handles[0].thread is not None
            assert handles[0].thread.is_alive()
            assert not handles[0].finished
            assert http_module.active.locked()
            assert http_module.is_fault_closed()
            row = _wait_db_diagnostic(db_path, run_id, "worker_stuck")
            assert row["status"] == "running"
            assert model.requests == 1
        finally:
            release.set()
            if state is not None:
                _wait_until(lambda: not state.tool_tracker.pending(), 5.0)


def test_http_cooperative_tool_exits_on_cancel_and_releases_slot(
    monkeypatch,
) -> None:
    started = Event()

    def cooperative(_tool_call_id, _params, signal, _on_update):
        started.set()
        assert signal.wait(30)
        return AgentToolResult([TextContent(type="text", text="stopped")])

    tool = AgentTool(
        "slow", "slow", ToolArgs, cooperative, execution_mode="sequential"
    )
    _install_tool(monkeypatch, tool)
    model = ModelServer("tool_once")
    with model, running_app(model, TMP_ROOT) as (http, db_path):
        monkeypatch.setenv("FIT_AGENT_RUN_BUDGET_SECONDS", "8.0")
        session_id = str(uuid4())
        create_session(http, session_id)
        status, events, run_id = post_stream(
            http, run_body(session_id, str(uuid4()), "执行工具")
        )
        assert status == 200
        _wait_until(lambda: started.is_set(), 5.0)
        assert started.is_set()
        assert events[-1]["event"] == "error"
        assert events[-1]["data"]["code"] == "run_timeout"
        row = db_run(db_path, run_id)
        assert row["status"] == "failed"
        assert row["error_code"] == "run_timeout"
        assert row["stop_reason"] == "run_timeout"
        assert model.requests == 1
        wait_idle()


def test_http_tool_timeout_is_persisted(monkeypatch) -> None:
    started = Event()

    def cooperative(_tool_call_id, _params, signal, _on_update):
        started.set()
        assert signal.wait(5)
        return AgentToolResult([TextContent(type="text", text="stopped")])

    tool = AgentTool(
        "slow", "slow", ToolArgs, cooperative, execution_mode="sequential"
    )
    _install_tool(monkeypatch, tool)
    monkeypatch.setattr(execution_module, "TOOL_EXECUTION_SECONDS", 0.2)
    model = ModelServer("tool_once")
    with model, running_app(model, TMP_ROOT) as (http, db_path):
        session_id = str(uuid4())
        create_session(http, session_id)
        status, events, run_id = post_stream(
            http, run_body(session_id, str(uuid4()), "执行工具")
        )
        assert status == 200
        assert started.is_set()
        assert events[-1]["event"] == "error"
        assert events[-1]["data"]["code"] == "tool_timeout"
        row = db_run(db_path, run_id)
        assert row["status"] == "failed"
        assert row["error_code"] == "tool_timeout"
        assert row["stop_reason"] == "tool_timeout"
        wait_idle()


def test_http_model_timeout_is_persisted(monkeypatch) -> None:
    # 真实适配器按注入的首个增量预算超时；仅缩短时限，不替换模型实现。
    timeouts = ModelTimeouts(first_increment_seconds=0.2)
    monkeypatch.setattr(openai_completions, "DEFAULT_MODEL_TIMEOUTS", timeouts)
    monkeypatch.setattr(ai_stream, "DEFAULT_MODEL_TIMEOUTS", timeouts)
    model = ModelServer("stall")
    with model, running_app(model, TMP_ROOT) as (http, db_path):
        session_id = str(uuid4())
        create_session(http, session_id)
        status, events, run_id = post_stream(
            http, run_body(session_id, str(uuid4()), "阻塞模型")
        )
        assert status == 200
        assert events[-1]["event"] == "error"
        assert events[-1]["data"]["code"] == "model_timeout"
        row = db_run(db_path, run_id)
        assert row["status"] == "failed"
        assert row["error_code"] == "model_timeout"
        assert row["stop_reason"] == "model_timeout"
        wait_idle()


def _locking_service(mode: str, lock_held: Event, release_lock: Event):
    class LockingService(SessionService):
        finish_calls = 0
        _triggered = False

        def __init__(self, repository) -> None:
            super().__init__(repository)
            queries = self.queries
            runs = self.runs
            original_projection = queries.get_projection
            original_get_run = queries.get_run
            original_finish = runs.finish_run

            async def _hold() -> None:
                async with self._repository.transaction():
                    lock_held.set()
                    await asyncio.to_thread(release_lock.wait, 10)

            async def _trigger() -> None:
                if type(self)._triggered:
                    return
                type(self)._triggered = True
                asyncio.create_task(_hold())
                await asyncio.to_thread(lock_held.wait, 5)

            async def get_projection(*args, **kwargs):
                if mode == "projection":
                    await _trigger()
                return await original_projection(*args, **kwargs)

            async def get_run(*args, **kwargs):
                if mode == "status":
                    await _trigger()
                    return await original_get_run(*args, **kwargs)
                run = await original_get_run(*args, **kwargs)
                if mode == "finish":
                    await _trigger()
                return run

            async def finish_run(*args, **kwargs):
                type(self).finish_calls += 1
                return await original_finish(*args, **kwargs)

            queries.get_projection = get_projection
            queries.get_run = get_run
            runs.finish_run = finish_run

    return LockingService


def test_projection_database_lock_fault_closes_within_budget(monkeypatch) -> None:
    _patch_short_cooperative(monkeypatch)
    lock_held, release_lock = Event(), Event()
    monkeypatch.setattr(
        http_module,
        "SessionService",
        _locking_service("projection", lock_held, release_lock),
    )
    model = ModelServer("success")
    state = None
    with model, running_app(model, TMP_ROOT) as (http, db_path):
        try:
            monkeypatch.setenv("FIT_AGENT_RUN_BUDGET_SECONDS", "0.2")
            session_id = str(uuid4())
            create_session(http, session_id)
            started = time.monotonic()
            status, _events, run_id = post_stream(
                http, run_body(session_id, str(uuid4()), "读取投影")
            )
            elapsed = time.monotonic() - started
            assert status == 200
            assert elapsed < 3.0
            state = _wait_fault_state(run_id)
            assert state.fault_reason == "terminal_persist_failed"
            assert state.closed_without_terminal is True
            assert state.inflight_reads
            assert any(not task.done() for task in state.inflight_reads)
            assert http_module.active.locked()
            assert http_module.is_fault_closed()
            assert model.requests == 0
        finally:
            release_lock.set()
        row = _wait_db_diagnostic(db_path, run_id, "terminal_persist_failed")
        assert row["status"] == "running"
        if state is not None:
            _wait_until(
                lambda: all(task.done() for task in state.inflight_reads), 5.0
            )


def test_status_read_database_lock_fault_closes_within_budget(monkeypatch) -> None:
    monkeypatch.setattr(coordination_module, "TERMINAL_PERSIST_WAIT_SECONDS", 0.2)
    lock_held, release_lock = Event(), Event()
    monkeypatch.setattr(
        http_module,
        "SessionService",
        _locking_service("status", lock_held, release_lock),
    )
    model = ModelServer("success")
    state = None
    with model, running_app(model, TMP_ROOT) as (http, db_path):
        try:
            session_id = str(uuid4())
            create_session(http, session_id)
            status, _events, run_id = post_stream(
                http, run_body(session_id, str(uuid4()), "读取状态")
            )
            assert status == 200
            state = _wait_fault_state(run_id)
            assert state.fault_reason == "terminal_persist_failed"
            assert state.closed_without_terminal is True
            assert state.inflight_reads
            assert http_module.active.locked()
            assert http_module.is_fault_closed()
            assert model.requests == 1
        finally:
            release_lock.set()
        row = _wait_db_diagnostic(db_path, run_id, "terminal_persist_failed")
        assert row["status"] == "running"
        if state is not None:
            _wait_until(
                lambda: all(task.done() for task in state.inflight_reads), 5.0
            )


def test_terminal_persist_timeout_uses_real_sqlite_lock(monkeypatch) -> None:
    monkeypatch.setattr(coordination_module, "TERMINAL_PERSIST_WAIT_SECONDS", 0.1)
    lock_held, release_lock = Event(), Event()
    locking_service = _locking_service("finish", lock_held, release_lock)
    monkeypatch.setattr(http_module, "SessionService", locking_service)
    model = ModelServer("success")
    state = None
    with model, running_app(model, TMP_ROOT) as (http, db_path):
        try:
            session_id = str(uuid4())
            create_session(http, session_id)
            status, _events, run_id = post_stream(
                http, run_body(session_id, str(uuid4()), "提交终态")
            )
            assert status == 200
            state = _wait_fault_state(run_id)
            assert state.fault_reason == "terminal_persist_failed"
            assert state.closed_without_terminal is True
            assert http_module.active.locked()
            assert http_module.is_fault_closed()
            early = db_run(db_path, run_id)
            assert early["status"] == "running"
        finally:
            release_lock.set()
        _wait_until(
            lambda: db_run(db_path, run_id)["status"] == "completed", 8.0
        )
        row = db_run(db_path, run_id)
        assert row["status"] == "completed"
        assert locking_service.finish_calls == 1
        diagnostic = _wait_db_diagnostic(
            db_path, run_id, "terminal_persist_failed"
        )
        assert diagnostic["status"] == "completed"
        assert http_module.active.locked()
        assert http_module.is_fault_closed()

