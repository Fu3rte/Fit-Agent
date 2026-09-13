"""S4-05b：Run 统一执行预算、墙钟、退避与失败分类（stage4.md S4-05 验收；08 8.5/8.6）。

覆盖：

1. 冻结参数与计数池：全部实际模型请求共用 20 次预算、工具 32、暂时故障重试池 1、纠错池 2
   （输出与工具参数共享）、退避默认 1 秒与 10 秒上限、取消／Run 到期／池尽都不启动新 effect。
2. 分类白名单：429（含／不含 ``Retry-After``）、500、503、连接失败在无输出时最多重发一次；
   400/401/402/404/422 与 ``finish_reason`` 永久结果不重试；已输出后的流中断不重放。
3. 墙钟映射：单次请求总时限（受 Run 剩余时间约束）→ ``model_request_timeout``；
   Run 总时限 → ``run_timeout``；永久失败与池尽 → ``model_request_failed``（2026-09-12 拍板新码）。

离线：本地回环假服务端（合成占位凭据）＋框架脚本桩模型，无真实 Provider 请求；
假服务端只监听 127.0.0.1，且每例都断言客户端端点仍在回环。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, replace
from datetime import date
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.exceptions import (
    ContentFilterError,
    ModelAPIError,
    ModelHTTPError,
    UnexpectedModelBehavior,
)
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.deepseek import DeepSeekProvider

from config import EffectiveHarness, HarnessConfig, effective_harness_config
from runtime.agent_factory import build_agent, build_run_work
from runtime.budget import (
    CORRECTION_POOL,
    PERMANENT_FINISH_REASONS,
    TRANSIENT_RETRY_POOL,
    BudgetedModel,
    RunBudget,
    classify_model_failure,
)
from runtime.error_codes import (
    MODEL_REQUEST_FAILED,
    MODEL_REQUEST_TIMEOUT,
    RUN_TIMEOUT,
)
from runtime.models import ModelSpec, resolve_model_profile
from runtime.provider import build_openai_client
from runtime.run_service import RunService
from runtime.run_task import ExecutionDriver, ExecutionFailure
from runtime.tools import BusinessTools, ToolIdentity
from storage.run_repo import RunRepo
from tests.support import open_database
from tests.test_stage3_plan_confirm import _formal_profile

#: 合成占位凭据：不是真实 Key，也不从环境读取（本文件绝不触碰 .env）。
_FAKE_KEY = "sk-synthetic-placeholder-for-offline-budget-check"
CONVERSATION_ID = "c1"
BUSINESS_DATE = date(2026, 9, 20)
_FINISH_REASON_ERRORS = (
    "length",
    "content_filter",
    "insufficient_system_resource",
    "aborted",
)


def _frozen_harness(**overrides: Any) -> EffectiveHarness:
    """已冻结的有效配置：默认取生产默认值，测试按需覆盖（直接构造即 S4-05a 已知旁路，仅测试用）。"""
    return replace(effective_harness_config(HarnessConfig()), **overrides)


class _Clock:
    """可控单调钟：墙钟断言不依赖真实等待（真实路径用 ``time.monotonic``）。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Sleeper:
    """退避等待替身：记录请求的等待时长，不真的睡（真实路径用 ``asyncio.sleep``）。"""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _guard(
    harness: EffectiveHarness | None = None,
    *,
    cancelled: Callable[[], bool] | None = None,
    clock: _Clock | None = None,
    sleeper: _Sleeper | None = None,
) -> RunBudget:
    return RunBudget(
        harness if harness is not None else _frozen_harness(),
        cancelled=cancelled if cancelled is not None else (lambda: False),
        clock=clock if clock is not None else _Clock(),
        sleep=sleeper if sleeper is not None else _Sleeper(),
    )


def _stub_tools() -> BusinessTools:
    """空工具面：本文件只测预算与分类，不执行工具（注册清单不依赖库）。"""
    return BusinessTools(None, ToolIdentity(CONVERSATION_ID, "run-1", BUSINESS_DATE))  # type: ignore[arg-type]


class _StubModel:
    """脚本桩模型：一次 ``respond`` 调用 = 一次实际模型请求（可睡眠、可按次序抛错）。"""

    def __init__(
        self,
        *,
        errors: list[BaseException] | None = None,
        delay: float = 0.0,
        text: str = "完成",
        finish_reason: str = "stop",
    ) -> None:
        self.errors = list(errors or [])
        self.delay = delay
        self.text = text
        self.finish_reason = finish_reason
        self.attempts = 0

    def model(self) -> FunctionModel:
        async def respond(
            messages: list[ModelMessage], info: AgentInfo
        ) -> ModelResponse:
            step = self.attempts
            self.attempts += 1
            if self.delay:
                await asyncio.sleep(self.delay)
            if step < len(self.errors):
                raise self.errors[step]
            return ModelResponse(
                parts=[TextPart(self.text)],
                finish_reason=self.finish_reason,  # type: ignore[arg-type]
            )

        return FunctionModel(respond)


