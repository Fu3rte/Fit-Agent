import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

import pytest

from evaluation import decision_feedback as df
from evaluation import external_summary as es
from evaluation import sdk_summary as ss
from evaluation import summary as sm
from evaluation.contracts import (
    EXTERNAL_JUDGE_METRICS,
    ExternalJudgeApplicability,
)
from evaluation.dataset import BACKEND, load_for_evaluation
from evaluation.decision_feedback import (
    _history_from_batches,
    _load_collection,
    _persist_batch,
    build_feedback_batch,
    collect_feedback,
    feedback_binding_path,
    feedback_index_path,
    load_feedback_context,
    normalize_feedback,
)
from evaluation.generation_upload import (
    _external_applicability_sha256,
    applicability_path,
    check_external_projection,
    load_external_applicability,
    load_external_run,
)
from evaluation.langsmith_scenarios import assertion_statistics
from evaluation.scenario_evidence import canonical_json, sha256_text
from evaluation.scenarios import (
    EXTERNAL_PROTOCOL_VERSION,
    load_external_scoring,
)
from evaluation.scenarios import (
    code_identity as scenario_code_identity,
)
from test.test_decision_feedback import (
    EVIDENCE_METRIC,
    REFERENCE_METRIC,
    binding_document,
    decisions_for,
    make_feedback,
    write_receipts,
)
from test.test_external_judge_checkpoints import (
    FAILED_SCENARIO,
    PROFILE_SCENARIO,
    RUN_ID,
    approval_entries,
    build_fixture_run,
    default_decisions,
    multi_turn_execution,
    write_approval,
    write_json,
    write_single_scenario_run,
)
from test.test_sdk_summary import _retrieval_run, _write_scenario_run

DATASET_DIR = BACKEND / "dataset" / "evaluation" / "rebuild-v1"
DATASET = load_for_evaluation(DATASET_DIR)


@dataclass(frozen=True)
class Prepared:
    run_dir: Path
    payload: dict[str, Any] | None
    runs: dict[str, str]
    context: Any

    @property
    def row(self) -> dict[str, Any] | None:
        return None if self.payload is None else self.payload["rows"][0]

    @property
    def run_id(self) -> str:
        assert self.row is not None
        return self.runs[self.row["row_id"]]


def _augment_run(run_dir: Path, *, purpose: str | None = None) -> None:
    path = run_dir / "sdk-run.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    code = scenario_code_identity()
    document["code_identity"] = {
        key: code[key]
        for key in ("commit", "dirty", "lock_sha256", "source_sha256")
    }
    document["generation"] = {
        "model": json.dumps(
            {
                "api": "openai-completions",
                "provider": "fixture",
                "model": "fixture-model",
            }
        ),
        "prompt_version": "a" * 64,
    }
    document["index"] = {
        "collection": "fit-agent-test-index",
        "manifest_sha256": "c" * 64,
        "build_id": "test",
    }
    document["experiment"] = {
        "id": "fixture-scenario-experiment",
        "name": "fixture",
        "url": "https://example.invalid",
    }
    document["concurrency"] = {
        "calls": len(document["samples"]),
        "max_in_flight": len(document["samples"]),
        "overlap_pairs": 1,
        "intervals": [],
    }
    document["max_concurrency"] = 6
    document["num_repetitions"] = 1
    if purpose is not None:
        document["purpose"] = purpose
    records = []
    for sample in document["samples"]:
        record = load_external_scoring(
            run_dir / sample["scoring_relative"]
        )
        records.append(
            {
                **{
                    key: sample.get(key)
                    for key in (
                        "example_id",
                        "scenario_id",
                        "scoring_mode",
                        "status",
                        "execution_status",
                        "execution_sha256",
                        "scoring_relative",
                        "scoring_sha256",
                        "scoring_reused",
                        "feedback",
                        "expected_feedback",
                        "incomplete",
                        "sdk_run_id",
                        "run_error",
                        "error",
                        "assertion_status",
                        "generation_scoring_status",
                        "feedback_version",
                    )
                },
                "assertions": assertion_statistics(record),
                "generation": [],
            }
        )
    document["scenarios"] = records
    write_json(path, document)


def _prepare(
    tmp_path: Path,
    *,
    decisions: dict | Callable | None = None,
    purpose: str | None = None,
    all_not_applicable: bool = False,
) -> Prepared:
    source = tmp_path / "source"
    run_dir, _ = build_fixture_run(DATASET, source)
    _augment_run(run_dir, purpose=purpose)
    evidence = load_external_run(run_dir, DATASET)
    if all_not_applicable:
        chosen = decisions_for(evidence, "all_not_applicable")
    elif callable(decisions):
        chosen = decisions(evidence)
    elif decisions is not None:
        chosen = decisions
    else:
        chosen = decisions_for(evidence, "complete")
    write_approval(run_dir, approval_entries(evidence, chosen), DATASET)
    if all_not_applicable:
        applicability = load_external_applicability(evidence)
        binding = binding_document(
            evidence,
            None,
            _external_applicability_sha256(evidence, applicability),
        )
        write_json(feedback_binding_path(run_dir), binding)
        context = load_feedback_context(
            run_dir, dataset_dir=DATASET_DIR
        )
        return Prepared(run_dir=run_dir, payload=None, runs={}, context=context)
    check_external_projection(run_dir, dataset_dir=DATASET_DIR)
    payload = json.loads(
        (
            run_dir / "external-judge" / "checkpoints-payload.json"
        ).read_text(encoding="utf-8")
    )
    runs = write_receipts(run_dir, payload)
    binding = binding_document(
        evidence, payload, payload["applicability_sha256"]
    )
    write_json(feedback_binding_path(run_dir), binding)
    context = load_feedback_context(run_dir, dataset_dir=DATASET_DIR)
    return Prepared(
        run_dir=run_dir, payload=payload, runs=runs, context=context
    )


