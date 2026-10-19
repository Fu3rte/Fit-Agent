import json
from dataclasses import dataclass

from app.application.business.catalog import Catalog
from app.application.business.retrieval_conditions import EvaluatedConditions
from app.application.business.retrieval_search import RetrievalCandidate
from app.domain.business.errors import ExerciseDetailsTooLarge, ExerciseNotFound
from app.domain.business.models import (
    CatalogExercise,
    CatalogSearchArguments,
    ExerciseDetailResult,
    ExerciseMatch,
    ExerciseSearchResult,
)

# 候选池与最终模型输入的独立上限：20 条候选参与 Recall@20，最多 5 条进入模型。
CANDIDATE_POOL_LIMIT = 20
SEARCH_LIMIT = 5
SEARCH_BUDGET = 4000
DETAILS_LIMIT = 5
DETAILS_BUDGET = 7000


@dataclass(frozen=True)
class SearchStage:
    # 同一候选池产生两个阶段：门槛前的真实候选池与公开的最终模型输入。
    candidate_pool: tuple[RetrievalCandidate, ...]
    final_input: tuple[RetrievalCandidate, ...]
    response: ExerciseSearchResult


@dataclass(frozen=True)
class SearchObservation:
    # 只读观测载荷：真实工具调用身份与本次检索实际命中的物理集合及清单。
    tool_call_id: str | None
    arguments: CatalogSearchArguments
    stage: SearchStage
    collection: str
    manifest_json: str


def encoded_length(payload: object) -> int:
    # 与 common.envelope 相同的序列化口径：统计最终公开 JSON 字符数。
    return len(json.dumps(payload, ensure_ascii=False, allow_nan=False))


def _matches(candidate: RetrievalCandidate, evaluated: EvaluatedConditions) -> list[str]:
    parts = [f"{field}={getattr(candidate, field)}" for field in evaluated.filtered_fields]
    parts.extend(evaluated.condition_notes)
    return parts


def build_search_result(
    candidates: list[RetrievalCandidate],
    evaluated: EvaluatedConditions,
    *,
    limit: int = SEARCH_LIMIT,
    budget: int = SEARCH_BUDGET,
) -> ExerciseSearchResult:
    # candidates 已通过门槛并按 RRF 融合顺序排列；只按完整条目裁剪，不截断 JSON 或单条结构。
    items: list[ExerciseMatch] = []
    for candidate in candidates[:limit]:
        item = ExerciseMatch(
            id=candidate.id,
            name=candidate.name,
            equipment=candidate.equipment,
            target=candidate.target,
            matches=_matches(candidate, evaluated),
        )
        trial = ExerciseSearchResult(
            items=[*items, item], has_more=len(candidates) > len(items) + 1
        )
        if encoded_length(trial.model_dump()) > budget:
            break
        items.append(item)
    return ExerciseSearchResult(
        items=items, has_more=len(candidates) > len(items)
    )


def build_search_stage(
    candidates: list[RetrievalCandidate],
    evaluated: EvaluatedConditions,
    *,
    threshold: float | None,
    limit: int = SEARCH_LIMIT,
    budget: int = SEARCH_BUDGET,
) -> SearchStage:
    # 候选池取门槛与响应预算裁剪前的实际 RRF 排名前 20 条；最终输入从同一候选池产生。
    pool = tuple(candidates[:CANDIDATE_POOL_LIMIT])
    eligible = [
        candidate
        for candidate in pool
        if threshold is None or candidate.score >= threshold
    ]
    response = build_search_result(eligible, evaluated, limit=limit, budget=budget)
    # 预算可能让公开条目少于数量上限；最终输入与公开条目严格一致。
    return SearchStage(
        candidate_pool=pool,
        final_input=tuple(eligible[: len(response.items)]),
        response=response,
    )


def build_details_result(
    ids: list[str],
    catalog: Catalog,
    *,
    budget: int = DETAILS_BUDGET,
) -> ExerciseDetailResult:
    # 先解析全部输入：未知 ID 明确报错，不允许以裁剪为由伪造或缺失详情。
    resolved: list[CatalogExercise] = []
    for exercise_id in ids:
        exercise = catalog.get(exercise_id)
        if exercise is None:
            raise ExerciseNotFound("ids: 动作 ID 不存在于动作目录。")
        resolved.append(exercise)
    items: list[CatalogExercise] = []
    for exercise in resolved:
        remaining = ids[len(items) + 1:]
        trial = ExerciseDetailResult(
            items=[*items, exercise], has_more=bool(remaining), remaining_ids=remaining
        )
        if encoded_length(trial.model_dump()) > budget:
            if not items:
                raise ExerciseDetailsTooLarge(
                    "ids: 单条完整动作详情超出响应预算。"
                )
            break
        items.append(exercise)
    remaining = ids[len(items):]
    return ExerciseDetailResult(
        items=items, has_more=bool(remaining), remaining_ids=remaining
    )