def _managed_agent(model: FunctionModel, budget: RunBudget) -> Agent[None, str]:
    return build_agent(BudgetedModel(model, budget), _stub_tools())


# ---------- 假服务端（回环，仅用于真实 SDK／框架路径的 HTTP 分类） ----------


@dataclass(frozen=True)
class _Reply:
    """一条脚本化响应；带 ``error`` 时作为 HTTP 错误体，带 ``chunks`` 时作为 SSE 流返回。"""

    status: int = 200
    error: str | None = None
    headers: tuple[tuple[str, str], ...] = ()
    chunks: tuple[str, ...] | None = None


def _chunk(delta: dict[str, Any], finish_reason: str | None = None) -> str:
    """一条 OpenAI 兼容 chat completion chunk（SSE ``data:`` 行）。"""
    event = {
        "id": "chatcmpl-offline",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "deepseek-flash",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(event)}\n\n"


def _sse_events(*events: str) -> tuple[str, ...]:
    """拼成一条完整 SSE 响应体（事件 + ``[DONE]``）。"""
    return (*events, "data: [DONE]\n\n")


def _completion(text: str = "完成") -> dict[str, Any]:
    return {
        "id": "chatcmpl-offline",
        "object": "chat.completion",
        "created": 0,
        "model": "deepseek-flash",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


class _ScriptedHandler(BaseHTTPRequestHandler):
    """按次序回脚本响应并计数：计数即「实际发送次数」（SDK ``max_retries=0``）。"""

    def __init__(
        self, *args: object, replies: list[_Reply], seen: list[str], **kwargs: object
    ) -> None:
        self._replies = replies
        self._seen = seen
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 接口名
        # 先读完请求体再应答：否则 Windows 上服务端带未读数据关连接会 RST，
        # 客户端在读响应体时偶发 ConnectionAborted，把 429+Retry-After 降级成连接错误（退避默认 1s）。
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        index = len(self._seen)
        self._seen.append(self.path)
        reply = self._replies[min(index, len(self._replies) - 1)]
        if reply.chunks is not None:
            payload = "".join(reply.chunks).encode("utf-8")
            self.send_response(reply.status)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        body: dict[str, Any] = (
            _completion()
            if reply.error is None
            else {"error": {"message": reply.error}}
        )
        payload = json.dumps(body).encode("utf-8")
        self.send_response(reply.status)
        self.send_header("Content-Type", "application/json")
        for name, value in reply.headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - 基类接口名
        return  # 静默：绝不打印请求内容或头


@contextlib.contextmanager
def _fake_server(replies: list[_Reply]) -> Iterator[tuple[str, list[str]]]:
    """回环假服务端；返回 ``(base_url, 实际收到的请求路径列表)``。"""
    seen: list[str] = []
    handler = partial(_ScriptedHandler, replies=replies, seen=seen)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = str(server.server_address[0]), int(server.server_address[1])
    try:
        yield f"http://{host}:{port}/v1", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _openai_agent(
    base_url: str, budget: RunBudget
) -> tuple[Agent[None, str], AsyncOpenAI]:
    """生产接缝组装：``build_openai_client``（``max_retries=0``）+ DeepSeek provider + 预算包装。"""
    spec = ModelSpec(
        model_id="deepseek-flash",
        provider="deepseek",
        base_url=base_url,
        context_window=1_000_000,
        max_output_tokens=384_000,
    )
    client = build_openai_client(_FAKE_KEY, spec=spec, timeout_seconds=5.0)
    hostname = urlsplit(str(client.base_url)).hostname
    assert hostname in {"127.0.0.1", "localhost", "::1"}, hostname
    assert client.max_retries == 0
    model = OpenAIChatModel(
        "deepseek-flash",
        provider=DeepSeekProvider(openai_client=client),
        profile=resolve_model_profile("deepseek-flash"),
    )
    return build_agent(BudgetedModel(model, budget), _stub_tools()), client


async def _run_openai_agent(base_url: str, budget: RunBudget) -> None:
    agent, client = _openai_agent(base_url, budget)
    try:
        result = await agent.run("ping")
        assert result.output
    finally:
        await client.close()


async def _expect_failure(awaitable: Any, error_code: str) -> ExecutionFailure:
    """断言底层以指定错误码终态失败（框架包装的任何异常组在这里统一解开）。"""
    with pytest.raises(Exception) as excinfo:
        await awaitable
    failure = _unwrap_failure(excinfo.value)
    assert failure is not None, f"未按执行失败上报: {excinfo.value!r}"
    assert failure.error_code == error_code
    return failure


def _unwrap_failure(exc: BaseException) -> ExecutionFailure | None:
    if isinstance(exc, ExecutionFailure):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            found = _unwrap_failure(inner)
            if found is not None:
                return found
    return None


# ---------- 1. 冻结参数、计数池与墙钟（纯预算，无网络） ----------


def test_frozen_defaults_are_the_approved_caps() -> None:
    """已拍默认值逐项：全部模型请求共用 20、工具 32、重试池 1、纠错池 2、120s/300s。"""
    budget = _guard()
    assert budget.harness.max_model_requests == 20
    assert budget.harness.max_tool_calls == 32
    assert TRANSIENT_RETRY_POOL == 1
    assert CORRECTION_POOL == 2
    assert budget.harness.run_timeout_seconds == 300.0
    assert budget.harness.request_timeout_seconds == 120.0
    assert (
        budget.begin_model_request() == 120.0
    )  # 单次请求时限受 Run 剩余时间约束（此处不裁剪）


def test_request_pool_is_shared_and_capped_at_20() -> None:
    budget = _guard()
    for _ in range(20):
        budget.begin_model_request()
    assert budget.requests_used == 20
    with pytest.raises(ExecutionFailure) as excinfo:
        budget.begin_model_request()
    assert excinfo.value.error_code == MODEL_REQUEST_FAILED


def test_tool_pool_is_capped_at_32() -> None:
    budget = _guard()
    for _ in range(32):
        budget.begin_tool_call()
    assert budget.tool_calls_used == 32
    with pytest.raises(ExecutionFailure) as excinfo:
        budget.begin_tool_call()
    assert excinfo.value.error_code == MODEL_REQUEST_FAILED


def test_retry_and_correction_pools_are_independent_and_bounded() -> None:
    budget = _guard()
    assert budget.begin_retry() is True  # 重试池 1 次
    assert budget.begin_retry() is False
    assert budget.retries_used == 1
    budget.begin_correction()  # 纠错池 2 次（输出与工具参数共享）
    budget.begin_correction()
    assert budget.corrections_used == 2
    with pytest.raises(ExecutionFailure) as excinfo:
        budget.begin_correction()
    assert excinfo.value.error_code == MODEL_REQUEST_FAILED


def test_run_wall_clock_expiry_starts_no_new_effect() -> None:
    """Run 总时限到期：新的模型请求／工具调用／重试／纠错都不启动（``run_timeout``）。"""
    clock = _Clock()
    budget = _guard(clock=clock)
    clock.now = budget.harness.run_timeout_seconds + 1
    for effect in (
        budget.begin_model_request,
        budget.begin_tool_call,
        budget.begin_correction,
    ):
        with pytest.raises(ExecutionFailure) as excinfo:
            effect()
        assert excinfo.value.error_code == RUN_TIMEOUT
    with pytest.raises(ExecutionFailure) as excinfo:
        budget.begin_retry()
    assert excinfo.value.error_code == RUN_TIMEOUT


async def test_cancelled_run_starts_no_new_effect() -> None:
    budget = _guard(cancelled=lambda: True)
    for effect in (
        budget.begin_model_request,
        budget.begin_tool_call,
        budget.begin_correction,
        budget.begin_retry,
    ):
        with pytest.raises(asyncio.CancelledError):
            effect()
    assert budget.requests_used == 0 and budget.tool_calls_used == 0


async def test_backoff_defaults_cap_and_insufficient_run_time() -> None:
    sleeper = _Sleeper()
    clock = _Clock()
    budget = _guard(clock=clock, sleeper=sleeper)
    assert budget.retry_delay(None) == 1.0  # 无 Retry-After 用默认 1 秒
    assert budget.retry_delay(-1.0) == 1.0  # 非法值退化为默认
    assert budget.retry_delay(0.0) == 0.0
    assert budget.retry_delay(10.0) == 10.0  # 10 秒仍接受
    assert budget.retry_delay(10.5) is None  # 超过 10 秒：结束，不提前重发
    await budget.wait_before_retry(4.0)
    assert sleeper.delays == [4.0]
    # Run 剩余时间不够等待：不睡、以单次请求超时结束（08「Run 剩余时间不足」）
    clock.now = budget.harness.run_timeout_seconds - 0.5
    with pytest.raises(ExecutionFailure) as excinfo:
        await budget.wait_before_retry(4.0)
    assert excinfo.value.error_code == MODEL_REQUEST_TIMEOUT
    assert sleeper.delays == [4.0]


# ---------- 2. 分类表（只按异常类型／HTTP 状态／finish_reason／是否已输出） ----------


@pytest.mark.parametrize("status", [429, 500, 503])
def test_whitelisted_statuses_are_retryable_only_before_output(status: int) -> None:
    decision = classify_model_failure(
        ModelHTTPError(status, "deepseek-flash"), output_emitted=False
    )
    assert decision is not None and decision.retryable is True
    after_output = classify_model_failure(
        ModelHTTPError(status, "deepseek-flash"), output_emitted=True
    )
    assert after_output is not None and after_output.retryable is False
    assert after_output.error_code == MODEL_REQUEST_FAILED


@pytest.mark.parametrize("status", [400, 401, 402, 404, 422])
def test_permanent_statuses_are_never_retryable(status: int) -> None:
    decision = classify_model_failure(
        ModelHTTPError(status, "deepseek-flash"), output_emitted=False
    )
    assert decision is not None
    assert decision.retryable is False
    assert decision.error_code == MODEL_REQUEST_FAILED


def test_retry_after_header_is_surfaced_for_the_backoff() -> None:
    decision = classify_model_failure(
        ModelHTTPError(429, "deepseek-flash", headers={"Retry-After": "7"}),
        output_emitted=False,
    )
    assert decision is not None and decision.retry_after == 7.0


def test_connection_class_and_transport_errors_follow_the_whitelist() -> None:
    connection = classify_model_failure(
        ModelAPIError(model_name="deepseek-flash", message="connection failed"),
        output_emitted=False,
    )
    assert connection is not None and connection.retryable is True
    connection_after_output = classify_model_failure(
        ModelAPIError(model_name="deepseek-flash", message="connection failed"),
        output_emitted=True,
    )
    assert connection_after_output is not None
    assert connection_after_output.retryable is False
    refused = classify_model_failure(
        httpx2.ConnectError("refused"), output_emitted=False
    )
    assert refused is not None and refused.retryable is True
    # body 阶段断流：已输出不可重放；未输出也不在白名单内（08 白名单封闭）
    read_timeout = classify_model_failure(
        httpx2.ReadTimeout("stalled"), output_emitted=False
    )
    assert read_timeout is not None and read_timeout.error_code == MODEL_REQUEST_TIMEOUT
    stalled_after_output = classify_model_failure(
        httpx2.ReadTimeout("stalled"), output_emitted=True
    )
    assert stalled_after_output is not None and stalled_after_output.retryable is False


def test_timeout_class_and_framework_failures() -> None:
    timeout = classify_model_failure(
        TimeoutError("request clock"), output_emitted=False
    )
    assert timeout is not None and timeout.retryable is True
    assert timeout.error_code == MODEL_REQUEST_TIMEOUT
    for exc in (
        ContentFilterError("filtered"),
        UnexpectedModelBehavior("correction budget exhausted"),
    ):
        decision = classify_model_failure(exc, output_emitted=False)
        assert decision is not None and decision.retryable is False
        assert decision.error_code == MODEL_REQUEST_FAILED


def test_unknown_exception_is_not_classified() -> None:
    """未分类异常不发明原因码（S4-03 语义）：向上抛，Run 不因此被改写终态。"""
    assert classify_model_failure(RuntimeError("bug"), output_emitted=False) is None


async def test_permanent_finish_reasons_fail_without_retry() -> None:
    """四种已拍永久 ``finish_reason``：一次请求即终态，不重试、不静默接受无效结果。"""
    for finish_reason in _FINISH_REASON_ERRORS:
        model = _StubModel(finish_reason=finish_reason)
        budget = _guard()
        failure = await _expect_failure(
            _managed_agent(model.model(), budget).run("ping"), MODEL_REQUEST_FAILED
        )
        assert failure.error_code == MODEL_REQUEST_FAILED
        assert model.attempts == 1, finish_reason
        assert budget.retries_used == 0, finish_reason


# ---------- 3. 适配层重试与墙钟（桩模型路径） ----------


async def test_connection_failure_retries_once_then_fails_with_permanent_code() -> None:
    sleeper = _Sleeper()
    model = _StubModel(
        errors=[
            ModelAPIError(model_name="deepseek-flash", message="connection failed"),
            ModelAPIError(model_name="deepseek-flash", message="connection failed"),
        ]
    )
    budget = _guard(sleeper=sleeper)
    await _expect_failure(
        _managed_agent(model.model(), budget).run("ping"), MODEL_REQUEST_FAILED
    )
    assert model.attempts == 2  # 白名单内：恰好重发一次
    assert budget.requests_used == 2
    assert budget.retries_used == 1
    assert sleeper.delays == [1.0]  # 无 Retry-After：默认 1 秒退避
    assert budget.begin_retry() is False  # 重试池已用尽


async def test_permanent_http_error_is_not_retried() -> None:
    model = _StubModel(errors=[ModelHTTPError(400, "deepseek-flash")])
    budget = _guard()
    await _expect_failure(
        _managed_agent(model.model(), budget).run("ping"), MODEL_REQUEST_FAILED
    )
    assert model.attempts == 1
    assert budget.requests_used == 1 and budget.retries_used == 0


async def test_request_clock_maps_to_model_request_timeout() -> None:
    """单次请求总时限由适配层实现：未产出输出→用重试池；重试用尽→``model_request_timeout``。"""
    sleeper = _Sleeper()
    model = _StubModel(delay=0.5)  # 远大于单次请求时限
    budget = _guard(
        _frozen_harness(request_timeout_seconds=0.05, run_timeout_seconds=30.0),
        sleeper=sleeper,
    )
    await _expect_failure(
        _managed_agent(model.model(), budget).run("ping"), MODEL_REQUEST_TIMEOUT
    )
    assert model.attempts == 2  # 未产出输出：先按重试池再试一次
    assert budget.requests_used == 2 and budget.retries_used == 1
    assert sleeper.delays == [1.0]
    await asyncio.sleep(0)  # 桩模型的第二次睡眠随任务取消一起结束，不留后台任务
    assert budget.remaining_run_seconds() > 0


# ---------- 4. 流式（首块前后断流） ----------


def _stream_agent(
    error: BaseException, *, chunks_before_error: int, budget: RunBudget
) -> tuple[Agent[None, str], list[int]]:
    attempts: list[int] = []

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("完成")])

    async def stream(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str]:
        attempts.append(len(messages))
        for _ in range(chunks_before_error):
            yield "部分"
        raise error

    model = FunctionModel(respond, stream_function=stream)
    return build_agent(BudgetedModel(model, budget), _stub_tools()), attempts


