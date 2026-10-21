import json
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from langsmith import schemas
from pydantic import ValidationError

from evaluation.contracts import (
    ExternalJudgeApplicability,
    ExternalJudgeScoringAttempt,
)
from evaluation.dataset import load_for_evaluation
from evaluation.decision_feedback import (
    FEEDBACK_BINDING_SCHEMA,
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
    group_run_feedback,
    load_feedback_context,
    main,
    normalize_feedback,
    question_digest,
    rubric_digest,
    verify_receipt_runs,
)
from evaluation.generation_upload import (
    EXTERNAL_DATASET_RECEIPT_NAME,
    EXTERNAL_DATASET_RECEIPT_SCHEMA,
    EXTERNAL_EXPERIMENT_PENDING_NAME,
    EXTERNAL_EXPERIMENT_RECEIPT_NAME,
    EXTERNAL_EXPERIMENT_RECEIPT_SCHEMA,
    UploadBlocked,
    _verify_external_examples,
    check_external_projection,
    load_external_run,
    payload_digest,
)
from evaluation.scenario_evidence import canonical_json, sha256_text
from evaluation.scenarios import DEFAULT_VERSION_DIR
from test.test_external_judge_checkpoints import (
    FAILED_SCENARIO,
    PROFILE_SCENARIO,
    RUN_ID,
    approval_entries,
    build_fixture_run,
    default_decisions,
    write_approval,
)

EXPERIMENT_ID = "11111111-1111-4111-8111-111111111111"
DATASET_ID = "22222222-2222-4222-8222-222222222222"
EVALUATOR_ID = "evaluator-task4-declared"
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
    config = {
        "model": "jev-latest",
        "state": {
            "user_request": "inputs.user_request",
            "answer": "outputs.answer",
            "context": "outputs.context.items",
            "reference": "reference_outputs.reference",
        },
        "questions": [
            {
                "id": EVIDENCE_METRIC,
                "type": "noul",
                "instructions": "回答事实是否全部由证据支持？",
            },
            {
                "id": REFERENCE_METRIC,
                "type": "noul",
                "instructions": "回答是否满足参考要求？",
            },
        ],
    }
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
            "provider": "TypeSafe",
            "evaluator": "fit-agent-external-judge",
            "evaluator_id": EVALUATOR_ID,
            "model_alias": "jev-latest",
            "config": config,
            "config_sha256": sha256_text(canonical_json(config)),
        },
        "state_mapping": {
            "user_request": "inputs.user_request",
            "answer": "outputs.answer",
            "context": "outputs.context.items",
            "reference": "reference_outputs.reference",
        },
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
        "example_ids": [row["row_id"] for row in payload["rows"]],
        "example_count": len(payload["rows"]),
        "created": True,
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
        "example_ids": [row["row_id"] for row in payload["rows"]],
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


def make_feedback(
    run_id: str,
    key: str,
    score: Any,
    *,
    feedback_id: UUID | None = None,
    comment: str | None = "真实分数",
    extra: dict[str, Any] | None = None,
    source_type: str = "model",
    typesafe: dict[str, Any] | None = None,
    value: Any = None,
    correction: Any = None,
    session_id: str | None = EXPERIMENT_ID,
) -> schemas.Feedback:
    metadata: dict[str, Any] = {}
    if typesafe is not None:
        metadata["typesafe"] = typesafe
    return schemas.Feedback(
        id=feedback_id or uuid4(),
        created_at=datetime.now(UTC),
        modified_at=datetime.now(UTC),
        run_id=UUID(run_id),
        trace_id=UUID(run_id),
        session_id=UUID(session_id) if session_id is not None else None,
        key=key,
        score=score,
        value=value,
        correction=correction,
        comment=comment,
        extra=extra,
        feedback_source=schemas.FeedbackSourceBase(
            type=source_type, metadata=metadata or None
        ),
    )


def raw_feedback(
    run_id: str,
    key: str,
    score: Any,
    *,
    feedback_id: str | None = None,
    comment: str | None = "真实分数",
    extra: dict[str, Any] | None = None,
    source_type: str = "model",
    typesafe: dict[str, Any] | None = {"model": "jev-1.13.0"},
    value: Any = None,
) -> dict[str, Any]:
    document = {
        "id": feedback_id or str(uuid4()),
        "created_at": "2026-10-21T00:00:00+00:00",
        "modified_at": "2026-10-21T00:00:00+00:00",
        "run_id": run_id,
        "trace_id": run_id,
        "session_id": EXPERIMENT_ID,
        "key": key,
        "score": score,
        "value": value,
        "correction": None,
        "comment": comment,
        "extra": extra,
        "feedback_source": {
            "type": source_type,
            "metadata": (
                {} if typesafe is None else {"typesafe": typesafe}
            ),
        },
    }
    return document


