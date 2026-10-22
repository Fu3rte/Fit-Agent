import copy
import json
from pathlib import Path

import pytest

from app.application.business import retrieval_text as rt
from app.application.business.catalog import Catalog
from app.application.business.retrieval_rerank import (
    RerankIdentity,
    RerankResult,
    RerankScored,
    RerankUsage,
)
from app.application.business.retrieval_response import build_search_stage
from app.application.business.retrieval_search import RetrievalCandidate
from evaluation import retrieval_support as support
from evaluation.contracts import RelevanceLabel, RerankEvidence, RetrievalInput
from evaluation.dataset import load_for_evaluation
from evaluation.retrieval import (
    RERANK_GROUP,
    replay_document,
    settle_document,
    upload_payload,
    validate_projections,
)
from evaluation.retrieval_metrics import evaluate_staged
from evaluation.scenarios import SearchStageRecord, executed_index_identity
from test.evaluation_fixtures import build_dataset, make_document

DATASET_PATH = support.BACKEND / "dataset/evaluation/rebuild-v1"
ENDPOINT = (
    "https://ws-example.cn-beijing.maas.aliyuncs.com"
    "/api/v1/services/rerank/text-rerank/text-rerank"
)
IDENTITY = RerankIdentity("qwen3.7-text-rerank", ENDPOINT, rt.ENCODING_RULE_VERSION)


@pytest.fixture(scope="module")
def dataset():
    return load_for_evaluation(DATASET_PATH)


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


def rerank_result(
    pool: list[RetrievalCandidate], scores: dict[str, float]
) -> RerankResult:
    rows = [
        RerankScored(item.id, scores[item.id], rank)
        for rank, item in enumerate(pool)
    ]
    rows.sort(key=lambda row: (-row.rerank_score, row.rrf_rank))
    return RerankResult(tuple(rows), RerankUsage(1, 20, 20), "request-id")


def reviewed_query(dataset, *, empty: bool = False) -> dict:
    qrels = dataset.qrels_by_query()
    for item in dataset.queries:
        positives = [row for row in qrels.get(item["query_id"], ()) if row["grade"] >= 1]
        if item["expect_empty"] is empty and (empty or len(positives) >= 2):
            return item
    raise AssertionError("缺少测试查询")


def projections(dataset, query: dict):
    catalog = Catalog.load()
    mappings = rt.load_mappings()
    qrels = dataset.qrels_by_query()[query["query_id"]]
    ids = [row["corpus_id"] for row in qrels if row["grade"] >= 1]
    recalled = [
        candidate(catalog, exercise_id, float(110 - index))
        for index, exercise_id in enumerate(ids)
    ]
    evaluated = support.dataset_conditions(query, catalog, mappings)
    baseline = support.project_response(
        catalog,
        query["query_id"],
        recalled,
        evaluated,
        0.0,
        experiment=support.EXPERIMENT_BASELINE,
    )
    scores = {item.id: (index + 1) / (len(recalled) + 1) for index, item in enumerate(recalled)}
    result = (
        rerank_result(recalled, scores)
        if recalled
        else RerankResult((), RerankUsage(), None)
    )
    reranked = support.project_response(
        catalog,
        query["query_id"],
        recalled,
        evaluated,
        0.0,
        rerank=result,
        rerank_identity=IDENTITY,
        experiment=support.EXPERIMENT_RERANK,
    )
    return catalog, evaluated, recalled, baseline, reranked


def query_item(dataset, query: dict) -> dict:
    return next(
        item for item in dataset.scoring_queries() if item["id"] == query["query_id"]
    )


def test_rerank_projection_records_identity_and_reorders_final_input(dataset):
    query = reviewed_query(dataset)
    catalog, evaluated, recalled, baseline, reranked = projections(dataset, query)
    assert baseline["experiment"] == support.EXPERIMENT_BASELINE
    assert baseline["rerank"] is None
    assert reranked["experiment"] == support.EXPERIMENT_RERANK
    evidence = RerankEvidence.model_validate(reranked["rerank"])
    assert evidence.identity.model == IDENTITY.model
    assert evidence.identity.endpoint == ENDPOINT
    assert [row.id for row in evidence.scored] == [
        item.id for item in reversed(recalled)
    ]
    assert reranked["candidate_pool"] == baseline["candidate_pool"]
    assert reranked["scores"] == baseline["scores"]
    assert reranked["final_model_input"] == [
        item.id for item in reversed(recalled)
    ][:5]
    stage = build_search_stage(
        [candidate(catalog, eid, reranked["scores"][eid]) for eid in reranked["candidate_pool"]],
        evaluated,
        rerank=support.rebuild_rerank(reranked["rerank"])[0],
        rerank_identity=IDENTITY,
        threshold=0.0,
    )
    assert [item.id for item in stage.final_input] == reranked["final_model_input"]
    assert stage.response.model_dump() == reranked["response"]


