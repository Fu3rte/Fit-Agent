# wire 层护栏离线验证：唯一真实入口 + FeeGuardTransport。
# 验证：请求体显式 max_tokens≤256；SDK 重试 0（429/5xx 恰好 1 次请求）；逐次 wire 级预留
# （一次 Run 内 2 次模型请求 = 2 条账目）；响应身份缺失/别名/不匹配停止；输入上界估算。

import asyncio
import json

import httpx2 as httpx
import pytest
from pydantic_ai import RunContext

from spike_lib.capture import ScriptedTransport, deepseek_usage
from spike_lib.fee_guard import MAX_TOKENS_LIMIT, FeeGuard, StopSpike
from spike_lib.guard_transport import FeeGuardTransport, estimate_input_tokens_upper_bound
from spike_lib.real_runner import build_spike_agent
from spike_lib.run_harness import RunHarness, RunOutcome

USAGE = deepseek_usage(prompt_tokens=90, completion_tokens=5)
DUMMY_KEY = "dummy-not-a-credential"  # 合成占位符（非凭据）；真实调用走环境变量
BASE = "https://api.deepseek.com"


def _agent(transport: ScriptedTransport, captured: list, *, tools: list | None = None, guard: FeeGuard | None = None):
    async def noop_tool(ctx: RunContext[None]) -> str:
        """空操作。"""
        return "ok"

    if tools is None:
        noop_tool.__name__ = "noop_tool"
        tools = [noop_tool]
    return build_spike_agent(
        "deepseek-v4-flash",
        guard=guard or FeeGuard(),
        api_key=DUMMY_KEY,
        tools=tools,
        instructions="常驻层",
        inner_transport=transport,
        captured=captured,
    )


def test_wire_request_carries_explicit_output_limit_256_and_known_model():
    transport = ScriptedTransport(script=[{"usage": USAGE, "content": "ok"}])
    captured: list = []
    agent = _agent(transport, captured)
    asyncio.run(agent.run("hi"))
    req = captured[0]
    # pydantic-ai/OpenAI SDK 3.x 发送新字段 max_completion_tokens；护栏两种形态都强制 ≤256
    assert req.body["max_completion_tokens"] == MAX_TOKENS_LIMIT == 256
    assert req.body["model"] == "deepseek-v4-flash"
    assert req.url.startswith(BASE)


def test_guard_rejects_request_without_max_tokens_before_dispatch():
    """max_tokens 缺失 → 拒绝发出（未到达 inner transport，无预留）。"""
    guard = FeeGuard()
    inner_calls: list = []

    def inner_handler(request: httpx.Request) -> httpx.Response:
        inner_calls.append(request)
        return httpx.Response(200, json={})

    transport = FeeGuardTransport(
        inner=httpx.MockTransport(inner_handler),
        guard=guard,
    )
    request = httpx.Request("POST", f"{BASE}/chat/completions", json={"model": "deepseek-v4-flash", "messages": []})
    with pytest.raises(StopSpike, match="输出上限"):
        asyncio.run(transport.handle_async_request(request))
    assert inner_calls == []  # 请求未发出
    assert guard.calls == [] and guard.reserved_usd == 0


def test_guard_rejects_output_limit_over_limit_both_field_forms():
    guard = FeeGuard()
    transport = FeeGuardTransport(inner=httpx.MockTransport(lambda r: httpx.Response(200, json={})), guard=guard)
    for field, value in (("max_tokens", 999), ("max_completion_tokens", 999)):
        request = httpx.Request(
            "POST", f"{BASE}/chat/completions", json={"model": "deepseek-v4-flash", "messages": [], field: value}
        )
        with pytest.raises(StopSpike, match="256"):
            asyncio.run(transport.handle_async_request(request))
    assert guard.reserved_usd == 0


def test_guard_rejects_unknown_model_in_request():
    guard = FeeGuard()
    transport = FeeGuardTransport(inner=httpx.MockTransport(lambda r: httpx.Response(200, json={})), guard=guard)
    request = httpx.Request(
        "POST", f"{BASE}/chat/completions", json={"model": "deepseek-v4-not-real", "messages": [], "max_tokens": 256}
    )
    with pytest.raises(StopSpike, match="未知模型"):
        asyncio.run(transport.handle_async_request(request))
    assert guard.reserved_usd == 0


def _status_response_run(status: int):
    """脚本返回 4xx/5xx：SDK max_retries=0 → 恰好 1 次 wire 请求；预留全额保留。"""
    transport = ScriptedTransport(script=[{"status": status, "error_message": "server error"}])
    captured: list = []
    guard = FeeGuard()
    outcome = RunOutcome(status="running")
    agent = _agent(transport, captured, guard=guard)
    harness = RunHarness(agent)
    result = asyncio.run(harness.run("触发错误", outcome=outcome))
    return result, captured, guard, outcome


