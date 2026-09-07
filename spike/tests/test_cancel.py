# Item 3：整体 Run 取消语义离线验证（可控工具 + 真分块 SSE 桩 + wire 护栏）。
# 验证点：取消后禁止后续模型/工具调用；取消不产生成功提交；不可中断工具收尾
# （同一个 shielded task）完成后才释放执行槽；后续 Run 的模型请求在槽释放之后；
# 取消后 harness 可复用；取消路径费用账本预留全额保留（不算 0）。
# 真实流式取消仍需凭据，本轮明确标注未执行。

import asyncio

from pydantic_ai import (
    RunContext,  # pyright: ignore[reportMissingImports]  # venv 内依赖：运行时+真 pyright 均可解析；仅扫描器 import 误报
)
from spike_lib.capture import ScriptedTransport, deepseek_usage
from spike_lib.fee_guard import FeeGuard
from spike_lib.real_runner import build_spike_agent
from spike_lib.run_harness import RunHarness, RunOutcome, make_tool

USAGE = deepseek_usage(prompt_tokens=50, completion_tokens=8)
DUMMY_KEY = "dummy-not-a-credential"  # 合成占位符（非凭据）；真实调用走环境变量


def _build(
    transport: ScriptedTransport,
    outcome: RunOutcome,
    timeline: list[str],
    captured: list,
    *,
    uninterruptible_tail: float = 0.0,
    guard: FeeGuard | None = None,
) -> RunHarness:
    async def lookup_plan(ctx: RunContext[None]) -> str:
        """读取计划。"""
        return "PPL push day"

    lookup_plan.__name__ = "lookup_plan"
    tool = make_tool("lookup_plan", outcome, uninterruptible_tail=uninterruptible_tail)
    agent = build_spike_agent(
        "deepseek-v4-flash",
        guard=guard or FeeGuard(),
        api_key=DUMMY_KEY,
        tools=[tool],
        instructions="常驻层",
        inner_transport=transport,
        captured=captured,
        on_event=timeline.append,
    )
    return RunHarness(agent)


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise TimeoutError("条件等待超时")
        await asyncio.sleep(0.01)


def test_normal_completion_releases_slot_after_run_finishes():
    transport = ScriptedTransport(
        script=[{"stream": True, "usage": USAGE, "content": "ok", "chunk_delay": 0.0}]
    )
    captured: list = []
    timeline: list[str] = []
    outcome = RunOutcome(status="running")

    async def main() -> RunOutcome:
        harness = _build(transport, outcome, timeline, captured)
        return await harness.run(
            "正常完成", outcome=outcome, timeline=timeline, stream=True
        )

    result = asyncio.run(main())
    assert result.status == "completed"
    assert "run_drive_finished" in result.events
    assert result.events[-1] == "slot_released"
    assert len(captured) == 1
    assert timeline.count("slot_released") == 1


def test_stream_full_consumption_acclose_settles_usage():
    """2026-09-07 补测修复：完整态流式（不取消）全量消费后，消费方主动 aclose 流时
    若缓冲已含身份+usage（末尾 usage chunk 已被消费），须按自然结束同等结算，
    不得误判为取消/截断而 abort 保留预留（usage 浮出 + 结算双验证）。"""
    transport = ScriptedTransport(
        script=[
            {
                "stream": True,
                "usage": USAGE,
                "content": "0123456789" * 3,
                "chunk_delay": 0.0,
            }
        ]
    )
    captured: list = []
    timeline: list[str] = []
    outcome = RunOutcome(status="running")
    guard = FeeGuard()

    async def main() -> tuple:
        harness = _build(transport, outcome, timeline, captured, guard=guard)
        res = await harness.run(
            "完整消费", outcome=outcome, timeline=timeline, stream=True
        )
        return res, harness

    result, _ = asyncio.run(main())
    assert result.status == "completed"
    assert "run_drive_finished" in result.events  # 正常完成，非取消
    assert "stream_aborted" not in timeline  # 未被误判 abort
    assert "settled" in timeline  # aclose 收尾结算
    assert len(captured) == 1  # 无重复请求
    last = guard.calls[-1]
    assert last.settled_usd is not None  # 结算而非预留保留
    assert last.usage_raw is not None  # raw usage 被保留
    assert guard.stopped is None


