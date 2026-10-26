import hashlib
import json
import shutil
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from langfuse.api import (
    Experiment,
    ExperimentItem,
    ExperimentItemsResponse,
    ExperimentsResponse,
    ExperimentsResponseMeta,
    NotFoundError,
)

from evaluation.contracts import (
    AnswerEvidence,
    BusinessProjection,
    ContextItem,
    DatasetIdentity,
    EvidenceLink,
    ExternalJudgeApplicability,
    ExternalJudgeCheckpointUpload,
    GenerationCheckpoint,
    GenerationReference,
    MessageNode,
    MetricApplicability,
    ModelInputContext,
    PriorMessage,
    RequestBoundaryEvidence,
    RerankEvidence,
    RerankIdentityEvidence,
    RerankScoredEvidence,
    RerankUsageEvidence,
    ToolNode,
    UserInputEvidence,
)
from evaluation.dataset import BACKEND, load_for_evaluation
from evaluation.generation_upload import (
    EXTERNAL_APPLICABILITY_REVIEW_SCHEMA,
    EXTERNAL_APPLICABILITY_SCHEMA,
    EXTERNAL_DATASET_RECEIPT_NAME,
    EXTERNAL_DATASET_RECEIPT_SCHEMA,
    EXTERNAL_EXPERIMENT_PENDING_NAME,
    EXTERNAL_EXPERIMENT_PENDING_SCHEMA,
    EXTERNAL_EXPERIMENT_RECEIPT_NAME,
    EXTERNAL_EXPERIMENT_RECEIPT_SCHEMA,
    EXTERNAL_PAYLOAD_NAME,
    EXTERNAL_PAYLOAD_SCHEMA,
    EXTERNAL_PRESENTATION_VERSION,
    ExternalApplicabilityEntry,
    ExternalApplicabilityInput,
    ExternalCheckpointEvidence,
    UploadBlocked,
    _assert_external_upload_whitelist,
    _collect_external_runs,
    _example_document,
    _external_receipt_identity,
    _external_retrieval_evidence,
    _frozen_run_output,
    _FrozenCheckpointTarget,
    _parse_args,
    _verify_external_examples,
    applicability_path,
    applicability_review_path,
    build_external_payload,
    build_external_projection,
    build_upload_payload,
    check_external_projection,
    external_dir,
    external_rule_filters,
    load_external_applicability,
    load_external_run,
    outbound_text,
    payload_digest,
    review_external_source,
    submit_external_experiment,
    sync_external_dataset,
)
from evaluation.langfuse_scenarios import EXTERNAL_PROTOCOL_VERSION
from evaluation.scenario_evidence import (
    EvaluationCallRecorder,
    canonical_json,
    normalize_tool_id,
    sha256_text,
)
from evaluation.scenarios import (
    DEFAULT_VERSION_DIR,
    EventCapture,
    ExecutionIdentityError,
    GenerationCheckpointEvidence,
    ScenarioExecution,
    SearchStageRecord,
    _model_call,
    _seal_execution,
    frozen_scenario_sha256,
    generation_reference,
    load_execution,
    scenario_plan,
    score_execution_external,
)
from evaluation.summary import SummaryError

RUN_ID = "task3-external-fixture"
CALL_MODEL = SimpleNamespace(
    api="openai-completions", provider="fixture", id="fixture-model"
)
PROFILE_SCENARIO = "s-profile-update-01"
FAILED_SCENARIO = "s-action-verification-02"
SEARCH_TOOL = "search_exercises"
SEARCH_CALL = "call-search-1"
READ_CALL = "call-read-1"
RERANK_ENDPOINT = "https://rerank.example.invalid/v1/rerank?token=hidden"
LOCAL_PATH = "D:\\datasets\\exercise.json"
SESSION_PATH = "sessions/abc/attachments/plan.md"


@pytest.fixture(scope="module")
def dataset():
    return load_for_evaluation(DEFAULT_VERSION_DIR)


def dataset_identity(dataset) -> DatasetIdentity:
    identity = dataset.identity()
    return DatasetIdentity(
        version=identity["version"],
        manifest_sha256=identity["manifest_sha256"],
        content_sha256=identity["content_sha256"],
        corpus_sha256=identity["corpus_sha256"],
    )


def scenario_by_id(dataset, scenario_id: str) -> dict:
    return next(
        scenario for scenario in dataset.scenarios
        if scenario["scenario_id"] == scenario_id
    )


def frozen_applicability(scenario: dict, checkpoint_id: str):
    return [
        MetricApplicability(
            metric=row["metric"],
            applicable=row["applicable"],
            reason=row["reason"],
        )
        for row in scenario["metric_applicability"]
        if row["checkpoint_id"] == checkpoint_id
        and row["metric"] in ("faithfulness", "factual_correctness")
    ]


def search_response() -> dict:
    return {
        "items": [
            {
                "id": "0030",
                "name": "杠铃窄距卧推",
                "equipment": "barbell",
                "target": "triceps",
                "matches": [],
            },
            {
                "id": "0025",
                "name": "杠铃卧推",
                "equipment": "barbell",
                "target": "pectorals",
                "matches": [],
            },
        ],
        "has_more": False,
    }


def search_text() -> str:
    return json.dumps(
        search_response(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def event_capture(
    run_id: str,
    scenario_id: str,
    event_id: str,
    event_type: str,
    request_entry_id: str | None,
) -> EventCapture:
    return EventCapture(
        run_id=run_id,
        scenario_id=scenario_id,
        event_id=event_id,
        event_type=event_type,
        source_event_id=None,
        session_id="session-1",
        operation_id=None,
        request_entry_id=request_entry_id,
        terminal_status="completed",
        terminal_event="run_completed",
        answer_text=None,
        before=BusinessProjection(),
        after=BusinessProjection(),
    )


def retrieval_record(run_id: str, scenario_id: str) -> SearchStageRecord:
    return SearchStageRecord(
        sequence=0,
        run_id=run_id,
        scenario_id=scenario_id,
        event_id="e01",
        tool_call_id=SEARCH_CALL,
        index_collection="fit-agent-test-index",
        index_manifest_sha256="c" * 64,
        index_manifest_json='{"build_id":"test"}',
        arguments={
            "query": "卧推",
            "source_file": "D:\\private\\search-input.json",
        },
        candidate_pool=["0025", "0030", "0036"],
        final_model_input=["0030", "0025"],
        response_json=search_response(),
        scores=[0.5, 0.4, 0.3],
        threshold=0.0,
        experiment="rerank",
        rerank=RerankEvidence(
            mode="rerank",
            identity=RerankIdentityEvidence(
                model="reranker-1",
                endpoint=RERANK_ENDPOINT,
                text_version="text-1",
            ),
            scored=[
                RerankScoredEvidence(id="0030", rerank_score=0.9, rrf_rank=1),
                RerankScoredEvidence(id="0025", rerank_score=0.8, rrf_rank=0),
            ],
            usage=RerankUsageEvidence(
                requests=1, prompt_tokens=10, total_tokens=10
            ),
            request_id="rerank-request-1",
        ),
    )


def full_checkpoint(
    run_id: str,
    scenario: dict,
    boundary: RequestBoundaryEvidence,
) -> GenerationCheckpoint:
    message = scenario["input"]["message"]
    applicability = frozen_applicability(scenario, "cp1")
    return GenerationCheckpoint(
        checkpoint=EvidenceLink(
            run_id=run_id,
            scenario_id=scenario["scenario_id"],
            event_id="e01",
            checkpoint_id="cp1",
        ),
        user_input=UserInputEvidence(
            message=message,
            prior_messages=[
                PriorMessage(role="user", content="我刚开始系统训练。"),
                PriorMessage(role="assistant", content="已记录你的训练背景。"),
            ],
        ),
        answer=AnswerEvidence(
            visible_text=(
                "杠铃卧推使用 barbell，目标肌群 pectorals。"
                f"资料来自 {LOCAL_PATH} 与 {SESSION_PATH}。"
            ),
            structured_snapshot={
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "杠铃卧推"}],
            },
        ),
        model_input=ModelInputContext(
            items=[
                ContextItem(
                    source="system_prompt",
                    content="你是训练助手。",
                    fact_origin="system",
                    message_role="system",
                ),
                ContextItem(
                    source="user_message",
                    content=message,
                    fact_origin="user",
                    message_entry_id="entry-user",
                    message_role="user",
                ),
                ContextItem(
                    source="business_projection",
                    content="<business_context>{}</business_context>",
                    fact_origin="system",
                    message_entry_id="entry-business",
                    message_role="user",
                ),
                ContextItem(
                    source="tool_result",
                    content=search_text(),
                    fact_origin="tool",
                    tool_call_id=SEARCH_CALL,
                    tool_name=SEARCH_TOOL,
                ),
                ContextItem(
                    source="tool_result",
                    content="动作详情：杠铃卧推，水平推举。",
                    fact_origin="tool",
                    tool_call_id=READ_CALL,
                    tool_name="read",
                    truncated=True,
                    omitted_chars=3,
                    original_chars=len("动作详情：杠铃卧推，水平推举。") + 3,
                ),
            ],
            truncated=True,
            omitted_chars=7,
        ),
        reference=generation_reference(scenario, "cp1"),
        applicability=applicability,
    )


