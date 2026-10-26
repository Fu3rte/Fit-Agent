import json
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from langfuse.api import (
    Experiment,
    ExperimentItem,
    GetScoresV3Meta,
    GetScoresV3Response,
    ObservationLevel,
    ObservationsV2Meta,
    ObservationsV2Response,
    ObservationV2,
    ScoreSource,
    ScoreSubjectV3_Observation,
    ScoreSubjectV3_Trace,
    ScoreV3_Categorical,
    ScoreV3_Numeric,
)
from pydantic import ValidationError

from evaluation.contracts import (
    ExternalJudgeApplicability,
    ExternalJudgeLangfuseScoringAttempt,
)
from evaluation.dataset import load_for_evaluation
from evaluation.decision_feedback import (
    FEEDBACK_BINDING_SCHEMA,
    WEB_JUDGE_ENVIRONMENT,
    FeedbackBinding,
    FeedbackDiagnostic,
    _bounded_collect,
    _load_collection,
    _parse_args,
    _persist_batch,
    build_feedback_batch,
    check_feedback,
    collect_feedback,
    feedback_binding_path,
    feedback_index_path,
    group_score_feedback,
    load_feedback_context,
    main,
    normalize_feedback,
    question_digest,
    read_remote_feedback,
    rubric_digest,
    verify_receipt_runs,
)
from evaluation.generation_upload import (
    EXPECTED_VARIABLE_MAPPING,
    EXTERNAL_DATASET_RECEIPT_NAME,
    EXTERNAL_DATASET_RECEIPT_SCHEMA,
    EXTERNAL_EXPERIMENT_PENDING_NAME,
    EXTERNAL_EXPERIMENT_RECEIPT_NAME,
    EXTERNAL_EXPERIMENT_RECEIPT_SCHEMA,
    EXTERNAL_PAYLOAD_SCHEMA,
    UploadBlocked,
    _dataset_marker,
    _example_document,
    _experiment_metadata,
    _frozen_run_output,
    _verify_external_examples,
    _web_judge_configuration,
    check_external_projection,
    external_rule_filters,
    load_external_run,
    payload_digest,
)
from evaluation.scenario_evidence import canonical_json, sha256_text
from evaluation.scenarios import DEFAULT_VERSION_DIR
from test.test_external_judge_checkpoints import (
    FAILED_SCENARIO,
    PROFILE_SCENARIO,
    RUN_ID,
    VERIFY_STAMP,
    MemoryClient,
    MemoryExperiment,
    approval_entries,
    build_fixture_run,
    default_decisions,
    write_approval,
)

EXPERIMENT_ID = "11111111-1111-4111-8111-111111111111"
DATASET_ID = "22222222-2222-4222-8222-222222222222"
EVALUATOR_ID = "evaluator-task4-declared"
EVIDENCE_EVALUATOR_NAME = "fit-agent-evidence-judge"
REFERENCE_EVALUATOR_NAME = "fit-agent-reference-judge"
EVIDENCE_METRIC = "evidence_supported_v1"
REFERENCE_METRIC = "reference_requirements_met_v1"
REAL_CONTEXT_LIMIT_COMMENT = (
    'ValueError("The model\'s context limit was exceeded. Narrow the '
    'input/output variable mapping or select a smaller sample, then test '
    'again.")Traceback (most recent call last):\n\n\n  File '
    '"/code/.venv/lib/python3.14/site-packages/langchain_core/runnables/'
    'base.py", line 1105, in ainvoke\n\n\n'
    'ValueError: The model\'s context limit was exceeded.'
)


@pytest.fixture(scope="module")
def dataset():
    return load_for_evaluation(DEFAULT_VERSION_DIR)


@dataclass(frozen=True)
class PreparedRun:
    run_dir: Path
    payload: dict[str, Any] | None
    binding: dict[str, Any]
    context: Any
    runs: dict[str, str]
    row: dict[str, Any] | None

    @property
    def run_id(self) -> str:
        return RUN_ID


