# 只读工具分支的端到端行为：view_schedule／view_progress 在安全检查与路由分类之后调用对应 harness，
# 工具结果来自真实 ToolNode 与真实迁移库，最终文本仍走既有 message／done，事件闭集不变。
# 依据：05-graph-integration.md §4-§8；路由 Schema 与其余分支的回归在 test_router_schema.py 与
# test_agent_run_branches.py。

import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.application.agent.budget import (
    ModelRequestBudget,
    ModelRequestBudgetExceeded,
    ToolCallBudgetExceeded,
)
from app.application.agent.contracts import thread_config
from app.application.agent.harness.tools.training import (
    PROGRESS_TOOLS,
    SCHEDULE_TOOLS,
)
from app.application.agent.prompts import TOOL_HARNESS_SYSTEM_PROMPT
from app.application.agent.run_service import stream_agent_run
from app.bootstrap import SqliteHealthProbe, build_repositories, build_services
from app.domain.conversations.context import ContextMessage
from app.domain.records.schema import WorkoutSetInput
from app.infrastructure.database.connection import Database
from app.infrastructure.database.repositories.plans_repository import PlanRepo
from config import MAX_TOOL_CALLS_PER_RUN
from tests.agent.test_agent_run_branches import (
    ANSWER,
    BUSINESS_DAY,
    CONVERSATION_ID,
    HarnessCall,
    _harness,
    _insert_active_plan,
    _route,
    _row_counts,
    _schedule_plan_content,
    final_answer,
    tool_call,
)

SCHEDULE_TOOL_NAMES = tuple(tool.name for tool in SCHEDULE_TOOLS)
PROGRESS_TOOL_NAMES = tuple(tool.name for tool in PROGRESS_TOOLS)
CALENDAR_DATE = BUSINESS_DAY.isoformat()
NEXT_DATE = "2026-06-02"
COVERAGE_END = "2026-06-07"
PULL_UP = "pull-up"
SCHEDULE_REQUEST = "今天训练什么？"
REQUEST_TEXTS = (
    "今天练什么？",
    "明天练什么？",
    "后天练什么？",
    "周五练什么？",
    "这个月有哪些训练？",
)


def _route_schedule(**fields) -> dict:
    """一次日程查询运行：Router 结构化响应落在 ``workout_execution/query/schedule_query``。"""
    return _route(
        domain="workout_execution",
        action="query",
        execution_type="schedule_query",
        **fields,
    )


def _tool_message(call: HarnessCall) -> ToolMessage:
    """一次 harness 调用里工具执行后的最后一条消息。"""
    last = call.messages[-1]
    assert isinstance(last, ToolMessage), type(last).__name__
    return last


async def _seed_plan_sessions(
    db: Database, plan_id: int, days: Sequence[date]
) -> None:
    """按训练日写入计划日程（真实 PlanRepo 写原语，外层事务由测试持有）。"""
    async with db.transaction() as conn:
        await PlanRepo(db).create_sessions_in_transaction(
            conn, plan_id, scheduled_on=days
        )


def test_default_tool_budget_matches_the_run_constant() -> None:
    """共享预算的默认上限就是 config 常量，工具调用与模型请求各自独立计数。"""
    budget = ModelRequestBudget()

    assert budget.max_tool_calls == MAX_TOOL_CALLS_PER_RUN
    assert budget.tool_calls == 0 and budget.used == 0
    for _ in range(MAX_TOOL_CALLS_PER_RUN):
        budget.take_tool_call()
    with pytest.raises(ToolCallBudgetExceeded):
        budget.take_tool_call()

    assert budget.tool_calls == MAX_TOOL_CALLS_PER_RUN
    assert budget.used == 0


async def test_schedule_query_answers_from_the_real_active_plan(tmp_path: Path) -> None:
    """``view_schedule``：模型调 read_active_plan，工具读到真实计划事实，最终文本落既有 message。"""
    async with _harness(
        tmp_path,
        structured=_route_schedule(),
        harness=[tool_call("read_active_plan"), final_answer(ANSWER)],
    ) as h:
        await _insert_active_plan(h.db, _schedule_plan_content())
        before = await _row_counts(h.db)
        result = await h.invoke(SCHEDULE_REQUEST)

        assert result.intent == "view_schedule"
        assert result.messages == (ANSWER,)
        assert result.draft_plan_id is None
        assert [call.offered for call in h.model.harness_calls] == [
            SCHEDULE_TOOL_NAMES
        ] * 2
        payload = json.loads(_tool_message(h.model.harness_calls[1]).content)
        assert payload["business_day"] == CALENDAR_DATE
        assert payload["active_plan"]["coverage"] == {
            "starts_on": CALENDAR_DATE,
            "ends_on": COVERAGE_END,
        }
        assert [
            day["scheduled_on"] for day in payload["active_plan"]["training_days"]
        ] == [CALENDAR_DATE, NEXT_DATE]
        assert await _row_counts(h.db) == before


