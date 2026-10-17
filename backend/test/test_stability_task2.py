"""任务二定向验证：模型消费边界的时间预算、有限重试、逐次记账与工具真实退出。

模型链路使用真实 SDK、真实 HTTPX 与本地 loopback 随机端口；工具执行使用真实工具。
不替换成功结果、不伪造用量。时间边界通过 ModelTimeouts/RetryPolicy/RunDeadline/工具 timeout 注入缩短值。
"""

from __future__ import annotations

import asyncio
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from typing import Callable
from uuid import uuid4

import pytest
from pydantic import BaseModel

from app.agent.budget import (
    ModelRequestLimitExceeded,
    RecoveryBudgetExhausted,
    RequestBudget,
)
from app.agent.agent_loop import run_agent_loop
from app.agent.cache_observation import NORMAL_CALL, CacheObserver, read_observations
from app.agent.compaction import compaction_scope, generate_compaction_summary
from app.agent.config import AgentLoopConfig
from app.agent.governance import RunControl, RunDeadline
from app.agent.tool import (
    AgentTool,
    AgentToolResult,
    CredentialDetectedError,
    ToolExecutionTimeout,
    ToolExecutionTracker,
    run_tool_batch,
)
from app.ai.messages import (
    SystemMessage,
    TextContent,
    ToolCall,
    UserMessage,
)
from app.ai.stream import (
    ModelAttemptTimeout,
    ModelIncrementTimeout,
    RunDeadlineExceeded,
    stream,
)
from app.ai.types import ModelSpec, ModelTimeouts, RetryPolicy
from app.model_config import ModelConfig

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


OPENAI_TOOL_CALL_CHUNKS = [
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
                            "function": {"name": "slow", "arguments": ""},
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
                        {"index": 0, "function": {"arguments": '{"text":"one"}'}}
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
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 1,
                            "id": "call_2",
                            "type": "function",
                            "function": {"name": "slow", "arguments": ""},
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
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {"index": 1, "function": {"arguments": '{"text":"two"}'}}
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
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
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


ANTHROPIC_SUCCESS_EVENTS = [
    (
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-test",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            },
        },
    ),
    (
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
    ),
    (
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "完成"},
        },
    ),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    (
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 5},
        },
    ),
    ("message_stop", {"type": "message_stop"}),
]


