import copy
import json
import math
import subprocess
import sys
from dataclasses import replace

import pytest

from evaluation import retrieval_support as support
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
from evaluation.retrieval_metrics import DEFAULT_KS, score
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
    result = score([query()], {"q1": ["0099", "0001", "0002"]}, ks=(1, 2, 5))
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
    result = score([query()], {"q1": ["0001"]})
    aliases = {f"{name}@5" for name in METRIC_NAMES}
    assert DEFAULT_KS == (5,)
    assert set(result["engine"]["aliases"]) == aliases
    assert set(result["overall"]) == aliases | {"queries", "with_labels", "empty_expectation_queries", "false_return_rate"}
    assert set(result["per_query"]["q1"]["scores"]) == aliases


def test_grade_one_counts_as_relevant():
    item = query(labels=[{"id": "0001", "grade": 1}])
    stats = score([item], {"q1": ["0001"]})["overall"]
    assert stats["hit@5"] == 1.0
    assert stats["recall@5"] == 1.0
    assert stats["precision@5"] == 0.2
    assert stats["mrr@5"] == 1.0
    assert stats["ndcg@5"] == pytest.approx(1.0)


def test_graded_gain_mapping():
    item = query(labels=[{"id": "0001", "grade": 3}, {"id": "0002", "grade": 1}])
    result = score([item], {"q1": ["0002", "0001"]})
    assert result["per_query"]["q1"]["expected_ids"] == {"0001": 3, "0002": 1}
    assert result["engine"]["gain_mapping"] == {"1": 1, "2": 3, "3": 7}
    contract = (1 + 7 / math.log2(3)) / (7 + 1 / math.log2(3))
    linear = (1 + 3 / math.log2(3)) / (3 + 1 / math.log2(3))
    assert result["overall"]["ndcg@5"] == pytest.approx(contract, abs=1e-12)
    assert result["overall"]["ndcg@5"] != pytest.approx(linear, abs=1e-12)
    assert result["overall"]["mrr@5"] == 1.0


def test_short_return_uses_five_as_precision_denominator():
    stats = score([query()], {"q1": ["0001"]})["overall"]
    assert stats["hit@5"] == 1.0
    assert stats["recall@5"] == 0.5
    assert stats["precision@5"] == 0.2
    assert stats["mrr@5"] == 1.0
    assert stats["ndcg@5"] == pytest.approx(7 / (7 + 3 / math.log2(3)), abs=1e-12)


def test_first_relevant_at_rank_five():
    item = query(labels=[{"id": "0001", "grade": 3}])
    stats = score([item], {"q1": ["0101", "0102", "0103", "0104", "0001"]})["overall"]
    assert stats["hit@5"] == 1.0
    assert stats["recall@5"] == 1.0
    assert stats["precision@5"] == 0.2
    assert stats["mrr@5"] == 0.2
    assert stats["ndcg@5"] == pytest.approx(1 / math.log2(6), abs=1e-12)


def test_first_relevant_at_rank_six_scores_zero_at_five():
    item = query(labels=[{"id": "0001", "grade": 3}])
    result = score([item], {"q1": ["0101", "0102", "0103", "0104", "0105", "0001"]})
    for name in METRIC_NAMES:
        assert result["overall"][f"{name}@5"] == 0


def test_full_return_retained_beyond_scoring_truncation():
    item = query(labels=[{"id": "0001", "grade": 3}])
    ids = ["0101", "0102", "0103", "0104", "0105", "0001", "0106"]
    result = score([item], {"q1": ids})
    assert result["per_query"]["q1"]["returned_ids"] == ids
    assert all(result["per_query"]["q1"]["scores"][f"{name}@5"] == 0 for name in METRIC_NAMES)


def test_more_relevant_actions_than_five():
    item = query(labels=[{"id": f"{index:04}", "grade": 3} for index in range(1, 21)])
    stats = score([item], {"q1": [f"{index:04}" for index in range(1, 6)]})["overall"]
    assert stats["hit@5"] == 1.0
    assert stats["recall@5"] == 0.25
    assert stats["precision@5"] == 1.0
    assert stats["ndcg@5"] == pytest.approx(1.0)


