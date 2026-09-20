# Harness 基础层契约：定界只保留 head 且字节与行上限同时成立；policy wrapper 是唯一执行边界
# （扣预算 → 单工具超时 → execute → 结果校验 → 输出定界），真实故障一律原样传播。
# 替身预算 + 替身 execute 覆盖 wrapper 契约。
# 不触达模型入口：ModelGateway 只用不可用的替身。

import asyncio
from typing import Any

import pytest
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest, ToolRuntime
from langgraph.types import Command

from graph.model import ModelGateway
from harness.bounded import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    TRUNCATION_NOTICE,
    BoundedText,
    bound_text,
)
from harness.declaration import HarnessContext
from harness.policy import ToolResultContractError, build_tool_call_wrapper


class ToolCallBudgetExceeded(RuntimeError):
    """替身工具预算耗尽：生产 ModelRequestBudget 到阶段 4 才覆盖工具调用预算。"""


class FakeBudget:
    """替身预算：分别记录模型请求与工具调用扣减次数，可切换为工具预算耗尽。"""

    def __init__(self, *, exhausted: bool = False) -> None:
        self.exhausted = exhausted
        self.requests = 0
        self.tool_calls = 0

    def begin_request(self) -> float:
        self.requests += 1
        return 60.0

    def take_tool_call(self) -> None:
        if self.exhausted:
            raise ToolCallBudgetExceeded("单次 Run 的工具调用预算已用尽")
        self.tool_calls += 1


async def _unavailable_model(*_args: Any) -> Any:
    raise AssertionError("Harness 基础层测试不触达模型入口")


_MODEL = ModelGateway(
    text=_unavailable_model, structured=_unavailable_model, tools=_unavailable_model
)


def _request(budget: FakeBudget, *, name: str = "read_active_plan") -> ToolCallRequest:
    runtime = ToolRuntime(
        state={},
        context=HarnessContext(model=_MODEL, budget=budget),
        config={},
        stream_writer=lambda *_args: None,
        tool_call_id="call-1",
        store=None,
    )
    return ToolCallRequest(
        tool_call={"name": name, "args": {}, "id": "call-1"},
        tool=None,
        state={},
        runtime=runtime,
    )


def test_bound_text_returns_text_within_limits_unchanged() -> None:
    result = bound_text("active plan: push day\n20 sets\n")

    assert result == BoundedText(text="active plan: push day\n20 sets\n", truncated=False)


def test_bound_text_truncates_over_limit_text_once() -> None:
    text = "\n".join(f"line {index}" for index in range(1000))

    result = bound_text(text)

    assert result.truncated is True
    assert result.text.count(TRUNCATION_NOTICE) == 1
    assert len(result.text.encode("utf-8")) <= DEFAULT_MAX_BYTES
    assert result.text.count("\n") <= DEFAULT_MAX_LINES
    assert text.startswith(result.text.removesuffix(TRUNCATION_NOTICE))


def test_bound_text_truncates_by_lines_when_bytes_fit() -> None:
    result = bound_text("x\n" * 500, max_lines=10)

    assert result.truncated is True
    assert result.text.count("\n") == 10
    assert result.text == "x\n" * 9 + TRUNCATION_NOTICE


def test_bound_text_returns_text_at_exact_limits_unchanged() -> None:
    # 原始输入恰好占满字节与行上限时不属于截断，提示不得占用其额度。
    text = "a" * 18

    result = bound_text(text, max_bytes=18, max_lines=1)

    assert result == BoundedText(text=text, truncated=False)

    lines = "line\n" * 4

    assert bound_text(lines, max_bytes=len(lines.encode("utf-8")), max_lines=4) == BoundedText(
        text=lines, truncated=False
    )


def test_bound_text_rejects_limits_below_truncation_notice() -> None:
    notice_bytes = len(TRUNCATION_NOTICE.encode("utf-8"))

    with pytest.raises(ValueError, match="max_bytes"):
        bound_text("x" * 100, max_bytes=notice_bytes - 1)

    with pytest.raises(ValueError, match="max_lines"):
        bound_text("x" * 100, max_lines=TRUNCATION_NOTICE.count("\n") - 1)

    # 恰好容纳提示的上限合法：提示完整出现且输出不超限。
    exact = bound_text("x" * 100, max_bytes=notice_bytes, max_lines=1)

    assert exact == BoundedText(text=TRUNCATION_NOTICE, truncated=True)


