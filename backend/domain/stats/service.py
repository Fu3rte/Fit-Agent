"""stats 用例编排：从当前有效训练组确定性现算三类 PB、趋势摘要与月历状态（讨论总结
§3.2／§6.4／§7.1、REFACTOR_PLAN §6.2／§6.4、stage2.md §6.2／§7／§8）。

边界：不接 HTTP／Agent（Stats API 与响应 DTO 在 ``api``），不写任何表、不建缓存、不调模型，
也不调 ``date.today()``——PB 的日期只来自来源训练自己的 ``performed_on``，趋势窗口与停训天数
的「今天」由调用方注入业务日期。SQL 只在 ``domain.stats.repo`` 与 ``domain.plans.repo``：本层
只做查询编排与确定性计算。

口径（stage2.md §7／§8）：

- **不补 0**：窗口内只返回真实存在的原始点；数据不足的摘要显式返回 ``no_data``／
  ``insufficient_data``，不伪造变化值。
- **不评价**：不输出进步、退步、停滞或疲劳判断，也不含训练容量与计划完成率。
- **不聚合完成率**：月历只给单次日程的取消／未完成／已完成状态与实际训练日。
"""

from calendar import monthrange
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TypeVar

from domain.actions.schema import LoadConvention
from domain.body_metrics.schema import BodyMetric
from domain.plans.repo import PlanRepo
from domain.plans.schema import PlanSession
from domain.stats.repo import StatsRepo
from domain.stats.schema import (
    PERSONAL_BEST_TYPES,
    TREND_WINDOW_DAYS,
    CalendarDay,
    CalendarMonth,
    CalendarPlanSession,
    CalendarSessionStatus,
    InvalidWorkSet,
    MetricChange,
    MetricPoint,
    PersonalBest,
    PersonalBestType,
    StrengthPoint,
    StrengthTrend,
    TrendReport,
    TrendSummary,
    ValidWorkSet,
    WorkoutFact,
    WorkoutGap,
)
from storage.db import Database

T = TypeVar("T")


class StatsService:
    """只读统计用例：当前有效训练组 → 三类 PB、趋势与月历（现算，不落表、不缓存）。"""

    def __init__(self, db: Database):
        self._repo = StatsRepo(db)
        self._plans = PlanRepo(db)

    async def list_personal_bests(self) -> tuple[PersonalBest, ...]:
        """按当前有效训练组现算全部三类 PB（修改或删除记录后再次查询即反映最新结果）。

        返回按动作身份、PB 类型（重量、次数、时长）、适用重量升序排列。
        """
        return compute_personal_bests(await self._repo.list_valid_work_sets())

    async def trends(self, business_day: date) -> TrendReport:
        """最近 30 天趋势：原始点、力量累计 PB 系列与确定性趋势摘要（均为现算）。"""
        from_on, to_on = trend_window(business_day)
        metrics = await self._repo.list_body_metrics_between(from_on, to_on)
        return TrendReport(
            window_days=TREND_WINDOW_DAYS,
            from_on=from_on,
            to_on=to_on,
            weight=tuple(MetricPoint(metric.measured_on, metric.weight_kg) for metric in metrics),
            body_fat=tuple(
                MetricPoint(metric.measured_on, metric.body_fat_pct)
                for metric in metrics
                if metric.body_fat_pct is not None
            ),
            strength=compute_strength_trends(
                await self._repo.list_valid_work_sets(), from_on, to_on
            ),
            trend_summary=await self.trend_summary(business_day),
        )

    async def trend_summary(self, business_day: date) -> TrendSummary:
        """确定性趋势摘要：最近两条体重、最近两条非空体脂与距上次训练天数。

        同一实现供看板与 Stage 3 MemoryAssembler 复用；本子任务不实现 Stage 3。
        """
        return compute_trend_summary(
            await self._repo.read_latest_two_weights(),
            await self._repo.read_latest_two_body_fats(),
            await self._repo.read_last_workout_on(),
            business_day,
        )

    async def calendar_month(self, year: int, month: int) -> CalendarMonth:
        """一个自然月的计划日程与实际训练事实（只读当前 active 计划，不聚合完成率）。"""
        first_on, last_on = month_bounds(year, month)
        active = await self._plans.read_active()
        sessions = (
            () if active is None else await self._plans.list_sessions(active.id)
        )
        completed = {
            fact.plan_session_id: fact
            for fact in await self._repo.list_linked_workouts()
        }
        return CalendarMonth(
            month=f"{year:04d}-{month:02d}",
            from_on=first_on,
            to_on=last_on,
            days=_calendar_days(
                sessions,
                completed,
                await self._repo.list_workouts_between(first_on, last_on),
                first_on,
                last_on,
            ),
        )



