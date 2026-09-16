"""Stage 2 Subtask 04 §B：月历与单次日程状态的确定性现算。

依据：``refactor-log/stage2.md`` §8／§11.4、``refactor-log/stage2-subTasks/04-trends-calendar-and-stats-api.md``
§B／§最小验证、``LANGGRAPH_REFACTOR_PLAN.md`` §6.3、``Fit-Agent-LangGraph-重构讨论总结.md`` §9。

覆盖：只读取当前 active 计划的日程（draft／archived 不进入月历）、取消／未完成／已完成三种状态、
完成由关联训练现算而不新增字段、实际训练按 ``performed_on`` 落天、跨日与跨月关联时计划事实与训练事实
分别落在各自日期、额外训练（``plan_session_id IS NULL``）显示为普通训练日且不完成任何日程、
空白日期不生成条目、同一日程最多完成一次、月份边界按 stdlib 月天数复算、输出不含完成率字段。

计划与日程按要求用直接 SQL 写入（正式计划创建／激活入口留 Stage 5）；训练一律经
``WorkoutRecordsService`` 走真实写入与关联校验路径。测试只用 pytest ``tmp_path`` 下的独立临时库。
"""

from dataclasses import fields
from datetime import date
from pathlib import Path
from typing import get_args

import pytest

from domain.records.schema import WorkoutSetInput
from domain.records.service import PlanSessionLinkUnavailable, WorkoutRecordsService
from domain.stats.schema import (
    CalendarDay,
    CalendarMonth,
    CalendarPlanSession,
    CalendarSessionStatus,
    WorkoutFact,
)
from domain.stats.service import StatsService
from storage.db import Database

SQUAT = "barbell-back-squat"
SQUAT_CONVENTION = "barbell_includes_bar_total"
CREATED_AT = "2026-06-01T08:00:00+08:00"
CANCELLED_AT = "2026-06-01T08:00:00+08:00"


async def _migrated(path: Path) -> Database:
    db = Database(path)
    await db.open()
    await db.migrate()
    return db


async def _insert_plan(db: Database, *, version: int, status: str) -> int:
    """直接 SQL 写入一个计划版本行，返回计划身份。"""
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "INSERT INTO plans (version, status, structured_content, created_at,"
            " confirmed_at) VALUES (?, ?, '{}', ?, ?)",
            (version, status, CREATED_AT, CREATED_AT if status == "active" else None),
        )
        try:
            return int(cursor.lastrowid or 0)
        finally:
            await cursor.close()


async def _insert_plan_session(
    db: Database, plan_id: int, scheduled_on: date, cancelled_at: str | None = None
) -> int:
    """直接 SQL 写入一条计划日程行，返回日程身份。"""
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "INSERT INTO plan_sessions (plan_id, scheduled_on, cancelled_at)"
            " VALUES (?, ?, ?)",
            (plan_id, scheduled_on.isoformat(), cancelled_at),
        )
        try:
            return int(cursor.lastrowid or 0)
        finally:
            await cursor.close()


async def _calendar(db: Database, year: int, month: int) -> CalendarMonth:
    return await StatsService(db).calendar_month(year, month)


def _squat(weight_kg: float, reps: int = 5) -> WorkoutSetInput:
    return WorkoutSetInput(
        exercise_id=SQUAT,
        set_no=1,
        reps=reps,
        set_type="work",
        load_convention=SQUAT_CONVENTION,
        weight_kg=weight_kg,
    )


def _days_by_date(month: CalendarMonth) -> dict[date, CalendarDay]:
    return {day.date: day for day in month.days}


# ---------- 只读 active 计划 ----------