def _prepare_multi_turn(tmp_path: Path) -> Prepared:
    execution = multi_turn_execution(DATASET)
    run_dir = write_single_scenario_run(
        tmp_path / "source", DATASET, execution
    )
    _augment_run(run_dir)
    evidence = load_external_run(run_dir, DATASET)
    decisions = {}
    for checkpoint in evidence.checkpoints:
        applicable = checkpoint.link.checkpoint_id == "cp2"
        decisions[checkpoint.key] = [
            ExternalJudgeApplicability(
                metric=metric,
                applicable=applicable,
                reason=(
                    None
                    if applicable
                    else "旧 Ragas 适用性不得替代外部批准"
                ),
            )
            for metric in EXTERNAL_JUDGE_METRICS
        ]
    write_approval(run_dir, approval_entries(evidence, decisions), DATASET)
    check_external_projection(run_dir, dataset_dir=DATASET_DIR)
    payload = json.loads(
        (
            run_dir / "external-judge" / "checkpoints-payload.json"
        ).read_text(encoding="utf-8")
    )
    runs = write_receipts(run_dir, payload)
    binding = binding_document(
        evidence, payload, payload["applicability_sha256"]
    )
    write_json(feedback_binding_path(run_dir), binding)
    context = load_feedback_context(run_dir, dataset_dir=DATASET_DIR)
    return Prepared(
        run_dir=run_dir, payload=payload, runs=runs, context=context
    )


def _complete_items(
    prepared: Prepared,
    evidence_score: float | None = 0.82,
    reference_score: float | None = 0.38,
    **overrides: Any,
) -> list[Any]:
    run_id = prepared.run_id
    evidence = make_feedback(
        run_id,
        EVIDENCE_METRIC,
        evidence_score,
        typesafe={"model": "jev-1.13.0"},
        **overrides,
    )
    reference = make_feedback(
        run_id,
        REFERENCE_METRIC,
        reference_score,
        typesafe={"model": "jev-1.13.0"},
        **overrides,
    )
    return [evidence, reference]


def _persist(
    prepared: Prepared,
    items: list[Any],
    history: Any = None,
) -> tuple[Any, Path]:
    assert prepared.row is not None
    run_id = prepared.run_id
    records = normalize_feedback(
        prepared.context,
        {prepared.row["row_id"]: run_id},
        {run_id: items},
        history,
    )
    batch = build_feedback_batch(prepared.context, records)
    loaded = _load_collection(prepared.context)
    path, _ = _persist_batch(prepared.context, batch, loaded)
    return batch, path


def _source(run_dir: Path) -> es.ExternalScenarioSource:
    return es.load_external_scenario_source(run_dir, DATASET)


def _summary(
    run_dir: Path,
    *,
    scope: str = "auto",
    retrieval: dict | None = None,
    retrieval_blocking: list[str] | None = None,
) -> dict:
    source = _source(run_dir)
    return es.build_summary(
        source,
        scope=scope,
        retrieval=retrieval,
        retrieval_blocking=retrieval_blocking or [],
    )


def _metric_view(
    status: str, score: float | None = None
) -> es.MetricView:
    return es.MetricView(
        metric=EVIDENCE_METRIC,
        applicable=status != "not_applicable",
        approved=True,
        status=status,
        score=score,
    )


def _scenario_view(
    scenario_id: str,
    specs: dict[str, tuple[str, float | None]],
) -> es.ScenarioView:
    view = es.ScenarioView(
        scenario_id=scenario_id,
        state="completed",
        execution={},
        assertions={},
    )
    for checkpoint_id, (status, score) in specs.items():
        checkpoint = es.CheckpointView(
            scenario_id=scenario_id,
            event_id="e01",
            checkpoint_id=checkpoint_id,
            execution_sha256="a" * 64,
            classification="uploadable",
        )
        checkpoint.entries[EVIDENCE_METRIC] = _metric_view(status, score)
        view.checkpoints[(scenario_id, "e01", checkpoint_id)] = checkpoint
    return view


def test_zero_one_probability_counts_and_scenario_values(tmp_path):
    prepared = _prepare(tmp_path)
    batch, _ = _persist(
        prepared,
        _complete_items(prepared, evidence_score=0.0, reference_score=1.0),
    )
    assert batch.status == "complete"
    summary = _summary(prepared.run_dir)
    assert summary["schema"] == es.EXTERNAL_SUMMARY_SCHEMA
    evidence = summary["generation"]["metrics"][EVIDENCE_METRIC]
    reference = summary["generation"]["metrics"][REFERENCE_METRIC]
    for row, value in ((evidence, 0.0), (reference, 1.0)):
        assert row["expected_checkpoints"] == 3
        assert row["applicable_checkpoints"] == 1
        assert row["not_applicable_checkpoints"] == 2
        assert row["succeeded_checkpoints"] == 1
        assert row["failed_checkpoints"] == 0
        assert row["available_scenarios"] == 1
        assert row["complete_scenarios"] == 1
        assert row["mean_including_partial"] == pytest.approx(value)
        assert row["mean_complete_only"] == pytest.approx(value)
    assert summary["feedback_status"] == "complete"
    assert summary["release"]["checks"]["feedback_evidence_complete"] is True
    assert summary["release"]["checks"]["generation_scoring_succeeded"] is True
    assert summary["release"]["effective"] is False
    versions = summary["judge"]["model_versions"][EVIDENCE_METRIC]
    assert versions["reported"] == ["jev-1.13.0"]
    assert versions["unreported"] == 0