def test_bound_text_truncates_multibyte_at_byte_budget_boundary() -> None:
    notice_bytes = len(TRUNCATION_NOTICE.encode("utf-8"))

    # 提示占 18 字节：max_bytes=21 给 head 留 3 字节，正好容纳一个「汉」。
    exact = bound_text("汉" * 100, max_bytes=notice_bytes + 3, max_lines=1)

    assert exact == BoundedText(text="汉" + TRUNCATION_NOTICE, truncated=True)
    assert len(exact.text.encode("utf-8")) == notice_bytes + 3

    # head 预算 2 字节落在「汉」的字节中间：整字符丢弃，仍为合法 UTF-8。
    partial = bound_text("汉" * 100, max_bytes=notice_bytes + 2, max_lines=1)

    assert partial == BoundedText(text=TRUNCATION_NOTICE, truncated=True)
    assert partial.text.encode("utf-8").decode("utf-8") == partial.text


async def test_wrapper_takes_tool_call_budget_before_execute() -> None:
    budget = FakeBudget()
    budget_at_execute: list[int] = []

    async def execute(request: ToolCallRequest) -> ToolMessage:
        budget_at_execute.append(budget.tool_calls)
        return ToolMessage(content="ok", tool_call_id="call-1", name="read_active_plan")

    result = await build_tool_call_wrapper(timeout_seconds=5.0)(_request(budget), execute)

    assert budget_at_execute == [1]
    assert result.content == "ok"


async def test_wrapper_skips_execute_when_budget_exhausted() -> None:
    budget = FakeBudget(exhausted=True)
    executed: list[str] = []

    async def execute(request: ToolCallRequest) -> ToolMessage:
        executed.append("executed")
        return ToolMessage(content="ok", tool_call_id="call-1")

    wrapper = build_tool_call_wrapper(timeout_seconds=5.0)

    with pytest.raises(ToolCallBudgetExceeded):
        await wrapper(_request(budget), execute)

    assert executed == []
    assert budget.tool_calls == 0


async def test_wrapper_propagates_tool_timeout() -> None:
    budget = FakeBudget()

    async def execute(request: ToolCallRequest) -> ToolMessage:
        await asyncio.sleep(5)
        return ToolMessage(content="ok", tool_call_id="call-1")

    wrapper = build_tool_call_wrapper(timeout_seconds=0.01)

    with pytest.raises(TimeoutError):
        await wrapper(_request(budget), execute)

    assert budget.tool_calls == 1


async def test_wrapper_truncates_content_and_keeps_message_identity() -> None:
    budget = FakeBudget()
    original = ToolMessage(
        content="y" * 40000,
        tool_call_id="call-1",
        name="read_recent_workouts",
        status="success",
        artifact={"source": "records"},
    )

    async def execute(request: ToolCallRequest) -> ToolMessage:
        return original

    result = await build_tool_call_wrapper(timeout_seconds=5.0)(_request(budget), execute)

    assert isinstance(result, ToolMessage)
    assert result is not original
    assert len(result.content.encode("utf-8")) <= DEFAULT_MAX_BYTES
    assert result.tool_call_id == "call-1"
    assert result.name == "read_recent_workouts"
    assert result.status == "success"
    assert result.artifact == {"source": "records"}


async def test_wrapper_returns_tool_message_untouched_when_within_limits() -> None:
    budget = FakeBudget()
    original = ToolMessage(content="短输出", tool_call_id="call-1", name="read_progress")

    async def execute(request: ToolCallRequest) -> ToolMessage:
        return original

    result = await build_tool_call_wrapper(timeout_seconds=5.0)(_request(budget), execute)

    assert result is original


async def test_wrapper_rejects_non_tool_message() -> None:
    budget = FakeBudget()

    async def execute(request: ToolCallRequest) -> Command:
        return Command(update={})

    wrapper = build_tool_call_wrapper(timeout_seconds=5.0)

    with pytest.raises(ToolResultContractError):
        await wrapper(_request(budget), execute)


async def test_wrapper_rejects_non_string_content() -> None:
    budget = FakeBudget()

    async def execute(request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(content=[{"type": "text", "text": "ok"}], tool_call_id="call-1")

    wrapper = build_tool_call_wrapper(timeout_seconds=5.0)

    with pytest.raises(ToolResultContractError):
        await wrapper(_request(budget), execute)


async def test_wrapper_propagates_tool_exception() -> None:
    budget = FakeBudget()

    async def execute(request: ToolCallRequest) -> ToolMessage:
        raise RuntimeError("查询训练记录失败")

    wrapper = build_tool_call_wrapper(timeout_seconds=5.0)

    with pytest.raises(RuntimeError, match="查询训练记录失败"):
        await wrapper(_request(budget), execute)


async def test_wrapper_propagates_cancellation() -> None:
    budget = FakeBudget()

    async def execute(request: ToolCallRequest) -> ToolMessage:
        raise asyncio.CancelledError

    wrapper = build_tool_call_wrapper(timeout_seconds=5.0)

    with pytest.raises(asyncio.CancelledError):
        await wrapper(_request(budget), execute)