def write_json(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def checkpoint_decision(
    applicable: bool, reason: str | None = None
) -> list[ExternalJudgeApplicability]:
    return [
        ExternalJudgeApplicability(
            metric=EVIDENCE_METRIC, applicable=applicable, reason=reason
        ),
        ExternalJudgeApplicability(
            metric=REFERENCE_METRIC, applicable=applicable, reason=reason
        ),
    ]


def decisions_for(evidence, mode: str) -> dict:
    if mode == "all_not_applicable":
        return {
            checkpoint.key: checkpoint_decision(
                False, "本运行不进入外部评分范围"
            )
            for checkpoint in evidence.checkpoints
        }
    decisions = default_decisions(evidence)
    if mode == "complete":
        for checkpoint in evidence.checkpoints:
            if checkpoint.link.scenario_id == FAILED_SCENARIO:
                decisions[checkpoint.key] = checkpoint_decision(
                    False, "执行失败，未生成回答"
                )
        return decisions
    return decisions


def binding_document(
    evidence,
    payload: dict[str, Any] | None,
    applicability_sha256: str,
) -> dict[str, Any]:
    config = _web_judge_configuration(
        payload
        or {
            "dataset_name": (
                "fit-agent-external-judge-langfuse-v2-binding-fixture"
            )
        },
        DATASET_ID,
    )
    for entry, name in zip(
        config["evaluators"],
        (EVIDENCE_EVALUATOR_NAME, REFERENCE_EVALUATOR_NAME),
        strict=True,
    ):
        entry["evaluator_name"] = name
    experiment = None
    if payload is not None:
        experiment = {
            "experiment_id": EXPERIMENT_ID,
            "experiment_name": payload["experiment_name"],
            "dataset_id": DATASET_ID,
            "dataset_name": payload["dataset_name"],
            "payload_sha256": payload_digest(payload),
            "rows_sha256": payload["rows_sha256"],
            "projections_sha256": payload["projections_sha256"],
        }
    question = "回答中的事实陈述是否全部由证据上下文支持？"
    reference_question = "回答是否满足参考答案中的任务要求？"
    return {
        "schema_version": FEEDBACK_BINDING_SCHEMA,
        "run_id": evidence.run_id,
        "source_sha256": evidence.source_sha256,
        "applicability_sha256": applicability_sha256,
        "experiment": experiment,
        "evaluator": {
            "provider": "Langfuse",
            "evaluator": "fit-agent-external-judge",
            "evaluator_id": EVALUATOR_ID,
            "model_alias": "jev-latest",
            "config": config,
            "config_sha256": sha256_text(canonical_json(config)),
        },
        "state_mapping": EXPECTED_VARIABLE_MAPPING,
        "metrics": [
            {
                "metric": EVIDENCE_METRIC,
                "feedback_key": EVIDENCE_METRIC,
                "question_id": EVIDENCE_METRIC,
                "question": question,
                "question_sha256": question_digest(EVIDENCE_METRIC, question),
                "rubric_id": None,
                "rubric": "任一具体事实无依据或矛盾即为假。",
                "rubric_sha256": rubric_digest(
                    None, "任一具体事实无依据或矛盾即为假。"
                ),
            },
            {
                "metric": REFERENCE_METRIC,
                "feedback_key": REFERENCE_METRIC,
                "question_id": REFERENCE_METRIC,
                "question": reference_question,
                "question_sha256": question_digest(
                    REFERENCE_METRIC, reference_question
                ),
                "rubric_id": "requirements",
                "rubric": "必须内容全部出现且没有禁止声明。",
                "rubric_sha256": rubric_digest(
                    "requirements", "必须内容全部出现且没有禁止声明。"
                ),
            },
        ],
    }


def write_receipts(
    run_dir: Path, payload: dict[str, Any]
) -> dict[str, str]:
    web_judge = _web_judge_configuration(payload, DATASET_ID)
    dataset_receipt = {
        "schema": EXTERNAL_DATASET_RECEIPT_SCHEMA,
        "run_id": payload["run_id"],
        "dataset": payload["dataset"],
        "source_sha256": payload["source_sha256"],
        "payload_sha256": payload_digest(payload),
        "applicability_sha256": payload["applicability_sha256"],
        "rows_sha256": payload["rows_sha256"],
        "projections_sha256": payload["projections_sha256"],
        "dataset_name": payload["dataset_name"],
        "dataset_id": DATASET_ID,
        "dataset_url": None,
        "item_ids": [row["row_id"] for row in payload["rows"]],
        "example_count": len(payload["rows"]),
        "created": True,
        "web_judge": web_judge,
        "synced_at": "2026-10-21T00:00:00+00:00",
    }
    write_json(
        run_dir / "external-judge" / EXTERNAL_DATASET_RECEIPT_NAME,
        dataset_receipt,
    )
    runs = {row["row_id"]: str(uuid4()) for row in payload["rows"]}
    experiment_receipt = {
        **{
            key: dataset_receipt[key]
            for key in (
                "run_id",
                "dataset",
                "source_sha256",
                "payload_sha256",
                "applicability_sha256",
                "rows_sha256",
                "projections_sha256",
                "dataset_name",
                "dataset_id",
            )
        },
        "schema": EXTERNAL_EXPERIMENT_RECEIPT_SCHEMA,
        "dataset_url": None,
        "experiment_name": payload["experiment_name"],
        "experiment_id": EXPERIMENT_ID,
        "experiment_url": None,
        "item_ids": [row["row_id"] for row in payload["rows"]],
        "runs": runs,
        "projections": {
            row["row_id"]: {
                "checkpoint": row["upload"]["metadata"]["checkpoint"],
                "execution_sha256": row["upload"]["metadata"][
                    "execution_sha256"
                ],
                "projection_sha256": row["projection_sha256"],
            }
            for row in payload["rows"]
        },
        "coverage": payload["coverage"],
        "coverage_complete": True,
        "external_scoring_status": "pending_collection",
        "web_judge": web_judge,
        "submitted_at": "2026-10-21T00:00:00+00:00",
    }
    write_json(
        run_dir / "external-judge" / EXTERNAL_EXPERIMENT_RECEIPT_NAME,
        experiment_receipt,
    )
    return runs


def prepare_run(tmp_path: Path, dataset, mode: str = "complete") -> PreparedRun:
    source = tmp_path / "source"
    run_dir, _ = build_fixture_run(dataset, source)
    evidence = load_external_run(run_dir, dataset)
    decisions = decisions_for(evidence, mode)
    write_approval(run_dir, approval_entries(evidence, decisions), dataset)
    if mode == "all_not_applicable":
        from evaluation.generation_upload import (
            _external_applicability_sha256,
            load_external_applicability,
        )

        applicability = load_external_applicability(evidence)
        binding = binding_document(
            evidence,
            None,
            _external_applicability_sha256(evidence, applicability),
        )
        write_json(feedback_binding_path(run_dir), binding)
        context = load_feedback_context(run_dir, dataset_dir=DEFAULT_VERSION_DIR)
        return PreparedRun(
            run_dir=run_dir,
            payload=None,
            binding=binding,
            context=context,
            runs={},
            row=None,
        )
    check_external_projection(run_dir, dataset_dir=DEFAULT_VERSION_DIR)
    payload = read_json(
        run_dir / "external-judge" / "checkpoints-payload.json"
    )
    runs = write_receipts(run_dir, payload)
    binding = binding_document(
        evidence, payload, payload["applicability_sha256"]
    )
    write_json(feedback_binding_path(run_dir), binding)
    context = load_feedback_context(run_dir, dataset_dir=DEFAULT_VERSION_DIR)
    return PreparedRun(
        run_dir=run_dir,
        payload=payload,
        binding=binding,
        context=context,
        runs=runs,
        row=payload["rows"][0],
    )


def make_score(
    trace_id: str,
    name: str,
    value: float,
    *,
    score_id: str | None = None,
    comment: str | None = "真实分数",
    config_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    source: ScoreSource = ScoreSource.EVAL,
    subject_kind: str = "observation",
) -> ScoreV3_Numeric:
    resolved = score_id or str(uuid4())
    subject = (
        ScoreSubjectV3_Trace(kind="trace", id=trace_id)
        if subject_kind == "trace"
        else ScoreSubjectV3_Observation(
            kind="observation", id=f"obs-{resolved[:8]}", trace_id=trace_id
        )
    )
    stamp = datetime(2026, 10, 21, tzinfo=UTC)
    return ScoreV3_Numeric(
        data_type="NUMERIC",
        value=value,
        id=resolved,
        project_id="project-1",
        name=name,
        source=source,
        timestamp=stamp,
        environment="default",
        created_at=stamp,
        updated_at=stamp,
        comment=comment,
        config_id=config_id,
        metadata=metadata,
        author_user_id=None,
        queue_id=None,
        subject=subject,
    )


def make_categorical_score(
    trace_id: str,
    name: str,
    value: str,
) -> ScoreV3_Categorical:
    stamp = datetime(2026, 10, 21, tzinfo=UTC)
    return ScoreV3_Categorical(
        data_type="CATEGORICAL",
        value=value,
        id=str(uuid4()),
        project_id="project-1",
        name=name,
        source=ScoreSource.EVAL,
        timestamp=stamp,
        environment="default",
        created_at=stamp,
        updated_at=stamp,
        comment="真实分数",
        config_id=None,
        metadata=None,
        author_user_id=None,
        queue_id=None,
        subject=ScoreSubjectV3_Observation(
            kind="observation", id=f"obs-{trace_id[:8]}", trace_id=trace_id
        ),
    )


def raw_score(
    trace_id: str,
    name: str,
    value: Any,
    *,
    score_id: str | None = None,
    comment: str | None = "真实分数",
    source: str = "EVAL",
    data_type: str = "NUMERIC",
    config_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    subject_kind: str = "observation",
) -> dict[str, Any]:
    stamp = "2026-10-21T00:00:00+00:00"
    subject = (
        {"kind": "trace", "id": trace_id}
        if subject_kind == "trace"
        else {
            "kind": "observation",
            "id": "obs-1",
            "traceId": trace_id,
        }
    )
    return {
        "id": score_id or str(uuid4()),
        "projectId": "project-1",
        "name": name,
        "source": source,
        "timestamp": stamp,
        "environment": "default",
        "createdAt": stamp,
        "updatedAt": stamp,
        "comment": comment,
        "configId": config_id,
        "metadata": metadata,
        "authorUserId": None,
        "queueId": None,
        "dataType": data_type,
        "value": value,
        "subject": subject,
    }


def judge_failure_observation(
    evaluator_name: str,
    trace_id: str,
    *,
    level: str = "ERROR",
    status_message: str | None = REAL_CONTEXT_LIMIT_COMMENT,
    name: str | None = None,
) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "traceId": trace_id,
        "startTime": "2026-10-21T00:00:00+00:00",
        "level": level,
        "statusMessage": status_message,
        "environment": WEB_JUDGE_ENVIRONMENT,
        "name": name or f"Execute evaluator: {evaluator_name}",
    }


