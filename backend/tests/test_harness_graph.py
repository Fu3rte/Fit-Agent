# Harness agent loop 契约：model → tools → model 由 LangGraph 表达，node 内无 while；
# 工具固化为一个 tuple 且 model node 与 ToolNode 共用；模型请求预算与工具调用预算各自独立终止 Run；
# compile 不传 checkpointer。模型入口用阶段 2 替身，工具调用预算用替身以分别切换两项终态。

import asyncio
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, ConfigDict

from graph.model import InvalidModelResponse, ModelGateway
from harness.declaration import HarnessContext
from harness.graph import build_tool_harness


class ModelRequestBudgetExceeded(RuntimeError):
    """替身模型请求预算耗尽：生产 ModelRequestBudget 到阶段 4 才进入 Harness。"""


class ToolCallBudgetExceeded(RuntimeError):
    """替身工具调用预算耗尽。"""


class FakeBudget:
    """替身预算：模型请求与工具调用各自独立计数，可分别切换为耗尽或极短超时。"""

    def __init__(
        self,
        *,
        request_timeout: float = 60.0,
        requests_exhausted: bool = False,
        tool_calls_exhausted: bool = False,
    ) -> None:
        self.request_timeout = request_timeout
        self.requests_exhausted = requests_exhausted
        self.tool_calls_exhausted = tool_calls_exhausted
        self.requests = 0
        self.tool_calls = 0

    def begin_request(self) -> float:
        if self.requests_exhausted:
            raise ModelRequestBudgetExceeded("单次 Run 的模型请求预算已用尽")
        self.requests += 1
        return self.request_timeout

    def take_tool_call(self) -> None:
        if self.tool_calls_exhausted:
            raise ToolCallBudgetExceeded("单次 Run 的工具调用预算已用尽")
        self.tool_calls += 1


class ScriptedToolModel:
    """阶段 2 替身：只提供 tools 形态，按序返回脚本化消息并记录每轮收到的消息与 offered tuple。"""

    def __init__(self, responses: list[AnyMessage], *, delay_seconds: float = 0.0) -> None:
        self._responses = responses
        self._delay_seconds = delay_seconds
        self.calls: list[list[AnyMessage]] = []
        self.offered: list[tuple[BaseTool, ...]] = []

    async def tools(
        self,
        messages: list[AnyMessage],
        offered: tuple[BaseTool, ...],
    ) -> AnyMessage:
        self.calls.append(list(messages))
        self.offered.append(offered)
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        return self._responses.pop(0)


class ParallelProbe:
    """并行探针：两个只读工具都到达屏障后才放行；被顺序执行时等待方超时。"""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.barrier = asyncio.Barrier(2)
        self.arrived: list[str] = []

    async def arrive(self, name: str) -> None:
        self.arrived.append(name)
        async with asyncio.timeout(1.0):
            await self.barrier.wait()


CALLS: list[str] = []
PROBE = ParallelProbe()


class StrictCalendarArgs(BaseModel):
    """严格参数 schema：类型错误与额外字段都必须被拒。"""

    model_config = ConfigDict(extra="forbid")

    day: int


@tool("read_training_calendar", args_schema=StrictCalendarArgs)
async def read_training_calendar(day: int) -> str:
    """读取指定训练日安排。"""
    CALLS.append(f"read_training_calendar:{day}")
    return f"day {day}"


@tool("read_recent_workouts")
async def read_recent_workouts() -> str:
    """读取近期训练记录；本替身固定失败，用于锁定真实故障上抛。"""
    raise RuntimeError("查询训练记录失败")


@tool("read_active_plan")
async def read_active_plan() -> str:
    """读取当前计划；并行探针。"""
    await PROBE.arrive("read_active_plan")
    return "active plan ok"


@tool("read_progress")
async def read_progress() -> str:
    """读取进展概览；并行探针。"""
    await PROBE.arrive("read_progress")
    return "progress ok"


@tool("read_slow_progress")
async def read_slow_progress() -> str:
    """读取进展概览；本替身固定慢于测试配置的工具超时，用于锁定超时上抛。"""
    await asyncio.sleep(5)
    return "progress ok"


