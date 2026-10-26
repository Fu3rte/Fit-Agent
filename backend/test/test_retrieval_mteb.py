import copy
import json
import math
import subprocess
import sys

import pytest

from app.application.business.catalog import Catalog
from app.application.business.retrieval_conditions import (
    EvaluatedConditions,
    SearchConditions,
)
from app.application.business.retrieval_response import (
    SEARCH_BUDGET,
    SEARCH_LIMIT,
    encoded_length,
)
from evaluation import retrieval_support as support
from evaluation.contracts import (
    ModelVisibleSearch,
    RelevanceLabel,
    RetrievalInput,
    SearchResponseEvidence,
    VisibleExercise,
)
from evaluation.retrieval import (
    base_document,
    identity,
    publishable,
    record_report,
    save_result,
    settle_document,
    upload_payload,
    validate_group,
    validate_history,
    validate_projections,
)
from evaluation.retrieval_metrics import DEFAULT_KS, evaluate_staged, score_legacy
from test.evaluation_fixtures import build_dataset, make_document

METRIC_NAMES = ("hit", "recall", "precision", "mrr", "ndcg")


def query(qid="q1", labels=None, empty=False, intent="standard_name"):
    return {
        "id": qid,
        "intent": intent,
        "family": "bench",
        "split": "dev",
        "labels": [{"id": "0001", "grade": 3}, {"id": "0002", "grade": 2}] if labels is None else labels,
        "expect_empty": empty,
    }


def test_known_ranking_math():
    result = score_legacy([query()], {"q1": ["0099", "0001", "0002"]}, ks=(1, 2, 5))
    stats = result["overall"]
    assert stats["hit@1"] == 0
    assert stats["hit@2"] == 1
    assert stats["recall@2"] == 0.5
    assert stats["precision@5"] == 0.4
    assert stats["mrr@5"] == 0.5
    expected = (7 / math.log2(3) + 3 / math.log2(4)) / (7 + 3 / math.log2(3))
    assert stats["ndcg@5"] == pytest.approx(expected, abs=1e-12)
    assert result["engine"]["aliases"]["ndcg@5"] == "ndcg_cut_5"
    assert result["engine"]["aliases"]["hit@5"] == "success_5"
    assert result["engine"]["aliases"]["mrr@5"] == "MRR@5"


def test_default_scores_are_at_five():
    result = score_legacy([query()], {"q1": ["0001"]})
    aliases = {f"{name}@5" for name in METRIC_NAMES}
    assert DEFAULT_KS == (5,)
    assert set(result["engine"]["aliases"]) == aliases
    assert set(result["overall"]) == aliases | {"queries", "with_labels", "empty_expectation_queries", "false_return_rate"}
    assert set(result["per_query"]["q1"]["scores"]) == aliases
    assert "recall@20" not in result["overall"]
    assert result["engine"]["identity"] == "historical_single_stage"


def test_grade_one_counts_as_relevant():
    item = query(labels=[{"id": "0001", "grade": 1}])
    stats = score_legacy([item], {"q1": ["0001"]})["overall"]
    assert stats["hit@5"] == 1.0
    assert stats["recall@5"] == 1.0
    assert stats["precision@5"] == 0.2
    assert stats["mrr@5"] == 1.0
    assert stats["ndcg@5"] == pytest.approx(1.0)


def test_graded_gain_mapping():
    item = query(labels=[{"id": "0001", "grade": 3}, {"id": "0002", "grade": 1}])
    result = score_legacy([item], {"q1": ["0002", "0001"]})
    assert result["per_query"]["q1"]["expected_ids"] == {"0001": 3, "0002": 1}
    assert result["engine"]["gain_mapping"] == {"1": 1, "2": 3, "3": 7}
    contract = (1 + 7 / math.log2(3)) / (7 + 1 / math.log2(3))
    linear = (1 + 3 / math.log2(3)) / (3 + 1 / math.log2(3))
    assert result["overall"]["ndcg@5"] == pytest.approx(contract, abs=1e-12)
    assert result["overall"]["ndcg@5"] != pytest.approx(linear, abs=1e-12)
    assert result["overall"]["mrr@5"] == 1.0


