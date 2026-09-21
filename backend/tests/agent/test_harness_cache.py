# 阶段 6 缓存契约：命中/失效由参数、依赖 namespace revision、业务日与 schema_version 决定；
# 只有成功的 ToolMessage 进缓存，命中时按当前 request.tool_call 重建消息；容量有界；关闭缓存时与阶段 5 一致。
# 依据：06-cache-observability.md「缓存键／不缓存的结果／缓存存储／测试」。

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt.tool_node import ToolCallRequest, ToolRuntime

from app.application.agent.harness.bounded import TRUNCATION_NOTICE
from app.application.agent.harness.cache import ToolResultCache
from app.application.agent.harness.declaration import HarnessContext, HarnessState
from app.application.agent.harness.graph import build_tool_harness
from app.application.agent.harness.policy import build_tool_call_wrapper
from app.application.agent.harness.tools.training import (
    PROGRESS_TOOLS,
    SCHEDULE_TOOLS,
    ReadTrainingCalendarArgs,
)
from app.application.ports import ModelGateway
from app.infrastructure.database.connection import Database
from app.infrastructure.database.repositories.tool_cache_repository import (
    ToolCacheRevisionsRepo,
)

BUSINESS_DAY = date(2026, 6, 1)
NEXT_BUSINESS_DAY = date(2026, 6, 2)
TOOL_TIMEOUT_SECONDS = 5.0

#: 生产装配的四个只读工具按名索引：键由它们的公开参数 Schema 决定。
CACHED_TOOLS: dict[str, BaseTool] = {
    tool.name: tool for tool in (*SCHEDULE_TOOLS, *PROGRESS_TOOLS)
}

#: 真实 ToolNode 下每次真实执行日程工具的记录：命中缓存时不再增长。
CALENDAR_EFFECTS: list[tuple[int, int]] = []


@dataclass
class FakeBudget:
    """替身工具预算：只记录扣减次数。"""

    tool_calls: int = 0

    def begin_request(self) -> float:
        return TOOL_TIMEOUT_SECONDS

    def take_tool_call(self) -> None:
        self.tool_calls += 1


@dataclass
class ScriptedToolModel:
    """替身模型入口：只实现 tools 形态，按序返回脚本化消息。"""

    responses: list[AIMessage]

    async def tools(self, messages: Any, offered: Any) -> AIMessage:
        return self.responses.pop(0)


@tool("read_training_calendar", args_schema=ReadTrainingCalendarArgs)
async def counting_calendar(
    year: int,
    month: int,
    runtime: ToolRuntime[HarnessContext, HarnessState],
) -> str:
    """替身日程工具：沿用生产参数 Schema，只记录真实执行。"""
    CALENDAR_EFFECTS.append((year, month))
    return f"{year}-{month:02d}"


@dataclass(frozen=True, slots=True)
class FakeContext:
    """缓存需要的运行时上下文：工具预算与业务日。"""

    budget: FakeBudget
    business_day: date


class RecordingExecute:
    """替身 execute：记录调用次数与最后一次 request，返回脚本化结果或抛出脚本化异常。"""

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls = 0
        self.requests: list[ToolCallRequest] = []

    async def __call__(self, request: ToolCallRequest) -> ToolMessage:
        self.calls += 1
        self.requests.append(request)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if callable(self.outcome):
            return self.outcome(request)
        return self.outcome


def _request(
    name: str,
    args: dict[str, Any],
    *,
    call_id: str,
    context: FakeContext,
) -> ToolCallRequest:
    runtime: ToolRuntime[FakeContext, Any] = ToolRuntime(
        state={},
        context=context,
        config={},
        stream_writer=lambda *_args: None,
        tool_call_id=call_id,
        store=None,
    )
    return ToolCallRequest(
        tool_call={"name": name, "args": args, "id": call_id},
        tool=CACHED_TOOLS.get(name),
        state={},
        runtime=runtime,
    )


def _message(request: ToolCallRequest, content: str, *, status: str = "success") -> ToolMessage:
    return ToolMessage(
        content=content,
        status=status,
        tool_call_id=request.tool_call["id"],
        name=request.tool_call["name"],
    )


def _tool_call(args: dict[str, Any], *, call_id: str) -> AIMessage:
    """脚本化一次日程工具调用。"""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "read_training_calendar",
                "args": args,
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