class MemoryScoresApi:
    def __init__(self, client: "MemoryFeedbackClient") -> None:
        self._client = client

    def get_many(
        self,
        *,
        experiment_id=None,
        fields=None,
        limit=None,
        cursor=None,
        **kwargs,
    ):
        scores = self._client.scores_by_experiment.get(experiment_id, [])
        return GetScoresV3Response(
            data=list(scores),
            meta=GetScoresV3Meta(cursor=None, limit=len(scores)),
        )


class MemoryObservationsApi:
    def __init__(self, client: "MemoryFeedbackClient") -> None:
        self._client = client

    def get_many(
        self,
        *,
        environment=None,
        trace_id=None,
        limit=None,
        cursor=None,
        **kwargs,
    ):
        data = [
            observation
            for observation in self._client.observations_by_trace.get(
                trace_id, []
            )
            if observation.environment == environment
        ]
        return ObservationsV2Response(
            data=list(data), meta=ObservationsV2Meta(cursor=None)
        )


class MemoryFeedbackClient(MemoryClient):
    # 在 T3 内存 Client 上补 scores/observations 读 API 面。
    def __init__(self) -> None:
        super().__init__()
        self.scores_by_experiment: dict[str, list[Any]] = {}
        self.observations_by_trace: dict[str, list[Any]] = {}
        self.api.scores = MemoryScoresApi(self)
        self.api.observations = MemoryObservationsApi(self)


def normalize(prepared: PreparedRun, items: list[Any], judge_failures=None):
    assert prepared.row is not None
    row_id = prepared.row["row_id"]
    trace_id = prepared.runs[row_id]
    return normalize_feedback(
        prepared.context,
        {row_id: trace_id},
        {trace_id: items},
        judge_failures=(
            None if not judge_failures else {trace_id: judge_failures}
        ),
    )


def uploaded_record(records):
    return next(
        record
        for record in records
        if record.row_id is not None
    )


def complete_items(prepared: PreparedRun) -> list[ScoreV3_Numeric]:
    trace_id = prepared.runs[prepared.row["row_id"]]
    return [
        make_score(trace_id, EVIDENCE_METRIC, 0.82),
        make_score(trace_id, REFERENCE_METRIC, 0.38),
    ]


def persisted(prepared: PreparedRun, items: list[Any]):
    records = normalize(prepared, items)
    batch = build_feedback_batch(prepared.context, records)
    loaded = _load_collection(prepared.context)
    path, reused = _persist_batch(prepared.context, batch, loaded)
    return records, batch, path, reused


def test_two_feedback_zero_and_one_preserve_probability_and_unreported_versions(
    tmp_path, dataset
):
    prepared = prepare_run(tmp_path, dataset)
    trace_id = prepared.runs[prepared.row["row_id"]]
    items = [
        make_score(trace_id, EVIDENCE_METRIC, 0.0),
        make_score(trace_id, REFERENCE_METRIC, 1.0),
    ]
    records = normalize(prepared, items)
    record = uploaded_record(records)
    assert record.status == "complete"
    feedback = {row.metric: row for row in record.feedback_set.feedback}
    assert feedback[EVIDENCE_METRIC].status == "succeeded"
    assert feedback[EVIDENCE_METRIC].score == 0.0
    assert feedback[REFERENCE_METRIC].score == 1.0
    versions = {
        attempt.feedback.metric: attempt.model.model_version
        for attempt in record.attempts
    }
    # Langfuse Score 不返回裁判模型版本，别名之外一律按平台未返回记录。
    assert versions[EVIDENCE_METRIC].kind == "unreported"
    assert versions[REFERENCE_METRIC].kind == "unreported"
    assert all(
        attempt.model.model_alias == "jev-latest"
        for attempt in record.attempts
    )
    batch = build_feedback_batch(prepared.context, records)
    assert batch.status == "complete"
    assert batch.source_assurance == "declared_binding"


@pytest.mark.parametrize(
    "score",
    [True, "0.5", float("nan"), float("inf"), float("-inf"), 2.0, -0.1],
)
def test_invalid_scores_block_success(tmp_path, dataset, score):
    prepared = prepare_run(tmp_path, dataset)
    trace_id = prepared.runs[prepared.row["row_id"]]
    items = [
        raw_score(trace_id, EVIDENCE_METRIC, score),
        make_score(trace_id, REFERENCE_METRIC, 0.4),
    ]
    records = normalize(prepared, items)
    record = uploaded_record(records)
    assert record.status == "blocked"
    feedback = {row.metric: row for row in record.feedback_set.feedback}
    assert feedback[EVIDENCE_METRIC].status == "pending"
    assert feedback[EVIDENCE_METRIC].score is None
    assert len(record.attempts) == 1
    assert record.attempts[0].feedback.metric == REFERENCE_METRIC
    assert any(
        exclusion.reason == "invalid_score" for exclusion in record.exclusions
    )
    assert any(
        diagnostic.code == "invalid_score"
        for diagnostic in record.diagnostics
    )
    batch = build_feedback_batch(prepared.context, records)
    assert batch.status == "blocked"
    assert batch.blocking


def test_non_numeric_data_type_rejected_with_terminal_evidence(
    tmp_path, dataset
):
    prepared = prepare_run(tmp_path, dataset)
    trace_id = prepared.runs[prepared.row["row_id"]]
    categorical = make_categorical_score(trace_id, EVIDENCE_METRIC, "pass")
    items = [
        categorical,
        make_score(trace_id, REFERENCE_METRIC, 0.4),
    ]
    records = normalize(prepared, items)
    record = uploaded_record(records)
    assert record.status == "blocked"
    exclusion = next(
        exclusion
        for exclusion in record.exclusions
        if exclusion.reason == "invalid_score"
    )
    assert "CATEGORICAL" in exclusion.detail
    assert "pass" in exclusion.detail
    stored = next(
        item for item in record.evidence if item.score_id == categorical.id
    )
    assert stored.raw["dataType"] == "CATEGORICAL"
    assert stored.raw["value"] == "pass"


def test_missing_metric_pending_then_later_collect_completes(
    tmp_path, dataset
):
    prepared = prepare_run(tmp_path, dataset)
    trace_id = prepared.runs[prepared.row["row_id"]]
    evidence_score = make_score(trace_id, EVIDENCE_METRIC, 0.7)
    reference_score = make_score(trace_id, REFERENCE_METRIC, 0.6)
    records, first, first_path, first_reused = persisted(
        prepared, [evidence_score]
    )
    assert not first_reused
    assert uploaded_record(records).status == "pending"
    assert first.status == "pending"
    loaded = _load_collection(prepared.context)
    assert loaded is not None
    from evaluation.decision_feedback import _history_from_batches

    history = _history_from_batches(
        prepared.context, list(loaded.batches.values())
    )
    second_records = normalize_feedback(
        prepared.context,
        {
            prepared.row["row_id"]: trace_id,
        },
        {trace_id: [evidence_score, reference_score]},
        history,
    )
    second = build_feedback_batch(prepared.context, second_records)
    assert second.status == "complete"
    second_path, second_reused = _persist_batch(
        prepared.context, second, loaded
    )
    assert not second_reused
    assert second_path != first_path
    assert first_path.is_file()
    reloaded = _load_collection(prepared.context)
    assert reloaded is not None
    assert len(reloaded.index.batches) == 2
    first_attempt = uploaded_record(records).attempts[0].attempt_id
    second_attempt_ids = {
        attempt.attempt_id
        for attempt in uploaded_record(second_records).attempts
    }
    assert first_attempt in second_attempt_ids


