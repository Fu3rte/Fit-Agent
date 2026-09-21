"""stats 确定性规则：有效工作组 → 三类 PB、力量趋势、趋势摘要与月历落天。"""

from calendar import monthrange
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TypeVar

from app.domain.actions.schema import LoadConvention
from app.domain.body_metrics.schema import BodyMetric
from app.domain.plans.schema import PlanSession
from app.domain.stats.schema import (
    PERSONAL_BEST_TYPES,
    TREND_WINDOW_DAYS,
    CalendarDay,
    CalendarPlanSession,
    CalendarSessionStatus,
    InvalidWorkSet,
    MetricChange,
    PersonalBest,
    PersonalBestType,
    StrengthPoint,
    StrengthTrend,
    TrendSummary,
    ValidWorkSet,
    WorkoutFact,
    WorkoutGap,
)

T = TypeVar("T")


def compute_personal_bests(facts: Sequence[ValidWorkSet]) -> tuple[PersonalBest, ...]:
    """把有效工作组归约成三类 PB。"""
    weighted: dict[tuple[str, LoadConvention | None], list[ValidWorkSet]] = {}
    bodyweight_reps: dict[str, list[ValidWorkSet]] = {}
    timed: dict[str, list[ValidWorkSet]] = {}

    for fact in facts:
        if fact.record_type == "reps_weight":
            weighted.setdefault((fact.exercise_id, fact.load_convention), []).append(
                fact
            )
        elif fact.record_type == "reps_bodyweight":
            bodyweight_reps.setdefault(fact.exercise_id, []).append(fact)
        else:
            timed.setdefault(fact.exercise_id, []).append(fact)

    bests: list[PersonalBest] = []
    for group in weighted.values():
        source, weight = _best_source(group, _weight_of)
        bests.append(_personal_best("weight_pb", source, weight, weight))
    for group in bodyweight_reps.values():
        source, reps = _best_source(group, _reps_of)
        bests.append(_personal_best("reps_pb", source, reps, None))
    for group in timed.values():
        source, seconds = _best_source(group, _duration_of)
        bests.append(_personal_best("duration_pb", source, seconds, None))

    return tuple(sorted(bests, key=_result_order))


def _best_source(
    group: Sequence[ValidWorkSet], measure: Callable[[ValidWorkSet], int | float]
) -> tuple[ValidWorkSet, int | float]:
    """取度量最大的组及其度量值；度量并列时取最先达成的来源。"""
    source = min(group, key=lambda fact: (-measure(fact), *_source_rank(fact)))
    return source, measure(source)


def _personal_best(
    pb_type: PersonalBestType,
    source: ValidWorkSet,
    value: int | float,
    weight_kg: float | None,
) -> PersonalBest:
    """由来源组事实与 PB 数值构造结果；负重口径取来源组自己的口径。"""
    return PersonalBest(
        exercise_id=source.exercise_id,
        exercise_name=source.exercise_name,
        pb_type=pb_type,
        value=value,
        load_convention=source.load_convention,
        weight_kg=weight_kg,
        workout_session_id=source.workout_session_id,
        set_no=source.set_no,
        performed_on=source.performed_on,
    )


def _source_rank(fact: ValidWorkSet) -> tuple[int, int, int]:
    """并列来源排序：来源训练日期 → 训练身份 → 组序号（升序即最先达成）。"""
    return (fact.performed_on.toordinal(), fact.workout_session_id, fact.set_no)


def _result_order(best: PersonalBest) -> tuple[str, int, float]:
    """稳定输出顺序：动作身份 → PB 类型（重量、次数、时长）→ 适用重量升序。"""
    return _order_key(best.exercise_id, best.pb_type, best.weight_kg)


def _order_key(
    exercise_id: str, pb_type: PersonalBestType, weight_kg: float | None
) -> tuple[str, int, float]:
    """PB 结果与力量趋势系列共用的输出顺序键。"""
    return (
        exercise_id,
        PERSONAL_BEST_TYPES.index(pb_type),
        -1.0 if weight_kg is None else weight_kg,
    )


