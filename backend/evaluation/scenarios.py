import argparse
import asyncio
import base64
import hashlib
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Literal, Self
from uuid import UUID, uuid4, uuid5

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.prompts import build_system_prompt
from app.agent.tools.business import business_tool_declarations
from app.agent.tools.files import create_file_tools
from app.ai.messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    Usage,
    UserMessage,
)
from app.application.business.catalog import Catalog
from app.application.business.confirmation import ConfirmationService
from app.application.business.coordination import ReplacementCoordinator
from app.application.business.retrieval_frozen import THRESHOLD, frozen_values
from app.application.business.retrieval_response import SearchObservation
from app.application.business.retrieval_runtime import ActionRetrievalRuntime
from app.application.business.service import BusinessService
from app.application.session.admission import admit_run
from app.application.session.credentials import CredentialFilter
from app.application.session.execution import (
    execute,  # noqa: F401 - 生产执行链路导入校验
)
from app.application.session.positions import set_leaf
from app.application.session.runtime import (
    RunState,
    accepting_runs,
    reset_fault_closure,
    reset_run_scope,
    set_run_scope,
)
from app.application.session.service import SessionService
from app.application.session.steering import SteeringCoordinator
from app.domain.business.models import (
    PlanContent,
    PlanRecord,
    ProfileContent,
    SaveConfirmationRequest,
    WorkoutContent,
    WorkoutRecord,
)
from app.domain.session.models import (
    SendCommand,
    SendRequest,
    SessionMessageEntry,
)
from app.infrastructure.persistence.sqlite.business_repository import (
    SqliteBusinessRepository,
)
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from app.model_config import ModelNotConfigured, load_model_config
from evaluation.contracts import (
    GENERATION_METRICS,
    AnswerEvidence,
    AssertionOutcome,
    BusinessProjection,
    CheckpointResult,
    ContextItem,
    DatasetIdentity,
    EvidenceLink,
    GenerationCheckpoint,
    GenerationReference,
    MetricApplicability,
    MetricOutcome,
    ModelInputContext,
    ProposalBinding,
    RequestBoundaryEvidence,
    RerankEvidence,
    Sha256,
    UserInputEvidence,
)
from evaluation.dataset import (
    BACKEND,
    EvaluationDataset,
    load_for_evaluation,
)
from evaluation.deterministic_assertions import (
    AssertionEvidence,
    AssertionInputError,
    BusinessContentEvidence,
    CancellationEvidence,
    DisplayContentEvidence,
    EventIdentity,
    EventTarget,
    PlanSnapshotEvidence,
    ProfileSnapshotEvidence,
    RankingEvidence,
    ToolResultEvidence,
    WorkoutSnapshotEvidence,
    evaluate_checkpoint,
    parse_assertion,
)
from evaluation.generation_scoring import (
    GenerationMetricEvidence,
    GenerationScoringSession,
    JudgeConfig,
    JudgeConfigError,
    ScoringAttemptSnapshot,
    failed_outcome,
    load_judge_model,
    run_judge_identity,
    validate_timeout,
)
from evaluation.scenario_evidence import (
    CancelEvidence,
    ContextAmbiguousError,
    ContextGap,
    ContextMappingError,
    EvaluationCallRecorder,
    EvaluationObserver,
    RequestNodes,
    build_proposal_bindings,
    canonical_json,
    context_items_from_request,
    current_input_entry_ids,
    display_content_for,
    event_target_for,
    message_visible_text,
    model_identity_summary,
    parse_timestamp,
    proposal_payload,
    read_business_projection_with_cancellations,
    read_cancel_evidence,
    read_request_nodes,
    sanitize_endpoint,
    sanitize_error_text,
    search_evidence_from_tool,
    sha256_text,
    structured_answer,
    tool_nodes_by_visible,
)

SCENARIO_ROOT = BACKEND / "temp" / "evaluation-scenarios"
DEFAULT_VERSION_DIR = BACKEND / "dataset" / "evaluation" / "rebuild-v3"
DEFAULT_JUDGE_TIMEOUT = 300.0
JUDGE_BASE_URL_VARIABLE = "FIT_AGENT_EVAL_JUDGE_BASE_URL"
JUDGE_API_KEY_VARIABLE = "FIT_AGENT_EVAL_JUDGE_API_KEY"
EVENT_TYPES = {"message", "confirm", "cancel", "session_switch", "restart"}

# 运行协议显式选择：历史记录缺少字段时按 Ragas 协议解释。
RAGAS_PROTOCOL_VERSION = "fit-agent-scenario-ragas-protocol-1"
EXTERNAL_PROTOCOL_VERSION = "fit-agent-external-scenario-protocol-1"
SCORING_PROTOCOLS: dict[str, str] = {
    "ragas": RAGAS_PROTOCOL_VERSION,
    "external": EXTERNAL_PROTOCOL_VERSION,
}
ScoringMode = Literal["ragas", "external"]
EXTERNAL_SCORING_SCHEMA = "fit-agent-external-scenario-scoring-1"
EXTERNAL_SCORING_FILENAME = "external-scoring.json"


class ScenarioError(Exception):
    pass


class ScenarioBlocked(ScenarioError):
    """冻结要求与当前生产协议无法同时满足时停止，并保留可核验依据。"""

    def __init__(
        self,
        scenario_id: str,
        event_id: str | None,
        reason: str,
        details: dict | None = None,
    ) -> None:
        self.scenario_id = scenario_id
        self.event_id = event_id
        self.reason = reason
        self.details = details
        self.observed_events = 0
        self.observed_checkpoints = 0
        self.context_gaps: list[str] = []
        self.artifact: ScenarioArtifact | None = None
        self.execution: "ScenarioExecution | None" = None
        super().__init__(f"{scenario_id}/{event_id or '-'}: {reason}")


class ScenarioModelUnavailable(ScenarioBlocked):
    pass


class ExecutionIdentityError(RuntimeError):
    pass


class ScoringResultExists(RuntimeError):
    pass


class ArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_nan=False)


class ScenarioPlan(ArtifactModel):
    scenario_id: str
    event_ids: list[str]
    checkpoint_ids: list[str]
    generation_checkpoint_ids: list[str]
    generation_metrics: dict[str, list[str]]
    event_types: dict[str, str]
    tags: list[str]


class ExecutionPlan(ArtifactModel):
    run_id: str
    purpose: str
    dataset_version: str
    dataset_manifest_sha256: str
    dataset_content_sha256: str
    scenarios: list[ScenarioPlan]
    expected_scenarios: int
    expected_events: int
    expected_checkpoints: int
    expected_metrics: int
    scoring_mode: ScoringMode = "ragas"
    protocol_version: str = RAGAS_PROTOCOL_VERSION

    @model_validator(mode="after")
    def validate_protocol(self) -> Self:
        if self.protocol_version != SCORING_PROTOCOLS[self.scoring_mode]:
            raise ValueError("执行计划的评分协议与模式不一致")
        return self


class ToolCallCapture(ArtifactModel):
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    content: str | None = None
    is_error: bool | None = None
    entry_id: str | None = None
    parent_entry_id: str | None = None
    duration_ms: float | None = None


class ModelCallCapture(ArtifactModel):
    sequence: int
    call_kind: str
    message_id: str | None
    compaction_id: str | None
    api: str
    provider: str
    model: str
    request_sha256: str
    request_path: str
    status: str | None
    input_tokens: int | None
    output_tokens: int | None


class EventCapture(ArtifactModel):
    run_id: str
    scenario_id: str
    event_id: str
    event_type: str
    source_event_id: str | None
    session_id: str
    operation_id: str | None
    request_entry_id: str | None
    terminal_status: str
    terminal_event: str | None
    answer_text: str | None = None
    answer_message_id: str | None = None
    answer_structured: dict[str, Any] | None = None
    before: BusinessProjection
    after: BusinessProjection
    tool_calls: list[ToolCallCapture] = Field(default_factory=list)
    model_calls: list[ModelCallCapture] = Field(default_factory=list)
    tool_identity_map: dict[str, str] = Field(default_factory=dict)
    context_gaps: list[ContextGap] = Field(default_factory=list)
    cancel_evidence: CancelEvidence | None = None
    driving_event_id: str | None = None
    operation_id_reused: bool = False
    error: str | None = None


class AssertionCheckpointArtifact(ArtifactModel):
    checkpoint_id: str
    event_id: str
    outcomes: list[AssertionOutcome] = Field(default_factory=list)
    binding_error: str | None = None
    runner_error: str | None = None


class GenerationCheckpointArtifact(ArtifactModel):
    checkpoint_id: str
    event_id: str
    checkpoint: GenerationCheckpoint | None = None
    result: CheckpointResult | None = None
    evidence: list[GenerationMetricEvidence] = Field(default_factory=list)
    attempts: list[ScoringAttemptSnapshot] = Field(default_factory=list)
    request_boundary: RequestBoundaryEvidence | None = None
    runner_error: str | None = None
    incomplete_metrics: list[str] = Field(default_factory=list)


class SearchStageRecord(ArtifactModel):
    sequence: int = Field(ge=0)
    run_id: str
    scenario_id: str
    event_id: str
    tool_call_id: str
    index_collection: str
    index_manifest_sha256: str
    index_manifest_json: str
    arguments: dict[str, Any]
    candidate_pool: list[str]
    final_model_input: list[str]
    response_json: dict[str, Any]
    scores: list[float]
    threshold: float | None
    # 历史记录缺少实验字段时按显式 RRF baseline 解释；新记录保存真实重排证据。
    experiment: Literal["rrf_baseline", "rerank"] = "rrf_baseline"
    rerank: RerankEvidence | None = None

    @model_validator(mode="after")
    def validate_experiment(self) -> Self:
        if self.experiment == "rerank":
            if self.rerank is None or self.rerank.mode != "rerank":
                raise ValueError("重排实验记录必须携带重排证据")
        elif self.rerank is not None:
            raise ValueError("RRF baseline 记录不得携带重排证据")
        return self


class ScenarioArtifact(ArtifactModel):
    run_id: str
    scenario_id: str
    purpose: str = "validation"
    session_ids: list[str]
    events: list[EventCapture]
    assertions: list[AssertionCheckpointArtifact]
    generation: list[GenerationCheckpointArtifact]
    retrieval: list[SearchStageRecord] = Field(default_factory=list)
    proposal_bindings: dict[str, ProposalBinding]
    generation_metrics: dict[str, list[str]] = Field(default_factory=dict)
    cancel_evidence: list[CancelEvidence] = Field(default_factory=list)
    context_gaps: list[ContextGap] = Field(default_factory=list)
    runner_error: str | None = None
    blocked: str | None = None


EXECUTION_SCHEMA = "fit-agent-scenario-execution-1"
EXECUTION_FILENAME = "execution.json"


def _require_execution_link(
    link: EvidenceLink,
    *,
    run_id: str,
    scenario_id: str,
    event_id: str,
    checkpoint_id: str,
) -> None:
    actual = (
        link.run_id,
        link.scenario_id,
        link.event_id,
        link.checkpoint_id,
    )
    if actual != (run_id, scenario_id, event_id, checkpoint_id):
        raise ValueError("证据身份与执行结果不一致")


def _require_assertion_links(
    evidence: AssertionEvidence,
    *,
    run_id: str,
    scenario_id: str,
    event_id: str,
    checkpoint_id: str,
) -> None:
    for group in (
        evidence.profile_snapshots,
        evidence.plan_snapshots,
        evidence.workout_snapshots,
        evidence.display_contents,
        evidence.business_contents,
        evidence.context_evidence,
        evidence.tool_results,
        evidence.rankings,
        evidence.cancel_evidence,
    ):
        for item in group:
            _require_execution_link(
                item.checkpoint,
                run_id=run_id,
                scenario_id=scenario_id,
                event_id=event_id,
                checkpoint_id=checkpoint_id,
            )
    if evidence.parameter_target is not None:
        _require_execution_link(
            evidence.parameter_target.checkpoint,
            run_id=run_id,
            scenario_id=scenario_id,
            event_id=event_id,
            checkpoint_id=checkpoint_id,
        )


class AssertionCheckpointEvidence(ArtifactModel):
    checkpoint_id: str
    event_id: str
    assertions: list[dict[str, Any]]
    evidence: AssertionEvidence | None = None
    binding_error: str | None = None
    runner_error: str | None = None

    @model_validator(mode="after")
    def validate_entry(self) -> Self:
        if not self.assertions:
            raise ValueError("断言检查点必须保留断言定义")
        for definition in self.assertions:
            parsed = parse_assertion(definition)
            if (
                parsed.checkpoint_id != self.checkpoint_id
                or parsed.event_id != self.event_id
            ):
                raise ValueError("断言定义与执行证据检查点不一致")
        if self.binding_error is not None and self.runner_error is not None:
            raise ValueError("断言检查点不能同时携带绑定错误与运行错误")
        if (
            self.evidence is None
            and self.binding_error is None
            and self.runner_error is None
        ):
            raise ValueError("断言检查点缺少证据或错误依据")
        return self


class GenerationCheckpointEvidence(ArtifactModel):
    checkpoint_id: str
    event_id: str
    applicability: list[MetricApplicability]
    checkpoint: GenerationCheckpoint | None = None
    request_boundary: RequestBoundaryEvidence | None = None
    runner_error: str | None = None

    @model_validator(mode="after")
    def validate_entry(self) -> Self:
        metrics = sorted(row.metric for row in self.applicability)
        if metrics != sorted(GENERATION_METRICS):
            raise ValueError("生成检查点必须逐项声明 Faithfulness 与 FactualCorrectness")
        if self.checkpoint is None:
            if self.request_boundary is not None:
                raise ValueError("缺失生成检查点时不得携带请求边界")
            if self.runner_error is None and any(
                row.applicable for row in self.applicability
            ):
                raise ValueError("缺失生成检查点时适用指标必须提供错误依据")
            return self
        if self.runner_error is not None:
            raise ValueError("已构造生成检查点时不得携带执行错误")
        if self.request_boundary is None:
            raise ValueError("生成检查点必须携带请求边界")
        if self.checkpoint.checkpoint.checkpoint_id != self.checkpoint_id or (
            self.checkpoint.checkpoint.event_id != self.event_id
        ):
            raise ValueError("生成检查点身份与执行证据不一致")
        expected = {
            (row.metric, row.applicable, row.reason)
            for row in self.checkpoint.applicability
        }
        actual = {
            (row.metric, row.applicable, row.reason)
            for row in self.applicability
        }
        if actual != expected:
            raise ValueError("生成检查点适用性与冻结证据不一致")
        return self


class ScenarioExecution(ArtifactModel):
    schema_version: str = EXECUTION_SCHEMA
    run_id: str
    scenario_id: str
    dataset: DatasetIdentity
    scenario_sha256: Sha256
    execution_sha256: Sha256
    status: str
    error: str | None = None
    session_ids: list[str]
    initial_state_sha256: Sha256
    initial_projection: BusinessProjection | None = None
    expected_event_ids: list[str]
    expected_checkpoint_ids: list[str]
    events: list[EventCapture]
    assertion_checkpoints: list[AssertionCheckpointEvidence] = Field(
        default_factory=list
    )
    generation_checkpoints: list[GenerationCheckpointEvidence] = Field(
        default_factory=list
    )
    retrieval: list[SearchStageRecord] = Field(default_factory=list)
    proposal_bindings: dict[str, ProposalBinding]
    generation_metrics: dict[str, list[str]] = Field(default_factory=dict)
    cancel_evidence: list[CancelEvidence] = Field(default_factory=list)
    context_gaps: list[ContextGap] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_execution(self) -> Self:
        if self.schema_version != EXECUTION_SCHEMA:
            raise ValueError("执行结果版本不受支持")
        if self.status not in {"completed", "blocked", "failed"}:
            raise ValueError("执行状态必须为 completed、blocked 或 failed")
        if self.status == "completed" and self.error is not None:
            raise ValueError("完成执行不得携带错误依据")
        if self.status != "completed" and self.error is None:
            raise ValueError("失败或阻断执行必须保留错误依据")
        if self.status == "completed" and not self.session_ids:
            raise ValueError("完成执行必须保留会话标识")
        if self.status == "completed" and self.initial_projection is None:
            raise ValueError("完成执行必须保留初始状态")
        observed_events = [event.event_id for event in self.events]
        if observed_events != self.expected_event_ids[: len(observed_events)]:
            raise ValueError("执行事件顺序与冻结计划不一致")
        if self.status == "completed" and observed_events != self.expected_event_ids:
            raise ValueError("完成执行的事件覆盖不完整")
        events = {event.event_id: event for event in self.events}
        if len(events) != len(self.events):
            raise ValueError("执行结果事件 ID 不能重复")
        for event in self.events:
            if event.scenario_id != self.scenario_id:
                raise ValueError("事件 scenario_id 与执行结果不一致")
        assertion_keys: set[tuple[str, str]] = set()
        for entry in self.assertion_checkpoints:
            key = (entry.event_id, entry.checkpoint_id)
            if key in assertion_keys:
                raise ValueError("断言检查点身份不能重复")
            assertion_keys.add(key)
            if entry.event_id not in events:
                raise ValueError("断言检查点事件不在执行结果中")
            if entry.evidence is not None:
                _require_execution_link(
                    entry.evidence.checkpoint,
                    run_id=self.run_id,
                    scenario_id=self.scenario_id,
                    event_id=entry.event_id,
                    checkpoint_id=entry.checkpoint_id,
                )
                _require_assertion_links(
                    entry.evidence,
                    run_id=self.run_id,
                    scenario_id=self.scenario_id,
                    event_id=entry.event_id,
                    checkpoint_id=entry.checkpoint_id,
                )
        generation_ids: set[str] = set()
        for entry in self.generation_checkpoints:
            if entry.checkpoint_id in generation_ids:
                raise ValueError("生成检查点身份不能重复")
            generation_ids.add(entry.checkpoint_id)
            event = events.get(entry.event_id)
            if event is None:
                raise ValueError("生成检查点事件不在执行结果中")
            if entry.checkpoint is not None:
                _require_execution_link(
                    entry.checkpoint.checkpoint,
                    run_id=self.run_id,
                    scenario_id=self.scenario_id,
                    event_id=entry.event_id,
                    checkpoint_id=entry.checkpoint_id,
                )
                boundary = entry.request_boundary
                if boundary is None or (
                    boundary.session_id != event.session_id
                    or boundary.run_id != event.run_id
                    or boundary.request_entry_id != event.request_entry_id
                ):
                    raise ValueError("请求边界与执行事件不一致")
        if not generation_ids <= set(self.generation_metrics):
            raise ValueError("生成检查点不在冻结指标覆盖中")
        if self.status == "completed" and set(self.generation_metrics) != generation_ids:
            raise ValueError("完成执行的生成指标覆盖不完整")
        for entry in self.generation_checkpoints:
            expected = [row.metric for row in entry.applicability]
            if sorted(self.generation_metrics[entry.checkpoint_id]) != sorted(expected):
                raise ValueError("生成指标与检查点适用性不一致")
        covered = assertion_keys | {
            (entry.event_id, entry.checkpoint_id)
            for entry in self.generation_checkpoints
        }
        covered_ids = {checkpoint_id for _, checkpoint_id in covered}
        if not covered_ids <= set(self.expected_checkpoint_ids):
            raise ValueError("执行检查点不在冻结计划中")
        if (
            self.status == "completed"
            and covered_ids != set(self.expected_checkpoint_ids)
        ):
            raise ValueError("完成执行的检查点覆盖不完整")
        for record in self.retrieval:
            if record.run_id != self.run_id or record.scenario_id != self.scenario_id:
                raise ValueError("检索记录身份与执行结果不一致")
        return self