async def test_stream_failure_before_first_output_uses_the_retry_pool() -> None:
    budget = _guard(sleeper=_Sleeper())
    agent, attempts = _stream_agent(
        ModelAPIError(model_name="deepseek-flash", message="stream dropped"),
        chunks_before_error=0,
        budget=budget,
    )
    await _expect_failure(_drain_stream(agent), MODEL_REQUEST_FAILED)
    assert len(attempts) == 2  # 尚未产出任何事件：按重试池再试一次
    assert budget.retries_used == 1


async def test_stream_failure_after_first_output_is_not_replayed() -> None:
    budget = _guard(sleeper=_Sleeper())
    agent, attempts = _stream_agent(
        ModelAPIError(model_name="deepseek-flash", message="stream dropped"),
        chunks_before_error=1,
        budget=budget,
    )
    await _expect_failure(_drain_stream(agent), MODEL_REQUEST_FAILED)
    assert len(attempts) == 1  # 已输出：不重放
    assert budget.retries_used == 0


async def _drain_stream(agent: Agent[None, str]) -> None:
    async with agent.run_stream("ping") as result:
        async for _ in result.stream_text(delta=False):
            pass


async def _run_openai_stream(base_url: str, budget: RunBudget) -> None:
    """生产接缝的流式路径（真实 SDK + 框架）：消费整条 SSE 流直到终态。"""
    agent, client = _openai_agent(base_url, budget)
    try:
        await _drain_stream(agent)
    finally:
        await client.close()


