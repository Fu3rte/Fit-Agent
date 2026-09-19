"""MemoryAssembler：一次 Graph Run 的固定范围上下文装配。"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from domain.plans.repo import PlanRepo
from domain.plans.schema import Plan
from domain.profile.schema import Profile
from domain.profile.service import ProfileService
from domain.records.schema import WorkoutSession
from domain.records.service import WorkoutRecordsService
from domain.stats.schema import PersonalBest, TrendSummary
from domain.stats.service import StatsService
from storage.db import Database

RECENT_SESSION_LIMIT = 4


@dataclass(frozen=True, slots=True)
class MemoryContext:
    """MemoryAssembler 的一次输出：恰好六类内容。"""

    profile: Profile | None
    active_plan: Plan | None
    recent_sessions: tuple[WorkoutSession, ...]
    personal_bests: tuple[PersonalBest, ...]
    trend_summary: TrendSummary
    request: str


class MemoryAssembler:
    """固定范围装配：画像、当前 active 计划、最近 4 次训练、相关动作 PB、``trend_summary``、请求。"""

    def __init__(self, db: Database):
        self._profiles = ProfileService(db)
        self._plans = PlanRepo(db)
        self._records = WorkoutRecordsService(db)
        self._stats = StatsService(db)

    async def assemble(
        self,
        request: str,
        *,
        business_day: date,
        exercise_ids: Sequence[str] | None = None,
    ) -> MemoryContext:
        """装配一次上下文；各项业务事实都在本次调用里重新读取。"""
        return MemoryContext(
            profile=await self._profiles.read(),
            active_plan=await self._plans.read_active(),
            recent_sessions=await self._records.list_recent(RECENT_SESSION_LIMIT),
            personal_bests=_related_bests(
                await self._stats.list_personal_bests(), exercise_ids
            ),
            trend_summary=await self._stats.trend_summary(business_day),
            request=request,
        )


def _related_bests(
    bests: Sequence[PersonalBest], exercise_ids: Sequence[str] | None
) -> tuple[PersonalBest, ...]:
    """按稳定 ``exercise_id`` 过滤 PB；``None`` 表示不过滤。"""
    if exercise_ids is None:
        return tuple(bests)
    related = frozenset(exercise_ids)
    return tuple(pb for pb in bests if pb.exercise_id in related)