async def test_schedule_query_without_an_active_plan_reads_null(tmp_path: Path) -> None:
    """没有 active 计划：工具明确给出 null，最终文本仍走既有 message／done。"""
    async with _harness(
        tmp_path,
        structured=_route_schedule(),
        harness=[tool_call("read_active_plan"), final_answer(ANSWER)],
    ) as h:
        result = await h.invoke(SCHEDULE_REQUEST)
        payload = json.loads(_tool_message(h.model.harness_calls[1]).content)

        assert payload["active_plan"] is None
        assert result.messages == (ANSWER,)


async def test_calendar_month_query_reads_the_month_facts(tmp_path: Path) -> None:
    """月历查询：read_training_calendar 读取指定自然月的真实日程与训练事实。"""
    async with _harness(
        tmp_path,
        structured=_route_schedule(),
        harness=[
            tool_call("read_training_calendar", {"year": 2026, "month": 6}),
            final_answer(ANSWER),
        ],
    ) as h:
        plan_id = await _insert_active_plan(h.db, _schedule_plan_content())
        await _seed_plan_sessions(h.db, plan_id, (BUSINESS_DAY,))
        before = await _row_counts(h.db)
        result = await h.invoke("这个月有哪些训练？")
        calendar = json.loads(_tool_message(h.model.harness_calls[1]).content)

        assert result.messages == (ANSWER,)
        assert calendar["month"] == "2026-06"
        assert (calendar["from_on"], calendar["to_on"]) == (
            CALENDAR_DATE,
            "2026-06-30",
        )
        assert [
            (day["date"], len(day["plan_sessions"])) for day in calendar["days"]
        ] == [(CALENDAR_DATE, 1)]
        assert await _row_counts(h.db) == before


async def test_progress_query_offers_only_the_progress_tools(tmp_path: Path) -> None:
    """``view_progress``：只提供进展侧工具，PB 与趋势摘要仍由既有 ``StatsService`` 现算。"""
    async with _harness(
        tmp_path,
        structured=_route(domain="analytics", action="query"),
        harness=[tool_call("read_progress"), final_answer(ANSWER)],
    ) as h:
        before = await _row_counts(h.db)
        result = await h.invoke("我上次练了什么？")

        assert result.intent == "view_progress"
        assert [call.offered for call in h.model.harness_calls] == [
            PROGRESS_TOOL_NAMES
        ] * 2
        payload = json.loads(_tool_message(h.model.harness_calls[1]).content)
        assert set(payload) == {"business_day", "personal_bests", "trend_summary"}
        assert payload["business_day"] == CALENDAR_DATE
        assert payload["personal_bests"] == []
        assert set(payload["trend_summary"]) == {
            "weight_change",
            "body_fat_change",
            "days_since_last_workout",
        }
        assert await _row_counts(h.db) == before


async def test_recent_workout_query_reads_the_written_session(tmp_path: Path) -> None:
    """最近训练查询：read_recent_workouts 按给定 limit 读取既有训练事实。"""
    async with _harness(
        tmp_path,
        structured=_route(domain="analytics", action="query"),
        harness=[
            tool_call("read_recent_workouts", {"limit": 2}),
            final_answer(ANSWER),
        ],
    ) as h:
        services = build_services(
            build_repositories(h.db),
            h.db,
            SqliteHealthProbe(h.db, h.db.path.parent),
        )
        await services.records.create(
            BUSINESS_DAY,
            (WorkoutSetInput(exercise_id=PULL_UP, set_no=1, set_type="work", reps=8),),
        )
        before = await _row_counts(h.db)
        result = await h.invoke("我最近练了什么？")
        sessions = json.loads(_tool_message(h.model.harness_calls[1]).content)

        assert result.messages == (ANSWER,)
        assert [
            (session["performed_on"], session["plan_session_id"])
            for session in sessions
        ] == [(CALENDAR_DATE, None)]
        assert sessions[0]["sets"][0]["reps"] == 8
        assert await _row_counts(h.db) == before


async def test_the_system_message_carries_business_day_and_the_tool_rules(
    tmp_path: Path,
) -> None:
    """工具路径的 system prompt 带当前业务日、必须调工具、只能依据工具结果、coverage 与无数据规则。"""
    async with _harness(
        tmp_path,
        structured=_route_schedule(),
        harness=[final_answer(ANSWER)],
    ) as h:
        await h.invoke(SCHEDULE_REQUEST)
        system = h.model.harness_calls[0].messages[0]

        assert isinstance(system, SystemMessage)
        assert system.content == TOOL_HARNESS_SYSTEM_PROMPT.format(
            business_day=CALENDAR_DATE
        )
        for rule in (
            "必须调用工具",
            "不得编造",
            "coverage 之外不得推断为休息日",
            "明确说明没有数据",
        ):
            assert rule in system.content


