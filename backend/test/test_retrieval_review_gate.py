import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from langfuse.api.commons.types.observation_level import ObservationLevel
from langfuse.api.experiments.types.experiment_item import ExperimentItem
from langfuse.api.experiments.types.experiment_items_response import (
    ExperimentItemsResponse,
)
from langfuse.api.experiments.types.experiments_response_meta import (
    ExperimentsResponseMeta,
)

from app.application.business import retrieval_text as rt
from app.application.business.catalog import Catalog
from app.application.business.retrieval_rerank import (
    RerankIdentity,
    RerankResult,
    RerankScored,
    RerankUsage,
)
from app.application.business.retrieval_search import RetrievalCandidate
from evaluation import langfuse_retrieval as lr
from evaluation import retrieval_support as support
from evaluation.dataset import load_for_evaluation
from evaluation.retrieval import validate_group

DATASET_PATH = support.BACKEND / "dataset/evaluation/rebuild-v1"
QUERY_ID = "q-lookup-001"
UNREVIEWED = "0022"
ARTIFACT = (
    support.BACKEND
    / "temp/retrieval-mteb/20261020T120245Z-c7542e56/evaluation.json"
)
IDENTITY = RerankIdentity(
    "qwen3.7-text-rerank",
    "https://example.invalid/rerank",
    rt.ENCODING_RULE_VERSION,
)


@pytest.fixture(scope="module")
def dataset():
    return load_for_evaluation(DATASET_PATH)


@pytest.fixture(scope="module")
def reviewed(dataset):
    return dataset.reviewed_corpus_ids()


@pytest.fixture(scope="module")
def query(dataset):
    return next(item for item in dataset.queries if item["query_id"] == QUERY_ID)


@pytest.fixture(scope="module")
def row(dataset):
    return next(
        item
        for item in lr.example_rows(dataset)
        if item["inputs"]["query_id"] == QUERY_ID
    )


def positives(row: dict) -> list[str]:
    return [
        item["exercise_id"]
        for item in row["outputs"]["labels"]
        if item["grade"] >= 1
    ]


def outputs(
    *,
    query_id: str,
    identity: dict,
    pool: list[str],
    final: list[str],
    group: str = "hybrid",
    experiment: str | None = None,
    rerank: dict | None = None,
    baseline: dict | None = None,
) -> dict:
    payload = {
        "query_id": query_id,
        "dataset_content_sha256": identity["content_sha256"],
        "strategy": group,
        "candidate_pool": pool,
        "final_model_input": final,
    }
    if experiment is not None:
        payload["experiment"] = experiment
    if rerank is not None:
        payload["rerank"] = rerank
    if baseline is not None:
        payload["baseline"] = baseline
    return payload


def feedback(
    dataset,
    reviewed: dict,
    row: dict,
    payload: dict,
    group: str = "hybrid",
) -> dict[str, float]:
    return lr.expected_feedback(
        group,
        row["inputs"],
        payload,
        row["outputs"],
        lr.dataset_binding(dataset),
        reviewed,
    )


def candidate(catalog: Catalog, exercise_id: str, score: float) -> RetrievalCandidate:
    exercise = catalog.get(exercise_id)
    return RetrievalCandidate(
        id=exercise.id,
        score=score,
        name=exercise.name,
        equipment=exercise.equipment,
        body_part=exercise.body_part,
        target=exercise.target,
        muscle_group=exercise.muscle_group,
    )


def projection(
    query: dict,
    pool: list[tuple[str, float]],
    *,
    threshold: float | None = 0.0,
    rerank: bool = False,
):
    catalog = Catalog.load()
    mappings = rt.load_mappings()
    evaluated = support.dataset_conditions(query, catalog, mappings)
    recalled = [candidate(catalog, exercise_id, score) for exercise_id, score in pool]
    if not rerank:
        return support.project_response(
            catalog,
            query["query_id"],
            recalled,
            evaluated,
            threshold,
            experiment=support.EXPERIMENT_BASELINE,
        )
    scores = {
        item.id: (index + 1) / (len(recalled) + 1)
        for index, item in enumerate(recalled)
    }
    scored = [
        RerankScored(item.id, scores[item.id], rank)
        for rank, item in enumerate(recalled)
    ]
    scored.sort(key=lambda item: (-item.rerank_score, item.rrf_rank))
    result = RerankResult(
        tuple(scored),
        RerankUsage(1, len(recalled), len(recalled)),
        "request-id",
    )
    return support.project_response(
        catalog,
        query["query_id"],
        recalled,
        evaluated,
        threshold,
        rerank=result,
        rerank_identity=IDENTITY,
        experiment=support.EXPERIMENT_RERANK,
    )