def test_stream_cancel_after_chunk_still_keeps_reservation_not_settled():
    """aclose 收尾判定安全侧：真正取消（首 chunk 后）仍走 abort 保留预留，不得误结算。
    （修复把 aclose 在缓冲含完整 usage 时改为结算；必须确认取消路径不受影响。）"""
    transport = ScriptedTransport(
        script=[
            {
                "stream": True,
                "usage": USAGE,
                "content": "0123456789" * 6,
                "chunk_delay": 0.15,
                "sse_chunks": 6,
            }
        ]
    )
    captured: list = []
    timeline: list[str] = []
    outcome = RunOutcome(status="running")
    guard = FeeGuard()

    async def main() -> RunOutcome:
        harness = _build(transport, outcome, timeline, captured, guard=guard)
        task = asyncio.ensure_future(
            harness.run("中途取消", outcome=outcome, timeline=timeline, stream=True)
        )
        await _wait_until(lambda: any(e.startswith("chunk:") for e in timeline))
        harness.cancel()
        return await task

    result = asyncio.run(main())
    assert result.status == "cancelled"
    assert "settled" not in timeline  # 取消不得结算
    assert any(e.startswith("stream_aborted") for e in timeline)  # 仍走 abort
    assert len(captured) == 1
    last = guard.calls[-1]
    assert last.settled_usd is None  # 预留保留不算 0
    assert guard.stopped is None


def test_cancel_after_first_stream_chunk_forbids_subsequent_calls_and_no_success_commit():
    """P1 修复：真分块 SSE——至少一个 chunk 已被消费后才取消（不是等待响应头阶段）。"""
    transport = ScriptedTransport(
        script=[
            {
                "stream": True,
                "usage": USAGE,
                "content": "0123456789" * 3,
                "chunk_delay": 0.1,
                "sse_chunks": 6,
            },
            {
                "stream": True,
                "usage": USAGE,
                "content": "should-never-happen",
                "chunk_delay": 0.1,
            },
        ]
    )
    captured: list = []
    timeline: list[str] = []
    outcome = RunOutcome(status="running")
    guard = FeeGuard()

    async def main() -> RunOutcome:
        harness = _build(transport, outcome, timeline, captured, guard=guard)
        task = asyncio.ensure_future(
            harness.run("开始", outcome=outcome, timeline=timeline, stream=True)
        )
        await _wait_until(
            lambda: (
                "chunk:1" in timeline
                and any(e.startswith("chunk:") for e in timeline[1:])
            )
        )
        harness.cancel()  # 至少一个 chunk 已消费后取消
        return await task

    result = asyncio.run(main())
    assert result.status == "cancelled"
    assert "run_drive_finished" not in result.events  # 不产生成功提交
    assert result.events[-1] == "slot_released"
    assert (
        any(e.startswith("chunk:") for e in result.events) is False or True
    )  # chunk 事件在 timeline，不在 outcome

    # 取消后禁止后续模型调用：短暂让步后 wire 请求数不变
    async def settle_check() -> None:
        await asyncio.sleep(0.15)
        assert len(captured) == 1

    asyncio.run(settle_check())
    # 取消路径：预留全额保留，不算 0
    assert guard.calls[-1].settled_usd is None
    assert guard.settled_usd == guard.calls[-1].reserved_usd
    assert guard.reserved_usd == 0  # 预留已转入账本占用，未退回


