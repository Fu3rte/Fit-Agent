"""Stage 2 Subtask 04 §A：趋势与趋势摘要的确定性现算。

依据：``refactor-log/stage2.md`` §7／§11.3、``refactor-log/stage2-subTasks/04-trends-calendar-and-stats-api.md``
§A／§最小验证、``LANGGRAPH_REFACTOR_PLAN.md`` §6.4、``Fit-Agent-LangGraph-重构讨论总结.md`` §3.2／§3.3。

覆盖：最近 30 天窗口两端与只返回真实原始点（体脂未记录的行不进曲线、不补 0）、力量趋势为截至各日期
的累计 PB 且按日期不下降（累计含窗口之前的历史，可由固定数据库输入复算）、外加重量／纯自重／计时三种
系列互不混算、热身与辅助组与不完整组不进曲线、``no_data``／``insufficient_data`` 状态、
用注入的业务日期复算停训天数、输出不含训练容量／完成率／效果评价、修改与删除记录后曲线重算、
趋势查询不落表也不建 View。

训练一律经 ``WorkoutRecordsService`` 走真实写入路径；领域层拒绝的不完整组（时长为 0）用直接 SQL 写入。
测试只使用 pytest ``tmp_path`` 下的独立临时库。
"""

from dataclasses import fields
from datetime import date
from pathlib import Path
from typing import Any, get_args

import pytest

from domain.body_metrics.service import BodyMetricsService
from domain.records.schema import SetType, WorkoutSetInput
from domain.records.service import WorkoutRecordsService
from domain.stats.schema import (
    PERSONAL_BEST_TYPES,
    TREND_WINDOW_DAYS,
    MetricChange,
    StrengthTrend,
    TrendReport,
    TrendStatus,
    TrendSummary,
    WorkoutGap,
)
from domain.stats.service import StatsService
from storage.db import Database

#: 业务日期固定注入：趋势窗口与停训天数都不从系统时钟取「今天」。
BUSINESS_DAY = date(2026, 6, 30)

SQUAT = "barbell-back-squat"  # 杠铃背蹲：外加重量（杠铃总重）
SQUAT_CONVENTION = "barbell_includes_bar_total"
PULL_UP = "pull-up"  # 纯自重引体
WEIGHTED_PULL_UP = "weighted-pull-up"  # 独立负重引体：只记外加重量
EXTERNAL_CONVENTION = "external_added_weight"
PLANK = "plank"  # 平板支撑：计时


async def _migrated(path: Path) -> Database:
    db = Database(path)
    await db.open()
    await db.migrate()
    return db


async def _trends(db: Database, business_day: date = BUSINESS_DAY) -> TrendReport:
    return await StatsService(db).trends(business_day)


def _series(
    report: TrendReport,
    exercise_id: str,
    pb_type: str,
    weight_kg: float | None = None,
) -> StrengthTrend:
    """按（动作、PB 类型、适用重量）取唯一一个力量趋势系列；不存在或重复即测试失败。"""
    matches = [
        trend
        for trend in report.strength
        if trend.exercise_id == exercise_id
        and trend.pb_type == pb_type
        and trend.weight_kg == weight_kg
    ]
    assert len(matches) == 1, matches
    return matches[0]


def _points(trend: StrengthTrend) -> list[tuple[date, int | float]]:
    return [(point.performed_on, point.value) for point in trend.points]


def _squat(
    set_no: int, weight_kg: float, reps: int, set_type: SetType = "work"
) -> WorkoutSetInput:
    return WorkoutSetInput(
        exercise_id=SQUAT,
        set_no=set_no,
        reps=reps,
        set_type=set_type,
        load_convention=SQUAT_CONVENTION,
        weight_kg=weight_kg,
    )


def _weighted_pull_up(
    set_no: int, weight_kg: float, reps: int, set_type: SetType = "work"
) -> WorkoutSetInput:
    return WorkoutSetInput(
        exercise_id=WEIGHTED_PULL_UP,
        set_no=set_no,
        reps=reps,
        set_type=set_type,
        load_convention=EXTERNAL_CONVENTION,
        weight_kg=weight_kg,
    )


def _pull_up(set_no: int, reps: int, set_type: SetType = "work") -> WorkoutSetInput:
    return WorkoutSetInput(
        exercise_id=PULL_UP, set_no=set_no, reps=reps, set_type=set_type
    )


def _timed(
    exercise_id: str, set_no: int, duration_seconds: int, set_type: SetType = "work"
) -> WorkoutSetInput:
    return WorkoutSetInput(
        exercise_id=exercise_id,
        set_no=set_no,
        reps=None,
        set_type=set_type,
        duration_seconds=duration_seconds,
    )