class ScriptedHttpServer:
    """本地可控真实端点：按场景返回 SSE、错误或延迟；只统计真实请求数。"""

    def __init__(self, scenario: str, protocol: str) -> None:
        self.scenario = scenario
        self.protocol = protocol
        self.requests = 0
        self.started = Event()
        self.release = Event()
        self._stop = Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
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

    def _write_event(self, handler: BaseHTTPRequestHandler, name: str, data: dict) -> None:
        handler.wfile.write(f"event: {name}\n".encode())
        handler.wfile.write(
            f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode()
        )
        handler.wfile.flush()

    def _write_openai(self, handler, chunks) -> None:
        for chunk in chunks:
            handler.wfile.write(
                f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
            )
            handler.wfile.flush()
        handler.wfile.write(b"data: [DONE]\n\n")
        handler.wfile.flush()

    def respond(self, handler: BaseHTTPRequestHandler) -> None:
        if self.scenario == "fail_then_success" and self.requests > 1:
            scenario = "success"
        elif self.scenario == "fail_then_success":
            scenario = "transient_500"
        elif self.scenario == "truncate_then_success" and self.requests > 1:
            scenario = "success"
        else:
            scenario = self.scenario
        if scenario in {
            "transient_500",
            "transient_500_wait",
            "non_transient_500",
            "auth_401",
            "permission_403",
            "rate_429",
            "quota_429",
        }:
            self.respond_error(handler, scenario)
            return
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        truncate = scenario in {"start_then_truncate", "truncate_then_success"}
        if truncate:
            # 承诺比实际发送更多的字节，然后提前关闭：读取以可重试传输错误结束。
            handler.send_header("Content-Length", "4096")
        handler.end_headers()
        if truncate:
            if self.protocol == "anthropic":
                self._write_event(handler, "message_start", ANTHROPIC_SUCCESS_EVENTS[0][1])
            handler.wfile.flush()
            return
        if scenario in {"stall", "delta_then_stall"}:
            if scenario == "delta_then_stall":
                if self.protocol == "anthropic":
                    for name, data in ANTHROPIC_SUCCESS_EVENTS[:3]:
                        self._write_event(handler, name, data)
                else:
                    chunk = dict(OPENAI_SUCCESS_CHUNKS[0])
                    chunk["choices"] = [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "开始"},
                            "finish_reason": None,
                        }
                    ]
                    handler.wfile.write(
                        f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
                    )
                    handler.wfile.flush()
            while not self._stop.wait(0.02):
                pass
            return
        if scenario == "partial_tool":
            chunk = {
                "id": "chatcmpl-1",
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
                                    "function": {
                                        "name": "echo",
                                        "arguments": '{"text":',
                                    },
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            }
            handler.wfile.write(
                f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
            )
            handler.wfile.flush()
            return
        if scenario == "tool_calls":
            self._write_openai(handler, OPENAI_TOOL_CALL_CHUNKS)
            return
        if scenario == "heartbeat":
            while not self._stop.wait(0.02):
                try:
                    handler.wfile.write(b": heartbeat\n\n")
                    handler.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
            return
        if scenario == "start_then_close":
            if self.protocol == "anthropic":
                self._write_event(handler, "message_start", ANTHROPIC_SUCCESS_EVENTS[0][1])
            return
        if self.protocol == "anthropic":
            for name, data in ANTHROPIC_SUCCESS_EVENTS:
                self._write_event(handler, name, data)
        else:
            self._write_openai(handler, OPENAI_SUCCESS_CHUNKS)

    def respond_error(self, handler: BaseHTTPRequestHandler, scenario: str) -> None:
        status, body, headers = {
            "transient_500": (
                500,
                {"error": {"message": "temporary", "type": "server_error", "code": "server_error"}},
                {},
            ),
            "transient_500_wait": (
                500,
                {"error": {"message": "temporary", "type": "server_error", "code": "server_error"}},
                {"Retry-After": "5"},
            ),
            "non_transient_500": (
                500,
                {"error": {"message": "bad request", "type": "invalid_request_error", "code": "invalid_request_error"}},
                {},
            ),
            "auth_401": (
                401,
                {"error": {"message": "invalid key", "type": "invalid_request_error", "code": "invalid_api_key"}},
                {},
            ),
            "permission_403": (
                403,
                {"error": {"message": "permission denied", "type": "permission_error", "code": "permission_denied"}},
                {},
            ),
            "rate_429": (
                429,
                {"error": {"message": "slow down", "type": "rate_limit_error", "code": "rate_limit_exceeded"}},
                {"Retry-After": "0.1"},
            ),
            "quota_429": (
                429,
                {"error": {"message": "quota exhausted", "type": "insufficient_quota", "code": "insufficient_quota"}},
                {},
            ),
        }[scenario]
        if self.protocol == "anthropic":
            body = {"type": "error", "error": body["error"]}
        payload = json.dumps(body, ensure_ascii=False).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        for name, value in headers.items():
            handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.write(payload)
        handler.wfile.flush()

    def __enter__(self) -> "ScriptedHttpServer":
        self._thread.start()
        return self

    def __exit__(self, *_error) -> None:
        self._stop.set()
        self.release.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


def spec(protocol: str, base_url: str) -> ModelSpec:
    return ModelSpec(
        api="openai-completions" if protocol == "openai" else "anthropic-messages",
        provider="local",
        id="test" if protocol == "openai" else "claude-test",
        base_url=base_url,
        context_window=128000,
        max_tokens=16384,
    )


def context() -> dict:
    return {"messages": [UserMessage(role="user", content="hi", timestamp=0)]}


def options(
    *,
    budget: RequestBudget | None = None,
    control: RunControl | None = None,
    timeouts: ModelTimeouts | None = None,
    retry: RetryPolicy | None = None,
    recovery: bool = False,
    observation_factory=None,
) -> dict:
    return {
        "api_key": "test-key",
        "max_tokens": 64,
        "budget": budget,
        "deadline": control.deadline if control is not None else None,
        "signal": control.cancel if control is not None else None,
        "run_control": control,
        "timeouts": timeouts,
        "retry_policy": retry,
        "recovery": recovery,
        "observation_factory": observation_factory,
    }


async def consume(response) -> AssistantMessage:
    async for _ in response:
        pass
    return await response.result()