class CoverageArtifact(ArtifactModel):
    expected_scenarios: int
    observed_scenarios: int
    expected_events: int
    observed_events: int
    expected_checkpoints: int
    observed_checkpoints: int
    expected_metrics: int
    observed_metrics: int
    succeeded_metrics: int = 0
    failed_metrics: int = 0
    not_applicable_metrics: int = 0
    unexecuted_metrics: int = 0
    context_gaps: int = 0
    missing: list[str] = Field(default_factory=list)
    missing_metrics: list[str] = Field(default_factory=list)


class RunSummary(ArtifactModel):
    run_id: str
    purpose: str
    dataset: dict[str, Any]
    identity: dict[str, Any]
    coverage: CoverageArtifact
    scenarios: list[str]
    blocked: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    incomplete: list[str] = Field(default_factory=list)
    context_gaps: list[str] = Field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None
    scoring_mode: ScoringMode = "ragas"
    protocol_version: str = RAGAS_PROTOCOL_VERSION


class ExternalScoringReadiness(ArtifactModel):
    """生成检查点的外部评分证据就绪情况；不代表任何新指标适用性。"""

    checkpoint_id: str
    event_id: str | None = None
    status: Literal["ready", "no_generation_evidence", "missing"]
    error: str | None = None
    missing: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_entry(self) -> Self:
        if self.status == "missing":
            if not self.missing:
                raise ValueError("缺失的外部评分证据必须列出缺失项")
            return self
        if self.error is not None or self.missing:
            raise ValueError("未缺失的外部评分证据不得携带错误或缺失项")
        if self.event_id is None:
            raise ValueError("已观察的生成检查点必须保留事件身份")
        return self


class ExternalScenarioScoring(ArtifactModel):
    """外部评分模式下单场景的本地结果：只执行确定性断言，生成评分保留待处理。"""

    schema_version: str = EXTERNAL_SCORING_SCHEMA
    scoring_mode: Literal["external"] = "external"
    protocol_version: str = EXTERNAL_PROTOCOL_VERSION
    run_id: str
    scenario_id: str
    execution_sha256: Sha256
    execution_status: Literal["completed", "blocked", "failed"]
    blocked: str | None = None
    runner_error: str | None = None
    assertion_status: Literal[
        "passed", "failed", "binding_error", "runner_error", "incomplete"
    ]
    generation_scoring_status: Literal["pending_external", "incomplete"]
    applicability_status: Literal["awaiting_external_applicability"] = (
        "awaiting_external_applicability"
    )
    expected_event_ids: list[str]
    observed_event_ids: list[str]
    expected_checkpoint_ids: list[str]
    missing_assertion_checkpoint_ids: list[str] = Field(default_factory=list)
    assertions: list[AssertionCheckpointArtifact] = Field(default_factory=list)
    generation: list[ExternalScoringReadiness] = Field(default_factory=list)
    incomplete: list[str] = Field(default_factory=list)
    context_gaps: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_scoring(self) -> Self:
        if self.schema_version != EXTERNAL_SCORING_SCHEMA:
            raise ValueError("外部评分结果版本不受支持")
        if self.protocol_version != EXTERNAL_PROTOCOL_VERSION:
            raise ValueError("外部评分协议版本不受支持")
        if self.execution_status == "completed":
            if self.blocked is not None or self.runner_error is not None:
                raise ValueError("完成执行不得携带阻断或失败依据")
        elif self.execution_status == "blocked":
            if self.blocked is None or self.runner_error is not None:
                raise ValueError("阻断执行必须仅保留阻断依据")
        elif self.blocked is not None or self.runner_error is None:
            raise ValueError("失败执行必须仅保留失败依据")
        assertion_ids = [item.checkpoint_id for item in self.assertions]
        if len(set(assertion_ids)) != len(assertion_ids):
            raise ValueError("断言检查点身份不能重复")
        readiness_ids = [item.checkpoint_id for item in self.generation]
        if len(set(readiness_ids)) != len(readiness_ids):
            raise ValueError("外部评分就绪检查点身份不能重复")
        if self.has_missing_generation_evidence():
            if self.generation_scoring_status != "incomplete":
                raise ValueError("存在缺失证据时生成评分状态必须为 incomplete")
        elif self.generation_scoring_status != "pending_external":
            raise ValueError("证据齐备时生成评分状态必须为待外部评分")
        return self

    def has_missing_generation_evidence(self) -> bool:
        return any(item.status == "missing" for item in self.generation)


class ExternalCoverageArtifact(ArtifactModel):
    expected_scenarios: int
    observed_scenarios: int
    expected_events: int
    observed_events: int
    expected_checkpoints: int
    observed_checkpoints: int
    assertions_passed: int
    assertions_failed: int
    assertions_binding_errors: int
    assertions_runner_errors: int
    generation_ready: int
    generation_missing: int
    context_gaps: int
    missing: list[str] = Field(default_factory=list)
    missing_assertion_checkpoints: list[str] = Field(default_factory=list)


class ExternalRunSummary(ArtifactModel):
    run_id: str
    purpose: str
    scoring_mode: Literal["external"] = "external"
    protocol_version: str = EXTERNAL_PROTOCOL_VERSION
    dataset: dict[str, Any]
    identity: dict[str, Any]
    coverage: ExternalCoverageArtifact
    scenarios: list[str]
    blocked: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    incomplete: list[str] = Field(default_factory=list)
    context_gaps: list[str] = Field(default_factory=list)
    status: Literal["awaiting_external_scoring", "incomplete"]
    started_at: str | None = None
    finished_at: str | None = None


@dataclass
class ExternalScenarioOutcome:
    record: ExternalScenarioScoring
    execution: ScenarioExecution


class EvaluationRunOutput:
    def __init__(self, run_id: UUID, control) -> None:
        self.run_id = run_id
        self.control = control
        self._lock = Lock()
        self.events: list[tuple[str, dict]] = []
        self.terminal: tuple[str, dict] | None = None
        self.closed = False

    def publish(self, name: str, data: dict) -> bool:
        with self._lock:
            self.events.append((name, data))
        return True

    def publish_terminal(self, name: str, data: dict) -> None:
        with self._lock:
            self.events.append((name, data))
            self.terminal = (name, data)

    def close_normal(self) -> None:
        self.closed = True


class EvaluationRunState(RunState):
    def __init__(self, session_id, *, run_id=None, budget_seconds=None) -> None:
        super().__init__(
            session_id,
            create_output=EvaluationRunOutput,
            budget_seconds=budget_seconds,
            run_id=run_id,
        )


@dataclass
class RuntimeHandle:
    root: Path
    evaluation_run_id: str = ""
    scenario_id: str = ""
    database: Any = None
    session_repository: Any = None
    service: SessionService | None = None
    business_repository: Any = None
    business: BusinessService | None = None
    confirmations: ConfirmationService | None = None
    retrieval: ActionRetrievalRuntime | None = None
    executor: ThreadPoolExecutor | None = None
    steering: SteeringCoordinator | None = None
    call_recorder: EvaluationCallRecorder | None = None
    scope_token: Any = None
    # 检索证据跨重启累积：序号按场景单调递增，事件绑定由 run_scenario 逐事件设置。
    event_id: str | None = None
    search_records: list[SearchStageRecord] = field(default_factory=list)
    search_lock: Lock = field(default_factory=Lock)

    @property
    def db_path(self) -> Path:
        return self.root / "fit-agent.db"

    async def open(self) -> None:
        # 场景执行使用独立容量与故障封闭作用域，避免并发场景互相重置或占用生产单槽。
        self.scope_token = set_run_scope(
            f"evaluation:{self.evaluation_run_id}:{self.scenario_id}"
        )
        reset_fault_closure()
        self.database = await open_database(self.db_path)
        self.session_repository = SqliteSessionRepository(self.database)
        self.service = SessionService(
            self.session_repository, project_root=self.root
        )
        await self.service.runs.recover_interrupted()
        self.business_repository = SqliteBusinessRepository(self.database)
        catalog = Catalog.load()
        replacements = ReplacementCoordinator()
        self.retrieval = ActionRetrievalRuntime()
        self.business = BusinessService(
            self.business_repository,
            catalog,
            self.service.queries,
            replacements,
            self.retrieval,
            search_observer=self._observe_search,
        )
        self.service.attach_snapshots(self.business.proposals)
        self.confirmations = ConfirmationService(
            self.business_repository,
            self.service.queries,
            self.service.operations,
            self.service.entries,
            self.business,
        )
        await self.business_repository.recover_interrupted_saves()
        await self.business_repository.recover_interrupted_workout_saves()
        await self.business_repository.recover_interrupted_plan_saves()
        await self.confirmations.recover_interrupted()
        async with self.business_repository.transaction():
            await self.business_repository.replace_exercises(catalog.all())
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.steering = SteeringCoordinator(
            self.service, self.service.operations, self.service.runs
        )
        self.call_recorder = EvaluationCallRecorder(self.root / "model-calls")
        accepting_runs.set()

    async def close(self) -> None:
        if self.retrieval is not None:
            await self.retrieval.close()
        if self.executor is not None:
            self.executor.shutdown(wait=True)
        if self.database is not None:
            await self.database.close()
        self.database = None
        self.session_repository = None
        self.service = None
        self.business_repository = None
        self.business = None
        self.confirmations = None
        self.retrieval = None
        self.executor = None
        self.steering = None
        self.call_recorder = None
        if self.scope_token is not None:
            reset_fault_closure()
            reset_run_scope(self.scope_token)
            self.scope_token = None

    async def projection(self, session_ids: Sequence[str]) -> BusinessProjection:
        return await read_business_projection_with_cancellations(
            self.business_repository, self.service.queries, session_ids
        )

    def _observe_search(self, observation: SearchObservation) -> None:
        if self.event_id is None or observation.tool_call_id is None:
            raise RuntimeError(
                "检索观测缺少事件或工具调用身份: "
                f"event={self.event_id} tool_call_id={observation.tool_call_id}"
            )
        result = observation.stage.rerank
        identity = observation.stage.rerank_identity
        if result is None:
            experiment = "rrf_baseline"
            rerank_evidence = None
        else:
            if identity is None:
                raise RuntimeError(
                    f"检索观测缺少重排身份: {observation.tool_call_id}"
                )
            experiment = "rerank"
            rerank_evidence = RerankEvidence(
                mode="rerank",
                identity={
                    "model": identity.model,
                    "endpoint": identity.endpoint,
                    "text_version": identity.text_version,
                },
                scored=[
                    {
                        "id": row.id,
                        "rerank_score": row.rerank_score,
                        "rrf_rank": row.rrf_rank,
                    }
                    for row in result.scored
                ],
                usage={
                    "requests": result.usage.requests,
                    "prompt_tokens": result.usage.prompt_tokens,
                    "total_tokens": result.usage.total_tokens,
                },
                request_id=result.request_id,
            )
        record = SearchStageRecord(
            sequence=len(self.search_records),
            run_id=self.evaluation_run_id,
            scenario_id=self.scenario_id,
            event_id=self.event_id,
            tool_call_id=observation.tool_call_id,
            index_collection=observation.collection,
            index_manifest_sha256=sha256_text(observation.manifest_json),
            index_manifest_json=observation.manifest_json,
            arguments=observation.arguments.model_dump(mode="json"),
            candidate_pool=[item.id for item in observation.stage.candidate_pool],
            final_model_input=[item.id for item in observation.stage.final_input],
            response_json=observation.stage.response.model_dump(mode="json"),
            scores=[float(item.score) for item in observation.stage.candidate_pool],
            threshold=None if THRESHOLD is None else float(THRESHOLD),
            experiment=experiment,
            rerank=rerank_evidence,
        )
        with self.search_lock:
            self.search_records.append(record)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _append_jsonl(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n"
        )