def compute_personal_bests(facts: Sequence[ValidWorkSet]) -> tuple[PersonalBest, ...]:
    """把有效工作组归约成三类 PB（同一份事实必得同一份结果：无模型、无缓存、无「今天」）。

    分组口径：``weight_pb`` 按（动作、负重口径）；外加重量动作的 ``reps_pb`` 按（动作、负重口径、
    重量），纯自重动作的 ``reps_pb`` 只按动作；``duration_pb`` 只按动作。故三类动作互不混算，
    且同一动作的每个重量各自有一条次数 PB。多组次数只比大小、不累加。
    """
    weighted: dict[tuple[str, LoadConvention | None], list[ValidWorkSet]] = {}
    weighted_reps: dict[
        tuple[str, LoadConvention | None, float], list[ValidWorkSet]
    ] = {}
    bodyweight_reps: dict[str, list[ValidWorkSet]] = {}
    timed: dict[str, list[ValidWorkSet]] = {}

    for fact in facts:
        if fact.record_type == "reps_weight":
            weighted.setdefault((fact.exercise_id, fact.load_convention), []).append(
                fact
            )
            weighted_reps.setdefault(
                (fact.exercise_id, fact.load_convention, _weight_of(fact)), []
            ).append(fact)
        elif fact.record_type == "reps_bodyweight":
            bodyweight_reps.setdefault(fact.exercise_id, []).append(fact)
        else:
            timed.setdefault(fact.exercise_id, []).append(fact)

    bests: list[PersonalBest] = []
    # 最大实际重量：同动作、同负重口径比较，次数只作为来源组事实。
    for group in weighted.values():
        source, weight = _best_source(group, _weight_of)
        bests.append(_personal_best("weight_pb", source, weight, weight))
    # 最大次数：外加重量动作按相同重量分别取（哑铃是单手指记录值，不做换算）。
    for (_, _, weight), group in weighted_reps.items():
        source, reps = _best_source(group, _reps_of)
        bests.append(_personal_best("reps_pb", source, reps, weight))
    # 最大次数：纯自重动作直接取单组最大次数（无重量、无口径）。
    for group in bodyweight_reps.values():
        source, reps = _best_source(group, _reps_of)
        bests.append(_personal_best("reps_pb", source, reps, None))
    # 最长时长：计时动作取单组最大持续秒数（无重量、无口径）。
    for group in timed.values():
        source, seconds = _best_source(group, _duration_of)
        bests.append(_personal_best("duration_pb", source, seconds, None))

    return tuple(sorted(bests, key=_result_order))