FAST_TIMEOUTS = ModelTimeouts(
    http_connect_seconds=2,
    http_pool_seconds=1,
    http_write_seconds=2,
    http_read_seconds=5,
    first_increment_seconds=0.2,
    increment_interval_seconds=0.2,
    attempt_seconds=5,
)
FAST_RETRY = RetryPolicy(backoff_caps=(0.01, 0.02), max_wait_seconds=1, max_attempts=3)


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_first_increment_timeout(protocol: str) -> None:
    with ScriptedHttpServer("stall", protocol) as server:
        control = RunControl(deadline=RunDeadline(seconds=5))
        budget = RequestBudget()
        response = stream(
            spec(protocol, server.base_url),
            context(),
            options(budget=budget, control=control, timeouts=FAST_TIMEOUTS, retry=FAST_RETRY),
        )
        with pytest.raises(ModelIncrementTimeout):
            asyncio.run(consume(response))
        assert server.requests == 1
        assert budget.used == 1 and budget.recovery_used == 0
        assert control.stop.reason == "model_timeout"
        # 超时路径同样关闭转发源；适配器内部 async with 负责响应与客户端关闭。
        assert response._source.ag_frame is None


def test_heartbeat_does_not_refresh_progress() -> None:
    with ScriptedHttpServer("heartbeat", "openai") as server:
        response = stream(
            spec("openai", server.base_url),
            context(),
            options(timeouts=FAST_TIMEOUTS, retry=FAST_RETRY),
        )
        with pytest.raises(ModelIncrementTimeout):
            asyncio.run(consume(response))
        assert server.requests == 1


def test_attempt_limit_not_extended_by_valid_increments(tmp_path) -> None:
    # 服务端持续发送有效增量，但仍必须在单次尝试上限处结束。
    class DripServer(ScriptedHttpServer):
        def respond(self, handler) -> None:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.end_headers()
            for index in range(200):
                if self._stop.is_set():
                    return
                chunk = dict(OPENAI_SUCCESS_CHUNKS[0])
                chunk["choices"] = [
                    {"index": 0, "delta": {"content": f"{index}"}, "finish_reason": None}
                ]
                handler.wfile.write(
                    f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
                )
                handler.wfile.flush()
                time.sleep(0.02)

    with DripServer("drip", "openai") as server:
        timeouts = ModelTimeouts(
            http_connect_seconds=2,
            http_pool_seconds=1,
            http_write_seconds=2,
            http_read_seconds=5,
            first_increment_seconds=1.0,
            increment_interval_seconds=1.0,
            attempt_seconds=1.0,
        )
        response = stream(
            spec("openai", server.base_url),
            context(),
            options(timeouts=timeouts, retry=FAST_RETRY),
        )
        with pytest.raises(ModelAttemptTimeout):
            asyncio.run(consume(response))
        assert server.requests == 1


def test_run_deadline_caps_model_wait() -> None:
    with ScriptedHttpServer("stall", "openai") as server:
        control = RunControl(deadline=RunDeadline(seconds=2.0))
        timeouts = ModelTimeouts(
            http_connect_seconds=2,
            http_pool_seconds=1,
            http_write_seconds=2,
            http_read_seconds=5,
            first_increment_seconds=5,
            increment_interval_seconds=5,
            attempt_seconds=10,
        )
        response = stream(
            spec("openai", server.base_url),
            context(),
            options(control=control, timeouts=timeouts, retry=FAST_RETRY),
        )
        with pytest.raises(RunDeadlineExceeded):
            asyncio.run(consume(response))
        assert server.requests == 1
        assert control.stop.reason == "run_timeout"


def test_http_read_timeout_at_progress_boundary_maps_to_model_timeout() -> None:
    # 生产默认读取超时与首增量期限同为 60s；读取超时赢得竞态时仍按 model_timeout 裁决。
    timeouts = ModelTimeouts(
        http_connect_seconds=2,
        http_pool_seconds=1,
        http_write_seconds=2,
        http_read_seconds=0.5,
        first_increment_seconds=0.2,
        increment_interval_seconds=0.2,
        attempt_seconds=5,
    )
    with ScriptedHttpServer("stall", "openai") as server:
        control = RunControl(deadline=RunDeadline(seconds=5))
        response = stream(
            spec("openai", server.base_url),
            context(),
            options(
                control=control,
                timeouts=timeouts,
                retry=FAST_RETRY,
            ),
        )
        with pytest.raises(ModelIncrementTimeout):
            asyncio.run(consume(response))
        assert server.requests == 1
        assert control.stop.reason == "model_timeout"


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_retry_before_public_start_success(protocol: str) -> None:
    with ScriptedHttpServer("fail_then_success", protocol) as server:
        budget = RequestBudget()
        response = stream(
            spec(protocol, server.base_url),
            context(),
            options(budget=budget, timeouts=FAST_TIMEOUTS, retry=FAST_RETRY),
        )
        message = asyncio.run(consume(response))
        assert message.stop_reason == "stop" and message.usage is not None
        assert server.requests == 2
        assert budget.used == 2 and budget.recovery_used == 1


