import json
import shutil
from argparse import Namespace
from pathlib import Path

import pytest

from app.application.business import retrieval_text as rt
from app.application.business.catalog import Catalog
from app.application.business.retrieval_frozen import frozen_values
from app.application.business.retrieval_rerank import (
    RerankIdentity,
    RerankResult,
    RerankScored,
    RerankUsage,
)
from app.application.business.retrieval_search import RetrievalCandidate
from evaluation import retrieval_support as support
from evaluation.dataset import (
    DatasetError,
    load_for_evaluation,
    qrel_digest,
    query_digest,
    release_digest,
)
from evaluation.retrieval import (
    RERANK_GROUP,
    base_document,
    publishable,
    replay_document,
    settle_document,
    upload_payload,
    validate_group,
    validate_projections,
)
from evaluation.retrieval_rescore import SCHEMA_RESCORE, rescore_staged
from test.evaluation_fixtures import (
    REVIEWED_AT,
    approve,
    build_dataset,
    draft_review,
)

IDENTITY = RerankIdentity(
    "qwen3.7-text-rerank",
    "https://example.invalid/rerank",
    rt.ENCODING_RULE_VERSION,
)


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
        newline="\n",
    )


def _evidence(corpus_id: str) -> dict:
    actions_path = support.BACKEND / "dataset/actions.agent.json"
    actions = json.loads(actions_path.read_text(encoding="utf-8"))
    position = next(
        index for index, row in enumerate(actions) if row["id"] == corpus_id
    )
    return {
        "source_path": "backend/dataset/actions.agent.json",
        "json_pointer": f"/{position}",
        "excerpt": actions[position]["name"],
    }


def derive_dataset(
    source_dir: Path,
    root: Path,
    name: str,
    *,
    add_qrels: list[dict] | None = None,
    mutate_query=None,
    mutate_qrel=None,
    mutate_corpus=None,
    approve_new: bool = True,
    approve_release: bool = True,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    target = root / name
    shutil.copytree(source_dir, target)
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    queries = _read_jsonl(target / "queries.jsonl")
    qrels = _read_jsonl(target / "qrels.jsonl")
    corpus = _read_jsonl(target / "corpus.jsonl")
    if mutate_corpus is not None:
        mutate_corpus(corpus)
    if mutate_query is not None:
        mutate_query(queries)
    if mutate_qrel is not None:
        mutate_qrel(qrels)
    for spec in add_qrels or []:
        row = {
            "query_id": spec["query_id"],
            "corpus_id": spec["corpus_id"],
            "grade": spec["grade"],
            "rationale": spec.get("rationale", "测试夹具补标"),
            "evidence": _evidence(spec["corpus_id"]),
            "review": draft_review(),
        }
        if approve_new:
            approve(
                row["review"],
                qrel_digest(row),
                f"test-added-{spec['query_id']}-{spec['corpus_id']}",
            )
        qrels.append(row)
    for qrel in qrels:
        if qrel["review"]["status"] == "approved":
            qrel["review"]["content_sha256"] = qrel_digest(qrel)
    grouped = {}
    for qrel in qrels:
        grouped.setdefault(qrel["query_id"], []).append(qrel)
    for query in queries:
        if query["review"]["status"] == "approved":
            query["review"]["content_sha256"] = query_digest(
                query, grouped.get(query["query_id"], [])
            )
    _write_jsonl(target / "queries.jsonl", queries)
    _write_jsonl(target / "qrels.jsonl", qrels)
    _write_jsonl(target / "corpus.jsonl", corpus)
    manifest["dataset_version"] = name
    for name_key in ("queries", "qrels", "corpus"):
        manifest["files"][name_key]["sha256"] = support.sha256(
            target / f"{name_key}.jsonl"
        )
    manifest["counts"]["qrels"] = len(qrels)
    manifest["release"]["content_sha256"] = release_digest(manifest)
    if approve_release:
        manifest["release"].update(
            {
                "status": "approved",
                "reviewer": "用户",
                "reviewer_type": "user",
                "reviewed_at": REVIEWED_AT,
                "comment": "测试夹具发布审批结构",
                "decision_ref": "test-rescore-release",
                "content_sha256": release_digest(manifest),
            }
        )
    else:
        manifest["release"].update(
            {
                "status": "draft",
                "reviewer": None,
                "reviewer_type": None,
                "reviewed_at": None,
                "comment": None,
                "decision_ref": None,
                "content_sha256": None,
            }
        )
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return target


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
    rows: list[tuple[str, float]],
    *,
    threshold: float | None,
    rerank_order: list[str] | None = None,
):
    catalog = Catalog.load()
    mappings = rt.load_mappings()
    evaluated = support.dataset_conditions(query, catalog, mappings)
    recalled = [candidate(catalog, corpus_id, score) for corpus_id, score in rows]
    if rerank_order is None:
        return support.project_response(
            catalog,
            query["query_id"],
            recalled,
            evaluated,
            threshold,
            experiment=support.EXPERIMENT_BASELINE,
        )
    if not recalled:
        result = RerankResult((), RerankUsage(), None)
    else:
        position = {corpus_id: index for index, corpus_id in enumerate(rerank_order)}
        scored = [
            RerankScored(
                row.id,
                (len(recalled) - position[row.id]) / (len(recalled) + 1),
                index,
            )
            for index, row in enumerate(recalled)
        ]
        scored.sort(key=lambda row: (-row.rerank_score, row.rrf_rank))
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


