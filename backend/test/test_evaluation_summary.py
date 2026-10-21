import json
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from langsmith.schemas import Example, Feedback, Run

from evaluation.contracts import (
    AnswerEvidence,
    AssertionOutcome,
    BusinessProjection,
    CheckpointResult,
    ContextItem,
    EvidenceLink,
    GenerationCheckpoint,
    MetricApplicability,
    MetricOutcome,
    ModelInputContext,
    ProposalBinding,
    RerankEvidence,
    SavedBusinessData,
    SavedDataComparison,
    StateComparison,
    UserInputEvidence,
)
from evaluation.dataset import BACKEND, load_for_evaluation
from evaluation.generation_scoring import (
    FactualCorrectnessParameters,
    FaithfulnessParameters,
    GenerationMetricEvidence,
    PromptFingerprint,
    RagasRunConfigEvidence,
    ScoringAttemptSnapshot,
    run_judge_identity,
)
from evaluation.generation_upload import (
    RemoteExperiment,
    UploadBlocked,
    assert_upload_whitelist,
    build_upload_payload,
    check_payload,
    outbound_text,
    payload_expectation,
    pending_matches,
    receipt_matches,
    remote_snapshot,
    require_remote_matches,
    resolve_pending,
    source_digest,
)
from evaluation.retrieval import SCHEMA_LEGACY, SCHEMA_STAGED, publishable
from evaluation.scenario_evidence import (
    EvaluationCallRecorder,
    MessageNode,
    RequestBoundaryEvidence,
    ToolNode,
    canonical_json,
    sha256_text,
)
from evaluation.scenarios import (
    AssertionCheckpointArtifact,
    EventCapture,
    GenerationCheckpointArtifact,
    RunAggregate,
    RunSummary,
    ScenarioArtifact,
    ScenarioPlan,
    SearchStageRecord,
    _model_call,
    _write_json,
    accumulate_artifact,
    build_coverage,
    build_execution_plan,
    code_identity,
    executed_index_identity,
    generation_identity,
    generation_reference,
    judge_attempt_identity,
    scenario_plan,
)
from evaluation.summary import (
    REPORT_NAME,
    SUMMARY_NAME,
    _validate_context_projection,
    _validate_retrieval,
    build_summary,
    write_summary,
)