def test_public_start_closes_retry_window_without_text() -> None:
    with ScriptedHttpServer("start_then_close", "openai") as server:
        budget = RequestBudget()
        response = stream(
            spec("openai", server.base_url),
            context(),
            options(budget=budget, timeouts=FAST_TIMEOUTS, retry=FAST_RETRY),
        )
        seen: list[str] = []

        async def run() -> None:
            async for event in response:
                seen.append(event["type"])

        with pytest.raises(Exception):
            asyncio.run(run())
        assert seen == ["start"]
        assert server.requests == 1
        assert budget.used == 1 and budget.recovery_used == 0


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_public_start_truncation_is_not_retried(protocol: str) -> None:
    # 公开 start 后以可重试传输错误结束；若重试窗口未关闭，会发出第二次请求。
    with ScriptedHttpServer("start_then_truncate", protocol) as server:
        budget = RequestBudget()
        response = stream(
            spec(protocol, server.base_url),
            context(),
            options(budget=budget, timeouts=FAST_TIMEOUTS, retry=FAST_RETRY),
        )
        seen: list[str] = []

        async def run() -> None:
            async for event in response:
                seen.append(event["type"])

        with pytest.raises(BaseException):
            asyncio.run(run())
        assert seen == ["start"]
        assert server.requests == 1
        assert budget.used == 1 and budget.recovery_used == 0


def test_retry_after_is_minimum_wait() -> None:
    with ScriptedHttpServer("rate_429", "openai") as server:
        # 第二次请求仍返回 429；用两次等待验证 Retry-After 作为下限而非被全抖动覆盖。
        start = time.monotonic()
        response = stream(
            spec("openai", server.base_url),
            context(),
            options(timeouts=FAST_TIMEOUTS, retry=RetryPolicy(backoff_caps=(0.0, 0.0), max_wait_seconds=1)),
        )
        with pytest.raises(Exception):
            asyncio.run(consume(response))
        elapsed = time.monotonic() - start
        assert server.requests == 3
        assert elapsed >= 0.2


def test_auth_error_not_retried() -> None:
    import openai

    with ScriptedHttpServer("auth_401", "openai") as server:
        response = stream(
            spec("openai", server.base_url),
            context(),
            options(timeouts=FAST_TIMEOUTS, retry=FAST_RETRY),
        )
        with pytest.raises(openai.AuthenticationError):
            asyncio.run(consume(response))
        assert server.requests == 1


def test_permission_denied_not_retried() -> None:
    import openai

    with ScriptedHttpServer("permission_403", "openai") as server:
        response = stream(
            spec("openai", server.base_url),
            context(),
            options(timeouts=FAST_TIMEOUTS, retry=FAST_RETRY),
        )
        with pytest.raises(openai.PermissionDeniedError):
            asyncio.run(consume(response))
        assert server.requests == 1


def test_budget_exhaustion_prevents_send() -> None:
    with ScriptedHttpServer("success", "openai") as server:
        budget = RequestBudget(limit=1)
        budget.reserve()
        response = stream(
            spec("openai", server.base_url),
            context(),
            options(budget=budget, timeouts=FAST_TIMEOUTS, retry=FAST_RETRY),
        )
        with pytest.raises(ModelRequestLimitExceeded):
            asyncio.run(consume(response))
        assert server.requests == 0
        assert budget.used == 1


def test_recovery_budget_exhaustion_prevents_retry() -> None:
    with ScriptedHttpServer("fail_then_success", "openai") as server:
        budget = RequestBudget(limit=10, recovery_limit=0)
        response = stream(
            spec("openai", server.base_url),
            context(),
            options(budget=budget, timeouts=FAST_TIMEOUTS, retry=FAST_RETRY),
        )
        with pytest.raises(RecoveryBudgetExhausted):
            asyncio.run(consume(response))
        assert server.requests == 1
        assert budget.used == 1