def source_document(
    dataset,
    selected_queries: list[dict],
    projections_by_group: dict[str, dict],
) -> dict:
    catalog = Catalog.load()
    mappings = rt.load_mappings()
    timings = {
        query["query_id"]: {
            "start_time": "2026-10-18T09:00:00+00:00",
            "end_time": "2026-10-18T09:00:01+00:00",
        }
        for query in selected_queries
    }
    document = base_document("live", dataset.version)
    document.update(
        dataset=dataset.identity(),
        parameters=frozen_values(),
        query_ids=[query["query_id"] for query in selected_queries],
        index_identity={},
        timing_scope="unit_test",
        observation_scope="unit_test",
    )
    for group, projections in projections_by_group.items():
        threshold = 0.0 if group in ("hybrid", RERANK_GROUP) else None
        document["groups"][group] = validate_group(
            dataset,
            group,
            projections,
            timings,
            catalog,
            mappings,
            threshold,
            selected_queries,
        )
    if RERANK_GROUP in projections_by_group:
        document["experiments"] = {
            "hybrid": support.EXPERIMENT_BASELINE,
            RERANK_GROUP: support.EXPERIMENT_RERANK,
        }
        document["rerank_identity"] = {
            "model": IDENTITY.model,
            "endpoint": IDENTITY.endpoint,
            "text_version": IDENTITY.text_version,
        }
    settle_document(document)
    return document


def save_source(root: Path, document: dict, name: str = "run") -> Path:
    run_dir = root / name
    support.save_document(run_dir / "evaluation.json", document)
    return run_dir / "evaluation.json"


def run_rescore(source: Path, dataset: Path, capsys, output: Path | None = None) -> dict:
    rescore_staged(Namespace(source=source, dataset=dataset, output=output))
    return json.loads(capsys.readouterr().out)


def selected_queries(dataset) -> tuple[dict, dict, dict, list[dict]]:
    positives = [query for query in dataset.queries if not query["expect_empty"]]
    empties = [query for query in dataset.queries if query["expect_empty"]]
    partial, full = positives[0], positives[1]
    empty = empties[0]
    ordered = sorted(
        [partial, full, empty], key=lambda query: dataset.queries.index(query)
    )
    return partial, full, empty, ordered


@pytest.fixture(scope="session")
def evaluation_data(tmp_path_factory):
    return build_dataset(tmp_path_factory.mktemp("rescore-fixtures"))


@pytest.fixture(scope="session")
def dataset(evaluation_data):
    return load_for_evaluation(evaluation_data)