TOOLS: tuple[BaseTool, ...] = (
    read_training_calendar,
    read_recent_workouts,
    read_active_plan,
    read_progress,
    read_slow_progress,
)


@pytest.fixture(autouse=True)
def _reset_fakes() -> None:
    CALLS.clear()
    PROBE.reset()


def _harness(*, timeout_seconds: float = 5.0) -> CompiledStateGraph:
    """传入 list 以锁定 builder 把 tools 固化为 tuple。"""
    return build_tool_harness(
        list(TOOLS),
        timeout_seconds=timeout_seconds,
    )


def _tool_call(name: str, args: dict[str, Any], call_id: str = "call-1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def _context(budget: FakeBudget, model: ScriptedToolModel) -> HarnessContext:
    # 阶段 4 给 ModelGateway 增加 tools 字段后，替身可直接构造成 ModelGateway。
    return HarnessContext(model=cast(ModelGateway, model), budget=budget)


async def _run(
    harness: CompiledStateGraph,
    budget: FakeBudget,
    model: ScriptedToolModel,
) -> list[AnyMessage]:
    state = await harness.ainvoke(
        {"messages": [HumanMessage(content="今天练什么")]},
        context=_context(budget, model),
    )
    return state["messages"]


def test_harness_compiles_without_checkpointer() -> None:
    assert _harness().checkpointer is None


async def test_model_returns_final_text_without_tool_calls() -> None:
    budget = FakeBudget()
    model = ScriptedToolModel([AIMessage(content="今天没有安排")])

    messages = await _run(_harness(), budget, model)

    assert [type(message) for message in messages] == [HumanMessage, AIMessage]
    assert messages[-1].content == "今天没有安排"
    assert budget.requests == 1
    assert budget.tool_calls == 0
    assert CALLS == []
    assert model.offered == [TOOLS]


async def test_single_tool_call_then_final_text() -> None:
    budget = FakeBudget()
    model = ScriptedToolModel(
        [
            _tool_call("read_training_calendar", {"day": 3}),
            AIMessage(content="第三天练腿"),
        ]
    )

    messages = await _run(_harness(), budget, model)

    assert [type(message) for message in messages] == [
        HumanMessage,
        AIMessage,
        ToolMessage,
        AIMessage,
    ]
    tool_message = messages[2]
    assert tool_message.content == "day 3"
    assert tool_message.tool_call_id == "call-1"
    assert tool_message.status == "success"
    assert messages[-1].content == "第三天练腿"
    assert budget.requests == 2
    assert budget.tool_calls == 1
    assert CALLS == ["read_training_calendar:3"]
    assert model.calls[1][-1] is tool_message


async def test_two_consecutive_tool_rounds() -> None:
    budget = FakeBudget()
    model = ScriptedToolModel(
        [
            _tool_call("read_training_calendar", {"day": 1}, "call-1"),
            _tool_call("read_training_calendar", {"day": 2}, "call-2"),
            AIMessage(content="两天都读到了"),
        ]
    )

    messages = await _run(_harness(), budget, model)

    assert [message.content for message in messages if isinstance(message, ToolMessage)] == [
        "day 1",
        "day 2",
    ]
    assert budget.requests == 3
    assert budget.tool_calls == 2
    assert CALLS == ["read_training_calendar:1", "read_training_calendar:2"]


async def test_unknown_tool_name_becomes_error_tool_message() -> None:
    # 锁定当前安装版本 ToolNode 默认分类：未知工具名由 execute() 生成 ToolMessage(status="error")。
    budget = FakeBudget()
    model = ScriptedToolModel(
        [
            _tool_call("read_sleep_log", {}),
            AIMessage(content="换成已提供的工具"),
        ]
    )

    messages = await _run(_harness(), budget, model)

    error_message = messages[2]
    assert isinstance(error_message, ToolMessage)
    assert error_message.status == "error"
    assert error_message.tool_call_id == "call-1"
    assert "read_sleep_log is not a valid tool" in error_message.content
    assert budget.tool_calls == 1
    assert model.calls[1][-1] is error_message


async def test_invalid_args_error_tool_message_then_model_corrects() -> None:
    budget = FakeBudget()
    model = ScriptedToolModel(
        [
            _tool_call("read_training_calendar", {"day": "abc"}, "call-1"),
            _tool_call("read_training_calendar", {"day": 3}, "call-2"),
            AIMessage(content="参数已修正"),
        ]
    )

    messages = await _run(_harness(), budget, model)

    tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
    assert [message.status for message in tool_messages] == ["error", "success"]
    assert "day" in tool_messages[0].content
    assert tool_messages[1].content == "day 3"
    assert budget.requests == 3
    assert budget.tool_calls == 2
    assert CALLS == ["read_training_calendar:3"]


async def test_tool_runtime_exception_propagates() -> None:
    budget = FakeBudget()
    model = ScriptedToolModel(
        [
            _tool_call("read_recent_workouts", {}),
            AIMessage(content="不会到达"),
        ]
    )

    with pytest.raises(RuntimeError) as excinfo:
        await _run(_harness(), budget, model)

    assert type(excinfo.value) is RuntimeError
    assert str(excinfo.value) == "查询训练记录失败"
    assert budget.tool_calls == 1
    assert len(model.calls) == 1


async def test_model_request_budget_exhausted_terminates_run() -> None:
    budget = FakeBudget(requests_exhausted=True)
    model = ScriptedToolModel([AIMessage(content="不会到达")])

    with pytest.raises(ModelRequestBudgetExceeded):
        await _run(_harness(), budget, model)

    assert budget.requests == 0
    assert budget.tool_calls == 0
    assert model.calls == []
    assert CALLS == []


async def test_tool_call_budget_exhausted_terminates_run() -> None:
    budget = FakeBudget(tool_calls_exhausted=True)
    model = ScriptedToolModel(
        [
            _tool_call("read_training_calendar", {"day": 3}),
            AIMessage(content="不会到达"),
        ]
    )

    with pytest.raises(ToolCallBudgetExceeded):
        await _run(_harness(), budget, model)

    assert budget.requests == 1
    assert budget.tool_calls == 0
    assert len(model.calls) == 1
    assert CALLS == []


async def test_parallel_read_only_tool_calls_execute_together() -> None:
    budget = FakeBudget()
    model = ScriptedToolModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_active_plan", "args": {}, "id": "call-1"},
                    {"name": "read_progress", "args": {}, "id": "call-2"},
                ],
            ),
            AIMessage(content="都读到了"),
        ]
    )

    messages = await _run(_harness(), budget, model)

    tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
    assert [message.tool_call_id for message in tool_messages] == ["call-1", "call-2"]
    assert [message.content for message in tool_messages] == ["active plan ok", "progress ok"]
    assert PROBE.arrived == ["read_active_plan", "read_progress"]
    assert budget.tool_calls == 2


