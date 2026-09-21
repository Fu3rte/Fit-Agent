"""统计只读端点的查询参数与响应 DTO。"""

from datetime import date
from typing import Annotated, Any

from fastapi import Query

from app.domain.stats.schema import (
    CalendarMonth,
    MetricChange,
    PersonalBest,
    TrendReport,
    WorkoutFact,
    WorkoutGap,
)

#: 月历月份参数：严格的 ``YYYY-MM``（年份首位不得为 0）。
_MONTH_PATTERN = r"^[1-9][0-9]{3}-(0[1-9]|1[0-2])$"

MonthQuery = Annotated[str, Query(pattern=_MONTH_PATTERN)]


def personal_best_dto(pb: PersonalBest) -> dict[str, Any]:
    """一条现算 PB → 传输对象：数值、适用负重口径与重量，加来源训练／组序号／日期。"""
    return {
        "exercise_id": pb.exercise_id,
        "exercise_name": pb.exercise_name,
        "pb_type": pb.pb_type,
        "value": pb.value,
        "load_convention": pb.load_convention,
        "weight_kg": pb.weight_kg,
        "workout_session_id": pb.workout_session_id,
        "set_no": pb.set_no,
        "performed_on": pb.performed_on.isoformat(),
    }


def metric_change_dto(change: MetricChange) -> dict[str, Any]:
    """体重／体脂最近两条记录的变化；状态不是 ``ok`` 时取值字段全为 null（不补 0）。"""
    return {
        "status": change.status,
        "current": change.current,
        "current_on": _optional_iso(change.current_on),
        "previous": change.previous,
        "previous_on": _optional_iso(change.previous_on),
        "change": change.change,
    }


def workout_gap_dto(gap: WorkoutGap) -> dict[str, Any]:
    """距上次训练天数；没有训练历史时状态为 ``no_data`` 且天数为 null。"""
    return {
        "status": gap.status,
        "days": gap.days,
        "last_performed_on": _optional_iso(gap.last_performed_on),
    }


def trends_dto(report: TrendReport) -> dict[str, Any]:
    """趋势报告 → 传输对象。"""
    return {
        "window_days": report.window_days,
        "from": report.from_on.isoformat(),
        "to": report.to_on.isoformat(),
        "weight": [
            {"measured_on": point.measured_on.isoformat(), "value": point.value}
            for point in report.weight
        ],
        "body_fat": [
            {"measured_on": point.measured_on.isoformat(), "value": point.value}
            for point in report.body_fat
        ],
        "strength": [
            {
                "exercise_id": trend.exercise_id,
                "exercise_name": trend.exercise_name,
                "pb_type": trend.pb_type,
                "load_convention": trend.load_convention,
                "weight_kg": trend.weight_kg,
                "points": [
                    {
                        "performed_on": point.performed_on.isoformat(),
                        "value": point.value,
                    }
                    for point in trend.points
                ],
            }
            for trend in report.strength
        ],
        "trend_summary": {
            "weight_change": metric_change_dto(report.trend_summary.weight_change),
            "body_fat_change": metric_change_dto(report.trend_summary.body_fat_change),
            "days_since_last_workout": workout_gap_dto(
                report.trend_summary.days_since_last_workout
            ),
        },
    }


def calendar_dto(month: CalendarMonth) -> dict[str, Any]:
    """月历 → 传输对象：只含有事实的日期。"""
    return {
        "month": month.month,
        "from": month.from_on.isoformat(),
        "to": month.to_on.isoformat(),
        "days": [
            {
                "date": day.date.isoformat(),
                "plan_sessions": [
                    {
                        "id": session.plan_session_id,
                        "scheduled_on": session.scheduled_on.isoformat(),
                        "status": session.status,
                        "workout_session_id": session.workout_session_id,
                        "actual_performed_on": _optional_iso(
                            session.actual_performed_on
                        ),
                    }
                    for session in day.plan_sessions
                ],
                "workouts": [workout_fact_dto(fact) for fact in day.workouts],
            }
            for day in month.days
        ],
    }


def workout_fact_dto(fact: WorkoutFact) -> dict[str, Any]:
    """一次实际训练事实 → 传输对象；``plan_session_id`` 为 null 即额外训练。"""
    return {
        "id": fact.workout_session_id,
        "performed_on": fact.performed_on.isoformat(),
        "plan_session_id": fact.plan_session_id,
    }


def _optional_iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()