def test_labelled_empty_return_scores_zero_and_enters_macro():
    result = score([query(), query("q2")], {"q1": [], "q2": []})
    stats = result["overall"]
    assert stats["with_labels"] == 2
    assert all(stats[f"{name}@5"] == 0 for name in METRIC_NAMES)
    assert result["per_query"]["q1"]["expected_ids"] == {"0001": 3, "0002": 2}
    assert result["per_query"]["q1"]["returned_ids"] == []
    assert result["per_query"]["q1"]["scores"]["recall@5"] == 0


def test_macro_average_includes_missed_queries():
    result = score([query(), query("q2")], {"q1": ["0001", "0002"], "q2": []})
    assert result["overall"]["recall@5"] == 0.5
    assert result["overall"]["mrr@5"] == 0.5
    assert result["per_query"]["q2"]["scores"]["recall@5"] == 0


def test_no_answer_queries_only_record_false_return():
    rows = [query(), query("empty1", [], True), query("empty2", [], True)]
    result = score(rows, {"q1": ["0001"], "empty1": ["0099"], "empty2": []})
    stats = result["overall"]
    assert stats["with_labels"] == 1
    assert stats["empty_expectation_queries"] == 2
    assert stats["false_return_rate"] == 0.5
    assert result["per_query"]["empty1"]["scores"] == {"false_return": 1.0}
    assert result["per_query"]["empty2"]["scores"] == {"false_return": 0.0}
    assert "recall@5" not in result["per_query"]["empty2"]["scores"]


def test_only_no_answer_queries_skips_ranking():
    rows = [query(labels=[], empty=True), query("q2", labels=[], empty=True)]
    result = score(rows, {"q1": [], "q2": ["0099"]})
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
    result = score(rows, {"z9": ["0001"], "a1": ["0002"], "m4": ["0099"]})
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
    result = score(rows, {"a1": ["0001", "0002"], "a2": [], "g1": ["0003"]})
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
    first = score([query("q1"), query("q2")], rankings)
    assert first["per_query"]["q1"]["scores"]["mrr@5"] == 1.0
    assert first["per_query"]["q2"]["scores"]["mrr@5"] == 0.5
    second = score([query("q2"), query("q1")], rankings)
    assert second["per_query"]["q1"]["scores"]["mrr@5"] == 1.0
    assert second["per_query"]["q2"]["scores"]["mrr@5"] == 0.5
    assert second["overall"]["mrr@5"] == 0.75


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


class StubCatalog:
    exercise_ids = {"0001", "0002"}


def test_validate_projections_rejects_out_of_catalog():
    with pytest.raises(ValueError, match="目录外"):
        validate_projections(
            [query()],
            {"q1": {"returned": ["9999"], "budget_compliant": True, "hard_constraint_violations": []}},
            StubCatalog(),
        )


def test_validate_projections_uses_mteb():
    metrics = validate_projections(
        [query()],
        {"q1": {"returned": ["0001"], "budget_compliant": True, "hard_constraint_violations": []}},
        StubCatalog(),
    )
    assert metrics["engine"]["name"] == "mteb"
    assert metrics["overall"]["recall@5"] == 0.5


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


def test_default_report_contains_only_at_five(tmp_path, document_and_query):
    document, qid, _ = document_and_query
    artifact = tmp_path / "run" / "evaluation.json"
    artifact.parent.mkdir()
    report = record_report(document, artifact)
    text = report.read_text(encoding="utf-8")
    for name in ("Hit", "Recall", "Precision", "MRR", "nDCG"):
        assert f"{name}@5" in text
    assert "@10" not in text
    assert "MTEB" in text
    assert "2^grade-1" in text


def test_evidence_schema_identity_and_output_root():
    assert base_document("live", "v-test-1")["schema"] == "fit-agent-retrieval-mteb-1"
    assert support.OUTPUT_ROOT.name == "retrieval-mteb"
    versions = identity()["versions"]
    assert "mteb" in versions and "ranx" not in versions


def test_score_groups_by_tags():
    item = {"id": "q1", "tags": ["lookup", "standard-name"], "split": "v-test-1", "family": "bench",
            "labels": [{"id": "0001", "grade": 3}], "expect_empty": False}
    result = score([item], {"q1": ["0001"]})
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
    assert row["actual_outputs"]["returned_ids"] == document["groups"]["hybrid"]["projections"][qid]["returned"]
    assert payload["dataset_name"].startswith(f"fit-agent-retrieval-{dataset.version}-")