def full_boundary(run_id: str, scenario: dict) -> RequestBoundaryEvidence:
    return RequestBoundaryEvidence(
        api="openai-completions",
        session_id="session-1",
        run_id=run_id,
        request_entry_id="req-e01",
        nodes=[
            MessageNode(
                entry_id="entry-user",
                session_id="session-1",
                role="user",
                content=scenario["input"]["message"],
                kind="message",
                run_id=run_id,
            ),
            MessageNode(
                entry_id="entry-business",
                session_id="session-1",
                role="user",
                content="<business_context>{}</business_context>",
                kind="business_context",
                run_id=run_id,
            ),
        ],
        tool_nodes=[
            ToolNode(
                tool_call_id=SEARCH_CALL,
                tool_name=SEARCH_TOOL,
                content=search_text(),
                entry_id="entry-search",
                session_id="session-1",
                run_id=run_id,
                is_error=False,
            ),
            ToolNode(
                tool_call_id=READ_CALL,
                tool_name="read",
                content="动作详情：杠铃卧推，水平推举。" + "xxx",
                entry_id="entry-read",
                session_id="session-1",
                run_id=run_id,
                is_error=False,
            ),
        ],
    )


def profile_execution(dataset) -> ScenarioExecution:
    scenario = scenario_by_id(dataset, PROFILE_SCENARIO)
    plan = scenario_plan(scenario)
    boundary = full_boundary(RUN_ID, scenario)
    checkpoint = full_checkpoint(RUN_ID, scenario, boundary)
    cp2_applicability = frozen_applicability(scenario, "cp2")
    assert all(not row.applicable for row in cp2_applicability)
    return _seal_execution(
        ScenarioExecution(
            run_id=RUN_ID,
            scenario_id=PROFILE_SCENARIO,
            dataset=dataset_identity(dataset),
            scenario_sha256=frozen_scenario_sha256(scenario),
            execution_sha256="0" * 64,
            status="completed",
            error=None,
            session_ids=["session-1"],
            initial_state_sha256="a" * 64,
            initial_projection=BusinessProjection(),
            expected_event_ids=list(plan.event_ids),
            expected_checkpoint_ids=list(plan.checkpoint_ids),
            events=[
                event_capture(RUN_ID, PROFILE_SCENARIO, "e01", "message", "req-e01"),
                event_capture(RUN_ID, PROFILE_SCENARIO, "e03", "confirm", None),
            ],
            assertion_checkpoints=[],
            generation_checkpoints=[
                GenerationCheckpointEvidence(
                    checkpoint_id="cp1",
                    event_id="e01",
                    applicability=checkpoint.applicability,
                    checkpoint=checkpoint,
                    request_boundary=boundary,
                ),
                GenerationCheckpointEvidence(
                    checkpoint_id="cp2",
                    event_id="e03",
                    applicability=cp2_applicability,
                    checkpoint=None,
                ),
            ],
            retrieval=[retrieval_record(RUN_ID, PROFILE_SCENARIO)],
            proposal_bindings={},
            generation_metrics=dict(plan.generation_metrics),
        )
    )


def failed_execution(dataset) -> ScenarioExecution:
    scenario = scenario_by_id(dataset, FAILED_SCENARIO)
    plan = scenario_plan(scenario)
    applicability = frozen_applicability(scenario, "cp1")
    return _seal_execution(
        ScenarioExecution(
            run_id=RUN_ID,
            scenario_id=FAILED_SCENARIO,
            dataset=dataset_identity(dataset),
            scenario_sha256=frozen_scenario_sha256(scenario),
            execution_sha256="0" * 64,
            status="failed",
            error="生成检查点构造失败（测试证据）",
            session_ids=["session-1"],
            initial_state_sha256="a" * 64,
            expected_event_ids=list(plan.event_ids),
            expected_checkpoint_ids=list(plan.checkpoint_ids),
            events=[
                event_capture(
                    RUN_ID, FAILED_SCENARIO, "e01", "message", "req-e01"
                )
            ],
            assertion_checkpoints=[],
            generation_checkpoints=[
                GenerationCheckpointEvidence(
                    checkpoint_id="cp1",
                    event_id="e01",
                    applicability=applicability,
                    checkpoint=None,
                    runner_error="生成检查点构造失败（测试证据）",
                )
            ],
            retrieval=[],
            proposal_bindings={},
            generation_metrics=dict(plan.generation_metrics),
        )
    )


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


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
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.tool_call_id,
                    "content": item.content,
                }
            )
        else:
            messages.append({"role": "user", "content": item.content})
    request = {"model": CALL_MODEL.id, "messages": messages}
    if system is not None:
        request["system"] = system
    return request


def attach_model_calls(
    execution: ScenarioExecution, scenario_root: Path
) -> ScenarioExecution:
    checkpoints = {
        entry.event_id: entry
        for entry in execution.generation_checkpoints
        if entry.checkpoint is not None
    }
    events = []
    for event in execution.events:
        entry = checkpoints.get(event.event_id)
        if entry is None:
            events.append(event)
            continue
        message_id = f"{execution.scenario_id}-{event.event_id}-answer"
        recorder = EvaluationCallRecorder(scenario_root / "model-calls")
        sequence = recorder.record_request(
            _request_for_items(entry.checkpoint.model_input.items),
            CALL_MODEL,
            "normal",
            message_id,
            None,
        )
        recorder.record_outcome(sequence, "stop", None)
        boundary = entry.request_boundary
        tool_identity_map = (
            {}
            if boundary is None
            else {
                node.tool_call_id: normalize_tool_id(
                    node.tool_call_id, boundary.api
                )
                for node in boundary.tool_nodes
                if node.session_id == boundary.session_id
            }
        )
        events.append(
            event.model_copy(
                update={
                    "answer_text": entry.checkpoint.answer.visible_text,
                    "answer_structured": entry.checkpoint.answer.structured_snapshot,
                    "answer_message_id": message_id,
                    "model_calls": [_model_call(recorder.records[-1])],
                    "tool_identity_map": tool_identity_map,
                }
            )
        )
    return _seal_execution(execution.model_copy(update={"events": events}))


def build_fixture_run(dataset, root: Path) -> tuple[Path, dict[str, ScenarioExecution]]:
    run_dir = root / RUN_ID
    executions = {
        PROFILE_SCENARIO: profile_execution(dataset),
        FAILED_SCENARIO: failed_execution(dataset),
    }
    samples = []
    example_ids = {}
    for scenario_id, execution in executions.items():
        example_id = str(
            uuid5(NAMESPACE_URL, f"{RUN_ID}:{scenario_id}")
        )
        example_ids[scenario_id] = example_id
        scenario_root = run_dir / "scenarios" / example_id / scenario_id
        execution = attach_model_calls(execution, scenario_root)
        executions[scenario_id] = execution
        write_json(
            scenario_root / "execution.json",
            execution.model_dump(mode="json"),
        )
        record = score_execution_external(
            execution, scenario_by_id(dataset, scenario_id)["assertions"]
        )
        scoring_path = scenario_root / "external-scoring.json"
        write_json(scoring_path, record.model_dump(mode="json"))
        samples.append(
            {
                "example_id": example_id,
                "scenario_id": scenario_id,
                "sdk_run_id": f"sdk-{scenario_id}",
                "status": (
                    "complete"
                    if execution.status == "completed"
                    else "failed"
                ),
                "execution_sha256": execution.execution_sha256,
                "execution_status": execution.status,
                "scoring_mode": "external",
                "feedback_version": 3,
                "scoring_relative": (
                    f"scenarios/{example_id}/{scenario_id}/external-scoring.json"
                ),
                "scoring_sha256": sha256_text(
                    scoring_path.read_text(encoding="utf-8")
                ),
                "run_error": None,
                "incomplete": list(record.incomplete),
            }
        )
    write_json(
        run_dir / "sdk-run.json",
        {
            "schema": "fit-agent-scenario-langfuse-1",
            "run_id": RUN_ID,
            "purpose": "validation",
            "feedback_version": 3,
            "scoring_mode": "external",
            "protocol_version": EXTERNAL_PROTOCOL_VERSION,
            "status": "incomplete",
            "started_at": "2026-10-21T00:00:00+00:00",
            "finished_at": "2026-10-21T00:05:00+00:00",
            "dataset": dataset.identity(),
            "dataset_name": "fit-agent-scenario-sdk-fixture",
            "sample_scope": {
                "scenario_ids": [PROFILE_SCENARIO, FAILED_SCENARIO],
                "count": 2,
                "selection": [],
            },
            "samples": samples,
            "example_ids": example_ids,
            "coverage": {},
            "issues": [],
        },
    )
    return run_dir, executions


@pytest.fixture
def run_dir(tmp_path, dataset):
    root = tmp_path / "source"
    build_fixture_run(dataset, root)
    target = tmp_path / "run" / RUN_ID
    shutil.copytree(root / RUN_ID, target)
    return target


def approval_entries(
    evidence, decisions: dict[tuple[str, str, str], list[ExternalJudgeApplicability]]
) -> list[ExternalApplicabilityEntry]:
    return [
        ExternalApplicabilityEntry(
            execution_sha256=checkpoint.execution_sha256,
            checkpoint=checkpoint.link,
            applicability=deepcopy(decisions[checkpoint.key]),
        )
        for checkpoint in evidence.checkpoints
    ]