def test_short_return_uses_five_as_precision_denominator():
    stats = score_legacy([query()], {"q1": ["0001"]})["overall"]
    assert stats["hit@5"] == 1.0
    assert stats["recall@5"] == 0.5
    assert stats["precision@5"] == 0.2
    assert stats["mrr@5"] == 1.0
    assert stats["ndcg@5"] == pytest.approx(7 / (7 + 3 / math.log2(3)), abs=1e-12)


def test_first_relevant_at_rank_five():
    item = query(labels=[{"id": "0001", "grade": 3}])
    stats = score_legacy([item], {"q1": ["0101", "0102", "0103", "0104", "0001"]})["overall"]
    assert stats["hit@5"] == 1.0
    assert stats["recall@5"] == 1.0
    assert stats["precision@5"] == 0.2
    assert stats["mrr@5"] == 0.2
    assert stats["ndcg@5"] == pytest.approx(1 / math.log2(6), abs=1e-12)


def test_first_relevant_at_rank_six_scores_zero_at_five():
    item = query(labels=[{"id": "0001", "grade": 3}])
    result = score_legacy([item], {"q1": ["0101", "0102", "0103", "0104", "0105", "0001"]})
    for name in METRIC_NAMES:
        assert result["overall"][f"{name}@5"] == 0


def test_full_return_retained_beyond_scoring_truncation():
    item = query(labels=[{"id": "0001", "grade": 3}])
    ids = ["0101", "0102", "0103", "0104", "0105", "0001", "0106"]
    result = score_legacy([item], {"q1": ids})
    assert result["per_query"]["q1"]["returned_ids"] == ids
    assert all(result["per_query"]["q1"]["scores"][f"{name}@5"] == 0 for name in METRIC_NAMES)


def test_more_relevant_actions_than_five():
    item = query(labels=[{"id": f"{index:04}", "grade": 3} for index in range(1, 21)])
    stats = score_legacy([item], {"q1": [f"{index:04}" for index in range(1, 6)]})["overall"]
    assert stats["hit@5"] == 1.0
    assert stats["recall@5"] == 0.25
    assert stats["precision@5"] == 1.0
    assert stats["ndcg@5"] == pytest.approx(1.0)


def test_labelled_empty_return_scores_zero_and_enters_macro():
    result = score_legacy([query(), query("q2")], {"q1": [], "q2": []})
    stats = result["overall"]
    assert stats["with_labels"] == 2
    assert all(stats[f"{name}@5"] == 0 for name in METRIC_NAMES)
    assert result["per_query"]["q1"]["expected_ids"] == {"0001": 3, "0002": 2}
    assert result["per_query"]["q1"]["returned_ids"] == []
    assert result["per_query"]["q1"]["scores"]["recall@5"] == 0


def test_macro_average_includes_missed_queries():
    result = score_legacy([query(), query("q2")], {"q1": ["0001", "0002"], "q2": []})
    assert result["overall"]["recall@5"] == 0.5
    assert result["overall"]["mrr@5"] == 0.5
    assert result["per_query"]["q2"]["scores"]["recall@5"] == 0


def test_no_answer_queries_only_record_false_return():
    rows = [query(), query("empty1", [], True), query("empty2", [], True)]
    result = score_legacy(rows, {"q1": ["0001"], "empty1": ["0099"], "empty2": []})
    stats = result["overall"]
    assert stats["with_labels"] == 1
    assert stats["empty_expectation_queries"] == 2
    assert stats["false_return_rate"] == 0.5
    assert result["per_query"]["empty1"]["scores"] == {"false_return": 1.0}
    assert result["per_query"]["empty2"]["scores"] == {"false_return": 0.0}
    assert "recall@5" not in result["per_query"]["empty2"]["scores"]


def test_only_no_answer_queries_skips_ranking():
    rows = [query(labels=[], empty=True), query("q2", labels=[], empty=True)]
    result = score_legacy(rows, {"q1": [], "q2": ["0099"]})
    stats = result["overall"]
    assert stats["with_labels"] == 0
    assert all(stats[f"{name}@5"] is None for name in METRIC_NAMES)
    assert stats["false_return_rate"] == 0.5
    assert result["by_intent"]["standard_name"]["hit@5"] is None