@dataclass(frozen=True, slots=True)
class CacheHarness:
    db: Database
    revisions: ToolCacheRevisionsRepo
    cache: ToolResultCache
    context: FakeContext

    def wrapper(self, *, max_bytes: int | None = None):
        if max_bytes is None:
            return build_tool_call_wrapper(timeout_seconds=5.0, cache=self.cache)
        return build_tool_call_wrapper(
            timeout_seconds=5.0, max_bytes=max_bytes, cache=self.cache
        )

    async def bump(self, namespace: str) -> int:
        async with self.db.transaction() as conn:
            return await self.revisions.bump_in_transaction(conn, namespace)


@asynccontextmanager
async def _harness(
    tmp_path: Path, *, capacity: int = 128
) -> AsyncIterator[CacheHarness]:
    db = Database(tmp_path / "fit_agent.db")
    await db.open()
    await db.migrate()
    try:
        revisions = ToolCacheRevisionsRepo(db)
        yield CacheHarness(
            db=db,
            revisions=revisions,
            cache=ToolResultCache(
                revisions=revisions, schema_version=5, capacity=capacity
            ),
            context=FakeContext(budget=FakeBudget(), business_day=BUSINESS_DAY),
        )
    finally:
        await db.close()


async def test_same_args_and_revision_hit(tmp_path: Path) -> None:
    async with _harness(tmp_path) as h:
        execute = RecordingExecute(
            lambda request: _message(request, '{"active_plan": null}')
        )
        wrapper = h.wrapper()
        first = await wrapper(
            _request("read_active_plan", {}, call_id="call-1", context=h.context), execute
        )
        second = await wrapper(
            _request("read_active_plan", {}, call_id="call-2", context=h.context), execute
        )

        assert execute.calls == 1
        assert second.content == first.content
        assert h.context.budget.tool_calls == 2


async def test_changed_args_miss(tmp_path: Path) -> None:
    async with _harness(tmp_path) as h:
        execute = RecordingExecute(lambda request: _message(request, "records"))
        wrapper = h.wrapper()
        await wrapper(
            _request("read_training_history", {"limit": 4}, call_id="call-1", context=h.context),
            execute,
        )
        await wrapper(
            _request("read_training_history", {"limit": 5}, call_id="call-2", context=h.context),
            execute,
        )

        assert execute.calls == 2


async def test_revision_bump_invalidates(tmp_path: Path) -> None:
    async with _harness(tmp_path) as h:
        execute = RecordingExecute(lambda request: _message(request, "workouts"))
        wrapper = h.wrapper()
        await wrapper(
            _request("read_training_history", {"limit": 4}, call_id="call-1", context=h.context),
            execute,
        )
        assert await h.bump("workouts") == 1
        await wrapper(
            _request("read_training_history", {"limit": 4}, call_id="call-2", context=h.context),
            execute,
        )

        assert execute.calls == 2


async def test_business_day_dimension_only_applies_to_dependent_tools(tmp_path: Path) -> None:
    async with _harness(tmp_path) as h:
        execute = RecordingExecute(lambda request: _message(request, "payload"))
        wrapper = h.wrapper()
        later = FakeContext(budget=FakeBudget(), business_day=NEXT_BUSINESS_DAY)

        for name in ("read_active_plan", "read_progress"):
            first = await wrapper(
                _request(name, {}, call_id="call-1", context=h.context), execute
            )
            same_day = await wrapper(
                _request(name, {}, call_id="call-2", context=h.context), execute
            )
            before = execute.calls
            await wrapper(_request(name, {}, call_id="call-3", context=later), execute)
            assert same_day.content == first.content
            assert execute.calls == before + 1

        for name, args in (
            ("read_training_calendar", {"year": 2026, "month": 6}),
            ("read_training_history", {"limit": 4}),
        ):
            execute.calls = 0
            await wrapper(_request(name, args, call_id="call-1", context=h.context), execute)
            await wrapper(_request(name, args, call_id="call-2", context=later), execute)
            assert execute.calls == 1


async def test_schema_version_dimension_only_applies_to_catalog_dependents(tmp_path: Path) -> None:
    async with _harness(tmp_path) as h:
        other = ToolResultCache(
            revisions=h.revisions, schema_version=6, capacity=h.cache.capacity
        )
        plan = _request("read_active_plan", {}, call_id="call-1", context=h.context)
        recent = _request(
            "read_training_history", {"limit": 4}, call_id="call-2", context=h.context
        )

        assert await h.cache.key_for(plan) != await other.key_for(plan)
        assert await h.cache.key_for(recent) == await other.key_for(recent)