def test_bounded_wait_stops_at_deadline_and_second_read_completes(
    tmp_path, dataset, monkeypatch
):
    prepared = prepare_run(tmp_path, dataset)
    trace_id = prepared.runs[prepared.row["row_id"]]
    evidence_score = make_score(trace_id, EVIDENCE_METRIC, 0.7)
    reference_score = make_score(trace_id, REFERENCE_METRIC, 0.6)
    mapping = {prepared.row["row_id"]: trace_id}

    def always_pending(context, timeout_ms):
        return mapping, {trace_id: [evidence_score]}, {}

    started = time.monotonic()
    batch, reads = _bounded_collect(
        prepared.context, 0.05, None, reader=always_pending
    )
    elapsed = time.monotonic() - started
    assert reads >= 1
    assert elapsed < 2.0
    assert batch.status == "pending"

    monkeypatch.setattr(
        "evaluation.decision_feedback.FEEDBACK_POLL_INTERVAL_SECONDS", 0.01
    )
    calls = {"count": 0}
    observed_timeouts: list[int] = []

    def eventually(context, timeout_ms):
        observed_timeouts.append(timeout_ms)
        calls["count"] += 1
        if calls["count"] == 1:
            return mapping, {trace_id: [evidence_score]}, {}
        return mapping, {trace_id: [evidence_score, reference_score]}, {}

    batch, reads = _bounded_collect(
        prepared.context, 3.0, None, reader=eventually
    )
    assert reads == 2
    assert batch.status == "complete"
    assert all(timeout > 0 for timeout in observed_timeouts)


def test_real_cloud_failure_is_failed_and_unattributed_stays_pending(
    tmp_path, dataset
):
    prepared = prepare_run(tmp_path, dataset)
    row_id = prepared.row["row_id"]
    trace_id = prepared.runs[row_id]
    failure = judge_failure_observation(EVIDENCE_EVALUATOR_NAME, trace_id)
    success = make_score(trace_id, REFERENCE_METRIC, 0.5)
    records = normalize(
        prepared, [success], judge_failures=[failure]
    )
    record = uploaded_record(records)
    assert record.status == "complete"
    feedback = {row.metric: row for row in record.feedback_set.feedback}
    assert feedback[EVIDENCE_METRIC].status == "failed"
    assert feedback[EVIDENCE_METRIC].score is None
    assert "context limit" in feedback[EVIDENCE_METRIC].failure_evidence
    assert WEB_JUDGE_ENVIRONMENT in feedback[EVIDENCE_METRIC].failure_evidence
    assert feedback[REFERENCE_METRIC].status == "succeeded"
    assert [attempt.feedback.metric for attempt in record.attempts] == [
        REFERENCE_METRIC
    ]
    assert any(
        diagnostic.code == "cloud_failure"
        for diagnostic in record.diagnostics
    )
    assert not any(
        diagnostic.code in ("invalid_score", "source_gap")
        for diagnostic in record.diagnostics
    )

    # 无法归属到声明 evaluator 的调试 observation 不得作为失败依据。
    foreign = judge_failure_observation(
        "other-judge", trace_id, name="Execute evaluator: other-judge"
    )
    records = normalize(
        prepared, [success], judge_failures=[foreign]
    )
    record = uploaded_record(records)
    assert record.status == "pending"
    feedback = {row.metric: row for row in record.feedback_set.feedback}
    assert feedback[EVIDENCE_METRIC].status == "pending"
    assert any(
        diagnostic.code == "awaiting_feedback"
        for diagnostic in record.diagnostics
    )


def test_remote_run_identity_must_match_receipt(tmp_path, dataset):
    prepared = prepare_run(tmp_path, dataset)
    row = prepared.row
    trace_id = prepared.runs[row["row_id"]]

    def run_object(**overrides):
        document = {
            "id": str(uuid4()),
            "trace_id": trace_id,
            "start_time": datetime.now(UTC),
            "end_time": None,
            "level": "DEFAULT",
            "environment": "default",
            "experiment_id": EXPERIMENT_ID,
            "experiment_name": row["upload"]["metadata"]["checkpoint"][
                "checkpoint_id"
            ],
            "experiment_item_id": row["row_id"],
            "input": deepcopy(row["dataset_input"]),
            "output": _frozen_run_output(row),
        }
        document.update(overrides)
        return ExperimentItem(**document)

    mapping = verify_receipt_runs(prepared.context, [run_object()])
    assert mapping == prepared.runs
    with pytest.raises(UploadBlocked, match="运行身份"):
        verify_receipt_runs(
            prepared.context, [run_object(trace_id=str(uuid4()))]
        )
    with pytest.raises(UploadBlocked, match="不属于回执实验"):
        verify_receipt_runs(
            prepared.context, [run_object(experiment_id="other-experiment")]
        )
    with pytest.raises(UploadBlocked, match="inputs"):
        verify_receipt_runs(
            prepared.context,
            [run_object(input={"tampered": True})],
        )
    with pytest.raises(UploadBlocked, match="outputs"):
        verify_receipt_runs(
            prepared.context,
            [run_object(output={"tampered": True})],
        )
    with pytest.raises(UploadBlocked, match="未知样例"):
        verify_receipt_runs(
            prepared.context, [run_object(experiment_item_id=str(uuid4()))]
        )
    with pytest.raises(UploadBlocked, match="多个实验条目"):
        verify_receipt_runs(
            prepared.context, [run_object(), run_object(trace_id=str(uuid4()))]
        )
    with pytest.raises(UploadBlocked, match="远端运行失败"):
        verify_receipt_runs(prepared.context, [run_object(level="ERROR")])


def test_example_reference_and_applicability_are_verified(tmp_path, dataset):
    prepared = prepare_run(tmp_path, dataset)
    row = prepared.row
    example = SimpleNamespace(
        id=row["row_id"],
        input=deepcopy(row["dataset_input"]),
        expected_output=deepcopy(row["upload"]["reference_outputs"]),
        metadata=_example_document(prepared.payload, row)["metadata"],
    )
    _verify_external_examples(
        prepared.payload, {str(example.id): example}
    )
    tampered = SimpleNamespace(
        id=example.id,
        input=example.input,
        expected_output={
            **example.expected_output,
            "applicability": [
                {"metric": EVIDENCE_METRIC, "applicable": False, "reason": "x"},
                {"metric": REFERENCE_METRIC, "applicable": False, "reason": "x"},
            ],
        },
        metadata=example.metadata,
    )
    with pytest.raises(UploadBlocked, match="expected_output"):
        _verify_external_examples(
            prepared.payload, {str(tampered.id): tampered}
        )
    assert example.metadata["schema"] == EXTERNAL_PAYLOAD_SCHEMA
    assert (
        example.metadata[f"judge_metric_{EVIDENCE_METRIC}"] == "applicable"
    )


def test_api_and_annotation_sources_do_not_satisfy_metrics(
    tmp_path, dataset
):
    prepared = prepare_run(tmp_path, dataset)
    trace_id = prepared.runs[prepared.row["row_id"]]
    valid = make_score(trace_id, REFERENCE_METRIC, 0.4)
    for source in ("API", "ANNOTATION"):
        items = [
            raw_score(
                trace_id,
                EVIDENCE_METRIC,
                0.9,
                source=source,
            ),
            valid,
        ]
        records = normalize(prepared, items)
        record = uploaded_record(records)
        assert record.status == "blocked"
        feedback = {row.metric: row for row in record.feedback_set.feedback}
        assert feedback[EVIDENCE_METRIC].status == "pending"
        assert any(
            exclusion.reason == "source_not_decision_model"
            for exclusion in record.exclusions
        )
        assert any(
            diagnostic.code == "source_gap"
            for diagnostic in record.diagnostics
        )
        assert all(
            attempt.feedback.metric == REFERENCE_METRIC
            for attempt in record.attempts
        )