async def test_calendar_reads_only_the_active_plan(tmp_path: Path) -> None:
    """只有当前 active 计划的日程进入月历：draft 与 archived 计划的日程不显示。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        draft = await _insert_plan(db, version=1, status="draft")
        await _insert_plan_session(db, draft, date(2026, 6, 3))
        archived = await _insert_plan(db, version=2, status="archived")
        await _insert_plan_session(db, archived, date(2026, 6, 4))
        active = await _insert_plan(db, version=3, status="active")
        active_session = await _insert_plan_session(db, active, date(2026, 6, 5))

        month = await _calendar(db, 2026, 6)
        assert month.month == "2026-06"
        assert (month.from_on, month.to_on) == (date(2026, 6, 1), date(2026, 6, 30))
        assert [day.date for day in month.days] == [date(2026, 6, 5)]
        assert month.days[0].plan_sessions == (
            CalendarPlanSession(
                plan_session_id=active_session,
                scheduled_on=date(2026, 6, 5),
                status="incomplete",
                workout_session_id=None,
                actual_performed_on=None,
            ),
        )
        assert month.days[0].workouts == ()
    finally:
        await db.close()


async def test_calendar_without_active_plan_shows_only_workouts(tmp_path: Path) -> None:
    """没有 active 计划时月历只剩实际训练：draft 日程不显示，额外训练照常显示。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        draft = await _insert_plan(db, version=1, status="draft")
        await _insert_plan_session(db, draft, date(2026, 6, 3))
        workout = await WorkoutRecordsService(db).create(date(2026, 6, 10), (_squat(60.0),))

        month = await _calendar(db, 2026, 6)
        assert [day.date for day in month.days] == [date(2026, 6, 10)]
        assert month.days[0].plan_sessions == ()
        assert month.days[0].workouts == (
            WorkoutFact(
                workout_session_id=workout.id,
                performed_on=date(2026, 6, 10),
                plan_session_id=None,
            ),
        )
    finally:
        await db.close()


# ---------- 单次日程状态 ----------


async def test_calendar_session_statuses_cancelled_incomplete_and_complete(
    tmp_path: Path,
) -> None:
    """三种单次日程状态：已取消（取消时间）、未完成（无关联训练）、已完成（有关联训练）。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        active = await _insert_plan(db, version=1, status="active")
        cancelled = await _insert_plan_session(
            db, active, date(2026, 6, 3), cancelled_at=CANCELLED_AT
        )
        pending = await _insert_plan_session(db, active, date(2026, 6, 4))
        done = await _insert_plan_session(db, active, date(2026, 6, 5))
        workout = await WorkoutRecordsService(db).create(
            date(2026, 6, 5), (_squat(100.0),), plan_session_id=done
        )

        days = _days_by_date(await _calendar(db, 2026, 6))
        assert [session.status for session in days[date(2026, 6, 3)].plan_sessions] == [
            "cancelled"
        ]
        assert days[date(2026, 6, 3)].plan_sessions[0].plan_session_id == cancelled
        assert [session.status for session in days[date(2026, 6, 4)].plan_sessions] == [
            "incomplete"
        ]
        assert days[date(2026, 6, 4)].plan_sessions[0].plan_session_id == pending
        completed = days[date(2026, 6, 5)].plan_sessions[0]
        assert completed.status == "complete"
        # 完成由关联训练现算：同时带出关联训练身份与真实发生日期。
        assert (completed.workout_session_id, completed.actual_performed_on) == (
            workout.id,
            date(2026, 6, 5),
        )
    finally:
        await db.close()


async def test_calendar_keeps_scheduled_and_performed_facts_on_their_own_dates(
    tmp_path: Path,
) -> None:
    """跨日（含跨月）关联：计划状态落在 ``scheduled_on``，实际训练落在 ``performed_on``。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        active = await _insert_plan(db, version=1, status="active")
        session = await _insert_plan_session(db, active, date(2026, 6, 30))
        workout = await WorkoutRecordsService(db).create(
            date(2026, 7, 1), (_squat(100.0),), plan_session_id=session
        )

        june = await _calendar(db, 2026, 6)
        assert [day.date for day in june.days] == [date(2026, 6, 30)]
        planned = june.days[0].plan_sessions[0]
        assert (
            planned.status,
            planned.workout_session_id,
            planned.actual_performed_on,
        ) == ("complete", workout.id, date(2026, 7, 1))
        assert june.days[0].workouts == ()  # 训练事实不在计划日出现

        july = await _calendar(db, 2026, 7)
        assert [day.date for day in july.days] == [date(2026, 7, 1)]
        assert july.days[0].plan_sessions == ()  # 日程属于 6/30，不重复落在 7 月
        assert july.days[0].workouts == (
            WorkoutFact(
                workout_session_id=workout.id,
                performed_on=date(2026, 7, 1),
                plan_session_id=session,
            ),
        )
    finally:
        await db.close()