async def test_hit_rebuilds_tool_message_with_current_call_identity(tmp_path: Path) -> None:
    async with _harness(tmp_path) as h:
        execute = RecordingExecute(lambda request: _message(request, "payload"))
        wrapper = h.wrapper()
        await wrapper(
            _request("read_active_plan", {}, call_id="call-old", context=h.context), execute
        )
        hit = await wrapper(
            _request("read_active_plan", {}, call_id="call-new", context=h.context), execute
        )

        assert isinstance(hit, ToolMessage)
        assert hit.tool_call_id == "call-new"
        assert hit.name == "read_active_plan"
        assert "call-old" not in repr(hit)


async def test_error_result_is_not_cached(tmp_path: Path) -> None:
    async with _harness(tmp_path) as h:
        execute = RecordingExecute(
            lambda request: _message(request, "boom", status="error")
        )
        wrapper = h.wrapper()
        first = await wrapper(
            _request("read_active_plan", {}, call_id="call-1", context=h.context), execute
        )
        second = await wrapper(
            _request("read_active_plan", {}, call_id="call-2", context=h.context), execute
        )

        assert execute.calls == 2
        assert first.status == second.status == "error"


async def test_cancellation_is_not_cached(tmp_path: Path) -> None:
    async with _harness(tmp_path) as h:
        execute = RecordingExecute(asyncio.CancelledError())
        wrapper = h.wrapper()
        with pytest.raises(asyncio.CancelledError):
            await wrapper(
                _request("read_active_plan", {}, call_id="call-1", context=h.context), execute
            )
        with pytest.raises(asyncio.CancelledError):
            await wrapper(
                _request("read_active_plan", {}, call_id="call-2", context=h.context), execute
            )

        assert execute.calls == 2


async def test_execution_failure_is_not_cached(tmp_path: Path) -> None:
    async with _harness(tmp_path) as h:
        execute = RecordingExecute(RuntimeError("数据库异常"))
        wrapper = h.wrapper()
        with pytest.raises(RuntimeError):
            await wrapper(
                _request("read_active_plan", {}, call_id="call-1", context=h.context), execute
            )
        with pytest.raises(RuntimeError):
            await wrapper(
                _request("read_active_plan", {}, call_id="call-2", context=h.context), execute
            )

        assert execute.calls == 2


async def test_tool_outside_dimension_table_is_never_cached(tmp_path: Path) -> None:
    async with _harness(tmp_path) as h:
        execute = RecordingExecute(lambda request: _message(request, "written"))
        wrapper = h.wrapper()
        await wrapper(
            _request("create_workout_record", {}, call_id="call-1", context=h.context), execute
        )
        await wrapper(
            _request("create_workout_record", {}, call_id="call-2", context=h.context), execute
        )

        assert execute.calls == 2
        assert len(h.cache) == 0


async def test_lru_capacity_is_bounded(tmp_path: Path) -> None:
    async with _harness(tmp_path, capacity=2) as h:
        execute = RecordingExecute(lambda request: _message(request, "payload"))
        wrapper = h.wrapper()
        for index, limit in enumerate((1, 2, 3)):
            await wrapper(
                _request(
                    "read_training_history",
                    {"limit": limit},
                    call_id=f"call-{index}",
                    context=h.context,
                ),
                execute,
            )

        assert len(h.cache) == 2
        await wrapper(
            _request("read_training_history", {"limit": 1}, call_id="call-9", context=h.context),
            execute,
        )
        assert execute.calls == 4