async def _raw_stream_response(base_url: str) -> Any:
    """不经适配层的原始框架流式响应：用于钉住「框架丢原值」这一机制（复审 P1）。"""
    spec = ModelSpec(
        model_id="deepseek-flash",
        provider="deepseek",
        base_url=base_url,
        context_window=1_000_000,
        max_output_tokens=384_000,
    )
    client = build_openai_client(_FAKE_KEY, spec=spec, timeout_seconds=5.0)
    try:
        model = OpenAIChatModel(
            "deepseek-flash",
            provider=DeepSeekProvider(openai_client=client),
            profile=resolve_model_profile("deepseek-flash"),
        )
        messages: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart("ping")])]
        async with model.request_stream(
            messages, None, ModelRequestParameters()
        ) as stream:
            async for _ in stream:
                pass
            return stream.finish_reason, dict(stream.provider_details or {})
    finally:
        await client.close()


async def test_framework_finish_reason_mapping_drops_deepseek_raw_value() -> None:
    """机制证据（复审 P1）：框架只映射 OpenAI 五个 chat finish_reason，Provider 原值仅在
    ``provider_details`` 里；只看归一化值就会把 Provider 中断当正常结束接受。"""
    chunks = _sse_events(
        _chunk({"role": "assistant", "content": "部分"}),
        _chunk({}, "aborted"),
    )
    with _fake_server([_Reply(chunks=chunks)]) as (base_url, _seen):
        finish_reason, provider_details = await _raw_stream_response(base_url)
    assert finish_reason is None  # 框架映射表里没有 aborted
    assert (
        provider_details.get("finish_reason") == "aborted"
    )  # 原值仍在 → 适配层据此终态


