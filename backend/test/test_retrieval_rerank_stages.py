import json
import math

import pytest

from app.application.business import retrieval_text as rt
from app.application.business.retrieval_conditions import EvaluatedConditions
from app.application.business.retrieval_rerank import (
    RerankIdentity,
    RerankResult,
    RerankScored,
    RerankUsage,
)
from app.application.business.retrieval_response import (
    CANDIDATE_POOL_LIMIT,
    SEARCH_BUDGET,
    SEARCH_LIMIT,
    build_search_stage,
)
from app.application.business.retrieval_runtime import ActionRetrievalRuntime
from app.application.business.retrieval_search import (
    RetrievalCandidate,
    SearchConditions,
)
from app.domain.business.models import ExerciseSearchResult

ENDPOINT = (
    "https://ws-example.cn-beijing.maas.aliyuncs.com"
    "/api/v1/services/rerank/text-rerank/text-rerank"
)
IDENTITY = RerankIdentity("qwen3.7-text-rerank", ENDPOINT, rt.ENCODING_RULE_VERSION)


def candidate(index: int, score: float | None = None) -> RetrievalCandidate:
    exercise_id = f"{index:04d}"
    return RetrievalCandidate(
        id=exercise_id,
        score=float(100 - index) if score is None else score,
        name=f"动作{exercise_id}",
        equipment="body weight",
        body_part="back",
        target="lats",
        muscle_group="back",
    )


def evaluated() -> EvaluatedConditions:
    return EvaluatedConditions(SearchConditions(), (), ())


def rerank_result(
    pool: list[RetrievalCandidate], scores: dict[str, float]
) -> RerankResult:
    rows = [
        RerankScored(item.id, scores[item.id], rank)
        for rank, item in enumerate(pool)
    ]
    rows.sort(key=lambda row: (-row.rerank_score, row.rrf_rank))
    return RerankResult(tuple(rows), RerankUsage(1, 20, 20), "request-id")


def build(pool, result, *, threshold=0.0, budget=SEARCH_BUDGET):
    return build_search_stage(
        pool,
        evaluated(),
        rerank=result,
        rerank_identity=IDENTITY,
        threshold=threshold,
        budget=budget,
    )


def test_empty_pool_keeps_legal_empty_result_without_model_usage():
    result = RerankResult((), RerankUsage(), None)
    stage = build([], result)
    assert stage.candidate_pool == ()
    assert stage.final_input == ()
    assert stage.response.items == [] and stage.response.has_more is False
    assert stage.rerank is result
    assert stage.rerank_identity == IDENTITY
    assert stage.response.model_dump().keys() == {"items", "has_more"}


def test_single_candidate_uses_the_rerank_result():
    pool = [candidate(1)]
    stage = build(pool, rerank_result(pool, {"0001": 0.9}))
    assert [item.id for item in stage.candidate_pool] == ["0001"]
    assert [item.id for item in stage.final_input] == ["0001"]
    assert [item.id for item in stage.response.items] == ["0001"]


def test_twenty_candidates_keep_rrf_pool_and_publish_rerank_top_five():
    pool = [candidate(index) for index in range(1, 21)]
    result = rerank_result(pool, {item.id: index / 100 for index, item in enumerate(pool)})
    stage = build(pool, result)
    assert len(stage.candidate_pool) == CANDIDATE_POOL_LIMIT == 20
    assert [item.id for item in stage.candidate_pool] == [
        f"{index:04d}" for index in range(1, 21)
    ]
    assert [row.id for row in result.scored] == [
        f"{index:04d}" for index in range(20, 0, -1)
    ]
    assert [item.id for item in stage.final_input] == [
        f"{index:04d}" for index in range(20, 15, -1)
    ]
    assert [item.id for item in stage.response.items] == [
        f"{index:04d}" for index in range(20, 15, -1)
    ]
    assert len(stage.response.items) == SEARCH_LIMIT
    assert stage.response.has_more is True
    assert all(item.score == float(100 - int(item.id)) for item in stage.candidate_pool)


def test_equal_rerank_scores_keep_original_rrf_order():
    pool = [candidate(index) for index in range(1, 7)]
    result = rerank_result(pool, {item.id: 0.5 for item in pool})
    stage = build(pool, result)
    assert [row.id for row in result.scored] == [
        f"{index:04d}" for index in range(1, 7)
    ]
    assert [item.id for item in stage.final_input] == [
        f"{index:04d}" for index in range(1, 6)
    ]


def test_threshold_applies_to_rrf_score_not_rerank_score():
    pool = [candidate(1, 1.0), candidate(2, 0.0)]
    result = rerank_result(pool, {"0001": 0.1, "0002": 0.9})
    stage = build(pool, result, threshold=0.5)
    assert [row.id for row in result.scored] == ["0002", "0001"]
    assert [item.id for item in stage.candidate_pool] == ["0001", "0002"]
    assert [item.id for item in stage.final_input] == ["0001"]
    assert [item.id for item in stage.response.items] == ["0001"]
    assert stage.response.has_more is False


