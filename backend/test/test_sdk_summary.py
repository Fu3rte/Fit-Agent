import json
from pathlib import Path

import pytest

from evaluation import langsmith_retrieval as lr
from evaluation import langsmith_scenarios as ls
from evaluation import sdk_summary as ss
from evaluation.contracts import (
    AssertionOutcome,
    BusinessProjection,
    CheckpointResult,
    EvidenceLink,
    MetricApplicability,
    MetricOutcome,
    StateComparison,
)
from evaluation.dataset import BACKEND, load_for_evaluation
from evaluation.scenario_evidence import sha256_text
from evaluation.scenarios import (
    AssertionCheckpointArtifact,
    AssertionCheckpointEvidence,
    EventCapture,
    GenerationCheckpointArtifact,
    GenerationCheckpointEvidence,
    ScenarioArtifact,
    ScenarioExecution,
    _seal_execution,
    frozen_scenario_sha256,
    scenario_plan,
)
from evaluation.scenarios import (
    code_identity as scenario_code_identity,
)

DATASET = load_for_evaluation(BACKEND / "dataset" / "evaluation" / "rebuild-v1")
SCENARIOS = {scenario["scenario_id"]: scenario for scenario in DATASET.scenarios}
VALIDATION_SCENARIO_ROWS, _ = ls.select_validation_scenarios(DATASET)
VALIDATION_SCENARIOS = [
    scenario["scenario_id"] for scenario in VALIDATION_SCENARIO_ROWS
]
RUN_ID = "run-sdk-test"
STARTED_AT = "2026-01-01T00:00:00+00:00"
FINISHED_AT = "2026-01-01T00:01:00+00:00"


def _link(scenario_id: str, event_id: str, checkpoint_id: str) -> EvidenceLink:
    return EvidenceLink(
        run_id=RUN_ID,
        scenario_id=scenario_id,
        event_id=event_id,
        checkpoint_id=checkpoint_id,
    )


def _event_by_checkpoint(scenario: dict) -> dict[str, str]:
    mapping = {}
    for key in ("reference", "rubric", "assertions", "metric_applicability"):
        for row in scenario[key]:
            mapping.setdefault(row["checkpoint_id"], row["event_id"])
    return mapping


def _execution(scenario: dict, session_id: str = "session-1") -> ScenarioExecution:
    plan = scenario_plan(scenario)
    scenario_id = scenario["scenario_id"]
    events = [
        EventCapture(
            run_id=RUN_ID,
            scenario_id=scenario_id,
            event_id=event_id,
            event_type=plan.event_types[event_id],
            source_event_id=None,
            session_id=session_id,
            operation_id=None,
            request_entry_id=f"{event_id}-request",
            terminal_status="completed",
            terminal_event="message_end",
            answer_text="fixture answer",
            answer_message_id=f"{event_id}-message",
            before=BusinessProjection(),
            after=BusinessProjection(),
        )
        for event_id in plan.event_ids
    ]
    event_map = _event_by_checkpoint(scenario)
    assertions = []
    for checkpoint_id in plan.checkpoint_ids:
        definitions = [
            row
            for row in scenario["assertions"]
            if row["checkpoint_id"] == checkpoint_id
        ]
        if definitions:
            assertions.append(
                AssertionCheckpointEvidence(
                    checkpoint_id=checkpoint_id,
                    event_id=event_map[checkpoint_id],
                    assertions=definitions,
                    runner_error="fixture execution",
                )
            )
    generation = []
    for checkpoint_id in plan.generation_metrics:
        applicability = [
            MetricApplicability(
                metric=row["metric"],
                applicable=row["applicable"],
                reason=row["reason"],
            )
            for row in scenario["metric_applicability"]
            if row["checkpoint_id"] == checkpoint_id
            and row["metric"] in ("faithfulness", "factual_correctness")
        ]
        generation.append(
            GenerationCheckpointEvidence(
                checkpoint_id=checkpoint_id,
                event_id=event_map[checkpoint_id],
                applicability=applicability,
                runner_error=(
                    "fixture execution"
                    if any(row.applicable for row in applicability)
                    else None
                ),
            )
        )
    execution = ScenarioExecution(
        run_id=RUN_ID,
        scenario_id=scenario_id,
        dataset=ls.dataset_identity(DATASET),
        scenario_sha256=frozen_scenario_sha256(scenario),
        execution_sha256="0" * 64,
        status="completed",
        error=None,
        session_ids=[session_id],
        initial_state_sha256="0" * 64,
        initial_projection=BusinessProjection(),
        expected_event_ids=list(plan.event_ids),
        expected_checkpoint_ids=list(plan.checkpoint_ids),
        events=events,
        assertion_checkpoints=assertions,
        generation_checkpoints=generation,
        retrieval=[],
        proposal_bindings={},
        generation_metrics=plan.generation_metrics,
        cancel_evidence=[],
        context_gaps=[],
    )
    return _seal_execution(execution)


