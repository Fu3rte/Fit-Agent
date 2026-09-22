"""stats 用例编排：从当前有效训练组确定性现算三类 PB、趋势摘要与月历状态。"""

from datetime import date

from app.application.ports import Plans, Stats
from app.domain.stats.rules import (
    compute_calendar_days,
    compute_personal_bests,
    compute_strength_trends,
    compute_trend_summary,
    month_bounds,
    trend_window,
)
from app.domain.stats.schema import (
    TREND_WINDOW_DAYS,
    CalendarMonth,
    MetricPoint,
    PersonalBest,
    TrendReport,
    TrendSummary,
    ValidWorkSet,
    WorkoutFact,
)


class StatsService:
    """只读统计用例：当前有效训练组 → 三类 PB、趋势与月历（现算，不落表、不缓存）。"""

    def __init__(self, stats: Stats, plans: Plans) -> None:
        self._stats = stats
        self._plans = plans

    async def list_valid_work_sets(self) -> tuple[ValidWorkSet, ...]:
        """有效工作组事实：确定性计划规则与 PB／趋势现算共用同一口径的只读输入。"""
        return await self._stats.list_valid_work_sets()

    async def list_linked_workouts(self) -> tuple[WorkoutFact, ...]:
        """已关联计划日程的训练事实：计划调整的关联口径与月历完成事实共用。"""
        return await self._stats.list_linked_workouts()

    async def list_personal_bests(self) -> tuple[PersonalBest, ...]:
        """按当前有效训练组现算全部三类 PB。"""
        return compute_personal_bests(await self._stats.list_valid_work_sets())

    async def trends(self, business_day: date) -> TrendReport:
        """最近 30 天趋势：原始点、力量累计 PB 系列与确定性趋势摘要（均为现算）。"""
        from_on, to_on = trend_window(business_day)
        metrics = await self._stats.list_body_metrics_between(from_on, to_on)
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
                await self._stats.list_valid_work_sets(), from_on, to_on
            ),
            trend_summary=await self.trend_summary(business_day),
        )

    async def trend_summary(self, business_day: date) -> TrendSummary:
        """确定性趋势摘要：最近两条体重、最近两条非空体脂与距上次训练天数。"""
        return compute_trend_summary(
            await self._stats.read_latest_two_weights(),
            await self._stats.read_latest_two_body_fats(),
            await self._stats.read_last_workout_on(),
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
            for fact in await self._stats.list_linked_workouts()
        }
        return CalendarMonth(
            month=f"{year:04d}-{month:02d}",
            from_on=first_on,
            to_on=last_on,
            days=compute_calendar_days(
                sessions,
                completed,
                await self._stats.list_workouts_between(first_on, last_on),
                first_on,
                last_on,
            ),
        )