def test_same_pool_keeps_recall_and_rerank_changes_at_five(dataset):
    query = reviewed_query(dataset)
    _, _, _, baseline, reranked = projections(dataset, query)
    item = query_item(dataset, query)
    inputs = []
    for projection in (baseline, reranked):
        inputs.append(
            RetrievalInput(
                query_id=item["id"],
                scenario_id=item.get("scenario_id"),
                expect_empty=item["expect_empty"],
                labels=[
                    RelevanceLabel(exercise_id=row["id"], grade=row["grade"])
                    for row in item["labels"]
                ],
                candidate_pool=projection["candidate_pool"],
                final_model_input=projection["final_model_input"],
            )
        )
    base_result = evaluate_staged([inputs[0]])[0]
    rerank_result = evaluate_staged([inputs[1]])[0]
    base_scores = {row.name: row.value for row in base_result.metrics}
    rerank_scores = {row.name: row.value for row in rerank_result.metrics}
    assert base_scores["recall@20"] == rerank_scores["recall@20"]
    assert base_scores["hit@5"] == 1.0 == rerank_scores["hit@5"]
    assert baseline["final_model_input"] != reranked["final_model_input"]


def test_empty_pool_rerank_records_zero_request_and_no_id(dataset):
    query = reviewed_query(dataset, empty=True)
    _, _, recalled, _, reranked = projections(dataset, query)
    assert recalled == []
    evidence = RerankEvidence.model_validate(reranked["rerank"])
    assert evidence.scored == []
    assert evidence.usage.requests == 0
    assert evidence.request_id is None
    assert reranked["final_model_input"] == []
    assert reranked["response"] == {"items": [], "has_more": False}


def test_rerank_evidence_accepts_unreported_prompt_tokens():
    evidence = RerankEvidence(
        mode="rerank",
        identity={"model": "m", "endpoint": "https://e", "text_version": "v"},
        scored=[
            {"id": "0002", "rerank_score": 0.9, "rrf_rank": 1},
            {"id": "0001", "rerank_score": 0.5, "rrf_rank": 0},
        ],
        usage={"requests": 1, "prompt_tokens": None, "total_tokens": 12},
        request_id="request-unreported",
    )
    assert evidence.usage.prompt_tokens is None
    assert evidence.usage.total_tokens == 12
    rebuilt, _ = support.rebuild_rerank(evidence.model_dump(mode="json"))
    assert rebuilt.usage.prompt_tokens is None
    assert rebuilt.usage.total_tokens == 12
    assert rebuilt.usage.requests == 1
    assert rebuilt.request_id == "request-unreported"


def test_validate_projections_rebuilds_rerank_stage(dataset):
    query = reviewed_query(dataset)
    catalog, evaluated, _, _, reranked = projections(dataset, query)
    item = query_item(dataset, query)
    metrics = validate_projections(
        [item], {item["id"]: reranked}, catalog, {item["id"]: evaluated}, 0.0
    )
    assert metrics["overall"]["recall@20"] is not None
    assert metrics["per_query"][item["id"]]["final_model_input"] == reranked["final_model_input"]


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda row: row.pop("rerank"), "缺少证据"),
        (
            lambda row: row["rerank"].pop("identity"),
            "RerankEvidence",
        ),
        (
            lambda row: row["rerank"]["scored"].pop(),
            "数量",
        ),
        (
            lambda row: row["rerank"]["scored"][0].update({"rrf_rank": 19}),
            "原排名",
        ),
        (
            lambda row: row["rerank"]["scored"][0].update({"id": "9999"}),
            "候选池",
        ),
        (
            lambda row: row["rerank"]["scored"].reverse(),
            "降序",
        ),
        (
            lambda row: row.update({"experiment": "unknown"}),
            "实验身份",
        ),
        (
            lambda row: row.update({"final_model_input": list(reversed(row["final_model_input"]))}),
            "模型可见",
        ),
    ],
)
def test_replay_rejects_corrupt_rerank_evidence(dataset, mutate, message):
    query = reviewed_query(dataset)
    catalog, evaluated, _, _, reranked = projections(dataset, query)
    row = copy.deepcopy(reranked)
    mutate(row)
    item = query_item(dataset, query)
    with pytest.raises(ValueError, match=message):
        validate_projections(
            [item], {item["id"]: row}, catalog, {item["id"]: evaluated}, 0.0
        )