def _required(value: T | None, label: str) -> T:
    """有效工作组按其记录口径必有的度量值；缺失即 repo 过滤口径与统计口径不一致。"""
    if value is None:
        raise InvalidWorkSet(f"有效工作组缺少{label}：repo 的过滤口径与统计口径不一致")
    return value


def _weight_of(fact: ValidWorkSet) -> float:
    """该有效工作组的重量（外加重量型必有）。"""
    return _required(fact.weight_kg, "重量")


def _reps_of(fact: ValidWorkSet) -> int:
    """该有效工作组的单组次数（外加重量型与纯自重型必有）。"""
    return _required(fact.reps, "次数")


def _duration_of(fact: ValidWorkSet) -> int:
    """该有效工作组的持续秒数（计时型必有）。"""
    return _required(fact.duration_seconds, "时长")


def trend_window(business_day: date) -> tuple[date, date]:
    """趋势窗口两端（含两端）：覆盖业务日期在内的最近 ``TREND_WINDOW_DAYS`` 个自然日。"""
    return business_day - timedelta(days=TREND_WINDOW_DAYS - 1), business_day


def month_bounds(year: int, month: int) -> tuple[date, date]:
    """``YYYY-MM`` 对应的自然月首末日期；月天数由 stdlib ``monthrange`` 给出（含闰年）。"""
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def compute_trend_summary(
    latest_weights: Sequence[BodyMetric],
    latest_body_fats: Sequence[BodyMetric],
    last_workout_on: date | None,
    business_day: date,
) -> TrendSummary:
    """把最近两条体重／体脂记录、最近一次训练日期与业务日期归约成摘要。"""
    return TrendSummary(
        weight_change=_metric_change(
            tuple((metric.measured_on, metric.weight_kg) for metric in latest_weights)
        ),
        body_fat_change=_metric_change(
            tuple(
                (metric.measured_on, metric.body_fat_pct)
                for metric in latest_body_fats
                if metric.body_fat_pct is not None
            )
        ),
        days_since_last_workout=_workout_gap(last_workout_on, business_day),
    )


def _metric_change(points: Sequence[tuple[date, float]]) -> MetricChange:
    """最近两条记录 → 变化（最新在前）；不足两条即无变化可言，不给当前值也不补 0。"""
    if not points:
        return MetricChange(
            status="no_data",
            current=None,
            current_on=None,
            previous=None,
            previous_on=None,
            change=None,
        )
    if len(points) == 1:
        return MetricChange(
            status="insufficient_data",
            current=None,
            current_on=None,
            previous=None,
            previous_on=None,
            change=None,
        )
    (current_on, current), (previous_on, previous) = points[0], points[1]
    return MetricChange(
        status="ok",
        current=current,
        current_on=current_on,
        previous=previous,
        previous_on=previous_on,
        change=current - previous,
    )


def _workout_gap(last_workout_on: date | None, business_day: date) -> WorkoutGap:
    """距上次训练天数；没有训练历史即 ``no_data``，不假装为 0 天。"""
    if last_workout_on is None:
        return WorkoutGap(status="no_data", days=None, last_performed_on=None)
    return WorkoutGap(
        status="ok",
        days=(business_day - last_workout_on).days,
        last_performed_on=last_workout_on,
    )


@dataclass(frozen=True, slots=True)
class _SeriesKey:
    """一个力量趋势系列的身份：动作身份 + PB 类型 + 负重口径。"""

    exercise_id: str
    exercise_name: str
    pb_type: PersonalBestType
    load_convention: LoadConvention | None