def test_threshold_can_exclude_every_candidate():
    pool = [candidate(1, 0.2), candidate(2, 0.1)]
    stage = build(pool, rerank_result(pool, {"0001": 0.9, "0002": 0.8}), threshold=0.5)
    assert stage.candidate_pool == (pool[0], pool[1])
    assert stage.final_input == ()
    assert stage.response.items == [] and stage.response.has_more is False


def test_budget_trims_complete_items_in_rerank_order():
    pool = [candidate(index) for index in range(1, 7)]
    scores = {item.id: index / 100 for index, item in enumerate(pool)}
    result = rerank_result(pool, scores)
    full = build(pool, result, budget=10**9).response
    reference = ExerciseSearchResult(items=full.items[:2], has_more=True)
    budget = len(json.dumps(reference.model_dump(), ensure_ascii=False))
    stage = build(pool, result, budget=budget)
    assert len(stage.response.items) == 2
    assert [item.id for item in stage.final_input] == [
        item.id for item in stage.response.items
    ] == ["0006", "0005"]
    assert stage.response.has_more is True
    text = json.dumps(stage.response.model_dump(), ensure_ascii=False, allow_nan=False)
    assert len(text) <= budget
    assert set(json.loads(text)) == {"items", "has_more"}


@pytest.mark.parametrize(
    "rows",
    [
        (
            RerankScored("0003", 0.9, 2),
            RerankScored("0002", 0.8, 1),
        ),
        (
            RerankScored("0003", 0.9, 2),
            RerankScored("0003", 0.8, 1),
            RerankScored("0001", 0.7, 0),
        ),
        (
            RerankScored("0003", 0.9, 3),
            RerankScored("0002", 0.8, 1),
            RerankScored("0001", 0.7, 0),
        ),
        (
            RerankScored("0003", 0.9, 2),
            RerankScored("0002", 0.8, 2),
            RerankScored("0001", 0.7, 0),
        ),
        (
            RerankScored("0003", 0.9, 1),
            RerankScored("0002", 0.8, 2),
            RerankScored("0001", 0.7, 0),
        ),
        (
            RerankScored("9999", 0.9, 2),
            RerankScored("0002", 0.8, 1),
            RerankScored("0001", 0.7, 0),
        ),
        (
            RerankScored("0002", 0.8, 1),
            RerankScored("0003", 0.9, 2),
            RerankScored("0001", 0.7, 0),
        ),
        (
            RerankScored("0003", 0.5, 2),
            RerankScored("0002", 0.5, 1),
            RerankScored("0001", 0.4, 0),
        ),
        (
            RerankScored("0003", math.nan, 2),
            RerankScored("0002", 0.8, 1),
            RerankScored("0001", 0.7, 0),
        ),
        (
            RerankScored("0003", 1.1, 2),
            RerankScored("0002", 0.8, 1),
            RerankScored("0001", 0.7, 0),
        ),
        (
            RerankScored("0003", -0.1, 2),
            RerankScored("0002", 0.8, 1),
            RerankScored("0001", 0.7, 0),
        ),
        (
            RerankScored("0003", True, 2),
            RerankScored("0002", 0.8, 1),
            RerankScored("0001", 0.7, 0),
        ),
    ],
)
def test_invalid_rerank_association_is_rejected(rows):
    pool = [candidate(index) for index in range(1, 4)]
    result = RerankResult(tuple(rows), RerankUsage(1, 20, 20), "request-id")
    with pytest.raises(ValueError):
        build(pool, result)


def test_rerank_result_and_identity_must_be_provided_together():
    pool = [candidate(1)]
    result = rerank_result(pool, {"0001": 0.9})
    with pytest.raises(ValueError):
        build_search_stage(
            pool, evaluated(), rerank=result, threshold=0.0
        )
    with pytest.raises(ValueError):
        build_search_stage(
            pool,
            evaluated(),
            rerank=None,
            rerank_identity=IDENTITY,
            threshold=0.0,
        )


def test_explicit_baseline_call_preserves_rrf_order():
    pool = [candidate(index) for index in range(1, 7)]
    stage = build_search_stage(
        pool, evaluated(), rerank=None, threshold=0.0
    )
    assert [item.id for item in stage.final_input] == [
        f"{index:04d}" for index in range(1, 6)
    ]
    assert stage.rerank is None and stage.rerank_identity is None
    assert stage.candidate_pool == tuple(pool)


def test_runtime_construction_does_not_read_rerank_config(monkeypatch):
    for name in (
        "RERANKER_MODEL",
        "RERANKER_BASE_URL",
        "RERANKER_API_KEY",
        "RERANKER_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)
    runtime = ActionRetrievalRuntime()
    assert runtime._retriever is None
    assert runtime._rerank is None
    assert runtime._rerank_identity is None
    assert runtime._stack is None