def approval_material(
    entries: list[ExternalApplicabilityEntry], dataset
) -> dict:
    ordered = sorted(
        entries,
        key=lambda entry: (
            entry.checkpoint.scenario_id,
            entry.checkpoint.event_id,
            entry.checkpoint.checkpoint_id,
        ),
    )
    return {
        "schema_version": EXTERNAL_APPLICABILITY_SCHEMA,
        "run_id": RUN_ID,
        "dataset": dataset_identity(dataset).model_dump(mode="json"),
        "entries": [entry.model_dump(mode="json") for entry in ordered],
    }


def write_approval(
    run_dir: Path,
    entries: list[ExternalApplicabilityEntry],
    dataset,
    **overrides,
) -> ExternalApplicabilityInput:
    material = approval_material(entries, dataset)
    digest = sha256_text(canonical_json(material))
    approval = {
        "status": "approved",
        "reviewer": "用户",
        "reviewer_type": "user",
        "reviewed_at": "2026-10-21T00:00:00+00:00",
        "comment": "显式批准外部检查点适用性",
        "content_sha256": digest,
        "decision_ref": "optimization/evaluation-contracts.md#13",
    }
    approval.update(overrides)
    document = {**material, "approval": approval}
    path = applicability_path(run_dir)
    write_json(path, document)
    return ExternalApplicabilityInput.model_validate(document)


def refresh_raw_approval(raw: dict) -> None:
    ordered = sorted(
        raw["entries"],
        key=lambda entry: (
            entry["checkpoint"]["scenario_id"],
            entry["checkpoint"]["event_id"],
            entry["checkpoint"]["checkpoint_id"],
        ),
    )
    raw["approval"]["content_sha256"] = sha256_text(
        canonical_json(
            {
                "schema_version": raw["schema_version"],
                "run_id": raw["run_id"],
                "dataset": raw["dataset"],
                "entries": ordered,
            }
        )
    )


def default_decisions(
    evidence, not_applicable_reason="纯状态确认，没有模型生成的事实"
):
    decisions = {}
    for checkpoint in evidence.checkpoints:
        if checkpoint.link.scenario_id == PROFILE_SCENARIO and (
            checkpoint.link.checkpoint_id == "cp2"
        ):
            decisions[checkpoint.key] = [
                ExternalJudgeApplicability(
                    metric="evidence_supported_v1",
                    applicable=False,
                    reason=not_applicable_reason,
                ),
                ExternalJudgeApplicability(
                    metric="reference_requirements_met_v1",
                    applicable=False,
                    reason=not_applicable_reason,
                ),
            ]
        else:
            decisions[checkpoint.key] = [
                ExternalJudgeApplicability(
                    metric="evidence_supported_v1", applicable=True
                ),
                ExternalJudgeApplicability(
                    metric="reference_requirements_met_v1", applicable=True
                ),
            ]
    return decisions


def test_review_exports_checkpoints_and_blocks_unapproved_projection(
    run_dir, dataset
):
    execution_bytes = (
        run_dir / "scenarios"
    ).glob("**/execution.json")
    before = {path: path.read_bytes() for path in execution_bytes}
    result = review_external_source(run_dir, dataset_dir=DEFAULT_VERSION_DIR)
    assert result["status"] == "review_ready"
    assert result["network"] == "not_called"
    assert result["counts"] == {
        "expected": 3,
        "uploadable": 0,
        "missing": 2,
        "not_applicable": 0,
        "pending_review": 1,
    }
    review = json.loads(
        applicability_review_path(run_dir).read_text(encoding="utf-8")
    )
    assert review["schema_version"] == EXTERNAL_APPLICABILITY_REVIEW_SCHEMA
    assert review["status"] == "draft_requires_user_approval"
    assert len(review["entries"]) == 3
    ready = next(
        entry for entry in review["entries"]
        if entry["checkpoint"]["scenario_id"] == PROFILE_SCENARIO
        and entry["checkpoint"]["checkpoint_id"] == "cp1"
    )
    assert ready["evidence"]["checkpoint_present"] is True
    assert ready["evidence"]["answer_present"] is True
    assert ready["evidence"]["request_boundary_present"] is True
    assert ready["evidence"]["retrieval"][0]["tool_call_id"] == SEARCH_CALL
    assert ready["reference"]["answer"]
    assert ready["reference"]["must_include"]
    assert {row["metric"] for row in ready["previous_applicability"]} == {
        "faithfulness",
        "factual_correctness",
    }
    assert [row["applicable"] for row in ready["metrics"]] == [None, None]
    assert ready["scenario"]["family"]
    assert ready["scenario"]["message"]
    missing = next(
        entry for entry in review["entries"]
        if entry["checkpoint"]["scenario_id"] == FAILED_SCENARIO
    )
    assert missing["evidence"]["checkpoint_present"] is False
    assert missing["evidence"]["runner_error"]
    assert missing["evidence"]["readiness"]["status"] == "missing"
    assert (
        applicability_review_path(run_dir).is_file()
        and not applicability_path(run_dir).exists()
    )
    with pytest.raises(UploadBlocked, match="缺少已批准"):
        check_external_projection(run_dir, dataset_dir=DEFAULT_VERSION_DIR)
    shutil.copyfile(
        applicability_review_path(run_dir), applicability_path(run_dir)
    )
    with pytest.raises(UploadBlocked, match="待审阅清单"):
        check_external_projection(run_dir, dataset_dir=DEFAULT_VERSION_DIR)
    for path, content in before.items():
        assert path.read_bytes() == content


def test_projection_maps_contract_and_keeps_audit_separate(run_dir, dataset):
    evidence = load_external_run(run_dir, dataset)
    decisions = default_decisions(evidence)
    write_approval(
        run_dir, approval_entries(evidence, decisions), dataset
    )
    execution_paths = {
        path: path.read_bytes()
        for path in (run_dir / "scenarios").glob("**/execution.json")
    }
    scoring_paths = {
        path: path.read_bytes()
        for path in (run_dir / "scenarios").glob("**/external-scoring.json")
    }
    result = check_external_projection(run_dir, dataset_dir=DEFAULT_VERSION_DIR)
    assert result["status"] == "offline_projection_checked"
    assert result["network"] == "not_called"
    assert result["rows"] == 1
    assert result["coverage"] == {
        "expected": 3,
        "uploadable": 1,
        "missing": 1,
        "not_applicable": 1,
        "pending_review": 0,
    }
    payload = json.loads(
        (run_dir / "external-judge" / EXTERNAL_PAYLOAD_NAME).read_text(
            encoding="utf-8"
        )
    )
    assert payload["schema"] == EXTERNAL_PAYLOAD_SCHEMA
    assert payload["source_sha256"] == result["source_sha256"]
    assert payload["applicability_sha256"] == result["applicability_sha256"]
    approval_document = json.loads(
        applicability_path(run_dir).read_text(encoding="utf-8")
    )
    assert (
        payload["applicability_sha256"]
        == approval_document["approval"]["content_sha256"]
    )
    assert payload["dataset_name"] == (
        f"fit-agent-external-judge-langfuse-v2-{RUN_ID}-"
        f"{payload['applicability_sha256'][:12]}"
    )
    assert payload["experiment_name"] == (
        f"fit-agent-external-judge-langfuse-v2-{RUN_ID}-"
        f"{payload['projections_sha256'][:12]}"
    )
    assert payload_digest(payload) == result["payload_sha256"]
    row = payload["rows"][0]
    upload = row["upload"]
    assert upload["schema_version"] == "fit-agent-external-judge-checkpoint-1"
    assert upload["metadata"]["dataset"] == dataset_identity(dataset).model_dump(
        mode="json"
    )
    projected = next(
        checkpoint for checkpoint in evidence.checkpoints
        if checkpoint.link.scenario_id == PROFILE_SCENARIO
        and checkpoint.link.checkpoint_id == "cp1"
    )
    assert (
        upload["metadata"]["execution_sha256"]
        == projected.execution_sha256
    )
    assert (
        upload["metadata"]["checkpoint"]
        == upload["inputs"]["user_request"]["checkpoint"]
    )
    assert row["row_id"] == str(
        uuid5(
            NAMESPACE_URL,
            "fit-agent-external-judge-langfuse-v2:"
            f"{RUN_ID}:{PROFILE_SCENARIO}:e01:cp1:"
            f"{upload['metadata']['execution_sha256']}",
        )
    )
    assert row["projection_sha256"] == sha256_text(canonical_json(upload))
    assert upload["inputs"]["user_request"]["message"] == scenario_by_id(
        dataset, PROFILE_SCENARIO
    )["input"]["message"]
    assert [
        item["role"] for item in upload["inputs"]["user_request"]["prior_messages"]
    ] == ["user", "assistant"]
    dataset_input = row["dataset_input"]
    assert set(dataset_input) == {"display", "user_request"}
    assert dataset_input["user_request"]["message"] == (
        upload["inputs"]["user_request"]["message"]
    )
    assert "checkpoint" not in dataset_input["user_request"]
    assert dataset_input["display"]["scenario_id"] == PROFILE_SCENARIO
    assert dataset_input["display"]["event_id"] == "e01"
    assert dataset_input["display"]["checkpoint_id"] == "cp1"
    assert dataset_input["display"]["question"] == (
        upload["inputs"]["user_request"]["message"]
    )
    assert dataset_input["display"]["scenario_label"]
    assert dataset_input["display"]["title"].startswith(PROFILE_SCENARIO)
    answer_text = upload["outputs"]["answer"]["visible_text"]
    assert LOCAL_PATH not in answer_text
    assert SESSION_PATH not in answer_text
    assert "local:" in answer_text and "attachment:" in answer_text
    assert answer_text == outbound_text(
        next(
            checkpoint.entry.checkpoint.answer.visible_text
            for checkpoint in evidence.checkpoints
            if checkpoint.link.scenario_id == PROFILE_SCENARIO
            and checkpoint.link.checkpoint_id == "cp1"
        )
    )
    assert upload["outputs"]["answer"]["structured_snapshot"]["stop_reason"] == (
        "end_turn"
    )
    context = upload["outputs"]["context"]
    assert context["truncated"] is True
    assert context["omitted_chars"] == 7
    truncated = next(
        item for item in context["items"] if item["tool_name"] == "read"
    )
    assert truncated["truncated"] is True
    assert truncated["omitted_chars"] == 3
    assert truncated["original_chars"] == len(truncated["content"]) + 3
    assert context["request_boundary"]["request_entry_id"] == "req-e01"
    assert len(context["retrieval"]) == 1
    retrieval = context["retrieval"][0]
    assert retrieval["tool_call_id"] == SEARCH_CALL
    assert retrieval["tool_name"] == SEARCH_TOOL
    assert retrieval["search"]["response_json"] == search_response()
    rerank = retrieval["rerank"]
    assert rerank["identity"]["endpoint"] == sha256_text(RERANK_ENDPOINT)
    assert rerank["scored"][0]["id"] == "0030"
    assert rerank["usage"]["requests"] == 1
    assert rerank["request_id"] == "rerank-request-1"
    assert RERANK_ENDPOINT not in canonical_json(payload)
    audit = row["audit"]
    assert audit["retrieval"][0]["candidate_pool"] == ["0025", "0030", "0036"]
    assert audit["retrieval"][0]["rerank_scores"][0]["rerank_score"] == 0.9
    assert "rerank_score" not in canonical_json(context["items"])
    assert "private" not in canonical_json(audit)
    reference = upload["reference_outputs"]["reference"]
    planned = generation_reference(
        scenario_by_id(dataset, PROFILE_SCENARIO), "cp1"
    )
    assert reference == planned.model_dump(mode="json")
    assert {
        row["metric"] for row in upload["reference_outputs"]["applicability"]
    } == {"evidence_supported_v1", "reference_requirements_met_v1"}
    assert all(
        row["applicable"]
        for row in upload["reference_outputs"]["applicability"]
    )
    ledger = json.loads(
        (run_dir / "external-judge" / "coverage-ledger.json").read_text(
            encoding="utf-8"
        )
    )
    missing = next(
        entry for entry in ledger["entries"]
        if entry["classification"] == "missing"
    )
    assert missing["checkpoint"]["scenario_id"] == FAILED_SCENARIO
    assert missing["evidence_status"] == "missing"
    assert missing["evidence_missing"] == ["generation_runner_error"]
    for path, content in {**execution_paths, **scoring_paths}.items():
        assert path.read_bytes() == content
    assert not (run_dir / "generation-payload.json").exists()
    assert not (run_dir / "langsmith-generation.json").exists()
    with pytest.raises(SummaryError):
        build_upload_payload(run_dir)