def test_duplicate_same_id_dedup_and_content_drift_conflict(
    tmp_path, dataset
):
    prepared = prepare_run(tmp_path, dataset)
    trace_id = prepared.runs[prepared.row["row_id"]]
    score_id = str(uuid4())
    reference = make_score(trace_id, REFERENCE_METRIC, 0.4)
    same = raw_score(
        trace_id,
        EVIDENCE_METRIC,
        0.8,
        score_id=score_id,
    )
    records = normalize(prepared, [same, deepcopy(same), reference])
    record = uploaded_record(records)
    assert record.status == "complete"
    evidence = [
        item
        for item in record.evidence
        if item.score_id == score_id
    ]
    assert len(evidence) == 1
    assert len(
        [
            attempt
            for attempt in record.attempts
            if attempt.feedback.metric == EVIDENCE_METRIC
        ]
    ) == 1

    drift = deepcopy(same)
    drift["value"] = 0.2
    records = normalize(prepared, [same, drift, reference])
    record = uploaded_record(records)
    assert record.status == "blocked"
    assert any(
        diagnostic.code == "feedback_content_drift"
        for diagnostic in record.diagnostics
    )
    versions = [
        item
        for item in record.evidence
        if item.score_id == score_id
    ]
    assert len(versions) == 2
    assert {item.raw["value"] for item in versions} == {0.8, 0.2}
    assert not any(
        attempt.feedback.metric == EVIDENCE_METRIC
        for attempt in record.attempts
    )


def test_different_score_ids_for_same_metric_conflict(tmp_path, dataset):
    prepared = prepare_run(tmp_path, dataset)
    trace_id = prepared.runs[prepared.row["row_id"]]
    first = raw_score(trace_id, EVIDENCE_METRIC, 0.8)
    second = raw_score(trace_id, EVIDENCE_METRIC, 0.2)
    reference = make_score(trace_id, REFERENCE_METRIC, 0.4)
    records = normalize(prepared, [first, second, reference])
    record = uploaded_record(records)
    assert record.status == "blocked"
    feedback = {row.metric: row for row in record.feedback_set.feedback}
    assert feedback[EVIDENCE_METRIC].status == "pending"
    assert len(
        [
            attempt
            for attempt in record.attempts
            if attempt.feedback.metric == EVIDENCE_METRIC
        ]
    ) == 2
    assert any(
        diagnostic.code == "feedback_conflict"
        for diagnostic in record.diagnostics
    )


def test_history_conflict_and_drift_preserve_old_batches(tmp_path, dataset):
    prepared = prepare_run(tmp_path, dataset)
    trace_id = prepared.runs[prepared.row["row_id"]]
    evidence_score = make_score(trace_id, EVIDENCE_METRIC, 0.8)
    reference_score = make_score(trace_id, REFERENCE_METRIC, 0.4)
    _, first, first_path, _ = persisted(
        prepared, [evidence_score, reference_score]
    )
    assert first.status == "complete"
    loaded = _load_collection(prepared.context)
    from evaluation.decision_feedback import _history_from_batches

    history = _history_from_batches(
        prepared.context, list(loaded.batches.values())
    )
    missing_reference = normalize_feedback(
        prepared.context,
        {prepared.row["row_id"]: trace_id},
        {trace_id: [evidence_score]},
        history,
    )
    second = build_feedback_batch(prepared.context, missing_reference)
    assert second.status == "blocked"
    assert any(
        diagnostic.code == "feedback_history_conflict"
        for record in second.checkpoints
        for diagnostic in record.diagnostics
    )
    second_path, _ = _persist_batch(prepared.context, second, loaded)

    loaded = _load_collection(prepared.context)
    history = _history_from_batches(
        prepared.context, list(loaded.batches.values())
    )
    drifted = raw_score(
        trace_id,
        EVIDENCE_METRIC,
        0.1,
        score_id=str(evidence_score.id),
    )
    third_records = normalize_feedback(
        prepared.context,
        {prepared.row["row_id"]: trace_id},
        {trace_id: [drifted, reference_score]},
        history,
    )
    third = build_feedback_batch(prepared.context, third_records)
    assert third.status == "blocked"
    assert any(
        diagnostic.code == "feedback_content_drift"
        for record in third.checkpoints
        for diagnostic in record.diagnostics
    )
    third_path, _ = _persist_batch(prepared.context, third, loaded)
    assert first_path.is_file()
    assert second_path.is_file()
    assert third_path.is_file()
    assert len({first_path, second_path, third_path}) == 3


def test_missing_upload_and_not_applicable_keep_denominator(tmp_path, dataset):
    prepared = prepare_run(tmp_path, dataset, mode="missing")
    records = normalize(prepared, complete_items(prepared))
    by_checkpoint = {
        (record.checkpoint.scenario_id, record.checkpoint.checkpoint_id): record
        for record in records
    }
    assert by_checkpoint[(PROFILE_SCENARIO, "cp1")].status == "complete"
    failed = by_checkpoint[(FAILED_SCENARIO, "cp1")]
    assert failed.status == "pending"
    assert failed.attempts == []
    assert all(
        row.status == "pending" for row in failed.feedback_set.feedback
    )
    assert any(
        diagnostic.code == "checkpoint_missing_upload"
        for diagnostic in failed.diagnostics
    )
    not_applicable = [
        record
        for record in records
        if all(
            row.status == "not_applicable"
            for row in record.feedback_set.feedback
        )
    ]
    assert not_applicable
    for record in not_applicable:
        assert record.attempts == []
    batch = build_feedback_batch(prepared.context, records)
    assert batch.status == "pending"
    assert batch.source.coverage.missing == 1
    assert batch.source.coverage.expected == 3


def test_unexpected_feedback_for_not_applicable_metric_is_audited(
    tmp_path, dataset
):
    source = tmp_path / "source"
    run_dir, _ = build_fixture_run(dataset, source)
    evidence = load_external_run(run_dir, dataset)
    decisions = default_decisions(evidence)
    for checkpoint in evidence.checkpoints:
        if checkpoint.link.scenario_id == PROFILE_SCENARIO and (
            checkpoint.link.checkpoint_id == "cp1"
        ):
            decisions[checkpoint.key] = [
                ExternalJudgeApplicability(
                    metric=EVIDENCE_METRIC,
                    applicable=False,
                    reason="证据指标在本次范围外",
                ),
                ExternalJudgeApplicability(
                    metric=REFERENCE_METRIC, applicable=True
                ),
            ]
        elif checkpoint.link.scenario_id == FAILED_SCENARIO:
            decisions[checkpoint.key] = checkpoint_decision(
                False, "执行失败，未生成回答"
            )
    write_approval(run_dir, approval_entries(evidence, decisions), dataset)
    check_external_projection(run_dir, dataset_dir=DEFAULT_VERSION_DIR)
    payload = read_json(
        run_dir / "external-judge" / "checkpoints-payload.json"
    )
    runs = write_receipts(run_dir, payload)
    binding = binding_document(
        evidence, payload, payload["applicability_sha256"]
    )
    write_json(feedback_binding_path(run_dir), binding)
    context = load_feedback_context(run_dir, dataset_dir=DEFAULT_VERSION_DIR)
    row = payload["rows"][0]
    trace_id = runs[row["row_id"]]
    items = [
        make_score(trace_id, EVIDENCE_METRIC, 0.9),
        make_score(trace_id, REFERENCE_METRIC, 0.6),
    ]
    records = normalize_feedback(
        context, {row["row_id"]: trace_id}, {trace_id: items}
    )
    record = uploaded_record(records)
    assert record.status == "complete"
    feedback = {row.metric: row for row in record.feedback_set.feedback}
    assert feedback[EVIDENCE_METRIC].status == "not_applicable"
    assert feedback[EVIDENCE_METRIC].score is None
    assert feedback[REFERENCE_METRIC].status == "succeeded"
    assert all(
        attempt.feedback.metric == REFERENCE_METRIC
        for attempt in record.attempts
    )
    assert any(
        exclusion.reason == "not_applicable_metric"
        for exclusion in record.exclusions
    )
    assert any(
        diagnostic.code == "unexpected_not_applicable_feedback"
        for diagnostic in record.diagnostics
    )