def test_all_not_applicable_is_complete_without_values_or_release(tmp_path):
    prepared = _prepare(tmp_path, all_not_applicable=True)
    result = collect_feedback(
        prepared.run_dir, dataset_dir=DATASET_DIR
    )
    assert result["status"] == "complete"
    summary = _summary(prepared.run_dir)
    for metric in EXTERNAL_JUDGE_METRICS:
        row = summary["generation"]["metrics"][metric]
        assert row["expected_checkpoints"] == 3
        assert row["applicable_checkpoints"] == 0
        assert row["not_applicable_checkpoints"] == 3
        assert row["mean_including_partial"] is None
        assert row["mean_complete_only"] is None
        assert row["all_not_applicable_scenarios"] == sorted(
            [PROFILE_SCENARIO, FAILED_SCENARIO]
        )
    assert summary["generation"]["status"] == "all_not_applicable"
    assert summary["release"]["checks"]["generation_all_not_applicable"] is True
    assert summary["release"]["effective"] is False
    json.dumps(summary, allow_nan=False)


def test_scenario_macro_mean_with_unequal_checkpoints_and_partial_success():
    scenarios = {
        "s1": _scenario_view(
            "s1", {"cp1": ("succeeded", 0.0), "cp2": ("succeeded", 1.0)}
        ),
        "s2": _scenario_view(
            "s2", {"cp1": ("succeeded", 0.2), "cp2": ("not_applicable", None)}
        ),
        "s3": _scenario_view(
            "s3", {"cp1": ("succeeded", 0.8), "cp2": ("failed", None)}
        ),
        "s4": _scenario_view("s4", {"cp1": ("not_applicable", None)}),
    }
    row = es._aggregate_metric(
        EVIDENCE_METRIC, ["s1", "s2", "s3", "s4"], scenarios
    )
    assert row["expected_checkpoints"] == 7
    assert row["applicable_checkpoints"] == 5
    assert row["not_applicable_checkpoints"] == 2
    assert row["succeeded_checkpoints"] == 4
    assert row["failed_checkpoints"] == 1
    assert row["expected_scenarios"] == 4
    assert row["available_scenarios"] == 3
    assert row["complete_scenarios"] == 2
    assert row["mean_including_partial"] == pytest.approx(0.5)
    assert row["mean_complete_only"] == pytest.approx(0.35)
    assert row["scenario_values"]["s1"]["value"] == pytest.approx(0.5)
    assert row["scenario_values"]["s2"]["value"] == pytest.approx(0.2)
    assert row["scenario_values"]["s3"]["value"] == pytest.approx(0.8)
    assert row["scenario_values"]["s3"]["complete"] is False
    assert row["scenario_values"]["s4"]["value"] is None
    assert row["scenario_values"]["s4"]["all_not_applicable"] is True
    assert row["scenario_values"]["s4"]["complete"] is False
    assert row["all_not_applicable_scenarios"] == ["s4"]


def test_approved_applicability_replaces_ragas_denominator(tmp_path):
    prepared = _prepare_multi_turn(tmp_path)
    batch, _ = _persist(
        prepared,
        _complete_items(prepared, evidence_score=0.7, reference_score=0.6),
    )
    assert batch.status == "complete"
    summary = _summary(prepared.run_dir)
    scenario = summary["scenarios"][PROFILE_SCENARIO]
    expected_values = {
        EVIDENCE_METRIC: 0.7,
        REFERENCE_METRIC: 0.6,
    }
    for metric in EXTERNAL_JUDGE_METRICS:
        row = summary["generation"]["metrics"][metric]
        assert row["expected_checkpoints"] == 2
        assert row["applicable_checkpoints"] == 1
        assert row["not_applicable_checkpoints"] == 1
        assert row["succeeded_checkpoints"] == 1
        assert scenario["metrics"][metric]["entries"]["cp1"][
            "status"
        ] == "not_applicable"
        assert scenario["metrics"][metric]["entries"]["cp2"][
            "status"
        ] == "succeeded"
        assert scenario["metrics"][metric]["value"] == pytest.approx(
            expected_values[metric]
        )
    frozen = next(
        item for item in DATASET.scenarios
        if item["scenario_id"] == PROFILE_SCENARIO
    )
    assert any(
        row["checkpoint_id"] == "cp1" and row["applicable"]
        for row in frozen["metric_applicability"]
    )


def test_missing_sample_keeps_full_denominator_and_blocks(tmp_path):
    prepared = _prepare(tmp_path)
    _persist(prepared, _complete_items(prepared))
    path = prepared.run_dir / "sdk-run.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["samples"] = [
        sample
        for sample in document["samples"]
        if sample["scenario_id"] != FAILED_SCENARIO
    ]
    write_json(path, document)
    summary = _summary(prepared.run_dir)
    assert summary["coverage"]["expected_scenarios"] == 2
    assert FAILED_SCENARIO in summary["coverage"]["missing_scenarios"]
    assert summary["coverage"]["expected_events"] == summary["execution"][
        "expected_events"
    ]
    assert summary["execution"]["observed_events"] < summary["execution"][
        "expected_events"
    ]
    for metric in EXTERNAL_JUDGE_METRICS:
        row = summary["generation"]["metrics"][metric]
        assert row["expected_checkpoints"] == 3
        assert row["unapproved_checkpoints"] == 3
    assert summary["release"]["effective"] is False
    assert any(
        "源证据校验失败" in issue
        for issue in summary["release"]["blocking_issues"]
    )