def test_historical_projection_without_experiment_still_replays(dataset):
    query = reviewed_query(dataset)
    catalog, evaluated, _, baseline, _ = projections(dataset, query)
    historical = copy.deepcopy(baseline)
    historical.pop("experiment")
    historical.pop("rerank")
    item = query_item(dataset, query)
    metrics = validate_projections(
        [item], {item["id"]: historical}, catalog, {item["id"]: evaluated}, 0.0
    )
    assert metrics["overall"]["recall@20"] == 1.0


def rerank_document(version_dir: Path):
    document, dataset = make_document(version_dir)
    catalog = Catalog.load()
    mappings = rt.load_mappings()
    base = document["groups"]["hybrid"]
    reranked_projections = {}
    for item in dataset.queries:
        qid = item["query_id"]
        projection = base["projections"][qid]
        evaluated = support.dataset_conditions(item, catalog, mappings)
        recalled = [
            candidate(catalog, eid, projection["scores"][eid])
            for eid in projection["recalled"]
        ]
        pool = [
            candidate(catalog, eid, projection["scores"][eid])
            for eid in projection["candidate_pool"]
        ]
        scores = {
            row.id: (index + 1) / (len(pool) + 1)
            for index, row in enumerate(pool)
        }
        result = rerank_result(pool, scores) if pool else RerankResult((), RerankUsage(), None)
        reranked_projections[qid] = support.project_response(
            catalog,
            qid,
            recalled,
            evaluated,
            projection["threshold"],
            rerank=result,
            rerank_identity=IDENTITY,
            experiment=support.EXPERIMENT_RERANK,
        )
    document["experiments"] = {
        "hybrid": support.EXPERIMENT_BASELINE,
        RERANK_GROUP: support.EXPERIMENT_RERANK,
    }
    document["rerank_identity"] = {
        "model": IDENTITY.model,
        "endpoint": IDENTITY.endpoint,
        "text_version": IDENTITY.text_version,
    }
    evaluated_by_query = {
        item["query_id"]: support.dataset_conditions(item, catalog, mappings)
        for item in dataset.queries
    }
    document["groups"][RERANK_GROUP] = {
        "status": "complete",
        "metrics": validate_projections(
            dataset.scoring_queries(),
            reranked_projections,
            catalog,
            evaluated_by_query,
            0.0,
        ),
        "projections": reranked_projections,
        "timings": copy.deepcopy(base["timings"]),
    }
    settle_document(document)
    return document, dataset


@pytest.fixture(scope="module")
def evaluated_document(tmp_path_factory):
    version_dir = build_dataset(tmp_path_factory.mktemp("rerank-document"))
    return rerank_document(version_dir)


def test_document_replays_without_models_or_database(evaluated_document, monkeypatch):
    document, _ = evaluated_document
    document = copy.deepcopy(document)

    def forbidden(*args, **kwargs):
        raise AssertionError("回放不得调用模型或数据库检索")

    monkeypatch.setattr(
        "app.application.business.retrieval_rerank.RerankClient.rerank", forbidden
    )
    monkeypatch.setattr(
        "evaluation.retrieval_support.search_vectors", forbidden
    )
    report = replay_document(document)
    assert report["status"] == "complete"
    assert report["groups"][RERANK_GROUP]["experiment"] == support.EXPERIMENT_RERANK