def test_all_not_applicable_collect_is_offline_and_complete(
    tmp_path, dataset, monkeypatch
):
    prepared = prepare_run(tmp_path, dataset, mode="all_not_applicable")
    execution_bytes = {
        path: path.read_bytes()
        for path in (prepared.run_dir / "scenarios").glob(
            "**/execution.json"
        )
    }
    scoring_bytes = {
        path: path.read_bytes()
        for path in (prepared.run_dir / "scenarios").glob(
            "**/external-scoring.json"
        )
    }

    def explode(*args, **kwargs):
        raise AssertionError("离线回收不得创建 Langfuse Client")

    monkeypatch.setattr(
        "evaluation.decision_feedback.Langfuse", explode
    )
    result = collect_feedback(prepared.run_dir)
    assert result["status"] == "complete"
    assert result["network"] == "not_called"
    assert result["reads"] == 0
    assert result["coverage"] == {
        "expected": 3,
        "uploadable": 0,
        "missing": 0,
        "not_applicable": 3,
        "pending_review": 0,
    }
    loaded = _load_collection(prepared.context)
    assert loaded is not None
    batch = loaded.batches[loaded.index.latest]
    assert batch.experiment is None
    assert all(
        record.status == "not_applicable" for record in batch.checkpoints
    )
    assert all(not record.attempts for record in batch.checkpoints)
    assert check_feedback(prepared.run_dir)["status"] == "complete"
    for path, content in execution_bytes.items():
        assert path.read_bytes() == content
    for path, content in scoring_bytes.items():
        assert path.read_bytes() == content
    assert not (
        prepared.run_dir / "external-judge" / EXTERNAL_EXPERIMENT_RECEIPT_NAME
    ).exists()


def test_read_remote_feedback_groups_scores_and_failures(
    tmp_path, dataset, monkeypatch
):
    prepared = prepare_run(tmp_path, dataset)
    payload = prepared.payload
    row = prepared.row
    trace_id = prepared.runs[row["row_id"]]
    client = MemoryFeedbackClient()
    client.create_dataset(
        name=payload["dataset_name"], metadata=_dataset_marker(payload)
    )
    client.datasets[payload["dataset_name"]].id = DATASET_ID
    for row_document in payload["rows"]:
        document = _example_document(payload, row_document)
        client.create_dataset_item(
            dataset_name=payload["dataset_name"],
            id=document["id"],
            input=document["input"],
            expected_output=document["expected_output"],
            metadata=document["metadata"],
        )
    items = [
        ExperimentItem(
            id=str(uuid4()),
            trace_id=prepared.runs[row_document["row_id"]],
            start_time=VERIFY_STAMP,
            end_time=VERIFY_STAMP,
            level="DEFAULT",
            environment="default",
            experiment_id=EXPERIMENT_ID,
            experiment_name=payload["experiment_name"],
            experiment_item_id=row_document["row_id"],
            input=row_document["dataset_input"],
            output=_frozen_run_output(row_document),
        )
        for row_document in payload["rows"]
    ]
    experiment = Experiment(
        id=EXPERIMENT_ID,
        name=payload["experiment_name"],
        description=None,
        start_time=VERIFY_STAMP,
        end_time=VERIFY_STAMP,
        item_count=len(items),
        dataset_id=DATASET_ID,
        metadata=_experiment_metadata(payload, DATASET_ID),
    )
    client.experiments[EXPERIMENT_ID] = MemoryExperiment(experiment, items)
    client.scores_by_experiment[EXPERIMENT_ID] = [
        make_score(trace_id, REFERENCE_METRIC, 0.55)
    ]
    client.observations_by_trace[trace_id] = [
        ObservationV2(
            id=str(uuid4()),
            trace_id=trace_id,
            start_time=VERIFY_STAMP,
            end_time=None,
            project_id="project-1",
            parent_observation_id=None,
            type="GENERATION",
            is_root_observation=True,
            level=ObservationLevel.ERROR,
            status_message=REAL_CONTEXT_LIMIT_COMMENT,
            environment=WEB_JUDGE_ENVIRONMENT,
            name=f"Execute evaluator: {EVIDENCE_EVALUATOR_NAME}",
        )
    ]
    monkeypatch.setattr(
        "evaluation.decision_feedback._feedback_client",
        lambda timeout_ms: client,
    )
    mapping, grouped, failures = read_remote_feedback(
        prepared.context, timeout_ms=30_000
    )
    assert mapping == prepared.runs
    assert list(grouped[trace_id])
    assert len(failures[trace_id]) == 1
    records = normalize_feedback(
        prepared.context,
        mapping,
        grouped,
        judge_failures=failures,
    )
    batch = build_feedback_batch(prepared.context, records)
    assert batch.status == "complete"
    record = uploaded_record(records)
    feedback = {row.metric: row for row in record.feedback_set.feedback}
    assert feedback[EVIDENCE_METRIC].status == "failed"
    assert feedback[REFERENCE_METRIC].status == "succeeded"
    # 越界 score：subject trace 不属于回执运行，归组阻断。
    client.scores_by_experiment[EXPERIMENT_ID] = [
        make_score(str(uuid4()), REFERENCE_METRIC, 0.55)
    ]
    with pytest.raises(UploadBlocked, match="回执运行之外"):
        read_remote_feedback(prepared.context, timeout_ms=30_000)


def test_check_is_offline_and_collect_requires_explicit_key(
    tmp_path, dataset, monkeypatch
):
    prepared = prepare_run(tmp_path, dataset)

    def explode(*args, **kwargs):
        raise AssertionError("check 不得创建 Langfuse Client")

    monkeypatch.setattr(
        "evaluation.decision_feedback.Langfuse", explode
    )
    result = check_feedback(prepared.run_dir)
    assert result["status"] == "pending"
    assert result["network"] == "not_called"
    assert result["reason"] == "collection_absent"
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.setattr(
        "evaluation.decision_feedback.load_dotenv",
        lambda *args, **kwargs: None,
    )
    with pytest.raises(UploadBlocked, match="LANGFUSE_PUBLIC_KEY"):
        collect_feedback(prepared.run_dir)