def _artifact(
    execution: ScenarioExecution,
    scenario: dict,
    outcomes: dict,
    assertions: list[AssertionCheckpointArtifact] | None = None,
) -> ScenarioArtifact:
    plan = scenario_plan(scenario)
    event_map = _event_by_checkpoint(scenario)
    generation = []
    for checkpoint_id, metrics in plan.generation_metrics.items():
        event_id = event_map[checkpoint_id]
        spec = outcomes.get(checkpoint_id, {})
        if spec == "incomplete":
            generation.append(
                GenerationCheckpointArtifact(
                    checkpoint_id=checkpoint_id,
                    event_id=event_id,
                    runner_error="fixture scoring",
                )
            )
            continue
        result_outcomes = []
        for metric in metrics:
            status, score = spec.get(metric, ("not_applicable", None))
            if status == "succeeded":
                result_outcomes.append(
                    MetricOutcome(metric=metric, status=status, score=score)
                )
            elif status == "failed":
                result_outcomes.append(
                    MetricOutcome(metric=metric, status=status, error="fixture failure")
                )
            elif status == "applicable":
                result_outcomes.append(MetricOutcome(metric=metric, status=status))
            else:
                result_outcomes.append(
                    MetricOutcome(
                        metric=metric, status="not_applicable", reason="fixture"
                    )
                )
        generation.append(
            GenerationCheckpointArtifact(
                checkpoint_id=checkpoint_id,
                event_id=event_id,
                result=CheckpointResult(
                    checkpoint=_link(
                        scenario["scenario_id"], event_id, checkpoint_id
                    ),
                    outcomes=result_outcomes,
                ),
            )
        )
    return ScenarioArtifact(
        run_id=execution.run_id,
        scenario_id=execution.scenario_id,
        purpose="validation",
        session_ids=list(execution.session_ids),
        events=list(execution.events),
        assertions=list(assertions or []),
        generation=generation,
        retrieval=[],
        proposal_bindings={},
        generation_metrics=dict(execution.generation_metrics),
        cancel_evidence=[],
        context_gaps=[],
    )