async def test_extra_workout_is_a_training_day_and_completes_no_session(
    tmp_path: Path,
) -> None:
    """额外训练（``plan_session_id IS NULL``）显示为普通训练日，不完成任何计划日程。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        active = await _insert_plan(db, version=1, status="active")
        session = await _insert_plan_session(db, active, date(2026, 6, 16))
        extra = await WorkoutRecordsService(db).create(date(2026, 6, 15), (_squat(60.0),))

        days = _days_by_date(await _calendar(db, 2026, 6))
        assert days[date(2026, 6, 15)].plan_sessions == ()
        assert days[date(2026, 6, 15)].workouts == (
            WorkoutFact(
                workout_session_id=extra.id,
                performed_on=date(2026, 6, 15),
                plan_session_id=None,
            ),
        )
        # 当天的日程仍未被完成（额外训练不占用名额）。
        assert days[date(2026, 6, 16)].plan_sessions[0].plan_session_id == session
        assert days[date(2026, 6, 16)].plan_sessions[0].status == "incomplete"
    finally:
        await db.close()


async def test_plan_session_can_be_completed_at_most_once(tmp_path: Path) -> None:
    """同一日程最多完成一次：第二次关联被拒绝，月历仍指向首次完成的训练。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        active = await _insert_plan(db, version=1, status="active")
        session = await _insert_plan_session(db, active, date(2026, 6, 5))
        records = WorkoutRecordsService(db)
        first = await records.create(
            date(2026, 6, 5), (_squat(100.0),), plan_session_id=session
        )

        with pytest.raises(PlanSessionLinkUnavailable):
            await records.create(
                date(2026, 6, 5), (_squat(90.0),), plan_session_id=session
            )

        days = _days_by_date(await _calendar(db, 2026, 6))
        assert days[date(2026, 6, 5)].plan_sessions[0].workout_session_id == first.id
    finally:
        await db.close()


# ---------- 日期边界与输出口径 ----------


async def test_calendar_days_only_include_dates_with_facts(tmp_path: Path) -> None:
    """只有真实发生计划的日期才出条目：没有训练也没有日程的日期不生成「休息日」。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        active = await _insert_plan(db, version=1, status="active")
        await _insert_plan_session(db, active, date(2026, 6, 1), cancelled_at=CANCELLED_AT)
        await _insert_plan_session(db, active, date(2026, 6, 20))
        await WorkoutRecordsService(db).create(date(2026, 6, 10), (_squat(60.0),))

        month = await _calendar(db, 2026, 6)
        assert [day.date for day in month.days] == [
            date(2026, 6, 1),
            date(2026, 6, 10),
            date(2026, 6, 20),
        ]
        # 6/1 只有已取消的日程：仍然出条目，但没有任何训练事实。
        assert month.days[0].workouts == ()
    finally:
        await db.close()


async def test_calendar_month_bounds_follow_the_real_month_length(
    tmp_path: Path,
) -> None:
    """月份边界按 stdlib 月天数复算（含闰年 2 月），跨月边界的日程不串月。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        active = await _insert_plan(db, version=1, status="active")
        await _insert_plan_session(db, active, date(2028, 2, 29))

        leap = await _calendar(db, 2028, 2)
        assert leap.month == "2028-02"
        assert (leap.from_on, leap.to_on) == (date(2028, 2, 1), date(2028, 2, 29))
        assert [day.date for day in leap.days] == [date(2028, 2, 29)]

        common = await _calendar(db, 2027, 2)
        assert (common.from_on, common.to_on) == (date(2027, 2, 1), date(2027, 2, 28))
        assert common.days == ()
    finally:
        await db.close()


def test_calendar_output_has_no_completion_rate_or_rest_day_fields() -> None:
    """输出结构固定：只有单次日程状态与实际训练事实，没有完成率或「休息日」字段。"""
    assert get_args(CalendarSessionStatus) == ("cancelled", "incomplete", "complete")
    assert {field.name for field in fields(CalendarMonth)} == {
        "month",
        "from_on",
        "to_on",
        "days",
    }
    assert {field.name for field in fields(CalendarDay)} == {
        "date",
        "plan_sessions",
        "workouts",
    }
    assert {field.name for field in fields(CalendarPlanSession)} == {
        "plan_session_id",
        "scheduled_on",
        "status",
        "workout_session_id",
        "actual_performed_on",
    }
    assert {field.name for field in fields(WorkoutFact)} == {
        "workout_session_id",
        "performed_on",
        "plan_session_id",
    }
    forbidden = ("rate", "ratio", "percent", "rest", "completion")
    names = [
        field.name
        for model in (CalendarMonth, CalendarDay, CalendarPlanSession, WorkoutFact)
        for field in fields(model)
    ]
    assert not [name for name in names if any(word in name for word in forbidden)]