@pytest.mark.parametrize("request_text", REQUEST_TEXTS)
async def test_each_schedule_phrasing_reaches_the_harness_once(
    tmp_path: Path, request_text: str
) -> None:
    """今天／明天／后天／星期与月历问法都进同一个 harness，原始请求在消息序列里只出现一次。"""
    async with _harness(
        tmp_path,
        structured=_route_schedule(),
        harness=[final_answer(ANSWER)],
    ) as h:
        result = await h.invoke(request_text)
        messages = h.model.harness_calls[0].messages

        assert result.messages == (ANSWER,)
        assert isinstance(messages[-1], HumanMessage)
        assert messages[-1].content == request_text
        assert [message.content for message in messages].count(request_text) == 1
        assert not any(isinstance(message, ToolMessage) for message in messages)


async def test_history_joins_the_harness_messages_once(tmp_path: Path) -> None:
    """历史上下文参与工具选择：已重建的历史在 system 之后、当前请求之前各出现一次。"""
    history = (
        ContextMessage(role="user", text="上一轮问题"),
        ContextMessage(role="assistant", text="上一轮回答"),
    )
    async with _harness(
        tmp_path,
        structured=_route_schedule(),
        harness=[tool_call("read_active_plan"), final_answer(ANSWER)],
    ) as h:
        result = await h.invoke(SCHEDULE_REQUEST, history=history)
        messages = h.model.harness_calls[0].messages

        assert result.messages == (ANSWER,)
        assert [type(message).__name__ for message in messages] == [
            "SystemMessage",
            "HumanMessage",
            "AIMessage",
            "HumanMessage",
        ]
        assert [message.content for message in messages[1:3]] == [
            "上一轮问题",
            "上一轮回答",
        ]
        assert [message.content for message in messages].count(SCHEDULE_REQUEST) == 1


async def test_streamed_frames_stay_in_the_closed_event_set(tmp_path: Path) -> None:
    """SSE 闭集不变：工具回答只产生既有 node／message／done，内部 ToolMessage 不成为事件。"""
    async with _harness(
        tmp_path,
        structured=_route_schedule(),
        harness=[tool_call("read_active_plan"), final_answer(ANSWER)],
    ) as h:
        events = [
            event
            async for event in stream_agent_run(
                h.graph,
                {"conversation_id": CONVERSATION_ID, "request": SCHEDULE_REQUEST},
                thread_config(CONVERSATION_ID),
                h.run_context(),
                h.run_deps,
            )
        ]

        assert [event.event for event in events] == ["node", "message", "done"]
        assert events[0].data == {"name": "view_schedule"}
        assert events[1].data == {"text": ANSWER}
        assert events[2].data == {
            "ok": True,
            "intent": "view_schedule",
            "termination_reason": None,
            "draft_plan_id": None,
        }


async def test_tool_calls_share_the_run_budget(tmp_path: Path) -> None:
    """工具调用与模型请求共享同一预算对象：工具次数上限即终止 Run，第二个工具不执行。"""
    budget = ModelRequestBudget(max_tool_calls=1)
    both_tools = AIMessage(
        content="",
        tool_calls=[
            {"name": "read_active_plan", "args": {}, "id": "call-1", "type": "tool_call"},
            {
                "name": "read_training_calendar",
                "args": {"year": 2026, "month": 6},
                "id": "call-2",
                "type": "tool_call",
            },
        ],
    )
    async with _harness(
        tmp_path,
        structured=_route_schedule(),
        harness=[both_tools],
    ) as h:
        await _insert_active_plan(h.db, _schedule_plan_content())
        before = await _row_counts(h.db)

        with pytest.raises(ToolCallBudgetExceeded):
            await h.invoke(SCHEDULE_REQUEST, budget=budget)

        assert budget.tool_calls == 1
        assert budget.used == 2
        assert await _row_counts(h.db) == before


async def test_model_request_budget_still_bounds_the_tool_loop(tmp_path: Path) -> None:
    """共享预算的模型请求侧不变：路由已用掉唯一一次请求时，harness 的模型调用即终止 Run。"""
    budget = ModelRequestBudget(max_requests=1)
    async with _harness(
        tmp_path,
        structured=_route_schedule(),
        harness=[final_answer(ANSWER)],
    ) as h:
        with pytest.raises(ModelRequestBudgetExceeded):
            await h.invoke(SCHEDULE_REQUEST, budget=budget)

        assert budget.used == 1
        assert h.model.harness_calls == []
        assert len(h.model.harness_scripts) == 1