@pytest.fixture(scope="session")
def six_reviewed_path(evaluation_data, tmp_path_factory):
    base = load_for_evaluation(evaluation_data)
    partial = next(
        query for query in base.queries if not query["expect_empty"]
    )
    reviewed = base.reviewed_corpus_ids()[partial["query_id"]]
    extras = [
        corpus_id
        for corpus_id in sorted(Catalog.load().exercise_ids)
        if corpus_id not in reviewed
    ][:3]
    return derive_dataset(
        evaluation_data,
        tmp_path_factory.mktemp("rescore-derived"),
        "v-test-2",
        add_qrels=[
            {"query_id": partial["query_id"], "corpus_id": corpus_id, "grade": 0}
            for corpus_id in extras
        ],
    )


@pytest.fixture(scope="session")
def six_reviewed(six_reviewed_path):
    return load_for_evaluation(six_reviewed_path)


@pytest.fixture(scope="session")
def unknown(six_reviewed):
    partial = selected_queries(six_reviewed)[0]
    reviewed = six_reviewed.reviewed_corpus_ids()[partial["query_id"]]
    return next(
        corpus_id
        for corpus_id in sorted(Catalog.load().exercise_ids)
        if corpus_id not in reviewed
    )


def test_partial_diagnostics_preserve_source_and_generate_pending_draft(
    tmp_path, six_reviewed, six_reviewed_path, unknown, capsys
):
    partial, full, empty, ordered = selected_queries(six_reviewed)
    reviewed = six_reviewed.reviewed_corpus_ids()
    partial_ids = sorted(reviewed[partial["query_id"]])
    assert len(partial_ids) >= 6
    partial_rows = [(corpus_id, 10.0 - index) for index, corpus_id in enumerate(partial_ids[:6])]
    partial_rows.append((unknown, 0.0))
    full_ids = sorted(reviewed[full["query_id"]])
    full_rows = [(corpus_id, 5.0 - index) for index, corpus_id in enumerate(full_ids)]
    projections = {
        partial["query_id"]: projection(partial, partial_rows, threshold=0.0),
        full["query_id"]: projection(full, full_rows, threshold=0.0),
        empty["query_id"]: projection(empty, [], threshold=0.0),
    }
    document = source_document(six_reviewed, ordered, {"hybrid": projections})
    assert document["status"] == "blocked"
    source = save_source(tmp_path, document)
    report_path = source.parent / "report.md"
    original_report = "# 原始报告\r\n\r\n原始内容保留。\r\n".encode("utf-8")
    report_path.write_bytes(original_report)
    source_sha = support.sha256(source)

    result = run_rescore(source, six_reviewed_path, capsys)
    assert result["source_sha256_unchanged"] is True
    assert support.sha256(source) == source_sha
    rescore = json.loads((source.parent / "followup" / result["rescore_id"] / "rescore.json").read_text(encoding="utf-8"))
    assert rescore["schema"] == SCHEMA_RESCORE
    assert rescore["source"]["status"] == "blocked"
    assert rescore["source"]["sha256"] == source_sha
    group = rescore["groups"]["hybrid"]
    assert group["eligible_count"] == 2
    assert group["excluded_query_ids"] == [partial["query_id"]]
    assert group["unreviewed_candidates"] == {partial["query_id"]: [unknown]}
    assert group["metric_denominators"] == {
        "ranking_with_labels": 1,
        "expect_empty_false_return": 1,
    }
    assert group["metrics"]["overall"]["with_labels"] == 1
    assert group["metrics"]["overall"]["empty_expectation_queries"] == 1
    assert group["metrics"]["per_query"][full["query_id"]]["scores"]["recall@20"] == 1.0
    assert rescore["formal_metrics"]["status"] == "blocked_unreviewed_candidates"
    assert rescore["formal_metrics"]["groups"]["hybrid"]["metrics"] is None
    assert rescore["new_requests"]["embedding"] == 0
    assert rescore["source_history_usage"]["historical_only"] is True

    draft_path = source.parent / "followup" / result["rescore_id"] / "review-draft.jsonl"
    drafts = _read_jsonl(draft_path)
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft["query"]["query_id"] == partial["query_id"]
    assert draft["exercise"]["corpus_id"] == unknown
    assert draft["appearances"]["hybrid"]["candidate_pool_rank"] == 7
    assert draft["appearances"]["hybrid"]["final_input_rank"] is None
    assert draft["suggested_grade"] is None
    assert draft["review_status"] == "pending_user_review"
    assert draft["review"]["reviewer_type"] is None
    assert draft["exercise"]["catalog_ref"]["json_pointer"].startswith("/")
    assert draft["suggestion_basis"]["condition_match"] is True

    summary = json.loads(
        (source.parent / "followup" / result["rescore_id"] / "review-summary.json").read_text(encoding="utf-8")
    )
    assert summary["total_unique_pairs"] == 1
    assert summary["suggested_grade_counts"]["null"] == 1
    assert summary["draft_sha256"] == support.sha256_text(draft_path.read_text(encoding="utf-8"))

    updated_report = report_path.read_bytes()
    assert updated_report.startswith(original_report)
    assert updated_report.count(b"retrieval-rescore-followup:begin") == 1
    assert updated_report.count(b"retrieval-rescore-followup:end") == 1
    assert b"\r\n" in updated_report

    with pytest.raises(ValueError, match="格式非法"):
        upload_payload(rescore, "hybrid")
    with pytest.raises(ValueError, match="格式非法"):
        replay_document(rescore)
    assert publishable(rescore) is False

    rescore_path = source.parent / "followup" / result["rescore_id"] / "rescore.json"
    rescore_bytes = rescore_path.read_bytes()
    second = run_rescore(source, six_reviewed_path, capsys)
    assert second["rescore_id"] == result["rescore_id"]
    assert support.sha256(source) == source_sha
    assert rescore_path.read_bytes() == rescore_bytes
    assert report_path.read_bytes() == updated_report