def test_upload_rounds_feedback_without_changing_local_scores(document_and_query):
    document, qid, _ = document_and_query
    metrics = document["groups"]["hybrid"]["metrics"]
    original = metrics["per_query"][qid]["scores"]["recall@5"]
    payload = upload_payload(document, "hybrid")
    row = upload_row(payload, qid)
    uploaded = {item["key"]: item["score"] for item in row["evaluation_scores"]}
    summary = {item["key"]: item["score"] for item in payload["summary_experiment_scores"]}
    assert uploaded["recall@5"] == round(original, 4)
    assert uploaded["recall@5"] != original
    assert summary["recall@5"] == round(metrics["overall"]["recall@5"], 4)
    assert all(item["score"] == round(item["score"], 4) for item in row["evaluation_scores"])
    assert all(item["score"] == round(item["score"], 4) for item in payload["summary_experiment_scores"])
    assert metrics["per_query"][qid]["scores"]["recall@5"] == original
    assert {item["key"] for item in row["evaluation_scores"]} == {
        f"{name}@5" for name in METRIC_NAMES
    } | {"hard_constraint_violation", "budget_compliant"}


def test_upload_uses_actual_group_time_bounds(document_and_query):
    document, qid, _ = document_and_query
    document["started_at"] = "2026-10-17T08:11:50+00:00"
    document["finished_at"] = "2026-10-17T08:11:57+00:00"
    timing = {"start_time": "2026-10-17T08:11:55+00:00", "end_time": "2026-10-17T08:11:56+00:00"}
    for item in document["groups"]["hybrid"]["timings"]:
        document["groups"]["hybrid"]["timings"][item] = timing
    payload = upload_payload(document, "hybrid")
    row = upload_row(payload, qid)
    assert payload["experiment_start_time"] == timing["start_time"]
    assert payload["experiment_end_time"] == timing["end_time"]
    assert row["start_time"] == timing["start_time"]
    assert row["end_time"] == timing["end_time"]


@pytest.mark.parametrize("field", ["ranking", "labels", "metric", "engine"])
def test_upload_rejects_detached_scores(field, document_and_query):
    document, qid, _ = document_and_query
    result = document["groups"]["hybrid"]
    if field == "ranking":
        result["metrics"]["per_query"][qid]["returned_ids"] = []
    elif field == "labels":
        result["metrics"]["per_query"][qid]["expected_ids"] = {}
    elif field == "metric":
        result["metrics"]["overall"]["recall@5"] = -1
    else:
        result["metrics"]["engine"]["version"] = "0"
    with pytest.raises(ValueError, match="实际候选"):
        upload_payload(document, "hybrid")


@pytest.mark.parametrize("scores", [{}, {"0001": float("nan")}])
def test_upload_rejects_invalid_candidate_scores(scores, document_and_query):
    document, qid, _ = document_and_query
    eid = document["groups"]["hybrid"]["projections"][qid]["returned"][0]
    document["groups"]["hybrid"]["projections"][qid]["scores"] = {} if not scores else {eid: float("nan")}
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


def test_sixth_unreviewed_candidate_blocks_group(evaluation_data, document_and_query):
    _, qid, dataset = document_and_query
    existing = [item for item in dataset.qrels if item["query_id"] == qid]
    reviewed = [item["corpus_id"] for item in existing]
    extra = [row["corpus_id"] for row in dataset.corpus if row["corpus_id"] not in reviewed][: 5 - len(reviewed)]
    template = existing[0]
    extended = tuple({**template, "corpus_id": corpus_id, "grade": 0} for corpus_id in extra)
    expanded = replace(dataset, qrels=dataset.qrels + extended)
    reviewed = reviewed + extra
    unknown = next(row["corpus_id"] for row in dataset.corpus if row["corpus_id"] not in reviewed)
    returned = reviewed + [unknown]
    assert returned.index(unknown) == 5
    projections = {qid: {"returned": returned, "scores": {corpus_id: 1.0 for corpus_id in returned},
                         "hard_constraint_violations": [], "budget_compliant": True}}
    group = validate_group(expanded, "hybrid", projections, {}, None)
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


def test_cli_requires_explicit_data_source():
    result = subprocess.run(
        [sys.executable, "-m", "evaluation.retrieval", "run"],
        cwd=support.BACKEND,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "--dataset" in result.stderr and "--legacy-split" in result.stderr
