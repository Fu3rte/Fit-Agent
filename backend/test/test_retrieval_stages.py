import json

from app.application.business import retrieval_text as rt
from app.application.business.catalog import Catalog
from app.application.business.retrieval_conditions import (
    EvaluatedConditions,
    evaluate_search_conditions,
)
from app.application.business.retrieval_response import (
    CANDIDATE_POOL_LIMIT,
    SEARCH_BUDGET,
    SEARCH_LIMIT,
    SearchStage,
    build_search_stage,
)
from app.application.business.retrieval_search import (
    RetrievalCandidate,
    SearchConditions,
)
from app.domain.business.models import (
    CatalogSearchArguments,
    ExerciseSearchResult,
    ProfileContent,
)
from evaluation.contracts import RelevanceLabel, RetrievalInput
from evaluation.retrieval_metrics import evaluate_staged


def candidate(index: int, score: float | None = None) -> RetrievalCandidate:
    exercise_id = f"{index:04d}"
    return RetrievalCandidate(
        id=exercise_id,
        score=float(100 - index) if score is None else score,
        name=f"动作{index:04d}",
        equipment="body weight",
        body_part="back",
        target="lats",
        muscle_group="back",
    )


def evaluated() -> EvaluatedConditions:
    return EvaluatedConditions(SearchConditions(), (), ())


def test_candidate_pool_caps_at_twenty_and_final_input_at_five():
    stage = build_search_stage(
        [candidate(index) for index in range(1, 26)],
        evaluated(),
        rerank=None,
        threshold=0.0,
    )
    assert isinstance(stage, SearchStage)
    assert len(stage.candidate_pool) == CANDIDATE_POOL_LIMIT == 20
    assert len(stage.response.items) == SEARCH_LIMIT == 5
    assert len(stage.final_input) == len(stage.response.items)
    assert [item.id for item in stage.candidate_pool] == [
        f"{index:04d}" for index in range(1, 21)
    ]
    assert [item.id for item in stage.final_input] == [
        f"{index:04d}" for index in range(1, 6)
    ]
    assert stage.response.has_more is True
    assert set(ExerciseSearchResult.model_fields) == {"items", "has_more"}


def test_twenty_first_candidate_is_excluded_from_pool_and_scoring():
    stage = build_search_stage(
        [candidate(index) for index in range(1, 22)],
        evaluated(),
        rerank=None,
        threshold=0.0,
    )
    assert "0021" not in {item.id for item in stage.candidate_pool}
    item = RetrievalInput(
        query_id="q1",
        expect_empty=False,
        labels=[RelevanceLabel(exercise_id="0021", grade=3)],
        candidate_pool=[candidate_item.id for candidate_item in stage.candidate_pool],
        final_model_input=[candidate_item.id for candidate_item in stage.final_input],
    )
    result = evaluate_staged([item])[0]
    scores = {metric.name: metric.value for metric in result.metrics}
    assert scores["recall@20"] == 0.0
    assert scores["hit@5"] == 0.0


def test_threshold_trims_final_input_but_preserves_candidate_pool():
    rows = [candidate(1, 1.0), candidate(2, 0.5), candidate(3, 0.0)]
    stage = build_search_stage(rows, evaluated(), rerank=None, threshold=0.45)
    assert [item.id for item in stage.candidate_pool] == ["0001", "0002", "0003"]
    assert [item.id for item in stage.final_input] == ["0001", "0002"]
    assert stage.response.has_more is False


def test_budget_can_return_fewer_than_five_complete_items():
    rows = [candidate(index) for index in range(1, 6)]
    unlimited = build_search_stage(
        rows, evaluated(), rerank=None, threshold=0.0, budget=10**9
    ).response
    reference = ExerciseSearchResult(items=unlimited.items[:2], has_more=True)
    budget = len(json.dumps(reference.model_dump(), ensure_ascii=False))
    stage = build_search_stage(rows, evaluated(), rerank=None, threshold=0.0, budget=budget)
    assert len(stage.response.items) == 2
    assert len(stage.final_input) == 2
    assert stage.response.has_more is True
    text = json.dumps(stage.response.model_dump(), ensure_ascii=False, allow_nan=False)
    assert len(text) <= budget
    payload = json.loads(text)
    assert set(payload) == {"items", "has_more"}
    assert all(
        set(item) == {"id", "name", "equipment", "target", "matches"}
        for item in payload["items"]
    )


def test_public_response_does_not_expose_hidden_candidate_identity():
    stage = build_search_stage(
        [candidate(index) for index in range(1, 21)],
        evaluated(),
        rerank=None,
        threshold=0.0,
        budget=SEARCH_BUDGET,
    )
    payload = stage.response.model_dump()
    assert set(payload) == {"items", "has_more"}
    assert "remaining_ids" not in payload
    text = json.dumps(payload, ensure_ascii=False)
    for index in range(6, 21):
        assert f'"id": "{index:04d}"' not in text
        assert f"动作{index:04d}" not in text


def test_lookup_ignores_saved_restrictions_but_generation_applies_them():
    catalog = Catalog.load()
    mappings = rt.load_mappings()
    equipment = sorted(catalog.equipment)[0]
    forbidden = sorted(catalog.exercise_ids)[0]
    profile = ProfileContent(
        goal=None,
        experience=None,
        environment=None,
        availability=None,
        health_notes=None,
        movement_restrictions=None,
        unavailable_equipment=[equipment],
        forbidden_exercise_ids=[forbidden],
    )
    lookup = evaluate_search_conditions(
        CatalogSearchArguments(purpose="lookup", name="卧推"),
        profile,
        catalog,
        mappings,
    )
    generation = evaluate_search_conditions(
        CatalogSearchArguments(purpose="generation", name="卧推"),
        profile,
        catalog,
        mappings,
    )
    assert equipment in lookup.conditions.available_equipment
    assert forbidden not in lookup.conditions.excluded_ids
    assert equipment not in generation.conditions.available_equipment
    assert forbidden in generation.conditions.excluded_ids