def test_target_returns_only_frozen_outputs(run_dir, dataset):
    evidence = load_external_run(run_dir, dataset)
    decisions = default_decisions(evidence)
    write_approval(
        run_dir, approval_entries(evidence, decisions), dataset
    )
    payload, _ = build_external_payload(
        evidence, load_external_applicability(evidence)
    )
    target = _FrozenCheckpointTarget(payload)
    row = payload["rows"][0]
    expected = _frozen_run_output(row)
    assert set(expected) == {"answer", "context", "reference"}
    assert (
        expected["reference"]
        == row["upload"]["reference_outputs"]["reference"]
    )
    outputs = target(item=SimpleNamespace(input=deepcopy(row["dataset_input"])))
    assert outputs == expected
    dict_item = {"input": deepcopy(row["dataset_input"])}
    assert target(item=dict_item) == expected
    outputs["answer"]["visible_text"] = "被篡改"
    assert (
        target(item=SimpleNamespace(input=row["dataset_input"]))[
            "answer"
        ]["visible_text"]
        != "被篡改"
    )
    with pytest.raises(UploadBlocked, match="未知输入"):
        target(item=SimpleNamespace(input={"user_request": {"message": "x"}}))


def test_applicability_rejections(run_dir, dataset):
    evidence = load_external_run(run_dir, dataset)
    decisions = default_decisions(evidence)
    entries = approval_entries(evidence, decisions)
    write_approval(run_dir, entries, dataset)
    with pytest.raises(UploadBlocked, match="缺少检查点"):
        write_approval(run_dir, entries[:-1], dataset)
        load_external_applicability(evidence)
    unknown = deepcopy(entries)
    unknown[0] = unknown[0].model_copy(
        update={
            "checkpoint": EvidenceLink(
                run_id=RUN_ID,
                scenario_id=unknown[0].checkpoint.scenario_id,
                event_id=unknown[0].checkpoint.event_id,
                checkpoint_id="cp-unknown",
            )
        }
    )
    with pytest.raises(UploadBlocked, match="未知检查点"):
        write_approval(run_dir, unknown, dataset)
        load_external_applicability(evidence)
    with pytest.raises(UploadBlocked, match="执行摘要冲突"):
        conflicted = deepcopy(entries)
        conflicted[0] = conflicted[0].model_copy(
            update={"execution_sha256": "0" * 64}
        )
        write_approval(run_dir, conflicted, dataset)
        load_external_applicability(evidence)
    with pytest.raises(UploadBlocked, match="批准摘要"):
        write_approval(
            run_dir, entries, dataset, content_sha256="0" * 64
        )
        load_external_applicability(evidence)
    with pytest.raises(UploadBlocked, match="检查点身份冲突"):
        conflicted = deepcopy(entries)
        conflicted[0] = conflicted[0].model_copy(
            update={
                "checkpoint": conflicted[0].checkpoint.model_copy(
                    update={"run_id": "other-run"}
                )
            }
        )
        write_approval(run_dir, conflicted, dataset)
        load_external_applicability(evidence)
    write_approval(run_dir, entries, dataset)
    raw = json.loads(
        applicability_path(run_dir).read_text(encoding="utf-8")
    )
    duplicate = deepcopy(raw)
    duplicate["entries"].append(deepcopy(duplicate["entries"][0]))
    refresh_raw_approval(duplicate)
    write_json(applicability_path(run_dir), duplicate)
    with pytest.raises(UploadBlocked, match="不能重复"):
        load_external_applicability(evidence)
    missing_metric = deepcopy(raw)
    missing_metric["entries"][0]["applicability"].pop()
    refresh_raw_approval(missing_metric)
    write_json(applicability_path(run_dir), missing_metric)
    with pytest.raises(UploadBlocked, match="适用性输入无法解析"):
        load_external_applicability(evidence)
    no_reason = deepcopy(raw)
    no_reason["entries"][0]["applicability"][0]["applicable"] = False
    no_reason["entries"][0]["applicability"][0]["reason"] = None
    refresh_raw_approval(no_reason)
    write_json(applicability_path(run_dir), no_reason)
    with pytest.raises(UploadBlocked, match="不适用指标必须写明原因"):
        load_external_applicability(evidence)
    pending = deepcopy(raw)
    pending["approval"]["status"] = "pending"
    refresh_raw_approval(pending)
    write_json(applicability_path(run_dir), pending)
    with pytest.raises(UploadBlocked, match="适用性输入无法解析"):
        load_external_applicability(evidence)
    assert len(entries) == 3