def test_missing_upload_and_assertions_keep_denominator(tmp_path):
    prepared = _prepare(tmp_path, decisions=default_decisions)
    summary = _summary(prepared.run_dir)
    assert summary["feedback_status"] == "absent"
    for metric in EXTERNAL_JUDGE_METRICS:
        row = summary["generation"]["metrics"][metric]
        assert row["expected_checkpoints"] == 3
        assert row["applicable_checkpoints"] == 2
        assert row["not_applicable_checkpoints"] == 1
        assert row["pending_checkpoints"] == 1
        assert row["missing_checkpoints"] == 1
        assert row["mean_including_partial"] is None
    assert summary["assertions"]["complete"] is False
    assert summary["assertions"]["observed"] < summary["assertions"]["expected"]
    assert summary["release"]["effective"] is False
    assert summary["release"]["checks"]["feedback_evidence_complete"] is False


def test_batch_complete_with_failed_score_blocks_release(tmp_path):
    prepared = _prepare(tmp_path)
    items = [
        make_feedback(
            prepared.run_id,
            EVIDENCE_METRIC,
            None,
            extra={"error": True},
            comment="云端评分失败",
        ),
        make_feedback(
            prepared.run_id,
            REFERENCE_METRIC,
            0.38,
            typesafe={"model": "jev-1.13.0"},
        ),
    ]
    batch, _ = _persist(prepared, items)
    assert batch.status == "complete"
    summary = _summary(prepared.run_dir)
    evidence = summary["generation"]["metrics"][EVIDENCE_METRIC]
    reference = summary["generation"]["metrics"][REFERENCE_METRIC]
    assert evidence["failed_checkpoints"] == 1
    assert evidence["succeeded_checkpoints"] == 0
    assert evidence["mean_including_partial"] is None
    assert reference["succeeded_checkpoints"] == 1
    assert summary["feedback_status"] == "complete"
    assert summary["release"]["checks"]["feedback_evidence_complete"] is True
    assert summary["release"]["checks"]["generation_scoring_succeeded"] is False
    assert summary["release"]["effective"] is False
    assert any(
        "存在失败检查点" in issue
        for issue in summary["release"]["blocking_issues"]
    )


def test_latest_pending_is_reported_without_fallback(tmp_path):
    prepared = _prepare(tmp_path)
    batch, _ = _persist(
        prepared,
        _complete_items(prepared, reference_score=None)[:1],
    )
    assert batch.status == "pending"
    summary = _summary(prepared.run_dir)
    assert summary["feedback_status"] == "pending"
    assert summary["identity"]["collection"]["latest"] == summary[
        "identity"
    ]["collection"]["batch_id"]
    assert summary["identity"]["collection"]["batches"] == 1
    reference = summary["generation"]["metrics"][REFERENCE_METRIC]
    evidence = summary["generation"]["metrics"][EVIDENCE_METRIC]
    assert reference["pending_checkpoints"] == 1
    assert reference["mean_including_partial"] is None
    assert evidence["succeeded_checkpoints"] == 1
    assert summary["release"]["effective"] is False


def test_latest_blocked_does_not_use_older_complete_or_stitch(tmp_path):
    prepared = _prepare(tmp_path)
    first, first_path = _persist(prepared, _complete_items(prepared))
    assert first.status == "complete"
    loaded = _load_collection(prepared.context)
    history = _history_from_batches(
        prepared.context, list(loaded.batches.values())
    )
    second, second_path = _persist(
        prepared, _complete_items(prepared)[:1], history
    )
    assert second.status == "blocked"
    summary = _summary(prepared.run_dir)
    assert summary["feedback_status"] == "blocked"
    assert summary["identity"]["collection"]["latest"] == summary[
        "identity"
    ]["collection"]["batch_id"]
    assert summary["identity"]["collection"]["batches"] == 2
    assert first_path.is_file()
    assert second_path.is_file()
    reference = summary["generation"]["metrics"][REFERENCE_METRIC]
    assert reference["blocked_checkpoints"] == 1
    assert reference["pending_checkpoints"] == 0
    assert reference["mean_including_partial"] is None
    assert summary["release"]["effective"] is False


def _tamper_conflict_attempt(document: dict) -> None:
    record = _scored_record(document)
    attempts = [
        item
        for item in record["attempts"]
        if item["feedback"]["metric"] == EVIDENCE_METRIC
    ]
    assert len(attempts) == 2
    kept = max(
        attempts, key=lambda item: item["feedback"]["score"] or 0.0
    )
    record["attempts"].remove(
        next(item for item in attempts if item is not kept)
    )
    row = next(
        item
        for item in record["feedback_set"]["feedback"]
        if item["metric"] == EVIDENCE_METRIC
    )
    row["status"] = kept["feedback"]["status"]
    row["score"] = kept["feedback"]["score"]
    row["failure_evidence"] = kept["feedback"]["failure_evidence"]
    record["diagnostics"] = [
        item
        for item in record["diagnostics"]
        if item["code"] != "feedback_conflict"
    ]
    record["status"] = "complete"
    document["status"] = "complete"
    document["blocking"] = []