async def test_truncated_result_is_cached_and_logged_without_payload(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async with _harness(tmp_path) as h:
        payload = "隐私字段" * 200
        execute = RecordingExecute(lambda request: _message(request, payload))
        wrapper = h.wrapper(max_bytes=256)
        with caplog.at_level(
            logging.INFO, logger="app.application.agent.harness.policy"
        ):
            bounded = await wrapper(
                _request("read_active_plan", {}, call_id="call-1", context=h.context), execute
            )
            second = await wrapper(
                _request("read_active_plan", {}, call_id="call-2", context=h.context), execute
            )

        assert isinstance(bounded, ToolMessage)
        assert bounded.content.endswith(TRUNCATION_NOTICE)
        assert second.content == bounded.content
        assert execute.calls == 1
        assert "cache_hit=True" in caplog.text
        assert "隐私字段" not in caplog.text


async def test_concurrent_calls_return_consistent_results(tmp_path: Path) -> None:
    async with _harness(tmp_path) as h:
        execute = RecordingExecute(lambda request: _message(request, "payload"))
        wrapper = h.wrapper()

        results = await asyncio.gather(
            *(
                wrapper(
                    _request(
                        "read_active_plan", {}, call_id=f"call-{index}", context=h.context
                    ),
                    execute,
                )
                for index in range(4)
            )
        )

        assert {message.content for message in results} == {"payload"}
        assert execute.calls <= 4


async def test_key_comes_from_validated_args(tmp_path: Path) -> None:
    """键取工具公开参数 Schema 的校验结果：Schema 默认值补齐，类型归一。"""
    async with _harness(tmp_path) as h:
        empty = _request(
            "read_training_history", {}, call_id="call-1", context=h.context
        )
        explicit = _request(
            "read_training_history", {"limit": 4}, call_id="call-2", context=h.context
        )
        text = _request(
            "read_training_history", {"limit": "4"}, call_id="call-3", context=h.context
        )

        assert await h.cache.key_for(empty) == await h.cache.key_for(explicit)
        assert await h.cache.key_for(text) == await h.cache.key_for(explicit)


async def test_real_tool_node_hit_reuses_message_with_current_call_identity(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """真实 build_tool_harness／ToolNode ＋ 真实缓存下，语义相同（仅类型不同）的第二次调用命中缓存。"""
    CALENDAR_EFFECTS.clear()
    async with _harness(tmp_path) as h:
        graph = build_tool_harness(
            [counting_calendar],
            timeout_seconds=TOOL_TIMEOUT_SECONDS,
            cache=h.cache,
        )
        model = ScriptedToolModel(
            [
                _tool_call({"year": 2026, "month": "6"}, call_id="call-1"),
                _tool_call({"year": 2026, "month": 6}, call_id="call-2"),
                AIMessage(content="这个月六次训练"),
            ]
        )
        with caplog.at_level(
            logging.INFO, logger="app.application.agent.harness.policy"
        ):
            state = await graph.ainvoke(
                {"messages": [HumanMessage(content="这个月练了什么")]},
                context=HarnessContext(
                    model=cast(ModelGateway, model), budget=FakeBudget()
                ),
            )

    messages = [
        message for message in state["messages"] if isinstance(message, ToolMessage)
    ]
    assert [message.tool_call_id for message in messages] == ["call-1", "call-2"]
    assert [message.name for message in messages] == [
        "read_training_calendar",
        "read_training_calendar",
    ]
    assert messages[0].content == messages[-1].content == "2026-06"
    assert CALENDAR_EFFECTS == [(2026, 6)]
    assert "cache_hit=True" in caplog.text


async def test_real_tool_node_rejects_undeclared_args_after_cache_warmup(
    tmp_path: Path,
) -> None:
    """缓存已预热时，含未声明字段的调用仍走 ToolNode 的参数校验错误，不被缓存命中吞掉。"""
    CALENDAR_EFFECTS.clear()
    async with _harness(tmp_path) as h:
        graph = build_tool_harness(
            [counting_calendar],
            timeout_seconds=TOOL_TIMEOUT_SECONDS,
            cache=h.cache,
        )
        model = ScriptedToolModel(
            [
                _tool_call({"year": 2026, "month": 6}, call_id="call-1"),
                _tool_call(
                    {"year": 2026, "month": 6, "business_day": "2026-06-01"},
                    call_id="call-2",
                ),
                AIMessage(content="这个月六次训练"),
            ]
        )
        state = await graph.ainvoke(
            {"messages": [HumanMessage(content="这个月练了什么")]},
            context=HarnessContext(model=cast(ModelGateway, model), budget=FakeBudget()),
        )
        assert len(h.cache) == 1

    messages = [
        message for message in state["messages"] if isinstance(message, ToolMessage)
    ]
    assert [message.status for message in messages] == ["success", "error"]
    assert messages[0].content == "2026-06"
    assert "business_day" in messages[1].content
    assert "Extra inputs are not permitted" in messages[1].content
    assert CALENDAR_EFFECTS == [(2026, 6)]


async def test_disabled_cache_behaves_like_uncached_harness(tmp_path: Path) -> None:
    async with _harness(tmp_path) as h:
        execute = RecordingExecute(lambda request: _message(request, "payload"))
        wrapper = build_tool_call_wrapper(timeout_seconds=5.0)
        first = await wrapper(
            _request("read_active_plan", {}, call_id="call-1", context=h.context), execute
        )
        second = await wrapper(
            _request("read_active_plan", {}, call_id="call-2", context=h.context), execute
        )

        assert execute.calls == 2
        assert isinstance(first, ToolMessage)
        assert first.tool_call_id == "call-1"
        assert second.tool_call_id == "call-2"
        assert len(h.cache) == 0