def test_raw_evidence_sanitized_and_bound(tmp_path, dataset):
    prepared = prepare_run(tmp_path, dataset)
    trace_id = prepared.runs[prepared.row["row_id"]]
    local_path = "D:\\private\\secret.txt"
    session_path = "sessions/abc/attachments/plan.md"
    items = [
        make_score(
            trace_id,
            EVIDENCE_METRIC,
            0.7,
            comment=f"路径 {local_path} 与 {session_path}",
        ),
        make_score(trace_id, REFERENCE_METRIC, 0.5),
    ]
    records = normalize(prepared, items)
    record = uploaded_record(records)
    stored = next(
        item for item in record.evidence if item.score_name == EVIDENCE_METRIC
    )
    assert local_path not in stored.raw["comment"]
    assert session_path not in stored.raw["comment"]
    assert "local:" in stored.raw["comment"]
    assert "attachment:" in stored.raw["comment"]
    assert stored.raw_sha256 == sha256_text(canonical_json(stored.raw))
    batch = build_feedback_batch(prepared.context, records)
    document = batch.model_dump(mode="json")
    uploaded_index = next(
        index for index, record in enumerate(records)
        if record.row_id is not None
    )
    document["checkpoints"][uploaded_index]["feedback_set"]["feedback"][0][
        "score"
    ] = 0.99
    with pytest.raises(ValidationError):
        type(batch).model_validate(document)
    with pytest.raises(UploadBlocked, match="凭据标记"):
        normalize(
            prepared,
            [
                make_score(
                    trace_id,
                    EVIDENCE_METRIC,
                    0.7,
                    comment="Authorization: Bearer hidden",
                )
            ],
        )


def test_batch_diagnostics_are_sanitized(tmp_path, dataset):
    prepared = prepare_run(tmp_path, dataset, mode="missing")
    records = normalize(prepared, complete_items(prepared))
    failed = next(
        record
        for record in records
        if record.checkpoint.scenario_id == FAILED_SCENARIO
    )
    diagnostics = list(failed.diagnostics) + [
        FeedbackDiagnostic(
            code="checkpoint_missing_upload",
            detail="执行错误路径 D:\\private\\execution.log",
        )
    ]
    replaced = failed.model_copy(update={"diagnostics": diagnostics})
    records = [
        replaced if record is failed else record for record in records
    ]
    batch = build_feedback_batch(prepared.context, records)
    details = [
        diagnostic.detail
        for record in batch.checkpoints
        for diagnostic in record.diagnostics
    ]
    assert any("local:" in detail for detail in details)
    assert all("D:\\private" not in detail for detail in details)


def test_persistence_idempotent_and_history_preserved(tmp_path, dataset):
    prepared = prepare_run(tmp_path, dataset)
    items = complete_items(prepared)
    records = normalize(prepared, items)
    batch = build_feedback_batch(prepared.context, records)
    path, reused = _persist_batch(prepared.context, batch, None)
    assert not reused
    batch_bytes = path.read_bytes()
    index_bytes = feedback_index_path(prepared.run_dir).read_bytes()
    loaded = _load_collection(prepared.context)
    same = build_feedback_batch(
        prepared.context, normalize(prepared, items)
    )
    same_path, reused = _persist_batch(prepared.context, same, loaded)
    assert reused
    assert same_path == path
    assert path.read_bytes() == batch_bytes
    assert feedback_index_path(prepared.run_dir).read_bytes() == index_bytes
    trace_id = prepared.runs[prepared.row["row_id"]]
    extra = make_score(trace_id, "unrelated_metric", 0.3)
    changed_records = normalize(
        prepared, complete_items(prepared) + [extra]
    )
    changed = build_feedback_batch(prepared.context, changed_records)
    changed_path, changed_reused = _persist_batch(
        prepared.context, changed, loaded
    )
    assert not changed_reused
    assert changed_path != path
    assert path.read_bytes() == batch_bytes
    reloaded = _load_collection(prepared.context)
    assert reloaded is not None
    assert len(reloaded.index.batches) == 2
    assert reloaded.index.latest == changed_path.stem


def test_corrupt_index_and_batch_are_rejected(tmp_path, dataset):
    prepared = prepare_run(tmp_path, dataset)
    records = normalize(prepared, complete_items(prepared))
    batch = build_feedback_batch(prepared.context, records)
    path, _ = _persist_batch(prepared.context, batch, None)
    index_path = feedback_index_path(prepared.run_dir)
    index_bytes = index_path.read_bytes()
    index_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(UploadBlocked, match="索引损坏"):
        check_feedback(prepared.run_dir)
    index_path.write_bytes(index_bytes)
    document = read_json(path)
    uploaded_index = next(
        index for index, record in enumerate(records)
        if record.row_id is not None
    )
    document["checkpoints"][uploaded_index]["feedback_set"]["feedback"][0][
        "score"
    ] = 0.11
    write_json(path, document)
    with pytest.raises(UploadBlocked, match="批次损坏"):
        check_feedback(prepared.run_dir)