def test_conflict_attempt_cannot_be_dropped_from_collection(tmp_path):
    prepared = _prepare(tmp_path)
    conflict = make_feedback(
        prepared.run_id,
        EVIDENCE_METRIC,
        0.2,
        typesafe={"model": "jev-1.13.0"},
    )
    batch, batch_path = _persist(
        prepared, [*_complete_items(prepared), conflict]
    )
    assert batch.status == "blocked"
    _rewrite_batch(prepared, batch_path, _tamper_conflict_attempt)
    summary = _summary(prepared.run_dir)
    assert summary["release"]["effective"] is False
    assert any(
        "原始终态" in item
        for item in summary["release"]["blocking_issues"]
    )
    for metric in EXTERNAL_JUDGE_METRICS:
        row = summary["generation"]["metrics"][metric]
        assert row["mean_including_partial"] is None
        assert row["mean_complete_only"] is None


def test_two_attempts_for_one_metric_do_not_produce_a_score(tmp_path):
    prepared = _prepare(tmp_path)
    _persist(prepared, _complete_items(prepared))
    loaded = _load_collection(prepared.context)
    history = _history_from_batches(
        prepared.context, list(loaded.batches.values())
    )
    items = _complete_items(prepared)
    conflict = make_feedback(
        prepared.run_id,
        EVIDENCE_METRIC,
        0.2,
        typesafe={"model": "jev-1.13.0"},
    )
    batch, _ = _persist(prepared, [*items, conflict], history)
    assert batch.status == "blocked"
    summary = _summary(prepared.run_dir)
    evidence = summary["generation"]["metrics"][EVIDENCE_METRIC]
    scenario = summary["scenarios"][PROFILE_SCENARIO]
    assert evidence["blocked_checkpoints"] == 1
    assert evidence["succeeded_checkpoints"] == 0
    assert evidence["mean_including_partial"] is None
    entry = scenario["metrics"][EVIDENCE_METRIC]["entries"]["cp1"]
    assert entry["status"] == "blocked"
    assert len(entry["attempt_ids"]) == 2
    assert entry["score"] is None
    assert summary["feedback_status"] == "blocked"
    assert summary["release"]["effective"] is False


def _tamper_execution(run_dir: Path) -> None:
    path = next((run_dir / "scenarios").glob("**/execution.json"))
    document = json.loads(path.read_text(encoding="utf-8"))
    document["session_ids"] = [*document["session_ids"], "tampered-session"]
    write_json(path, document)


def _tamper_index(run_dir: Path) -> None:
    path = feedback_index_path(run_dir)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["latest"] = "batch-missing"
    write_json(path, document)


def _scored_record(document: dict) -> dict:
    return next(
        item
        for item in document["checkpoints"]
        if item["attempts"] and item["evidence"]
    )


def _tamper_batch(run_dir: Path, prepared: Prepared, batch_path: Path) -> None:
    document = json.loads(batch_path.read_text(encoding="utf-8"))
    _scored_record(document)["attempts"][0]["feedback"]["score"] = 0.5
    write_json(batch_path, document)


def _tamper_evidence(run_dir: Path, prepared: Prepared, batch_path: Path) -> None:
    document = json.loads(batch_path.read_text(encoding="utf-8"))
    _scored_record(document)["evidence"][0]["raw"]["score"] = 0.5
    write_json(batch_path, document)


def _tamper_projection(run_dir: Path) -> None:
    path = run_dir / "external-judge" / "checkpoints-payload.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["rows"][0]["projection_sha256"] = "0" * 64
    write_json(path, document)


def _tamper_approval(run_dir: Path) -> None:
    path = applicability_path(run_dir)
    document = json.loads(path.read_text(encoding="utf-8"))
    approval = document["entries"][0]["applicability"][0]
    approval["applicable"] = not approval["applicable"]
    if approval["applicable"]:
        approval["reason"] = None
    else:
        approval["reason"] = "篡改测试"
    write_json(path, document)


@pytest.mark.parametrize(
    "tamper",
    [
        "execution",
        "index",
        "batch",
        "evidence",
        "projection",
        "approval",
    ],
)
def test_corrupt_or_tampered_evidence_is_rejected(tmp_path, tamper):
    prepared = _prepare(tmp_path)
    _, batch_path = _persist(prepared, _complete_items(prepared))
    if tamper == "execution":
        _tamper_execution(prepared.run_dir)
    elif tamper == "index":
        _tamper_index(prepared.run_dir)
    elif tamper == "batch":
        _tamper_batch(prepared.run_dir, prepared, batch_path)
    elif tamper == "evidence":
        _tamper_evidence(prepared.run_dir, prepared, batch_path)
    elif tamper == "projection":
        _tamper_projection(prepared.run_dir)
    else:
        _tamper_approval(prepared.run_dir)
    summary = _summary(prepared.run_dir)
    assert summary["release"]["effective"] is False
    assert summary["release"]["blocking_issues"]
    for metric in EXTERNAL_JUDGE_METRICS:
        row = summary["generation"]["metrics"][metric]
        assert row["expected_checkpoints"] == 3
    assert summary["feedback_status"] == "absent"


def _rewrite_batch(
    prepared: Prepared, batch_path: Path, mutate: Callable[[dict], None]
) -> None:
    document = json.loads(batch_path.read_text(encoding="utf-8"))
    mutate(document)
    for record in document["checkpoints"]:
        record["evidence_sha256"] = sha256_text(
            canonical_json(
                {
                    "evidence": sorted(
                        record["evidence"],
                        key=lambda item: item["evidence_id"],
                    ),
                    "feedback_set": record["feedback_set"],
                    "attempts": sorted(
                        record["attempts"],
                        key=lambda item: item["attempt_id"],
                    ),
                }
            )
        )
    document["content_sha256"] = df._batch_content_sha256(document)
    batch_id = df._batch_id(document["content_sha256"])
    target = batch_path.parent / f"{batch_id}.json"
    write_json(target, document)
    index_path = feedback_index_path(prepared.run_dir)
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["batches"] = [
        {
            "batch_id": batch_id,
            "content_sha256": document["content_sha256"],
            "path": f"batches/{batch_id}.json",
            "read_at": document["read_at"],
            "status": document["status"],
        }
    ]
    index["latest"] = batch_id
    write_json(index_path, index)
    if target != batch_path:
        batch_path.unlink()