def test_reviewed_candidates_reuse_dataset_rule(dataset, reviewed, row):
    pool = sorted(reviewed[QUERY_ID])
    assert lr.unreviewed_in_pool(reviewed, QUERY_ID, pool) == []
    assert lr.unreviewed_in_pool(reviewed, QUERY_ID, pool + [UNREVIEWED]) == [
        UNREVIEWED
    ]
    assert dataset.unreviewed_candidates({QUERY_ID: pool + [UNREVIEWED]}) == {
        QUERY_ID: [UNREVIEWED]
    }
    assert lr.review_evidence(reviewed, QUERY_ID, pool + [UNREVIEWED]) == {
        "status": "blocked_unreviewed_candidates",
        "query_id": QUERY_ID,
        "unreviewed_candidates": [UNREVIEWED],
    }
    assert lr.review_evidence(reviewed, QUERY_ID, pool)["status"] == "complete"


def test_gate_blocks_unreviewed_candidate_in_top_five(dataset, reviewed, row):
    pool = [UNREVIEWED, *positives(row)[:2]]
    payload = outputs(
        query_id=QUERY_ID,
        identity=lr.dataset_binding(dataset),
        pool=pool,
        final=pool[:2],
    )
    with pytest.raises(support.EvaluationBlocked) as error:
        feedback(dataset, reviewed, row, payload)
    assert QUERY_ID in error.value.reason
    assert error.value.missing == (UNREVIEWED,)


def test_gate_blocks_unreviewed_candidate_after_fifth(dataset, reviewed, row):
    pool = [*positives(row)[:5], UNREVIEWED]
    payload = outputs(
        query_id=QUERY_ID,
        identity=lr.dataset_binding(dataset),
        pool=pool,
        final=positives(row)[:5],
    )
    with pytest.raises(support.EvaluationBlocked) as error:
        feedback(dataset, reviewed, row, payload)
    assert error.value.missing == (UNREVIEWED,)


def test_gate_blocks_unreviewed_candidate_cut_by_threshold(dataset, reviewed, row):
    pool = [
        *((item, 10.0 - index) for index, item in enumerate(positives(row)[:5])),
        (UNREVIEWED, -1.0),
    ]
    query = next(item for item in dataset.queries if item["query_id"] == QUERY_ID)
    baseline = projection(query, pool)
    assert UNREVIEWED in baseline["candidate_pool"]
    assert UNREVIEWED not in baseline["final_model_input"]
    payload = outputs(
        query_id=QUERY_ID,
        identity=lr.dataset_binding(dataset),
        pool=baseline["candidate_pool"],
        final=baseline["final_model_input"],
        experiment=baseline["experiment"],
    )
    with pytest.raises(support.EvaluationBlocked) as error:
        feedback(dataset, reviewed, row, payload)
    assert error.value.missing == (UNREVIEWED,)


def test_reviewed_grade_zero_candidate_does_not_block(dataset, reviewed, row):
    grade_zero = next(
        item["corpus_id"]
        for item in dataset.qrels
        if item["query_id"] == QUERY_ID and item["grade"] == 0
    )
    assert grade_zero in reviewed[QUERY_ID]
    pool = [grade_zero, *positives(row)[:2]]
    payload = outputs(
        query_id=QUERY_ID,
        identity=lr.dataset_binding(dataset),
        pool=pool,
        final=positives(row)[:2],
    )
    scores = feedback(dataset, reviewed, row, payload)
    assert set(scores) == set(lr.RETRIEVAL_METRICS)