async def test_tool_timeout_terminates_run_without_a_tool_message() -> None:
    # 工具自身慢于配置的超时：astream 的 updates 里不含 tools 节点产出，即 TimeoutError 前无 ToolMessage。
    budget = FakeBudget()
    model = ScriptedToolModel(
        [
            _tool_call("read_slow_progress", {}),
            AIMessage(content="不会到达"),
        ]
    )
    updates: list[dict[str, Any]] = []

    with pytest.raises(TimeoutError):
        async for update in _harness(timeout_seconds=0.01).astream(
            {"messages": [HumanMessage(content="今天练什么")]},
            context=_context(budget, model),
            stream_mode="updates",
        ):
            updates.append(update)

    assert budget.tool_calls == 1
    assert not any("tools" in update for update in updates)
    assert len(model.calls) == 1


async def test_model_request_timeout_terminates_run() -> None:
    budget = FakeBudget(request_timeout=0.01)
    model = ScriptedToolModel([AIMessage(content="迟到")], delay_seconds=5)

    with pytest.raises(TimeoutError):
        await _run(_harness(), budget, model)

    assert budget.requests == 1
    assert budget.tool_calls == 0


async def test_model_response_must_be_aimessage() -> None:
    budget = FakeBudget()
    model = ScriptedToolModel([HumanMessage(content="不是 AIMessage")])

    with pytest.raises(InvalidModelResponse, match="不是 AIMessage"):
        await _run(_harness(), budget, model)

    assert budget.requests == 1