def test_upload_rerank_payload_uses_whitelist_and_hashes_endpoint(evaluated_document):
    document, _ = evaluated_document
    payload = upload_payload(document, RERANK_GROUP)
    row = payload["results"][0]
    assert row["actual_outputs"]["experiment"] == support.EXPERIMENT_RERANK
    rerank = row["actual_outputs"]["rerank"]
    assert rerank["identity"]["model"] == IDENTITY.model
    assert rerank["identity"]["endpoint"] == support.sha256_text(ENDPOINT)
    assert "https://" not in json.dumps(payload, ensure_ascii=False)
    assert "RERANKER_API_KEY" not in json.dumps(payload, ensure_ascii=False)
    assert row["actual_outputs"]["baseline"]["candidate_pool"] == row["actual_outputs"]["candidate_pool"]
    assert payload["experiment_metadata"]["rerank"]["endpoint_sha256"] == support.sha256_text(ENDPOINT)


def test_same_pool_validator_rejects_separate_recalls(evaluated_document):
    document, _ = evaluated_document
    broken = copy.deepcopy(document)
    qid = next(iter(broken["groups"][RERANK_GROUP]["projections"]))
    broken["groups"][RERANK_GROUP]["projections"][qid]["scores"] = {"9999": 1.0}
    with pytest.raises(ValueError, match="同池 RRF 分数|分数"):
        replay_document(broken)


def test_scenario_record_saves_and_validates_rerank_evidence():
    pool = ["0001", "0002"]
    evidence = RerankEvidence(
        mode="rerank",
        identity={"model": "m", "endpoint": "https://e", "text_version": "v"},
        scored=[
            {"id": "0002", "rerank_score": 0.9, "rrf_rank": 1},
            {"id": "0001", "rerank_score": 0.5, "rrf_rank": 0},
        ],
        usage={"requests": 1, "prompt_tokens": 4, "total_tokens": 4},
        request_id="r",
    )
    record = SearchStageRecord(
        sequence=0,
        run_id="run",
        scenario_id="s",
        event_id="e",
        tool_call_id="t",
        index_collection="c",
        index_manifest_sha256=support.sha256_text("{}"),
        index_manifest_json="{}",
        arguments={},
        candidate_pool=pool,
        final_model_input=["0002", "0001"],
        response_json={"items": [], "has_more": False},
        scores=[1.0, 0.5],
        threshold=0.0,
        experiment="rerank",
        rerank=evidence,
    )
    retrieval = {"missing": [], "query_vector_identity": support.json_text({"k": "v"})}
    executed_index_identity([record], retrieval)
    assert retrieval["experiment"] == "rerank"
    assert json.loads(retrieval["rerank"]) == {
        "model": "m",
        "endpoint": support.sha256_text("https://e"),
        "text_version": "v",
    }
    with pytest.raises(ValueError):
        SearchStageRecord.model_validate(
            {
                **record.model_dump(mode="json"),
                "experiment": "rrf_baseline",
            }
        )


def stage_record(
    evidence: RerankEvidence | None,
    *,
    experiment: str,
    sequence: int = 0,
) -> SearchStageRecord:
    return SearchStageRecord(
        sequence=sequence,
        run_id="run",
        scenario_id="s",
        event_id="e",
        tool_call_id="t",
        index_collection="c",
        index_manifest_sha256=support.sha256_text("{}"),
        index_manifest_json="{}",
        arguments={},
        candidate_pool=["0001", "0002"],
        final_model_input=["0002", "0001"],
        response_json={"items": [], "has_more": False},
        scores=[1.0, 0.5],
        threshold=0.0,
        experiment=experiment,
        rerank=evidence,
    )


def rerank_evidence(model: str) -> RerankEvidence:
    return RerankEvidence(
        mode="rerank",
        identity={"model": model, "endpoint": "https://e", "text_version": "v"},
        scored=[
            {"id": "0002", "rerank_score": 0.9, "rrf_rank": 1},
            {"id": "0001", "rerank_score": 0.5, "rrf_rank": 0},
        ],
        usage={"requests": 1, "prompt_tokens": 4, "total_tokens": 4},
        request_id="r",
    )