def test_approved_added_grade_zero_enables_full_subset_rescore(
    tmp_path, six_reviewed, six_reviewed_path, unknown, capsys
):
    partial, full, empty, ordered = selected_queries(six_reviewed)
    reviewed = six_reviewed.reviewed_corpus_ids()
    partial_ids = sorted(reviewed[partial["query_id"]])
    partial_rows = [(corpus_id, 10.0 - index) for index, corpus_id in enumerate(partial_ids[:6])]
    partial_rows.append((unknown, 0.0))
    full_rows = [(corpus_id, 5.0 - index) for index, corpus_id in enumerate(sorted(reviewed[full["query_id"]]))]
    projections = {
        partial["query_id"]: projection(partial, partial_rows, threshold=0.0),
        full["query_id"]: projection(full, full_rows, threshold=0.0),
        empty["query_id"]: projection(empty, [], threshold=0.0),
    }
    document = source_document(six_reviewed, ordered, {"hybrid": projections})
    source = save_source(tmp_path, document)
    derived_path = derive_dataset(
        six_reviewed_path,
        tmp_path / "derived",
        "v-test-3",
        add_qrels=[{"query_id": partial["query_id"], "corpus_id": unknown, "grade": 0}],
    )
    derived = load_for_evaluation(derived_path)
    result = run_rescore(source, derived_path, capsys)
    rescore = json.loads(
        (source.parent / "followup" / result["rescore_id"] / "rescore.json").read_text(encoding="utf-8")
    )
    group = rescore["groups"]["hybrid"]
    assert group["eligible_count"] == 3
    assert group["excluded_query_ids"] == []
    assert group["fully_reviewed"] is True
    assert rescore["formal_metrics"]["status"] == "complete_reviewed_full_coverage"
    assert rescore["formal_metrics"]["publishable"] is False
    assert rescore["verification"]["added_label_count"] == 1
    assert rescore["verification"]["added_labels_in_source_pools"] == 1
    assert rescore["verification"]["existing_labels_unchanged"] is True
    assert rescore["source"]["dataset"]["version"] == "v-test-2"
    assert rescore["scoring_dataset"]["version"] == "v-test-3"

    catalog = Catalog.load()
    mappings = rt.load_mappings()
    scoring_map = {item["id"]: item for item in derived.scoring_queries()}
    selected = [scoring_map[query["query_id"]] for query in ordered]
    evaluated = {
        query["query_id"]: support.dataset_conditions(query, catalog, mappings)
        for query in derived.queries
        if query["query_id"] in {item["query_id"] for item in ordered}
    }
    independent = validate_projections(
        selected,
        {query["query_id"]: projections[query["query_id"]] for query in ordered},
        catalog,
        evaluated,
        0.0,
    )
    assert independent == group["metrics"]