def _write_json_rows(path: Path, rows: Sequence[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(
        json.dumps(
            row if isinstance(row, dict) else row.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
        for row in rows
    )
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _context_rows(captures: Sequence[EventCapture]) -> list[dict[str, Any]]:
    return [
        {
            "event_id": capture.event_id,
            "tool_identity_map": capture.tool_identity_map,
            "context_gaps": [
                gap.model_dump(mode="json") for gap in capture.context_gaps
            ],
        }
        for capture in captures
    ]


def _write_event_evidence(
    scenario_root: Path, captures: Sequence[EventCapture]
) -> None:
    if not captures:
        return
    _write_json_rows(
        scenario_root / "events.jsonl",
        [capture.model_dump(mode="json") for capture in captures],
    )
    _write_json_rows(
        scenario_root / "context.jsonl", _context_rows(captures)
    )


def _frozen_scenario_payload(scenario: dict) -> dict:
    return {
        key: value for key, value in scenario.items() if not key.startswith("_")
    }


def frozen_scenario_sha256(scenario: dict) -> str:
    return sha256_text(canonical_json(_frozen_scenario_payload(scenario)))


def _dataset_identity(dataset: EvaluationDataset) -> DatasetIdentity:
    return DatasetIdentity(
        version=dataset.version,
        manifest_sha256=dataset.manifest_sha256,
        content_sha256=dataset.content_sha256,
        corpus_sha256=dataset.corpus_sha256,
    )


def _execution_digest(execution: ScenarioExecution) -> str:
    payload = execution.model_dump(mode="json")
    payload.pop("execution_sha256", None)
    return sha256_text(canonical_json(payload))


def _seal_execution(execution: ScenarioExecution) -> ScenarioExecution:
    return execution.model_copy(update={"execution_sha256": _execution_digest(execution)})


def verify_execution_identity(
    execution: ScenarioExecution,
    *,
    expected_run_id: str | None = None,
    expected_scenario_id: str | None = None,
    expected_dataset: DatasetIdentity | None = None,
    expected_scenario_sha256: str | None = None,
) -> ScenarioExecution:
    if _execution_digest(execution) != execution.execution_sha256:
        raise ExecutionIdentityError("执行结果摘要不一致")
    if expected_run_id is not None and execution.run_id != expected_run_id:
        raise ExecutionIdentityError("执行结果 run_id 不一致")
    if (
        expected_scenario_id is not None
        and execution.scenario_id != expected_scenario_id
    ):
        raise ExecutionIdentityError("执行结果 scenario_id 不一致")
    if expected_dataset is not None and execution.dataset != expected_dataset:
        raise ExecutionIdentityError("执行结果数据身份不一致")
    if (
        expected_scenario_sha256 is not None
        and execution.scenario_sha256 != expected_scenario_sha256
    ):
        raise ExecutionIdentityError("执行结果场景身份不一致")
    return execution


def load_execution(
    path: Path,
    *,
    expected_run_id: str | None = None,
    expected_scenario_id: str | None = None,
    expected_dataset: DatasetIdentity | None = None,
    expected_scenario_sha256: str | None = None,
) -> ScenarioExecution:
    path = Path(path)
    if path.is_dir():
        path = path / EXECUTION_FILENAME
    try:
        execution = ScenarioExecution.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except Exception as error:
        raise ExecutionIdentityError(
            f"执行结果无法解析: {type(error).__name__}: {error}"
        ) from error
    return verify_execution_identity(
        execution,
        expected_run_id=expected_run_id,
        expected_scenario_id=expected_scenario_id,
        expected_dataset=expected_dataset,
        expected_scenario_sha256=expected_scenario_sha256,
    )


def write_scoring_result(
    execution: ScenarioExecution,
    artifact: ScenarioArtifact,
    *,
    path: Path,
) -> None:
    verify_execution_identity(execution)
    if (
        artifact.run_id != execution.run_id
        or artifact.scenario_id != execution.scenario_id
        or artifact.session_ids != execution.session_ids
        or [event.event_id for event in artifact.events]
        != [event.event_id for event in execution.events]
    ):
        raise ExecutionIdentityError("评分结果身份与执行结果不一致")
    path = Path(path)
    if path.exists():
        raise ScoringResultExists(f"评分产物已存在，拒绝覆盖: {path}")
    _write_json(path, artifact.model_dump(mode="json"))


def _failure_artifact(
    execution: ScenarioExecution, error_text: str
) -> ScenarioArtifact:
    return ScenarioArtifact(
        run_id=execution.run_id,
        scenario_id=execution.scenario_id,
        session_ids=list(execution.session_ids),
        events=list(execution.events),
        assertions=[],
        generation=[],
        retrieval=list(execution.retrieval),
        proposal_bindings=dict(execution.proposal_bindings),
        generation_metrics=dict(execution.generation_metrics),
        cancel_evidence=list(execution.cancel_evidence),
        context_gaps=list(execution.context_gaps),
        runner_error=sanitize_error_text(error_text),
    )


def _blocked_artifact(
    execution: ScenarioExecution, error_text: str
) -> ScenarioArtifact:
    return _failure_artifact(execution, error_text).model_copy(update={
        "blocked": sanitize_error_text(error_text),
        "runner_error": None,
    })


def select_scenarios(
    dataset: EvaluationDataset, scenario_ids: Sequence[str] | None
) -> list[dict]:
    known = {scenario["scenario_id"]: scenario for scenario in dataset.scenarios}
    if scenario_ids is None:
        return list(dataset.scenarios)
    selected = []
    missing = []
    for scenario_id in scenario_ids:
        scenario = known.get(scenario_id)
        if scenario is None:
            missing.append(scenario_id)
        else:
            selected.append(scenario)
    if missing:
        raise ScenarioError(f"未知 scenario_id: {sorted(missing)}")
    if len({item["scenario_id"] for item in selected}) != len(selected):
        raise ScenarioError("scenario_id 不得重复")
    return selected


def scenario_plan(scenario: dict) -> ScenarioPlan:
    checkpoints = {
        (row["checkpoint_id"], row["event_id"])
        for key in ("reference", "rubric", "assertions", "metric_applicability")
        for row in scenario[key]
    }
    generation = {row["checkpoint_id"] for row in scenario["reference"]}
    generation_metrics: dict[str, list[str]] = {}
    for row in scenario["metric_applicability"]:
        if row["metric"] not in {"faithfulness", "factual_correctness"}:
            continue
        generation_metrics.setdefault(row["checkpoint_id"], []).append(row["metric"])
    return ScenarioPlan(
        scenario_id=scenario["scenario_id"],
        event_ids=[event["event_id"] for event in scenario["events"]],
        checkpoint_ids=sorted({checkpoint_id for checkpoint_id, _ in checkpoints}),
        generation_checkpoint_ids=sorted(generation),
        generation_metrics={
            checkpoint_id: sorted(set(metrics))
            for checkpoint_id, metrics in sorted(generation_metrics.items())
        },
        event_types={event["event_id"]: event["type"] for event in scenario["events"]},
        tags=list(scenario["tags"]),
    )


def build_execution_plan(
    dataset: EvaluationDataset,
    scenarios: Sequence[dict],
    run_id: str,
    scoring_mode: ScoringMode = "ragas",
) -> ExecutionPlan:
    plans = [scenario_plan(scenario) for scenario in scenarios]
    return ExecutionPlan(
        run_id=run_id,
        purpose="validation",
        dataset_version=dataset.version,
        dataset_manifest_sha256=dataset.manifest_sha256,
        dataset_content_sha256=dataset.content_sha256,
        scenarios=plans,
        expected_scenarios=len(plans),
        expected_events=sum(len(item.event_ids) for item in plans),
        expected_checkpoints=sum(len(item.checkpoint_ids) for item in plans),
        expected_metrics=sum(
            len(metrics)
            for item in plans
            for metrics in item.generation_metrics.values()
        ),
        scoring_mode=scoring_mode,
        protocol_version=scoring_protocol(scoring_mode),
    )


def _git_output(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _source_digest() -> str:
    paths = [
        path
        for root in (BACKEND / "app", BACKEND / "evaluation", BACKEND / "skills")
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".md"}
        and "__pycache__" not in path.parts
    ]
    paths.extend([BACKEND / "pyproject.toml", BACKEND / "uv.lock"])
    payload = "\n".join(
        f"{path.relative_to(BACKEND).as_posix()}:{_file_sha256(path)}"
        for path in sorted(paths)
    )
    return sha256_text(payload)


def code_identity() -> dict:
    commit = _git_output(BACKEND.parent, "rev-parse", "HEAD")
    status = _git_output(BACKEND.parent, "status", "--porcelain")
    lock = BACKEND / "uv.lock"
    if not lock.is_file():
        raise ScenarioError("缺少 backend/uv.lock，无法生成代码身份")
    return {
        "commit": commit,
        "dirty": bool(status),
        "lock_sha256": sha256_text(lock.read_text(encoding="utf-8")),
        "source_sha256": _source_digest(),
        "status_porcelain": status,
    }


def generation_identity(model) -> dict:
    prompt = build_system_prompt()
    return {
        "model": canonical_json(model_identity_summary(model)),
        "prompt_version": sha256_text(prompt),
    }


def retrieval_identity() -> dict:
    from app.application.business.retrieval_config import load_embedding_config
    from app.application.business.retrieval_index import RetrievalCorpus
    from app.application.business.retrieval_text import ENCODING_RULE_VERSION
    from evaluation.retrieval_support import query_vector_identity

    # 配置层身份：策略、Embedding 模型与查询向量身份；物理索引身份只允许来自实际执行检索的调用。
    identity: dict[str, Any] = {
        "strategy": canonical_json(frozen_values()),
        "encoding_rule_version": ENCODING_RULE_VERSION,
    }
    missing = []
    try:
        config = load_embedding_config()
        identity["model"] = canonical_json({
            "model": config.model,
            "dimensions": config.dimensions,
            "base_url": sanitize_endpoint(config.base_url),
        })
        identity["query_vector_identity"] = canonical_json(
            query_vector_identity(config, RetrievalCorpus.load())
        )
    except Exception as error:
        missing.append(f"retrieval:{type(error).__name__}")
    identity["missing"] = missing
    return identity


def executed_index_identity(
    records: Sequence[SearchStageRecord], retrieval: dict
) -> None:
    # 运行索引身份由真实检索调用产生的集合与清单聚合；出现多个身份时登记冲突，禁止事后补填。
    distinct: dict[tuple[str, str], SearchStageRecord] = {}
    for record in records:
        distinct.setdefault((record.index_collection, record.index_manifest_sha256), record)
    if not distinct:
        retrieval["missing"] = sorted(set(retrieval["missing"]) | {
            "retrieval_index_identity:未执行真实检索"
        })
        return
    if len(distinct) > 1:
        retrieval["index_conflict"] = canonical_json([
            {"collection": collection, "manifest_sha256": manifest_sha256}
            for collection, manifest_sha256 in sorted(distinct)
        ])
        retrieval["missing"] = sorted(set(retrieval["missing"]) | {
            "retrieval_index_identity:单次运行存在多个物理索引身份"
        })
        return
    record = next(iter(distinct.values()))
    manifest = json.loads(record.index_manifest_json)
    index_identity = {
        "collection": record.index_collection,
        "manifest_sha256": record.index_manifest_sha256,
        "build_id": str(
            manifest.get("build_id")
            or manifest.get("build")
            or record.index_collection
        ),
        "compatibility": manifest.get("compatibility"),
    }
    retrieval["index"] = canonical_json(index_identity)
    if "query_vector_identity" in retrieval:
        retrieval["cache_identity"] = sha256_text(canonical_json({
            "query": json.loads(retrieval["query_vector_identity"]),
            "index_manifest_sha256": index_identity["manifest_sha256"],
        }))
    # 重排身份只来自实际重排记录；历史全部为基线记录时不追加新身份。
    rerank_records = [item for item in records if item.rerank is not None]
    experiments = {item.experiment for item in records}
    if rerank_records:
        identities = {
            (
                item.rerank.identity.model,
                item.rerank.identity.endpoint,
                item.rerank.identity.text_version,
            )
            for item in rerank_records
            if item.rerank is not None and item.rerank.identity is not None
        }
        if len(identities) > 1:
            retrieval["rerank_conflict"] = canonical_json(
                [
                    {"model": model, "endpoint": endpoint, "text_version": version}
                    for model, endpoint, version in sorted(identities)
                ]
            )
            retrieval["missing"] = sorted(set(retrieval["missing"]) | {
                "retrieval_rerank_identity:单次运行存在多个重排身份"
            })
        elif identities:
            model, endpoint, version = next(iter(identities))
            retrieval["rerank"] = canonical_json({
                "model": model,
                "endpoint": sha256_text(endpoint),
                "text_version": version,
            })
        if experiments != {"rerank"}:
            retrieval["missing"] = sorted(set(retrieval["missing"]) | {
                "retrieval_rerank_identity:重排与基线记录混合"
            })
    retrieval["experiment"] = (
        "rerank" if experiments == {"rerank"} else "rrf_baseline"
    )


def _retrieval_secret() -> str | None:
    value = os.environ.get("DASHSCOPE_API_KEY")
    return value if value and value.strip() else None


def _active_secrets(model) -> tuple[str, ...]:
    return tuple(
        secret for secret in (model.api_key, _retrieval_secret()) if secret
    )


def _system_message(model, tools, now: int) -> SystemMessage:
    return SystemMessage(
        role="system",
        content=build_system_prompt(),
        tools_added=[tool.definition() for tool in tools.values()]
        + business_tool_declarations(),
        timestamp=now,
    )


async def _seed_initial_state(
    runtime: RuntimeHandle, state: dict, now: int
) -> None:
    repository = runtime.business_repository
    if state["profile"] is not None:
        content = ProfileContent.model_validate(state["profile"])
        async with repository.transaction():
            await repository.save_profile(content, 1, now)
    for plan in state["plans"]:
        await repository.insert_plan(PlanRecord.model_validate(plan))
    for workout in state["workouts"]:
        record = WorkoutRecord.model_validate(workout)
        if record.created_at != record.updated_at:
            raise ScenarioBlocked(
                "", None, "冻结训练记录的创建与更新时间不一致，无法按生产首次写入语义装载"
            )
        first = record.model_copy(
            update={"version": 1, "updated_at": record.created_at}
        )
        await repository.insert_workout(first)
        for version in range(2, record.version + 1):
            updated = await repository.update_workout(
                record.id,
                version - 1,
                record.content,
                record.created_at,
            )
            if not updated:
                raise ScenarioBlocked(
                    "", None, f"训练记录版本装载失败: {record.id}->{version}"
                )


def _verify_initial_projection(
    projection: BusinessProjection, state: dict
) -> dict:
    if state["profile"] is None:
        if projection.profile is not None:
            raise ScenarioBlocked(
                "", None, "冻结画像为空但隔离库存在画像记录"
            )
    else:
        expected = ProfileContent.model_validate(state["profile"])
        if projection.profile is None:
            raise ScenarioBlocked("", None, "冻结画像存在但隔离库缺少画像记录")
        if projection.profile.content != expected:
            raise ScenarioBlocked("", None, "初始画像内容与冻结状态不一致")
    plans = {plan.id: plan for plan in projection.plans}
    if set(plans) != {plan["id"] for plan in state["plans"]}:
        raise ScenarioBlocked("", None, "初始计划集合与冻结状态不一致")
    for item in state["plans"]:
        record = plans[item["id"]]
        if (
            record.is_current != item["is_current"]
            or record.created_at != item["created_at"]
            or record.content != PlanContent.model_validate(item["content"])
        ):
            raise ScenarioBlocked("", None, f"初始计划与冻结状态不一致: {item['id']}")
    workouts = {workout.id: workout for workout in projection.workouts}
    if set(workouts) != {workout["id"] for workout in state["workouts"]}:
        raise ScenarioBlocked("", None, "初始训练记录集合与冻结状态不一致")
    for item in state["workouts"]:
        record = workouts[item["id"]]
        if (
            record.performed_on != item["performed_on"]
            or record.version != item["version"]
            or record.created_at != item["created_at"]
            or record.updated_at != item["updated_at"]
            or record.content != WorkoutContent.model_validate(item["content"])
        ):
            raise ScenarioBlocked("", None, f"初始训练记录与冻结状态不一致: {item['id']}")
    return {
        "profile_version": None
        if projection.profile is None
        else projection.profile.version,
        "profile_present": projection.profile is not None,
        "plan_ids": [plan.id for plan in projection.plans],
        "workout_ids": [workout.id for workout in projection.workouts],
        "current_plan_id": next(
            (plan.id for plan in projection.plans if plan.is_current), None
        ),
    }


async def _seed_prior_messages(
    runtime: RuntimeHandle,
    session_id: str,
    prior_messages: Sequence[dict],
    model,
    tools,
    now: int,
) -> None:
    if not prior_messages:
        return
    repository = runtime.session_repository
    queries = runtime.service.queries
    async with repository.transaction():
        session = await queries.require_session(session_id)
        system_entry = SessionMessageEntry(
            session_id=session_id,
            id=str(uuid4()),
            parent_id=None,
            run_id=None,
            type="message",
            messages=[_system_message(model, tools, now)],
            created_at=now,
        )
        await repository.insert_entry(system_entry)
        parent_id = system_entry.id
        for item in prior_messages:
            role = item["role"]
            content = item["content"]
            if role == "user":
                message = UserMessage(role="user", content=content, timestamp=now)
            elif role == "assistant":
                if model is None:
                    raise ScenarioBlocked(
                        "", None, "冻结历史包含助手消息但缺少真实模型配置"
                    )
                message = AssistantMessage(
                    role="assistant",
                    content=[TextContent(type="text", text=content)],
                    api=model.api,
                    provider=model.provider,
                    model=model.model,
                    usage=Usage(
                        input=0,
                        output=0,
                        cache_read=0,
                        cache_write=0,
                        total_tokens=0,
                    ),
                    stop_reason="stop",
                    timestamp=now,
                )
            else:
                raise ScenarioBlocked("", None, f"未知历史角色: {role}")
            entry = SessionMessageEntry(
                session_id=session_id,
                id=str(uuid4()),
                parent_id=parent_id,
                run_id=None,
                type="message",
                messages=[message],
                created_at=now,
            )
            await repository.insert_entry(entry)
            parent_id = entry.id
        await set_leaf(repository, session, parent_id, now)


def _attachments_for_event(
    dataset: EvaluationDataset, asset_ids: Sequence[str], event_index: int
) -> list[dict]:
    attachments = []
    for asset_id in asset_ids:
        path = dataset.assets[asset_id]
        data = path.read_bytes()
        expected = next(
            row["sha256"]
            for row in dataset.manifest["assets"]
            if row["asset_id"] == asset_id
        )
        if _file_sha256(path) != expected:
            raise ScenarioBlocked("", None, f"附件哈希不符: {asset_id}")
        attachment_id = str(
            uuid5(
                uuid.NAMESPACE_URL,
                f"fit-agent:evaluation:attachment:{dataset.version}:{asset_id}:{event_index}",
            )
        )
        attachments.append({
            "kind": "upload",
            "attachment_id": attachment_id,
            "file_name": path.name,
            "data_base64": base64.b64encode(data).decode("ascii"),
        })
    return attachments


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _event_attachments(
    dataset: EvaluationDataset, scenario: dict, event: dict, index: int
) -> list[dict]:
    asset_ids = list(event["attachments"])
    if index == 0 and not asset_ids:
        asset_ids = list(scenario["input"]["attachments"])
    return _attachments_for_event(dataset, asset_ids, index)


async def _run_message_event(
    runtime: RuntimeHandle,
    dataset: EvaluationDataset,
    scenario: dict,
    event: dict,
    event_index: int,
    session_id: str,
    session_ids: Sequence[str],
    now: int,
) -> EventCapture:
    before = await runtime.projection(session_ids)
    try:
        model = load_model_config()
    except ModelNotConfigured as error:
        raise ScenarioModelUnavailable(
            scenario["scenario_id"], event["event_id"], str(error)
        ) from error
    secrets = _active_secrets(model)
    tools = create_file_tools(session_id, tmp_root=runtime.root / "tmp")
    operation_id = str(uuid4())
    request = SendRequest(
        text=event["content"] or "",
        attachments=_event_attachments(dataset, scenario, event, event_index),
    )
    captured: dict[str, Any] = {}

    async def prepare():
        captured["secrets"] = secrets
        return model, tools, secrets, False

    async def accept(svc, accepted_tools, accepted_secrets):
        return await svc.accept_send(
            SendCommand(
                operation_id=operation_id,
                session_id=session_id,
                request=request,
            ),
            system_message=_system_message(model, accepted_tools, now),
            credentials=accepted_secrets,
            now=now,
        )

    sequence_start = len(runtime.call_recorder.records)

    def create_state(session_uuid, *, run_id=None, budget_seconds=None):
        state = EvaluationRunState(
            session_uuid, run_id=run_id, budget_seconds=budget_seconds
        )
        state.observation_factory = lambda: EvaluationObserver(
            session_id=session_id,
            run_id=str(state.run_id),
            directory=runtime.root / "cache-observation",
            contains=CredentialFilter(captured["secrets"]).contains,
            recorder=runtime.call_recorder,
        )
        return state

    result = await admit_run(
        service=runtime.service,
        queries=runtime.service.queries,
        entries=runtime.service.entries,
        operations=runtime.service.operations,
        run_lifecycle=runtime.service.runs,
        session_id=session_id,
        operation_id=operation_id,
        origin="send",
        operation_request=request,
        credentials=lambda _session: secrets,
        prepare=prepare,
        accept=accept,
        coordinator=runtime.steering,
        business=runtime.business,
        executor=runtime.executor,
        trace_store=None,
        create_state=create_state,
        create_events=_evaluation_events,
    )
    if result.state is None:
        raise ScenarioBlocked(
            scenario["scenario_id"], event["event_id"], "运行未创建"
        )
    state = result.state
    await state.task
    after = await runtime.projection(session_ids)
    run = await runtime.service.queries.get_run(session_id, result.outcome.run.id)
    events = list(state.output.events)
    terminal = state.output.terminal
    terminal_status = "unknown"
    terminal_event = None
    if terminal is not None:
        terminal_event, terminal_data = terminal
        terminal_status = str(terminal_data.get("status", terminal_event))
    captures = _tool_calls(events)
    message_end = _last_answer_message(events)
    answer_text = None
    answer_message_id = None
    answer_structured = None
    if message_end is not None:
        answer_message_id = message_end["entry_id"]
        entry = await runtime.service.queries.require_entry(
            session_id, answer_message_id
        )
        if not isinstance(entry, SessionMessageEntry):
            raise ScenarioBlocked(
                scenario["scenario_id"], event["event_id"], "回答节点不是消息节点"
            )
        message = entry.messages[0]
        if not isinstance(message, AssistantMessage):
            raise ScenarioBlocked(
                scenario["scenario_id"], event["event_id"], "回答节点不是助手消息"
            )
        answer_text = message_visible_text(message)
        answer_structured = structured_answer(message)
    records = runtime.call_recorder.records[sequence_start:]
    return EventCapture(
        run_id=str(run.id),
        scenario_id=scenario["scenario_id"],
        event_id=event["event_id"],
        event_type="message",
        source_event_id=None,
        session_id=session_id,
        operation_id=operation_id,
        request_entry_id=run.request_entry_id,
        terminal_status=terminal_status,
        terminal_event=terminal_event,
        answer_text=answer_text,
        answer_message_id=answer_message_id,
        answer_structured=answer_structured,
        before=before,
        after=after,
        tool_calls=captures,
        model_calls=[_model_call(record) for record in records],
        error=None
        if run.status == "completed"
        else sanitize_error_text(
            f"{run.status}:{run.error_code or ''}:{run.error_message or ''}"
        ),
    )


def _evaluation_events(publish, guard, parents, record_first_text):
    return _CollectingEvents(publish, guard, parents, record_first_text)


class _CollectingEvents:
    def __init__(self, publish, guard, parents, record_first_text) -> None:
        self._publish = publish
        self._guard = guard
        self._parents = parents
        self._record_first_text = record_first_text

    def custom_message(self, entry) -> None:
        self._publish("custom_message", {
            "entry_id": entry.id,
            "parent_id": entry.parent_id,
            "run_id": entry.run_id,
            "created_at": entry.created_at,
        })

    async def emit(self, event) -> None:
        kind = event["type"]
        if kind in {"message_start", "message_update", "message_end"}:
            message = event["message"]
            text = "".join(
                block.text
                for block in message.content
                if isinstance(block, TextContent)
            )
            if text.strip():
                self._record_first_text()
            if kind != "message_end":
                return
            data = {
                "message_id": event["message_id"],
                "entry_id": event["message_id"],
                "parent_id": self._parents.get(event["message_id"]),
                "stop_reason": message.stop_reason,
                "content": message.model_dump(mode="json")["content"],
            }
            self._publish(kind, data)
        elif kind == "tool_start":
            self._publish(kind, {
                "tool_call_id": event["tool_call_id"],
                "name": event["name"],
                "arguments": event["arguments"],
            })
        elif kind == "tool_result":
            self._publish(kind, {
                "tool_call_id": event["tool_call_id"],
                "content": event["content"],
                "is_error": event["is_error"],
                "entry_id": event["message_id"],
                "parent_id": self._parents.get(event["message_id"]),
            })
        elif kind == "tool_execution_end":
            self._publish(kind, {
                "tool_call_id": event["tool_call_id"],
                "name": event["name"],
                "content": event["content"],
                "is_error": event["is_error"],
                "duration_ms": event["duration_ms"],
            })
        elif kind in {"compaction_start", "compaction_end", "custom_message"}:
            self._publish(kind, dict(event))

    def tool_update(self, tool_call_id: str, tool_name: str, result) -> None:
        pass


def _last_answer_message(events: Sequence[tuple[str, dict]]) -> dict | None:
    for name, data in reversed(events):
        if name == "message_end" and data.get("stop_reason") == "stop":
            return data
    return None


def _tool_calls(events: Sequence[tuple[str, dict]]) -> list[ToolCallCapture]:
    starts: dict[str, dict] = {}
    endings: dict[str, dict] = {}
    results: dict[str, dict] = {}
    for name, data in events:
        call_id = data.get("tool_call_id")
        if call_id is None:
            continue
        if name == "tool_start":
            starts[call_id] = data
        elif name == "tool_execution_end":
            endings[call_id] = data
        elif name == "tool_result":
            results[call_id] = data
    captures = []
    for call_id in starts:
        start = starts[call_id]
        ending = endings.get(call_id, {})
        result = results.get(call_id, {})
        captures.append(ToolCallCapture(
            tool_call_id=call_id,
            tool_name=start["name"],
            arguments=start.get("arguments") or {},
            content=ending.get("content"),
            is_error=ending.get("is_error"),
            entry_id=result.get("entry_id"),
            parent_entry_id=result.get("parent_id"),
            duration_ms=ending.get("duration_ms"),
        ))
    return captures


def _model_call(record) -> ModelCallCapture:
    return ModelCallCapture(
        sequence=record.sequence,
        call_kind=record.call_kind,
        message_id=record.message_id,
        compaction_id=record.compaction_id,
        api=record.api,
        provider=record.provider,
        model=record.model,
        request_sha256=record.request_sha256,
        request_path=record.request_path,
        status=record.status,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
    )


def _answer_call(runtime: RuntimeHandle, capture: EventCapture):
    if capture.answer_message_id is None:
        raise ScenarioBlocked(
            capture.scenario_id, capture.event_id, "缺少回答生成调用"
        )
    matching = [
        record
        for record in runtime.call_recorder.records
        if record.message_id == capture.answer_message_id
    ]
    successful = [record for record in matching if record.status == "stop"]
    chosen = successful[-1] if successful else (matching[-1] if matching else None)
    if chosen is None:
        raise ScenarioBlocked(
            capture.scenario_id, capture.event_id, "回答未关联真实模型请求"
        )
    return chosen


def _answer_request(runtime: RuntimeHandle, capture: EventCapture) -> dict:
    return runtime.call_recorder.requests[_answer_call(runtime, capture).sequence]


async def _answer_nodes(
    runtime: RuntimeHandle, capture: EventCapture
) -> RequestNodes:
    # 候选池固定到被评分请求的真实边界：回答节点的父亲即本次模型调用读取投影时的位置，
    # 之后的 Steering 消费、后续轮次与压缩都不改变本次请求实际可见的节点集合。
    entry = await runtime.service.queries.require_entry(
        capture.session_id, capture.answer_message_id
    )
    if entry.parent_id is None:
        raise ContextMappingError("回答节点缺少父节点，无法定位请求投影边界")
    return await read_request_nodes(
        runtime.service.queries, capture.session_id, entry.parent_id
    )


def _entry_to_event(captures: Sequence[EventCapture]) -> dict[str, str]:
    mapping = {}
    for capture in captures:
        if capture.request_entry_id is not None:
            mapping[capture.request_entry_id] = capture.event_id
    return mapping


def _bindings_for_projection(
    runtime: RuntimeHandle,
    projection: BusinessProjection,
    entry_to_event: dict[str, str],
    operation_by_ref: dict[str, str],
) -> dict[str, ProposalBinding]:
    return build_proposal_bindings(
        projection, entry_to_event, operation_by_ref
    )


def _binding_for(
    bindings: dict[str, ProposalBinding], reference: str
) -> ProposalBinding:
    binding = bindings.get(reference)
    if binding is None:
        raise AssertionInputError(f"proposal_ref 缺少唯一真实绑定: {reference}")
    return binding


def _message_state_target(
    scenario: dict, event: dict, assertions: Sequence[dict]
) -> EventTarget | None:
    # 消息检查点没有事件目标：失效迁移的目标只能来自场景中明确的后续取消事件目标。
    transitions = [
        assertion
        for assertion in assertions
        if assertion["kind"] == "state_transition"
        and assertion["to_status"] == "invalidated"
        and assertion["event_id"] == event["event_id"]
    ]
    if not transitions:
        return None
    kinds = {assertion["business_kind"] for assertion in transitions}
    if len(kinds) != 1:
        raise AssertionInputError("消息检查点的失效状态迁移目标不唯一")
    business_kind = next(iter(kinds))
    order = {
        row["event_id"]: index for index, row in enumerate(scenario["events"])
    }
    current = order[event["event_id"]]
    candidates = []
    for row in scenario["events"]:
        if row["type"] != "cancel" or row["target"] is None:
            continue
        if order[row["event_id"]] <= current:
            continue
        source = row["source_event_id"]
        if source not in order or order[source] >= current:
            continue
        target = event_target_for(row)
        if target is None or target.business_kind != business_kind:
            continue
        expected = f"{business_kind}-proposal-{source}"
        if target.proposal_ref != expected:
            raise AssertionInputError("取消事件目标与触发提案不一致")
        candidates.append(target)
    if len(candidates) != 1:
        raise AssertionInputError(
            "消息检查点的失效状态迁移缺少唯一后续取消事件目标"
        )
    return candidates[0]


def _checkpoint_cancel_evidence(
    item: CancelEvidence, link: EvidenceLink
) -> CancellationEvidence:
    return CancellationEvidence(
        checkpoint=link,
        session_id=item.session_id,
        assistant_run_id=item.run_id,
        request_entry_id=item.request_entry_id,
        source_entry_id=item.source_entry_id,
        result_entry_id=item.result_entry_id,
        tool_call_id=item.tool_call_id,
        business_kind=item.business_kind,
        proposal_id=item.proposal_id,
        result=item.result,
    )


def _event_identity(capture: EventCapture) -> EventIdentity | None:
    if capture.request_entry_id is None:
        return None
    return EventIdentity(
        session_id=capture.session_id,
        run_id=capture.run_id,
        request_entry_id=capture.request_entry_id,
    )


async def _assertion_evidence(
    runtime: RuntimeHandle,
    scenario: dict,
    event: dict,
    capture: EventCapture,
    checkpoint_id: str,
    assertions: Sequence[dict],
    bindings: dict[str, ProposalBinding],
) -> AssertionEvidence:
    link = EvidenceLink(
        run_id=scenario["_evaluation_run_id"],
        scenario_id=scenario["scenario_id"],
        event_id=event["event_id"],
        checkpoint_id=checkpoint_id,
    )
    references = {
        assertion["proposal_ref"]
        for assertion in assertions
        if "proposal_ref" in assertion
    }
    target = event_target_for(event)
    if target is None and event["type"] == "message":
        target = _message_state_target(scenario, event, assertions)
    if target is not None:
        references.add(target.proposal_ref)
    proposal_bindings = [_binding_for(bindings, ref) for ref in sorted(references)]
    before = capture.before
    after = capture.after
    profile_snapshots = []
    plan_snapshots = []
    workout_snapshots = []
    display_contents = []
    business_contents = []
    for binding in proposal_bindings:
        if binding.business_kind == "profile":
            snapshot = await runtime.business_repository.get_snapshot(
                binding.proposal_id
            )
            if snapshot is not None:
                profile_snapshots.append(ProfileSnapshotEvidence(
                    proposal_ref=binding.proposal_ref,
                    checkpoint=link,
                    snapshot=snapshot,
                ))
        elif binding.business_kind == "plan":
            snapshot = await runtime.business_repository.get_plan_snapshot(
                binding.proposal_id
            )
            if snapshot is not None:
                plan_snapshots.append(PlanSnapshotEvidence(
                    proposal_ref=binding.proposal_ref,
                    checkpoint=link,
                    snapshot=snapshot,
                ))
        else:
            snapshot = await runtime.business_repository.get_workout_snapshot(
                binding.proposal_id
            )
            if snapshot is not None:
                workout_snapshots.append(WorkoutSnapshotEvidence(
                    proposal_ref=binding.proposal_ref,
                    checkpoint=link,
                    snapshot=snapshot,
                ))
        if binding.display_entry_id is not None:
            entry = await runtime.service.queries.require_entry(
                capture.session_id, binding.display_entry_id
            )
            content, tool_call_id, tool_name = display_content_for(
                entry, binding.business_kind
            )
            display_contents.append(DisplayContentEvidence(
                proposal_ref=binding.proposal_ref,
                business_kind=binding.business_kind,
                checkpoint=link,
                display_entry_id=binding.display_entry_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                content=content,
            ))
    entries = _relevant_contents(
        after, capture, target, proposal_bindings
    )
    for binding, proposal in entries:
        has_display = any(
            item.proposal_ref == binding.proposal_ref
            for item in display_contents
        )
        business_contents.append(BusinessContentEvidence(
            business_kind=binding.business_kind,
            source="display" if has_display else "proposal",
            checkpoint=link,
            proposal_ref=binding.proposal_ref,
            payload=proposal_payload(proposal, binding.business_kind).model_dump(
                mode="json"
            ),
        ))
    parameter_target = None
    parameter_refs = {
        assertion["proposal_ref"]
        for assertion in assertions
        if assertion["kind"] == "parameter_preserved"
        and "proposal_ref" in assertion
    }
    if parameter_refs:
        if len(parameter_refs) != 1:
            raise AssertionInputError("参数断言的 proposal_ref 不唯一")
        reference = next(iter(parameter_refs))
        parameter_target = next(
            (
                item
                for item in business_contents
                if item.proposal_ref == reference
            ),
            None,
        )
    elif any(
        assertion["kind"] == "parameter_preserved" for assertion in assertions
    ):
        if len(business_contents) != 1:
            raise AssertionInputError("参数断言缺少唯一目标业务内容")
        parameter_target = business_contents[0]
    context_evidence = []
    tool_results = []
    rankings = []
    if capture.answer_message_id is not None:
        call = _answer_call(runtime, capture)
        request = runtime.call_recorder.requests[call.sequence]
        request_nodes = await _answer_nodes(runtime, capture)
        current_entries = current_input_entry_ids(
            request_nodes.nodes, capture.run_id, capture.request_entry_id
        )
        mapping = context_items_from_request(
            request,
            api=call.api,
            session_id=capture.session_id,
            current_entry_ids=current_entries,
            nodes=request_nodes.nodes,
            tool_nodes=request_nodes.tool_nodes,
        )
        capture.tool_identity_map = dict(mapping.tool_identity_map)
        capture.context_gaps = list(mapping.gaps)
        if mapping.ambiguous:
            raise ContextAmbiguousError(
                "模型上下文存在无法按真实节点身份绑定的正文: "
                + "; ".join(gap.reason for gap in mapping.gaps if gap.ambiguous)
            )
        from evaluation.deterministic_assertions import ContextEvidence

        for item in mapping.items:
            context_evidence.append(ContextEvidence(checkpoint=link, item=item))
        full_by_visible = tool_nodes_by_visible(
            request_nodes.tool_nodes, call.api
        )
        for item in mapping.items:
            if item.source != "tool_result":
                continue
            node = full_by_visible.get(item.tool_call_id)
            if node is None:
                raise ContextMappingError(
                    f"工具结果缺少完整持久化正文: {item.tool_call_id}"
                )
            tool_results.append(ToolResultEvidence(
                checkpoint=link,
                tool_call_id=item.tool_call_id,
                tool_name=item.tool_name,
                content=node.content,
            ))
            if (
                item.tool_name == "search_exercises"
                and not item.truncated
                and not node.is_error
            ):
                try:
                    search = search_evidence_from_tool(
                        node.content, item.tool_call_id
                    )
                except (ValueError, KeyError) as error:
                    raise ContextMappingError(
                        f"检索证据无法构造: {item.tool_call_id}: {error}"
                    ) from error
                rankings.append(RankingEvidence(
                    checkpoint=link,
                    tool_call_id=item.tool_call_id,
                    tool_name=item.tool_name,
                    search=search,
                ))
    cancel_evidence = []
    if capture.cancel_evidence is not None:
        cancel_evidence.append(
            _checkpoint_cancel_evidence(capture.cancel_evidence, link)
        )
    elif event["type"] == "message" and capture.request_entry_id is not None:
        for item in await read_cancel_evidence(
            runtime.service.queries, [capture.session_id]
        ):
            if (
                item.session_id == capture.session_id
                and item.run_id == capture.run_id
                and item.request_entry_id == capture.request_entry_id
            ):
                cancel_evidence.append(_checkpoint_cancel_evidence(item, link))
    return AssertionEvidence(
        checkpoint=link,
        event_type=event["type"],
        event_target=target,
        event_identity=_event_identity(capture),
        before=before,
        after=after,
        proposal_bindings=proposal_bindings,
        profile_snapshots=profile_snapshots,
        plan_snapshots=plan_snapshots,
        workout_snapshots=workout_snapshots,
        display_contents=display_contents,
        business_contents=business_contents,
        parameter_target=parameter_target,
        context_evidence=context_evidence,
        tool_results=tool_results,
        rankings=rankings,
        cancel_evidence=cancel_evidence,
    )


def _relevant_contents(
    projection: BusinessProjection,
    capture: EventCapture,
    target,
    bindings: Sequence[ProposalBinding],
) -> list[tuple[ProposalBinding, Any]]:
    if target is not None:
        return [
            (
                binding,
                next(
                    proposal
                    for proposal in projection.proposals
                    if proposal.business_kind == binding.business_kind
                    and proposal.proposal_id == binding.proposal_id
                ),
            )
            for binding in bindings
            if binding.proposal_ref == target.proposal_ref
        ]
    if capture.event_type == "message" and capture.request_entry_id is not None:
        selected = [
            binding
            for binding in bindings
            if any(
                proposal.request_entry_id == capture.request_entry_id
                for proposal in projection.proposals
                if proposal.business_kind == binding.business_kind
                and proposal.proposal_id == binding.proposal_id
            )
        ]
        return [
            (
                binding,
                next(
                    proposal
                    for proposal in projection.proposals
                    if proposal.business_kind == binding.business_kind
                    and proposal.proposal_id == binding.proposal_id
                ),
            )
            for binding in selected
        ]
    return []


def _assertion_error_artifact(
    checkpoint_id: str, event_id: str, error: AssertionInputError
) -> AssertionCheckpointArtifact:
    message = str(error)
    binding_failure = "proposal_ref" in message or "绑定" in message
    return AssertionCheckpointArtifact(
        checkpoint_id=checkpoint_id,
        event_id=event_id,
        binding_error=message if binding_failure else None,
        runner_error=None if binding_failure else message,
    )


async def _assertion_checkpoint_evidence(
    runtime: RuntimeHandle,
    dataset: EvaluationDataset,
    scenario: dict,
    event: dict,
    capture: EventCapture,
    bindings: dict[str, ProposalBinding],
) -> list[AssertionCheckpointEvidence]:
    grouped: dict[str, list[dict]] = {}
    for assertion in scenario["assertions"]:
        if assertion["event_id"] != event["event_id"]:
            continue
        grouped.setdefault(assertion["checkpoint_id"], []).append(assertion)
    entries = []
    for checkpoint_id, assertions in grouped.items():
        definitions = [dict(assertion) for assertion in assertions]
        try:
            evidence = await _assertion_evidence(
                runtime,
                scenario,
                event,
                capture,
                checkpoint_id,
                assertions,
                bindings,
            )
        except AssertionInputError as error:
            message = str(error)
            binding_failure = "proposal_ref" in message or "绑定" in message
            entries.append(AssertionCheckpointEvidence(
                checkpoint_id=checkpoint_id,
                event_id=event["event_id"],
                assertions=definitions,
                binding_error=message if binding_failure else None,
                runner_error=None if binding_failure else message,
            ))
        except ContextMappingError as error:
            entries.append(AssertionCheckpointEvidence(
                checkpoint_id=checkpoint_id,
                event_id=event["event_id"],
                assertions=definitions,
                runner_error=str(error),
            ))
        else:
            entries.append(AssertionCheckpointEvidence(
                checkpoint_id=checkpoint_id,
                event_id=event["event_id"],
                assertions=definitions,
                evidence=evidence,
            ))
    return entries


def _evaluate_assertion_entry(
    entry: AssertionCheckpointEvidence,
) -> AssertionCheckpointArtifact:
    if entry.binding_error is not None:
        return AssertionCheckpointArtifact(
            checkpoint_id=entry.checkpoint_id,
            event_id=entry.event_id,
            binding_error=entry.binding_error,
        )
    if entry.runner_error is not None:
        return AssertionCheckpointArtifact(
            checkpoint_id=entry.checkpoint_id,
            event_id=entry.event_id,
            runner_error=entry.runner_error,
        )
    if entry.evidence is None:
        raise ExecutionIdentityError("断言检查点缺少可评分证据")
    try:
        outcomes = evaluate_checkpoint(entry.assertions, entry.evidence)
    except AssertionInputError as error:
        return _assertion_error_artifact(
            entry.checkpoint_id, entry.event_id, error
        )
    return AssertionCheckpointArtifact(
        checkpoint_id=entry.checkpoint_id,
        event_id=entry.event_id,
        outcomes=outcomes,
    )


async def _evaluate_assertions(
    runtime: RuntimeHandle,
    dataset: EvaluationDataset,
    scenario: dict,
    event: dict,
    capture: EventCapture,
    bindings: dict[str, ProposalBinding],
) -> list[AssertionCheckpointArtifact]:
    entries = await _assertion_checkpoint_evidence(
        runtime, dataset, scenario, event, capture, bindings
    )
    return [_evaluate_assertion_entry(entry) for entry in entries]


def generation_reference(scenario: dict, checkpoint_id: str) -> GenerationReference:
    reference = next(
        item
        for item in scenario["reference"]
        if item["checkpoint_id"] == checkpoint_id
    )
    rubric = next(
        item
        for item in scenario["rubric"]
        if item["checkpoint_id"] == checkpoint_id
    )
    from evaluation.contracts import ReferenceFact

    return GenerationReference(
        answer=reference["answer"],
        facts=[
            ReferenceFact(
                fact_id=fact["fact_id"],
                text=fact["text"],
                source=fact["source"],
                asset_id=fact["asset_id"],
            )
            for fact in reference["facts"]
        ],
        must_include=list(rubric["must_include"]),
        allowed_variation=list(rubric["allowed_variation"]),
        forbidden_claims=list(rubric["forbidden_claims"]),
    )


def _current_input_text(
    items: Sequence[ContextItem], current_entry_ids: set[str]
) -> str:
    # 当前输入取真实请求中绑定到本次运行用户节点的正文：纯附件回合的模型输入即附件投影。
    return "\n\n".join(
        item.content
        for item in items
        if item.source in {"user_message", "attachment"}
        and item.message_entry_id in current_entry_ids
    )


async def _generation_checkpoint(
    runtime: RuntimeHandle,
    scenario: dict,
    event: dict,
    capture: EventCapture,
    checkpoint_id: str,
) -> tuple[GenerationCheckpoint, RequestBoundaryEvidence]:
    if capture.answer_text is None:
        raise ScenarioBlocked(
            scenario["scenario_id"], event["event_id"], "回答文本缺失，禁止填占位答案"
        )
    call = _answer_call(runtime, capture)
    request = runtime.call_recorder.requests[call.sequence]
    try:
        request_nodes = await _answer_nodes(runtime, capture)
        current_ids = current_input_entry_ids(
            request_nodes.nodes, capture.run_id, capture.request_entry_id
        )
        mapping = context_items_from_request(
            request,
            api=call.api,
            session_id=capture.session_id,
            current_entry_ids=current_ids,
            nodes=request_nodes.nodes,
            tool_nodes=request_nodes.tool_nodes,
        )
    except ContextMappingError as error:
        raise ScenarioBlocked(
            scenario["scenario_id"], event["event_id"], str(error)
        ) from error
    capture.tool_identity_map = dict(mapping.tool_identity_map)
    seen = {
        (gap.reason, gap.message_entry_id, gap.tool_call_id)
        for gap in capture.context_gaps
    }
    for gap in mapping.gaps:
        key = (gap.reason, gap.message_entry_id, gap.tool_call_id)
        if key not in seen:
            capture.context_gaps.append(gap)
            seen.add(key)
    if mapping.ambiguous:
        raise ContextAmbiguousError(
            "模型上下文存在无法按真实节点身份绑定的正文: "
            + "; ".join(gap.reason for gap in mapping.gaps if gap.ambiguous)
        )
    current_text = _current_input_text(mapping.items, current_ids)
    frozen_text = (event["content"] or "").strip()
    if frozen_text and frozen_text not in current_text:
        raise ScenarioBlocked(
            scenario["scenario_id"], event["event_id"],
            "真实请求的当前用户正文与冻结事件输入不一致",
            details={"event_content_sha256": sha256_text(frozen_text),
                     "current_input_sha256": sha256_text(current_text)},
        )
    if not current_text:
        raise ScenarioBlocked(
            scenario["scenario_id"], event["event_id"],
            "真实请求未包含当前输入的用户正文，禁止填占位答案"
        )
    applicability = _frozen_generation_applicability(scenario, checkpoint_id)
    reference = (
        generation_reference(scenario, checkpoint_id)
        if any(row.applicable for row in applicability)
        else None
    )
    link = EvidenceLink(
        run_id=scenario["_evaluation_run_id"],
        scenario_id=scenario["scenario_id"],
        event_id=event["event_id"],
        checkpoint_id=checkpoint_id,
    )
    boundary = RequestBoundaryEvidence(
        api=call.api,
        session_id=capture.session_id,
        run_id=capture.run_id,
        request_entry_id=capture.request_entry_id,
        nodes=request_nodes.nodes,
        tool_nodes=request_nodes.tool_nodes,
    )
    return GenerationCheckpoint(
        checkpoint=link,
        user_input=UserInputEvidence(
            message=current_text,
            prior_messages=[
                {"role": row["role"], "content": row["content"]}
                for row in scenario["input"]["prior_messages"]
            ],
        ),
        answer=AnswerEvidence(
            visible_text=capture.answer_text,
            structured_snapshot=capture.answer_structured,
        ),
        model_input=ModelInputContext(items=mapping.items),
        reference=reference,
        applicability=applicability,
    ), boundary


def _frozen_generation_applicability(
    scenario: dict, checkpoint_id: str
) -> list[MetricApplicability]:
    return [
        MetricApplicability(
            metric=row["metric"],
            applicable=row["applicable"],
            reason=row["reason"],
        )
        for row in scenario["metric_applicability"]
        if row["checkpoint_id"] == checkpoint_id
        and row["metric"] in GENERATION_METRICS
    ]


async def _generation_checkpoint_evidence(
    runtime: RuntimeHandle,
    scenario: dict,
    event: dict,
    capture: EventCapture,
) -> list[GenerationCheckpointEvidence]:
    entries: list[GenerationCheckpointEvidence] = []
    covered: set[str] = set()
    for reference in scenario["reference"]:
        if reference["event_id"] != event["event_id"]:
            continue
        checkpoint_id = reference["checkpoint_id"]
        applicability = _frozen_generation_applicability(
            scenario, checkpoint_id
        )
        try:
            checkpoint, boundary = await _generation_checkpoint(
                runtime, scenario, event, capture, checkpoint_id
            )
        except ContextAmbiguousError as error:
            entries.append(GenerationCheckpointEvidence(
                checkpoint_id=checkpoint_id,
                event_id=event["event_id"],
                applicability=applicability,
                runner_error=sanitize_error_text(str(error)),
            ))
        else:
            entries.append(GenerationCheckpointEvidence(
                checkpoint_id=checkpoint_id,
                event_id=event["event_id"],
                applicability=applicability,
                checkpoint=checkpoint,
                request_boundary=boundary,
            ))
        covered.add(checkpoint_id)
    for row in scenario["metric_applicability"]:
        if row["event_id"] != event["event_id"]:
            continue
        if row["metric"] not in GENERATION_METRICS:
            continue
        checkpoint_id = row["checkpoint_id"]
        if checkpoint_id in covered:
            continue
        applicability = _frozen_generation_applicability(
            scenario, checkpoint_id
        )
        if len(applicability) != len(GENERATION_METRICS):
            raise ScenarioBlocked(
                scenario["scenario_id"],
                event["event_id"],
                f"检查点缺少生成指标适用性记录: {checkpoint_id}",
            )
        if any(item.applicable for item in applicability):
            raise ScenarioBlocked(
                scenario["scenario_id"],
                event["event_id"],
                f"检查点声明生成指标适用但缺少 reference: {checkpoint_id}",
            )
        entries.append(GenerationCheckpointEvidence(
            checkpoint_id=checkpoint_id,
            event_id=event["event_id"],
            applicability=applicability,
        ))
        covered.add(checkpoint_id)
    return entries


def _not_applicable_generation(
    scenario: dict,
    event: dict,
    checkpoint_id: str,
) -> GenerationCheckpointArtifact:
    rows = [
        row
        for row in scenario["metric_applicability"]
        if row["checkpoint_id"] == checkpoint_id
        and row["metric"] in GENERATION_METRICS
    ]
    if not rows:
        raise ScenarioBlocked(
            scenario["scenario_id"],
            event["event_id"],
            f"检查点缺少生成指标适用性记录: {checkpoint_id}",
        )
    if any(row["applicable"] for row in rows):
        raise ScenarioBlocked(
            scenario["scenario_id"],
            event["event_id"],
            f"检查点声明生成指标适用但缺少 reference: {checkpoint_id}",
        )
    for row in rows:
        if not row["reason"]:
            raise ScenarioBlocked(
                scenario["scenario_id"],
                event["event_id"],
                f"不适用指标缺少冻结原因: {checkpoint_id}/{row['metric']}",
            )
    link = EvidenceLink(
        run_id=scenario["_evaluation_run_id"],
        scenario_id=scenario["scenario_id"],
        event_id=event["event_id"],
        checkpoint_id=checkpoint_id,
    )
    return GenerationCheckpointArtifact(
        checkpoint_id=checkpoint_id,
        event_id=event["event_id"],
        checkpoint=None,
        result=CheckpointResult(
            checkpoint=link,
            outcomes=[
                MetricOutcome(
                    metric=row["metric"],
                    status="not_applicable",
                    reason=row["reason"],
                )
                for row in rows
            ],
        ),
    )


def _score_assertion_entries(
    execution: ScenarioExecution,
) -> list[AssertionCheckpointArtifact]:
    return [
        _evaluate_assertion_entry(entry)
        for entry in execution.assertion_checkpoints
    ]


def _not_applicable_generation_artifact(
    execution: ScenarioExecution,
    entry: GenerationCheckpointEvidence,
) -> GenerationCheckpointArtifact:
    link = EvidenceLink(
        run_id=execution.run_id,
        scenario_id=execution.scenario_id,
        event_id=entry.event_id,
        checkpoint_id=entry.checkpoint_id,
    )
    return GenerationCheckpointArtifact(
        checkpoint_id=entry.checkpoint_id,
        event_id=entry.event_id,
        checkpoint=None,
        result=CheckpointResult(
            checkpoint=link,
            outcomes=[
                MetricOutcome(
                    metric=row.metric,
                    status="not_applicable",
                    reason=row.reason,
                )
                for row in entry.applicability
            ],
        ),
    )


async def _score_generation_entries(
    execution: ScenarioExecution,
    entries: Sequence[GenerationCheckpointEvidence],
    judge: JudgeConfig | None,
    timeout: float | None,
) -> list[GenerationCheckpointArtifact]:
    artifacts = []
    if not entries:
        return artifacts
    applicable_entry = next(
        (
            entry
            for entry in entries
            if entry.checkpoint is not None
            and entry.runner_error is None
            and any(row.applicable for row in entry.applicability)
        ),
        None,
    )
    if applicable_entry is not None and judge is None:
        raise ScenarioModelUnavailable(
            execution.scenario_id,
            applicable_entry.event_id,
            "生成指标适用但缺少裁判配置",
        )
    groups: dict[str, list[GenerationCheckpointEvidence]] = {}
    for entry in entries:
        groups.setdefault(entry.event_id, []).append(entry)
    for group in groups.values():
        artifacts.extend(
            await _score_generation_group(execution, group, judge, timeout)
        )
    return artifacts


async def _score_generation_group(
    execution: ScenarioExecution,
    entries: Sequence[GenerationCheckpointEvidence],
    judge: JudgeConfig | None,
    timeout: float | None,
) -> list[GenerationCheckpointArtifact]:
    artifacts = []
    async with GenerationScoringSession(judge, timeout=timeout) as session:
        for entry in entries:
            if entry.runner_error is not None:
                artifacts.append(GenerationCheckpointArtifact(
                    checkpoint_id=entry.checkpoint_id,
                    event_id=entry.event_id,
                    runner_error=entry.runner_error,
                ))
                continue
            if entry.checkpoint is None:
                artifacts.append(
                    _not_applicable_generation_artifact(execution, entry)
                )
                continue
            checkpoint = entry.checkpoint
            outcome_start = len(session.outcomes)
            evidence_start = len(session.evidence)
            try:
                result = await session.score(checkpoint)
                evidence = list(session.evidence[evidence_start:])
                attempts = _attempt_snapshots(session, checkpoint)
                artifacts.append(GenerationCheckpointArtifact(
                    checkpoint_id=entry.checkpoint_id,
                    event_id=entry.event_id,
                    checkpoint=checkpoint,
                    result=result,
                    evidence=evidence,
                    attempts=attempts,
                    request_boundary=entry.request_boundary,
                ))
            except Exception as error:
                by_metric = {
                    row.metric: row
                    for row in session.outcomes[outcome_start:]
                }
                attempt = session.current_attempt
                failed_metric = (
                    None
                    if attempt is None
                    or attempt.checkpoint.checkpoint != checkpoint.checkpoint
                    else attempt.plan.metric
                )
                if failed_metric is None:
                    failed_metric = next(
                        (
                            row.metric
                            for row in entry.applicability
                            if row.applicable and row.metric not in by_metric
                        ),
                        None,
                    )
                if failed_metric is not None:
                    by_metric[failed_metric] = failed_outcome(
                        failed_metric, error
                    )
                incomplete = []
                for row in entry.applicability:
                    if row.metric in by_metric:
                        continue
                    if row.applicable:
                        incomplete.append(row.metric)
                        by_metric[row.metric] = MetricOutcome(
                            metric=row.metric,
                            status="applicable",
                        )
                    else:
                        by_metric[row.metric] = MetricOutcome(
                            metric=row.metric,
                            status="not_applicable",
                            reason=row.reason,
                        )
                produced = sorted(
                    by_metric.values(),
                    key=lambda row: (
                        "faithfulness",
                        "factual_correctness",
                    ).index(row.metric),
                )
                artifacts.append(GenerationCheckpointArtifact(
                    checkpoint_id=entry.checkpoint_id,
                    event_id=entry.event_id,
                    checkpoint=checkpoint,
                    result=CheckpointResult(
                        checkpoint=checkpoint.checkpoint,
                        outcomes=produced,
                    ),
                    evidence=list(session.evidence[evidence_start:]),
                    attempts=_attempt_snapshots(session, checkpoint),
                    request_boundary=entry.request_boundary,
                    runner_error=f"{type(error).__name__}: {error}",
                    incomplete_metrics=incomplete,
                ))
    return artifacts


async def score_execution(
    execution: ScenarioExecution,
    *,
    judge: JudgeConfig | None = None,
    judge_timeout: float | None = DEFAULT_JUDGE_TIMEOUT,
) -> ScenarioArtifact:
    verify_execution_identity(execution)
    assertions = _score_assertion_entries(execution)
    generation = await _score_generation_entries(
        execution,
        execution.generation_checkpoints,
        judge,
        judge_timeout,
    )
    return ScenarioArtifact(
        run_id=execution.run_id,
        scenario_id=execution.scenario_id,
        session_ids=list(execution.session_ids),
        events=list(execution.events),
        assertions=assertions,
        generation=generation,
        retrieval=list(execution.retrieval),
        proposal_bindings=dict(execution.proposal_bindings),
        generation_metrics=dict(execution.generation_metrics),
        cancel_evidence=list(execution.cancel_evidence),
        context_gaps=list(execution.context_gaps),
        blocked=execution.error if execution.status == "blocked" else None,
        runner_error=execution.error if execution.status == "failed" else None,
    )


def scoring_protocol(scoring_mode: ScoringMode) -> str:
    try:
        return SCORING_PROTOCOLS[scoring_mode]
    except KeyError as error:
        raise ScenarioError(f"未知 scoring_mode: {scoring_mode!r}") from error


def _external_readiness(
    execution: ScenarioExecution,
) -> list[ExternalScoringReadiness]:
    entries = {
        entry.checkpoint_id: entry
        for entry in execution.generation_checkpoints
    }
    planned_ids = sorted(
        set(execution.generation_metrics)
        | {entry.checkpoint_id for entry in execution.generation_checkpoints}
    )
    readiness: list[ExternalScoringReadiness] = []
    for checkpoint_id in planned_ids:
        entry = entries.get(checkpoint_id)
        if entry is None:
            readiness.append(ExternalScoringReadiness(
                checkpoint_id=checkpoint_id,
                status="missing",
                missing=["generation_checkpoint"],
            ))
            continue
        if entry.runner_error is not None:
            readiness.append(ExternalScoringReadiness(
                checkpoint_id=checkpoint_id,
                event_id=entry.event_id,
                status="missing",
                error=entry.runner_error,
                missing=["generation_runner_error"],
            ))
            continue
        if entry.checkpoint is None:
            readiness.append(ExternalScoringReadiness(
                checkpoint_id=checkpoint_id,
                event_id=entry.event_id,
                status="no_generation_evidence",
            ))
            continue
        missing = []
        if entry.request_boundary is None:
            missing.append("request_boundary")
        if not entry.checkpoint.answer.visible_text.strip():
            missing.append("answer")
        readiness.append(
            ExternalScoringReadiness(
                checkpoint_id=checkpoint_id,
                event_id=entry.event_id,
                status="missing" if missing else "ready",
                missing=missing,
            )
        )
    return readiness


def assertion_statistics(
    assertions: Sequence[AssertionCheckpointArtifact],
) -> dict[str, int]:
    outcomes = [
        outcome for checkpoint in assertions for outcome in checkpoint.outcomes
    ]
    return {
        "passed": sum(1 for outcome in outcomes if outcome.passed),
        "failed": sum(1 for outcome in outcomes if not outcome.passed),
        "binding_errors": sum(
            1 for checkpoint in assertions if checkpoint.binding_error is not None
        ),
        "runner_errors": sum(
            1 for checkpoint in assertions if checkpoint.runner_error is not None
        ),
    }


def _external_assertion_status(
    assertions: Sequence[AssertionCheckpointArtifact],
    missing_checkpoint_ids: Sequence[str],
) -> str:
    counts = assertion_statistics(assertions)
    if counts["runner_errors"]:
        return "runner_error"
    if counts["binding_errors"]:
        return "binding_error"
    if counts["failed"]:
        return "failed"
    if missing_checkpoint_ids:
        return "incomplete"
    return "passed"


def _external_incomplete_items(
    assertions: Sequence[AssertionCheckpointArtifact],
    readiness: Sequence[ExternalScoringReadiness],
    assertion_gaps: Sequence[tuple[str, str, str]],
) -> list[str]:
    items: list[str] = []
    for checkpoint in assertions:
        if checkpoint.binding_error is not None:
            items.append(f"{checkpoint.event_id}/{checkpoint.checkpoint_id}:binding_error")
        if checkpoint.runner_error is not None:
            items.append(f"{checkpoint.event_id}/{checkpoint.checkpoint_id}:runner_error")
        for index, outcome in enumerate(checkpoint.outcomes):
            if not outcome.passed:
                items.append(
                    f"{checkpoint.event_id}/{checkpoint.checkpoint_id}"
                    f":assertion_failed:{outcome.kind}:{index}"
                )
    for event_id, checkpoint_id, reason in assertion_gaps:
        items.append(f"{event_id}/{checkpoint_id}:{reason}")
    for entry in readiness:
        if entry.status != "missing":
            continue
        detail = ",".join(entry.missing)
        suffix = f":{detail}" if detail else ""
        items.append(
            f"{entry.checkpoint_id}:generation_evidence_missing{suffix}"
        )
    return items


def expected_assertion_definitions(
    assertions: Sequence[dict[str, Any]],
) -> dict[tuple[str, str], tuple[str, ...]]:
    grouped: dict[tuple[str, str], list[str]] = {}
    for row in assertions:
        parsed = parse_assertion(row)
        grouped.setdefault(
            (parsed.event_id, parsed.checkpoint_id), []
        ).append(parsed.kind)
    return {key: tuple(sorted(kinds)) for key, kinds in grouped.items()}


def score_execution_external(
    execution: ScenarioExecution,
    assertions: Sequence[dict[str, Any]],
) -> ExternalScenarioScoring:
    # 外部模式只读取封存执行并运行确定性断言；不创建评分会话、不调用任何裁判。
    verify_execution_identity(execution)
    expected_assertions = expected_assertion_definitions(assertions)
    planned_checkpoints = set(execution.expected_checkpoint_ids)
    unknown = {
        checkpoint_id
        for _, checkpoint_id in expected_assertions
        if checkpoint_id not in planned_checkpoints
    }
    if unknown:
        raise ExecutionIdentityError(
            f"冻结断言不在执行检查点计划中: {sorted(unknown)}"
        )
    observed_assertions = {
        (entry.event_id, entry.checkpoint_id): tuple(
            sorted(parse_assertion(row).kind for row in entry.assertions)
        )
        for entry in execution.assertion_checkpoints
    }
    assertion_gaps: list[tuple[str, str, str]] = []
    for (event_id, checkpoint_id), kinds in expected_assertions.items():
        observed = observed_assertions.get((event_id, checkpoint_id))
        if observed is None:
            assertion_gaps.append((event_id, checkpoint_id, "assertion_missing"))
        elif observed != kinds:
            assertion_gaps.append(
                (event_id, checkpoint_id, "assertion_definition_mismatch")
            )
    assertion_gaps.sort()
    missing_assertion_ids = sorted(
        {checkpoint_id for _, checkpoint_id, _ in assertion_gaps}
    )
    artifacts = _score_assertion_entries(execution)
    readiness = _external_readiness(execution)
    return ExternalScenarioScoring(
        run_id=execution.run_id,
        scenario_id=execution.scenario_id,
        execution_sha256=execution.execution_sha256,
        execution_status=execution.status,
        blocked=execution.error if execution.status == "blocked" else None,
        runner_error=execution.error if execution.status == "failed" else None,
        assertion_status=_external_assertion_status(
            artifacts, missing_assertion_ids
        ),
        generation_scoring_status=(
            "incomplete"
            if any(item.status == "missing" for item in readiness)
            else "pending_external"
        ),
        expected_event_ids=list(execution.expected_event_ids),
        observed_event_ids=[event.event_id for event in execution.events],
        expected_checkpoint_ids=list(execution.expected_checkpoint_ids),
        missing_assertion_checkpoint_ids=missing_assertion_ids,
        assertions=artifacts,
        generation=readiness,
        incomplete=_external_incomplete_items(
            artifacts, readiness, assertion_gaps
        ),
        context_gaps=[gap.reason for gap in execution.context_gaps],
    )


def write_external_scoring_result(
    execution: ScenarioExecution,
    record: ExternalScenarioScoring,
    *,
    path: Path,
) -> None:
    verify_execution_identity(execution)
    if (
        record.run_id != execution.run_id
        or record.scenario_id != execution.scenario_id
        or record.execution_sha256 != execution.execution_sha256
    ):
        raise ExecutionIdentityError("外部评分结果身份与执行结果不一致")
    path = Path(path)
    if path.exists():
        raise ScoringResultExists(f"外部评分产物已存在，拒绝覆盖: {path}")
    _write_json(path, record.model_dump(mode="json"))


def load_external_scoring(
    path: Path,
    *,
    expected_execution_sha256: str | None = None,
) -> ExternalScenarioScoring:
    path = Path(path)
    if path.is_dir():
        path = path / EXTERNAL_SCORING_FILENAME
    record = ExternalScenarioScoring.model_validate_json(
        path.read_text(encoding="utf-8")
    )
    if (
        expected_execution_sha256 is not None
        and record.execution_sha256 != expected_execution_sha256
    ):
        raise ExecutionIdentityError("外部评分结果与执行摘要不一致")
    return record


def _attempt_snapshots(session, checkpoint) -> list[ScoringAttemptSnapshot]:
    snapshots = []
    for attempt in session.attempts:
        if attempt.checkpoint.checkpoint != checkpoint.checkpoint:
            continue
        try:
            snapshots.append(attempt.snapshot())
        except Exception:
            continue
    return snapshots


def _judge_config() -> tuple[JudgeConfig | None, float | None]:
    try:
        model_name = load_judge_model(os.environ)
    except JudgeConfigError:
        return None, None
    base_url = (os.environ.get(JUDGE_BASE_URL_VARIABLE) or "").strip()
    if not base_url:
        return None, None
    judge_api_key = (os.environ.get(JUDGE_API_KEY_VARIABLE) or "").strip()
    if not judge_api_key:
        return None, None
    return (
        JudgeConfig(
            model=model_name,
            base_url=base_url,
            api_key=judge_api_key,
        ),
        DEFAULT_JUDGE_TIMEOUT,
    )


async def _run_confirm_event(
    runtime: RuntimeHandle,
    scenario: dict,
    event: dict,
    session_id: str,
    session_ids: Sequence[str],
    bindings: dict[str, ProposalBinding],
    operation_by_ref: dict[str, str],
    repeated: set[str],
    now: int,
) -> EventCapture:
    before = await runtime.projection(session_ids)
    target = event["target"]
    reference = target["proposal_ref"]
    try:
        binding = _binding_for(bindings, reference)
    except AssertionInputError as error:
        raise ScenarioBlocked(
            scenario["scenario_id"], event["event_id"], str(error)
        ) from error
    if binding.display_entry_id is None:
        raise ScenarioBlocked(
            scenario["scenario_id"],
            event["event_id"],
            f"确认目标缺少展示绑定: {reference}",
        )
    operation_id = str(
        uuid5(
            uuid.NAMESPACE_URL,
            f"fit-agent:evaluation:{scenario['_evaluation_run_id']}:"
            f"{scenario['scenario_id']}:confirm:{reference}",
        )
    )
    reused = reference in repeated
    repeated.add(reference)
    request = SaveConfirmationRequest(
        operation_id=operation_id,
        business_kind=binding.business_kind,
        proposal_id=binding.proposal_id,
        display_entry_id=binding.display_entry_id,
    )
    secrets = ()
    try:
        model = load_model_config()
        secrets = _active_secrets(model)
    except Exception:
        secrets = ()
    operation = await runtime.confirmations.confirm(
        session_id,
        request,
        trace_store=None,
        contains=CredentialFilter(secrets).contains,
        now=now,
    )
    operation_by_ref[reference] = operation.operation_id
    after = await runtime.projection(session_ids)
    return EventCapture(
        run_id=operation.confirmation_entry_id,
        scenario_id=scenario["scenario_id"],
        event_id=event["event_id"],
        event_type="confirm",
        source_event_id=event["source_event_id"],
        session_id=session_id,
        operation_id=operation_id,
        request_entry_id=operation.confirmation_entry_id,
        terminal_status=operation.status,
        terminal_event=None,
        before=before,
        after=after,
        operation_id_reused=reused,
        error=None
        if operation.status != "failed"
        else None
        if operation.error is None
        else operation.error.model_dump_json(),
    )


def _proposal_status(
    projection: BusinessProjection, business_kind: str, proposal_id: str
) -> str | None:
    for proposal in projection.proposals:
        if (
            proposal.business_kind == business_kind
            and proposal.proposal_id == proposal_id
        ):
            return proposal.status
    return None


def _cancel_driving_capture(
    captures: Sequence[EventCapture], evidence: CancelEvidence
) -> EventCapture | None:
    # 驱动事件按（会话, 运行）复合身份匹配：同名调用 ID 属于其它会话或运行时不得关联。
    same_identity = [
        capture
        for capture in captures
        if capture.session_id == evidence.session_id
        and capture.run_id == evidence.run_id
    ]
    for capture in same_identity:
        if any(
            call.tool_call_id == evidence.tool_call_id for call in capture.tool_calls
        ):
            return capture
    for capture in same_identity:
        if capture.request_entry_id == evidence.request_entry_id:
            return capture
    return None


async def _run_cancel_event(
    runtime: RuntimeHandle,
    scenario: dict,
    event: dict,
    session_id: str,
    session_ids: Sequence[str],
    bindings: dict[str, ProposalBinding],
    captures: Sequence[EventCapture],
) -> EventCapture:
    before = await runtime.projection(session_ids)
    reference = event["target"]["proposal_ref"]
    try:
        binding = _binding_for(bindings, reference)
    except AssertionInputError as error:
        raise ScenarioBlocked(
            scenario["scenario_id"], event["event_id"], str(error)
        ) from error
    evidence_items = await read_cancel_evidence(
        runtime.service.queries, session_ids
    )
    matches = [
        item
        for item in evidence_items
        if item.business_kind == binding.business_kind
        and item.proposal_id == binding.proposal_id
    ]
    if not matches:
        raise ScenarioBlocked(
            scenario["scenario_id"],
            event["event_id"],
            "冻结 cancel 事件缺少成功的 cancel_pending_proposal 工具结果；"
            "单独 invalidated 状态只作为状态事实，不作为取消成功证据。",
            details={"before_status": _proposal_status(
                before, binding.business_kind, binding.proposal_id
            )},
        )
    if len(matches) > 1:
        raise ScenarioBlocked(
            scenario["scenario_id"],
            event["event_id"],
            "同一提案存在多个成功取消工具结果，无法唯一绑定取消证据。",
            details={"tool_call_ids": [item.tool_call_id for item in matches]},
        )
    evidence = matches[0]
    driving = _cancel_driving_capture(captures, evidence)
    if driving is None:
        raise ScenarioBlocked(
            scenario["scenario_id"],
            event["event_id"],
            "取消证据无法关联到真实消息事件。",
            details=evidence.model_dump(mode="json"),
        )
    after = await runtime.projection(session_ids)
    return EventCapture(
        run_id=evidence.run_id,
        scenario_id=scenario["scenario_id"],
        event_id=event["event_id"],
        event_type="cancel",
        source_event_id=event["source_event_id"],
        session_id=evidence.session_id,
        operation_id=None,
        request_entry_id=evidence.request_entry_id,
        terminal_status="completed",
        terminal_event=None,
        before=before,
        after=after,
        cancel_evidence=evidence,
        driving_event_id=driving.event_id,
    )


async def _run_session_switch(
    runtime: RuntimeHandle, scenario: dict, event: dict, session_ids: list[str]
) -> tuple[EventCapture, str]:
    before = await runtime.projection(session_ids)
    new_session_id = str(uuid4())
    await runtime.service.create_session_result(
        new_session_id,
        f"{scenario['scenario_id']}-{event['event_id']}",
        credentials=(),
    )
    session_ids.append(new_session_id)
    after = await runtime.projection(session_ids)
    capture = EventCapture(
        run_id=new_session_id,
        scenario_id=scenario["scenario_id"],
        event_id=event["event_id"],
        event_type="session_switch",
        source_event_id=None,
        session_id=new_session_id,
        operation_id=None,
        request_entry_id=None,
        terminal_status="completed",
        terminal_event=None,
        before=before,
        after=after,
    )
    return capture, new_session_id


async def _run_restart_event(
    runtime: RuntimeHandle,
    scenario: dict,
    event: dict,
    session_id: str,
    session_ids: Sequence[str],
) -> EventCapture:
    before = await runtime.projection(session_ids)
    await runtime.close()
    await runtime.open()
    after = await runtime.projection(session_ids)
    return EventCapture(
        run_id=str(uuid4()),
        scenario_id=scenario["scenario_id"],
        event_id=event["event_id"],
        event_type="restart",
        source_event_id=None,
        session_id=session_id,
        operation_id=None,
        request_entry_id=None,
        terminal_status="completed",
        terminal_event=None,
        before=before,
        after=after,
    )


def _observed_checkpoints(
    assertions: Sequence[AssertionCheckpointArtifact],
    generation: Sequence[GenerationCheckpointArtifact],
) -> int:
    return len(
        {
            (item.event_id, item.checkpoint_id)
            for item in assertions
            if item.outcomes
        }
        | {
            (item.event_id, item.checkpoint_id)
            for item in generation
            if item.result is not None
        }
    )


async def _partial_artifact(
    runtime: RuntimeHandle,
    scenario: dict,
    *,
    run_id: str,
    captures: Sequence[EventCapture],
    assertions: Sequence[AssertionCheckpointArtifact],
    generation: Sequence[GenerationCheckpointArtifact],
    bindings: dict[str, ProposalBinding],
    retrieval: Sequence[SearchStageRecord],
    error: Exception,
) -> ScenarioArtifact:
    # 阻塞或失败场景保留已真实产出的事件、断言、评分与检索证据，供运行汇总按真实状态登记。
    blocked = isinstance(error, ScenarioBlocked)
    cancel_evidence: list[CancelEvidence] = []
    if runtime.service is not None:
        cancel_evidence = await read_cancel_evidence(
            runtime.service.queries, scenario["_session_ids"]
        )
    return ScenarioArtifact(
        run_id=run_id,
        scenario_id=scenario["scenario_id"],
        purpose="validation",
        session_ids=list(scenario["_session_ids"]),
        events=list(captures),
        assertions=list(assertions),
        generation=list(generation),
        retrieval=list(retrieval),
        proposal_bindings=dict(bindings),
        generation_metrics=scenario_plan(scenario).generation_metrics,
        cancel_evidence=cancel_evidence,
        context_gaps=[gap for capture in captures for gap in capture.context_gaps],
        blocked=sanitize_error_text(str(error)) if blocked else None,
        runner_error=(
            None
            if blocked
            else sanitize_error_text(f"{type(error).__name__}: {error}")
        ),
    )


def _execution_checkpoint_count(
    assertions: Sequence[AssertionCheckpointEvidence],
    generation: Sequence[GenerationCheckpointEvidence],
) -> int:
    return len(
        {
            (entry.event_id, entry.checkpoint_id)
            for entry in assertions
            if entry.evidence is not None
        }
        | {
            (entry.event_id, entry.checkpoint_id)
            for entry in generation
            if entry.checkpoint is not None
        }
    )


def _external_observed_checkpoints(execution: ScenarioExecution) -> int:
    # 外部模式把真实到达的检查点（含错误与无生成证据）计为已观测；缺失仍由 incomplete 报告。
    return len(
        {
            (entry.event_id, entry.checkpoint_id)
            for entry in execution.assertion_checkpoints
        }
        | {
            (entry.event_id, entry.checkpoint_id)
            for entry in execution.generation_checkpoints
        }
    )


def _require_execution_config(
    scenario: dict,
    *,
    enable_model: bool,
    judge: JudgeConfig | None,
    judge_timeout: float | None,
    scoring_mode: ScoringMode = "ragas",
) -> None:
    if not enable_model:
        raise ScenarioModelUnavailable(
            scenario["scenario_id"], None, "未显式启用真实模型调用"
        )
    prior_messages = scenario["input"]["prior_messages"]
    needs_model = any(
        event["type"] == "message" for event in scenario["events"]
    ) or any(row["role"] == "assistant" for row in prior_messages)
    model_available = True
    if needs_model:
        try:
            load_model_config()
        except ModelNotConfigured:
            model_available = False
    if scoring_mode == "external":
        # 外部评分不依赖本地裁判配置，生成与检索配置仍按原规则校验。
        return
    applicable = any(
        row["applicable"]
        for row in scenario["metric_applicability"]
        if row["metric"] in GENERATION_METRICS
    )
    if applicable and model_available:
        if judge is None:
            raise ScenarioModelUnavailable(
                scenario["scenario_id"], None, "生成指标适用但缺少裁判配置"
            )
        validate_timeout(judge_timeout)


async def execute_scenario(
    dataset: EvaluationDataset,
    scenario: dict,
    *,
    run_id: str,
    root: Path,
    enable_model: bool,
) -> ScenarioExecution:
    if not enable_model:
        raise ScenarioModelUnavailable(
            scenario["scenario_id"], None, "未显式启用真实模型调用"
        )
    scenario_sha256 = frozen_scenario_sha256(scenario)
    scenario = dict(scenario)
    scenario["_evaluation_run_id"] = run_id
    scenario["_session_ids"] = []
    scenario["_captures"] = []
    plan = scenario_plan(scenario)
    scenario_root = root / scenario["scenario_id"]
    scenario_root.mkdir(parents=True, exist_ok=False)
    runtime = RuntimeHandle(
        root=scenario_root,
        evaluation_run_id=run_id,
        scenario_id=scenario["scenario_id"],
    )
    initial_now = parse_timestamp(scenario["initial_state"]["now"])
    captures: list[EventCapture] = []
    assertion_entries: list[AssertionCheckpointEvidence] = []
    generation_entries: list[GenerationCheckpointEvidence] = []
    retrieval_records: list[SearchStageRecord] = []
    bindings: dict[str, ProposalBinding] = {}
    initial_projection: BusinessProjection | None = None
    flushed_searches = 0

    def collect_search_records() -> list[SearchStageRecord]:
        nonlocal flushed_searches
        for record in runtime.search_records[flushed_searches:]:
            _append_jsonl(
                scenario_root / "retrieval.jsonl", record.model_dump(mode="json")
            )
            retrieval_records.append(record)
        flushed_searches = len(runtime.search_records)
        return list(retrieval_records)

    async def finish(
        status: str, error: str | None
    ) -> ScenarioExecution:
        cancel_evidence: list[CancelEvidence] = []
        if runtime.service is not None:
            cancel_evidence = await read_cancel_evidence(
                runtime.service.queries, scenario["_session_ids"]
            )
        execution = _seal_execution(ScenarioExecution(
            run_id=run_id,
            scenario_id=scenario["scenario_id"],
            dataset=_dataset_identity(dataset),
            scenario_sha256=scenario_sha256,
            execution_sha256="0" * 64,
            status=status,
            error=error,
            session_ids=list(scenario["_session_ids"]),
            initial_state_sha256=sha256_text(
                canonical_json(scenario["initial_state"])
            ),
            initial_projection=initial_projection,
            expected_event_ids=list(plan.event_ids),
            expected_checkpoint_ids=list(plan.checkpoint_ids),
            events=list(captures),
            assertion_checkpoints=list(assertion_entries),
            generation_checkpoints=list(generation_entries),
            retrieval=list(retrieval_records),
            proposal_bindings=dict(bindings),
            generation_metrics=dict(plan.generation_metrics),
            cancel_evidence=cancel_evidence,
            context_gaps=[
                gap for capture in captures for gap in capture.context_gaps
            ],
        ))
        _write_event_evidence(scenario_root, captures)
        _write_json(
            scenario_root / EXECUTION_FILENAME,
            execution.model_dump(mode="json"),
        )
        return execution

    try:
        await runtime.open()
        session_id = str(uuid4())
        await runtime.service.create_session_result(
            session_id, scenario["scenario_id"], credentials=()
        )
        scenario["_session_ids"].append(session_id)
        await _seed_initial_state(
            runtime, scenario["initial_state"], initial_now
        )
        projection = await runtime.projection(scenario["_session_ids"])
        initial = _verify_initial_projection(
            projection, scenario["initial_state"]
        )
        initial_projection = projection
        _write_json(scenario_root / "initial-state.json", {
            "scenario_id": scenario["scenario_id"],
            "initial_state": scenario["initial_state"],
            "projection": projection.model_dump(mode="json"),
            "verified": initial,
        })
        prior_messages = scenario["input"]["prior_messages"]
        needs_model = any(
            event["type"] == "message" for event in scenario["events"]
        ) or any(row["role"] == "assistant" for row in prior_messages)
        if needs_model:
            try:
                model = load_model_config()
            except ModelNotConfigured as error:
                raise ScenarioModelUnavailable(
                    scenario["scenario_id"], None, str(error)
                ) from error
        else:
            model = None
        tools = create_file_tools(session_id, tmp_root=scenario_root / "tmp")
        await _seed_prior_messages(
            runtime,
            session_id,
            prior_messages,
            model,
            tools,
            initial_now,
        )
        operation_by_ref: dict[str, str] = {}
        repeated_confirms: set[str] = set()
        entry_to_event = _entry_to_event(captures)
        bindings = _bindings_for_projection(
            runtime,
            await runtime.projection(scenario["_session_ids"]),
            entry_to_event,
            operation_by_ref,
        )
        for index, event in enumerate(scenario["events"]):
            event_type = event["type"]
            event_now = initial_now + index
            if event_type not in EVENT_TYPES:
                raise ScenarioBlocked(
                    scenario["scenario_id"], event["event_id"], "未知事件类型"
                )
            runtime.event_id = event["event_id"]
            if event_type == "message":
                capture = await _run_message_event(
                    runtime,
                    dataset,
                    scenario,
                    event,
                    index,
                    session_id,
                    scenario["_session_ids"],
                    event_now,
                )
                entry_to_event[capture.request_entry_id] = event["event_id"]
            elif event_type == "confirm":
                projection = await runtime.projection(
                    scenario["_session_ids"]
                )
                bindings = _bindings_for_projection(
                    runtime, projection, entry_to_event, operation_by_ref
                )
                capture = await _run_confirm_event(
                    runtime,
                    scenario,
                    event,
                    session_id,
                    scenario["_session_ids"],
                    bindings,
                    operation_by_ref,
                    repeated_confirms,
                    event_now,
                )
            elif event_type == "cancel":
                projection = await runtime.projection(
                    scenario["_session_ids"]
                )
                bindings = _bindings_for_projection(
                    runtime, projection, entry_to_event, operation_by_ref
                )
                capture = await _run_cancel_event(
                    runtime,
                    scenario,
                    event,
                    session_id,
                    scenario["_session_ids"],
                    bindings,
                    captures,
                )
            elif event_type == "session_switch":
                capture, session_id = await _run_session_switch(
                    runtime, scenario, event, scenario["_session_ids"]
                )
            else:
                capture = await _run_restart_event(
                    runtime, scenario, event, session_id, scenario["_session_ids"]
                )
            captures.append(capture)
            scenario["_captures"] = captures
            _append_jsonl(
                scenario_root / "events.jsonl",
                capture.model_dump(mode="json"),
            )
            _append_jsonl(
                scenario_root / "context.jsonl",
                _context_rows([capture])[0],
            )
            collect_search_records()
            runtime.event_id = None
            projection = await runtime.projection(
                scenario["_session_ids"]
            )
            bindings = _bindings_for_projection(
                runtime, projection, entry_to_event, operation_by_ref
            )
            event_assertions = await _assertion_checkpoint_evidence(
                runtime,
                dataset,
                scenario,
                event,
                capture,
                bindings,
            )
            for entry in event_assertions:
                assertion_entries.append(entry)
                _append_jsonl(
                    scenario_root / "assertion-evidence.jsonl",
                    entry.model_dump(mode="json"),
                )
            event_generation = await _generation_checkpoint_evidence(
                runtime, scenario, event, capture
            )
            for entry in event_generation:
                generation_entries.append(entry)
                _append_jsonl(
                    scenario_root / "generation-input.jsonl",
                    entry.model_dump(mode="json"),
                )
            _write_event_evidence(scenario_root, captures)
        return await finish("completed", None)
    except ScenarioError as error:
        execution = await finish(
            "blocked", sanitize_error_text(str(error))
        )
        error.execution = execution
        error.observed_events = len(captures)
        error.observed_checkpoints = _execution_checkpoint_count(
            assertion_entries, generation_entries
        )
        error.context_gaps = [
            f"{scenario['scenario_id']}:{gap.reason}"
            for capture in captures
            for gap in capture.context_gaps
        ]
        raise
    except Exception as error:
        execution = await finish(
            "failed",
            sanitize_error_text(f"{type(error).__name__}: {error}"),
        )
        error.execution = execution
        raise
    finally:
        await runtime.close()


def _write_jsonl(path: Path, rows: Sequence[ArtifactModel]) -> None:
    _write_json_rows(path, rows)


def _write_scoring_jsonl(
    scenario_root: Path, artifact: ScenarioArtifact
) -> None:
    if artifact.assertions:
        _write_jsonl(scenario_root / "assertions.jsonl", artifact.assertions)
    if artifact.generation:
        _write_jsonl(scenario_root / "generation.jsonl", artifact.generation)


def _write_external_evidence(
    scenario_root: Path, record: ExternalScenarioScoring
) -> None:
    _write_json(
        scenario_root / EXTERNAL_SCORING_FILENAME,
        record.model_dump(mode="json"),
    )
    if record.assertions:
        _write_jsonl(scenario_root / "assertions.jsonl", record.assertions)


def _external_error_document(
    error: Exception,
    outcome: ExternalScenarioOutcome,
) -> dict[str, Any]:
    execution = outcome.execution
    return {
        "scenario_id": execution.scenario_id,
        "error_type": type(error).__name__,
        "reason": sanitize_error_text(str(error)),
        "details": getattr(error, "details", None),
        "scoring_mode": "external",
        "protocol_version": EXTERNAL_PROTOCOL_VERSION,
        "execution_status": outcome.record.execution_status,
        "assertion_status": outcome.record.assertion_status,
        "generation_scoring_status": outcome.record.generation_scoring_status,
        "observed_events": len(execution.events),
        "observed_checkpoints": _external_observed_checkpoints(execution),
        "incomplete": outcome.record.incomplete,
    }


async def run_external_scenario(
    dataset: EvaluationDataset,
    scenario: dict,
    *,
    run_id: str,
    root: Path,
    enable_model: bool,
) -> ExternalScenarioOutcome:
    scenario_root = root / scenario["scenario_id"]
    try:
        execution = await execute_scenario(
            dataset,
            scenario,
            run_id=run_id,
            root=root,
            enable_model=enable_model,
        )
    except ScenarioError as error:
        execution = getattr(error, "execution", None)
        if execution is None:
            raise
        outcome = ExternalScenarioOutcome(
            record=score_execution_external(execution, scenario["assertions"]),
            execution=execution,
        )
        error.external_outcome = outcome
        error.observed_events = len(execution.events)
        error.observed_checkpoints = _execution_checkpoint_count(
            execution.assertion_checkpoints,
            execution.generation_checkpoints,
        )
        error.context_gaps = [
            f"{execution.scenario_id}:{gap.reason}"
            for gap in execution.context_gaps
        ]
        _write_external_evidence(scenario_root, outcome.record)
        _write_json(
            scenario_root / "blocked.json",
            _external_error_document(error, outcome),
        )
        raise
    except Exception as error:
        execution = getattr(error, "execution", None)
        if execution is None:
            raise
        outcome = ExternalScenarioOutcome(
            record=score_execution_external(execution, scenario["assertions"]),
            execution=execution,
        )
        error.external_outcome = outcome
        _write_external_evidence(scenario_root, outcome.record)
        _write_json(
            scenario_root / "failure.json",
            _external_error_document(error, outcome),
        )
        raise
    outcome = ExternalScenarioOutcome(
        record=score_execution_external(execution, scenario["assertions"]),
        execution=execution,
    )
    _write_external_evidence(scenario_root, outcome.record)
    return outcome


async def run_scenario(
    dataset: EvaluationDataset,
    scenario: dict,
    *,
    run_id: str,
    root: Path,
    enable_model: bool,
    judge: JudgeConfig | None,
    judge_timeout: float | None,
    scoring_mode: ScoringMode = "ragas",
) -> ScenarioArtifact | ExternalScenarioScoring:
    scoring_protocol(scoring_mode)
    _require_execution_config(
        scenario,
        enable_model=enable_model,
        judge=judge,
        judge_timeout=judge_timeout,
        scoring_mode=scoring_mode,
    )
    if scoring_mode == "external":
        outcome = await run_external_scenario(
            dataset,
            scenario,
            run_id=run_id,
            root=root,
            enable_model=enable_model,
        )
        return outcome.record
    scenario_root = root / scenario["scenario_id"]
    try:
        execution = await execute_scenario(
            dataset,
            scenario,
            run_id=run_id,
            root=root,
            enable_model=enable_model,
        )
    except ScenarioError as error:
        execution = getattr(error, "execution", None)
        if execution is None:
            raise
        try:
            partial = await score_execution(
                execution, judge=judge, judge_timeout=judge_timeout
            )
        except Exception as scoring_error:
            partial = _blocked_artifact(
                execution,
                f"{type(scoring_error).__name__}: {scoring_error}",
            )
        if isinstance(error, ScenarioBlocked):
            error.observed_events = len(partial.events)
            error.observed_checkpoints = _observed_checkpoints(
                partial.assertions, partial.generation
            )
            error.context_gaps = [
                f"{execution.scenario_id}:{gap.reason}"
                for gap in partial.context_gaps
            ]
            error.artifact = partial
        _write_json(scenario_root / "blocked.json", {
            "scenario_id": execution.scenario_id,
            "error_type": type(error).__name__,
            "reason": sanitize_error_text(str(error)),
            "details": getattr(error, "details", None),
            "observed_events": len(partial.events),
            "observed_checkpoints": _observed_checkpoints(
                partial.assertions, partial.generation
            ),
        })
        _write_json(scenario_root / "partial.json", partial.model_dump(mode="json"))
        _write_scoring_jsonl(scenario_root, partial)
        raise
    except Exception as error:
        execution = getattr(error, "execution", None)
        if execution is None:
            raise
        try:
            partial = await score_execution(
                execution, judge=judge, judge_timeout=judge_timeout
            )
        except Exception as scoring_error:
            partial = _failure_artifact(
                execution,
                f"{type(scoring_error).__name__}: {scoring_error}",
            )
        _write_json(scenario_root / "failure.json", {
            "scenario_id": execution.scenario_id,
            "error_type": type(error).__name__,
            "reason": sanitize_error_text(str(error)),
            "observed_events": len(partial.events),
            "observed_checkpoints": _observed_checkpoints(
                partial.assertions, partial.generation
            ),
        })
        _write_json(scenario_root / "partial.json", partial.model_dump(mode="json"))
        _write_scoring_jsonl(scenario_root, partial)
        raise
    try:
        artifact = await score_execution(
            execution, judge=judge, judge_timeout=judge_timeout
        )
    except ScenarioBlocked as error:
        partial = _blocked_artifact(execution, str(error))
        error.artifact = partial
        error.observed_events = len(partial.events)
        error.observed_checkpoints = _execution_checkpoint_count(
            execution.assertion_checkpoints,
            execution.generation_checkpoints,
        )
        error.context_gaps = [
            f"{execution.scenario_id}:{gap.reason}"
            for gap in partial.context_gaps
        ]
        _write_json(scenario_root / "blocked.json", {
            "scenario_id": execution.scenario_id,
            "error_type": type(error).__name__,
            "reason": sanitize_error_text(str(error)),
            "details": getattr(error, "details", None),
            "observed_events": len(partial.events),
            "observed_checkpoints": error.observed_checkpoints,
        })
        _write_json(scenario_root / "partial.json", partial.model_dump(mode="json"))
        _write_scoring_jsonl(scenario_root, partial)
        raise
    except Exception as error:
        partial = _failure_artifact(
            execution, f"{type(error).__name__}: {error}"
        )
        _write_json(scenario_root / "failure.json", {
            "scenario_id": execution.scenario_id,
            "error_type": type(error).__name__,
            "reason": sanitize_error_text(str(error)),
            "observed_events": len(execution.events),
            "observed_checkpoints": _execution_checkpoint_count(
                execution.assertion_checkpoints,
                execution.generation_checkpoints,
            ),
        })
        _write_json(scenario_root / "partial.json", partial.model_dump(mode="json"))
        _write_scoring_jsonl(scenario_root, partial)
        raise
    _write_json(
        scenario_root / "scenario.json", artifact.model_dump(mode="json")
    )
    _write_scoring_jsonl(scenario_root, artifact)
    return artifact


def _incomplete_items(artifact: ScenarioArtifact) -> list[str]:
    items: list[str] = []
    for checkpoint in artifact.assertions:
        if checkpoint.binding_error is not None:
            items.append(
                f"{artifact.scenario_id}/{checkpoint.checkpoint_id}:binding_error"
            )
        if checkpoint.runner_error is not None:
            items.append(
                f"{artifact.scenario_id}/{checkpoint.checkpoint_id}:runner_error"
            )
    for checkpoint in artifact.generation:
        if checkpoint.runner_error is not None:
            items.append(
                f"{artifact.scenario_id}/{checkpoint.checkpoint_id}:scoring_error"
            )
        if checkpoint.result is not None:
            for outcome in checkpoint.result.outcomes:
                if outcome.status in {"failed", "applicable"}:
                    items.append(
                        f"{artifact.scenario_id}/{checkpoint.checkpoint_id}/"
                        f"{outcome.metric}:{outcome.status}"
                    )
    actual_metrics = {
        (checkpoint.checkpoint_id, outcome.metric)
        for checkpoint in artifact.generation
        if checkpoint.result is not None
        for outcome in checkpoint.result.outcomes
    }
    for checkpoint_id, metrics in artifact.generation_metrics.items():
        for metric in metrics:
            if (checkpoint_id, metric) not in actual_metrics:
                items.append(
                    f"{artifact.scenario_id}/{checkpoint_id}/{metric}:missing"
                )
    return items


@dataclass
class RunAggregate:
    events: int = 0
    checkpoints: int = 0
    metric_keys: set[tuple[str, str, str]] = field(default_factory=set)
    statuses: set[tuple[str, str, str, str]] = field(default_factory=set)
    search_records: list[SearchStageRecord] = field(default_factory=list)
    judge_evidence: list[GenerationMetricEvidence] = field(default_factory=list)
    judge_attempt_models: set[str] = field(default_factory=set)
    incomplete: list[str] = field(default_factory=list)
    context_gaps: list[str] = field(default_factory=list)


def accumulate_artifact(
    aggregate: RunAggregate, artifact: ScenarioArtifact
) -> None:
    # 完成场景与阻塞场景的部分产物使用同一汇总口径：已产出的结果保留真实状态与身份。
    aggregate.incomplete.extend(_incomplete_items(artifact))
    aggregate.judge_evidence.extend(
        item for checkpoint in artifact.generation for item in checkpoint.evidence
    )
    aggregate.judge_attempt_models.update(
        attempt.judge_model
        for checkpoint in artifact.generation
        for attempt in checkpoint.attempts
    )
    aggregate.events += len(artifact.events)
    aggregate.checkpoints += _observed_checkpoints(
        artifact.assertions, artifact.generation
    )
    aggregate.metric_keys.update(
        (artifact.scenario_id, item.checkpoint_id, outcome.metric)
        for item in artifact.generation
        if item.result is not None
        for outcome in item.result.outcomes
    )
    aggregate.statuses.update(
        (artifact.scenario_id, item.checkpoint_id, outcome.metric, outcome.status)
        for item in artifact.generation
        if item.result is not None
        for outcome in item.result.outcomes
    )
    aggregate.search_records.extend(artifact.retrieval)
    aggregate.context_gaps.extend(
        f"{artifact.scenario_id}:{gap.reason}" for gap in artifact.context_gaps
    )


def accumulate_partial(
    scenario_root: Path, aggregate: RunAggregate
) -> None:
    # 失败场景按已落盘的 partial.json 交接证据：真实产出进入汇总，失败本身仍留在 failed 列表。
    path = scenario_root / "partial.json"
    if not path.exists():
        return
    accumulate_artifact(
        aggregate, ScenarioArtifact.model_validate_json(path.read_text(encoding="utf-8"))
    )


def build_coverage(
    plan: ExecutionPlan, aggregate: RunAggregate, completed: Sequence[str]
) -> CoverageArtifact:
    expected_metric_keys = {
        (item.scenario_id, checkpoint_id, metric)
        for item in plan.scenarios
        for checkpoint_id, metrics in item.generation_metrics.items()
        for metric in metrics
    }
    status_by_metric: dict[tuple[str, str, str], str] = {}
    for scenario_id, checkpoint_id, metric, status in aggregate.statuses:
        key = (scenario_id, checkpoint_id, metric)
        if key in expected_metric_keys:
            status_by_metric[key] = status
    observed = expected_metric_keys & aggregate.metric_keys
    return CoverageArtifact(
        expected_scenarios=plan.expected_scenarios,
        observed_scenarios=len(completed),
        expected_events=plan.expected_events,
        observed_events=aggregate.events,
        expected_checkpoints=plan.expected_checkpoints,
        observed_checkpoints=aggregate.checkpoints,
        expected_metrics=plan.expected_metrics,
        observed_metrics=len(observed),
        succeeded_metrics=sum(
            1 for status in status_by_metric.values() if status == "succeeded"
        ),
        failed_metrics=sum(
            1 for status in status_by_metric.values() if status == "failed"
        ),
        not_applicable_metrics=sum(
            1 for status in status_by_metric.values() if status == "not_applicable"
        ),
        unexecuted_metrics=len(expected_metric_keys - aggregate.metric_keys),
        context_gaps=len(aggregate.context_gaps),
        missing=[
            item.scenario_id
            for item in plan.scenarios
            if item.scenario_id not in completed
        ],
        missing_metrics=sorted(
            f"{scenario_id}/{checkpoint_id}/{metric}"
            for scenario_id, checkpoint_id, metric in expected_metric_keys - observed
        ),
    )


def judge_attempt_identity(
    attempt_models: Iterable[str],
    generation_model: str | None,
    judge_model: str | None,
) -> tuple[dict[str, Any], list[str]]:
    # 裁判尝试模型必须唯一：多模型冲突同时登记身份字段与缺失项，供 missing 与 incomplete 派生。
    models = sorted(attempt_models)
    fields: dict[str, Any] = {}
    missing: list[str] = []
    if len(models) == 1:
        fields["judge_attempt_model"] = models[0]
    elif len(models) > 1:
        fields["judge_attempt_model_conflict"] = models
        missing.append("judge_model_conflict")
    effective = judge_model
    if effective is None and len(models) == 1:
        effective = models[0]
    if len(models) > 1 or effective is None or generation_model is None:
        fields["judge_same_as_generation"] = None
    else:
        fields["judge_same_as_generation"] = generation_model == effective
    return fields, missing


async def _run_external_selected(
    dataset: EvaluationDataset,
    scenarios: Sequence[dict],
    *,
    run_id: str,
    run_root: Path,
    enable_model: bool,
    plan: ExecutionPlan,
    started_at: str,
) -> ExternalRunSummary:
    blocked: list[str] = []
    failed: list[str] = []
    completed: list[str] = []
    incomplete: list[str] = []
    context_gaps: list[str] = []
    search_records: list[SearchStageRecord] = []
    missing_assertions: list[str] = []
    counts = {
        "passed": 0,
        "failed": 0,
        "binding_errors": 0,
        "runner_errors": 0,
        "generation_ready": 0,
        "generation_missing": 0,
    }
    events = 0
    checkpoints = 0

    def accumulate(outcome: ExternalScenarioOutcome) -> None:
        nonlocal events, checkpoints
        record = outcome.record
        execution = outcome.execution
        events += len(execution.events)
        checkpoints += _external_observed_checkpoints(execution)
        statistics = assertion_statistics(record.assertions)
        for key, value in statistics.items():
            counts[key] += value
        counts["generation_ready"] += sum(
            1 for item in record.generation if item.status == "ready"
        )
        counts["generation_missing"] += sum(
            1 for item in record.generation if item.status == "missing"
        )
        incomplete.extend(
            f"{execution.scenario_id}:{item}" for item in record.incomplete
        )
        context_gaps.extend(
            f"{execution.scenario_id}:{gap}" for gap in record.context_gaps
        )
        missing_assertions.extend(
            f"{execution.scenario_id}/{checkpoint_id}"
            for checkpoint_id in record.missing_assertion_checkpoint_ids
        )
        search_records.extend(execution.retrieval)

    for scenario in scenarios:
        try:
            outcome = await run_external_scenario(
                dataset,
                scenario,
                run_id=run_id,
                root=run_root,
                enable_model=enable_model,
            )
            completed.append(scenario["scenario_id"])
            accumulate(outcome)
        except ScenarioBlocked as error:
            blocked.append(sanitize_error_text(str(error)))
            outcome = getattr(error, "external_outcome", None)
            if outcome is not None:
                accumulate(outcome)
        except Exception as error:
            failed.append(sanitize_error_text(
                f"{scenario['scenario_id']}: {type(error).__name__}: {error}"
            ))
            outcome = getattr(error, "external_outcome", None)
            if outcome is not None:
                accumulate(outcome)
    coverage = ExternalCoverageArtifact(
        expected_scenarios=plan.expected_scenarios,
        observed_scenarios=len(completed),
        expected_events=plan.expected_events,
        observed_events=events,
        expected_checkpoints=plan.expected_checkpoints,
        observed_checkpoints=checkpoints,
        assertions_passed=counts["passed"],
        assertions_failed=counts["failed"],
        assertions_binding_errors=counts["binding_errors"],
        assertions_runner_errors=counts["runner_errors"],
        generation_ready=counts["generation_ready"],
        generation_missing=counts["generation_missing"],
        context_gaps=len(context_gaps),
        missing=[
            item.scenario_id
            for item in plan.scenarios
            if item.scenario_id not in completed
        ],
        missing_assertion_checkpoints=sorted(missing_assertions),
    )
    identity: dict[str, Any] = {
        "run_id": run_id,
        "purpose": "validation",
        "scoring_mode": "external",
        "protocol_version": EXTERNAL_PROTOCOL_VERSION,
        "dataset": {
            "version": dataset.version,
            "manifest_sha256": dataset.manifest_sha256,
            "content_sha256": dataset.content_sha256,
            "corpus_sha256": dataset.corpus_sha256,
        },
        "code": code_identity(),
        # 裁判配置由 LangSmith 外部管理；实际 Evaluator 身份由后续上传与回收证据记录。
        "judge": {
            "ownership": "langsmith",
            "configuration": "external",
        },
    }
    missing: list[str] = []
    if enable_model:
        try:
            model = load_model_config()
            identity["generation"] = generation_identity(model)
        except Exception as error:
            identity["generation_error"] = sanitize_error_text(
                f"{type(error).__name__}: {error}"
            )
            missing.append("generation")
        retrieval = retrieval_identity()
        executed_index_identity(search_records, retrieval)
        identity["retrieval"] = retrieval
        missing.extend(retrieval.get("missing", []))
    else:
        missing.extend(["generation", "retrieval"])
    identity["missing"] = sorted(set(missing))
    incomplete.extend(f"identity:{name}" for name in identity["missing"])
    identity["incomplete"] = sorted(set(incomplete))
    identity["status"] = "complete" if not incomplete else "incomplete"
    complete = (
        not blocked
        and not failed
        and not incomplete
        and coverage.observed_scenarios == coverage.expected_scenarios
        and coverage.observed_events == coverage.expected_events
        and coverage.observed_checkpoints == coverage.expected_checkpoints
    )
    summary = ExternalRunSummary(
        run_id=run_id,
        purpose="validation",
        dataset=dataset.identity(),
        identity=identity,
        coverage=coverage,
        scenarios=completed,
        blocked=blocked,
        failed=failed,
        incomplete=sorted(set(incomplete)),
        context_gaps=sorted(set(context_gaps)),
        status="awaiting_external_scoring" if complete else "incomplete",
        started_at=started_at,
        finished_at=datetime.now(timezone.utc).isoformat(),
    )
    _write_json(run_root / "run.json", summary.model_dump(mode="json"))
    return summary


async def run_selected(
    dataset: EvaluationDataset,
    scenarios: Sequence[dict],
    *,
    run_id: str,
    root: Path,
    enable_model: bool,
    judge: JudgeConfig | None,
    judge_timeout: float | None,
    scoring_mode: ScoringMode = "ragas",
) -> RunSummary | ExternalRunSummary:
    scoring_protocol(scoring_mode)
    run_root = root / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    started_at = datetime.now(timezone.utc).isoformat()
    plan = build_execution_plan(dataset, scenarios, run_id, scoring_mode)
    _write_json(run_root / "plan.json", plan.model_dump(mode="json"))
    if scoring_mode == "external":
        return await _run_external_selected(
            dataset,
            scenarios,
            run_id=run_id,
            run_root=run_root,
            enable_model=enable_model,
            plan=plan,
            started_at=started_at,
        )
    blocked: list[str] = []
    failed: list[str] = []
    completed: list[str] = []
    aggregate = RunAggregate()

    for scenario in scenarios:
        try:
            artifact = await run_scenario(
                dataset,
                scenario,
                run_id=run_id,
                root=run_root,
                enable_model=enable_model,
                judge=judge,
                judge_timeout=judge_timeout,
            )
            completed.append(scenario["scenario_id"])
            accumulate_artifact(aggregate, artifact)
        except ScenarioBlocked as error:
            blocked.append(sanitize_error_text(str(error)))
            if error.artifact is not None:
                accumulate_artifact(aggregate, error.artifact)
        except Exception as error:
            failed.append(sanitize_error_text(
                f"{scenario['scenario_id']}: {type(error).__name__}: {error}"
            ))
            accumulate_partial(run_root / scenario["scenario_id"], aggregate)
    coverage = build_coverage(plan, aggregate, completed)
    incomplete = aggregate.incomplete
    context_gaps = aggregate.context_gaps
    judge_evidence = aggregate.judge_evidence
    judge_attempt_models = aggregate.judge_attempt_models
    identity: dict[str, Any] = {
        "run_id": run_id,
        "purpose": "validation",
        "dataset": {
            "version": dataset.version,
            "manifest_sha256": dataset.manifest_sha256,
            "content_sha256": dataset.content_sha256,
            "corpus_sha256": dataset.corpus_sha256,
        },
        "code": code_identity(),
    }
    missing: list[str] = []
    if enable_model:
        try:
            model = load_model_config()
            identity["generation"] = generation_identity(model)
        except Exception as error:
            identity["generation_error"] = sanitize_error_text(
                f"{type(error).__name__}: {error}"
            )
            missing.append("generation")
        retrieval = retrieval_identity()
        executed_index_identity(aggregate.search_records, retrieval)
        identity["retrieval"] = retrieval
        missing.extend(retrieval.get("missing", []))
    else:
        missing.extend(["generation", "retrieval"])
    if judge_evidence:
        try:
            identity["judge"] = run_judge_identity(judge_evidence).model_dump(
                mode="json"
            )
        except Exception as error:
            identity["judge_error"] = sanitize_error_text(
                f"{type(error).__name__}: {error}"
            )
            missing.append("judge")
    else:
        missing.append("judge")
    generation_model = None
    if "generation" in identity:
        generation_model = json.loads(identity["generation"]["model"]).get("model")
    attempt_fields, attempt_missing = judge_attempt_identity(
        judge_attempt_models, generation_model, identity.get("judge", {}).get("model")
    )
    identity.update(attempt_fields)
    missing.extend(attempt_missing)
    # 全部身份诊断完成后一次性生成缺失清单：后续登记的冲突同样进入 missing 与 incomplete。
    identity["missing"] = sorted(set(missing))
    incomplete.extend(f"identity:{name}" for name in identity["missing"])
    identity["incomplete"] = sorted(set(incomplete))
    identity["status"] = "complete" if not incomplete else "incomplete"
    summary = RunSummary(
        run_id=run_id,
        purpose="validation",
        dataset=dataset.identity(),
        identity=identity,
        coverage=coverage,
        scenarios=completed,
        blocked=blocked,
        failed=failed,
        incomplete=sorted(set(incomplete)),
        context_gaps=sorted(set(context_gaps)),
        started_at=started_at,
        finished_at=datetime.now(timezone.utc).isoformat(),
        scoring_mode="ragas",
        protocol_version=RAGAS_PROTOCOL_VERSION,
    )
    _write_json(run_root / "run.json", summary.model_dump(mode="json"))
    return summary


def _dataset(version_dir: Path) -> EvaluationDataset:
    return load_for_evaluation(version_dir)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="冻结评测场景运行器：默认只校验并展示执行计划，不读取密钥、不外呼模型。"
    )
    parser.add_argument("--version-dir", type=Path, default=DEFAULT_VERSION_DIR)
    parser.add_argument("--root", type=Path, default=SCENARIO_ROOT)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--scenario-id", action="append", default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--enable-model", action="store_true")
    parser.add_argument("--judge-timeout", type=float, default=DEFAULT_JUDGE_TIMEOUT)
    parser.add_argument(
        "--scoring-mode",
        choices=("ragas", "external"),
        default="ragas",
        help="ragas 为本地 Ragas 评分；external 只执行确定性断言并等待外部评分",
    )
    parser.add_argument("--list", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv(BACKEND / ".env", override=False)
    args = _parse_args(argv)
    dataset = _dataset(args.version_dir)
    if args.list:
        for scenario in dataset.scenarios:
            plan = scenario_plan(scenario)
            print(canonical_json(plan.model_dump(mode="json")))
        return 0
    if args.scenario_id and args.all:
        raise SystemExit("--scenario-id 与 --all 不能同时使用")
    scenario_ids = None if args.all or not args.scenario_id else args.scenario_id
    scenarios = select_scenarios(dataset, scenario_ids)
    run_id = args.run_id or (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    )
    plan = build_execution_plan(dataset, scenarios, run_id, args.scoring_mode)
    print(canonical_json(plan.model_dump(mode="json")))
    if not args.enable_model:
        print("未启用真实模型：仅加载、校验并展示执行计划。", file=sys.stderr)
        return 0
    if args.scoring_mode == "external":
        summary = asyncio.run(run_selected(
            dataset,
            scenarios,
            run_id=run_id,
            root=args.root,
            enable_model=True,
            judge=None,
            judge_timeout=None,
            scoring_mode="external",
        ))
        print(canonical_json(summary.model_dump(mode="json")))
        return (
            0 if summary.status == "awaiting_external_scoring" else 1
        )
    timeout = validate_timeout(args.judge_timeout)
    judge, _ = _judge_config()
    summary = asyncio.run(run_selected(
        dataset,
        scenarios,
        run_id=run_id,
        root=args.root,
        enable_model=True,
        judge=judge,
        judge_timeout=timeout,
        scoring_mode="ragas",
    ))
    print(canonical_json(summary.model_dump(mode="json")))
    if (
        summary.blocked
        or summary.failed
        or summary.incomplete
        or summary.coverage.observed_scenarios
        != summary.coverage.expected_scenarios
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