def test_local_macro_preserves_raw_precision():
    rows = [
        query("z9", labels=[{"id": "0001", "grade": 3}]),
        query("a1", labels=[{"id": "0002", "grade": 2}]),
        query("m4", labels=[{"id": "0003", "grade": 1}]),
    ]
    result = score_legacy(rows, {"z9": ["0001"], "a1": ["0002"], "m4": ["0099"]})
    stats = result["overall"]
    assert list(result["per_query"]) == ["z9", "a1", "m4"]
    assert stats["recall@5"] == pytest.approx(2 / 3, abs=1e-15)
    assert stats["precision@5"] == pytest.approx(2 / 15, abs=1e-15)
    assert stats["ndcg@5"] == pytest.approx(2 / 3, abs=1e-15)
    assert stats["recall@5"] != round(stats["recall@5"], 5)
    assert stats["precision@5"] != round(stats["precision@5"], 5)
    assert result["per_query"]["z9"]["scores"]["ndcg@5"] == 1.0
    assert result["per_query"]["m4"]["scores"]["ndcg@5"] == 0.0


def test_by_intent_groups_counts_and_macro():
    rows = [
        query("a1", intent="lookup"),
        query("a2", intent="lookup"),
        query("g1", labels=[{"id": "0003", "grade": 1}], intent="generation"),
    ]
    result = score_legacy(rows, {"a1": ["0001", "0002"], "a2": [], "g1": ["0003"]})
    lookup = result["by_intent"]["lookup"]
    assert lookup["queries"] == 2
    assert lookup["with_labels"] == 2
    assert lookup["empty_expectation_queries"] == 0
    assert lookup["recall@5"] == 0.5
    assert lookup["precision@5"] == 0.2
    generation = result["by_intent"]["generation"]
    assert generation["queries"] == 1
    assert generation["recall@5"] == 1.0
    assert generation["precision@5"] == 0.2


def test_mrr_alignment_follows_query_order():
    rankings = {"q1": ["0001"], "q2": ["0099", "0002"]}
    first = score_legacy([query("q1"), query("q2")], rankings)
    assert first["per_query"]["q1"]["scores"]["mrr@5"] == 1.0
    assert first["per_query"]["q2"]["scores"]["mrr@5"] == 0.5
    second = score_legacy([query("q2"), query("q1")], rankings)
    assert second["per_query"]["q1"]["scores"]["mrr@5"] == 1.0
    assert second["per_query"]["q2"]["scores"]["mrr@5"] == 0.5
    assert second["overall"]["mrr@5"] == 0.75


def test_preserves_given_order_and_leading_zero_ids():
    result = score_legacy([query()], {"q1": ["0099", "0002", "0001"]})
    assert result["per_query"]["q1"]["returned_ids"] == ["0099", "0002", "0001"]
    assert result["overall"]["mrr@5"] == 0.5


def staged_input(query_id="q1", labels=None, pool=None, final=None, empty=False):
    return RetrievalInput(
        query_id=query_id,
        expect_empty=empty,
        labels=labels or [],
        candidate_pool=pool or [],
        final_model_input=final or [],
    )


def staged_scores(item):
    result = evaluate_staged([item])[0]
    return result, {metric.name: metric.value for metric in result.metrics}


def test_recall_at_twenty_uses_sixth_and_twentieth_candidate():
    pool = [f"{index:04d}" for index in range(1, 21)]
    item = staged_input(
        labels=[
            RelevanceLabel(exercise_id="0006", grade=2),
            RelevanceLabel(exercise_id="0020", grade=3),
        ],
        pool=pool,
        final=pool[:5],
    )
    result, scores = staged_scores(item)
    assert scores["recall@20"] == 1.0
    assert all(scores[name] == 0 for name in ("hit@5", "mrr@5", "ndcg@5", "precision@5"))
    assert result.candidate_pool == pool
    assert result.final_model_input == pool[:5]
    assert {metric.name: metric.stage for metric in result.metrics}["recall@20"] == "candidate_pool"


def test_candidate_pool_recall_differs_from_final_input_metrics():
    pool = ["0099", "0001", "0002", "0003", "0004", "0005"]
    item = staged_input(
        labels=[RelevanceLabel(exercise_id="0005", grade=3)],
        pool=pool,
        final=pool[:5],
    )
    _, scores = staged_scores(item)
    assert scores["recall@20"] == 1.0
    assert scores["hit@5"] == 0
    assert scores["mrr@5"] == 0