def test_all_unknown_yields_null_metrics_and_no_zero_fill(
    tmp_path, six_reviewed, six_reviewed_path, unknown, capsys
):
    partial = selected_queries(six_reviewed)[0]
    document = source_document(
        six_reviewed,
        [partial],
        {
            "hybrid": {
                partial["query_id"]: projection(
                    partial, [(unknown, 1.0)], threshold=0.0
                )
            }
        },
    )
    assert document["status"] == "blocked"
    source = save_source(tmp_path, document)
    result = run_rescore(source, six_reviewed_path, capsys)
    rescore = json.loads(
        (source.parent / "followup" / result["rescore_id"] / "rescore.json").read_text(encoding="utf-8")
    )
    group = rescore["groups"]["hybrid"]
    assert group["eligible_count"] == 0
    assert group["status"] == "diagnostic_unavailable"
    assert group["metrics"] is None
    assert group["eligible_coverage"] == 0
    assert group["exclusion_reasons"] == {
        partial["query_id"]: {
            "reason": "unreviewed_candidates",
            "missing_candidates": [unknown],
        }
    }
    assert rescore["formal_metrics"]["status"] == "blocked_unreviewed_candidates"


def test_empty_pool_and_false_return_keep_existing_metric_semantics(
    tmp_path, six_reviewed, six_reviewed_path, capsys
):
    _, full, empty, ordered = selected_queries(six_reviewed)
    _, second_empty, *_ = [
        query for query in six_reviewed.queries if query["expect_empty"]
    ]
    reviewed = six_reviewed.reviewed_corpus_ids()
    empty_label = sorted(reviewed[empty["query_id"]])[0]
    projections = {
        "hybrid": {
            full["query_id"]: projection(full, [], threshold=0.0),
            empty["query_id"]: projection(
                empty, [(empty_label, 1.0)], threshold=0.0
            ),
            second_empty["query_id"]: projection(second_empty, [], threshold=0.0),
        }
    }
    document = source_document(
        six_reviewed,
        sorted(
            [full, empty, second_empty],
            key=lambda query: six_reviewed.queries.index(query),
        ),
        projections,
    )
    assert document["status"] == "complete"
    source = save_source(tmp_path, document)
    result = run_rescore(source, six_reviewed_path, capsys)
    rescore = json.loads(
        (source.parent / "followup" / result["rescore_id"] / "rescore.json").read_text(encoding="utf-8")
    )
    group = rescore["groups"]["hybrid"]
    overall = group["metrics"]["overall"]
    assert group["eligible_count"] == 3
    assert overall["with_labels"] == 1
    assert overall["empty_expectation_queries"] == 2
    assert overall["false_return_rate"] == 0.5
    assert overall["recall@20"] == 0.0
    assert overall["hit@5"] == 0.0
    assert group["metrics"]["per_query"][full["query_id"]]["scores"]["recall@20"] == 0.0
    assert group["metrics"]["per_query"][empty["query_id"]]["scores"]["false_return"] == 1.0
    assert group["metrics"]["per_query"][second_empty["query_id"]]["scores"]["false_return"] == 0.0
    assert rescore["formal_metrics"]["status"] == "complete_reviewed_full_coverage"