def _write_scenario_run(
    tmp_path: Path,
    outcomes_by_scenario: dict[str, dict],
    *,
    purpose: str = "validation",
    sample_scope_ids: list[str] | None = None,
    sample_status: str = "complete",
    missing_scoring: bool = False,
    assertions_by_scenario: dict[str, list[AssertionCheckpointArtifact]] | None = None,
) -> Path:
    root = tmp_path / "scenario-run"
    root.mkdir(parents=True)
    samples = []
    records = []
    for scenario_id, outcomes in outcomes_by_scenario.items():
        scenario = SCENARIOS[scenario_id]
        execution = _execution(scenario)
        artifact = _artifact(
            execution,
            scenario,
            outcomes,
            (assertions_by_scenario or {}).get(scenario_id),
        )
        example_id = ls.stable_example_id(ls.dataset_name(DATASET), scenario_id)
        scenario_root = root / "scenarios" / example_id / scenario_id
        scenario_root.mkdir(parents=True)
        execution_path = scenario_root / "execution.json"
        execution_path.write_text(
            json.dumps(execution.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        scoring_text = json.dumps(
            artifact.model_dump(mode="json"), ensure_ascii=False, indent=2
        )
        scoring_path = scenario_root / "scoring.json"
        if not missing_scoring:
            scoring_path.write_text(scoring_text, encoding="utf-8")
        scoring_sha = sha256_text(scoring_text)
        feedback = ls.scenario_feedback(execution, artifact)
        incomplete = ls.incomplete_items(artifact)
        evaluation = {
            "schema": ls.SCHEMA,
            "feedback_version": ls.FEEDBACK_VERSION,
            "run_id": RUN_ID,
            "example_id": example_id,
            "scenario_id": scenario_id,
            "execution_sha256": execution.execution_sha256,
            "execution_relative": f"scenarios/{example_id}/{scenario_id}/execution.json",
            "scoring_relative": f"scenarios/{example_id}/{scenario_id}/scoring.json",
            "scoring_sha256": scoring_sha,
            "reused": False,
            "scoring_binding": "initial",
            "execution_status": execution.status,
            "feedback": feedback,
            "checkpoints": ls.checkpoint_records(artifact),
            "incomplete": incomplete,
            "written_at": FINISHED_AT,
        }
        (scenario_root / "evaluation.json").write_text(
            json.dumps(evaluation, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (scenario_root / "scoring-state.json").write_text(
            json.dumps(
                {
                    "status": "completed",
                    "run_id": RUN_ID,
                    "scenario_id": scenario_id,
                    "execution_sha256": execution.execution_sha256,
                    "scoring_sha256": scoring_sha,
                    "finished_at": FINISHED_AT,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        sample = {
            "example_id": example_id,
            "scenario_id": scenario_id,
            "sdk_run_id": f"sdk-{scenario_id}",
            "status": sample_status,
            "execution_sha256": execution.execution_sha256,
            "execution_status": execution.status,
            "scoring_relative": f"scenarios/{example_id}/{scenario_id}/scoring.json",
            "scoring_sha256": scoring_sha,
            "feedback_version": ls.FEEDBACK_VERSION,
            "feedback": feedback,
            "expected_feedback": feedback,
            "incomplete": incomplete,
            "run_error": None,
            "run_start_time": STARTED_AT,
            "run_end_time": FINISHED_AT,
        }
        samples.append(sample)
        records.append(ls.scenario_record(root, sample))
    document = {
        "schema": ls.SCHEMA,
        "run_id": RUN_ID,
        "purpose": purpose,
        "feedback_version": ls.FEEDBACK_VERSION,
        "status": (
            "incomplete"
            if any(sample["incomplete"] for sample in samples)
            else "complete"
        ),
        "started_at": STARTED_AT,
        "finished_at": FINISHED_AT,
        "dataset": DATASET.identity(),
        "dataset_name": ls.dataset_name(DATASET),
        "dataset_id": "fixture-dataset",
        "example_ids": {
            scenario["scenario_id"]: ls.stable_example_id(
                ls.dataset_name(DATASET), scenario["scenario_id"]
            )
            for scenario in DATASET.scenarios
        },
        "code_identity": {
            key: scenario_code_identity()[key]
            for key in ("commit", "dirty", "lock_sha256", "source_sha256")
        },
        "generation": {
            "model": json.dumps(
                {
                    "api": "openai-completions",
                    "provider": "fixture",
                    "model": "fixture-model",
                }
            ),
            "prompt_version": "a" * 64,
        },
        "judge": {"model": "decision-model-preview", "endpoint_sha256": "b" * 64},
        "index": {
            "collection": "fixture-collection",
            "manifest_sha256": "c" * 64,
            "build_id": "fixture-build",
        },
        "max_concurrency": 6,
        "num_repetitions": 1,
        "sample_scope": {
            "scenario_ids": sample_scope_ids or list(VALIDATION_SCENARIOS),
            "count": len(sample_scope_ids or VALIDATION_SCENARIOS),
            "selection": [],
        },
        "samples": samples,
        "scenarios": records,
        "coverage": {
            "status": (
                "complete"
                if all(sample["status"] == "complete" for sample in samples)
                else "incomplete"
            ),
            "expected_runs": len(samples),
            "observed_runs": len(samples),
            "extra_example_ids": [],
        },
        "concurrency": {
            "calls": len(samples),
            "max_in_flight": min(len(samples), 6),
            "overlap_pairs": len(samples) * (len(samples) - 1) // 2,
            "intervals": [],
        },
        "issues": [],
        "experiment": {
            "id": "fixture-experiment",
            "name": "fixture",
            "url": "https://example.invalid",
        },
    }
    (root / "sdk-run.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return root


def _retrieval_run(
    tmp_path: Path,
    *,
    purpose: str = "validation",
    groups: tuple[str, ...] = ("bm25",),
    samples_by_group: dict[str, list[dict]] | None = None,
    dataset_identity: dict | None = None,
    code_identity: dict | None = None,
    sample_scope_ids: list[str] | None = None,
) -> Path:
    expected_rows = (
        lr.select_validation_queries(DATASET)
        if purpose == "validation"
        else lr.select_formal_queries(DATASET)
    )
    expected_ids = [row["query_id"] for row in expected_rows]
    groups_document = {}
    for group in groups:
        groups_document[group] = {
            "status": "incomplete",
            "purpose": purpose,
            "group": group,
            "parameters": lr.validated_parameters(),
            "index_collection": "fixture-collection",
            "index_manifest_sha256": "c" * 64,
            "index_build_id": "fixture-build",
            "vector_cache_identity_sha256": "d" * 64,
            "vector_cache_required": False,
            "samples": (samples_by_group or {}).get(group, []),
            "coverage": {
                "status": "incomplete",
                "expected_runs": len(expected_ids),
                "observed_runs": 0,
                "extra_example_ids": [],
            },
            "concurrency": {
                "calls": 0,
                "max_in_flight": 0,
                "overlap_pairs": 0,
                "intervals": [],
            },
            "milvus_rpc": {"calls": 0, "max_in_flight": 0, "serialized": True},
            "experiment_id": "fixture-experiment",
            "experiment_name": "fixture",
            "experiment_url": "https://example.invalid",
        }
    document = {
        "schema": lr.SCHEMA,
        "run_id": "retrieval-sdk-test",
        "purpose": purpose,
        "status": "incomplete",
        "started_at": STARTED_AT,
        "finished_at": FINISHED_AT,
        "dataset": dataset_identity or DATASET.identity(),
        "dataset_name": lr.dataset_name(DATASET),
        "dataset_id": "fixture-dataset",
        "example_ids": {
            query["query_id"]: lr.stable_example_id(
                lr.dataset_name(DATASET), query["query_id"]
            )
            for query in DATASET.queries
        },
        "parameters": lr.validated_parameters(),
        "code_identity": code_identity or lr.sdk_code_identity(),
        "max_concurrency": 6,
        "num_repetitions": 1,
        "sample_scope": {"query_ids": sample_scope_ids or expected_ids},
        "groups": groups_document,
    }
    root = tmp_path / "retrieval-run"
    root.mkdir(parents=True)
    (root / "run.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return root


def _build(tmp_path: Path, scenario_root: Path, retrieval_root: Path) -> dict:
    return ss.build_summary(
        retrieval_root,
        scenario_root,
        DATASET.version_dir,
    )


def _file_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256_text(path.read_text(encoding="utf-8"))
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_generation_aggregation_keeps_zero_failed_and_na(tmp_path):
    scenario_root = _write_scenario_run(
        tmp_path / "positive",
        {
            "s-confirmation-cancellation-state-07": {
                "cp1": {
                    "faithfulness": ("succeeded", 0.0),
                    "factual_correctness": ("succeeded", 0.5),
                },
                "cp4": {
                    "faithfulness": ("failed", None),
                    "factual_correctness": ("succeeded", 0.5),
                },
            },
            "s-action-verification-01": {
                "cp1": {
                    "faithfulness": ("succeeded", 0.5),
                    "factual_correctness": ("succeeded", 0.5),
                }
            },
        },
    )
    retrieval_root = _retrieval_run(tmp_path / "positive")
    summary = _build(tmp_path / "positive", scenario_root, retrieval_root)
    faithfulness = summary["generation"]["metrics"]["faithfulness"]
    factual = summary["generation"]["metrics"]["factual_correctness"]
    assert faithfulness["succeeded_checkpoints"] == 2
    assert faithfulness["failed_checkpoints"] == 1
    assert faithfulness["not_applicable_checkpoints"] == 2
    assert faithfulness["available_scenarios"] == 2
    assert faithfulness["complete_scenarios"] == 1
    assert faithfulness["mean_including_partial"] == pytest.approx(0.25)
    assert faithfulness["mean_complete_only"] == pytest.approx(0.5)
    assert faithfulness["scenario_values"]["s-confirmation-cancellation-state-07"]["value"] == 0.0
    assert factual["failed_checkpoints"] == 0
    assert factual["available_scenarios"] == 2
    assert factual["complete_scenarios"] == 2
    assert factual["mean_including_partial"] == pytest.approx(0.5)
    assert factual["mean_complete_only"] == pytest.approx(0.5)


def test_retrieval_summary_denominators_and_false_return():
    rows = [
        {
            "query_id": "q1",
            "purpose": "lookup",
            "tags": ["a"],
            "family": "f1",
            "expect_empty": False,
            "scores": {metric: 1.0 for metric in ("recall@20", "hit@5", "mrr@5", "ndcg@5", "precision@5")},
        },
        {
            "query_id": "q2",
            "purpose": "lookup",
            "tags": ["a"],
            "family": "f2",
            "expect_empty": False,
            "scores": {metric: 0.0 for metric in ("recall@20", "hit@5", "mrr@5", "ndcg@5", "precision@5")},
        },
        {
            "query_id": "q3",
            "purpose": "lookup",
            "tags": ["empty"],
            "family": "f3",
            "expect_empty": True,
            "scores": {"false_return": 1.0},
        },
        {
            "query_id": "q4",
            "purpose": "lookup",
            "tags": ["empty"],
            "family": "f4",
            "expect_empty": True,
            "scores": {"false_return": 0.0},
        },
    ]
    stats = ss._summarize_retrieval_rows(rows)
    assert stats["with_labels"] == 2
    assert stats["empty_expectation_queries"] == 2
    assert stats["recall@20"] == pytest.approx(0.5)
    assert stats["hit@5"] == pytest.approx(0.5)
    assert stats["false_return_rate"] == pytest.approx(0.5)
    assert stats["empty_returned"] == 1


def test_missing_scoring_is_incomplete(tmp_path):
    scenario_root = _write_scenario_run(
        tmp_path,
        {"s-action-verification-01": {}},
        missing_scoring=True,
    )
    retrieval_root = _retrieval_run(tmp_path)
    summary = _build(tmp_path, scenario_root, retrieval_root)
    issues = summary["release"]["blocking_issues"]
    assert any("评分产物缺失" in issue for issue in issues)
    assert summary["coverage"]["failed_scenarios"]
    assert summary["evidence"]["scoring_artifacts"] == 0


def test_scenario_status_success_with_incomplete_is_blocked(tmp_path):
    scenario_root = _write_scenario_run(
        tmp_path,
        {"s-action-verification-01": {"cp1": "incomplete"}},
    )
    retrieval_root = _retrieval_run(tmp_path)
    summary = _build(tmp_path, scenario_root, retrieval_root)
    issues = summary["release"]["blocking_issues"]
    assert any("scenario_status 成功但评分不完整" in issue for issue in issues)
    assert any("评分不完整" in issue for issue in issues)


def test_wrong_scoring_digest_is_rejected(tmp_path):
    scenario_root = _write_scenario_run(
        tmp_path, {"s-action-verification-01": {}}
    )
    document = json.loads((scenario_root / "sdk-run.json").read_text(encoding="utf-8"))
    document["samples"][0]["scoring_sha256"] = "0" * 64
    (scenario_root / "sdk-run.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    retrieval_root = _retrieval_run(tmp_path)
    summary = _build(tmp_path, scenario_root, retrieval_root)
    assert any("scoring_sha256" in issue for issue in summary["release"]["blocking_issues"])


def test_scoring_path_traversal_is_rejected(tmp_path):
    scenario_root = _write_scenario_run(
        tmp_path, {"s-action-verification-01": {}}
    )
    document = json.loads((scenario_root / "sdk-run.json").read_text(encoding="utf-8"))
    document["samples"][0]["scoring_relative"] = "../outside-scoring.json"
    (scenario_root / "sdk-run.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    retrieval_root = _retrieval_run(tmp_path)
    summary = _build(tmp_path, scenario_root, retrieval_root)
    assert any("越出受控根目录" in issue for issue in summary["release"]["blocking_issues"])


def test_duplicate_scenario_sample_is_rejected(tmp_path):
    scenario_root = _write_scenario_run(
        tmp_path, {"s-action-verification-01": {}}
    )
    document = json.loads((scenario_root / "sdk-run.json").read_text(encoding="utf-8"))
    document["samples"].append(dict(document["samples"][0]))
    (scenario_root / "sdk-run.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    retrieval_root = _retrieval_run(tmp_path)
    summary = _build(tmp_path, scenario_root, retrieval_root)
    assert any("场景样本重复" in issue for issue in summary["release"]["blocking_issues"])


def test_same_count_wrong_sample_ids_is_rejected(tmp_path):
    scenario_root = _write_scenario_run(
        tmp_path, {"s-action-verification-01": {}}
    )
    document = json.loads((scenario_root / "sdk-run.json").read_text(encoding="utf-8"))
    document["samples"][0]["scenario_id"] = "s-unknown"
    (scenario_root / "sdk-run.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    retrieval_root = _retrieval_run(tmp_path)
    summary = _build(tmp_path, scenario_root, retrieval_root)
    issues = summary["release"]["blocking_issues"]
    assert any("场景样本缺失" in issue for issue in issues)
    assert any("场景样本包含未知 ID" in issue for issue in issues)


def test_unreviewed_candidates_block_retrieval_summary(tmp_path):
    scenario_root = _write_scenario_run(
        tmp_path, {"s-action-verification-01": {}}
    )
    retrieval_root = _retrieval_run(tmp_path)
    document = json.loads((retrieval_root / "run.json").read_text(encoding="utf-8"))
    query_id = "q-lookup-001"
    unreviewed = "0022"
    row = next(
        item
        for item in lr.example_rows(DATASET)
        if item["inputs"]["query_id"] == query_id
    )
    identity = lr.dataset_binding(DATASET)
    assert unreviewed not in DATASET.reviewed_corpus_ids()[query_id]
    payload = {
        "query_id": query_id,
        "dataset_content_sha256": identity["content_sha256"],
        "strategy": "bm25",
        "candidate_pool": [unreviewed],
        "final_model_input": [],
    }
    sample = {
        "query_id": query_id,
        "example_id": row["id"],
        "run_id": "run-blocked",
        "status": "blocked_unreviewed_candidates",
        "outputs": payload,
        "reference_outputs": row["outputs"],
        "unknown_candidates": [unreviewed],
        "error": f"{query_id} 存在未审阅候选: {unreviewed}",
    }
    group = document["groups"]["bm25"]
    group["status"] = "blocked_unreviewed_candidates"
    group["error"] = "存在未审阅候选，已保留候选与阻断证据"
    group["unknown_candidates"] = {query_id: [unreviewed]}
    group["samples"] = [sample]
    (retrieval_root / "run.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    summary = _build(tmp_path, scenario_root, retrieval_root)
    result = summary["retrieval"]["groups"]["bm25"]
    assert result["status"] == "blocked_unreviewed_candidates"
    assert result["metrics"] is None
    assert result["unknown_candidates"] == {query_id: [unreviewed]}
    assert summary["retrieval"]["overall_status"] == "incomplete"
    assert summary["retrieval"]["all_groups_complete"] is False
    assert summary["release"]["effective"] is False
    assert any(
        "未审阅候选" in issue for issue in summary["release"]["blocking_issues"]
    )


def test_bm25_only_is_not_all_groups_complete(tmp_path):
    scenario_root = _write_scenario_run(
        tmp_path, {"s-action-verification-01": {}}
    )
    retrieval_root = _retrieval_run(tmp_path, groups=("bm25",))
    summary = _build(tmp_path, scenario_root, retrieval_root)
    assert summary["retrieval"]["all_groups_complete"] is False
    assert set(summary["retrieval"]["groups"]) == {"bm25"}
    assert any("未覆盖全部批准组" in issue for issue in summary["release"]["blocking_issues"])
    assert summary["formal_scope"] is False


def test_validation_subset_cannot_become_formal(tmp_path):
    scenario_root = _write_scenario_run(
        tmp_path,
        {"s-action-verification-01": {}},
        purpose="formal",
        sample_scope_ids=list(VALIDATION_SCENARIOS),
    )
    retrieval_root = _retrieval_run(tmp_path, purpose="validation")
    summary = _build(tmp_path, scenario_root, retrieval_root)
    assert summary["formal_scope"] is False
    assert summary["release"]["effective"] is False
    assert any("正式全量范围" in issue for issue in summary["release"]["blocking_issues"])


def test_wrong_dataset_digest_is_rejected(tmp_path):
    scenario_root = _write_scenario_run(
        tmp_path, {"s-action-verification-01": {}}
    )
    identity = dict(DATASET.identity())
    identity["content_sha256"] = "0" * 64
    retrieval_root = _retrieval_run(tmp_path, dataset_identity=identity)
    summary = _build(tmp_path, scenario_root, retrieval_root)
    assert any("检索数据身份 不一致" in issue for issue in summary["release"]["blocking_issues"])


def test_retrieval_duplicate_query_is_rejected(tmp_path):
    scenario_root = _write_scenario_run(
        tmp_path, {"s-action-verification-01": {}}
    )
    sample = {"query_id": "q-lookup-001", "status": "failed", "error": "fixture"}
    retrieval_root = _retrieval_run(
        tmp_path,
        samples_by_group={"bm25": [sample, dict(sample)]},
    )
    summary = _build(tmp_path, scenario_root, retrieval_root)
    assert any("样本 query_id 重复" in issue for issue in summary["release"]["blocking_issues"])


def test_summary_does_not_modify_evidence(tmp_path):
    scenario_root = _write_scenario_run(
        tmp_path, {"s-action-verification-01": {}}
    )
    retrieval_root = _retrieval_run(tmp_path)
    before = _file_hashes(scenario_root)
    run_before = (retrieval_root / "run.json").read_bytes()
    _build(tmp_path, scenario_root, retrieval_root)
    assert _file_hashes(scenario_root) == before
    assert (retrieval_root / "run.json").read_bytes() == run_before


def test_v3_write_is_idempotent_and_preserves_content(tmp_path):
    scenario_root = _write_scenario_run(
        tmp_path, {"s-action-verification-01": {}}
    )
    retrieval_root = _retrieval_run(tmp_path)
    summary = _build(tmp_path, scenario_root, retrieval_root)
    path = tmp_path / "v3.md"
    path.write_text("# 用户已有内容\n\n保留说明。\n", encoding="utf-8")
    ss.write_v3(path, summary)
    first = path.read_text(encoding="utf-8")
    ss.write_v3(path, summary)
    second = path.read_text(encoding="utf-8")
    assert first == second
    assert first.count(ss.START_MARKER) == 1
    assert first.count(ss.END_MARKER) == 1
    assert first.startswith("# 用户已有内容")


def test_v3_write_failure_preserves_original(tmp_path):
    scenario_root = _write_scenario_run(
        tmp_path, {"s-action-verification-01": {}}
    )
    retrieval_root = _retrieval_run(tmp_path)
    summary = _build(tmp_path, scenario_root, retrieval_root)
    path = tmp_path / "v3.md"
    original = (
        f"# 用户内容\n\n{ss.START_MARKER}\n{ss.START_MARKER}\n"
    )
    path.write_text(original, encoding="utf-8")
    with pytest.raises(ss.SDKSummaryError):
        ss.write_v3(path, summary)
    assert path.read_text(encoding="utf-8") == original


def _failed_assertion(scenario_id: str) -> AssertionCheckpointArtifact:
    link = EvidenceLink(
        run_id=RUN_ID,
        scenario_id=scenario_id,
        event_id="e01",
        checkpoint_id="cp1",
    )
    return AssertionCheckpointArtifact(
        checkpoint_id="cp1",
        event_id="e01",
        outcomes=[
            AssertionOutcome(
                checkpoint=link,
                kind="state_unchanged",
                passed=True,
                evidence="fixture",
                comparison=StateComparison(
                    kind="state_unchanged",
                    business_kind="all",
                    checkpoint=link,
                    before=BusinessProjection(),
                    after=BusinessProjection(),
                ),
            ),
            AssertionOutcome(
                checkpoint=link,
                kind="evidence_present",
                passed=False,
                evidence="fixture failure",
            ),
        ],
    )


def _scenario_files(root: Path, scenario_id: str) -> Path:
    example_id = ls.stable_example_id(ls.dataset_name(DATASET), scenario_id)
    return root / "scenarios" / example_id / scenario_id


def test_failed_assertion_reaches_feedback_incomplete_and_gate(tmp_path):
    scenario_id = "s-action-verification-01"
    scenario_root = _write_scenario_run(
        tmp_path,
        {scenario_id: {}},
        assertions_by_scenario={
            scenario_id: [_failed_assertion(scenario_id)]
        },
    )
    retrieval_root = _retrieval_run(tmp_path)
    summary = _build(tmp_path, scenario_root, retrieval_root)
    document = json.loads(
        (scenario_root / "sdk-run.json").read_text(encoding="utf-8")
    )
    sample = document["samples"][0]
    assert document["feedback_version"] == ls.FEEDBACK_VERSION
    assert sample["feedback_version"] == ls.FEEDBACK_VERSION
    assert sample["feedback"]["scenario_status"] == 1.0
    assert sample["feedback"]["assertions_failed"] == 1.0
    assert any(
        "assertion_failed:evidence_present" in item
        for item in sample["incomplete"]
    )
    evaluation = json.loads(
        (_scenario_files(scenario_root, scenario_id) / "evaluation.json")
        .read_text(encoding="utf-8")
    )
    assert evaluation["feedback"]["assertions_failed"] == 1.0
    assert evaluation["incomplete"] == sample["incomplete"]
    issues = summary["release"]["blocking_issues"]
    assert any("断言失败 cp1/evidence_present" in issue for issue in issues)
    assert any("scenario_status 成功但评分不完整" in issue for issue in issues)
    assert summary["coverage"]["failed_scenarios"] == [scenario_id]
    assert summary["scenarios"][scenario_id]["assertions"]["failed"] == 1
    assert any(
        "assertion_failed:evidence_present" in item
        for item in summary["run"]["incomplete"]
    )


def _downgrade_to_legacy_feedback(root: Path, scenario_id: str) -> None:
    scenario_file = _scenario_files(root, scenario_id)
    execution = ls.load_execution(scenario_file / "execution.json")
    artifact = ScenarioArtifact.model_validate_json(
        (scenario_file / "scoring.json").read_text(encoding="utf-8")
    )
    legacy_feedback = ls.scenario_feedback(
        execution, artifact, version=ls.LEGACY_FEEDBACK_VERSION
    )
    legacy_incomplete = ls.incomplete_items(
        artifact, version=ls.LEGACY_FEEDBACK_VERSION
    )
    document = json.loads((root / "sdk-run.json").read_text(encoding="utf-8"))
    document.pop("feedback_version")
    for sample in document["samples"]:
        sample.pop("feedback_version", None)
        sample["feedback"] = legacy_feedback
        sample["incomplete"] = legacy_incomplete
    for record in document["scenarios"]:
        record.pop("feedback_version", None)
        record["feedback"] = legacy_feedback
        record["incomplete"] = legacy_incomplete
    (root / "sdk-run.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    evaluation_path = scenario_file / "evaluation.json"
    evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
    evaluation.pop("feedback_version")
    evaluation["feedback"] = legacy_feedback
    evaluation["incomplete"] = legacy_incomplete
    evaluation_path.write_text(
        json.dumps(evaluation, ensure_ascii=False), encoding="utf-8"
    )


def test_legacy_feedback_is_readable_and_failed_assertion_still_blocks(tmp_path):
    scenario_id = "s-action-verification-01"
    scenario_root = _write_scenario_run(
        tmp_path,
        {scenario_id: {}},
        assertions_by_scenario={
            scenario_id: [_failed_assertion(scenario_id)]
        },
    )
    _downgrade_to_legacy_feedback(scenario_root, scenario_id)
    source = ss.load_scenario_source(scenario_root, DATASET)
    assert source.feedback_version == ls.LEGACY_FEEDBACK_VERSION
    assert source.samples[scenario_id].feedback["scenario_status"] == 1.0
    assert "assertions_failed" not in source.samples[scenario_id].feedback
    assert any(
        "assertion_failed:evidence_present" in item
        for item in source.samples[scenario_id].incomplete
    )
    retrieval_root = _retrieval_run(tmp_path)
    summary = _build(tmp_path, scenario_root, retrieval_root)
    issues = summary["release"]["blocking_issues"]
    assert any("断言失败 cp1/evidence_present" in issue for issue in issues)
    assert not any("feedback 协议版本" in issue for issue in issues)
    assert summary["coverage"]["failed_scenarios"] == [scenario_id]


def test_unknown_feedback_version_is_rejected(tmp_path):
    scenario_root = _write_scenario_run(
        tmp_path, {"s-action-verification-01": {}}
    )
    document = json.loads(
        (scenario_root / "sdk-run.json").read_text(encoding="utf-8")
    )
    document["feedback_version"] = 99
    (scenario_root / "sdk-run.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    source = ss.load_scenario_source(scenario_root, DATASET)
    assert source.available is False
    assert any("feedback 协议版本不受支持" in issue for issue in source.issues)
    with pytest.raises(ls.ScenarioSDKError, match="版本"):
        ls.finalize_run(scenario_root)


def _write_document(root: Path, document: dict) -> None:
    (root / "sdk-run.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )


def test_run_declared_v2_rejects_legacy_child_records(tmp_path):
    scenario_id = "s-action-verification-01"
    scenario_root = _write_scenario_run(tmp_path, {scenario_id: {}})
    _downgrade_to_legacy_feedback(scenario_root, scenario_id)
    document = json.loads(
        (scenario_root / "sdk-run.json").read_text(encoding="utf-8")
    )
    document["feedback_version"] = ls.FEEDBACK_VERSION
    _write_document(scenario_root, document)
    source = ss.load_scenario_source(scenario_root, DATASET)
    assert source.feedback_version == ls.FEEDBACK_VERSION
    assert any(
        "sample feedback 协议版本与运行不一致" in issue
        for issue in source.issues
    )
    assert any(
        "evaluation feedback 协议版本与运行不一致" in issue
        for issue in source.issues
    )
    assert any(
        "场景记录反馈协议版本与运行不一致" in issue
        for issue in source.issues
    )
    retrieval_root = _retrieval_run(tmp_path)
    summary = _build(tmp_path, scenario_root, retrieval_root)
    assert any(
        "协议版本与运行不一致" in issue
        for issue in summary["release"]["blocking_issues"]
    )


def test_run_declared_v1_rejects_v2_sample(tmp_path):
    scenario_id = "s-action-verification-01"
    scenario_root = _write_scenario_run(tmp_path, {scenario_id: {}})
    _downgrade_to_legacy_feedback(scenario_root, scenario_id)
    document = json.loads(
        (scenario_root / "sdk-run.json").read_text(encoding="utf-8")
    )
    document["samples"][0]["feedback_version"] = ls.FEEDBACK_VERSION
    _write_document(scenario_root, document)
    source = ss.load_scenario_source(scenario_root, DATASET)
    assert source.feedback_version == ls.LEGACY_FEEDBACK_VERSION
    assert any(
        "sample feedback 协议版本与运行不一致" in issue
        for issue in source.issues
    )
    assert any(
        "feedback 协议版本与 evaluation 不一致" in issue
        for issue in source.issues
    )


def test_scenario_record_version_must_match_run(tmp_path):
    scenario_id = "s-action-verification-01"
    scenario_root = _write_scenario_run(tmp_path, {scenario_id: {}})
    document = json.loads(
        (scenario_root / "sdk-run.json").read_text(encoding="utf-8")
    )
    document["scenarios"][0]["feedback_version"] = ls.LEGACY_FEEDBACK_VERSION
    _write_document(scenario_root, document)
    source = ss.load_scenario_source(scenario_root, DATASET)
    assert any(
        "场景记录反馈协议版本与运行不一致" in issue
        for issue in source.issues
    )