def compute_strength_trends(
    facts: Sequence[ValidWorkSet], from_on: date, to_on: date
) -> tuple[StrengthTrend, ...]:
    """把有效工作组归约成「截至各日期的累计 PB」系列。"""
    daily: dict[_SeriesKey, dict[date, int | float]] = {}
    for fact in facts:
        key, measure = _measures(fact)
        by_date = daily.setdefault(key, {})
        known = by_date.get(fact.performed_on)
        by_date[fact.performed_on] = (
            measure if known is None else max(known, measure)
        )

    trends: list[StrengthTrend] = []
    for key, by_date in daily.items():
        running: int | float | None = None
        points: list[StrengthPoint] = []
        for day in sorted(by_date):
            measure = by_date[day]
            running = measure if running is None else max(running, measure)
            if from_on <= day <= to_on:
                points.append(StrengthPoint(performed_on=day, value=running))
        if points:
            trends.append(_strength_trend(key, tuple(points)))
    return tuple(sorted(trends, key=_series_order))


def _measures(fact: ValidWorkSet) -> tuple[_SeriesKey, int | float]:
    """一个有效工作组贡献的趋势系列与度量（三种记录口径互不混算，各自只有一个系列）。"""
    if fact.record_type == "reps_weight":
        return _series_key(fact, "weight_pb"), _weight_of(fact)
    if fact.record_type == "reps_bodyweight":
        return _series_key(fact, "reps_pb"), _reps_of(fact)
    return _series_key(fact, "duration_pb"), _duration_of(fact)


def _series_key(fact: ValidWorkSet, pb_type: PersonalBestType) -> _SeriesKey:
    return _SeriesKey(
        exercise_id=fact.exercise_id,
        exercise_name=fact.exercise_name,
        pb_type=pb_type,
        load_convention=fact.load_convention,
    )


def _strength_trend(
    key: _SeriesKey, points: tuple[StrengthPoint, ...]
) -> StrengthTrend:
    return StrengthTrend(
        exercise_id=key.exercise_id,
        exercise_name=key.exercise_name,
        pb_type=key.pb_type,
        load_convention=key.load_convention,
        weight_kg=None,
        points=points,
    )


def _series_order(trend: StrengthTrend) -> tuple[str, int, float]:
    return _order_key(trend.exercise_id, trend.pb_type, trend.weight_kg)


def compute_calendar_days(
    sessions: Sequence[PlanSession],
    completed: Mapping[int, WorkoutFact],
    workouts: Sequence[WorkoutFact],
    from_on: date,
    to_on: date,
) -> tuple[CalendarDay, ...]:
    """把 active 计划的日程与实际训练按各自日期落天。"""
    sessions_by_day: dict[date, list[CalendarPlanSession]] = {}
    for session in sessions:
        if not from_on <= session.scheduled_on <= to_on:
            continue
        sessions_by_day.setdefault(session.scheduled_on, []).append(
            _calendar_plan_session(session, completed.get(session.id))
        )
    workouts_by_day: dict[date, list[WorkoutFact]] = {}
    for fact in workouts:
        workouts_by_day.setdefault(fact.performed_on, []).append(fact)
    return tuple(
        CalendarDay(
            date=day,
            plan_sessions=tuple(sessions_by_day.get(day, ())),
            workouts=tuple(workouts_by_day.get(day, ())),
        )
        for day in sorted(sessions_by_day.keys() | workouts_by_day.keys())
    )


def _calendar_plan_session(
    session: PlanSession, workout: WorkoutFact | None
) -> CalendarPlanSession:
    """单次日程状态：已取消 → ``cancelled``；否则已有关联训练 → ``complete``；其余 ``incomplete``。"""
    status: CalendarSessionStatus
    if session.cancelled_at is not None:
        status = "cancelled"
    elif workout is not None:
        status = "complete"
    else:
        status = "incomplete"
    return CalendarPlanSession(
        plan_session_id=session.id,
        scheduled_on=session.scheduled_on,
        status=status,
        workout_session_id=None if workout is None else workout.workout_session_id,
        actual_performed_on=None if workout is None else workout.performed_on,
    )