def _best_source(
    group: Sequence[ValidWorkSet], measure: Callable[[ValidWorkSet], int | float]
) -> tuple[ValidWorkSet, int | float]:
    """取度量最大的组及其度量值；度量并列时取最先达成的来源。

    ``min`` 配「度量取负 + 来源排序升序」即「度量最大、并列取最早」；度量是不小于 0 的次数、
    秒数或重量，取负不改变可比性。
    """
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
    """PB 结果与力量趋势系列共用的输出顺序键。

    无适用重量（纯自重次数 PB 与计时 PB）用 -1 占位即可：重量下限是 0kg，且同一动作同一 PB
    类型下不会既有适用重量又无适用重量——区分它们的是动作自己的记录口径。
    """
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
    """把「最近两条体重／体脂记录 + 最近一次训练日期 + 注入的业务日期」归约成摘要。

    ``latest_weights``／``latest_body_fats`` 由 repo 以最新在前给出；体脂为 NULL 的行不参与
    （未记录体脂不是 0%）。停训天数由业务日期与最近 ``performed_on`` 相减得到，不由本层取「今天」。
    """
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
    """一个力量趋势系列的身份：动作身份 + PB 类型 + 负重口径 + 次数 PB 的分组重量。"""

    exercise_id: str
    exercise_name: str
    pb_type: PersonalBestType
    load_convention: LoadConvention | None
    weight_kg: float | None


def compute_strength_trends(
    facts: Sequence[ValidWorkSet], from_on: date, to_on: date
) -> tuple[StrengthTrend, ...]:
    """把有效工作组归约成「截至各日期的累计 PB」系列（历史最好成绩，曲线不下降）。

    系列口径与三类 PB 的分组一致（同一个有效工作组事实，不另写一套过滤）：外加重量动作同时
    进入「累计最大重量」与「按同重量的累计单组最大次数」两个系列；纯自重动作进入累计单组最大
    次数系列；计时动作进入累计单组最长秒数系列。累计值包含窗口之前的全部历史，故窗口内每个
    有该系列有效组的日期都给一个点，没有有效组的日期不补点（不补 0）。
    """
    daily: dict[_SeriesKey, dict[date, int | float]] = {}
    for fact in facts:
        for key, measure in _measures(fact):
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


def _measures(fact: ValidWorkSet) -> tuple[tuple[_SeriesKey, int | float], ...]:
    """一个有效工作组贡献的趋势系列与度量（三种记录口径互不混算）。"""
    if fact.record_type == "reps_weight":
        weight = _weight_of(fact)
        return (
            (_series_key(fact, "weight_pb", None), weight),
            (_series_key(fact, "reps_pb", weight), _reps_of(fact)),
        )
    if fact.record_type == "reps_bodyweight":
        return ((_series_key(fact, "reps_pb", None), _reps_of(fact)),)
    return ((_series_key(fact, "duration_pb", None), _duration_of(fact)),)


def _series_key(
    fact: ValidWorkSet, pb_type: PersonalBestType, weight_kg: float | None
) -> _SeriesKey:
    return _SeriesKey(
        exercise_id=fact.exercise_id,
        exercise_name=fact.exercise_name,
        pb_type=pb_type,
        load_convention=fact.load_convention,
        weight_kg=weight_kg,
    )


def _strength_trend(
    key: _SeriesKey, points: tuple[StrengthPoint, ...]
) -> StrengthTrend:
    return StrengthTrend(
        exercise_id=key.exercise_id,
        exercise_name=key.exercise_name,
        pb_type=key.pb_type,
        load_convention=key.load_convention,
        weight_kg=key.weight_kg,
        points=points,
    )


def _series_order(trend: StrengthTrend) -> tuple[str, int, float]:
    return _order_key(trend.exercise_id, trend.pb_type, trend.weight_kg)


def _calendar_days(
    sessions: Sequence[PlanSession],
    completed: Mapping[int, WorkoutFact],
    workouts: Sequence[WorkoutFact],
    from_on: date,
    to_on: date,
) -> tuple[CalendarDay, ...]:
    """把 active 计划的日程与实际训练按各自日期落天：跨日关联时两边各出现在自己的日期。

    ``completed`` 是「计划日程身份 → 完成它的训练事实」；只含窗口内的日程与训练，两端都按
    日期升序（``PlanRepo`` 与 ``StatsRepo`` 的查询顺序）分组，不给空白日期造「休息日」条目。
    """
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
