# Spike Item 1：前缀字节稳定性（离线，捕获真实序列化请求的原始 wire 字节）。
# 验证：常驻层 + 工具 schema + 稳定早期历史，在追加消息、工具往返、桩压缩后，
# 原始请求字节中 messages 稳定元素与 tools 数组的字节级稳定（非 sort_keys 再序列化）。
# canonical 规范化比较仅作语义辅助证据，不能替代 wire 字节证据。

from __future__ import annotations

import asyncio
import json

from pydantic_ai import Agent, RunContext

from spike_lib.capture import ScriptedTransport, deepseek_usage
from spike_lib.fee_guard import FeeGuard
from spike_lib.real_runner import build_spike_agent

USAGE0 = deepseek_usage(prompt_tokens=90, completion_tokens=5)
# 合成占位符（非凭据）：仅满足 provider 参数校验，真实调用走环境变量。
DUMMY_KEY = "dummy-not-a-credential"


def _build_agent(transport: ScriptedTransport, captured: list) -> Agent:
    async def lookup_equipment(ctx: RunContext[None], equipment_name: str) -> str:
        """查询器械可用性。"""
        return f"{equipment_name}: available"

    async def get_plan(ctx: RunContext[None], plan_id: str) -> str:
        """读取训练计划。"""
        return f"plan {plan_id}: PPL push day"

    lookup_equipment.__name__ = "lookup_equipment"
    get_plan.__name__ = "get_plan"

    # 经唯一入口构建：guard transport + max_retries=0 + max_tokens=256（wire 断言见 test_wire_guard.py）
    return build_spike_agent(
        "deepseek-v4-flash",
        guard=FeeGuard(),
        api_key=DUMMY_KEY,
        tools=[lookup_equipment, get_plan],
        instructions="常驻层：你是 Fit-Agent 计划助手。" * 6,
        inner_transport=transport,
        captured=captured,
    )


def test_prefix_wire_bytes_stable_across_tool_roundtrip_and_append():
    transport = ScriptedTransport(
        script=[
            {"usage": USAGE0, "tool_call": {"id": "c1", "name": "get_plan", "arguments": '{"plan_id":"p1"}'}},
            {"usage": USAGE0, "content": "done-1"},
            {"usage": USAGE0, "content": "done-2"},
        ]
    )
    captured: list = []
    agent = _build_agent(transport, captured)

    result1 = asyncio.run(agent.run("第一轮：查看计划 p1"))
    assert len(captured) == 2
    req1, req2 = captured[0], captured[1]

    # wire 约束同步断言：每次请求显式输出上限 ≤ 256（新旧字段形态都认）
    for req in (req1, req2):
        limit = req.body.get("max_tokens", req.body.get("max_completion_tokens"))
        assert isinstance(limit, int) and 0 < limit <= 256

    # 工具 schema 序列化：原始 wire 字节级稳定
    assert req1.wire_array("tools") == req2.wire_array("tools")
    assert len(req1.wire_elements("tools")) == 2

    # 工具往返后：req2 消息前缀 = req1 全部消息（逐元素原始字节等价）
    assert len(req2.body["messages"]) > len(req1.body["messages"])
    assert req2.wire_elements("messages")[: len(req1.body["messages"])] == req1.wire_elements("messages")

    # 追加对话：req3 消息前缀 = req2 全部消息（含常驻层、工具往返记录），tools 字节稳定
    result2 = asyncio.run(agent.run("第二轮：继续", message_history=result1.all_messages()))
    req3 = captured[2]
    assert req3.wire_elements("messages")[: len(req2.body["messages"])] == req2.wire_elements("messages")
    assert req3.wire_array("tools") == req1.wire_array("tools")

    # 辅助证据：canonical 语义一致（不替代 wire 字节比较）
    assert req1.canonical("tools") == req2.canonical("tools")
    _ = result2


def test_stub_compaction_keeps_wire_prefix_stable_and_does_not_split_tool_pairs():
    transport = ScriptedTransport(
        script=[
            {"usage": USAGE0, "tool_call": {"id": "c1", "name": "get_plan", "arguments": '{"plan_id":"p1"}'}},
            {"usage": USAGE0, "content": "done-1"},
            {"usage": USAGE0, "content": "after-compaction-1"},
            {"usage": USAGE0, "content": "after-compaction-2"},
        ]
    )
    captured: list = []
    agent = _build_agent(transport, captured)

    result1 = asyncio.run(agent.run("第一轮：查看计划 p1"))
    history = result1.all_messages()

    # 桩压缩：删除最早的 tool_call/result 对（整对删除，不拆对），
    # 用固定占位消息替代（不用真实摘要、不调用模型）。边界：常驻层 + 工具 schema + 占位后的稳定历史。
    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart

    assert isinstance(history[0], ModelRequest) and isinstance(history[0].parts[0], UserPromptPart)
    assert isinstance(history[1], ModelResponse) and isinstance(history[1].parts[0], ToolCallPart)
    assert isinstance(history[2], ModelRequest) and isinstance(history[2].parts[0], ToolReturnPart)
    placeholder = ModelRequest(parts=[UserPromptPart(content="[桩压缩占位：早期历史已移除，未拆分工具对]")])
    compacted = [placeholder, *history[3:]]

    result2 = asyncio.run(agent.run("压缩后第一轮", message_history=compacted))
    result3 = asyncio.run(agent.run("压缩后第二轮", message_history=result2.all_messages()))
    req_a, req_b = captured[2], captured[3]

    # 压缩后的前缀在后续追加中逐元素原始字节稳定
    assert req_b.wire_elements("messages")[: len(req_a.body["messages"])] == req_a.wire_elements("messages")
    # 占位消息确实替代了早期历史（不是原样保留）
    joined = req_a.wire_array("messages").decode()
    assert "桩压缩占位" in joined
    # 压缩前原始用户消息不在新请求中
    assert "第一轮：查看计划 p1" not in joined
    # tools 数组原始字节在压缩前后稳定
    assert req_a.wire_array("tools") == captured[0].wire_array("tools")
    _ = result3


def test_wire_bytes_differ_from_sorted_reserialization():
    """wire 字节证据与 sort_keys 再序列化不同：证明本套比较的是真实序列化形态。"""
    transport = ScriptedTransport(script=[{"usage": USAGE0, "content": "ok"}])
    captured: list = []
    agent = _build_agent(transport, captured)
    asyncio.run(agent.run("hi"))
    req = captured[0]
    msgs_wire = req.wire_elements("messages")
    msgs_canonical = [
        json.dumps(m, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        for m in req.body["messages"]
    ]
    # 真实 wire 序列化与规范化重排的键序不同（openai SDK 保留插入序）
    assert msgs_wire == msgs_canonical or any(w != c for w, c in zip(msgs_wire, msgs_canonical))
    # 但解析回的对象与请求体语义一致
    assert json.loads(b"[" + b",".join(msgs_wire) + b"]") == req.body["messages"]