def test_failed_attempt_observation_preserved(tmp_path) -> None:
    observer = CacheObserver(
        session_id=str(uuid4()), run_id=str(uuid4()), directory=tmp_path
    )
    with ScriptedHttpServer("fail_then_success", "openai") as server:
        response = stream(
            spec("openai", server.base_url),
            context(),
            options(
                timeouts=FAST_TIMEOUTS,
                retry=FAST_RETRY,
                observation_factory=lambda: observer.start_call(NORMAL_CALL),
            ),
        )
        asyncio.run(consume(response))
    records = read_observations(tmp_path)
    assert [record.call_index for record in records] == [1, 2]
    assert records[0].status == "failed" and records[0].input_tokens is None
    assert records[1].status == "stop" and records[1].input_tokens == 15


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_increment_interval_timeout(protocol: str) -> None:
    with ScriptedHttpServer("delta_then_stall", protocol) as server:
        response = stream(
            spec(protocol, server.base_url),
            context(),
            options(timeouts=FAST_TIMEOUTS, retry=FAST_RETRY),
        )
        with pytest.raises(ModelIncrementTimeout):
            asyncio.run(consume(response))
        assert server.requests == 1


def test_quota_429_not_retried() -> None:
    from app.ai.quota import ProviderQuotaExceeded

    with ScriptedHttpServer("quota_429", "openai") as server:
        response = stream(
            spec("openai", server.base_url),
            context(),
            options(timeouts=FAST_TIMEOUTS, retry=FAST_RETRY),
        )
        with pytest.raises(ProviderQuotaExceeded):
            asyncio.run(consume(response))
        assert server.requests == 1


def test_backoff_cancel_stops_retry() -> None:
    signal = Event()
    with ScriptedHttpServer("transient_500_wait", "openai") as server:
        response = stream(
            spec("openai", server.base_url),
            context(),
            {
                **options(
                    timeouts=FAST_TIMEOUTS,
                    # Retry-After 固定为 5s，取消发生在等待期间，避免全抖动过短导致第二次请求。
                    retry=RetryPolicy(backoff_caps=(0.0,), max_wait_seconds=10),
                ),
                "signal": signal,
            },
        )

        def cancel_during_backoff() -> None:
            server.started.wait(2)
            time.sleep(0.1)
            signal.set()

        thread = Thread(target=cancel_during_backoff, daemon=True)
        thread.start()
        with pytest.raises(BaseException) as info:
            asyncio.run(consume(response))
        thread.join(1)
        assert type(info.value).__name__ == "CancelledError"
        assert server.requests == 1


def test_partial_tool_arguments_do_not_reach_final_message() -> None:
    with ScriptedHttpServer("partial_tool", "openai") as server:
        response = stream(
            spec("openai", server.base_url),
            context(),
            options(timeouts=FAST_TIMEOUTS, retry=FAST_RETRY),
        )
        seen: list[str] = []

        async def run() -> None:
            async for event in response:
                seen.append(event["type"])

        with pytest.raises(Exception):
            asyncio.run(run())
        assert server.requests == 1
        assert "toolcall_delta" in seen
        assert "toolcall_end" not in seen and "done" not in seen


def test_normal_success_does_not_consume_recovery() -> None:
    with ScriptedHttpServer("success", "openai") as server:
        budget = RequestBudget()
        response = stream(
            spec("openai", server.base_url),
            context(),
            options(budget=budget, timeouts=FAST_TIMEOUTS, retry=FAST_RETRY),
        )
        asyncio.run(consume(response))
        assert budget.used == 1 and budget.recovery_used == 0


def test_recovery_resend_consumes_recovery_budget() -> None:
    with ScriptedHttpServer("success", "openai") as server:
        budget = RequestBudget()
        response = stream(
            spec("openai", server.base_url),
            context(),
            options(
                budget=budget,
                timeouts=FAST_TIMEOUTS,
                retry=FAST_RETRY,
                recovery=True,
            ),
        )
        asyncio.run(consume(response))
        assert budget.used == 1 and budget.recovery_used == 1


def test_threshold_summary_does_not_consume_recovery() -> None:
    with ScriptedHttpServer("success", "openai") as server:
        budget = RequestBudget()
        from app.agent.compaction import CompactionPreparation

        preparation = CompactionPreparation(
            first_kept_entry_id="kept",
            messages_to_summarize=[UserMessage(role="user", content="历史" * 50, timestamp=0)],
            turn_prefix_messages=[],
            is_split_turn=False,
            tokens_before=0,
            previous_summary=None,
            attachments=[],
        )
        with compaction_scope("threshold"):
            asyncio.run(
                generate_compaction_summary(
                    preparation,
                    spec("openai", server.base_url),
                    api_key="test-key",
                    budget=budget,
                )
            )
        assert budget.used == 1 and budget.recovery_used == 0


