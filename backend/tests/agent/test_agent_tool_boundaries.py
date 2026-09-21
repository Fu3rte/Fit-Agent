# 阶段 7 的跨阶段回归补充：# coverage 内休息日问法、coverage 外日期（active 计划已过期）、下个月月历问法
# 与无训练记录走真实迁移库与真实 Agent Run，最终仍落既有 message／done；draft 唯一性的库级兜底在此补上执行证据。
# 依据：07-validation.md §2 必测用户问题的日程与边界两组、§3「draft 唯一性」；
# 03-read-tools.md「覆盖范围外的日期不标记为休息日」；storage/migrations/003_rejected_plan_status.sql:54 的部分唯一索引。
# 工具分支的常规行为在 test_agent_tool_branches.py，这里只补它没有执行证据的问法与边界。

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.bootstrap import SqliteHealthProbe, build_repositories, build_services
from app.domain.plans.schema import PlanDraft
from tests.agent.test_agent_run_branches import (
    ANSWER,
    BUSINESS_DAY,
    _harness,
    _insert_active_plan,
    _route,
    _row_counts,
    _schedule_plan_content,
    final_answer,
    tool_call,
)
from tests.agent.test_agent_tool_branches import _route_schedule, _tool_message
from tests.integration.test_tool_cache_revisions import (
    CREATED_AT,
    _draft_content,
    _evaluation,
)
from tests.integration.test_tool_cache_revisions import _harness as _plan_harness

COVERAGE_END = "2026-06-07"
REST_DAY_QUESTION = "三天后是不是休息日？"
DATED_QUESTION = "2026-06-07 练什么？"
NEXT_MONTH_QUESTION = "下个月有哪些训练？"
RECENT_WORKOUT_QUESTION = "最近四次训练内容"


def _expired_plan_content() -> dict[str, Any]:
    """coverage 已结束的 active 计划：全部日期前移七天，业务日落在 coverage 之外。"""
    content = _schedule_plan_content()
    content["starts_on"] = _shift_days(content["starts_on"], -7)
    for day in content["training_days"]:
        day["scheduled_on"] = _shift_days(day["scheduled_on"], -7)
    return content


def _shift_days(value: str, days: int) -> str:
    return (date.fromisoformat(value) + timedelta(days=days)).isoformat()


@pytest.mark.parametrize("request_text", [REST_DAY_QUESTION, DATED_QUESTION])
async def test_coverage_relative_schedule_questions_read_the_same_window(
    tmp_path: Path, request_text: str
) -> None:
    """「三天后是否休息日」「指定日期练什么」都靠 coverage 与训练日事实回答，行程外日期不落休息日。"""
    async with _harness(
        tmp_path,
        structured=_route_schedule(),
        harness=[tool_call("read_active_plan"), final_answer(ANSWER)],
    ) as h:
        await _insert_active_plan(h.db, _schedule_plan_content())
        before = await _row_counts(h.db)
        result = await h.invoke(request_text)
        payload = json.loads(_tool_message(h.model.harness_calls[1]).content)

        assert result.messages == (ANSWER,)
        assert payload["business_day"] == BUSINESS_DAY.isoformat()
        assert payload["active_plan"]["coverage"] == {
            "starts_on": BUSINESS_DAY.isoformat(),
            "ends_on": COVERAGE_END,
        }
        scheduled = [
            day["scheduled_on"] for day in payload["active_plan"]["training_days"]
        ]
        assert scheduled == ["2026-06-01", "2026-06-02"]
        assert COVERAGE_END not in scheduled
        assert await _row_counts(h.db) == before


async def test_expired_active_plan_reports_coverage_outside_the_business_day(
    tmp_path: Path,
) -> None:
    """active 计划已过期：coverage 原样给出且整段早于业务日，业务日不被读成训练日或休息日。"""
    async with _harness(
        tmp_path,
        structured=_route_schedule(),
        harness=[tool_call("read_active_plan"), final_answer(ANSWER)],
    ) as h:
        await _insert_active_plan(h.db, _expired_plan_content())
        before = await _row_counts(h.db)
        result = await h.invoke("今天练什么？")
        payload = json.loads(_tool_message(h.model.harness_calls[1]).content)

        assert result.messages == (ANSWER,)
        assert payload["business_day"] == BUSINESS_DAY.isoformat()
        plan = payload["active_plan"]
        assert plan["coverage"] == {"starts_on": "2026-05-25", "ends_on": "2026-05-31"}
        assert [
            day["scheduled_on"] for day in plan["training_days"]
        ] == ["2026-05-25", "2026-05-26"]
        assert BUSINESS_DAY.isoformat() not in {
            day["scheduled_on"] for day in plan["training_days"]
        }
        assert await _row_counts(h.db) == before


async def test_next_month_calendar_query_reads_the_next_month(tmp_path: Path) -> None:
    """「下个月有哪些训练」读下一个自然月的真实日程事实：没有计划日程与训练时只回空 days。"""
    async with _harness(
        tmp_path,
        structured=_route_schedule(),
        harness=[
            tool_call("read_training_calendar", {"year": 2026, "month": 7}),
            final_answer(ANSWER),
        ],
    ) as h:
        await _insert_active_plan(h.db, _schedule_plan_content())
        before = await _row_counts(h.db)
        result = await h.invoke(NEXT_MONTH_QUESTION)
        calendar = json.loads(_tool_message(h.model.harness_calls[1]).content)

        assert result.messages == (ANSWER,)
        assert calendar == {
            "month": "2026-07",
            "from_on": "2026-07-01",
            "to_on": "2026-07-31",
            "days": [],
        }
        assert await _row_counts(h.db) == before


async def test_recent_workout_query_without_records_returns_an_empty_list(
    tmp_path: Path,
) -> None:
    """没有训练记录：默认 limit 下读回空列表，两个只读 intent 的行程都不进入写路径。"""
    async with _harness(
        tmp_path,
        structured=_route(domain="analytics", action="query"),
        harness=[tool_call("read_training_history", {}), final_answer(ANSWER)],
    ) as h:
        before = await _row_counts(h.db)
        result = await h.invoke(RECENT_WORKOUT_QUESTION)
        workouts = json.loads(_tool_message(h.model.harness_calls[1]).content)

        assert isinstance(workouts, list)
        assert workouts == []
        assert result.messages == (ANSWER,)
        assert await _row_counts(h.db) == before


async def test_second_open_draft_is_rejected_by_the_database(tmp_path: Path) -> None:
    """draft 唯一性：已有 draft 时再次插入新 draft 被库级部分唯一索引拒绝，旧 draft 原样保留。"""
    async with _plan_harness(tmp_path) as h:
        persistence = build_services(
            build_repositories(h.db),
            h.db,
            SqliteHealthProbe(h.db, h.db.path.parent),
        ).plan_persistence
        draft = PlanDraft.model_validate(_draft_content())
        first = await persistence.persist_plan_result(
            draft,
            _evaluation(passed=True),
            existing_draft_id=None,
            created_at=CREATED_AT,
        )

        with pytest.raises(sqlite3.IntegrityError):
            await persistence.persist_plan_result(
                draft,
                _evaluation(passed=True),
                existing_draft_id=None,
                created_at=CREATED_AT,
            )

        assert (await _row_counts(h.db))["plans"] == 1
        kept = await persistence.get_unique_draft()
        assert kept is not None and kept.id == first.id
        assert kept.version == first.version
