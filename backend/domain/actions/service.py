"""actions 用例编排：按身份读取、别名候选解析、推荐候选筛选（正本 architecture/03）。

边界（stage1.md §5 S1-03）：这里只提供目录查询与候选集合，不实现打卡匹配交互，
也不代表 Agent 已遵守推荐规则。目录写入只在编号迁移内；本服务不写目录、不写档案、
不推进 ``context_version``。
"""

from domain.actions.repo import ExerciseRepo
from domain.actions.schema import AliasCandidate, AliasResolution, Exercise
from storage.db import Database


class ActionCatalogService:
    """动作目录读取用例（repo + 规则的组合点，供后续 Stage 3/4 接线复用）。"""

    def __init__(self, db: Database):
        self._repo = ExerciseRepo(db)

    async def get_by_id(self, exercise_id: str) -> Exercise | None:
        """按稳定身份读取；停用动作仍返回（历史引用可解释）。"""
        return await self._repo.get_by_id(exercise_id)

    async def resolve(self, term: str) -> AliasResolution:
        """标准名 / 别名精确解析，保留全部候选。

        命中多个身份时 ``AliasResolution.is_ambiguous`` 为真，由调用方询问用户，
        不静默取第一项（03 3.1）。标准名空间与别名空间分别匹配；同一身份若两处都
        命中，只保留标准名候选。
        """
        candidates: dict[str, AliasCandidate] = {}
        standard_match = await self._repo.get_by_standard_name(term)
        if standard_match is not None:
            candidates[standard_match.id] = AliasCandidate(
                exercise=standard_match,
                matched_text=term,
                match_kind="standard_name",
            )
        for exercise in await self._repo.find_by_alias(term):
            candidates.setdefault(
                exercise.id,
                AliasCandidate(
                    exercise=exercise, matched_text=term, match_kind="alias"
                ),
            )
        return AliasResolution(term=term, candidates=tuple(candidates.values()))

    async def recommendation_candidates(self) -> tuple[Exercise, ...]:
        """推荐候选：仅未停用且已检查标记可推荐的动作（03 3.2）。"""
        return await self._repo.list_recommendable()