def test_fully_reviewed_query_scores_unchanged(dataset, reviewed, row):
    pool = sorted(reviewed[QUERY_ID])
    final = positives(row)[:5]
    payload = outputs(
        query_id=QUERY_ID,
        identity=lr.dataset_binding(dataset),
        pool=pool,
        final=final,
    )
    scores = feedback(dataset, reviewed, row, payload)
    assert set(scores) == set(lr.RETRIEVAL_METRICS)
    assert scores["recall@20"] == 1.0
    assert scores["hit@5"] == 1.0


def test_empty_expectation_query_uses_same_review_rule(dataset, reviewed):
    empty_query = next(
        item for item in dataset.queries if item["query_id"] == "q-lookup-051"
    )
    empty_row = next(
        item
        for item in lr.example_rows(dataset)
        if item["inputs"]["query_id"] == empty_query["query_id"]
    )
    empty_reviewed = sorted(reviewed[empty_query["query_id"]])
    assert empty_row["outputs"]["expect_empty"] is True
    accepted = outputs(
        query_id=empty_query["query_id"],
        identity=lr.dataset_binding(dataset),
        pool=empty_reviewed,
        final=[],
    )
    assert feedback(dataset, reviewed, empty_row, accepted) == {"false_return": 0.0}
    blocked = outputs(
        query_id=empty_query["query_id"],
        identity=lr.dataset_binding(dataset),
        pool=[UNREVIEWED],
        final=[],
    )
    with pytest.raises(support.EvaluationBlocked):
        feedback(dataset, reviewed, empty_row, blocked)


def test_empty_pool_keeps_existing_scoring(dataset, reviewed):
    empty_query = next(
        item for item in dataset.queries if item["query_id"] == "q-lookup-051"
    )
    empty_row = next(
        item
        for item in lr.example_rows(dataset)
        if item["inputs"]["query_id"] == empty_query["query_id"]
    )
    payload = outputs(
        query_id=empty_query["query_id"],
        identity=lr.dataset_binding(dataset),
        pool=[],
        final=[],
    )
    assert feedback(dataset, reviewed, empty_row, payload) == {"false_return": 0.0}


async def test_evaluator_does_not_bypass_review_gate(dataset, reviewed, row):
    pool = [*positives(row)[:5], UNREVIEWED]
    payload = outputs(
        query_id=QUERY_ID,
        identity=lr.dataset_binding(dataset),
        pool=pool,
        final=positives(row)[:5],
    )
    evaluator = lr.make_evaluator(
        "hybrid", lr.dataset_binding(dataset), reviewed
    )
    with pytest.raises(support.EvaluationBlocked) as error:
        await evaluator(
            input=row["inputs"],
            output=payload,
            expected_output=row["outputs"],
            metadata=None,
        )
    assert error.value.missing == (UNREVIEWED,)


def test_parity_report_refuses_unreviewed_candidates(dataset, reviewed, row, query):
    pool = [*positives(row)[:2], UNREVIEWED]
    payload = outputs(
        query_id=QUERY_ID,
        identity=lr.dataset_binding(dataset),
        pool=pool,
        final=positives(row)[:2],
    )
    samples = [
        {
            "query_id": QUERY_ID,
            "status": "complete",
            "outputs": payload,
            "feedback": {},
        }
    ]
    with pytest.raises(support.EvaluationBlocked) as error:
        lr.parity_report(
            dataset,
            [query],
            samples,
            lr.RetrievalCorpus.load(),
            "hybrid",
            lr.validated_parameters(),
        )
    assert error.value.missing == (UNREVIEWED,)


class BlockedItemApi:
    def __init__(self, item) -> None:
        self.item = item

    @property
    def experiments(self):
        return self

    def list_items(self, **kwargs):
        return ExperimentItemsResponse(data=[self.item], meta=ExperimentsResponseMeta())