def test_empty_final_input_keeps_candidate_pool_recall():
    item = staged_input(
        labels=[RelevanceLabel(exercise_id="0001", grade=2)],
        pool=["0001", "0002"],
        final=[],
    )
    result, scores = staged_scores(item)
    assert scores["recall@20"] == 1.0
    assert all(scores[name] == 0 for name in ("hit@5", "mrr@5", "ndcg@5", "precision@5"))
    assert result.final_model_input == []


def test_empty_pool_and_input_score_zero_for_labelled_query():
    item = staged_input(
        labels=[RelevanceLabel(exercise_id="0001", grade=2)],
        pool=[],
        final=[],
    )
    _, scores = staged_scores(item)
    assert all(value == 0 for value in scores.values())


def test_no_answer_false_return_only_uses_final_input():
    no_return = staged_input(empty=True, pool=["0001", "0002"], final=[])
    returned = staged_input("q2", empty=True, pool=["0001"], final=["0001"])
    first, second = evaluate_staged([no_return, returned])
    assert first.false_return is False
    assert second.false_return is True


def test_grade_zero_is_negative_and_ndcg_uses_graded_gain():
    item = staged_input(
        labels=[
            RelevanceLabel(exercise_id="0001", grade=0),
            RelevanceLabel(exercise_id="0002", grade=3),
            RelevanceLabel(exercise_id="0003", grade=1),
        ],
        pool=["0001", "0002", "0003"],
        final=["0001", "0002", "0003"],
    )
    _, scores = staged_scores(item)
    assert scores["recall@20"] == 1.0
    assert scores["precision@5"] == pytest.approx(0.4)
    expected = (7 / math.log2(3) + 1 / math.log2(4)) / (7 + 1 / math.log2(3))
    assert scores["ndcg@5"] == pytest.approx(expected, abs=1e-12)
    assert scores["mrr@5"] == 0.5


def test_recall_denominator_uses_all_positive_labels_beyond_pool():
    labels = [RelevanceLabel(exercise_id=f"{index:04d}", grade=3) for index in range(1, 26)]
    pool = [f"{index:04d}" for index in range(1, 21)]
    item = staged_input(labels=labels, pool=pool, final=pool[:5])
    _, scores = staged_scores(item)
    assert scores["recall@20"] == pytest.approx(20 / 25)
    assert scores["precision@5"] == 1.0


@pytest.mark.parametrize("queries,rankings", [
    ([query()], {}),
    ([query()], {"q1": [], "extra": []}),
    ([query(), query()], {"q1": []}),
    ([query()], {"q1": ["0001", "0001"]}),
    ([query()], {"q1": [1]}),
    ([query()], {"q1": "0001"}),
    ([query(labels=[])], {"q1": []}),
    ([query(empty=True)], {"q1": []}),
    ([query(labels=[{"id": "0001", "grade": 4}])], {"q1": []}),
    ([query(labels=[{"id": "0001", "grade": True}])], {"q1": []}),
    ([query(labels=[{"id": "0001", "grade": 1}, {"id": "0001", "grade": 2}])], {"q1": []}),
])
def test_rejects_invalid_evidence(queries, rankings):
    with pytest.raises(ValueError):
        score_legacy(queries, rankings)


@pytest.mark.parametrize("ks", [(), (0,), (-1,), (True,)])
def test_rejects_invalid_cutoff(ks):
    with pytest.raises(ValueError):
        score_legacy([query()], {"q1": []}, ks=ks)