@pytest.mark.parametrize(
    "provider_finish_reason", ["aborted", "insufficient_system_resource"]
)
def test_deepseek_only_finish_reasons_are_permanent(
    provider_finish_reason: str,
) -> None:
    """Provider 原值不在框架映射表里（归一化为 ``None``），必须仍归永久失败（复审 P1）。"""
    assert provider_finish_reason in PERMANENT_FINISH_REASONS
    assert provider_finish_reason not in {"stop", "length", "tool_call"}


async def test_streamed_provider_abort_ends_as_permanent_failure() -> None:
    """回环假服务端以 ``finish_reason=aborted`` 结束 SSE：不得当成正常完成接受（复审 P1）。"""
    chunks = _sse_events(
        _chunk({"role": "assistant", "content": "部分"}),
        _chunk({}, "aborted"),
    )
    with _fake_server([_Reply(chunks=chunks)]) as (base_url, seen):
        budget = _guard(sleeper=_Sleeper())
        await _expect_failure(
            _run_openai_stream(base_url, budget), MODEL_REQUEST_FAILED
        )
    assert len(seen) == 1  # 已产出输出：不重放
    assert budget.requests_used == 1 and budget.retries_used == 0


async def test_streamed_normal_completion_is_accepted() -> None:
    """对照组：``finish_reason=stop`` 的同一条 SSE 路径正常结束（证明上例不是空断言）。"""
    chunks = _sse_events(
        _chunk({"role": "assistant", "content": "完成"}),
        _chunk({}, "stop"),
    )
    with _fake_server([_Reply(chunks=chunks)]) as (base_url, seen):
        budget = _guard(sleeper=_Sleeper())
        await _run_openai_stream(base_url, budget)
    assert len(seen) == 1
    assert budget.requests_used == 1