def test_retrieval_must_bind_visible_tool_results(tmp_path, dataset):
    scenario = scenario_by_id(dataset, PROFILE_SCENARIO)
    boundary = full_boundary(RUN_ID, scenario)
    checkpoint = full_checkpoint(RUN_ID, scenario, boundary)
    base = profile_execution(dataset).model_dump(mode="json")
    base["generation_checkpoints"] = [
        {
            **base["generation_checkpoints"][0],
            "checkpoint": checkpoint.model_dump(mode="json"),
            "request_boundary": boundary.model_dump(mode="json"),
        },
        base["generation_checkpoints"][1],
    ]
    record = retrieval_record(RUN_ID, PROFILE_SCENARIO)
    record = record.model_copy(update={"tool_call_id": "call-not-visible"})
    base["retrieval"] = [record.model_dump(mode="json")]
    execution = ScenarioExecution.model_validate(base)
    execution = _seal_execution(execution)
    run_dir = tmp_path / "run" / RUN_ID
    scenario_root = run_dir / "scenarios" / "example" / PROFILE_SCENARIO
    write_json(
        scenario_root / "execution.json",
        execution.model_dump(mode="json"),
    )
    write_json(
        scenario_root / "external-scoring.json",
        score_execution_external(
            execution, scenario["assertions"]
        ).model_dump(mode="json"),
    )
    write_json(
        run_dir / "sdk-run.json",
        {
            "schema": "fit-agent-scenario-langfuse-1",
            "run_id": RUN_ID,
            "purpose": "validation",
            "feedback_version": 3,
            "scoring_mode": "external",
            "protocol_version": EXTERNAL_PROTOCOL_VERSION,
            "status": "incomplete",
            "dataset": dataset.identity(),
            "sample_scope": {
                "scenario_ids": [PROFILE_SCENARIO],
                "count": 1,
                "selection": [],
            },
            "samples": [
                {
                    "example_id": "example",
                    "scenario_id": PROFILE_SCENARIO,
                    "sdk_run_id": "sdk",
                    "status": "complete",
                    "execution_sha256": execution.execution_sha256,
                    "execution_status": execution.status,
                    "scoring_mode": "external",
                    "feedback_version": 3,
                    "scoring_relative": (
                        f"scenarios/example/{PROFILE_SCENARIO}/"
                        "external-scoring.json"
                    ),
                    "scoring_sha256": sha256_text(
                        (scenario_root / "external-scoring.json").read_text(
                            encoding="utf-8"
                        )
                    ),
                    "run_error": None,
                    "incomplete": [],
                }
            ],
            "example_ids": {PROFILE_SCENARIO: "example"},
            "coverage": {},
            "issues": [],
        },
    )
    evidence = load_external_run(run_dir, dataset)
    assert evidence.checkpoints[0].retrieval == ()
    decisions = default_decisions(evidence)
    write_approval(
        run_dir, approval_entries(evidence, decisions), dataset
    )
    with pytest.raises(UploadBlocked, match="缺少检索证据"):
        build_external_payload(
            evidence, load_external_applicability(evidence)
        )