async def _raw_session(db: Database, session_id: int, performed_on: date) -> None:
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO workout_sessions (id, performed_on) VALUES (?, ?)",
            (session_id, performed_on.isoformat()),
        )


async def _raw_set(
    db: Database,
    session_id: int,
    exercise_id: str,
    set_no: int,
    *,
    set_type: str = "work",
    duration_seconds: int | None = None,
) -> None:
    """绕过领域写入一个领域层拒绝的不完整行（计时时长为 0）。"""
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO workout_sets (workout_session_id, exercise_id, set_no, set_type,"
            " reps, duration_seconds) VALUES (?, ?, ?, ?, NULL, ?)",
            (session_id, exercise_id, set_no, set_type, duration_seconds),
        )


async def _schema(db: Database) -> dict[str, set[str]]:
    """库内对象清单（按 sqlite_master 类型分组）：用于核对趋势现算不落表、不建 View。"""

    async def op(conn: Any) -> dict[str, set[str]]:
        async with conn.execute("SELECT type, name FROM sqlite_master") as cursor:
            rows = await cursor.fetchall()
        schema: dict[str, set[str]] = {}
        for row in rows:
            schema.setdefault(str(row["type"]), set()).add(str(row["name"]))
        return schema

    return await db.under_lock(op)


# ---------- 原始点与窗口 ----------


async def test_trend_points_cover_the_last_30_days_only(tmp_path: Path) -> None:
    """窗口为含业务日期在内的最近 30 个自然日；窗口外的记录不进点，体脂未记录不补 0。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        metrics = BodyMetricsService(db)
        await metrics.create(date(2026, 5, 31), 82.0, 21.0)  # 窗口前一天：不返回
        await metrics.create(date(2026, 6, 1), 81.5, None)  # 窗口首日边界
        await metrics.create(date(2026, 6, 30), 80.0, 19.5)  # 业务日期当天：窗口末日边界

        report = await _trends(db)
        assert report.window_days == 30
        assert (report.from_on, report.to_on) == (date(2026, 6, 1), BUSINESS_DAY)
        assert [(point.measured_on, point.value) for point in report.weight] == [
            (date(2026, 6, 1), 81.5),
            (date(2026, 6, 30), 80.0),
        ]
        # 6/1 未记录体脂：该日不进体脂曲线，也不以 0 或上一个值顶替。
        assert [(point.measured_on, point.value) for point in report.body_fat] == [
            (date(2026, 6, 30), 19.5)
        ]
    finally:
        await db.close()


async def test_trend_query_creates_no_result_table_or_view(tmp_path: Path) -> None:
    """趋势现算不落表：查询前后 Schema 不变，仍无统计结果表与有效工作组 View。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        await WorkoutRecordsService(db).create(BUSINESS_DAY, (_squat(1, 100.0, 5),))
        before = await _schema(db)
        report = await _trends(db)
        assert report.strength != ()
        assert await _schema(db) == before
        assert before.get("view", set()) == set()
    finally:
        await db.close()


# ---------- 力量趋势 ----------