@pytest.mark.parametrize("reason", ["threshold", "overflow"])
def test_summary_uses_run_deadline(reason: str) -> None:
    with ScriptedHttpServer("stall", "openai") as server:
        from app.agent.compaction import CompactionPreparation

        control = RunControl(deadline=RunDeadline(seconds=1.5))
        preparation = CompactionPreparation(
            first_kept_entry_id="kept",
            messages_to_summarize=[UserMessage(role="user", content="历史" * 50, timestamp=0)],
            turn_prefix_messages=[],
            is_split_turn=False,
            tokens_before=0,
            previous_summary=None,
            attachments=[],
        )
        with compaction_scope(reason):
            with pytest.raises(RunDeadlineExceeded):
                asyncio.run(
                    generate_compaction_summary(
                        preparation,
                        spec("openai", server.base_url),
                        api_key="test-key",
                        budget=RequestBudget(),
                        deadline=control.deadline,
                        run_control=control,
                    )
                )
        assert server.requests == 1
        assert control.stop.reason == "run_timeout"


@pytest.mark.parametrize("reason", ["threshold", "overflow"])
def test_normal_and_summary_share_run_deadline(reason: str) -> None:
    # 同一 RunDeadline 实例先约束普通请求，再约束摘要：截止已耗尽时摘要不得再发送。
    deadline = RunDeadline(seconds=2.0)
    control = RunControl(deadline=deadline)
    with ScriptedHttpServer("stall", "openai") as server:
        timeouts = ModelTimeouts(
            http_connect_seconds=2,
            http_pool_seconds=1,
            http_write_seconds=2,
            http_read_seconds=5,
            first_increment_seconds=5,
            increment_interval_seconds=5,
            attempt_seconds=10,
        )
        response = stream(
            spec("openai", server.base_url),
            context(),
            options(control=control, timeouts=timeouts, retry=FAST_RETRY),
        )
        with pytest.raises(RunDeadlineExceeded):
            asyncio.run(consume(response))
        assert server.requests == 1

        from app.agent.compaction import CompactionPreparation

        preparation = CompactionPreparation(
            first_kept_entry_id="kept",
            messages_to_summarize=[UserMessage(role="user", content="历史" * 50, timestamp=0)],
            turn_prefix_messages=[],
            is_split_turn=False,
            tokens_before=0,
            previous_summary=None,
            attachments=[],
        )
        with compaction_scope(reason):
            with pytest.raises(RunDeadlineExceeded):
                asyncio.run(
                    generate_compaction_summary(
                        preparation,
                        spec("openai", server.base_url),
                        api_key="test-key",
                        budget=RequestBudget(),
                        deadline=deadline,
                        run_control=control,
                    )
                )
        assert server.requests == 1


def test_early_close_releases_source() -> None:
    with ScriptedHttpServer("stall", "openai") as server:
        response = stream(
            spec("openai", server.base_url),
            context(),
            options(timeouts=ModelTimeouts(
                http_connect_seconds=2,
                http_pool_seconds=1,
                http_write_seconds=2,
                http_read_seconds=5,
                first_increment_seconds=5,
                increment_interval_seconds=5,
                attempt_seconds=10,
            ), retry=FAST_RETRY),
        )

        async def run() -> None:
            iterator = response.__aiter__()
            event = await iterator.__anext__()
            assert event["type"] == "start"
            await response.aclose()

        asyncio.run(run())
        assert response._source.ag_frame is None
        assert server.requests == 1