def test_failed_retrieval_tool_stays_in_context_without_search_evidence(dataset):
    scenario = scenario_by_id(dataset, PROFILE_SCENARIO)
    execution = profile_execution(dataset)
    entry = execution.generation_checkpoints[0]
    error_text = json.dumps(
        {"code": "tool_execution_failed", "message": "工具执行失败。"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    items = [
        item.model_copy(update={"content": error_text})
        if item.tool_call_id == SEARCH_CALL
        else item
        for item in entry.checkpoint.model_input.items
    ]
    boundary = entry.request_boundary.model_copy(
        update={
            "tool_nodes": [
                node.model_copy(
                    update={"content": error_text, "is_error": True}
                )
                if node.tool_call_id == SEARCH_CALL
                else node
                for node in entry.request_boundary.tool_nodes
            ]
        }
    )
    entry = entry.model_copy(
        update={
            "checkpoint": entry.checkpoint.model_copy(
                update={
                    "model_input": entry.checkpoint.model_input.model_copy(
                        update={"items": items}
                    )
                }
            ),
            "request_boundary": boundary,
        }
    )
    record = score_execution_external(execution, scenario["assertions"])
    checkpoint = ExternalCheckpointEvidence(
        scenario=scenario,
        execution=execution,
        record=record,
        entry=entry,
        link=entry.checkpoint.checkpoint,
        readiness=next(
            row for row in record.generation if row.checkpoint_id == "cp1"
        ),
        retrieval=(),
    )
    retrieval, audit = _external_retrieval_evidence(
        checkpoint, boundary, items
    )
    assert retrieval == []
    assert audit == []
    assert any(
        item.tool_call_id == SEARCH_CALL and item.content == error_text
        for item in items
    )


def test_external_whitelist_ignores_serialized_escape_sequences():
    payload = {
        "rows": [
            {
                "upload": {
                    "outputs": {"context": {"retrieval": []}},
                    "reference_outputs": {
                        "reference": {"answer": "说明:\n\n1. 第一点"}
                    },
                }
            }
        ]
    }
    _assert_external_upload_whitelist(payload)
    payload["rows"][0]["upload"]["reference_outputs"]["reference"]["answer"] = (
        "D:\\datasets\\exercise.json"
    )
    with pytest.raises(UploadBlocked, match="本地路径"):
        _assert_external_upload_whitelist(payload)


def test_generation_checkpoint_does_not_cover_missing_assertions(dataset):
    scenario = scenario_by_id(dataset, PROFILE_SCENARIO)
    execution = profile_execution(dataset)
    assert {
        row.checkpoint_id
        for row in execution.generation_checkpoints
        if row.checkpoint is not None
    } == {"cp1"}
    record = score_execution_external(execution, scenario["assertions"])
    assert record.assertion_status == "incomplete"
    assert record.missing_assertion_checkpoint_ids == ["cp1", "cp2"]
    assert "e01/cp1:assertion_missing" in record.incomplete
    assert "e03/cp2:assertion_missing" in record.incomplete


def test_assertion_definition_gap_marks_checkpoint_incomplete(dataset):
    scenario = scenario_by_id(dataset, PROFILE_SCENARIO)
    definitions = [
        row for row in scenario["assertions"] if row["event_id"] == "e01"
    ]
    base = profile_execution(dataset).model_dump(mode="json")
    base["assertion_checkpoints"] = [
        {
            "checkpoint_id": "cp1",
            "event_id": "e01",
            "assertions": deepcopy(definitions[:2]),
            "binding_error": "测试绑定缺失",
        }
    ]
    execution = _seal_execution(ScenarioExecution.model_validate(base))
    record = score_execution_external(execution, scenario["assertions"])
    assert "e01/cp1:assertion_definition_mismatch" in record.incomplete
    assert "cp1" in record.missing_assertion_checkpoint_ids


def multi_turn_execution(dataset) -> ScenarioExecution:
    scenario = scenario_by_id(dataset, PROFILE_SCENARIO)
    base = profile_execution(dataset).model_dump(mode="json")
    applicability = [
        MetricApplicability(metric="faithfulness", applicable=True),
        MetricApplicability(metric="factual_correctness", applicable=True),
    ]
    checkpoint = GenerationCheckpoint(
        checkpoint=EvidenceLink(
            run_id=RUN_ID,
            scenario_id=PROFILE_SCENARIO,
            event_id="e03",
            checkpoint_id="cp2",
        ),
        user_input=UserInputEvidence(message=scenario["input"]["message"]),
        answer=AnswerEvidence(visible_text="已按确认保存。"),
        model_input=ModelInputContext(
            items=[
                ContextItem(
                    source="tool_result",
                    content=search_text(),
                    fact_origin="tool",
                    tool_call_id=SEARCH_CALL,
                    tool_name=SEARCH_TOOL,
                )
            ]
        ),
        reference=GenerationReference(answer="已按确认保存。"),
        applicability=applicability,
    )
    boundary = RequestBoundaryEvidence(
        api="openai-completions",
        session_id="session-1",
        run_id=RUN_ID,
        request_entry_id=None,
        nodes=[],
        tool_nodes=[
            ToolNode(
                tool_call_id=SEARCH_CALL,
                tool_name=SEARCH_TOOL,
                content=search_text(),
                entry_id="entry-search",
                session_id="session-1",
                run_id=RUN_ID,
                is_error=False,
            )
        ],
    )
    base["generation_checkpoints"] = [
        base["generation_checkpoints"][0],
        {
            "checkpoint_id": "cp2",
            "event_id": "e03",
            "applicability": [
                row.model_dump(mode="json") for row in applicability
            ],
            "checkpoint": checkpoint.model_dump(mode="json"),
            "request_boundary": boundary.model_dump(mode="json"),
        },
    ]
    return _seal_execution(ScenarioExecution.model_validate(base))


def write_single_scenario_run(tmp_path, dataset, execution) -> Path:
    scenario = scenario_by_id(dataset, execution.scenario_id)
    run_dir = tmp_path / "run" / RUN_ID
    scenario_root = run_dir / "scenarios" / "example" / execution.scenario_id
    execution = attach_model_calls(execution, scenario_root)
    write_json(
        scenario_root / "execution.json",
        execution.model_dump(mode="json"),
    )
    record = score_execution_external(execution, scenario["assertions"])
    scoring_path = scenario_root / "external-scoring.json"
    write_json(scoring_path, record.model_dump(mode="json"))
    write_json(
        run_dir / "sdk-run.json",
        {
            "schema": "fit-agent-scenario-langfuse-1",
            "run_id": RUN_ID,
            "purpose": "validation",
            "feedback_version": 3,
            "scoring_mode": "external",
            "protocol_version": EXTERNAL_PROTOCOL_VERSION,
            "status": "incomplete",
            "dataset": dataset.identity(),
            "sample_scope": {
                "scenario_ids": [execution.scenario_id],
                "count": 1,
                "selection": [],
            },
            "samples": [
                {
                    "example_id": "example",
                    "scenario_id": execution.scenario_id,
                    "sdk_run_id": "sdk",
                    "status": "complete",
                    "execution_sha256": execution.execution_sha256,
                    "execution_status": execution.status,
                    "scoring_mode": "external",
                    "feedback_version": 3,
                    "scoring_relative": (
                        f"scenarios/example/{execution.scenario_id}/"
                        "external-scoring.json"
                    ),
                    "scoring_sha256": sha256_text(
                        scoring_path.read_text(encoding="utf-8")
                    ),
                    "run_error": None,
                    "incomplete": list(record.incomplete),
                }
            ],
            "example_ids": {execution.scenario_id: "example"},
            "coverage": {},
            "issues": [],
        },
    )
    return run_dir


def test_prior_event_retrieval_enters_later_checkpoint_projection(tmp_path, dataset):
    execution = multi_turn_execution(dataset)
    run_dir = write_single_scenario_run(tmp_path, dataset, execution)
    evidence = load_external_run(run_dir, dataset)
    checkpoint = next(
        item for item in evidence.checkpoints if item.link.checkpoint_id == "cp2"
    )
    assert [record.event_id for record in checkpoint.retrieval] == ["e01"]
    decisions = default_decisions(evidence)
    decisions[checkpoint.key] = [
        ExternalJudgeApplicability(
            metric="evidence_supported_v1", applicable=True
        ),
        ExternalJudgeApplicability(
            metric="reference_requirements_met_v1", applicable=True
        ),
    ]
    write_approval(run_dir, approval_entries(evidence, decisions), dataset)
    approved = load_external_applicability(evidence)
    upload, audit = build_external_projection(
        checkpoint, approved[checkpoint.key]
    )
    assert upload["outputs"]["context"]["retrieval"][0]["tool_call_id"] == (
        SEARCH_CALL
    )
    assert upload["outputs"]["context"]["retrieval"][0]["search"][
        "response_json"
    ] == search_response()
    assert audit["retrieval"][0]["event_id"] == "e01"


def test_judge_state_uses_visible_context_items_only(run_dir, dataset):
    evidence = load_external_run(run_dir, dataset)
    write_approval(
        run_dir, approval_entries(evidence, default_decisions(evidence)), dataset
    )
    check_external_projection(run_dir, dataset_dir=DEFAULT_VERSION_DIR)
    payload = json.loads(
        (run_dir / "external-judge" / EXTERNAL_PAYLOAD_NAME).read_text(
            encoding="utf-8"
        )
    )
    row = payload["rows"][0]
    upload = row["upload"]
    judge_state = {
        "user_request": row["dataset_input"]["user_request"],
        "answer": upload["outputs"]["answer"],
        "context": upload["outputs"]["context"]["items"],
        "reference": upload["reference_outputs"]["reference"],
    }
    rendered = canonical_json(judge_state)
    assert "xxx" not in rendered
    assert "rerank_score" not in rendered
    assert "candidate_pool" not in rendered
    read_item = next(
        item for item in judge_state["context"] if item["tool_name"] == "read"
    )
    assert read_item["content"] == "动作详情：杠铃卧推，水平推举。"
    context = upload["outputs"]["context"]
    assert "xxx" in canonical_json(context["request_boundary"])
    assert "rerank_score" in canonical_json(context["retrieval"])


def test_source_identity_rejections(run_dir, dataset, tmp_path):
    def clone(name):
        target = tmp_path / name / RUN_ID
        shutil.copytree(run_dir, target)
        return target

    def sdk_path(path):
        return path / "sdk-run.json"

    ragas = clone("ragas")
    document = json.loads(sdk_path(ragas).read_text(encoding="utf-8"))
    document.update(
        scoring_mode="ragas",
        feedback_version=2,
        protocol_version="fit-agent-scenario-ragas-protocol-1",
    )
    write_json(sdk_path(ragas), document)
    with pytest.raises(UploadBlocked, match="不是 external"):
        load_external_run(ragas, dataset)

    missing = clone("missing")
    next((missing / "scenarios").glob("**/external-scoring.json")).unlink()
    with pytest.raises(UploadBlocked, match="外部评分证据缺失"):
        load_external_run(missing, dataset)

    tampered = clone("tampered")
    execution_path = next((tampered / "scenarios").glob("**/execution.json"))
    execution = json.loads(execution_path.read_text(encoding="utf-8"))
    execution["answer"] = "changed"
    write_json(execution_path, execution)
    with pytest.raises(UploadBlocked, match="执行身份"):
        load_external_run(tampered, dataset)

    sample = clone("sample")
    document = json.loads(sdk_path(sample).read_text(encoding="utf-8"))
    document["samples"][0]["scoring_sha256"] = "0" * 64
    write_json(sdk_path(sample), document)
    with pytest.raises(UploadBlocked, match="评分摘要"):
        load_external_run(sample, dataset)

    running = clone("running")
    document = json.loads(sdk_path(running).read_text(encoding="utf-8"))
    document["status"] = "running"
    write_json(sdk_path(running), document)
    with pytest.raises(UploadBlocked, match="仍在进行"):
        load_external_run(running, dataset)

    wrong_dataset = clone("dataset")
    document = json.loads(sdk_path(wrong_dataset).read_text(encoding="utf-8"))
    document["dataset"]["content_sha256"] = "0" * 64
    write_json(sdk_path(wrong_dataset), document)
    with pytest.raises(UploadBlocked, match="数据身份"):
        load_external_run(wrong_dataset, dataset)

    partial = clone("partial")
    document = json.loads(sdk_path(partial).read_text(encoding="utf-8"))
    document["samples"] = document["samples"][:1]
    write_json(sdk_path(partial), document)
    with pytest.raises(UploadBlocked, match="缩小评分分母"):
        load_external_run(partial, dataset)


def test_example_and_receipt_identity_are_verified_once(run_dir, dataset):
    evidence = load_external_run(run_dir, dataset)
    decisions = default_decisions(evidence)
    write_approval(
        run_dir, approval_entries(evidence, decisions), dataset
    )
    payload, _ = build_external_payload(
        evidence, load_external_applicability(evidence)
    )
    row = payload["rows"][0]
    document = _example_document(payload, row)
    checkpoint = row["upload"]["metadata"]["checkpoint"]
    assert document["metadata"] == {
        **{
            "schema": EXTERNAL_PAYLOAD_SCHEMA,
            "presentation_version": EXTERNAL_PRESENTATION_VERSION,
            "row_id": row["row_id"],
            "dataset": payload["dataset"],
            "checkpoint": checkpoint,
            "run_id": checkpoint["run_id"],
            "scenario_id": checkpoint["scenario_id"],
            "event_id": checkpoint["event_id"],
            "checkpoint_id": checkpoint["checkpoint_id"],
            "display": row["dataset_input"]["display"],
            "execution_sha256": row["upload"]["metadata"]["execution_sha256"],
            "projection_sha256": row["projection_sha256"],
        },
        "judge_metric_evidence_supported_v1": "applicable",
        "judge_metric_reference_requirements_met_v1": "applicable",
    }
    example = SimpleNamespace(
        id=document["id"],
        input=document["input"],
        expected_output=document["expected_output"],
        metadata=document["metadata"],
    )
    _verify_external_examples(payload, {str(example.id): example})
    tampered = example.__class__(
        **{**example.__dict__, "expected_output": {"reference": {}}}
    )
    with pytest.raises(UploadBlocked, match="expected_output"):
        _verify_external_examples(payload, {str(tampered.id): tampered})
    flag_tampered = example.__class__(
        **{
            **example.__dict__,
            "metadata": {
                **document["metadata"],
                "judge_metric_evidence_supported_v1": "not_applicable",
            },
        }
    )
    with pytest.raises(UploadBlocked, match="evidence_supported_v1"):
        _verify_external_examples(
            payload, {str(flag_tampered.id): flag_tampered}
        )
    receipt = {
        "schema": EXTERNAL_DATASET_RECEIPT_SCHEMA,
        "run_id": payload["run_id"],
        "source_sha256": payload["source_sha256"],
        "payload_sha256": payload_digest(payload),
        "applicability_sha256": payload["applicability_sha256"],
        "rows_sha256": payload["rows_sha256"],
        "dataset_id": "dataset-1",
    }
    _external_receipt_identity(
        receipt, payload, schema=EXTERNAL_DATASET_RECEIPT_SCHEMA
    )
    receipt["payload_sha256"] = "0" * 64
    with pytest.raises(UploadBlocked, match="提交身份"):
        _external_receipt_identity(
            receipt, payload, schema=EXTERNAL_DATASET_RECEIPT_SCHEMA
        )


def test_remote_runs_must_match_frozen_outputs(run_dir, dataset):
    evidence = load_external_run(run_dir, dataset)
    decisions = default_decisions(evidence)
    write_approval(
        run_dir, approval_entries(evidence, decisions), dataset
    )
    payload, _ = build_external_payload(
        evidence, load_external_applicability(evidence)
    )
    row = payload["rows"][0]

    def run(outputs=None, level="DEFAULT"):
        return ExperimentItem(
            id=str(uuid4()),
            trace_id=str(uuid4()),
            start_time=datetime.now(UTC),
            end_time=None,
            level=level,
            environment="default",
            experiment_id="exp-1",
            experiment_name="experiment",
            experiment_item_id=row["row_id"],
            input=row["dataset_input"],
            output=_frozen_run_output(row) if outputs is None else outputs,
        )

    missing, mapping = _collect_external_runs(payload, [run()])
    assert not missing
    assert mapping[row["row_id"]]
    with pytest.raises(UploadBlocked, match="多个实验条目"):
        _collect_external_runs(payload, [run(), run()])
    with pytest.raises(UploadBlocked, match="远端运行失败"):
        _collect_external_runs(payload, [run(level="ERROR")])
    with pytest.raises(UploadBlocked, match="outputs"):
        _collect_external_runs(payload, [run(outputs={"answer": {}})])
    with pytest.raises(UploadBlocked, match="inputs"):
        tampered_input = run().model_copy(update={"input": {}})
        _collect_external_runs(payload, [tampered_input])


VERIFY_STAMP = datetime(2026, 1, 1, tzinfo=UTC)


class MemoryDataset:
    def __init__(self, name: str, metadata: dict, project_id: str) -> None:
        self.id = str(uuid4())
        self.name = name
        self.metadata = deepcopy(metadata)
        self.project_id = project_id
        self.items_by_id: dict[str, MemoryDatasetItem] = {}

    @property
    def items(self) -> list["MemoryDatasetItem"]:
        return list(self.items_by_id.values())


class MemoryDatasetItem:
    def __init__(
        self, dataset: MemoryDataset, row: dict, *, item_id: str | None = None
    ) -> None:
        self.id = item_id if item_id is not None else row["id"]
        self.dataset_id = dataset.id
        self.dataset_name = dataset.name
        self.input = deepcopy(row["input"])
        self.expected_output = deepcopy(row["expected_output"])
        self.metadata = deepcopy(row["metadata"])


class MemoryExperiment:
    def __init__(self, experiment: Experiment, items: list[ExperimentItem]) -> None:
        self.experiment = experiment
        self.items = items


class MemoryExperimentsApi:
    def __init__(self, client: "MemoryClient") -> None:
        self._client = client

    def list(
        self,
        *,
        from_start_time,
        id=None,
        name=None,
        dataset_id=None,
        fields=None,
        cursor=None,
        **kwargs,
    ):
        matches = [
            entry.experiment
            for entry in self._client.experiments.values()
            if id is None or entry.experiment.id == id
        ]
        matches = [
            experiment for experiment in matches if name is None or experiment.name == name
        ]
        return ExperimentsResponse(
            data=matches, meta=ExperimentsResponseMeta(cursor=None)
        )

    def list_items(
        self,
        *,
        from_start_time,
        experiment_id=None,
        fields=None,
        cursor=None,
        **kwargs,
    ):
        entry = self._client.experiments[experiment_id]
        return ExperimentItemsResponse(
            data=list(entry.items), meta=ExperimentsResponseMeta(cursor=None)
        )


class MemoryClient:
    # 覆盖 generation_upload 外部链路使用的 Langfuse client 方法面。
    _base_url = "https://cloud.langfuse.test"
    project_id = "project-1"

    def __init__(self) -> None:
        self.datasets: dict[str, MemoryDataset] = {}
        self.experiments: dict[str, MemoryExperiment] = {}
        self.task_calls = 0
        self.api = SimpleNamespace(experiments=MemoryExperimentsApi(self))

    def get_dataset(self, name: str) -> MemoryDataset:
        remote = self.datasets.get(name)
        if remote is None:
            raise NotFoundError({})
        return remote

    def create_dataset(self, *, name, description=None, metadata=None, **kwargs):
        assert name not in self.datasets, "重复创建同名远端数据集"
        remote = MemoryDataset(name, metadata or {}, self.project_id)
        self.datasets[name] = remote
        return remote

    def create_dataset_item(
        self,
        *,
        dataset_name,
        id=None,
        input=None,
        expected_output=None,
        metadata=None,
        **kwargs,
    ):
        remote = self.datasets[dataset_name]
        existing = remote.items_by_id.get(id)
        if existing is not None:
            assert (
                existing.input == input
                and existing.expected_output == expected_output
                and existing.metadata == metadata
            ), "同 id 重复提交且内容不一致"
            return existing
        item = MemoryDatasetItem(
            remote,
            {
                "id": id,
                "input": input,
                "expected_output": expected_output,
                "metadata": metadata,
            },
        )
        remote.items_by_id[id] = item
        return item

    def run_experiment(
        self,
        *,
        name,
        run_name=None,
        description=None,
        data,
        task,
        evaluators=None,
        run_evaluators=None,
        max_concurrency=50,
        metadata=None,
        experiment_id=None,
    ):
        # 模拟 SDK：hosted item 携带 dataset_id，同 dataset 同 run_name 派生同一实验 id。
        dataset_id = data[0].dataset_id
        resolved = experiment_id
        if resolved is None:
            material = json.dumps(
                ["langfuse-experiment-v1", self.project_id, dataset_id, run_name],
                separators=(",", ":"),
                ensure_ascii=False,
            )
            resolved = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
        items = []
        for item in data:
            self.task_calls += 1
            output = task(item=item)
            items.append(
                ExperimentItem(
                    id=str(uuid4()),
                    trace_id=str(uuid4()),
                    start_time=VERIFY_STAMP,
                    end_time=VERIFY_STAMP,
                    level="DEFAULT",
                    environment="default",
                    experiment_id=resolved,
                    experiment_name=run_name,
                    experiment_item_id=item.id,
                    input=item.input,
                    output=output,
                )
            )
        experiment = Experiment(
            id=resolved,
            name=run_name,
            description=description,
            start_time=VERIFY_STAMP,
            end_time=VERIFY_STAMP,
            item_count=len(items),
            dataset_id=dataset_id,
            metadata=deepcopy(metadata),
        )
        existing = self.experiments.get(resolved)
        if existing is not None:
            existing.items.extend(items)
        else:
            self.experiments[resolved] = MemoryExperiment(experiment, items)
        return SimpleNamespace(
            experiment_id=resolved,
            run_name=run_name,
            experiment_url=(
                f"{self._base_url}/project/{self.project_id}"
                f"/experiments/results?baseline={resolved}"
            ),
            item_results=[],
        )


def approve_default(run_dir: Path, dataset) -> None:
    evidence = load_external_run(run_dir, dataset)
    write_approval(
        run_dir, approval_entries(evidence, default_decisions(evidence)), dataset
    )


def test_external_dataset_sync_langfuse_lifecycle(run_dir, dataset, monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    approve_default(run_dir, dataset)
    client = MemoryClient()
    first = sync_external_dataset(
        run_dir, dataset_dir=DEFAULT_VERSION_DIR, client=client
    )
    assert first["status"] == "created"
    assert first["example_count"] == 1
    remote = client.datasets[first["dataset_name"]]
    assert first["dataset_url"] == (
        f"https://cloud.langfuse.test/project/{client.project_id}"
        f"/datasets/{remote.id}"
    )
    item = remote.items[0]
    assert item.metadata["judge_metric_evidence_supported_v1"] == "applicable"
    assert (
        item.metadata["judge_metric_reference_requirements_met_v1"]
        == "applicable"
    )
    assert item.metadata["row_id"] == item.id
    assert item.metadata["display"]["question"] == item.input["display"]["question"]
    assert set(item.input) == {"display", "user_request"}
    assert "checkpoint" not in item.input["user_request"]
    original_input = deepcopy(item.input)
    original_metadata = deepcopy(item.metadata)
    receipt = json.loads(
        (external_dir(run_dir) / EXTERNAL_DATASET_RECEIPT_NAME).read_text(
            encoding="utf-8"
        )
    )
    assert receipt["schema"] == EXTERNAL_DATASET_RECEIPT_SCHEMA
    assert receipt["dataset_id"] == str(remote.id)
    assert receipt["item_ids"] == [item.id]
    web_judge = receipt["web_judge"]
    assert web_judge["scope"]["dataset_id"] == receipt["dataset_id"]
    assert web_judge["scope"]["observation_scope"] == "isExperimentItemRootSpan"
    assert web_judge["scope"]["sampling"] == 1.0
    assert {
        row["metric"] for row in web_judge["evaluators"]
    } == {
        "evidence_supported_v1",
        "reference_requirements_met_v1",
    }
    for evaluator in web_judge["evaluators"]:
        assert evaluator["rule_filters"] == external_rule_filters(
            evaluator["metric"], receipt["dataset_id"]
        )
    assert web_judge["variable_mapping"] == {
        "user_request": {"source": "input", "path": "user_request"},
        "answer": {"source": "output", "path": "answer"},
        "context": {"source": "output", "path": "context.items"},
        "reference": {
            "source": "expected_output",
            "path": "reference",
        },
    }
    second = sync_external_dataset(
        run_dir, dataset_dir=DEFAULT_VERSION_DIR, client=client
    )
    assert second["status"] == "verified_existing"
    assert second["dataset_id"] == first["dataset_id"]
    item.input = {"user_request": {"message": "被篡改"}}
    with pytest.raises(UploadBlocked, match="样例 input 不符"):
        sync_external_dataset(run_dir, dataset_dir=DEFAULT_VERSION_DIR, client=client)
    item.input = deepcopy(original_input)
    item.metadata = {
        **item.metadata,
        "judge_metric_evidence_supported_v1": "not_applicable",
    }
    with pytest.raises(UploadBlocked, match="样例 metadata"):
        sync_external_dataset(run_dir, dataset_dir=DEFAULT_VERSION_DIR, client=client)
    item.metadata = {
        key: value
        for key, value in item.metadata.items()
        if key != "judge_metric_evidence_supported_v1"
    }
    with pytest.raises(
        UploadBlocked, match="字段不符: judge_metric_evidence_supported_v1"
    ):
        sync_external_dataset(run_dir, dataset_dir=DEFAULT_VERSION_DIR, client=client)
    # 未知远端样例阻断
    item.metadata = deepcopy(original_metadata)
    unknown = MemoryDatasetItem(
        remote, {"id": str(uuid4()), "input": {}, "expected_output": {}, "metadata": {}}
    )
    remote.items_by_id[unknown.id] = unknown
    with pytest.raises(UploadBlocked, match="未知样例"):
        sync_external_dataset(run_dir, dataset_dir=DEFAULT_VERSION_DIR, client=client)
    remote.items_by_id.pop(unknown.id)
    # 删除回执与远端样例后重同步：远端数据集已存在，仅补齐缺失样例
    (external_dir(run_dir) / EXTERNAL_DATASET_RECEIPT_NAME).unlink()
    remote.items_by_id.pop(item.id)
    third = sync_external_dataset(
        run_dir, dataset_dir=DEFAULT_VERSION_DIR, client=client
    )
    assert third["status"] == "reused"
    assert str(remote.items_by_id[item.id].id) == item.id
    assert (
        remote.items_by_id[item.id].metadata[
            "judge_metric_evidence_supported_v1"
        ]
        == "applicable"
    )


def test_external_submit_langfuse_lifecycle(run_dir, dataset, monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    approve_default(run_dir, dataset)
    client = MemoryClient()
    with pytest.raises(UploadBlocked, match="--confirm-external-judge-cost"):
        submit_external_experiment(
            run_dir, dataset_dir=DEFAULT_VERSION_DIR, confirm_cost=False, client=client
        )
    with pytest.raises(UploadBlocked, match="先同步"):
        submit_external_experiment(
            run_dir, dataset_dir=DEFAULT_VERSION_DIR, confirm_cost=True, client=client
        )
    sync_external_dataset(run_dir, dataset_dir=DEFAULT_VERSION_DIR, client=client)
    result = submit_external_experiment(
        run_dir, dataset_dir=DEFAULT_VERSION_DIR, confirm_cost=True, client=client
    )
    assert result["status"] == "submitted"
    assert result["external_scoring_status"] == "pending_collection"
    assert result["experiment_name"].startswith(
        "fit-agent-external-judge-langfuse-v2-"
    )
    assert result["experiment_url"].endswith(result["experiment_id"])
    row_id = next(iter(result["runs"]))
    assert client.task_calls == 1
    receipt_path = external_dir(run_dir) / EXTERNAL_EXPERIMENT_RECEIPT_NAME
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["schema"] == EXTERNAL_EXPERIMENT_RECEIPT_SCHEMA
    assert receipt["runs"] == result["runs"]
    assert receipt["projections"][row_id]["projection_sha256"]
    assert receipt["web_judge"]["variable_mapping"]["reference"] == {
        "source": "expected_output",
        "path": "reference",
    }
    assert receipt["web_judge"]["scope"]["dataset_id"] == result["dataset_id"]
    assert not (external_dir(run_dir) / EXTERNAL_EXPERIMENT_PENDING_NAME).exists()
    again = submit_external_experiment(
        run_dir, dataset_dir=DEFAULT_VERSION_DIR, confirm_cost=True, client=client
    )
    assert again["status"] == "verified_existing"
    assert again["runs"] == result["runs"]
    assert client.task_calls == 1
    # 中断恢复：删除回执、重建 pending，远端实验完整时同名复用且不重跑
    pending_document = {
        "schema": EXTERNAL_EXPERIMENT_PENDING_SCHEMA,
        "run_id": receipt["run_id"],
        "source_sha256": receipt["source_sha256"],
        "payload_sha256": receipt["payload_sha256"],
        "applicability_sha256": receipt["applicability_sha256"],
        "rows_sha256": receipt["rows_sha256"],
        "projections_sha256": receipt["projections_sha256"],
        "dataset_name": receipt["dataset_name"],
        "dataset_id": receipt["dataset_id"],
        "experiment_name": receipt["experiment_name"],
        "started_at": receipt["submitted_at"],
    }
    receipt_path.unlink()
    write_json(external_dir(run_dir) / EXTERNAL_EXPERIMENT_PENDING_NAME, pending_document)
    recovered = submit_external_experiment(
        run_dir, dataset_dir=DEFAULT_VERSION_DIR, confirm_cost=True, client=client
    )
    assert recovered["status"] == "submitted"
    assert recovered["experiment_id"] == result["experiment_id"]
    assert recovered["runs"] == result["runs"]
    assert client.task_calls == 1
    assert not (external_dir(run_dir) / EXTERNAL_EXPERIMENT_PENDING_NAME).exists()
    # 部分恢复：远端实验缺一条运行，只补齐缺失行
    experiment = client.experiments[result["experiment_id"]]
    experiment.items.clear()
    receipt_path.unlink()
    write_json(external_dir(run_dir) / EXTERNAL_EXPERIMENT_PENDING_NAME, pending_document)
    partial = submit_external_experiment(
        run_dir, dataset_dir=DEFAULT_VERSION_DIR, confirm_cost=True, client=client
    )
    assert partial["status"] == "submitted"
    assert client.task_calls == 2
    assert set(partial["runs"]) == {row_id}
    # 远端实验 metadata 冲突阻断
    experiment.experiment = experiment.experiment.model_copy(update={"metadata": {}})
    receipt_path.unlink()
    write_json(external_dir(run_dir) / EXTERNAL_EXPERIMENT_PENDING_NAME, pending_document)
    with pytest.raises(UploadBlocked, match="远端实验 metadata 不符"):
        submit_external_experiment(
            run_dir, dataset_dir=DEFAULT_VERSION_DIR, confirm_cost=True, client=client
        )


def test_cli_exposes_external_commands_and_cost_gate(tmp_path):
    submit = _parse_args(
        [
            "external-submit",
            "--source",
            str(tmp_path),
            "--confirm-external-judge-cost",
        ]
    )
    assert submit.command == "external-submit"
    assert submit.confirm_external_judge_cost is True
    project = _parse_args(["external-project", "--source", str(tmp_path)])
    assert project.command == "external-project"
    assert project.dataset == DEFAULT_VERSION_DIR
    with pytest.raises(SystemExit):
        _parse_args(["external-project"])
    with pytest.raises(SystemExit):
        _parse_args(
            ["external-project", "--source", str(tmp_path), "--scope", "formal"]
        )


def test_real_production_execution_evidence_projection():
    # 使用生产运行目录中真实封存的执行证据；不构造外部运行摘要，也不写源证据。
    candidates = sorted(
        (BACKEND / "temp" / "scenario-langsmith" / "runs").glob(
            "*/scenarios/*/*/execution.json"
        )
    )
    if not candidates:
        pytest.skip("缺少真实生产执行证据")
    dataset = load_for_evaluation(DEFAULT_VERSION_DIR)
    dataset_scenarios = {
        scenario["scenario_id"]: scenario for scenario in dataset.scenarios
    }
    chosen = None
    for path in candidates:
        try:
            execution = load_execution(path)
        except ExecutionIdentityError:
            # 历史封存证据在模型新增默认字段后无法重算摘要，只能跳过，不能改写源证据。
            continue
        scenario = dataset_scenarios.get(execution.scenario_id)
        if scenario is None:
            continue
        if execution.scenario_sha256 != frozen_scenario_sha256(scenario):
            continue
        for entry in execution.generation_checkpoints:
            if (
                entry.checkpoint is None
                or entry.runner_error is not None
                or entry.checkpoint.reference is None
                or not entry.checkpoint.answer.visible_text.strip()
                or entry.request_boundary is None
            ):
                continue
            chosen = (path, execution, scenario, entry)
            break
        if chosen is not None:
            break
    if chosen is None:
        pytest.skip("真实生产执行证据中没有可投影检查点")
    path, execution, scenario, entry = chosen
    before = path.read_bytes()
    record = score_execution_external(execution, scenario["assertions"])
    readiness = next(
        item
        for item in record.generation
        if item.checkpoint_id == entry.checkpoint_id
    )
    assert readiness.status == "ready"
    checkpoint = ExternalCheckpointEvidence(
        scenario=scenario,
        execution=execution,
        record=record,
        entry=entry,
        link=entry.checkpoint.checkpoint,
        readiness=readiness,
        retrieval=tuple(
            row for row in execution.retrieval if row.event_id == entry.event_id
        ),
    )
    approved = ExternalApplicabilityEntry(
        execution_sha256=execution.execution_sha256,
        checkpoint=entry.checkpoint.checkpoint,
        applicability=[
            ExternalJudgeApplicability(
                metric="evidence_supported_v1", applicable=True
            ),
            ExternalJudgeApplicability(
                metric="reference_requirements_met_v1", applicable=True
            ),
        ],
    )
    upload, audit = build_external_projection(checkpoint, approved)
    assert upload["outputs"]["answer"]["visible_text"] == outbound_text(
        entry.checkpoint.answer.visible_text
    )
    assert len(upload["outputs"]["context"]["items"]) == len(
        entry.checkpoint.model_input.items
    )
    assert upload["outputs"]["context"]["request_boundary"] == (
        entry.request_boundary.model_dump(mode="json")
    )
    visible_tools = {
        (item["tool_call_id"], item["tool_name"])
        for item in upload["outputs"]["context"]["items"]
        if item["source"] == "tool_result"
    }
    for retrieval in upload["outputs"]["context"]["retrieval"]:
        assert (retrieval["tool_call_id"], retrieval["tool_name"]) in visible_tools
    _assert_external_upload_whitelist({"rows": [{"upload": upload}]})
    ExternalJudgeCheckpointUpload.model_validate(upload)
    assert path.read_bytes() == before
    assert audit["execution_sha256"] == execution.execution_sha256