def test_common_intersection_and_same_pool_use_identical_query_sets(
    tmp_path, six_reviewed, six_reviewed_path, unknown, capsys
):
    partial, full, empty, _ = selected_queries(six_reviewed)
    reviewed = six_reviewed.reviewed_corpus_ids()
    partial_ids = sorted(reviewed[partial["query_id"]])
    partial_rows = [(corpus_id, 10.0 - index) for index, corpus_id in enumerate(partial_ids[:6])]
    full_rows = [(corpus_id, 5.0 - index) for index, corpus_id in enumerate(sorted(reviewed[full["query_id"]]))]
    empty_rows = [(sorted(reviewed[empty["query_id"]])[0], 1.0)]
    selected = sorted(
        [partial, full, empty],
        key=lambda query: six_reviewed.queries.index(query),
    )

    def group_rows(include_unknown: bool):
        rows = {
            partial["query_id"]: list(partial_rows),
            full["query_id"]: list(full_rows),
            empty["query_id"]: list(empty_rows),
        }
        if include_unknown:
            rows[full["query_id"]].append((unknown, 0.0))
            rows[empty["query_id"]].append((unknown, 0.0))
        return rows

    bm25_rows = group_rows(True)
    shared_rows = group_rows(False)
    bm25 = {
        query["query_id"]: projection(
            query, bm25_rows[query["query_id"]], threshold=None
        )
        for query in selected
    }
    hybrid = {
        query["query_id"]: projection(
            query, shared_rows[query["query_id"]], threshold=0.0
        )
        for query in selected
    }
    reranked = {
        query["query_id"]: projection(
            query,
            shared_rows[query["query_id"]],
            threshold=0.0,
            rerank_order=list(
                reversed([corpus_id for corpus_id, _ in shared_rows[query["query_id"]]])
            ),
        )
        for query in selected
    }
    document = source_document(
        six_reviewed,
        selected,
        {"bm25": bm25, "hybrid": hybrid, RERANK_GROUP: reranked},
    )
    assert document["status"] == "blocked"
    source = save_source(tmp_path, document)
    result = run_rescore(source, six_reviewed_path, capsys)
    rescore = json.loads(
        (source.parent / "followup" / result["rescore_id"] / "rescore.json").read_text(encoding="utf-8")
    )
    assert rescore["groups"]["bm25"]["eligible_query_ids"] == [partial["query_id"]]
    assert set(rescore["groups"]["hybrid"]["eligible_query_ids"]) == {
        partial["query_id"],
        full["query_id"],
        empty["query_id"],
    }
    common = rescore["common_intersection"]
    assert common["query_ids"] == [partial["query_id"]]
    assert common["count"] == 1
    assert common["expected_count"] == 3
    for group in common["groups"].values():
        assert group["metric_denominators"] == {
            "ranking_with_labels": 1,
            "expect_empty_false_return": 0,
        }
        assert group["metrics"]["overall"]["with_labels"] == 1
    comparison = rescore["same_pool_comparison"]
    assert comparison["status"] == "diagnostic_available"
    assert set(comparison["query_ids"]) == {
        query["query_id"] for query in selected
    }
    assert comparison["recall_at_20_equal"] is True
    assert comparison["final_input_reordered_query_count"] >= 1
    assert comparison["baseline"]["mean"]["recall@20"] == comparison["reranked"]["mean"]["recall@20"]
    assert rescore["source_history_usage"]["rerank_requests_recorded"] == 3


@pytest.mark.parametrize("mutation", ["reorder", "remove", "identity"])
def test_structural_errors_fail_instead_of_becoming_unreviewed(
    tmp_path, six_reviewed, six_reviewed_path, unknown, capsys, mutation
):
    partial, full, empty, ordered = selected_queries(six_reviewed)
    reviewed = six_reviewed.reviewed_corpus_ids()
    partial_rows = [(corpus_id, 10.0 - index) for index, corpus_id in enumerate(sorted(reviewed[partial["query_id"]])[:6])]
    partial_rows.append((unknown, 0.0))
    projections = {
        partial["query_id"]: projection(partial, partial_rows, threshold=0.0),
        full["query_id"]: projection(
            full,
            [(corpus_id, 5.0 - index) for index, corpus_id in enumerate(sorted(reviewed[full["query_id"]]))],
            threshold=0.0,
        ),
        empty["query_id"]: projection(empty, [], threshold=0.0),
    }
    document = source_document(six_reviewed, ordered, {"hybrid": projections})
    if mutation == "reorder":
        row = document["groups"]["hybrid"]["projections"][full["query_id"]]
        row["final_model_input"] = list(reversed(row["final_model_input"]))
    elif mutation == "remove":
        document["groups"]["hybrid"]["projections"].pop(full["query_id"])
    else:
        document["dataset"]["content_sha256"] = "0" * 64
    source = save_source(tmp_path, document)
    with pytest.raises(ValueError):
        rescore_staged(Namespace(source=source, dataset=six_reviewed_path, output=None))
    assert not (source.parent / "followup").exists()