async def test_strength_trend_is_cumulative_and_never_decreases(tmp_path: Path) -> None:
    """力量趋势是截至各日期的累计 PB：含窗口之前的历史，曲线按日期不下降。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        records = WorkoutRecordsService(db)
        await records.create(date(2026, 5, 20), (_squat(1, 120.0, 3),))  # 窗口前的最好成绩
        await records.create(date(2026, 6, 2), (_squat(1, 100.0, 5),))
        await records.create(date(2026, 6, 5), (_squat(1, 90.0, 8),))  # 更轻：不刷新
        await records.create(date(2026, 6, 9), (_squat(1, 130.0, 1),))

        trend = _series(await _trends(db), SQUAT, "weight_pb")
        assert _points(trend) == [
            (date(2026, 6, 2), 120.0),  # 截至 6/2 的历史最好成绩含 5/20 的 120kg
            (date(2026, 6, 5), 120.0),
            (date(2026, 6, 9), 130.0),
        ]
        values = [value for _, value in _points(trend)]
        assert values == sorted(values)  # 可由固定数据库输入复算：120 → 120 → 130
        # 窗口外的 5/20 不生成点，但仍参与累计值。
        assert date(2026, 5, 20) not in [day for day, _ in _points(trend)]
    finally:
        await db.close()


async def test_strength_trend_covers_weight_reps_and_duration_series(
    tmp_path: Path,
) -> None:
    """三种记录口径各自的系列互不混算：外加重量重量／同重量次数、纯自重次数、计时秒数。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        records = WorkoutRecordsService(db)
        await records.create(date(2026, 6, 2), (_weighted_pull_up(1, 10.0, 6),))
        await records.create(
            date(2026, 6, 6), (_weighted_pull_up(1, 10.0, 9), _timed(PLANK, 1, 45))
        )
        await records.create(date(2026, 6, 8), (_pull_up(1, 8), _timed(PLANK, 1, 60)))

        report = await _trends(db)
        assert {
            (trend.exercise_id, trend.pb_type, trend.weight_kg)
            for trend in report.strength
        } == {
            (WEIGHTED_PULL_UP, "weight_pb", None),
            (WEIGHTED_PULL_UP, "reps_pb", 10.0),
            (PULL_UP, "reps_pb", None),
            (PLANK, "duration_pb", None),
        }
        assert _points(_series(report, WEIGHTED_PULL_UP, "weight_pb")) == [
            (date(2026, 6, 2), 10.0),
            (date(2026, 6, 6), 10.0),
        ]
        assert _points(_series(report, WEIGHTED_PULL_UP, "reps_pb", 10.0)) == [
            (date(2026, 6, 2), 6),
            (date(2026, 6, 6), 9),
        ]
        assert _points(_series(report, PULL_UP, "reps_pb")) == [(date(2026, 6, 8), 8)]
        assert _points(_series(report, PLANK, "duration_pb")) == [
            (date(2026, 6, 6), 45),
            (date(2026, 6, 8), 60),
        ]
        assert _series(report, WEIGHTED_PULL_UP, "reps_pb", 10.0).load_convention == (
            EXTERNAL_CONVENTION
        )
    finally:
        await db.close()


async def test_strength_trend_excludes_warmup_assisted_and_incomplete_sets(
    tmp_path: Path,
) -> None:
    """热身组、辅助组与不完整组不进曲线：它们复用与 PB 同一份有效工作组过滤口径。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        await WorkoutRecordsService(db).create(
            date(2026, 6, 2),
            (
                _squat(1, 100.0, 5),
                _squat(2, 200.0, 1, "warmup"),  # 更重但不算
                _squat(3, 180.0, 2, "assisted"),  # 借力组不算
            ),
        )
        await _raw_session(db, 99, date(2026, 6, 3))
        await _raw_set(db, 99, PLANK, 1, duration_seconds=0)  # 计时时长为 0：无效组

        report = await _trends(db)
        assert _points(_series(report, SQUAT, "weight_pb")) == [(date(2026, 6, 2), 100.0)]
        assert not [trend for trend in report.strength if trend.exercise_id == PLANK]
    finally:
        await db.close()


async def test_strength_trend_recomputes_after_update_and_delete(tmp_path: Path) -> None:
    """修改或删除训练记录后曲线立即重算：趋势与 PB 一样不落表、不缓存。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        records = WorkoutRecordsService(db)
        session = await records.create(date(2026, 6, 2), (_squat(1, 100.0, 5),))
        assert _points(_series(await _trends(db), SQUAT, "weight_pb")) == [
            (date(2026, 6, 2), 100.0)
        ]

        await records.update(session.id, date(2026, 6, 20), (_squat(1, 70.0, 3),))
        assert _points(_series(await _trends(db), SQUAT, "weight_pb")) == [
            (date(2026, 6, 20), 70.0)
        ]

        await records.delete(session.id)
        assert (await _trends(db)).strength == ()
    finally:
        await db.close()


# ---------- 趋势摘要 ----------


async def test_trend_summary_uses_the_latest_two_weight_and_body_fat_records(
    tmp_path: Path,
) -> None:
    """体重／体脂变化取最近两条记录：体脂未记录的行不参与，差值与两次日期可复算。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        metrics = BodyMetricsService(db)
        await metrics.create(date(2026, 6, 1), 82.0, 21.0)
        await metrics.create(date(2026, 6, 10), 81.0, None)  # 未记录体脂：跳过它
        await metrics.create(date(2026, 6, 20), 80.5, 20.0)

        summary = (await _trends(db)).trend_summary
        assert summary.weight_change.status == "ok"
        assert summary.weight_change.current == 80.5
        assert summary.weight_change.current_on == date(2026, 6, 20)
        assert summary.weight_change.previous == 81.0
        assert summary.weight_change.previous_on == date(2026, 6, 10)
        assert summary.weight_change.change == pytest.approx(-0.5)

        assert summary.body_fat_change.status == "ok"
        assert summary.body_fat_change.current == pytest.approx(20.0)
        assert summary.body_fat_change.current_on == date(2026, 6, 20)
        assert summary.body_fat_change.previous == pytest.approx(21.0)
        assert summary.body_fat_change.previous_on == date(2026, 6, 1)
        assert summary.body_fat_change.change == pytest.approx(-1.0)
    finally:
        await db.close()


async def test_trend_summary_is_not_limited_to_the_30_day_window(tmp_path: Path) -> None:
    """摘要比的是最近两条记录，不受 30 天窗口限制：窗口外的历史同样参与变化值。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        metrics = BodyMetricsService(db)
        await metrics.create(date(2026, 4, 1), 85.0, None)
        await metrics.create(date(2026, 4, 20), 83.0, None)

        report = await _trends(db)
        assert report.weight == ()  # 原始点为空：窗口内没有记录
        assert report.trend_summary.weight_change.status == "ok"
        assert report.trend_summary.weight_change.change == pytest.approx(-2.0)
        assert report.trend_summary.body_fat_change.status == "no_data"
    finally:
        await db.close()