def test_binding_rejects_old_metrics_state_and_secret(tmp_path, dataset):
    prepared = prepare_run(tmp_path, dataset)
    base = deepcopy(prepared.binding)
    old_metric = deepcopy(base)
    old_metric["metrics"][0]["metric"] = "faithfulness"
    old_metric["metrics"][0]["feedback_key"] = "faithfulness"
    with pytest.raises(ValidationError):
        FeedbackBinding.model_validate(old_metric)
    wrong_state = deepcopy(base)
    wrong_state["state_mapping"]["context"] = {
        "source": "output",
        "path": "context",
    }
    with pytest.raises(ValidationError):
        FeedbackBinding.model_validate(wrong_state)
    wrong_reference = deepcopy(base)
    wrong_reference["state_mapping"]["reference"] = {
        "source": "expected_output",
        "path": "reference_outputs.reference",
    }
    with pytest.raises(ValidationError):
        FeedbackBinding.model_validate(wrong_reference)
    secret = deepcopy(base)
    secret["evaluator"]["config"]["api_key"] = "hidden"
    secret["evaluator"]["config_sha256"] = sha256_text(
        canonical_json(secret["evaluator"]["config"])
    )
    with pytest.raises(ValidationError, match="禁止字段"):
        FeedbackBinding.model_validate(secret)
    wrong_question = deepcopy(base)
    wrong_question["metrics"][0]["question_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="question_sha256"):
        FeedbackBinding.model_validate(wrong_question)
    duplicate_key = deepcopy(base)
    duplicate_key["metrics"][1]["feedback_key"] = EVIDENCE_METRIC
    with pytest.raises(ValidationError):
        FeedbackBinding.model_validate(duplicate_key)
    wrong_filter = deepcopy(base)
    wrong_filter["evaluator"]["config"]["evaluators"][0]["rule_filters"] = (
        external_rule_filters(REFERENCE_METRIC, DATASET_ID)
    )
    wrong_filter["evaluator"]["config_sha256"] = sha256_text(
        canonical_json(wrong_filter["evaluator"]["config"])
    )
    with pytest.raises(ValidationError, match="规则过滤"):
        FeedbackBinding.model_validate(wrong_filter)
    wrong_mapping = deepcopy(base)
    wrong_mapping["evaluator"]["config"]["variable_mapping"]["reference"] = {
        "source": "output",
        "path": "reference",
    }
    wrong_mapping["evaluator"]["config_sha256"] = sha256_text(
        canonical_json(wrong_mapping["evaluator"]["config"])
    )
    with pytest.raises(ValidationError, match="变量映射"):
        FeedbackBinding.model_validate(wrong_mapping)
    missing_name = deepcopy(base)
    missing_name["evaluator"]["config"]["evaluators"][0].pop(
        "evaluator_name"
    )
    missing_name["evaluator"]["config_sha256"] = sha256_text(
        canonical_json(missing_name["evaluator"]["config"])
    )
    with pytest.raises(ValidationError, match="evaluator 名称"):
        FeedbackBinding.model_validate(missing_name)


def test_terminal_attempt_requires_cloud_score_identity(tmp_path, dataset):
    prepared = prepare_run(tmp_path, dataset)
    records = normalize(prepared, complete_items(prepared))
    attempt = uploaded_record(records).attempts[0]
    document = attempt.model_dump(mode="json")
    document["cloud_score"] = None
    with pytest.raises(ValidationError, match="云端实际返回身份"):
        ExternalJudgeLangfuseScoringAttempt.model_validate(document)


def test_malformed_feedback_without_identity_creates_no_attempt(
    tmp_path, dataset
):
    prepared = prepare_run(tmp_path, dataset)
    trace_id = prepared.runs[prepared.row["row_id"]]
    malformed = raw_score(trace_id, EVIDENCE_METRIC, 0.8)
    malformed.pop("id")
    records = normalize(
        prepared,
        [
            malformed,
            make_score(trace_id, REFERENCE_METRIC, 0.4),
        ],
    )
    record = uploaded_record(records)
    assert record.status == "blocked"
    assert not any(
        attempt.feedback.metric == EVIDENCE_METRIC
        for attempt in record.attempts
    )
    assert any(
        diagnostic.code == "malformed_cloud_feedback"
        for diagnostic in record.diagnostics
    )


@pytest.mark.parametrize(
    "mutate, message",
    [
        (
            lambda receipt: receipt.__setitem__("runs", {}),
            "运行映射",
        ),
        (
            lambda receipt: receipt["projections"][
                next(iter(receipt["projections"]))
            ].__setitem__("execution_sha256", "0" * 64),
            "逐行投影",
        ),
        (
            lambda receipt: receipt.__setitem__("dataset_id", "other"),
            "数据集 ID",
        ),
        (
            lambda receipt: receipt.__setitem__("item_ids", []),
            "样例身份",
        ),
        (
            lambda receipt: receipt.__setitem__("payload_sha256", "0" * 64),
            "回执",
        ),
        (
            lambda receipt: receipt.__setitem__(
                "external_scoring_status", "collected"
            ),
            "回收状态",
        ),
    ],
)
def test_experiment_receipt_drift_is_blocked(
    tmp_path, dataset, mutate, message
):
    prepared = prepare_run(tmp_path, dataset)
    path = (
        prepared.run_dir
        / "external-judge"
        / EXTERNAL_EXPERIMENT_RECEIPT_NAME
    )
    receipt = read_json(path)
    mutate(receipt)
    write_json(path, receipt)
    with pytest.raises(UploadBlocked, match=message):
        load_feedback_context(
            prepared.run_dir, dataset_dir=DEFAULT_VERSION_DIR
        )


@pytest.mark.parametrize(
    "field",
    [
        "source_sha256",
        "applicability_sha256",
        "payload_sha256",
        "rows_sha256",
        "projections_sha256",
    ],
)
def test_binding_digest_drift_is_blocked(tmp_path, dataset, field):
    prepared = prepare_run(tmp_path, dataset)
    binding = deepcopy(prepared.binding)
    if field == "source_sha256" or field == "applicability_sha256":
        binding[field] = "0" * 64
    else:
        binding["experiment"][field] = "0" * 64
    write_json(feedback_binding_path(prepared.run_dir), binding)
    with pytest.raises(UploadBlocked):
        load_feedback_context(
            prepared.run_dir, dataset_dir=DEFAULT_VERSION_DIR
        )


@pytest.mark.parametrize(
    "field, value, message",
    [
        ("experiment_id", "other-experiment", "实验身份"),
        ("dataset_id", "other-dataset", "数据集身份"),
    ],
)
def test_binding_identity_drift_is_blocked(
    tmp_path, dataset, field, value, message
):
    prepared = prepare_run(tmp_path, dataset)
    binding = deepcopy(prepared.binding)
    binding["experiment"][field] = value
    write_json(feedback_binding_path(prepared.run_dir), binding)
    with pytest.raises(UploadBlocked, match=message):
        load_feedback_context(
            prepared.run_dir, dataset_dir=DEFAULT_VERSION_DIR
        )


def test_missing_or_pending_experiment_receipt_blocks(tmp_path, dataset):
    prepared = prepare_run(tmp_path, dataset)
    path = (
        prepared.run_dir
        / "external-judge"
        / EXTERNAL_EXPERIMENT_RECEIPT_NAME
    )
    receipt = read_json(path)
    path.unlink()
    with pytest.raises(UploadBlocked, match="缺少已提交实验回执"):
        load_feedback_context(
            prepared.run_dir, dataset_dir=DEFAULT_VERSION_DIR
        )
    write_json(
        prepared.run_dir
        / "external-judge"
        / EXTERNAL_EXPERIMENT_PENDING_NAME,
        receipt,
    )
    with pytest.raises(UploadBlocked, match="提交未完成"):
        load_feedback_context(
            prepared.run_dir, dataset_dir=DEFAULT_VERSION_DIR
        )


def test_payload_projection_drift_is_blocked(tmp_path, dataset):
    prepared = prepare_run(tmp_path, dataset)
    path = prepared.run_dir / "external-judge" / "checkpoints-payload.json"
    payload = read_json(path)
    payload["rows"][0]["projection_sha256"] = "0" * 64
    write_json(path, payload)
    with pytest.raises(UploadBlocked, match="本地投影"):
        load_feedback_context(
            prepared.run_dir, dataset_dir=DEFAULT_VERSION_DIR
        )


def test_feedback_scope_rejects_foreign_subject():
    trace_id = str(uuid4())
    foreign = make_score(str(uuid4()), EVIDENCE_METRIC, 0.5)
    with pytest.raises(UploadBlocked, match="回执运行之外"):
        group_score_feedback([trace_id], [foreign])
    session_subject = raw_score(trace_id, EVIDENCE_METRIC, 0.5)
    session_subject["subject"] = {"kind": "session", "id": "session-1"}
    with pytest.raises(UploadBlocked, match="subject"):
        group_score_feedback([trace_id], [session_subject])
    allowed = make_score(trace_id, EVIDENCE_METRIC, 0.5)
    grouped = group_score_feedback([trace_id], [allowed])
    assert grouped[trace_id] == [allowed]


def test_wait_seconds_rejects_non_finite_and_negative():
    for value in ("-1", "nan", "inf", "-inf", "abc"):
        with pytest.raises(SystemExit):
            _parse_args(
                ["collect", "--source", "x", "--wait-seconds", value]
            )
    args = _parse_args(
        ["collect", "--source", "x", "--wait-seconds", "0"]
    )
    assert args.wait_seconds == 0.0


def test_cli_exit_codes(tmp_path, dataset, capsys):
    prepared = prepare_run(tmp_path, dataset)
    code = main(["check", "--source", str(prepared.run_dir)])
    output = json.loads(capsys.readouterr().out)
    assert code == 2
    assert output["status"] == "pending"
    all_na = prepare_run(tmp_path / "all-na", dataset, mode="all_not_applicable")
    code = main(["collect", "--source", str(all_na.run_dir)])
    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["status"] == "complete"
    blocked = prepare_run(tmp_path / "blocked", dataset)
    trace_id = blocked.runs[blocked.row["row_id"]]
    records = normalize(
        blocked,
        [
            raw_score(trace_id, EVIDENCE_METRIC, True),
            make_score(trace_id, REFERENCE_METRIC, 0.5),
        ],
    )
    batch = build_feedback_batch(blocked.context, records)
    _persist_batch(blocked.context, batch, None)
    code = main(["check", "--source", str(blocked.run_dir)])
    output = json.loads(capsys.readouterr().out)
    assert code == 3
    assert output["status"] == "blocked"