def test_unapproved_or_drifted_scoring_data_is_rejected(
    tmp_path, six_reviewed, six_reviewed_path, unknown, capsys
):
    partial, full, empty, ordered = selected_queries(six_reviewed)
    reviewed = six_reviewed.reviewed_corpus_ids()
    partial_rows = [(corpus_id, 10.0 - index) for index, corpus_id in enumerate(sorted(reviewed[partial["query_id"]])[:6])]
    partial_rows.append((unknown, 0.0))
    projections = {
        partial["query_id"]: projection(partial, partial_rows, threshold=0.0),
        full["query_id"]: projection(
            full,
            [(corpus_id, 5.0 - index) for index, corpus_id in enumerate(sorted(reviewed[full["query_id"]]))],
            threshold=0.0,
        ),
        empty["query_id"]: projection(empty, [], threshold=0.0),
    }
    document = source_document(six_reviewed, ordered, {"hybrid": projections})
    source = save_source(tmp_path, document)

    unapproved = derive_dataset(
        six_reviewed_path,
        tmp_path / "unapproved",
        "v-test-unapproved",
        add_qrels=[{"query_id": partial["query_id"], "corpus_id": unknown, "grade": 0}],
        approve_new=False,
    )
    with pytest.raises(DatasetError):
        load_for_evaluation(unapproved)

    def change_query(queries):
        target = next(query for query in queries if query["query_id"] == full["query_id"])
        target["query"] = target["query"] + "-漂移"

    query_drift = derive_dataset(
        six_reviewed_path,
        tmp_path / "query-drift",
        "v-test-query-drift",
        mutate_query=change_query,
    )
    with pytest.raises(ValueError, match="查询正文"):
        rescore_staged(Namespace(source=source, dataset=query_drift, output=None))

    def change_grade(qrels):
        target = next(
            qrel
            for qrel in qrels
            if qrel["query_id"] == full["query_id"] and qrel["grade"] >= 1
        )
        target["grade"] = 0

    qrel_drift = derive_dataset(
        six_reviewed_path,
        tmp_path / "qrel-drift",
        "v-test-qrel-drift",
        mutate_qrel=change_grade,
    )
    with pytest.raises(ValueError, match="已有金标"):
        rescore_staged(Namespace(source=source, dataset=qrel_drift, output=None))

    def change_corpus(corpus):
        corpus[0]["text"] = corpus[0]["text"] + "-漂移"

    corpus_drift = derive_dataset(
        six_reviewed_path,
        tmp_path / "corpus-drift",
        "v-test-corpus-drift",
        mutate_corpus=change_corpus,
    )
    with pytest.raises(DatasetError):
        load_for_evaluation(corpus_drift)


def test_output_directory_must_stay_under_followup(
    tmp_path, six_reviewed, six_reviewed_path, unknown
):
    partial = selected_queries(six_reviewed)[0]
    document = source_document(
        six_reviewed,
        [partial],
        {
            "hybrid": {
                partial["query_id"]: projection(
                    partial, [(unknown, 1.0)], threshold=0.0
                )
            }
        },
    )
    source = save_source(tmp_path, document)
    with pytest.raises(ValueError, match="followup"):
        rescore_staged(
            Namespace(
                source=source,
                dataset=six_reviewed_path,
                output=tmp_path / "outside",
            )
        )
