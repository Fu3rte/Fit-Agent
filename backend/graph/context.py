"""MemoryAssembler：一次 Graph Run 的固定范围上下文装配（讨论总结 §5.2／§14、REFACTOR_PLAN §8.2、
stage3.md §4）。

装配范围严格六类，字段顺序即讨论总结 §5.2 的读取清单：用户长期画像、当前 active 计划、最近 4 次
训练、相关动作最新 PB、确定性 ``trend_summary``、当前请求。Planner 与调整流程共用同一个装配入口
（REFACTOR_PLAN §8.2：单个服务负责装配）。

五条硬边界：

- **业务事实每次从 SQLite 重读**：画像、计划、训练、PB、趋势一律现取，不引入业务事实缓存、聊天摘要
  或统计结果表（讨论总结 §5.2、REFACTOR_PLAN §8.2）；修改或删除训练、更新画像后下次装配立即反映。
- **有界读取**：近期训练在 ``domain.records`` 内有界取最近 ``RECENT_SESSION_LIMIT`` 次及其组，不先读
  完整训练历史再在这里截断。
- **不另算业务口径**：PB 与 ``trend_summary`` 直接复用 ``domain.stats.StatsService``，画像复用
  ``domain.profile.ProfileService``、计划复用 ``domain.plans.PlanReadService``；本层不写 SQL、不重算
  PB、不从训练记录推导统计，也不解释 ``plans.structured_content`` 这个不透明 JSON。
- **不顶替事实**：active 计划缺失就是缺失（不拿 draft／archived 顶替），未建档画像就是缺失（不伪造
  空画像）。
- **业务日期由调用方注入**：本层不调 ``date.today()``，``business_day`` 由调用方按固定业务时区传入
  （REFACTOR_PLAN §5.5）。

相关动作 PB 的范围口径（stage3.md §2.1）：「相关」只过滤哪些动作进入上下文，不改变动作适用的 PB
类型——类型组成与来源信息都由 ``StatsService`` 决定，本层只按稳定 ``exercise_id`` 过滤。生成新计划时
传 ``exercise_ids=None``（全部已有动作 PB）；调整计划时传当前 active 计划涉及的 ID，ID 由 Stage 4
从统一计划 Schema 提取后注入。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from domain.plans.schema import Plan
from domain.plans.service import PlanReadService
from domain.profile.schema import Profile
from domain.profile.service import ProfileService
from domain.records.schema import WorkoutSession
from domain.records.service import WorkoutRecordsService
from domain.stats.schema import PersonalBest, TrendSummary
from domain.stats.service import StatsService
from storage.db import Database

#: 装配范围里的近期训练条数（讨论总结 §5.2：「最近 4 次训练」；同日训练各算一次）。
RECENT_SESSION_LIMIT = 4


@dataclass(frozen=True, slots=True)
class MemoryContext:
    """MemoryAssembler 的一次输出：恰好六类内容，不夹带业务库副本或统计缓存。

    ``profile``／``active_plan`` 为 ``None`` 即该事实确实缺失，不用空值伪造；``recent_sessions``
    保留每次训练的全部组与组类型（不裁成 PB 有效工作组）；``personal_bests`` 与 ``trend_summary``
    是 ``StatsService`` 的现算结果，本层未做二次加工。
    """

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
        self._plans = PlanReadService(db)
        self._records = WorkoutRecordsService(db)
        self._stats = StatsService(db)

    async def assemble(
        self,
        request: str,
        *,
        business_day: date,
        exercise_ids: Sequence[str] | None = None,
    ) -> MemoryContext:
        """装配一次上下文；各项业务事实都在本次调用里重新读取。

        ``business_day`` 是调用方注入的业务自然日（趋势窗口与停训天数按它复算）；
        ``exercise_ids`` 为 ``None`` 即装配全部已有动作 PB（生成新计划），给出序列即只装配这些稳定
        ``exercise_id`` 的 PB（调整计划，ID 由 Stage 4 从统一计划 Schema 提取）；空序列即无相关动作。
        """
        return MemoryContext(
            profile=await self._profiles.read(),
            active_plan=await self._plans.get_active(),
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
    """按稳定 ``exercise_id`` 过滤 PB；``None`` 表示不过滤（生成新计划要全部已有动作 PB）。

    只做身份筛选：PB 类型、数值与来源（训练、组序号、``performed_on``）原样保留。
    """
    if exercise_ids is None:
        return tuple(bests)
    related = frozenset(exercise_ids)
    return tuple(pb for pb in bests if pb.exercise_id in related)
