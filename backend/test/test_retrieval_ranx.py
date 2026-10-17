import math

import pytest

from evaluation import retrieval_support as support
from evaluation.retrieval import base_document, now, upload_payload, validate_history
from evaluation.retrieval_metrics import score


def query(qid="q1", labels=None, empty=False):
    return {"id": qid, "intent": "standard_name", "family": "bench", "split": "dev",
            "labels": [{"id": "0001", "grade": 3}, {"id": "0002", "grade": 2}] if labels is None else labels,
            "expect_empty": empty}


def test_textbook_ranking():
    result = score([query()], {"q1": ["0099", "0001", "0002"]}, ks=(1, 2, 5))
    stats = result["overall"]
    assert stats["hit@1"] == 0
    assert stats["hit@2"] == 1
    assert stats["recall@2"] == 0.5
    assert stats["precision@5"] == 0.4
    assert stats["mrr@5"] == 0.5
    expected = (7 / math.log2(3) + 3 / math.log2(4)) / (7 + 3 / math.log2(3))
    assert stats["ndcg@5"] == pytest.approx(expected)
    assert result["engine"]["aliases"]["ndcg@5"] == "ndcg_burges@5"
    assert result["engine"]["aliases"]["hit@5"] == "hit_rate@5"


def test_all_empty_rankings_remain_in_denominator():
    stats = score([query(), query("q2")], {"q1": [], "q2": []})["overall"]
    assert stats["with_labels"] == 2
    assert all(stats[f"{name}@{k}"] == 0 for name in ("hit", "recall", "precision", "mrr", "ndcg") for k in (5, 10))


def test_macro_average_includes_missed_queries():
    result = score([query(), query("q2")], {"q1": ["0001", "0002"], "q2": []})
    assert result["overall"]["recall@5"] == 0.5
    assert result["overall"]["mrr@5"] == 0.5
    assert result["per_query"]["q2"]["scores"]["recall@5"] == 0


def test_no_answer_is_separate():
    rows = [query(), query("empty1", [], True), query("empty2", [], True)]
    result = score(rows, {"q1": ["0001"], "empty1": ["0099"], "empty2": []})
    stats = result["overall"]
    assert stats["with_labels"] == 1
    assert stats["empty_expectation_queries"] == 2
    assert stats["false_return_rate"] == 0.5
    assert result["per_query"]["empty1"]["scores"] == {"false_return": 1.0}
    assert "recall@5" not in result["per_query"]["empty2"]["scores"]


def test_only_no_answer_queries():
    stats = score([query(labels=[], empty=True)], {"q1": []})["overall"]
    assert stats["hit@5"] is None
    assert stats["false_return_rate"] == 0


def test_more_relevant_documents_than_k():
    item = query(labels=[{"id": f"{i:04}", "grade": 3} for i in range(1, 21)])
    stats = score([item], {"q1": [f"{i:04}" for i in range(1, 6)]})["overall"]
    assert stats["recall@5"] == 0.25
    assert stats["ndcg@5"] == pytest.approx(1)


def test_preserves_given_order_and_leading_zero_ids():
    result = score([query()], {"q1": ["0099", "0002", "0001"]})
    assert result["per_query"]["q1"]["returned_ids"] == ["0099", "0002", "0001"]
    assert result["overall"]["mrr@5"] == 0.5


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
        score(queries, rankings)


@pytest.mark.parametrize("ks", [(), (0,), (-1,), (True,)])
def test_rejects_invalid_cutoff(ks):
    with pytest.raises(ValueError):
        score([query()], {"q1": []}, ks=ks)


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


def payload_example():
    item = support.load_queries("dev")[0]
    qid, eid = item["id"], item["labels"][0]["id"]
    document = base_document("historical_rescore", "dev")
    document.update(query_ids=[qid], finished_at=now(), index_identity={}, timing_scope="unit_test",
                    observation_scope="unit_test")
    document["groups"]["hybrid"] = {
        "metrics": score([item], {qid: [eid]}),
        "projections": {qid: {"returned": [eid], "scores": {eid: 1.0},
                              "hard_constraint_violations": [], "budget_compliant": True}},
        "timings": {qid: {"start_time": document["started_at"], "end_time": document["finished_at"]}},
    }
    return document, qid


def test_upload_preserves_whitelisted_inputs():
    document, qid = payload_example()
    payload = upload_payload(document, "hybrid")
    row = payload["results"][0]
    assert row["run_metadata"]["query_id"] == qid
    assert set(row["inputs"]) == {"query", "filters", "environment", "declared_equipment",
                                  "saved_profile_restrictions", "purpose", "temporary_mapping"}
    assert row["actual_outputs"]["returned_ids"] == document["groups"]["hybrid"]["projections"][qid]["returned"]


def test_upload_rounds_feedback_without_changing_local_scores():
    document, qid = payload_example()
    metrics = document["groups"]["hybrid"]["metrics"]
    original = metrics["per_query"][qid]["scores"]["recall@5"]
    payload = upload_payload(document, "hybrid")
    uploaded = {item["key"]: item["score"] for item in payload["results"][0]["evaluation_scores"]}
    summary = {item["key"]: item["score"] for item in payload["summary_experiment_scores"]}
    assert uploaded["recall@5"] == round(original, 4)
    assert uploaded["recall@5"] != original
    assert summary["recall@5"] == round(metrics["overall"]["recall@5"], 4)
    assert all(item["score"] == round(item["score"], 4) for item in payload["results"][0]["evaluation_scores"])
    assert all(item["score"] == round(item["score"], 4) for item in payload["summary_experiment_scores"])
    assert metrics["per_query"][qid]["scores"]["recall@5"] == original


def test_upload_uses_actual_group_time_bounds():
    document, qid = payload_example()
    document["started_at"] = "2026-10-17T08:11:50+00:00"
    document["finished_at"] = "2026-10-17T08:11:57+00:00"
    timing = {"start_time": "2026-10-17T08:11:55+00:00", "end_time": "2026-10-17T08:11:56+00:00"}
    document["groups"]["hybrid"]["timings"][qid] = timing
    payload = upload_payload(document, "hybrid")
    assert payload["experiment_start_time"] == timing["start_time"]
    assert payload["experiment_end_time"] == timing["end_time"]
    assert payload["results"][0]["start_time"] == timing["start_time"]
    assert payload["results"][0]["end_time"] == timing["end_time"]


@pytest.mark.parametrize("field", ["ranking", "labels", "metric"])
def test_upload_rejects_detached_scores(field):
    document, qid = payload_example()
    result = document["groups"]["hybrid"]
    if field == "ranking":
        result["metrics"]["per_query"][qid]["returned_ids"] = []
    elif field == "labels":
        result["metrics"]["per_query"][qid]["expected_ids"] = {}
    else:
        result["metrics"]["overall"]["recall@5"] = -1
    with pytest.raises(ValueError, match="实际候选或金标"):
        upload_payload(document, "hybrid")


@pytest.mark.parametrize("scores", [{}, {"0001": float("nan")}])
def test_upload_rejects_invalid_candidate_scores(scores):
    document, qid = payload_example()
    eid = document["groups"]["hybrid"]["projections"][qid]["returned"][0]
    document["groups"]["hybrid"]["projections"][qid]["scores"] = {} if not scores else {eid: float("nan")}
    with pytest.raises(ValueError, match="分数"):
        upload_payload(document, "hybrid")