def staged_projection(pool, final, *, scores=None, threshold=0.0, has_more=None):
    catalog = Catalog.load()
    exercises = [catalog.get(eid) for eid in final]
    response = {
        "items": [
            {
                "id": exercise.id,
                "name": exercise.name,
                "equipment": exercise.equipment,
                "target": exercise.target,
                "matches": [],
            }
            for exercise in exercises
        ],
        "has_more": len(pool) > len(final) if has_more is None else has_more,
    }
    evidence = SearchResponseEvidence(
        query_id="q1",
        response_json=response,
        model_visible=ModelVisibleSearch(
            items=[
                VisibleExercise(
                    exercise_id=exercise.id,
                    name=exercise.name,
                    equipment=exercise.equipment,
                    target=exercise.target,
                    matches=[],
                )
                for exercise in exercises
            ],
            has_more=response["has_more"],
        ),
    )
    return {
        "candidate_pool": list(pool),
        "final_model_input": list(final),
        "scores": scores if scores is not None else {eid: 1.0 for eid in pool},
        "response": response,
        "search_evidence": evidence.model_dump(),
        "threshold": threshold,
        "hard_constraint_violations": [],
        "budget_compliant": True,
        "budget_chars": SEARCH_BUDGET,
        "item_limit": SEARCH_LIMIT,
        "serialized_chars": encoded_length(response),
    }


def empty_conditions() -> dict[str, EvaluatedConditions]:
    return {"q1": EvaluatedConditions(SearchConditions(), (), ())}


def query_item(eid: str) -> dict:
    return {
        "id": "q1",
        "tags": ["lookup"],
        "family": "bench",
        "split": "v-test-1",
        "labels": [{"id": eid, "grade": 3}],
        "expect_empty": False,
    }


def test_validate_projections_rejects_out_of_catalog():
    with pytest.raises(ValueError, match="目录外"):
        validate_projections(
            [query()],
            {"q1": {"candidate_pool": ["9999"], "final_model_input": ["9999"]}},
            Catalog.load(),
            empty_conditions(),
            0.0,
        )


def test_validate_projections_rejects_unordered_final_input():
    catalog = Catalog.load()
    first, second = sorted(catalog.exercise_ids)[:2]
    projection = staged_projection([first, second], [second])
    with pytest.raises(ValueError, match="顺序"):
        validate_projections(
            [query_item(first)], {"q1": projection}, catalog, empty_conditions(), 0.0
        )


def test_validate_projections_uses_two_stages():
    catalog = Catalog.load()
    eid = next(iter(sorted(catalog.exercise_ids)))
    metrics = validate_projections(
        [query_item(eid)],
        {"q1": staged_projection([eid], [eid])},
        catalog,
        empty_conditions(),
        0.0,
    )
    assert metrics["engine"]["name"] == "mteb"
    assert metrics["overall"]["recall@20"] == 1.0
    assert metrics["overall"]["hit@5"] == 1.0
    assert metrics["per_query"]["q1"]["metrics"][0]["stage"] == "candidate_pool"


def test_validate_projections_rejects_over_budget_marked_compliant():
    catalog = Catalog.load()
    eid = next(iter(sorted(catalog.exercise_ids)))
    projection = staged_projection([eid], [eid])
    text = "x" * 5000
    projection["response"]["items"][0]["matches"] = [text]
    projection["search_evidence"]["response_json"] = copy.deepcopy(projection["response"])
    projection["search_evidence"]["model_visible"]["items"][0]["matches"] = [text]
    projection["serialized_chars"] = encoded_length(projection["response"])
    projection["budget_compliant"] = True
    with pytest.raises(ValueError, match="预算"):
        validate_projections(
            [query_item(eid)], {"q1": projection}, catalog, empty_conditions(), 0.0
        )


def test_validate_projections_rejects_condition_violation_with_empty_marker():
    catalog = Catalog.load()
    eid = next(iter(sorted(catalog.exercise_ids)))
    projection = staged_projection([eid], [eid])
    projection["hard_constraint_violations"] = []
    evaluated = {
        "q1": EvaluatedConditions(SearchConditions(excluded_ids=(eid,)), (), ())
    }
    with pytest.raises(ValueError, match="条件"):
        validate_projections(
            [query_item(eid)], {"q1": projection}, catalog, evaluated, 0.0
        )


@pytest.mark.parametrize("threshold", [True, float("nan"), float("inf"), "0.0"])
def test_validate_projections_rejects_invalid_threshold(threshold):
    catalog = Catalog.load()
    eid = next(iter(sorted(catalog.exercise_ids)))
    projection = staged_projection([eid], [eid], threshold=threshold)
    with pytest.raises(ValueError, match="门槛"):
        validate_projections(
            [query_item(eid)], {"q1": projection}, catalog, empty_conditions(), threshold
        )