async def test_streamed_deepseek_finish_reasons_end_as_permanent_failure() -> None:
    """DeepSeek 原值（aborted／insufficient_system_resource）经真实 SDK 流式路径均终态失败。"""
    for provider_finish_reason in ("aborted", "insufficient_system_resource"):
        chunks = _sse_events(
            _chunk({"role": "assistant", "content": "部分"}),
            _chunk({}, provider_finish_reason),
        )
        with _fake_server([_Reply(chunks=chunks)]) as (base_url, seen):
            budget = _guard(sleeper=_Sleeper())
            await _expect_failure(
                _run_openai_stream(base_url, budget), MODEL_REQUEST_FAILED
            )
        assert len(seen) == 1, provider_finish_reason
        assert budget.requests_used == 1, provider_finish_reason


# ---------- 5. 假服务端：429／500／503／永久状态／连接失败／Retry-After ----------


async def test_http_500_retries_once_then_terminal() -> None:
    with _fake_server(
        [_Reply(500, error="server error"), _Reply(500, error="server error")]
    ) as (
        base_url,
        seen,
    ):
        budget = _guard(sleeper=_Sleeper())
        await _expect_failure(_run_openai_agent(base_url, budget), MODEL_REQUEST_FAILED)
    assert seen == ["/v1/chat/completions"] * 2  # 实际发送 2 次：一次重发
    assert budget.requests_used == 2 and budget.retries_used == 1


async def test_http_503_retry_succeeds() -> None:
    with _fake_server([_Reply(503, error="unavailable"), _Reply(200)]) as (
        base_url,
        seen,
    ):
        budget = _guard(sleeper=_Sleeper())
        await _run_openai_agent(base_url, budget)
    assert seen == ["/v1/chat/completions"] * 2
    assert budget.requests_used == 2 and budget.retries_used == 1