def test_recovery_summary_consumes_recovery_budget() -> None:
    with ScriptedHttpServer("success", "openai") as server:
        budget = RequestBudget()
        from app.agent.compaction import (
            CompactionPreparation,
            generate_compaction_summary,
        )

        preparation = CompactionPreparation(
            first_kept_entry_id="kept",
            messages_to_summarize=[UserMessage(role="user", content="历史" * 50, timestamp=0)],
            turn_prefix_messages=[],
            is_split_turn=False,
            tokens_before=0,
            previous_summary=None,
            attachments=[],
        )
        with compaction_scope("overflow"):
            asyncio.run(
                generate_compaction_summary(
                    preparation,
                    spec("openai", server.base_url),
                    api_key="test-key",
                    budget=budget,
                    run_control=RunControl(deadline=RunDeadline(seconds=5)),
                )
            )
        assert server.requests == 1
        assert budget.used == 1 and budget.recovery_used == 1


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
@pytest.mark.parametrize("reason", ["threshold", "overflow"])
def test_summary_truncation_recovers(protocol: str, reason: str) -> None:
    from app.agent.compaction import CompactionPreparation

    with ScriptedHttpServer("truncate_then_success", protocol) as server:
        budget = RequestBudget()
        control = RunControl(deadline=RunDeadline(seconds=10))
        preparation = CompactionPreparation(
            first_kept_entry_id="kept",
            messages_to_summarize=[
                UserMessage(role="user", content="历史" * 50, timestamp=0)
            ],
            turn_prefix_messages=[],
            is_split_turn=False,
            tokens_before=0,
            previous_summary=None,
            attachments=[],
        )
        with compaction_scope(reason):
            result = asyncio.run(
                generate_compaction_summary(
                    preparation,
                    spec(protocol, server.base_url),
                    api_key="test-key",
                    budget=budget,
                    deadline=control.deadline,
                    run_control=control,
                )
            )
        assert result.summary == "完成"
        assert server.requests == 2
        assert budget.used == 2
        assert budget.recovery_used == (2 if reason == "overflow" else 1)


class EchoArguments(BaseModel):
    text: str


def test_sync_tool_timeout_keeps_real_exit_evidence() -> None:
    release = Event()
    started: list[str] = []

    def slow(tool_call_id, params, signal, on_update):
        started.append(params.text)
        release.wait(5)
        return AgentToolResult([TextContent(type="text", text="released")])

    tool = AgentTool("slow", "slow", EchoArguments, slow, execution_mode="sequential")
    tracker = ToolExecutionTracker()
    control = RunControl(deadline=RunDeadline(seconds=5))

    async def scenario() -> None:
        result = await run_tool_batch(
            [
                ToolCall(type="toolCall", id="c-1", name="slow", arguments={"text": "one"}),
                ToolCall(type="toolCall", id="c-2", name="slow", arguments={"text": "two"}),
            ],
            tools={"slow": tool},
            declared={"slow": tool.definition()},
            execution_mode="sequential",
            tracker=tracker,
            run_control=control,
            timeout=0.15,
        )
        assert isinstance(result.failure, ToolExecutionTimeout)
        assert started == ["one"]
        assert len(result.inflight) == 1 and not result.inflight[0].finished
        assert control.stop.reason == "tool_timeout"
        release.set()
        # 等待真实线程退出；句柄只在真实返回后完成。
        for _ in range(100):
            if tracker.wait_all(0.05):
                break
        assert tracker.pending() == ()

    asyncio.run(scenario())


def test_async_tool_timeout_and_real_exit() -> None:
    started: list[str] = []

    async def slow(tool_call_id, params, signal, on_update):
        started.append(params.text)
        await asyncio.sleep(0.4)
        return AgentToolResult([TextContent(type="text", text="done")])

    tool = AgentTool("slow", "slow", EchoArguments, slow, execution_mode="sequential")
    tracker = ToolExecutionTracker()

    async def scenario() -> None:
        result = await run_tool_batch(
            [ToolCall(type="toolCall", id="c-1", name="slow", arguments={"text": "one"})],
            tools={"slow": tool},
            declared={"slow": tool.definition()},
            execution_mode="sequential",
            tracker=tracker,
            timeout=0.1,
        )
        assert isinstance(result.failure, ToolExecutionTimeout)
        assert started == ["one"]
        assert len(result.inflight) == 1 and not result.inflight[0].finished
        await asyncio.sleep(0.5)
        assert tracker.pending() == ()

    asyncio.run(scenario())


def test_cooperative_cancel_waits_for_real_exit() -> None:
    started = Event()

    def cooperative(tool_call_id, params, signal, on_update):
        started.set()
        assert signal.wait(2)
        return AgentToolResult([TextContent(type="text", text="stopped")])

    tool = AgentTool("coop", "coop", EchoArguments, cooperative)
    signal = Event()
    threaded = Thread(target=lambda: (started.wait(2), signal.set()), daemon=True)
    threaded.start()

    async def scenario() -> None:
        result = await run_tool_batch(
            [ToolCall(type="toolCall", id="c-1", name="coop", arguments={"text": "x"})],
            tools={"coop": tool},
            declared={"coop": tool.definition()},
            signal=signal,
            timeout=5,
        )
        assert result.failure is None
        assert result.messages and result.messages[0].tool_call_id == "c-1"

    asyncio.run(scenario())
    threaded.join(1)