def normalize(prepared: PreparedRun, items: list[Any]):
    assert prepared.row is not None
    row_id = prepared.row["row_id"]
    run_id = prepared.runs[row_id]
    return normalize_feedback(
        prepared.context,
        {row_id: run_id},
        {run_id: items},
    )


def uploaded_record(records):
    return next(
        record
        for record in records
        if record.row_id is not None
    )


def complete_items(prepared: PreparedRun) -> list[schemas.Feedback]:
    run_id = prepared.runs[prepared.row["row_id"]]
    return [
        make_feedback(
            run_id,
            EVIDENCE_METRIC,
            0.82,
            typesafe={"model": "jev-1.13.0"},
        ),
        make_feedback(
            run_id,
            REFERENCE_METRIC,
            0.38,
            typesafe={"model": "jev-1.13.0"},
        ),
    ]


def persisted(prepared: PreparedRun, items: list[Any]):
    records = normalize(prepared, items)
    batch = build_feedback_batch(prepared.context, records)
    loaded = _load_collection(prepared.context)
    path, reused = _persist_batch(prepared.context, batch, loaded)
    return records, batch, path, reused


def test_two_feedback_zero_and_one_preserve_probability_and_versions(
    tmp_path, dataset
):
    prepared = prepare_run(tmp_path, dataset)
    run_id = prepared.runs[prepared.row["row_id"]]
    items = [
        make_feedback(
            run_id,
            EVIDENCE_METRIC,
            0.0,
            typesafe={"model": "jev-1.13.0"},
        ),
        make_feedback(
            run_id, REFERENCE_METRIC, 1.0, typesafe={}
        ),
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
    assert versions[EVIDENCE_METRIC].kind == "reported"
    assert versions[EVIDENCE_METRIC].version == "jev-1.13.0"
    assert versions[REFERENCE_METRIC].kind == "unreported"
    batch = build_feedback_batch(prepared.context, records)
    assert batch.status == "complete"
    assert batch.source_assurance == "declared_binding"


@pytest.mark.parametrize(
    "score",
    [True, "0.5", float("nan"), float("inf"), float("-inf"), 2.0, -0.1],
)
def test_invalid_scores_block_success(tmp_path, dataset, score):
    prepared = prepare_run(tmp_path, dataset)
    run_id = prepared.runs[prepared.row["row_id"]]
    items = [
        raw_feedback(run_id, EVIDENCE_METRIC, score),
        make_feedback(
            run_id, REFERENCE_METRIC, 0.4, typesafe={"model": "jev-1.13.0"}
        ),
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


def test_missing_metric_pending_then_later_collect_completes(
    tmp_path, dataset
):
    prepared = prepare_run(tmp_path, dataset)
    run_id = prepared.runs[prepared.row["row_id"]]
    evidence_feedback = make_feedback(
        run_id, EVIDENCE_METRIC, 0.7, typesafe={"model": "jev-1.13.0"}
    )
    reference_feedback = make_feedback(
        run_id, REFERENCE_METRIC, 0.6, typesafe={"model": "jev-1.13.0"}
    )
    records, first, first_path, first_reused = persisted(
        prepared, [evidence_feedback]
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
            prepared.row["row_id"]: run_id,
        },
        {run_id: [evidence_feedback, reference_feedback]},
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
    run_id = prepared.runs[prepared.row["row_id"]]
    evidence_feedback = make_feedback(
        run_id, EVIDENCE_METRIC, 0.7, typesafe={"model": "jev-1.13.0"}
    )
    reference_feedback = make_feedback(
        run_id, REFERENCE_METRIC, 0.6, typesafe={"model": "jev-1.13.0"}
    )
    mapping = {prepared.row["row_id"]: run_id}

    def always_pending(context, timeout_ms):
        return mapping, {run_id: [evidence_feedback]}

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
            return mapping, {run_id: [evidence_feedback]}
        return mapping, {run_id: [evidence_feedback, reference_feedback]}

    batch, reads = _bounded_collect(
        prepared.context, 3.0, None, reader=eventually
    )
    assert reads == 2
    assert batch.status == "complete"
    assert all(timeout > 0 for timeout in observed_timeouts)


def test_real_cloud_failure_is_failed_and_local_invalid_score_is_blocked(
    tmp_path, dataset
):
    prepared = prepare_run(tmp_path, dataset)
    run_id = prepared.runs[prepared.row["row_id"]]
    failure = make_feedback(
        run_id,
        EVIDENCE_METRIC,
        None,
        comment=REAL_CONTEXT_LIMIT_COMMENT,
        extra={"error": True},
        typesafe={"model": "jev-1.13.0"},
    )
    success = make_feedback(
        run_id, REFERENCE_METRIC, 0.5, typesafe={"model": "jev-1.13.0"}
    )
    records = normalize(prepared, [failure, success])
    record = uploaded_record(records)
    assert record.status == "complete"
    feedback = {row.metric: row for row in record.feedback_set.feedback}
    assert feedback[EVIDENCE_METRIC].status == "failed"
    assert feedback[EVIDENCE_METRIC].score is None
    assert "context limit" in feedback[EVIDENCE_METRIC].failure_evidence
    assert feedback[REFERENCE_METRIC].status == "succeeded"
    assert {attempt.feedback.status for attempt in record.attempts} == {
        "failed",
        "succeeded",
    }
    assert any(
        diagnostic.code == "cloud_failure"
        for diagnostic in record.diagnostics
    )
    assert not any(
        diagnostic.code in ("invalid_score", "source_gap")
        for diagnostic in record.diagnostics
    )


def test_remote_run_identity_must_match_receipt(tmp_path, dataset):
    prepared = prepare_run(tmp_path, dataset)
    row = prepared.row
    run_id = prepared.runs[row["row_id"]]

    def run_object(**overrides):
        document = {
            "id": UUID(run_id),
            "name": row["upload"]["metadata"]["checkpoint"]["checkpoint_id"],
            "start_time": datetime.now(UTC),
            "run_type": "chain",
            "session_id": UUID(EXPERIMENT_ID),
            "reference_example_id": UUID(row["row_id"]),
            "inputs": deepcopy(row["upload"]["inputs"]),
            "outputs": deepcopy(row["upload"]["outputs"]),
            "error": None,
        }
        document.update(overrides)
        return schemas.Run(**document)

    mapping = verify_receipt_runs(prepared.context, [run_object()])
    assert mapping == prepared.runs
    with pytest.raises(UploadBlocked, match="运行身份"):
        verify_receipt_runs(prepared.context, [run_object(id=uuid4())])
    with pytest.raises(UploadBlocked, match="inputs"):
        verify_receipt_runs(
            prepared.context,
            [run_object(inputs={"tampered": True})],
        )
    with pytest.raises(UploadBlocked, match="outputs"):
        verify_receipt_runs(
            prepared.context,
            [run_object(outputs={"tampered": True})],
        )
    with pytest.raises(UploadBlocked, match="未知样例"):
        verify_receipt_runs(
            prepared.context, [run_object(reference_example_id=uuid4())]
        )
    with pytest.raises(UploadBlocked, match="多个根运行"):
        verify_receipt_runs(
            prepared.context, [run_object(), run_object(id=uuid4())]
        )
    with pytest.raises(UploadBlocked, match="不属于回执实验"):
        verify_receipt_runs(
            prepared.context, [run_object(session_id=uuid4())]
        )


def test_example_reference_and_applicability_are_verified(tmp_path, dataset):
    prepared = prepare_run(tmp_path, dataset)
    row = prepared.row
    example = schemas.Example(
        id=UUID(row["row_id"]),
        dataset_id=UUID(DATASET_ID),
        inputs=deepcopy(row["upload"]["inputs"]),
        outputs=deepcopy(row["upload"]["reference_outputs"]),
        metadata={
            "schema": "fit-agent-external-judge-upload-payload-1",
            "row_id": row["row_id"],
            "dataset": prepared.payload["dataset"],
            "checkpoint": row["upload"]["metadata"]["checkpoint"],
            "execution_sha256": row["upload"]["metadata"]["execution_sha256"],
            "projection_sha256": row["projection_sha256"],
        },
    )
    _verify_external_examples(
        prepared.payload, {str(example.id): example}
    )
    tampered = schemas.Example(
        id=example.id,
        dataset_id=example.dataset_id,
        inputs=example.inputs,
        outputs={
            **example.outputs,
            "applicability": [
                {"metric": EVIDENCE_METRIC, "applicable": False, "reason": "x"},
                {"metric": REFERENCE_METRIC, "applicable": False, "reason": "x"},
            ],
        },
        metadata=example.metadata,
    )
    with pytest.raises(UploadBlocked, match="outputs"):
        _verify_external_examples(
            prepared.payload, {str(tampered.id): tampered}
        )


def test_source_gap_and_annotation_feedback_do_not_satisfy_metrics(
    tmp_path, dataset
):
    prepared = prepare_run(tmp_path, dataset)
    run_id = prepared.runs[prepared.row["row_id"]]
    valid = make_feedback(
        run_id, REFERENCE_METRIC, 0.4, typesafe={"model": "jev-1.13.0"}
    )
    for source_type, typesafe in (
        ("api", {"model": "jev-1.13.0"}),
        ("model", None),
    ):
        items = [
            raw_feedback(
                run_id,
                EVIDENCE_METRIC,
                0.9,
                source_type=source_type,
                typesafe=typesafe,
            ),
            valid,
        ]
        records = normalize(prepared, items)
        record = uploaded_record(records)
        assert record.status == "blocked"
        feedback = {row.metric: row for row in record.feedback_set.feedback}
        assert feedback[EVIDENCE_METRIC].status == "pending"
        assert any(
            exclusion.reason
            in ("source_not_decision_model", "source_gap")
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
    run_id = prepared.runs[prepared.row["row_id"]]
    feedback_id = str(uuid4())
    reference = make_feedback(
        run_id, REFERENCE_METRIC, 0.4, typesafe={"model": "jev-1.13.0"}
    )
    same = raw_feedback(
        run_id,
        EVIDENCE_METRIC,
        0.8,
        feedback_id=feedback_id,
    )
    records = normalize(prepared, [same, deepcopy(same), reference])
    record = uploaded_record(records)
    assert record.status == "complete"
    evidence = [
        item
        for item in record.evidence
        if item.feedback_id == feedback_id
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
    drift["score"] = 0.2
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
        if item.feedback_id == feedback_id
    ]
    assert len(versions) == 2
    assert {item.raw["score"] for item in versions} == {0.8, 0.2}
    assert not any(
        attempt.feedback.metric == EVIDENCE_METRIC
        for attempt in record.attempts
    )


def test_different_feedback_ids_for_same_metric_conflict(tmp_path, dataset):
    prepared = prepare_run(tmp_path, dataset)
    run_id = prepared.runs[prepared.row["row_id"]]
    first = raw_feedback(run_id, EVIDENCE_METRIC, 0.8)
    second = raw_feedback(run_id, EVIDENCE_METRIC, 0.2)
    reference = make_feedback(
        run_id, REFERENCE_METRIC, 0.4, typesafe={"model": "jev-1.13.0"}
    )
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
    run_id = prepared.runs[prepared.row["row_id"]]
    evidence_feedback = make_feedback(
        run_id, EVIDENCE_METRIC, 0.8, typesafe={"model": "jev-1.13.0"}
    )
    reference_feedback = make_feedback(
        run_id, REFERENCE_METRIC, 0.4, typesafe={"model": "jev-1.13.0"}
    )
    _, first, first_path, _ = persisted(
        prepared, [evidence_feedback, reference_feedback]
    )
    assert first.status == "complete"
    loaded = _load_collection(prepared.context)
    from evaluation.decision_feedback import _history_from_batches

    history = _history_from_batches(
        prepared.context, list(loaded.batches.values())
    )
    missing_reference = normalize_feedback(
        prepared.context,
        {prepared.row["row_id"]: run_id},
        {run_id: [evidence_feedback]},
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
    drifted = raw_feedback(
        run_id,
        EVIDENCE_METRIC,
        0.1,
        feedback_id=str(evidence_feedback.id),
    )
    third_records = normalize_feedback(
        prepared.context,
        {prepared.row["row_id"]: run_id},
        {run_id: [drifted, reference_feedback]},
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
    run_id = runs[row["row_id"]]
    items = [
        make_feedback(
            run_id, EVIDENCE_METRIC, 0.9, typesafe={"model": "jev-1.13.0"}
        ),
        make_feedback(
            run_id, REFERENCE_METRIC, 0.6, typesafe={"model": "jev-1.13.0"}
        ),
    ]
    records = normalize_feedback(
        context, {row["row_id"]: run_id}, {run_id: items}
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
        raise AssertionError("离线回收不得创建 LangSmith Client")

    monkeypatch.setattr(
        "evaluation.decision_feedback.Client", explode
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


def test_check_is_offline_and_collect_requires_explicit_key(
    tmp_path, dataset, monkeypatch
):
    prepared = prepare_run(tmp_path, dataset)

    def explode(*args, **kwargs):
        raise AssertionError("check 不得创建 LangSmith Client")

    monkeypatch.setattr(
        "evaluation.decision_feedback.Client", explode
    )
    result = check_feedback(prepared.run_dir)
    assert result["status"] == "pending"
    assert result["network"] == "not_called"
    assert result["reason"] == "collection_absent"
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.setattr(
        "evaluation.decision_feedback.load_dotenv",
        lambda *args, **kwargs: None,
    )
    with pytest.raises(UploadBlocked, match="LANGSMITH_API_KEY"):
        collect_feedback(prepared.run_dir)


def test_raw_evidence_sanitized_and_bound(tmp_path, dataset):
    prepared = prepare_run(tmp_path, dataset)
    run_id = prepared.runs[prepared.row["row_id"]]
    local_path = "D:\\private\\secret.txt"
    session_path = "sessions/abc/attachments/plan.md"
    items = [
        make_feedback(
            run_id,
            EVIDENCE_METRIC,
            0.7,
            comment=f"路径 {local_path} 与 {session_path}",
            typesafe={"model": "jev-1.13.0"},
        ),
        make_feedback(
            run_id, REFERENCE_METRIC, 0.5, typesafe={"model": "jev-1.13.0"}
        ),
    ]
    records = normalize(prepared, items)
    record = uploaded_record(records)
    stored = next(
        item for item in record.evidence if item.key == EVIDENCE_METRIC
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
                make_feedback(
                    run_id,
                    EVIDENCE_METRIC,
                    0.7,
                    comment="Authorization: Bearer hidden",
                    typesafe={"model": "jev-1.13.0"},
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
    run_id = prepared.runs[prepared.row["row_id"]]
    extra = make_feedback(
        run_id, "unrelated_metric", 0.3, typesafe={"model": "jev-1.13.0"}
    )
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
    wrong_state["state_mapping"]["context"] = "outputs.context"
    with pytest.raises(ValidationError):
        FeedbackBinding.model_validate(wrong_state)
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


def test_terminal_attempt_requires_cloud_identity(tmp_path, dataset):
    prepared = prepare_run(tmp_path, dataset)
    records = normalize(prepared, complete_items(prepared))
    attempt = uploaded_record(records).attempts[0]
    document = attempt.model_dump(mode="json")
    document["cloud_feedback"] = None
    with pytest.raises(ValidationError, match="云端实际返回身份"):
        ExternalJudgeScoringAttempt.model_validate(document)


def test_malformed_feedback_without_identity_creates_no_attempt(
    tmp_path, dataset
):
    prepared = prepare_run(tmp_path, dataset)
    run_id = prepared.runs[prepared.row["row_id"]]
    malformed = raw_feedback(run_id, EVIDENCE_METRIC, 0.8)
    malformed.pop("id")
    records = normalize(
        prepared,
        [
            malformed,
            make_feedback(
                run_id,
                REFERENCE_METRIC,
                0.4,
                typesafe={"model": "jev-1.13.0"},
            ),
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


def test_feedback_scope_rejects_foreign_run_and_session():
    experiment_id = EXPERIMENT_ID
    run_id = str(uuid4())
    foreign = make_feedback(
        str(uuid4()), EVIDENCE_METRIC, 0.5
    )
    with pytest.raises(UploadBlocked, match="回执运行之外"):
        group_run_feedback(experiment_id, [run_id], [foreign])
    wrong_session = make_feedback(
        run_id, EVIDENCE_METRIC, 0.5, session_id=str(uuid4())
    )
    with pytest.raises(UploadBlocked, match="会话身份"):
        group_run_feedback(experiment_id, [run_id], [wrong_session])
    allowed = make_feedback(run_id, EVIDENCE_METRIC, 0.5)
    grouped = group_run_feedback(experiment_id, [run_id], [allowed])
    assert grouped[run_id] == [allowed]


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
    run_id = blocked.runs[blocked.row["row_id"]]
    records = normalize(
        blocked,
        [
            raw_feedback(run_id, EVIDENCE_METRIC, True),
            make_feedback(
                run_id,
                REFERENCE_METRIC,
                0.5,
                typesafe={"model": "jev-1.13.0"},
            ),
        ],
    )
    batch = build_feedback_batch(blocked.context, records)
    _persist_batch(blocked.context, batch, None)
    code = main(["check", "--source", str(blocked.run_dir)])
    output = json.loads(capsys.readouterr().out)
    assert code == 3
    assert output["status"] == "blocked"