async def test_http_429_with_retry_after_is_honoured() -> None:
    with _fake_server(
        [
            _Reply(429, error="rate limited", headers=(("Retry-After", "7"),)),
            _Reply(200),
        ]
    ) as (base_url, seen):
        sleeper = _Sleeper()
        budget = _guard(sleeper=sleeper)
        await _run_openai_agent(base_url, budget)
    assert len(seen) == 2
    assert sleeper.delays == [7.0]  # 按服务端指示等待（≤10 秒）


async def test_http_429_without_retry_after_uses_one_second() -> None:
    with _fake_server([_Reply(429, error="rate limited"), _Reply(200)]) as (
        base_url,
        seen,
    ):
        sleeper = _Sleeper()
        budget = _guard(sleeper=sleeper)
        await _run_openai_agent(base_url, budget)
    assert len(seen) == 2
    assert sleeper.delays == [1.0]


async def test_retry_after_over_ten_seconds_ends_without_resend() -> None:
    with _fake_server(
        [_Reply(429, error="rate limited", headers=(("Retry-After", "60"),))]
    ) as (base_url, seen):
        sleeper = _Sleeper()
        budget = _guard(sleeper=sleeper)
        await _expect_failure(_run_openai_agent(base_url, budget), MODEL_REQUEST_FAILED)
    assert len(seen) == 1  # 不提前重发
    assert sleeper.delays == []  # 也不等待
    assert budget.retries_used == 1


@pytest.mark.parametrize("status", [400, 401, 402, 404, 422])
def test_permanent_http_statuses_are_never_retried_offline(status: int) -> None:
    """（分类表）永久 HTTP 状态：白名单外，一律不重试。"""
    decision = classify_model_failure(
        ModelHTTPError(status, "deepseek-flash"), output_emitted=False
    )
    assert decision is not None and decision.retryable is False


async def test_permanent_http_statuses_are_not_retried() -> None:
    """（真实 SDK 路径）400/401/402/404/422：实际只发送一次并终态 ``model_request_failed``。"""
    for status in (400, 401, 402, 404, 422):
        with _fake_server([_Reply(status, error="permanent")]) as (base_url, seen):
            budget = _guard(sleeper=_Sleeper())
            await _expect_failure(
                _run_openai_agent(base_url, budget), MODEL_REQUEST_FAILED
            )
        assert len(seen) == 1, status
        assert budget.requests_used == 1 and budget.retries_used == 0, status


async def test_connection_refused_is_retried_once() -> None:
    """连接建立失败（回环上无监听端口）：白名单内，最多重发一次，最终永久失败码。"""
    budget = _guard(sleeper=_Sleeper())
    await _expect_failure(
        _run_openai_agent("http://127.0.0.1:1/v1", budget), MODEL_REQUEST_FAILED
    )
    assert budget.requests_used == 2 and budget.retries_used == 1


# ---------- 6. 与 S4-03 驱动／S4-04 工具面接通后的终态与 effect 数 ----------


class _ScriptedModel:
    """脚本桩：``(工具名, 参数)`` 或 ``None``（直接回答）；记录实际模型请求次数。"""

    def __init__(
        self, script: list[tuple[str, dict[str, Any]] | None], *, delay: float = 0.0
    ) -> None:
        self.script = script
        self.delay = delay
        self.calls = 0

    def model(self) -> FunctionModel:
        async def respond(
            messages: list[ModelMessage], info: AgentInfo
        ) -> ModelResponse:
            step = self.calls
            self.calls += 1
            if self.delay:
                await asyncio.sleep(self.delay)
            if step < len(self.script) and self.script[step] is not None:
                name, args = self.script[step]  # type: ignore[misc]
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            tool_name=name, args=args, tool_call_id=f"call-{step}"
                        )
                    ]
                )
            return ModelResponse(parts=[TextPart("完成")])

        return FunctionModel(respond)


async def _drive_run(
    db: Any, *, run_id: str, harness: EffectiveHarness, model: FunctionModel
) -> tuple[ExecutionDriver, "asyncio.Task[None]"]:
    """提交一次请求并启动执行；返回驱动与执行任务（需要中途介入的用例自己等任务）。"""
    repo = RunRepo(db)
    await RunService(repo).submit_request(
        conversation_id=CONVERSATION_ID,
        run_id=run_id,
        client_request_id=f"req-{run_id}",
        text="今天练得怎么样",
    )
    driver = ExecutionDriver(repo)
    work = build_run_work(
        db=db,
        repo=repo,
        model=model,
        harness=harness,
        conversation_id=CONVERSATION_ID,
        run_id=run_id,
        business_date=BUSINESS_DATE,
    )
    return driver, driver.start(run_id, work)


async def _run_record(db: Any, run_id: str) -> dict[str, Any]:
    run = await RunRepo(db).get_run(run_id)
    assert run is not None
    return dict(run)