def test_rerank_identity_aggregation_blocks_conflicted_and_mixed_runs():
    single = {"missing": []}
    executed_index_identity([stage_record(rerank_evidence("m"), experiment="rerank")], single)
    assert single["experiment"] == "rerank"
    assert single["missing"] == []
    assert json.loads(single["rerank"])["model"] == "m"

    baseline = {"missing": []}
    executed_index_identity([stage_record(None, experiment="rrf_baseline")], baseline)
    assert baseline["experiment"] == "rrf_baseline"
    assert baseline["missing"] == []
    assert "rerank" not in baseline

    conflict = {"missing": []}
    executed_index_identity(
        [
            stage_record(rerank_evidence("m"), experiment="rerank"),
            stage_record(rerank_evidence("other-model"), experiment="rerank", sequence=1),
        ],
        conflict,
    )
    assert conflict["missing"] == ["retrieval_rerank_identity:单次运行存在多个重排身份"]
    assert "rerank" not in conflict
    assert json.loads(conflict["rerank_conflict"]) == [
        {"model": "m", "endpoint": "https://e", "text_version": "v"},
        {"model": "other-model", "endpoint": "https://e", "text_version": "v"},
    ]

    mixed = {"missing": []}
    executed_index_identity(
        [
            stage_record(rerank_evidence("m"), experiment="rerank"),
            stage_record(None, experiment="rrf_baseline", sequence=1),
        ],
        mixed,
    )
    assert mixed["missing"] == ["retrieval_rerank_identity:重排与基线记录混合"]
    assert mixed["experiment"] == "rrf_baseline"


def test_langsmith_rerank_target_evidence_replays(dataset):
    from evaluation import langsmith_retrieval as lr

    query = reviewed_query(dataset)
    _, _, _, baseline, reranked = projections(dataset, query)
    rows = lr.example_rows(dataset)
    row = next(
        item for item in rows if item["inputs"]["query_id"] == query["query_id"]
    )
    identity = lr.dataset_binding(dataset)
    outputs = {
        "query_id": query["query_id"],
        "strategy": lr.RERANK_GROUP,
        "dataset_content_sha256": identity["content_sha256"],
        "experiment": support.EXPERIMENT_RERANK,
        "candidate_pool": reranked["candidate_pool"],
        "final_model_input": reranked["final_model_input"],
        "scores": reranked["scores"],
        "search_evidence": reranked["search_evidence"],
        "response": reranked["response"],
        "rerank": support.public_rerank_evidence(reranked["rerank"]),
        "baseline": {
            "candidate_pool": baseline["candidate_pool"],
            "final_model_input": baseline["final_model_input"],
        },
    }
    reviewed = dataset.reviewed_corpus_ids()
    item = lr.retrieval_input(
        row["inputs"],
        outputs,
        row["outputs"],
        group=lr.RERANK_GROUP,
        dataset_identity=identity,
        reviewed=reviewed,
    )
    assert item.final_model_input == reranked["final_model_input"]
    assert item.candidate_pool == baseline["candidate_pool"]
    assert lr.expected_feedback(
        lr.RERANK_GROUP, row["inputs"], outputs, row["outputs"], identity, reviewed
    )["recall@20"] == lr.expected_feedback(
        lr.RERANK_GROUP,
        row["inputs"],
        {**outputs, "final_model_input": baseline["final_model_input"]},
        row["outputs"],
        identity,
        reviewed,
    )["recall@20"]
    metadata = lr.experiment_metadata(
        run_id="run",
        group=lr.RERANK_GROUP,
        purpose="validation",
        dataset=dataset,
        parameters=lr.validated_parameters(),
        collection="collection",
        index_manifest_sha256="a" * 64,
        vector_identity_sha256="b" * 64,
        vector_required=True,
        selected=[query],
        identity=lr.sdk_code_identity(),
        rerank_identity={
            "model": IDENTITY.model,
            "text_version": IDENTITY.text_version,
            "endpoint_sha256": support.sha256_text(ENDPOINT),
        },
    )
    assert metadata["experiment"] == support.EXPERIMENT_RERANK
    assert metadata["rerank_identity"]["endpoint_sha256"] == support.sha256_text(ENDPOINT)


def test_vector_cache_identity_does_not_depend_on_rerank():
    from types import SimpleNamespace

    from app.application.business.retrieval_index import RetrievalCorpus

    identity = support.query_vector_identity(
        SimpleNamespace(
            model="test-embedding",
            dimensions=1024,
            base_url="https://example.invalid/compatible-mode/v1",
        ),
        RetrievalCorpus.load(),
    )
    text = support.json_text(identity)
    assert "rerank" not in text.casefold()
    assert "RERANKER" not in text
