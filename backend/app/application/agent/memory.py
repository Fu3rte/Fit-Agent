"""MemoryAssembler：一次 Graph Run 的固定范围上下文装配。"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from app.application.ports import Plans, Profiles, Records
from app.application.services.stats_service import StatsService
from app.domain.conversations.context import ContextMessage
from app.domain.plans.schema import Plan
from app.domain.profile.schema import Profile
from app.domain.records.schema import WorkoutSession
from app.domain.stats.schema import PersonalBest, TrendSummary

RECENT_SESSION_LIMIT = 4


@dataclass(frozen=True, slots=True)
class MemoryContext:
    """MemoryAssembler 的一次输出：恰好六类业务事实、当前请求、业务日与对话历史投影。"""

    profile: Profile | None
    active_plan: Plan | None
    recent_sessions: tuple[WorkoutSession, ...]
    personal_bests: tuple[PersonalBest, ...]
    trend_summary: TrendSummary
    request: str
    business_day: date
    conversation_messages: tuple[ContextMessage, ...] = ()


class MemoryAssembler:
    """固定范围装配：画像、当前 active 计划、最近 4 次训练、相关动作 PB、``trend_summary``、请求。"""

    def __init__(
        self,
        profiles: Profiles,
        plans: Plans,
        records: Records,
        stats: StatsService,
    ) -> None:
        self._profiles = profiles
        self._plans = plans
        self._records = records
        self._stats = stats

    async def assemble(
        self,
        request: str,
        *,
        business_day: date,
        exercise_ids: Sequence[str] | None = None,
        conversation_messages: Sequence[ContextMessage] = (),
    ) -> MemoryContext:
        """装配一次上下文；各项业务事实都在本次调用里重新读取，对话历史由调用方投影后传入。"""
        return MemoryContext(
            conversation_messages=tuple(conversation_messages),
            profile=await self._profiles.read(),
            active_plan=await self._plans.read_active(),
            recent_sessions=await self._records.list_recent(RECENT_SESSION_LIMIT),
            personal_bests=_related_bests(
                await self._stats.list_personal_bests(), exercise_ids
            ),
            trend_summary=await self._stats.trend_summary(business_day),
            request=request,
            business_day=business_day,
        )


def _related_bests(
    bests: Sequence[PersonalBest], exercise_ids: Sequence[str] | None
) -> tuple[PersonalBest, ...]:
    """按稳定 ``exercise_id`` 过滤 PB；``None`` 表示不过滤。"""
    if exercise_ids is None:
        return tuple(bests)
    related = frozenset(exercise_ids)
    return tuple(pb for pb in bests if pb.exercise_id in related)