def _tamper_normalized(document: dict, kind: str) -> None:
    record = _scored_record(document)
    attempt = next(
        item
        for item in record["attempts"]
        if item["feedback"]["status"] == "succeeded"
    )
    if kind == "score":
        attempt["feedback"]["score"] = 0.11
        row = next(
            item
            for item in record["feedback_set"]["feedback"]
            if item["metric"] == attempt["feedback"]["metric"]
        )
        row["score"] = 0.11
        return
    evidence = next(
        item
        for item in record["evidence"]
        if item["feedback_id"] == attempt["cloud_feedback"]["feedback_id"]
    )
    raw = evidence["raw"]
    raw["extra"] = {**(raw.get("extra") or {}), "error": True}
    digest = sha256_text(canonical_json(raw))
    evidence["raw_sha256"] = digest
    evidence["evidence_id"] = f"{evidence['feedback_id']}@{digest[:12]}"


@pytest.mark.parametrize("tamper", ["score", "error"])
def test_normalized_feedback_must_match_raw_response(tmp_path, tamper):
    prepared = _prepare(tmp_path)
    _, batch_path = _persist(prepared, _complete_items(prepared))
    _rewrite_batch(
        prepared,
        batch_path,
        lambda document: _tamper_normalized(document, tamper),
    )
    summary = _summary(prepared.run_dir)
    assert summary["release"]["effective"] is False
    assert any(
        "归一化结果与原始反馈不一致" in item
        for item in summary["release"]["blocking_issues"]
    )
    for metric in EXTERNAL_JUDGE_METRICS:
        row = summary["generation"]["metrics"][metric]
        assert row["mean_including_partial"] is None
        assert row["mean_complete_only"] is None


def _request_evidence(run_dir: Path):
    for execution_path in sorted(
        (run_dir / "scenarios").glob("**/execution.json")
    ):
        document = json.loads(execution_path.read_text(encoding="utf-8"))
        events = {event["event_id"]: event for event in document["events"]}
        for entry in document["generation_checkpoints"]:
            if entry["checkpoint"] is None:
                continue
            event = events[entry["event_id"]]
            call = next(
                item
                for item in event["model_calls"]
                if item["message_id"] == event["answer_message_id"]
            )
            return (
                execution_path.parent / "model-calls" / call["request_path"],
                execution_path,
                document,
                event,
                call,
            )
    raise AssertionError("夹具缺少回答模型调用")


def test_missing_request_evidence_blocks_external_release(tmp_path):
    prepared = _prepare(tmp_path)
    path, *_ = _request_evidence(prepared.run_dir)
    path.unlink()
    summary = _summary(prepared.run_dir)
    assert summary["release"]["effective"] is False
    assert any(
        "模型请求证据文件缺失" in item
        for item in summary["release"]["blocking_issues"]
    )


def test_request_digest_mismatch_blocks_external_release(tmp_path):
    prepared = _prepare(tmp_path)
    path, *_ = _request_evidence(prepared.run_dir)
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    summary = _summary(prepared.run_dir)
    assert summary["release"]["effective"] is False
    assert any(
        "模型请求证据摘要不一致" in item
        for item in summary["release"]["blocking_issues"]
    )