def test_upload_rejects_unknown_schema():
    with pytest.raises(ValueError, match="格式非法"):
        upload_payload({"schema": "unknown"}, "hybrid")


def test_history_rejects_adaptation_drift():
    history = {"conditions_mode": "adapted", "run_identity": {"file_sha256": {
        "dataset/action-retrieval-queries.json": support.sha256(support.QUERIES_PATH),
        "dataset/action-retrieval-purpose-adaptation.json": "0" * 64,
    }}}
    with pytest.raises(ValueError, match="用途适配哈希"):
        validate_history(history)
    history["run_identity"]["file_sha256"]["dataset/action-retrieval-purpose-adaptation.json"] = support.sha256(support.ADAPTATION_PATH)
    validate_history(history)


@pytest.fixture(scope="session")
def evaluation_data(tmp_path_factory):
    return build_dataset(tmp_path_factory.mktemp("evaluation-fixtures"))


@pytest.fixture(scope="session")
def payload_template(evaluation_data):
    return make_document(evaluation_data)


@pytest.fixture
def document_and_query(payload_template):
    document, dataset = payload_template
    qid = next(item["query_id"] for item in dataset.queries if not item["expect_empty"])
    return copy.deepcopy(document), qid, dataset


def upload_row(payload, qid):
    return next(item for item in payload["results"] if item["run_metadata"]["query_id"] == qid)


def test_report_identifies_recall_at_twenty_and_at_five(tmp_path, document_and_query):
    document, qid, _ = document_and_query
    artifact = tmp_path / "run" / "evaluation.json"
    artifact.parent.mkdir()
    report = record_report(document, artifact)
    text = report.read_text(encoding="utf-8")
    for name in ("Recall@20", "Hit@5", "Precision@5", "MRR@5", "nDCG@5"):
        assert name in text
    assert "Recall@5" not in text
    assert "MTEB" in text
    assert "2^grade-1" in text


def test_evidence_schema_identity_and_output_root():
    assert base_document("live", "v-test-1")["schema"] == "fit-agent-retrieval-mteb-2"
    assert base_document("live", "dev", staged=False)["schema"] == "fit-agent-retrieval-mteb-1"
    assert support.OUTPUT_ROOT.name == "retrieval-mteb"
    versions = identity()["versions"]
    assert "mteb" in versions and "ranx" not in versions
    assert "langfuse" in versions and "langsmith" not in versions


def test_score_groups_by_tags():
    item = {"id": "q1", "tags": ["lookup", "standard-name"], "split": "v-test-1", "family": "bench",
            "labels": [{"id": "0001", "grade": 3}], "expect_empty": False}
    result = score_legacy([item], {"q1": ["0001"]})
    assert result["per_query"]["q1"]["intent"] is None
    assert result["per_query"]["q1"]["tags"] == ["lookup", "standard-name"]
    assert set(result["by_tag"]) == {"lookup", "standard-name"}
    assert result["by_intent"] == {}
    assert result["by_tag"]["lookup"]["recall@5"] == 1.0


def test_upload_preserves_whitelisted_inputs(document_and_query):
    document, qid, dataset = document_and_query
    payload = upload_payload(document, "hybrid")
    row = upload_row(payload, qid)
    query = next(item for item in dataset.queries if item["query_id"] == qid)
    assert row["run_metadata"]["query_id"] == qid
    assert row["run_metadata"]["tags"] == query["tags"]
    assert set(row["inputs"]) == {"query", "conditions", "purpose", "tags", "family"}
    projection = document["groups"]["hybrid"]["projections"][qid]
    assert row["actual_outputs"]["candidate_pool"] == projection["candidate_pool"]
    assert row["actual_outputs"]["final_model_input"] == projection["final_model_input"]
    assert row["actual_outputs"]["search_evidence"]["response_json"] == projection["response"]
    assert payload["dataset_name"].startswith(f"fit-agent-retrieval-langfuse-{dataset.version}-")
    assert payload["run_name"] == f"{document['run_id']}-hybrid"
    assert payload["dataset_marker"]["marker"] == "fit-agent-retrieval-upload-langfuse-1"
    assert payload["dataset_marker"]["example_count"] == len(payload["results"])


