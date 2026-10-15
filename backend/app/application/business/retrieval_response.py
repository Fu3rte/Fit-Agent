import json

from app.application.business.catalog import Catalog
from app.application.business.retrieval_conditions import EvaluatedConditions
from app.application.business.retrieval_search import RetrievalCandidate
from app.domain.business.errors import ExerciseDetailsTooLarge, ExerciseNotFound
from app.domain.business.models import (
    CatalogExercise,
    ExerciseDetailResult,
    ExerciseMatch,
    ExerciseSearchResult,
)

# 固定预算与上限：搜索 10 条简要候选、4000 字符；详情 5 条、7000 字符。
SEARCH_LIMIT = 10
SEARCH_BUDGET = 4000
DETAILS_LIMIT = 5
DETAILS_BUDGET = 7000


def _encoded_length(payload: object) -> int:
    # 与 common.envelope 相同的序列化口径：统计最终 JSON 字符数（含元信息开销）。
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
    recalled = [candidate.id for candidate in candidates]
    items: list[ExerciseMatch] = []
    for candidate in candidates[:limit]:
        item = ExerciseMatch(
            id=candidate.id,
            name=candidate.name,
            equipment=candidate.equipment,
            target=candidate.target,
            matches=_matches(candidate, evaluated),
        )
        remaining = recalled[len(items) + 1:]
        trial = ExerciseSearchResult(
            items=[*items, item], has_more=bool(remaining), remaining_ids=remaining
        )
        if _encoded_length(trial.model_dump()) > budget:
            break
        items.append(item)
    remaining = recalled[len(items):]
    return ExerciseSearchResult(
        items=items, has_more=bool(remaining), remaining_ids=remaining
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
        if _encoded_length(trial.model_dump()) > budget:
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