def test_request_context_replay_mismatch_is_rejected(tmp_path):
    prepared = _prepare(tmp_path)
    evidence = load_external_run(prepared.run_dir, DATASET)
    document = json.loads(
        (prepared.run_dir / "sdk-run.json").read_text(encoding="utf-8")
    )
    row = next(
        item
        for item in evidence.scenarios
        if any(
            entry.checkpoint is not None
            for entry in item.execution.generation_checkpoints
        )
    )
    entry = next(
        item
        for item in row.execution.generation_checkpoints
        if item.checkpoint is not None
    )
    event = next(
        item for item in row.execution.events if item.event_id == entry.event_id
    )
    call = next(
        item
        for item in event.model_calls
        if item.message_id == event.answer_message_id
    )
    path = row.root / "model-calls" / call.request_path
    request = json.loads(path.read_text(encoding="utf-8"))
    request["messages"][0]["content"] = "被替换的请求正文"
    text = json.dumps(
        request,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    path.write_text(text, encoding="utf-8")
    execution = row.execution.model_copy(deep=True)
    changed_event = next(
        item for item in execution.events if item.event_id == event.event_id
    )
    changed_call = next(
        item
        for item in changed_event.model_calls
        if item.message_id == event.answer_message_id
    )
    changed_call.request_sha256 = sha256_text(text)
    changed_row = replace(row, execution=execution)
    changed_evidence = replace(
        evidence,
        scenarios=tuple(
            changed_row if item is row else item for item in evidence.scenarios
        ),
    )
    issues = es._request_issues(changed_evidence, document)
    assert any("真实请求" in item or "上下文" in item for item in issues)


@pytest.mark.parametrize("field", ["answer_text", "answer_structured"])
def test_checkpoint_answer_must_match_event_capture(tmp_path, field):
    prepared = _prepare(tmp_path)
    evidence = load_external_run(prepared.run_dir, DATASET)
    document = json.loads(
        (prepared.run_dir / "sdk-run.json").read_text(encoding="utf-8")
    )
    assert es._request_issues(evidence, document) == []
    row = next(
        item
        for item in evidence.scenarios
        if any(
            entry.checkpoint is not None
            for entry in item.execution.generation_checkpoints
        )
    )
    entry = next(
        item
        for item in row.execution.generation_checkpoints
        if item.checkpoint is not None
    )
    execution = row.execution.model_copy(deep=True)
    event = next(
        item for item in execution.events if item.event_id == entry.event_id
    )
    if field == "answer_text":
        event.answer_text = "被替换的回答正文"
        expected = "回答与事件捕获不一致"
    else:
        event.answer_structured = {"tampered": True}
        expected = "结构化回答与事件捕获不一致"
    changed_row = replace(row, execution=execution)
    changed_evidence = replace(
        evidence,
        scenarios=tuple(
            changed_row if item is row else item for item in evidence.scenarios
        ),
    )
    issues = es._request_issues(changed_evidence, document)
    assert any(expected in item for item in issues)


def test_unreported_versions_and_declared_binding_are_not_blockers(tmp_path):
    prepared = _prepare(tmp_path)
    items = [
        make_feedback(
            prepared.run_id,
            EVIDENCE_METRIC,
            0.8,
            typesafe={"model": "jev-latest"},
        ),
        make_feedback(
            prepared.run_id,
            REFERENCE_METRIC,
            0.4,
            typesafe={"model": "jev-latest"},
        ),
    ]
    _persist(prepared, items)
    summary = _summary(prepared.run_dir)
    assert summary["source_assurance"] == "declared_binding"
    assert summary["definitions"]["source_assurance"]["kind"] == "declared_binding"
    assert summary["identity"]["binding_sha256"]
    for metric in EXTERNAL_JUDGE_METRICS:
        versions = summary["judge"]["model_versions"][metric]
        assert versions["unreported"] == 1
        assert versions["reported"] == []
    joined = " ".join(summary["release"]["blocking_issues"])
    assert "declared_binding" not in joined
    assert "unreported" not in joined
    assert summary["generation"]["metrics"][EVIDENCE_METRIC][
        "succeeded_checkpoints"
    ] == 1


def test_cloud_success_cannot_override_local_failure(tmp_path):
    prepared = _prepare(tmp_path)
    _persist(prepared, _complete_items(prepared))
    summary = _summary(prepared.run_dir)
    assert summary["release"]["checks"]["generation_scoring_succeeded"] is True
    assert summary["release"]["checks"]["execution_complete"] is False
    assert summary["release"]["checks"]["assertions_complete"] is False
    assert summary["release"]["effective"] is False
    joined = " ".join(summary["release"]["blocking_issues"])
    assert "执行" in joined
    assert "断言" in joined


def test_formal_scope_cannot_force_validation_run(tmp_path):
    prepared = _prepare(tmp_path)
    _persist(prepared, _complete_items(prepared))
    summary = _summary(
        prepared.run_dir,
        scope="formal",
        retrieval_blocking=["正式检索来源不可用"],
    )
    assert summary["scope"] == "formal"
    assert summary["formal_scope"] is False
    assert summary["release"]["effective"] is False
    joined = " ".join(summary["release"]["blocking_issues"])
    assert "定向验证范围不能作为正式发布" in joined
    assert "定向验证范围不能作为有效发布" in joined
    assert "正式检索来源不可用" in joined


def test_sdk_summary_missing_retrieval_lists_blocker(tmp_path):
    prepared = _prepare(tmp_path)
    _persist(prepared, _complete_items(prepared))
    summary = ss.build_summary(
        tmp_path / "missing-retrieval",
        prepared.run_dir,
        DATASET_DIR,
    )
    assert summary["schema"] == es.EXTERNAL_SUMMARY_SCHEMA
    assert summary["release"]["effective"] is False
    assert any(
        "来源不存在" in issue or "检索" in issue
        for issue in summary["release"]["blocking_issues"]
    )


def test_protocol_dispatch_rejects_unknown_or_conflicting_versions(tmp_path):
    prepared = _prepare(tmp_path)
    doc = json.loads(
        (prepared.run_dir / "sdk-run.json").read_text(encoding="utf-8")
    )
    cases = [
        {"feedback_version": 4},
        {"feedback_version": 2},
    ]
    for change in cases:
        path = tmp_path / f"case-{change['feedback_version']}"
        path.mkdir()
        target = path / RUN_ID
        target.mkdir()
        document = {**doc, **change}
        write_json(target / "sdk-run.json", document)
        with pytest.raises(ss.SDKSummaryError):
            ss.build_summary(
                tmp_path / "missing-retrieval",
                target,
                DATASET_DIR,
            )
    missing_protocol = dict(doc)
    missing_protocol.pop("protocol_version")
    target = tmp_path / "missing-protocol" / RUN_ID
    target.mkdir(parents=True)
    write_json(target / "sdk-run.json", missing_protocol)
    with pytest.raises(es.ExternalSummaryError):
        ss.build_summary(
            tmp_path / "missing-retrieval",
            target,
            DATASET_DIR,
        )


def test_local_summary_rejects_external_layout_and_contradictions(tmp_path):
    run_dir = tmp_path / "local"
    run_dir.mkdir()
    cases = [
        (
            {
                "run_id": "local",
                "scoring_mode": "external",
                "protocol_version": EXTERNAL_PROTOCOL_VERSION,
            },
            "SDK 目录布局",
        ),
        (
            {
                "run_id": "local",
                "scoring_mode": "ragas",
                "protocol_version": EXTERNAL_PROTOCOL_VERSION,
            },
            "不一致",
        ),
        (
            {
                "run_id": "local",
                "scoring_mode": "unknown",
            },
            "不受支持",
        ),
    ]
    for document, fragment in cases:
        write_json(run_dir / "run.json", document)
        write_json(run_dir / "plan.json", {})
        with pytest.raises(sm.SummaryError) as error:
            sm.load_run(run_dir)
        assert fragment in str(error.value)


def test_ragas_protocol_keeps_original_summary_schema(tmp_path):
    scenario_root = _write_scenario_run(
        tmp_path / "ragas",
        {
            "s-action-verification-01": {
                "cp1": {
                    "faithfulness": ("succeeded", 0.5),
                    "factual_correctness": ("succeeded", 0.5),
                }
            }
        },
    )
    retrieval_root = _retrieval_run(tmp_path / "ragas")
    summary = ss.build_summary(
        retrieval_root, scenario_root, DATASET_DIR
    )
    assert summary["schema"] == sm.SUMMARY_SCHEMA
    assert summary["generation"]["metrics"]["faithfulness"][
        "succeeded_checkpoints"
    ] == 1
    report = "\n".join(ss.report_lines(summary))
    assert "SDK 评测汇总" in report
    assert "faithfulness" in report


def _file_hashes(paths: list[Path]) -> dict[str, str]:
    return {
        str(path): sha256_text(path.read_text(encoding="utf-8"))
        for path in paths
        if path.is_file()
    }


def test_cli_external_paths_are_offline_and_read_only(
    tmp_path, monkeypatch, capsys
):
    prepared = _prepare(tmp_path)
    _persist(prepared, _complete_items(prepared))
    retrieval_root = _retrieval_run(
        tmp_path / "cli", purpose="validation", groups=("bm25",)
    )
    source_files = [
        prepared.run_dir / "sdk-run.json",
        *sorted(
            (prepared.run_dir / "scenarios").glob(
                "**/execution.json"
            )
        ),
        *sorted(
            (prepared.run_dir / "scenarios").glob(
                "**/external-scoring.json"
            )
        ),
        *sorted(
            (prepared.run_dir / "external-judge").glob("*.json")
        ),
        *sorted(
            (
                prepared.run_dir
                / "external-judge"
                / "decision-feedback"
            ).glob("**/*.json")
        ),
    ]
    before = _file_hashes(source_files)
    monkeypatch.setenv("LANGSMITH_API_KEY", "offline-test-key")

    def _no_client(*args, **kwargs):
        raise AssertionError("汇总路径不得创建 LangSmith Client")

    def _no_collect(*args, **kwargs):
        raise AssertionError("汇总路径不得执行云端回收")

    monkeypatch.setattr(df, "Client", _no_client)
    monkeypatch.setattr(df, "read_remote_feedback", _no_client)
    monkeypatch.setattr(df, "collect_feedback", _no_collect)
    arguments = [
        "--retrieval",
        str(retrieval_root),
        "--scenarios",
        str(prepared.run_dir),
        "--dataset",
        str(DATASET_DIR),
    ]
    code = ss.main(["check", *arguments])
    checked = json.loads(capsys.readouterr().out)
    assert code == 1
    assert checked["effective_release"] is False
    output = tmp_path / "out"
    code = ss.main(["report", *arguments, "--output", str(output)])
    reported = json.loads(capsys.readouterr().out)
    assert code == 0
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["schema"] == es.EXTERNAL_SUMMARY_SCHEMA
    assert "外部裁判汇总" in (output / "report.md").read_text(encoding="utf-8")
    assert reported["summary"] == str(output / "summary.json")
    v3 = tmp_path / "v3.md"
    code = ss.main(
        ["write-v3", *arguments, "--output", str(output), "--v3", str(v3)]
    )
    assert code == 0
    first = v3.read_text(encoding="utf-8")
    assert first.count(ss.START_MARKER) == 1
    assert first.count(ss.END_MARKER) == 1
    ss.main(
        ["write-v3", *arguments, "--output", str(output), "--v3", str(v3)]
    )
    second = v3.read_text(encoding="utf-8")
    assert second == first
    assert _file_hashes(source_files) == before


def test_archived_external_run_is_read_only(tmp_path):
    run_dir = (
        BACKEND
        / "temp"
        / "scenario-langsmith"
        / "runs"
        / "20261021T034031Z-b5fb95de"
    )
    if not (run_dir / "sdk-run.json").is_file():
        pytest.skip("缺少已归档的外部运行证据")
    files = [
        run_dir / "sdk-run.json",
        *sorted(
            (run_dir / "scenarios").glob("**/execution.json")
        ),
        *sorted(
            (run_dir / "scenarios").glob("**/external-scoring.json")
        ),
        *sorted((run_dir / "external-judge").glob("*.json")),
    ]
    before = _file_hashes(files)
    summary = _summary(run_dir)
    assert summary["coverage"]["expected_checkpoints"] == 15
    assert summary["coverage"]["checkpoint_classification"] == {
        "expected": 15,
        "uploadable": 8,
        "missing": 0,
        "not_applicable": 7,
        "pending_review": 0,
    }
    assert summary["feedback_status"] == "absent"
    assert summary["release"]["effective"] is False
    assert _file_hashes(files) == before