def test_upload_keeps_full_precision_feedback(document_and_query):
    document, qid, _ = document_and_query
    metrics = document["groups"]["hybrid"]["metrics"]
    original = metrics["per_query"][qid]["scores"]["recall@20"]
    payload = upload_payload(document, "hybrid")
    row = upload_row(payload, qid)
    uploaded = {item["key"]: item["score"] for item in row["evaluation_scores"]}
    summary = {item["key"]: item["score"] for item in payload["summary_experiment_scores"]}
    assert uploaded["recall@20"] == original
    assert summary["recall@20"] == metrics["overall"]["recall@20"]
    assert all(
        isinstance(item["score"], float)
        for item in row["evaluation_scores"]
        if item["key"] not in ("hard_constraint_violation", "budget_compliant")
    )
    assert {item["score"] for item in row["evaluation_scores"] if item["key"] in ("hard_constraint_violation", "budget_compliant")} == {0, 1}
    assert metrics["per_query"][qid]["scores"]["recall@20"] == original
    assert {item["key"] for item in row["evaluation_scores"]} == {
        "recall@20", "hit@5", "mrr@5", "ndcg@5", "precision@5"
    } | {"hard_constraint_violation", "budget_compliant"}


def test_upload_records_replay_scope_and_original_timings(document_and_query):
    document, qid, _ = document_and_query
    timing = {"start_time": "2026-10-17T08:11:55+00:00", "end_time": "2026-10-17T08:11:56+00:00"}
    for item in document["groups"]["hybrid"]["timings"]:
        document["groups"]["hybrid"]["timings"][item] = timing
    payload = upload_payload(document, "hybrid")
    row = upload_row(payload, qid)
    assert row["start_time"] == timing["start_time"]
    assert row["end_time"] == timing["end_time"]
    assert row["run_metadata"]["timing_scope"] == document["timing_scope"]
    assert payload["experiment_metadata"]["evidence_replay"] is True
    assert payload["experiment_metadata"]["timing_scope"] == document["timing_scope"]
    assert payload["experiment_metadata"]["dataset_name"] == payload["dataset_name"]


@pytest.mark.parametrize("field", ["ranking", "labels", "metric", "engine"])
def test_upload_rejects_detached_scores(field, document_and_query):
    document, qid, _ = document_and_query
    result = document["groups"]["hybrid"]
    if field == "ranking":
        result["metrics"]["per_query"][qid]["final_model_input"] = []
    elif field == "labels":
        result["metrics"]["per_query"][qid]["expected_ids"] = {}
    elif field == "metric":
        result["metrics"]["overall"]["recall@20"] = -1
    else:
        result["metrics"]["engine"]["version"] = "0"
    with pytest.raises(ValueError, match="实际候选|评分列表"):
        upload_payload(document, "hybrid")


@pytest.mark.parametrize("invalid", [False, True])
def test_upload_rejects_invalid_candidate_scores(invalid, document_and_query):
    document, qid, _ = document_and_query
    projection = document["groups"]["hybrid"]["projections"][qid]
    eid = projection["candidate_pool"][0]
    projection["scores"] = {eid: float("nan")} if invalid else {}
    with pytest.raises(ValueError, match="分数"):
        upload_payload(document, "hybrid")


def test_upload_rejects_incomplete_query_coverage(document_and_query):
    document, qid, _ = document_and_query
    document["groups"]["hybrid"]["projections"].pop(qid)
    with pytest.raises(ValueError, match="覆盖不完整"):
        upload_payload(document, "hybrid")