def test_tool_deadline_prevents_start() -> None:
    executed: list[str] = []

    def tool_fn(tool_call_id, params, signal, on_update):
        executed.append(params.text)
        return AgentToolResult([TextContent(type="text", text="ok")])

    tool = AgentTool("t", "t", EchoArguments, tool_fn)
    deadline = RunDeadline(seconds=0.01)
    time.sleep(0.03)

    async def scenario() -> None:
        result = await run_tool_batch(
            [ToolCall(type="toolCall", id="c-1", name="t", arguments={"text": "x"})],
            tools={"t": tool},
            declared={"t": tool.definition()},
            deadline=deadline,
        )
        assert isinstance(result.failure, ToolExecutionTimeout)
        assert executed == []

    asyncio.run(scenario())


def test_parallel_timeout_then_credential_prefers_credential() -> None:
    release = Event()
    started: list[str] = []

    def slow(tool_call_id, params, signal, on_update):
        started.append(params.text)
        release.wait(5)
        return AgentToolResult([TextContent(type="text", text="slow done")])

    async def leak(tool_call_id, params, signal, on_update):
        started.append(params.text)
        await asyncio.sleep(0.25)
        return AgentToolResult([TextContent(type="text", text="SECRET token")])

    slow_tool = AgentTool("slow", "slow", EchoArguments, slow)
    leak_tool = AgentTool("leak", "leak", EchoArguments, leak)
    tools = {"slow": slow_tool, "leak": leak_tool}
    declared = {name: tool.definition() for name, tool in tools.items()}
    signal = Event()
    tracker = ToolExecutionTracker()

    async def scenario() -> None:
        result = await run_tool_batch(
            [
                ToolCall(type="toolCall", id="c-1", name="slow", arguments={"text": "slow"}),
                ToolCall(type="toolCall", id="c-2", name="leak", arguments={"text": "leak"}),
            ],
            tools=tools,
            declared=declared,
            execution_mode="parallel",
            signal=signal,
            tracker=tracker,
            timeout=0.15,
            contains_credentials=lambda text: "SECRET" in text,
        )
        assert isinstance(result.failure, CredentialDetectedError)
        assert set(started) == {"slow", "leak"}
        release.set()
        for _ in range(100):
            if tracker.wait_all(0.05):
                break
        assert tracker.pending() == ()

    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_agent_loop_tool_timeout_stops_next_tool() -> None:
    started: list[str] = []

    def slow(tool_call_id, params, signal, on_update):
        started.append(params.text)
        signal.wait(5)
        return AgentToolResult([TextContent(type="text", text="done")])

    tool = AgentTool("slow", "slow", EchoArguments, slow, execution_mode="sequential")
    signal = Event()
    control = RunControl(deadline=RunDeadline(seconds=5), cancel=signal)
    tracker = ToolExecutionTracker()

    with ScriptedHttpServer("tool_calls", "openai") as server:
        config = AgentLoopConfig(
            model=ModelConfig(
                api="openai-completions",
                base_url=server.base_url,
                model="test",
                provider="local",
                api_key="test-key",
            ),
            model_spec=spec("openai", server.base_url),
            max_turns=4,
            request_budget=RequestBudget(),
            run_control=control,
            tool_tracker=tracker,
            tool_timeout_seconds=0.15,
        )
        system = SystemMessage(
            role="system", content="", tools_added=[tool.definition()], timestamp=0
        )

        async def emit(_event) -> None:
            return None

        async def scenario() -> None:
            with pytest.raises(ToolExecutionTimeout):
                await run_agent_loop(
                    [UserMessage(role="user", content="go", timestamp=0)],
                    {"messages": [system], "tools": {"slow": tool}},
                    config,
                    emit,
                    signal=signal,
                )
            assert started == ["one"]
            assert server.requests == 1
            assert control.stop.reason == "tool_timeout"
            for _ in range(100):
                if tracker.wait_all(0.05):
                    break
            assert tracker.pending() == ()

        asyncio.run(scenario())