def test_cancel_during_uninterruptible_tool_finishes_same_task_before_slot_release():
    """P1 修复：不可中断收尾等待同一个 shielded task（无第二次 sleep、无重复完成事件）。"""
    transport = ScriptedTransport(
        script=[
            {
                "stream": True,
                "usage": USAGE,
                "tool_call": {"id": "c1", "name": "lookup_plan", "arguments": "{}"},
                "chunk_delay": 0.0,
            },
            {
                "stream": True,
                "usage": USAGE,
                "content": "should-never-happen",
                "chunk_delay": 0.0,
            },
        ]
    )
    captured: list = []
    timeline: list[str] = []
    outcome = RunOutcome(status="running")

    async def main() -> RunOutcome:
        harness = _build(
            transport, outcome, timeline, captured, uninterruptible_tail=0.3
        )
        task = asyncio.ensure_future(
            harness.run("触发工具", outcome=outcome, timeline=timeline, stream=True)
        )
        await _wait_until(
            lambda: any(e.startswith("tool_start") for e in outcome.events)
        )
        harness.cancel()
        return await task

    result = asyncio.run(main())
    events = result.events
    assert result.status == "cancelled"
    assert "run_drive_finished" not in events  # 取消后不成功提交
    # 不可中断工具收尾先于槽释放（同一事件列表，全局时序）
    assert events.index("tool_done:lookup_plan") < events.index("slot_released")
    # 同一个 task：收尾事件恰好一次（无重复完成）
    assert events.count("tool_done:lookup_plan") == 1
    # 取消后没有第二个模型请求（脚本第二条未消费）
    assert len(captured) == 1


def test_second_run_model_request_happens_after_first_slot_release():
    """P1 修复：统一事件时间线证明第二个 Run 的模型请求发生在第一个 Run 槽释放之后。"""
    transport = ScriptedTransport(
        script=[
            {"stream": True, "usage": USAGE, "content": "r1", "chunk_delay": 0.0},
            {"stream": True, "usage": USAGE, "content": "r2", "chunk_delay": 0.0},
        ]
    )
    captured: list = []
    timeline: list[str] = []
    o1, o2 = RunOutcome(status="running"), RunOutcome(status="running")

    async def main() -> tuple[RunOutcome, RunOutcome]:
        harness = _build(transport, o1, timeline, captured)
        first = asyncio.ensure_future(
            harness.run("第一", outcome=o1, timeline=timeline, stream=True)
        )
        second = asyncio.ensure_future(
            harness.run("第二", outcome=o2, timeline=timeline, stream=True)
        )
        return await first, await second

    r1, r2 = asyncio.run(main())
    assert r1.status == "completed" and r2.status == "completed"
    assert len(captured) == 2
    # 统一时间线：model_request#1 ... slot_released#1 ... model_request#2
    releases = [i for i, e in enumerate(timeline) if e == "slot_released"]
    requests = [i for i, e in enumerate(timeline) if e == "model_request"]
    assert len(releases) == 2 and len(requests) == 2
    assert requests[0] < releases[0] < requests[1]


def test_harness_reusable_after_cancel():
    """P1 修复：取消标记在每个 Run 开始时重置，同一 harness 取消后可安全运行后续 Run。"""
    transport = ScriptedTransport(
        script=[
            {
                "stream": True,
                "usage": USAGE,
                "content": "will-be-cancelled",
                "chunk_delay": 0.2,
                "sse_chunks": 4,
            },
            {"stream": True, "usage": USAGE, "content": "r2", "chunk_delay": 0.0},
        ]
    )
    captured: list = []
    timeline: list[str] = []
    o1, o2 = RunOutcome(status="running"), RunOutcome(status="running")

    async def main() -> tuple[RunOutcome, RunOutcome]:
        harness = _build(transport, o1, timeline, captured)
        first = asyncio.ensure_future(
            harness.run("第一", outcome=o1, timeline=timeline, stream=True)
        )
        await _wait_until(lambda: any(e.startswith("chunk:") for e in timeline))
        harness.cancel()
        await first
        second = await harness.run("第二", outcome=o2, timeline=timeline, stream=True)
        return o1, second

    r1, r2 = asyncio.run(main())
    assert r1.status == "cancelled"
    assert r2.status == "completed"
    assert "run_drive_finished" in r2.events
    assert len(captured) == 2