def test_stable_row_ids_and_version_isolation(document_and_query):
    document, qid, _ = document_and_query
    first = upload_row(upload_payload(document, "hybrid"), qid)
    second = upload_row(upload_payload(copy.deepcopy(document), "hybrid"), qid)
    assert first["row_id"] == second["row_id"]
    document["dataset"]["content_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="内容摘要"):
        upload_payload(document, "hybrid")


def test_unreviewed_candidate_only_in_pool_blocks_group(document_and_query):
    _, qid, dataset = document_and_query
    reviewed = {
        item["corpus_id"] for item in dataset.qrels if item["query_id"] == qid
    }
    unknown = next(
        row["corpus_id"] for row in dataset.corpus if row["corpus_id"] not in reviewed
    )
    # 未审阅动作只出现在参与 Recall@20 的候选池，不进入最终模型输入。
    projections = {qid: {"candidate_pool": sorted(reviewed) + [unknown]}}
    group = validate_group(dataset, "hybrid", projections, {}, None)
    assert group["status"] == "blocked_unreviewed_candidates"
    assert group["unknown_candidates"] == {qid: [unknown]}


def test_gate_failure_retains_evidence_and_rejects_upload(evaluation_data, tmp_path):
    document, _ = make_document(evaluation_data, unknown=True)
    assert document["status"] == "blocked"
    assert document["groups"]["hybrid"]["projections"]
    assert document["groups"]["hybrid"]["unknown_candidates"]
    artifact = tmp_path / "run" / "evaluation.json"
    artifact.parent.mkdir()
    report = record_report(document, artifact).read_text(encoding="utf-8")
    assert "未审阅候选" in report
    with pytest.raises(ValueError, match="发布门禁"):
        upload_payload(document, "hybrid")


def test_publishable_revalidates_staged_scores_and_response_evidence(document_and_query):
    document, qid, _ = document_and_query
    document["status"] = "running"
    assert publishable(document) is True

    scored = copy.deepcopy(document)
    row = next(
        item
        for item in scored["groups"]["hybrid"]["metrics"]["per_query"].values()
        if not item["expect_empty"]
    )
    key = next(iter(row["scores"]))
    row["scores"][key] = float(row["scores"][key]) + 0.5
    assert publishable(scored) is False

    response = copy.deepcopy(document)
    projection = response["groups"]["hybrid"]["projections"][qid]
    projection["serialized_chars"] = int(projection["serialized_chars"]) + 1
    assert publishable(response) is False

    budget = copy.deepcopy(document)
    projection = budget["groups"]["hybrid"]["projections"][qid]
    projection["budget_compliant"] = False
    assert publishable(budget) is False

    missing = copy.deepcopy(document)
    missing.pop("dataset")
    assert publishable(missing) is False


def test_settle_document_transitions_running_to_complete(document_and_query):
    document, _, _ = document_and_query
    document["status"] = "running"
    document.pop("finished_at", None)
    assert publishable(document) is True
    assert settle_document(document) == "complete"
    assert document["status"] == "complete"
    assert document["finished_at"]
    document["status"] = "running"
    document["groups"]["hybrid"]["metrics"] = None
    assert publishable(document) is False
    assert settle_document(document) == "blocked"


def test_save_result_serializes_blocked_group_without_metrics(evaluation_data, tmp_path, monkeypatch, capsys):
    document, _ = make_document(evaluation_data, unknown=True)
    monkeypatch.setattr(support, "OUTPUT_ROOT", tmp_path / "out")
    artifact = save_result(document)
    saved = json.loads(artifact.read_text(encoding="utf-8"))
    assert saved["status"] == "blocked"
    assert "未审阅候选" in (artifact.parent / "report.md").read_text(encoding="utf-8")
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "blocked"
    assert summary["groups"]["hybrid"]["overall"] is None
    assert summary["groups"]["hybrid"]["unknown_candidates"]


def test_rrf_parameters_survive_report_and_upload(document_and_query, tmp_path):
    from app.application.business.retrieval_frozen import frozen_values

    document, _, _ = document_and_query
    document["parameters"] = frozen_values()
    payload = upload_payload(document, "hybrid")
    assert payload["experiment_metadata"]["parameters"] == document["parameters"]
    artifact = tmp_path / "evaluation.json"
    text = record_report(document, artifact).read_text(encoding="utf-8")
    assert '"fusion": "rrf"' in text and '"rrf_k": 60' in text


def test_rrf_cli_exposes_smoothing_parameter():
    result = subprocess.run(
        [sys.executable, "-m", "evaluation.retrieval", "run", "--help"],
        cwd=support.BACKEND, capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "--rrf-k" in result.stdout
    assert "--alpha" not in result.stdout


def test_cli_requires_explicit_data_source():
    result = subprocess.run(
        [sys.executable, "-m", "evaluation.retrieval", "run"],
        cwd=support.BACKEND,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "--dataset" in result.stderr and "--legacy-split" in result.stderr