def blocked_item(example_id: str, payload: dict):
    start = datetime.now(UTC)
    return ExperimentItem(
        id=str(uuid4()),
        trace_id=uuid4().hex,
        start_time=start,
        end_time=start,
        level=ObservationLevel.DEFAULT,
        environment="default",
        experiment_id="experiment",
        experiment_name="run-hybrid-validation",
        experiment_item_id=example_id,
        output=payload,
        scores=[],
    )


class BlockedRunClient:
    def __init__(self, item) -> None:
        self.api = BlockedItemApi(item)


def test_verify_group_marks_review_blocked_evidence(dataset, reviewed, row):
    pool = [UNREVIEWED, *positives(row)[:2]]
    payload = {
        **outputs(
            query_id=QUERY_ID,
            identity=lr.dataset_binding(dataset),
            pool=pool,
            final=pool[:2],
        ),
        "review": lr.review_evidence(reviewed, QUERY_ID, pool),
    }
    samples, coverage = lr.verify_group(
        BlockedRunClient(blocked_item(row["id"], payload)),
        group="hybrid",
        dataset_identity=lr.dataset_binding(dataset),
        reviewed=reviewed,
        rows={row["id"]: row},
        selected_example_ids=[row["id"]],
        experiment_id="experiment",
        from_start_time=datetime.now(UTC),
    )
    assert coverage["status"] == "incomplete"
    assert coverage["unknown_candidates"] == {QUERY_ID: [UNREVIEWED]}
    assert samples[0]["status"] == "blocked_unreviewed_candidates"
    assert samples[0]["outputs"] == payload
    assert samples[0]["unknown_candidates"] == [UNREVIEWED]
    assert UNREVIEWED in samples[0]["error"]


def test_same_pool_baseline_and_rerank_block_identically(dataset, reviewed, row):
    query = next(item for item in dataset.queries if item["query_id"] == QUERY_ID)
    pool = [
        *((item, 10.0 - index) for index, item in enumerate(positives(row)[:2])),
        (UNREVIEWED, 1.0),
    ]
    baseline = projection(query, pool)
    reranked = projection(query, pool, rerank=True)
    assert baseline["candidate_pool"] == reranked["candidate_pool"]
    assert baseline["scores"] == reranked["scores"]
    identity = lr.dataset_binding(dataset)
    baseline_outputs = outputs(
        query_id=QUERY_ID,
        identity=identity,
        pool=baseline["candidate_pool"],
        final=baseline["final_model_input"],
        experiment=baseline["experiment"],
    )
    rerank_outputs = outputs(
        query_id=QUERY_ID,
        identity=identity,
        pool=reranked["candidate_pool"],
        final=reranked["final_model_input"],
        group=lr.RERANK_GROUP,
        experiment=reranked["experiment"],
        rerank=support.public_rerank_evidence(reranked["rerank"]),
        baseline={
            "candidate_pool": baseline["candidate_pool"],
            "final_model_input": baseline["final_model_input"],
        },
    )
    with pytest.raises(support.EvaluationBlocked) as baseline_error:
        feedback(dataset, reviewed, row, baseline_outputs)
    with pytest.raises(support.EvaluationBlocked) as rerank_error:
        feedback(dataset, reviewed, row, rerank_outputs, group=lr.RERANK_GROUP)
    assert baseline_error.value.missing == rerank_error.value.missing == (UNREVIEWED,)


def test_local_artifact_reproduction_blocks_same_candidates(dataset, reviewed):
    if not ARTIFACT.is_file():
        pytest.skip("本地复现产物不存在")
    document = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    group = document["groups"]["hybrid"]
    projection_row = group["projections"][QUERY_ID]
    expected = group["unknown_candidates"][QUERY_ID]
    assert expected == ["0022", "0589", "0574", "0055", "0788"]

    query = next(item for item in dataset.queries if item["query_id"] == QUERY_ID)
    local = validate_group(
        dataset,
        "hybrid",
        {QUERY_ID: projection_row},
        {},
        None,
        queries=[query],
    )
    assert local["status"] == "blocked_unreviewed_candidates"
    assert local["unknown_candidates"] == {QUERY_ID: expected}
    assert lr.unreviewed_in_pool(reviewed, QUERY_ID, projection_row["candidate_pool"]) == expected