DATASET = load_for_evaluation(
    BACKEND / "dataset" / "evaluation" / "rebuild-v1"
)
MODEL = type("SummaryModel", (), {
    "api": "openai-completions",
    "provider": "summary-test",
    "model": "evaluation-model",
    "id": "evaluation-model",
    "base_url": "https://generation.invalid/v1",
})()
TEST_ROOT = BACKEND / "temp" / "summary-tests"
UNAVAILABLE_REASON = "纯状态检查点，没有模型生成的事实内容"
ATTACHMENT_REF = (
    "sessions/c24448a2-42b4-4b06-9e7c-4ce7927e5b9e/attachments/"
    "1d109ad8-2a5f-5799-bfc6-1d54f89c9960.md"
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def _link(run_id: str, scenario_id: str, event_id: str, checkpoint_id: str) -> EvidenceLink:
    return EvidenceLink(
        run_id=run_id,
        scenario_id=scenario_id,
        event_id=event_id,
        checkpoint_id=checkpoint_id,
    )


def _parameters(metric: str):
    if metric == "faithfulness":
        return FaithfulnessParameters(name="Faithfulness", max_retries=10)
    return FactualCorrectnessParameters(
        name="FactualCorrectness",
        mode="f1",
        beta=1.0,
        atomicity="low",
        coverage="low",
        language="english",
    )


def _evidence(
    checkpoint: GenerationCheckpoint,
    metric: str,
    score: float | None,
    error: str | None = None,
) -> GenerationMetricEvidence:
    contexts = (
        list(checkpoint.model_input.items)
        if metric == "faithfulness"
        else []
    )
    if metric == "faithfulness":
        scoring_input = {
            "response": checkpoint.answer.visible_text,
            "user_input": checkpoint.user_input.message,
            "retrieved_contexts": [item.content for item in contexts],
            "reference": None,
            "contexts_truncated": checkpoint.model_input.truncated,
            "contexts_omitted_chars": checkpoint.model_input.omitted_chars,
        }
    else:
        assert checkpoint.reference is not None
        scoring_input = {
            "response": checkpoint.answer.visible_text,
            "user_input": None,
            "retrieved_contexts": None,
            "reference": checkpoint.reference.answer,
            "contexts_truncated": False,
            "contexts_omitted_chars": None,
        }
    return GenerationMetricEvidence(
        checkpoint=checkpoint.checkpoint,
        metric=metric,
        metric_class="Faithfulness" if metric == "faithfulness" else "FactualCorrectness",
        metric_parameters=_parameters(metric),
        metric_version=sha256_text(f"metric-version:{metric}"),
        ragas_version="0.3.1",
        dependency_versions={"ragas": "0.3.1", "openai": "3.13.0"},
        judge_model="decision-model-preview",
        judge_base_url="https://judge.invalid/v1",
        judge_timeout_seconds=300.0,
        judge_sdk_max_retries=0,
        judge_temperature=0.0,
        ragas_run_config=RagasRunConfigEvidence(
            timeout_seconds=300.0,
            max_retries=10,
            max_wait_seconds=60.0,
            max_workers=16,
            exception_types=["RateLimitError"],
        ),
        prompt_fingerprints=[
            PromptFingerprint(
                name=metric,
                sha256=sha256_text(f"prompt-fingerprint:{metric}"),
                instruction="评分指令",
                language="english",
                input_model="string",
                output_model="string",
                example_count=0,
            )
        ],
        prompt_digest=sha256_text(f"prompt:{metric}"),
        scoring_input=scoring_input,
        contexts=contexts,
        model_input_truncated=(
            checkpoint.model_input.truncated
            if metric == "faithfulness"
            else False
        ),
        model_input_omitted_chars=(
            checkpoint.model_input.omitted_chars
            if metric == "faithfulness"
            else None
        ),
        score=score,
        error=error,
    )


def _attempt(evidence: GenerationMetricEvidence) -> ScoringAttemptSnapshot:
    data = evidence.model_dump(mode="json")
    data["status"] = "succeeded"
    return ScoringAttemptSnapshot.model_validate(data)


def _failed_attempt(checkpoint: GenerationCheckpoint, metric: str) -> ScoringAttemptSnapshot:
    template = _evidence(checkpoint, metric, score=None, error="TimeoutError: test")
    data = template.model_dump(mode="json")
    data["status"] = "failed"
    return ScoringAttemptSnapshot.model_validate(data)


def _checkpoint_model(
    run_id: str,
    scenario: dict,
    checkpoint_id: str,
    event_id: str,
    applicability: list[dict],
    attachment_ref: str | None = None,
) -> GenerationCheckpoint:
    frozen = (
        next(row for row in scenario["events"] if row["event_id"] == event_id)["content"]
        or ""
    ).strip()
    message = frozen or f"{scenario['scenario_id']}/{checkpoint_id} 当前输入"
    if attachment_ref is not None:
        # 与运行侧附件投影一致：读取引用作为用户正文的一部分进入真实请求。
        message = f"{message}\n\n[附件] 训练计划.md: {attachment_ref}"
    item = ContextItem(
        source="user_message",
        content=message,
        fact_origin="user",
        message_entry_id=(
            f"{scenario['scenario_id']}-{event_id}-{checkpoint_id}-input"
        ),
        message_role="user",
    )
    return GenerationCheckpoint(
        checkpoint=_link(run_id, scenario["scenario_id"], event_id, checkpoint_id),
        user_input=UserInputEvidence(
            message=message,
            prior_messages=[
                {"role": item["role"], "content": item["content"]}
                for item in scenario["input"]["prior_messages"]
            ],
        ),
        answer=AnswerEvidence(
            visible_text=f"{scenario['scenario_id']}/{checkpoint_id} 测试回答",
            structured_snapshot=None,
        ),
        model_input=ModelInputContext(items=[item]),
        reference=generation_reference(scenario, checkpoint_id),
        applicability=[
            MetricApplicability(
                metric=row["metric"],
                applicable=row["applicable"],
                reason=row["reason"],
            )
            for row in applicability
        ],
    )


def _proposal_binding(scenario_id: str, row: dict) -> ProposalBinding:
    reference = row["proposal_ref"]
    business_kind = reference.partition("-proposal-")[0]
    def value(name: str) -> str:
        return str(uuid5(NAMESPACE_URL, f"{scenario_id}:{reference}:{name}"))
    return ProposalBinding(
        proposal_ref=reference,
        business_kind=business_kind,
        proposal_id=value("proposal"),
        display_entry_id=value("display"),
        confirmation_entry_id=value("confirmation"),
        confirmation_operation_id=value("operation"),
    )


def _assertion_artifacts(
    scenario: dict,
    run_id: str,
    failed: set[tuple[str, str, str]],
) -> list[AssertionCheckpointArtifact]:
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in scenario["assertions"]:
        grouped.setdefault((row["event_id"], row["checkpoint_id"]), []).append(row)
    artifacts = []
    for (event_id, checkpoint_id), rows in grouped.items():
        link = _link(run_id, scenario["scenario_id"], event_id, checkpoint_id)
        outcomes = []
        for row in rows:
            proposal = None
            comparison = None
            if row["kind"] in {"snapshot_integrity", "save_authorization"}:
                proposal = _proposal_binding(scenario["scenario_id"], row)
            elif row["kind"] == "state_unchanged":
                comparison = StateComparison(
                    kind="state_unchanged",
                    business_kind=row["business_kind"],
                    checkpoint=link,
                    before=BusinessProjection(),
                    after=BusinessProjection(),
                )
            elif row["kind"] == "saved_data_unchanged":
                comparison = SavedDataComparison(
                    kind="saved_data_unchanged",
                    business_kind=row["business_kind"],
                    checkpoint=link,
                    before=SavedBusinessData(),
                    after=SavedBusinessData(),
                )
            outcomes.append(AssertionOutcome(
                checkpoint=link,
                kind=row["kind"],
                passed=(scenario["scenario_id"], checkpoint_id, row["kind"]) not in failed,
                evidence="测试断言证据",
                proposal=proposal,
                comparison=comparison,
            ))
        artifacts.append(AssertionCheckpointArtifact(
            checkpoint_id=checkpoint_id,
            event_id=event_id,
            outcomes=outcomes,
        ))
    return artifacts


def _capture_run_id(scenario: dict) -> str:
    return f"{scenario['scenario_id']}-run"


def _capture_session_id(scenario: dict) -> str:
    return f"{scenario['scenario_id']}-session"


def _request_for_items(items) -> dict:
    system = next(
        (item.content for item in items if item.source == "system_prompt"),
        None,
    )
    messages = []
    for item in items:
        if item.source == "system_prompt":
            continue
        if item.source == "assistant_message":
            messages.append({"role": "assistant", "content": item.content})
        elif item.source == "tool_result":
            messages.append({
                "role": "tool",
                "tool_call_id": item.tool_call_id,
                "content": item.content,
            })
        else:
            messages.append({"role": "user", "content": item.content})
    request = {"model": MODEL.id, "messages": messages}
    if system is not None:
        request["system"] = system
    return request


def _request_boundary(scenario: dict, checkpoint) -> RequestBoundaryEvidence:
    run_id = _capture_run_id(scenario)
    session_id = _capture_session_id(scenario)
    nodes = []
    tool_nodes = []
    for index, item in enumerate(checkpoint.model_input.items):
        if item.source == "system_prompt":
            continue
        if item.source == "tool_result":
            tool_nodes.append(ToolNode(
                tool_call_id=item.tool_call_id,
                tool_name=item.tool_name,
                content=item.content,
                entry_id=f"{scenario['scenario_id']}-tool-{index}",
                session_id=session_id,
                run_id=run_id,
                is_error=False,
            ))
            continue
        kind = "message"
        if item.source == "business_projection":
            kind = "business_context"
        elif item.source == "prior_message" and item.fact_origin == "system":
            kind = "compaction"
        nodes.append(MessageNode(
            entry_id=(
                item.message_entry_id
                or f"{scenario['scenario_id']}-node-{index}"
            ),
            session_id=session_id,
            role=item.message_role or "user",
            content=item.content,
            kind=kind,
            has_attachments=item.source == "attachment",
            run_id=(
                run_id
                if item.source in {"user_message", "attachment"}
                else "prior-run"
            ),
        ))
    return RequestBoundaryEvidence(
        api=MODEL.api,
        session_id=session_id,
        run_id=run_id,
        request_entry_id=None,
        nodes=nodes,
        tool_nodes=tool_nodes,
    )


def _event_captures(
    scenario: dict,
    plan,
    generation: list[GenerationCheckpointArtifact],
    scenario_root: Path,
) -> list[EventCapture]:
    checkpoint_by_event = {
        row.event_id: row.checkpoint for row in generation if row.checkpoint is not None
    }
    captures = []
    for event_id in plan.event_ids:
        checkpoint = checkpoint_by_event.get(event_id)
        answer_text = None
        answer_structured = None
        answer_message_id = None
        model_calls = []
        if checkpoint is not None:
            # 事件捕获的回答与真实请求全文由运行侧同一写入器产出。
            message_id = f"{scenario['scenario_id']}-{event_id}-answer"
            recorder = EvaluationCallRecorder(scenario_root / "model-calls")
            request = _request_for_items(checkpoint.model_input.items)
            sequence = recorder.record_request(request, MODEL, "normal", message_id, None)
            recorder.record_outcome(sequence, "stop", None)
            model_calls = [_model_call(recorder.records[-1])]
            answer_text = checkpoint.answer.visible_text
            answer_structured = checkpoint.answer.structured_snapshot
            answer_message_id = message_id
        captures.append(EventCapture(
            run_id=_capture_run_id(scenario),
            scenario_id=scenario["scenario_id"],
            event_id=event_id,
            event_type=plan.event_types[event_id],
            source_event_id=None,
            session_id=_capture_session_id(scenario),
            operation_id=None,
            request_entry_id=None,
            terminal_status="completed",
            terminal_event=None,
            answer_text=answer_text,
            answer_message_id=answer_message_id,
            answer_structured=answer_structured,
            before=BusinessProjection(),
            after=BusinessProjection(),
            model_calls=model_calls,
        ))
    return captures


def _search_record(scenario: dict, plan, run_id: str) -> SearchStageRecord:
    manifest = '{"build_id":"summary-build","compatibility":{}}'
    return SearchStageRecord(
        sequence=0,
        run_id=run_id,
        scenario_id=scenario["scenario_id"],
        event_id=plan.event_ids[0],
        tool_call_id=f"{scenario['scenario_id']}-tool",
        index_collection="summary-collection",
        index_manifest_sha256=sha256_text(manifest),
        index_manifest_json=manifest,
        arguments={"purpose": "lookup"},
        candidate_pool=[],
        final_model_input=[],
        response_json={"items": [], "has_more": False},
        scores=[],
        threshold=0.0,
    )


def _generation_artifacts(
    run_id: str,
    scenario: dict,
    plan,
    scores: dict[tuple[str, str, str], float],
    failed: set[tuple[str, str, str]],
    applicable: set[tuple[str, str, str]],
    omitted: set[tuple[str, str]],
    attachment_paths: set[str],
) -> tuple[list[GenerationCheckpointArtifact], list[GenerationMetricEvidence]]:
    artifacts = []
    evidence_rows = []
    event_by_checkpoint = {
        row["checkpoint_id"]: row["event_id"]
        for row in scenario["metric_applicability"]
        if row["metric"] in {"faithfulness", "factual_correctness"}
    }
    for checkpoint_id, metrics in plan.generation_metrics.items():
        if (scenario["scenario_id"], checkpoint_id) in omitted:
            continue
        event_id = event_by_checkpoint[checkpoint_id]
        rows = [
            row
            for row in scenario["metric_applicability"]
            if row["checkpoint_id"] == checkpoint_id
            and row["metric"] in {"faithfulness", "factual_correctness"}
        ]
        if not any(row["applicable"] for row in rows):
            result = CheckpointResult(
                checkpoint=_link(
                    run_id, scenario["scenario_id"], event_id, checkpoint_id
                ),
                outcomes=[
                    MetricOutcome(
                        metric=row["metric"],
                        status="not_applicable",
                        reason=row["reason"],
                    )
                    for row in rows
                ],
            )
            artifacts.append(GenerationCheckpointArtifact(
                checkpoint_id=checkpoint_id,
                event_id=event_id,
                checkpoint=None,
                result=result,
            ))
            continue
        checkpoint = _checkpoint_model(
            run_id,
            scenario,
            checkpoint_id,
            event_id,
            rows,
            ATTACHMENT_REF if scenario["scenario_id"] in attachment_paths else None,
        )
        boundary = _request_boundary(scenario, checkpoint)
        outcomes = []
        evidence = []
        attempts = []
        for row in rows:
            metric = row["metric"]
            key = (scenario["scenario_id"], checkpoint_id, metric)
            if not row["applicable"]:
                outcomes.append(MetricOutcome(
                    metric=metric,
                    status="not_applicable",
                    reason=row["reason"],
                ))
                continue
            if key in applicable:
                outcomes.append(MetricOutcome(metric=metric, status="applicable"))
                continue
            if key in failed:
                outcomes.append(MetricOutcome(
                    metric=metric,
                    status="failed",
                    error="TimeoutError: test",
                ))
                attempts.append(_failed_attempt(checkpoint, metric))
                continue
            score = scores.get(key, 0.8)
            item = _evidence(checkpoint, metric, score)
            outcomes.append(MetricOutcome(
                metric=metric,
                status="succeeded",
                score=score,
            ))
            evidence.append(item)
            attempts.append(_attempt(item))
            evidence_rows.append(item)
        artifacts.append(GenerationCheckpointArtifact(
            checkpoint_id=checkpoint_id,
            event_id=event_id,
            checkpoint=checkpoint,
            result=CheckpointResult(
                checkpoint=checkpoint.checkpoint,
                outcomes=outcomes,
            ),
            evidence=evidence,
            attempts=attempts,
            request_boundary=boundary,
        ))
    return artifacts, evidence_rows


# 离线构造运行产物，仅验证汇总与门禁；不代表真实评测结果。
def build_run(
    root: Path,
    *,
    scenario_ids: list[str] | None = None,
    scores: dict[tuple[str, str, str], float] | None = None,
    failed: set[tuple[str, str, str]] | None = None,
    applicable: set[tuple[str, str, str]] | None = None,
    omitted: set[tuple[str, str]] | None = None,
    assertion_failed: set[tuple[str, str, str]] | None = None,
    blocked: set[str] | None = None,
    attachment_paths: set[str] | None = None,
) -> Path:
    scores = scores or {}
    failed = failed or set()
    applicable = applicable or set()
    omitted = omitted or set()
    assertion_failed = assertion_failed or set()
    blocked = blocked or set()
    attachment_paths = attachment_paths or set()
    selected = (
        [scenario for scenario in DATASET.scenarios if scenario["scenario_id"] in scenario_ids]
        if scenario_ids is not None
        else list(DATASET.scenarios)
    )
    run_id = f"summary-{uuid.uuid4().hex[:12]}"
    plan = build_execution_plan(DATASET, selected, run_id)
    run_root = Path(root) / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    _write_json(run_root / "plan.json", plan.model_dump(mode="json"))
    artifacts = {}
    all_evidence = []
    all_attempt_models = set()
    retrieval_records = []
    for index, scenario in enumerate(selected):
        plan_item = next(
            item for item in plan.scenarios
            if item.scenario_id == scenario["scenario_id"]
        )
        scenario_root = run_root / scenario["scenario_id"]
        scenario_root.mkdir()
        generation, evidence = _generation_artifacts(
            run_id,
            scenario,
            plan_item,
            scores,
            failed,
            applicable,
            omitted,
            attachment_paths,
        )
        all_evidence.extend(evidence)
        all_attempt_models.update(
            item.judge_model
            for row in generation
            for item in row.attempts
        )
        search = [_search_record(scenario, plan_item, run_id)] if index == 0 else []
        retrieval_records.extend(search)
        artifact = ScenarioArtifact(
            run_id=run_id,
            scenario_id=scenario["scenario_id"],
            session_ids=[f"{scenario['scenario_id']}-session"],
            events=_event_captures(scenario, plan_item, generation, scenario_root),
            assertions=_assertion_artifacts(
                scenario, run_id, assertion_failed
            ),
            generation=generation,
            retrieval=search,
            proposal_bindings={},
            generation_metrics=plan_item.generation_metrics,
            cancel_evidence=[],
            context_gaps=[],
            blocked=(
                "冻结要求与当前生产协议无法同时满足"
                if scenario["scenario_id"] in blocked
                else None
            ),
        )
        artifacts[scenario["scenario_id"]] = artifact
        target = "partial.json" if scenario["scenario_id"] in blocked else "scenario.json"
        _write_json(scenario_root / target, artifact.model_dump(mode="json"))
        if scenario["scenario_id"] in blocked:
            _write_json(scenario_root / "blocked.json", {
                "scenario_id": scenario["scenario_id"],
                "reason": artifact.blocked,
            })
        _write_jsonl(
            scenario_root / "events.jsonl",
            [item.model_dump(mode="json") for item in artifact.events],
        )
        _write_jsonl(
            scenario_root / "assertions.jsonl",
            [item.model_dump(mode="json") for item in artifact.assertions],
        )
        _write_jsonl(
            scenario_root / "generation.jsonl",
            [item.model_dump(mode="json") for item in artifact.generation],
        )
        _write_jsonl(
            scenario_root / "retrieval.jsonl",
            [item.model_dump(mode="json") for item in artifact.retrieval],
        )
        _write_jsonl(
            scenario_root / "context.jsonl",
            [
                {
                    "event_id": item.event_id,
                    "tool_identity_map": item.tool_identity_map,
                    "context_gaps": [
                        gap.model_dump(mode="json") for gap in item.context_gaps
                    ],
                }
                for item in artifact.events
            ],
        )
    completed = [
        scenario_id for scenario_id in artifacts
        if scenario_id not in blocked
    ]
    aggregate = RunAggregate()
    for artifact in artifacts.values():
        accumulate_artifact(aggregate, artifact)
    coverage = build_coverage(plan, aggregate, completed)
    identity = {
        "run_id": run_id,
        "purpose": "validation",
        "dataset": DATASET.identity(),
        "code": code_identity(),
        "generation": generation_identity(MODEL),
    }
    if all_evidence:
        judge = run_judge_identity(all_evidence).model_dump(mode="json")
        identity["judge"] = judge
        generation_model = json.loads(identity["generation"]["model"])["model"]
        fields, missing = judge_attempt_identity(
            all_attempt_models, generation_model, judge["model"]
        )
        identity.update(fields)
        identity["missing"] = sorted(set(missing))
    else:
        identity["missing"] = ["judge"]
    retrieval = {
        "missing": [],
        "query_vector_identity": canonical_json({"model": "summary-test"}),
    }
    executed_index_identity(retrieval_records, retrieval)
    identity["retrieval"] = retrieval
    incomplete = sorted({
        item
        for artifact in artifacts.values()
        for item in _incomplete_items(artifact)
    })
    identity["incomplete"] = incomplete
    identity["status"] = "complete" if not incomplete and not blocked else "incomplete"
    summary = RunSummary(
        run_id=run_id,
        purpose="validation",
        dataset=DATASET.identity(),
        identity=identity,
        coverage=coverage,
        scenarios=completed,
        blocked=[f"{item}: blocked" for item in sorted(blocked)],
        failed=[],
        incomplete=incomplete,
        context_gaps=[],
        started_at="2026-10-18T00:00:00+00:00",
        finished_at="2026-10-18T00:05:00+00:00",
    )
    _write_json(run_root / "run.json", summary.model_dump(mode="json"))
    return run_root


def _incomplete_items(artifact: ScenarioArtifact) -> list[str]:
    from evaluation.scenarios import _incomplete_items as items

    return items(artifact)


@pytest.fixture(scope="module")
def formal_run():
    TEST_ROOT.mkdir(parents=True, exist_ok=True)
    root = build_run(TEST_ROOT, attachment_paths={"s-plan-import-file-01"})
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def run_copy(formal_run):
    TEST_ROOT.mkdir(parents=True, exist_ok=True)
    target = TEST_ROOT / f"copy-{uuid.uuid4().hex}"
    shutil.copytree(formal_run, target)
    try:
        yield target
    finally:
        shutil.rmtree(target, ignore_errors=True)


def _scenario_row(artifact: dict, checkpoint_id: str) -> dict:
    return next(
        row for row in artifact["generation"]
        if row["checkpoint_id"] == checkpoint_id
    )


def _first_succeeded_checkpoint(scenario_root: Path) -> tuple[str, str]:
    artifact = json.loads(
        (scenario_root / "scenario.json").read_text(encoding="utf-8")
    )
    for row in artifact["generation"]:
        for outcome in (row["result"] or {}).get("outcomes", []):
            if outcome["status"] == "succeeded":
                return row["checkpoint_id"], outcome["metric"]
    raise AssertionError("场景缺少成功生成指标")


def _edit_generation(scenario_root: Path, checkpoint_id: str, mutate) -> None:
    artifact_path = scenario_root / "scenario.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    mutate(_scenario_row(artifact, checkpoint_id))
    artifact_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    path = scenario_root / "generation.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    mutate(next(row for row in rows if row["checkpoint_id"] == checkpoint_id))
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def test_valid_formal_run_is_release_ready(formal_run):
    summary = build_summary(formal_run)
    assert summary["formal_scope"] is True
    assert summary["release"]["effective"] is True
    assert summary["release"]["blocking_issues"] == []
    assert summary["coverage"]["observed_scenarios"] == 80
    assert summary["coverage"]["failed_metrics"] == 0
    assert summary["coverage"]["applicable_metrics"] == 0
    assert summary["coverage"]["missing_metrics"] == []
    assert summary["assertions"]["failed"] == 0
    assert summary["retrieval"]["index"]["collection"] == "summary-collection"
    paths = write_summary(formal_run, summary)
    assert Path(paths["summary"]).name == SUMMARY_NAME
    assert Path(paths["report"]).name == REPORT_NAME
    assert "有效发布：是" in Path(paths["report"]).read_text(encoding="utf-8")


def test_scenario_weighting_uses_successful_checkpoint_mean():
    multi = next(
        scenario for scenario in DATASET.scenarios
        if len(scenario_plan(scenario).generation_metrics) == 2
        and all(
            row["applicable"]
            for row in scenario["metric_applicability"]
            if row["metric"] == "faithfulness"
        )
    )
    single = next(
        scenario for scenario in DATASET.scenarios
        if len(scenario_plan(scenario).generation_metrics) == 1
        and all(
            row["applicable"]
            for row in scenario["metric_applicability"]
            if row["metric"] == "faithfulness"
        )
    )
    multi_checkpoints = list(scenario_plan(multi).generation_metrics)
    single_checkpoint = next(iter(scenario_plan(single).generation_metrics))
    scores = {
        (multi["scenario_id"], multi_checkpoints[0], "faithfulness"): 1.0,
        (multi["scenario_id"], multi_checkpoints[1], "faithfulness"): 0.0,
        (single["scenario_id"], single_checkpoint, "faithfulness"): 1.0,
    }
    TEST_ROOT.mkdir(parents=True, exist_ok=True)
    root = build_run(
        TEST_ROOT / f"weight-{uuid.uuid4().hex}",
        scenario_ids=[multi["scenario_id"], single["scenario_id"]],
        scores=scores,
    )
    try:
        summary = build_summary(root)
        metric = summary["generation"]["metrics"]["faithfulness"]
        assert metric["scenario_values"][multi["scenario_id"]]["value"] == 0.5
        assert metric["scenario_values"][single["scenario_id"]]["value"] == 1.0
        assert metric["mean_including_partial"] == pytest.approx(0.75)
        assert metric["mean_complete_only"] == pytest.approx(0.75)
        assert summary["release"]["effective"] is False
        assert any("定向验证" in issue for issue in summary["release"]["blocking_issues"])
    finally:
        shutil.rmtree(root.parent, ignore_errors=True)


def test_zero_failed_not_applicable_and_applicable_are_distinct():
    scenario = next(
        scenario for scenario in DATASET.scenarios
        if scenario["scenario_id"] == "s-profile-update-01"
    )
    checkpoints = list(scenario_plan(scenario).generation_metrics)
    scores = {
        (scenario["scenario_id"], checkpoints[0], "faithfulness"): 0.0,
    }
    failed = {(scenario["scenario_id"], checkpoints[0], "factual_correctness")}
    TEST_ROOT.mkdir(parents=True, exist_ok=True)
    root = build_run(
        TEST_ROOT / f"status-{uuid.uuid4().hex}",
        scenario_ids=[scenario["scenario_id"]],
        scores=scores,
        failed=failed,
    )
    try:
        summary = build_summary(root)
        faithfulness = summary["generation"]["metrics"]["faithfulness"]
        factual = summary["generation"]["metrics"]["factual_correctness"]
        assert faithfulness["succeeded_checkpoints"] == 1
        assert faithfulness["scenario_values"][scenario["scenario_id"]]["value"] == 0.0
        assert factual["succeeded_checkpoints"] == 0
        assert factual["failed_checkpoints"] == 1
        assert factual["mean_including_partial"] is None
        assert factual["mean_complete_only"] is None
        assert factual["not_applicable_checkpoints"] == 1
        assert faithfulness["not_applicable_checkpoints"] == 1
        assert summary["coverage"]["failed_metrics"] == 1
        assert summary["release"]["effective"] is False
        assert any("failed" in issue for issue in summary["release"]["blocking_issues"])
    finally:
        shutil.rmtree(root.parent, ignore_errors=True)


def test_applicable_and_unexecuted_metrics_block_release():
    scenario = next(
        scenario for scenario in DATASET.scenarios
        if scenario["scenario_id"] == "s-profile-update-02"
    )
    checkpoints = list(scenario_plan(scenario).generation_metrics)
    TEST_ROOT.mkdir(parents=True, exist_ok=True)
    root = build_run(
        TEST_ROOT / f"missing-{uuid.uuid4().hex}",
        scenario_ids=[scenario["scenario_id"]],
        applicable={(scenario["scenario_id"], checkpoints[0], "faithfulness")},
        omitted={(scenario["scenario_id"], checkpoints[1])},
    )
    try:
        summary = build_summary(root)
        assert summary["coverage"]["applicable_metrics"] == 1
        assert summary["coverage"]["unexecuted_metrics"] > 0
        assert summary["release"]["effective"] is False
        assert any("尚无结果" in issue for issue in summary["release"]["blocking_issues"])
        assert any("未执行" in issue for issue in summary["release"]["blocking_issues"])
    finally:
        shutil.rmtree(root.parent, ignore_errors=True)


def test_partial_run_preserves_succeeded_results_and_blocks():
    scenario = next(
        scenario for scenario in DATASET.scenarios
        if scenario["scenario_id"] == "s-profile-update-01"
    )
    checkpoint = next(iter(scenario_plan(scenario).generation_metrics))
    TEST_ROOT.mkdir(parents=True, exist_ok=True)
    root = build_run(
        TEST_ROOT / f"partial-{uuid.uuid4().hex}",
        scenario_ids=[scenario["scenario_id"]],
        scores={(scenario["scenario_id"], checkpoint, "faithfulness"): 1.0},
        blocked={scenario["scenario_id"]},
    )
    try:
        summary = build_summary(root)
        assert summary["coverage"]["blocked_scenarios"] == [scenario["scenario_id"]]
        assert summary["generation"]["metrics"]["faithfulness"]["succeeded_checkpoints"] >= 1
        assert summary["release"]["effective"] is False
        assert any("blocked" in issue for issue in summary["release"]["blocking_issues"])
    finally:
        shutil.rmtree(root.parent, ignore_errors=True)


def test_assertion_failure_blocks_release(formal_run):
    plan = json.loads((formal_run / "plan.json").read_text(encoding="utf-8"))
    first = plan["scenarios"][0]["scenario_id"]
    scenario = next(
        item for item in DATASET.scenarios if item["scenario_id"] == first
    )
    assertion = scenario["assertions"][0]
    TEST_ROOT.mkdir(parents=True, exist_ok=True)
    root = build_run(
        TEST_ROOT / f"assert-{uuid.uuid4().hex}",
        scenario_ids=[first],
        assertion_failed={(first, assertion["checkpoint_id"], assertion["kind"])},
    )
    try:
        summary = build_summary(root)
        assert summary["assertions"]["failed"] == 1
        assert summary["release"]["effective"] is False
        assert any("断言失败" in issue for issue in summary["release"]["blocking_issues"])
    finally:
        shutil.rmtree(root.parent, ignore_errors=True)


def test_evidence_score_tamper_blocks_release(formal_run, run_copy):
    target = run_copy
    plan = json.loads((formal_run / "plan.json").read_text(encoding="utf-8"))
    scenario_id = plan["scenarios"][0]["scenario_id"]
    scenario_root = target / scenario_id
    checkpoint_id, metric = _first_succeeded_checkpoint(scenario_root)

    def mutate(row):
        for outcome in row["result"]["outcomes"]:
            if outcome["metric"] == metric:
                outcome["score"] = 0.123

    _edit_generation(scenario_root, checkpoint_id, mutate)
    summary = build_summary(target)
    assert summary["release"]["effective"] is False
    assert any(
        "分数" in issue for issue in summary["release"]["blocking_issues"]
    )


def test_prompt_tamper_blocks_release(formal_run, run_copy):
    target = run_copy
    plan = json.loads((formal_run / "plan.json").read_text(encoding="utf-8"))
    scenario_id = plan["scenarios"][0]["scenario_id"]
    scenario_root = target / scenario_id
    checkpoint_id, metric = _first_succeeded_checkpoint(scenario_root)

    def mutate(row):
        for evidence in row["evidence"]:
            if evidence["metric"] == metric:
                evidence["prompt_digest"] = sha256_text("tampered-prompt")

    _edit_generation(scenario_root, checkpoint_id, mutate)
    summary = build_summary(target)
    assert summary["release"]["effective"] is False
    issues = " ".join(summary["release"]["blocking_issues"])
    assert "prompt" in issues or "裁判" in issues or "证据" in issues


def test_dataset_and_index_identity_tamper_blocks_release(formal_run, run_copy):
    target = run_copy
    run_path = target / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["dataset"]["content_sha256"] = "0" * 64
    run["identity"]["retrieval"]["index"] = canonical_json({
        "collection": "other-collection",
        "manifest_sha256": sha256_text("other-manifest"),
        "build_id": "other",
        "compatibility": {},
    })
    run_path.write_text(
        json.dumps(run, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    summary = build_summary(target)
    assert summary["release"]["effective"] is False
    issues = " ".join(summary["release"]["blocking_issues"])
    assert "数据身份" in issues
    assert "物理索引" in issues


def test_duplicate_generation_identity_blocks_release(formal_run, run_copy):
    target = run_copy
    plan = json.loads((formal_run / "plan.json").read_text(encoding="utf-8"))
    scenario_id = plan["scenarios"][0]["scenario_id"]
    scenario_root = target / scenario_id
    artifact_path = scenario_root / "scenario.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["generation"].append(dict(artifact["generation"][0]))
    artifact_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    path = scenario_root / "generation.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows.append(dict(rows[0]))
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    summary = build_summary(target)
    assert summary["release"]["effective"] is False
    assert any(
        "重复" in issue or "覆盖" in issue
        for issue in summary["release"]["blocking_issues"]
    )


def test_checkpoint_source_binding_tamper_blocks_release(formal_run, run_copy):
    plan = json.loads((formal_run / "plan.json").read_text(encoding="utf-8"))
    scenario_root = run_copy / plan["scenarios"][0]["scenario_id"]

    def tamper(mutate, fragment):
        checkpoint_id, _ = _first_succeeded_checkpoint(scenario_root)
        _edit_generation(scenario_root, checkpoint_id, mutate)
        issues = build_summary(run_copy)["release"]["blocking_issues"]
        assert any(fragment in issue for issue in issues), issues

    def change_reference(row):
        row["checkpoint"]["reference"]["answer"] = "被替换的参考答案"

    tamper(change_reference, "reference 与冻结数据不一致")

    def change_answer(row):
        row["checkpoint"]["answer"]["visible_text"] += " 篡改"

    tamper(change_answer, "检查点回答与事件捕获不一致")

    def change_context(row):
        row["checkpoint"]["model_input"]["items"][0]["content"] = "真实请求中不存在的上下文"

    tamper(change_context, "模型输入上下文顺序或正文与真实请求不一致")

    def change_applicability(row):
        row["checkpoint"]["applicability"][0]["reason"] = "被改写的适用性原因"

    tamper(change_applicability, "检查点适用性偏离冻结数据")

    def change_prior(row):
        row["checkpoint"]["user_input"]["prior_messages"] = [
            {"role": "user", "content": "伪造的历史输入"}
        ]

    tamper(change_prior, "检查点历史输入偏离冻结数据")


def test_attempt_judge_call_mismatch_still_blocks_release(run_copy):
    plan = json.loads((run_copy / "plan.json").read_text(encoding="utf-8"))
    scenario_root = run_copy / plan["scenarios"][0]["scenario_id"]
    checkpoint_id, _ = _first_succeeded_checkpoint(scenario_root)

    def recorded_call(prompt_name: str) -> dict:
        prompt = "真实保存的裁判提示"
        return {
            "sequence": 0,
            "parent_run_id": "replay-chain",
            "prompt_name": prompt_name,
            "prompt": prompt,
            "prompt_sha256": sha256_text(prompt),
            "output": "{}",
            "error": None,
        }

    def change_judge_calls(row):
        evidence = row["evidence"][0]
        attempt = next(
            item for item in row["attempts"] if item["metric"] == evidence["metric"]
        )
        evidence["judge_calls"] = [recorded_call("statement_generator_prompt")]
        attempt["judge_calls"] = [recorded_call("n_l_i_statement_prompt")]

    _edit_generation(scenario_root, checkpoint_id, change_judge_calls)
    issues = build_summary(run_copy)["release"]["blocking_issues"]
    assert any(
        "成功证据与评分尝试不一致" in issue and "judge_calls" in issue
        for issue in issues
    ), issues


def _projection_checkpoint(items, message: str) -> GenerationCheckpoint:
    return GenerationCheckpoint(
        checkpoint=_link("run", "scenario", "e01", "cp1"),
        user_input=UserInputEvidence(message=message),
        answer=AnswerEvidence(visible_text="测试回答"),
        model_input=ModelInputContext(items=items),
        reference=None,
        applicability=[
            MetricApplicability(metric="faithfulness", applicable=False, reason="测试"),
            MetricApplicability(
                metric="factual_correctness", applicable=False, reason="测试"
            ),
        ],
    )


PROJECTION_REQUEST = {
    "system": "系统提示",
    "messages": [
        {"role": "user", "content": "历史附件正文"},
        {"role": "user", "content": "当前输入"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "search_exercises", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "工具结果"},
    ],
}


def _projection_event() -> EventCapture:
    return EventCapture(
        run_id="run",
        scenario_id="scenario",
        event_id="e01",
        event_type="message",
        source_event_id=None,
        session_id="session",
        operation_id=None,
        request_entry_id=None,
        terminal_status="completed",
        terminal_event=None,
        before=BusinessProjection(),
        after=BusinessProjection(),
        tool_identity_map={"call_1": "call_1"},
    )


def _projection_boundary() -> RequestBoundaryEvidence:
    return RequestBoundaryEvidence(
        api=MODEL.api,
        session_id="session",
        run_id="run",
        request_entry_id=None,
        nodes=[
            MessageNode(
                entry_id="historical-attachment",
                session_id="session",
                role="user",
                content="历史附件正文",
                kind="message",
                has_attachments=True,
                run_id="prior-run",
            ),
            MessageNode(
                entry_id="current-user",
                session_id="session",
                role="user",
                content="当前输入",
                kind="message",
                run_id="run",
            ),
        ],
        tool_nodes=[
            ToolNode(
                tool_call_id="call_1",
                tool_name="search_exercises",
                content="工具结果",
                entry_id="tool-entry",
                session_id="session",
                run_id="run",
                is_error=False,
            )
        ],
    )


def test_context_projection_requires_complete_ordered_request():
    items = [
        ContextItem(
            source="system_prompt",
            content="系统提示",
            fact_origin="system",
            message_role="system",
        ),
        ContextItem(
            source="attachment",
            content="历史附件正文",
            fact_origin="user",
            message_entry_id="historical-attachment",
            message_role="user",
        ),
        ContextItem(
            source="user_message",
            content="当前输入",
            fact_origin="user",
            message_entry_id="current-user",
            message_role="user",
        ),
        ContextItem(
            source="tool_result",
            content="工具结果",
            fact_origin="tool",
            tool_call_id="call_1",
            tool_name="search_exercises",
        ),
    ]

    def issues_for(
        checkpoint: GenerationCheckpoint,
        boundary: RequestBoundaryEvidence | None = None,
    ) -> list[str]:
        issues: list[str] = []
        _validate_context_projection(
            checkpoint,
            _projection_boundary() if boundary is None else boundary,
            _projection_event(),
            PROJECTION_REQUEST,
            issues,
            "test",
            "cp1",
        )
        return issues

    assert issues_for(_projection_checkpoint(items, "当前输入")) == []
    assert any(
        "条目数量" in issue
        for issue in issues_for(_projection_checkpoint(items[1:], "当前输入"))
    )
    assert any(
        "条目数量" in issue
        for issue in issues_for(
            _projection_checkpoint(
                [items[0], items[1], items[1], items[2], items[3]],
                "当前输入",
            )
        )
    )
    assert any(
        "顺序或正文" in issue
        for issue in issues_for(
            _projection_checkpoint(
                [items[0], items[2], items[1], items[3]], "当前输入"
            )
        )
    )
    relabeled = items[1].model_copy(update={
        "source": "business_projection",
        "fact_origin": "system",
    })
    assert any(
        "来源或身份" in issue
        for issue in issues_for(
            _projection_checkpoint(
                [items[0], relabeled, items[2], items[3]], "当前输入"
            )
        )
    )
    renamed = items[3].model_copy(update={"tool_call_id": "call_foreign"})
    assert any(
        "来源或身份" in issue
        for issue in issues_for(
            _projection_checkpoint(
                [items[0], items[1], items[2], renamed], "当前输入"
            )
        )
    )
    assert any(
        "当前输入" in issue
        for issue in issues_for(_projection_checkpoint(items, "伪造当前输入"))
    )
    issues: list[str] = []
    _validate_context_projection(
        _projection_checkpoint(items, "当前输入"),
        None,
        _projection_event(),
        PROJECTION_REQUEST,
        issues,
        "test",
        "cp1",
    )
    assert any("缺少请求节点身份证据" in issue for issue in issues)


def test_request_evidence_tamper_blocks_release(formal_run, run_copy):
    plan = json.loads((formal_run / "plan.json").read_text(encoding="utf-8"))
    scenario_root = run_copy / plan["scenarios"][0]["scenario_id"]
    events = [
        json.loads(line)
        for line in (scenario_root / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    calls = next(event for event in events if event["model_calls"])["model_calls"]
    request = scenario_root / "model-calls" / calls[0]["request_path"]
    payload = json.loads(request.read_text(encoding="utf-8"))
    payload["messages"][0]["content"] = "被替换的请求正文"
    request.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    summary = build_summary(run_copy)
    assert summary["release"]["effective"] is False
    assert any(
        "模型请求证据摘要不一致" in issue
        for issue in summary["release"]["blocking_issues"]
    )


def test_missing_generation_prompt_identity_blocks_release_and_upload(formal_run, run_copy):
    run_path = run_copy / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    del run["identity"]["generation"]["prompt_version"]
    run_path.write_text(
        json.dumps(run, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    summary = build_summary(run_copy)
    assert summary["release"]["effective"] is False
    assert any(
        "缺少生成提示身份" in issue for issue in summary["release"]["blocking_issues"]
    )
    with pytest.raises(UploadBlocked):
        build_upload_payload(run_copy)


def test_outbound_text_replaces_local_references():
    attachment = f"[附件] 训练计划.md: {ATTACHMENT_REF}"
    replaced = outbound_text(attachment)
    assert ATTACHMENT_REF not in replaced
    assert f"attachment:{sha256_text(ATTACHMENT_REF)[:12]}" in replaced
    drive_path = "D:/Repository/Fit-Agent/backend/temp/run/scenario.json"
    assert "D:/" not in outbound_text(drive_path)
    assert outbound_text(
        "依据 backend/dataset/actions.agent.json 记录 equipment=barbell，3 组 / 12 次。"
    ) == "依据 backend/dataset/actions.agent.json 记录 equipment=barbell，3 组 / 12 次。"
    with pytest.raises(UploadBlocked):
        assert_upload_whitelist({"inputs": {"message": attachment}})


def test_upload_payload_binds_sanitized_attachment_references(formal_run):
    payload, _, _ = build_upload_payload(formal_run)
    artifact = json.loads(
        (formal_run / "s-plan-import-file-01" / "scenario.json").read_text(
            encoding="utf-8"
        )
    )
    originals = [
        item["content"]
        for row in artifact["generation"]
        if row["checkpoint"] is not None
        for item in row["checkpoint"]["model_input"]["items"]
        if ATTACHMENT_REF in item["content"]
    ]
    assert originals
    uploaded = [
        item
        for row in payload["results"]
        if row["inputs"]["scenario_id"] == "s-plan-import-file-01"
        for item in row["inputs"]["model_input"]["items"]
    ]
    token = f"attachment:{sha256_text(ATTACHMENT_REF)[:12]}"
    assert ATTACHMENT_REF not in json.dumps(payload, ensure_ascii=False)
    assert {item["content_sha256"] for item in uploaded} == {
        sha256_text(content) for content in originals
    }
    assert all(token in item["content"] for item in uploaded)
    assert_upload_whitelist(payload)


def test_upload_payload_whitelist_and_offline_check(formal_run):
    payload, summary, _ = build_upload_payload(formal_run)
    assert summary["release"]["effective"] is True
    assert payload["dataset_name"].startswith("fit-agent-generation-rebuild-v1-")
    assert payload["results"]
    text = json.dumps(payload, ensure_ascii=False)
    for forbidden in (
        "judge_base_url",
        "judge_calls",
        "chain_steps",
        "prompt_fingerprints",
        "scoring_input",
        "base_url",
        "status_porcelain",
        "version_dir",
        "sessions/",
        ATTACHMENT_REF,
    ):
        assert forbidden not in text
    assert "https://judge.invalid" not in text
    assert_upload_whitelist(payload)
    with pytest.raises(Exception):
        bad = json.loads(text)
        bad["results"][0]["run_metadata"]["judge_base_url"] = "https://leak.invalid"
        assert_upload_whitelist(bad)
    checked = check_payload(formal_run)
    assert checked["status"] == "offline_payload_checked"
    assert checked["network"] == "not_called"
    assert checked["rows"] == len(payload["results"])


def test_source_digest_receipt_and_interrupted_boundary(formal_run, run_copy):
    target = run_copy
    before = source_digest(target)
    assert source_digest(target) == before
    scenario_id = next(
        path.name for path in target.iterdir() if path.is_dir()
    )
    generation = target / scenario_id / "generation.jsonl"
    generation.write_text(
        generation.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    assert source_digest(target) != before
    source_sha = before
    payload_sha = sha256_text("payload")
    assert receipt_matches(
        {"source_sha256": source_sha, "payload_sha256": payload_sha},
        source_sha,
        payload_sha,
    )
    assert not receipt_matches(
        {"source_sha256": "x", "payload_sha256": payload_sha},
        source_sha,
        payload_sha,
    )
    pending = {
        "experiment_name": "run-generation",
        "dataset_name": "dataset",
        "source_sha256": source_sha,
        "payload_sha256": payload_sha,
    }
    remote = RemoteExperiment(
        experiment_id="experiment",
        experiment_name="run-generation",
        dataset_id="dataset-id",
        dataset_name="dataset",
        example_count=2,
        run_count=2,
    )
    assert pending_matches(pending, source_sha, payload_sha)
    assert resolve_pending(pending, remote, rows=2) == "recovered"
    assert resolve_pending(pending, None, rows=2) == "resubmit"
    conflict = RemoteExperiment(
        experiment_id="experiment",
        experiment_name="run-generation",
        dataset_id="dataset-id",
        dataset_name="dataset",
        example_count=3,
        run_count=3,
    )
    assert resolve_pending(pending, conflict, rows=2) == "conflict"


def _remote_readback(payload):
    started = datetime(2026, 10, 18, tzinfo=UTC)
    experiment_id = str(
        uuid.uuid5(NAMESPACE_URL, f"remote-experiment:{payload['experiment_name']}")
    )
    examples = [
        Example(
            id=row["row_id"],
            inputs=row["inputs"],
            outputs=row["expected_outputs"],
        )
        for row in payload["results"]
    ]
    runs = []
    feedback = []
    for row in payload["results"]:
        run_id = str(uuid.uuid5(NAMESPACE_URL, f"remote-run:{row['row_id']}"))
        runs.append(Run(
            id=run_id,
            name=row["run_name"],
            run_type="root",
            start_time=started,
            reference_example_id=row["row_id"],
            outputs=row["actual_outputs"],
            extra={"metadata": row["run_metadata"]},
        ))
        for score in row["evaluation_scores"]:
            feedback.append(Feedback(
                id=str(uuid.uuid4()),
                run_id=run_id,
                trace_id=run_id,
                key=score["key"],
                score=score["score"],
                created_at=started,
                modified_at=started,
            ))
    summary_feedback = [
        Feedback(
            id=str(uuid.uuid4()),
            run_id=None,
            trace_id=None,
            key=score["key"],
            score=score["score"],
            session_id=experiment_id,
            created_at=started,
            modified_at=started,
        )
        for score in payload["summary_experiment_scores"]
    ]
    return experiment_id, examples, runs, feedback, summary_feedback


def _remote_snapshot_for(
    payload,
    examples,
    runs,
    feedback,
    summary_feedback,
    *,
    experiment_metadata=None,
):
    return remote_snapshot(
        examples,
        runs,
        feedback,
        experiment_metadata=(
            payload["experiment_metadata"]
            if experiment_metadata is None
            else experiment_metadata
        ),
        summary_feedback=summary_feedback,
    )


def test_remote_readback_detects_cloud_score_and_row_drift(formal_run):
    payload, _, _ = build_upload_payload(formal_run)
    expectation = payload_expectation(payload)
    _, examples, runs, feedback, summary_feedback = _remote_readback(payload)
    require_remote_matches(
        expectation,
        _remote_snapshot_for(
            payload, examples, runs, feedback, summary_feedback
        ),
    )

    drifted = list(feedback)
    first = drifted[0]
    drifted[0] = first.model_copy(update={"score": float(first.score) + 0.25})
    with pytest.raises(UploadBlocked, match="scores"):
        require_remote_matches(
            expectation,
            _remote_snapshot_for(
                payload, examples, runs, drifted, summary_feedback
            ),
        )

    with pytest.raises(UploadBlocked, match="字段"):
        require_remote_matches(
            expectation,
            _remote_snapshot_for(
                payload, examples, runs[:-1], feedback, summary_feedback
            ),
        )

    altered = examples[0].model_copy(
        update={"inputs": {**examples[0].inputs, "checkpoint_id": "cp-foreign"}}
    )
    with pytest.raises(UploadBlocked, match="inputs"):
        require_remote_matches(
            expectation,
            _remote_snapshot_for(
                payload, [altered, *examples[1:]], runs, feedback, summary_feedback
            ),
        )


def test_remote_readback_detects_identity_and_summary_drift(formal_run):
    payload, _, _ = build_upload_payload(formal_run)
    expectation = payload_expectation(payload)
    _, examples, runs, feedback, summary_feedback = _remote_readback(payload)

    def snapshot(**changes):
        return _remote_snapshot_for(
            payload,
            examples,
            runs,
            feedback,
            summary_feedback,
            **changes,
        )

    generation_drift = json.loads(json.dumps(payload["experiment_metadata"]))
    generation_drift["generation"]["model"] = "foreign-model"
    with pytest.raises(UploadBlocked, match="实验元数据"):
        require_remote_matches(
            expectation, snapshot(experiment_metadata=generation_drift)
        )

    source_drift = json.loads(json.dumps(payload["experiment_metadata"]))
    source_drift["source_sha256"] = "0" * 64
    with pytest.raises(UploadBlocked, match="source_sha256"):
        require_remote_matches(
            expectation, snapshot(experiment_metadata=source_drift)
        )

    rows_drift = json.loads(json.dumps(payload["experiment_metadata"]))
    rows_drift["rows_sha256"] = "0" * 64
    with pytest.raises(UploadBlocked, match="rows_sha256"):
        require_remote_matches(expectation, snapshot(experiment_metadata=rows_drift))

    summary_drift = list(summary_feedback)
    first = summary_drift[0]
    summary_drift[0] = first.model_copy(
        update={"score": float(first.score) + 0.25}
    )
    with pytest.raises(UploadBlocked, match="汇总分数"):
        require_remote_matches(
            expectation,
            _remote_snapshot_for(
                payload, examples, runs, feedback, summary_drift
            ),
        )

    run_metadata = dict(runs[0].metadata)
    run_metadata["run_id"] = "foreign-run"
    run_drift = [
        runs[0].model_copy(update={"extra": {"metadata": run_metadata}}),
        *runs[1:],
    ]
    with pytest.raises(UploadBlocked, match="run_metadata"):
        require_remote_matches(
            expectation,
            _remote_snapshot_for(
                payload, examples, run_drift, feedback, summary_feedback
            ),
        )


def test_retrieval_publish_gate_requires_staged_coverage():
    row = {
        "expect_empty": False,
        "scores": {
            "recall@20": 1.0,
            "hit@5": 1.0,
            "mrr@5": 1.0,
            "ndcg@5": 1.0,
            "precision@5": 1.0,
        },
        "candidate_pool": [],
        "final_model_input": [],
    }
    projection = {
        "candidate_pool": [],
        "final_model_input": [],
        "scores": {},
        "search_evidence": {"model_visible": {"items": []}},
        "hard_constraint_violations": [],
        "budget_compliant": True,
    }
    document = {
        "schema": SCHEMA_STAGED,
        "query_ids": ["q1", "q2"],
        "groups": {
            "hybrid": {
                "status": "complete",
                "metrics": {
                    "overall": {key: 1.0 for key in row["scores"]},
                    "per_query": {"q1": row},
                },
                "projections": {"q1": projection},
            }
        },
    }
    assert publishable(document) is False
    document["groups"]["hybrid"]["metrics"]["per_query"]["q2"] = dict(row)
    document["groups"]["hybrid"]["projections"]["q2"] = dict(projection)
    # 覆盖补齐仍缺数据身份、批准参数与可重算响应证据，空池满分不得判为可发布。
    assert publishable(document) is False
    legacy = {
        "schema": SCHEMA_LEGACY,
        "query_ids": ["q1"],
        "groups": {
            "hybrid": {
                "status": "complete",
                "metrics": {
                    "overall": {
                        key: 1.0
                        for key in ("hit@5", "recall@5", "precision@5", "mrr@5", "ndcg@5")
                    },
                    "per_query": {
                        "q1": {
                            "expect_empty": False,
                            "scores": {
                                key: 1.0
                                for key in ("hit@5", "recall@5", "precision@5", "mrr@5", "ndcg@5")
                            },
                        }
                    },
                },
            }
        },
    }
    assert publishable(legacy) is True


RERANK_POOL = ["0001", "0002", "0003", "0004", "0005", "0006"]
RERANK_FINAL = ["0003", "0001", "0005"]
RERANK_ORDER = [2, 0, 4, 1, 5, 3]
RERANK_MANIFEST = '{"build_id":"rerank-build","compatibility":{}}'


def _rerank_rows(order: list[int]) -> list[dict]:
    # 分数严格降序，rrf_rank 指向该名次在原始候选池中的位置。
    return [
        {
            "id": RERANK_POOL[rank],
            "rerank_score": round(0.9 - 0.1 * position, 4),
            "rrf_rank": rank,
        }
        for position, rank in enumerate(order)
    ]


def _rerank_record(
    rows: list[dict],
    *,
    pool: list[str] = RERANK_POOL,
    final: list[str] = RERANK_FINAL,
) -> SearchStageRecord:
    return SearchStageRecord(
        sequence=0,
        run_id="rerank-run",
        scenario_id="s-rerank",
        event_id="e01",
        tool_call_id="rerank-tool",
        index_collection="rerank-collection",
        index_manifest_sha256=sha256_text(RERANK_MANIFEST),
        index_manifest_json=RERANK_MANIFEST,
        arguments={"purpose": "lookup"},
        candidate_pool=pool,
        final_model_input=final,
        response_json={
            "items": [
                {
                    "id": item,
                    "name": item,
                    "equipment": "barbell",
                    "target": "chest",
                    "matches": [],
                }
                for item in final
            ],
            "has_more": len(pool) > len(final),
        },
        scores=[float(len(pool) - index) for index in range(len(pool))],
        threshold=0.0,
        experiment="rerank",
        rerank=RerankEvidence(
            mode="rerank",
            identity={
                "model": "summary-rerank",
                "endpoint": "https://rerank.invalid/text-rerank",
                "text_version": "retrieval-text-2026-10-14.2",
            },
            scored=rows,
            usage={
                "requests": 1 if rows else 0,
                "prompt_tokens": 120 if rows else 0,
                "total_tokens": 120 if rows else 0,
            },
            request_id="rerank-request" if rows else None,
        ),
    )


def _retrieval_issues(record: SearchStageRecord) -> list[str]:
    plan = ScenarioPlan(
        scenario_id="s-rerank",
        event_ids=["e01"],
        checkpoint_ids=[],
        generation_checkpoint_ids=[],
        generation_metrics={},
        event_types={"e01": "message"},
        tags=["rerank-test"],
    )
    artifact = ScenarioArtifact(
        run_id="rerank-run",
        scenario_id="s-rerank",
        session_ids=["rerank-session"],
        events=[],
        assertions=[],
        generation=[],
        retrieval=[record],
        proposal_bindings={},
    )
    issues: list[str] = []
    _validate_retrieval(plan, artifact, "rerank-run", issues, "rerank", [])
    return [item.split(":", 1)[1] for item in issues]


def test_valid_rerank_record_passes_summary_gate():
    assert _retrieval_issues(_rerank_record(_rerank_rows(RERANK_ORDER))) == []


def test_empty_pool_rerank_record_keeps_zero_request_semantics():
    record = _rerank_record([], pool=[], final=[])
    assert _retrieval_issues(record) == []
    assert record.rerank.usage.requests == 0
    assert record.rerank.request_id is None


def _rank_beyond_pool() -> SearchStageRecord:
    # 池内 3 条候选，其中一条原排名越出候选池长度但仍落在字段上限内。
    return _rerank_record(
        [
            {"id": "0003", "rerank_score": 0.9, "rrf_rank": 5},
            {"id": "0001", "rerank_score": 0.8, "rrf_rank": 0},
            {"id": "0002", "rerank_score": 0.7, "rrf_rank": 1},
        ],
        pool=["0001", "0002", "0003"],
        final=["0003", "0001"],
    )


def _swapped_rank_binding() -> list[dict]:
    # 保持名次与分数不变，只把首尾两行的动作 ID 对调，使原排名指向其他候选。
    rows = _rerank_rows(RERANK_ORDER)
    rows[0]["id"], rows[-1]["id"] = rows[-1]["id"], rows[0]["id"]
    return rows


@pytest.mark.parametrize(
    ("build", "expected"),
    [
        (
            lambda: _rerank_record(_rerank_rows(RERANK_ORDER)[:4]),
            ["重排结果数量与候选池不一致", "重排结果未完整覆盖候选池"],
        ),
        (
            lambda: _rerank_record(_swapped_rank_binding()),
            ["重排原排名与候选池不一致", "最终输入未按重排顺序排列"],
        ),
        (
            lambda: _rerank_record(_rerank_rows([0, 2, 4, 1, 5, 3])),
            ["最终输入未按重排顺序排列"],
        ),
        (_rank_beyond_pool, ["重排原排名越界"]),
    ],
    ids=["scored_count", "rank_binding", "final_order", "rank_range"],
)
def test_summary_rejects_rerank_evidence_broken_against_candidate_pool(build, expected):
    assert _retrieval_issues(build()) == expected