def test_http_429_exactly_one_request_no_retry_reservation_kept():
    result, captured, guard, _ = _status_response_run(429)
    assert result.status == "failed"
    assert len(captured) == 1  # SDK max_retries=0：没有第二次请求
    assert guard.calls[-1].settled_usd is None  # 预留保留，不算 0
    assert guard.reserved_usd == 0  # 已转入账本占用
    assert guard.calls[-1].note == "http_429"


def test_http_500_exactly_one_request_no_retry_reservation_kept():
    result, captured, guard, _ = _status_response_run(500)
    assert result.status == "failed"
    assert len(captured) == 1
    assert guard.calls[-1].settled_usd is None
    assert guard.calls[-1].note == "http_500"


def test_per_wire_call_budget_two_model_requests_two_ledger_entries():
    """一次 Run 内工具往返产生 2 次模型请求 → 2 条独立账目（逐次 wire 级，不是逐 Run）。"""
    transport = ScriptedTransport(
        script=[
            {"usage": USAGE, "tool_call": {"id": "c1", "name": "noop_tool", "arguments": "{}"}},
            {"usage": USAGE, "content": "final"},
        ]
    )
    captured: list = []
    guard = FeeGuard()

    async def noop_tool(ctx: RunContext[None]) -> str:
        """空操作。"""
        return "ok"

    noop_tool.__name__ = "noop_tool"
    agent = build_spike_agent(
        "deepseek-v4-flash",
        guard=guard,
        api_key=DUMMY_KEY,
        tools=[noop_tool],
        instructions="常驻层",
        inner_transport=transport,
        captured=captured,
    )
    result = asyncio.run(agent.run("调用工具"))
    assert result.output == "final"
    assert len(captured) == 2
    assert len(guard.calls) == 2  # 两条独立账目
    assert all(c.settled_usd is not None for c in guard.calls)
    assert guard.reserved_usd == 0
    assert guard.settled_usd > 0


def test_response_model_missing_stops_and_keeps_reservation():
    transport = ScriptedTransport(script=[{"usage": USAGE, "content": "ok", "omit_model": True}])
    captured: list = []
    guard = FeeGuard()
    agent = _agent(transport, captured, tools=[], guard=guard)
    with pytest.raises(Exception):  # 响应缺 model 身份：SDK 校验失败或 guard 停止均算拦截
        asyncio.run(agent.run("hi"))
    assert guard.stopped is not None and "身份" in guard.stopped
    assert guard.calls[-1].settled_usd is None  # 预留保留


def test_response_official_version_alias_is_explicitly_unverified_not_silent():
    transport = ScriptedTransport(script=[{"usage": USAGE, "content": "ok", "model": "DeepSeek-V4-Flash-0731"}])
    captured: list = []
    guard = FeeGuard()
    agent = _agent(transport, captured, tools=[], guard=guard)
    # 语义：本次运行完成（响应可解析），但身份未验证 → 账本停止，后续 reserve 拒绝，预留保留
    asyncio.run(agent.run("hi"))
    assert guard.stopped is not None and "未经验证" in guard.stopped
    assert guard.calls[-1].settled_usd is None
    assert guard.calls[-1].note == "identity_alias_unverified:DeepSeek-V4-Flash-0731"
    with pytest.raises(StopSpike, match="已停止"):
        guard.reserve("deepseek-v4-flash", input_tokens_reserve=1000)


def test_response_known_model_mismatch_stops():
    transport = ScriptedTransport(script=[{"usage": USAGE, "content": "ok", "model": "deepseek-v4-pro"}])
    captured: list = []
    guard = FeeGuard()
    agent = _agent(transport, captured, tools=[], guard=guard)
    # 语义：本次运行完成（响应可解析），但身份不匹配 → 账本停止，后续 reserve 拒绝，预留保留
    asyncio.run(agent.run("hi"))
    assert guard.stopped is not None and "不匹配" in guard.stopped
    assert guard.calls[-1].settled_usd is None
    assert guard.calls[-1].note == "identity_mismatch:deepseek-v4-pro"
    with pytest.raises(StopSpike, match="已停止"):
        guard.reserve("deepseek-v4-flash", input_tokens_reserve=1000)


def test_estimate_input_tokens_upper_bound_scales_and_is_conservative():
    small = {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 256}
    big = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "常驻层内容" * 500}],
        "max_tokens": 256,
        "tools": [{"type": "function", "function": {"name": "t", "description": "d" * 200, "parameters": {}}}],
    }
    s = estimate_input_tokens_upper_bound(small)
    b = estimate_input_tokens_upper_bound(big)
    assert s > 0 and b > s
    # 保守性：UTF-8 字节/2 + 开销 ≥ 任何合理分词的 token 数（粗上限 sanity）
    import math

    payload_bytes = len(json.dumps(big["messages"], ensure_ascii=False).encode("utf-8"))
    assert b >= math.ceil(payload_bytes / 2)