async def test_trend_summary_reports_no_data_and_insufficient_data(
    tmp_path: Path,
) -> None:
    """无记录返回 no_data，仅一条返回 insufficient_data：不给当前值、不补 0。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        empty = (await _trends(db)).trend_summary
        assert empty.weight_change == MetricChange(
            status="no_data",
            current=None,
            current_on=None,
            previous=None,
            previous_on=None,
            change=None,
        )
        assert empty.body_fat_change.status == "no_data"
        assert empty.days_since_last_workout == WorkoutGap(
            status="no_data", days=None, last_performed_on=None
        )

        metrics = BodyMetricsService(db)
        await metrics.create(date(2026, 6, 20), 80.5, 20.0)
        single = (await _trends(db)).trend_summary
        assert single.weight_change.status == "insufficient_data"
        assert single.weight_change.current is None
        assert single.weight_change.previous is None
        assert single.weight_change.change is None
        assert single.body_fat_change.status == "insufficient_data"
    finally:
        await db.close()


async def test_trend_summary_days_since_last_workout_uses_injected_business_date(
    tmp_path: Path,
) -> None:
    """停训天数由注入的业务日期与最近 performed_on 复算，且不受 30 天窗口限制。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        records = WorkoutRecordsService(db)
        await records.create(date(2026, 6, 24), (_squat(1, 100.0, 5),))

        gap = (await _trends(db)).trend_summary.days_since_last_workout
        assert gap.status == "ok"
        assert gap.days == 6  # 2026-06-30 - 2026-06-24
        assert gap.last_performed_on == date(2026, 6, 24)
        earlier = (await _trends(db, date(2026, 6, 25))).trend_summary
        assert earlier.days_since_last_workout.days == 1  # 业务日期由调用方注入，不取系统时钟

        # 窗口外的训练不生成趋势点，但仍是「最近一次训练」，停训天数照旧复算。
        await records.create(date(2026, 4, 1), (_squat(1, 90.0, 5),))
        report = await _trends(db)
        assert [day for day, _ in _points(_series(report, SQUAT, "weight_pb"))] == [
            date(2026, 6, 24)
        ]
        assert report.trend_summary.days_since_last_workout.days == 6
    finally:
        await db.close()


# ---------- 输出口径 ----------


def test_trend_output_has_no_volume_completion_rate_or_evaluation() -> None:
    """输出结构固定且不含训练容量、计划完成率、估算 1RM 或效果评价字段。"""
    assert TREND_WINDOW_DAYS == 30
    assert PERSONAL_BEST_TYPES == ("weight_pb", "reps_pb", "duration_pb")
    assert get_args(TrendStatus) == ("ok", "no_data", "insufficient_data")
    assert {field.name for field in fields(TrendReport)} == {
        "window_days",
        "from_on",
        "to_on",
        "weight",
        "body_fat",
        "strength",
        "trend_summary",
    }
    assert {field.name for field in fields(TrendSummary)} == {
        "weight_change",
        "body_fat_change",
        "days_since_last_workout",
    }
    assert {field.name for field in fields(MetricChange)} == {
        "status",
        "current",
        "current_on",
        "previous",
        "previous_on",
        "change",
    }
    assert {field.name for field in fields(WorkoutGap)} == {
        "status",
        "days",
        "last_performed_on",
    }
    assert {field.name for field in fields(StrengthTrend)} == {
        "exercise_id",
        "exercise_name",
        "pb_type",
        "load_convention",
        "weight_kg",
        "points",
    }
    forbidden = ("volume", "completion", "1rm", "progress", "evaluation")
    names = [
        field.name
        for model in (TrendReport, TrendSummary, MetricChange, WorkoutGap, StrengthTrend)
        for field in fields(model)
    ]
    assert not [name for name in names if any(word in name for word in forbidden)]