async def test_run_starts_with_frozen_harness_and_records_tool_calls(
    tmp_path: Any,
) -> None:
    """正常闭环：工具调用计入工具池、请求计入请求池，Run 走到 ``completed``。"""
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile(db)
        model = _ScriptedModel([("read_training_records", {}), None])
        _driver, task = await _drive_run(
            db, run_id="run-ok", harness=_frozen_harness(), model=model.model()
        )
        await task
        run = await _run_record(db, "run-ok")
    assert run["status"] == "completed"
    assert run["error_code"] is None
    assert model.calls == 2  # 工具结果后仍允许第二次模型请求（20 次预算内）


async def test_request_pool_refuses_the_next_request_and_fails_the_run(
    tmp_path: Any,
) -> None:
    """全部实际模型请求共用同一预算：超出即终态 ``model_request_failed``，不发新请求。"""
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile(db)
        model = _ScriptedModel([("read_training_records", {}), None])
        _driver, task = await _drive_run(
            db,
            run_id="run-pool",
            harness=_frozen_harness(max_model_requests=1),
            model=model.model(),
        )
        await task
        run = await _run_record(db, "run-pool")
        messages = await RunRepo(db).list_run_messages("run-pool")
    assert run["status"] == "failed"
    assert run["error_code"] == MODEL_REQUEST_FAILED
    assert model.calls == 1  # 第二次请求被预算闸拦下，未实际发送
    # 失败不写框架回答：只留创建时同事务写入的用户请求事实
    assert [row["kind"] for row in messages] == ["user_request"]


async def test_tool_pool_refuses_the_next_tool_and_fails_the_run(tmp_path: Any) -> None:
    """工具池：第二个合法工具调用被闸下即终态 ``model_request_failed``。"""
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile(db)
        model = _ScriptedModel(
            [("read_training_records", {}), ("read_training_records", {}), None]
        )
        _driver, task = await _drive_run(
            db,
            run_id="run-tools",
            harness=_frozen_harness(max_tool_calls=1),
            model=model.model(),
        )
        await task
        run = await _run_record(db, "run-tools")
    assert run["status"] == "failed"
    assert run["error_code"] == MODEL_REQUEST_FAILED


async def test_request_clock_failure_is_persisted_as_model_request_timeout(
    tmp_path: Any,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile(db)
        model = _ScriptedModel([None], delay=0.5)
        _driver, task = await _drive_run(
            db,
            run_id="run-timeout",
            harness=_frozen_harness(
                request_timeout_seconds=0.05, run_timeout_seconds=30.0
            ),
            model=model.model(),
        )
        await task
        run = await _run_record(db, "run-timeout")
    assert run["status"] == "failed"
    assert run["error_code"] == MODEL_REQUEST_TIMEOUT
    assert model.calls == 2  # 未产出输出：先用重试池，再用尽后终态


async def test_cancel_during_model_request_starts_no_further_attempt(
    tmp_path: Any,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile(db)
        model = _ScriptedModel([None], delay=5.0)
        driver, task = await _drive_run(
            db, run_id="run-cancel", harness=_frozen_harness(), model=model.model()
        )
        await asyncio.sleep(0.05)
        await driver.cancel("run-cancel")
        await task
        run = await _run_record(db, "run-cancel")
        assert driver.active_run_id is None  # 名额已释放
    assert run["status"] == "cancelled"
    assert run["error_code"] is None
    assert model.calls == 1  # 取消后不启动新尝试，也不重试


async def test_correction_pool_is_shared_across_tools_and_caps_at_two() -> None:
    """输出／工具参数纠错共享 2 次池；框架纠错预算不能多出未计数尝试（池尽即终态失败）。"""
    script: list[tuple[str, dict[str, Any]]] = [
        (
            "read_training_records",
            {"week_no": "不是数字"},
        ),  # 参数校验失败 → 第 1 次纠错
        (
            "read_personal_record",
            {"exercise_id": 5},
        ),  # 另一个工具 → 第 2 次纠错（共享池）
        ("read_training_records", {"week_no": "仍不是数字"}),  # 池尽：不再发送
    ]
    model = _ScriptedModel([*script, None])  # type: ignore[list-item]
    budget = _guard(sleeper=_Sleeper())
    await _expect_failure(
        _managed_agent(model.model(), budget).run("ping"), MODEL_REQUEST_FAILED
    )
    assert model.calls == 3  # 首次 + 两次纠错；第三次纠错请求被池闸拦下
    assert budget.corrections_used == CORRECTION_POOL
    assert budget.requests_used == 3
