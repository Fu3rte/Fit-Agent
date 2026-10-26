import argparse
import json
import math
import os
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Literal, Sequence
from uuid import NAMESPACE_URL, uuid5

from dotenv import load_dotenv
from langfuse import Langfuse
from pydantic import Field, TypeAdapter, ValidationError, model_validator

from evaluation.contracts import (
    EXTERNAL_JUDGE_FEEDBACK_LANGFUSE_SCHEMA_VERSION,
    EXTERNAL_JUDGE_FEEDBACK_SCHEMA_VERSION,
    EXTERNAL_JUDGE_METRICS,
    DatasetIdentity,
    EvaluationContract,
    EvidenceLink,
    ExternalJudgeCloudScoreIdentity,
    ExternalJudgeDefinitionIdentity,
    ExternalJudgeExperimentIdentity,
    ExternalJudgeFeedback,
    ExternalJudgeFeedbackSet,
    ExternalJudgeLangfuseScoringAttempt,
    ExternalJudgeLangfuseSource,
    ExternalJudgeMetric,
    ExternalJudgeModelIdentity,
    ExternalJudgeOutboundProjection,
    ExternalJudgeScoreSource,
    NonEmptyText,
    ProbabilityScore,
    Sha256,
    UnreportedModelVersion,
)
from evaluation.dataset import EvaluationDataset, load_for_evaluation
from evaluation.generation_upload import (
    BACKEND,
    EXPECTED_VARIABLE_MAPPING,
    EXTERNAL_DATASET_RECEIPT_NAME,
    EXTERNAL_DATASET_RECEIPT_SCHEMA,
    EXTERNAL_EXPERIMENT_PENDING_NAME,
    EXTERNAL_EXPERIMENT_RECEIPT_NAME,
    EXTERNAL_EXPERIMENT_RECEIPT_SCHEMA,
    EXTERNAL_OBSERVATION_SCOPE,
    EXTERNAL_PAYLOAD_NAME,
    EXTERNAL_PAYLOAD_SCHEMA,
    EXTERNAL_RULE_SAMPLING,
    FORBIDDEN_KEYS,
    ExternalApplicabilityEntry,
    ExternalRunEvidence,
    UploadBlocked,
    _collect_external_runs,
    _coverage_ledger,
    _experiment_metadata,
    _external_applicability_sha256,
    _external_receipt_identity,
    _local_path_residue,
    _read_json,
    _require,
    _root_runs,
    _sanitize_outbound,
    _verify_experiment_project,
    _verify_remote_dataset,
    _walk_keys,
    _walk_strings,
    _web_judge_configuration,
    _write_json,
    build_external_payload,
    external_dir,
    external_rule_filters,
    load_external_applicability,
    load_external_run,
    payload_digest,
)
from evaluation.scenario_evidence import canonical_json, sha256_text
from evaluation.scenarios import DEFAULT_VERSION_DIR

FEEDBACK_BINDING_SCHEMA = "fit-agent-external-judge-feedback-binding-langfuse-2"
FEEDBACK_COLLECTION_SCHEMA = (
    "fit-agent-external-judge-feedback-collection-langfuse-2"
)
FEEDBACK_INDEX_SCHEMA = "fit-agent-external-judge-feedback-index-langfuse-2"
FEEDBACK_DIR = "decision-feedback"
FEEDBACK_BINDING_NAME = "feedback-binding.json"
FEEDBACK_INDEX_NAME = "index.json"
FEEDBACK_BATCH_DIR = "batches"
FEEDBACK_BATCH_SIZE = 100
FEEDBACK_READ_TIMEOUT_SECONDS = 30.0
FEEDBACK_POLL_INTERVAL_SECONDS = 1.5
SOURCE_ASSURANCE = "declared_binding"
CONTEXT_STATE_PATH = "output.context.items"
WEB_JUDGE_ENVIRONMENT = "langfuse-llm-as-a-judge"
CREDENTIAL_MARKERS = (
    "Bearer ",
    "Authorization:",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
)
SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookies",
    "headers",
    "password",
    "secret",
    "token",
}
BLOCKING_CODES = {
    "invalid_score",
    "source_gap",
    "feedback_conflict",
    "feedback_content_drift",
    "feedback_history_conflict",
    "malformed_cloud_feedback",
    "missing_cloud_trace",
    "experiment_absent",
}


def feedback_dir(run_dir: Path) -> Path:
    return external_dir(run_dir) / FEEDBACK_DIR


def feedback_binding_path(run_dir: Path) -> Path:
    return feedback_dir(run_dir) / FEEDBACK_BINDING_NAME


def feedback_index_path(run_dir: Path) -> Path:
    return feedback_dir(run_dir) / FEEDBACK_INDEX_NAME


def feedback_batch_path(run_dir: Path, batch_id: str) -> Path:
    return feedback_dir(run_dir) / FEEDBACK_BATCH_DIR / f"{batch_id}.json"


def question_digest(question_id: str, question: str) -> str:
    return sha256_text(
        canonical_json({"question_id": question_id, "question": question})
    )


def rubric_digest(rubric_id: str | None, rubric: str) -> str:
    return sha256_text(
        canonical_json({"rubric_id": rubric_id, "rubric": rubric})
    )


def _assert_no_secret(value: Any, label: str) -> None:
    secrets = [
        os.environ.get(name)
        for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
    ]
    for key in _walk_keys(value):
        if isinstance(key, str) and key.lower() in SECRET_KEYS:
            raise UploadBlocked(f"{label} 包含认证字段: {key}")
    for text in _walk_strings(value):
        residue = _local_path_residue(text)
        if residue is not None:
            raise UploadBlocked(f"{label} 包含本地路径: {residue}")
        for secret in secrets:
            if secret and secret in text:
                raise UploadBlocked(f"{label} 包含 Langfuse 凭据")
        for marker in CREDENTIAL_MARKERS:
            if marker in text:
                raise UploadBlocked(f"{label} 包含凭据标记: {marker}")


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _raw_feedback(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        raw = value.model_dump(mode="json")
    elif isinstance(value, dict):
        raw = deepcopy(value)
    else:
        raise UploadBlocked("云端反馈必须为对象")
    if not isinstance(raw, dict):
        raise UploadBlocked("云端反馈必须为对象")
    sanitized = _sanitize_outbound(_json_safe(raw))
    _assert_no_secret(sanitized, "云端反馈证据")
    return sanitized


def _score_identity(raw: dict[str, Any]) -> tuple[str, str, str]:
    # Langfuse Score 身份：score id + subject trace + score name。
    score_id = raw.get("id")
    name = raw.get("name")
    subject = raw.get("subject")
    _require(
        isinstance(score_id, str) and bool(score_id.strip()),
        "云端反馈缺少 score id",
    )
    _require(
        isinstance(name, str) and bool(name.strip()),
        "云端反馈缺少 score name",
    )
    _require(isinstance(subject, dict), "云端反馈缺少 subject")
    kind = subject.get("kind")
    if kind == "trace":
        trace_id = subject.get("id")
    elif kind == "observation":
        trace_id = subject.get("traceId")
    else:
        raise UploadBlocked(
            f"云端反馈的 subject 不属于 trace/observation: {kind}"
        )
    _require(
        isinstance(trace_id, str) and bool(trace_id.strip()),
        "云端反馈的 subject 缺少所属 trace",
    )
    return score_id, trace_id, name


def _semantic_feedback(raw: dict[str, Any]) -> dict[str, Any]:
    # timestamp 与创建/更新时间变化不代表评分内容变化；数值、来源与归属字段逐项比较。
    return {
        field: raw.get(field)
        for field in (
            "name",
            "dataType",
            "value",
            "comment",
            "source",
            "configId",
            "metadata",
            "authorUserId",
            "queueId",
            "environment",
            "subject",
        )
    }


def _semantic_sha256(raw: dict[str, Any]) -> str:
    return sha256_text(canonical_json(_semantic_feedback(raw)))


class FeedbackStatePath(EvaluationContract):
    source: Literal["input", "output", "expected_output"]
    path: NonEmptyText


class FeedbackStateMapping(EvaluationContract):
    user_request: FeedbackStatePath
    answer: FeedbackStatePath
    context: FeedbackStatePath
    reference: FeedbackStatePath

    @model_validator(mode="after")
    def validate_mapping(self) -> "FeedbackStateMapping":
        if self.model_dump(mode="json") != EXPECTED_VARIABLE_MAPPING:
            raise ValueError(
                "四变量映射必须为 input.user_request、output.answer、"
                "output.context.items、expected_output.reference"
            )
        return self


def _binding_evaluator_names(config: dict[str, Any]) -> dict[str, str]:
    return {
        entry["metric"]: entry["evaluator_name"]
        for entry in config["evaluators"]
    }


class FeedbackEvaluatorBinding(EvaluationContract):
    provider: NonEmptyText
    evaluator: NonEmptyText
    evaluator_id: NonEmptyText
    model_alias: NonEmptyText
    config: dict[str, Any]
    config_sha256: Sha256

    @model_validator(mode="after")
    def validate_config(self) -> "FeedbackEvaluatorBinding":
        expected = sha256_text(canonical_json(self.config))
        if self.config_sha256 != expected:
            raise ValueError(f"config_sha256 应为 {expected}")
        forbidden = sorted(set(_walk_keys(self.config)) & FORBIDDEN_KEYS)
        if forbidden:
            raise ValueError("Evaluator 配置快照包含禁止字段: " + ", ".join(forbidden))
        for text in _walk_strings(self.config):
            residue = _local_path_residue(text)
            if residue is not None:
                raise ValueError("Evaluator 配置快照包含本地路径")
            for marker in CREDENTIAL_MARKERS:
                if marker in text:
                    raise ValueError("Evaluator 配置快照包含凭据标记")
        scope = self.config.get("scope")
        if not isinstance(scope, dict):
            raise ValueError("Evaluator 配置缺少 rule 作用域")
        dataset_id = scope.get("dataset_id")
        dataset_name = scope.get("dataset_name")
        if (
            not isinstance(dataset_id, str)
            or not dataset_id.strip()
            or not isinstance(dataset_name, str)
            or not dataset_name.strip()
        ):
            raise ValueError("Evaluator 配置的 rule 作用域缺少数据集身份")
        if scope.get("observation_scope") != EXTERNAL_OBSERVATION_SCOPE:
            raise ValueError(
                f"Evaluator 配置的 rule 必须限定 {EXTERNAL_OBSERVATION_SCOPE}"
            )
        if scope.get("sampling") != EXTERNAL_RULE_SAMPLING:
            raise ValueError("Evaluator 配置的 rule 采样率必须为全量 1.0")
        if self.config.get("variable_mapping") != EXPECTED_VARIABLE_MAPPING:
            raise ValueError("Evaluator 配置的变量映射与冻结声明不一致")
        evaluators = self.config.get("evaluators")
        if not isinstance(evaluators, list) or len(evaluators) != 2:
            raise ValueError("Evaluator 配置必须为两个指标各声明一个 evaluator")
        names: list[str] = []
        metrics: list[str] = []
        for entry in evaluators:
            if not isinstance(entry, dict):
                raise ValueError("Evaluator 配置的 evaluator 声明必须为对象")
            metric = entry.get("metric")
            if metric not in EXTERNAL_JUDGE_METRICS:
                raise ValueError("Evaluator 配置包含未知外部裁判指标")
            metrics.append(metric)
            evaluator_name = entry.get("evaluator_name")
            if (
                not isinstance(evaluator_name, str)
                or not evaluator_name.strip()
            ):
                raise ValueError("Evaluator 配置缺少 evaluator 名称")
            if entry.get("score_name") != metric:
                raise ValueError(
                    f"Evaluator 配置的 score 名称必须等于指标名: {metric}"
                )
            if entry.get("score_type") != "NUMERIC":
                raise ValueError("Evaluator 配置的 score 类型必须为 NUMERIC")
            if entry.get("value_range") != [0.0, 1.0]:
                raise ValueError("Evaluator 配置的 score 范围必须为 0..1")
            if entry.get("sampling") != EXTERNAL_RULE_SAMPLING:
                raise ValueError("Evaluator 配置的采样率必须为全量 1.0")
            for field in ("system_prompt", "user_prompt"):
                prompt = entry.get(field)
                if not isinstance(prompt, str) or not prompt.strip():
                    raise ValueError(f"Evaluator 配置缺少 {field}")
            if entry.get("rule_filters") != external_rule_filters(
                metric, dataset_id
            ):
                raise ValueError(
                    "Evaluator 配置的规则过滤必须限定检查点数据集、"
                    "root observation 与字符串适用性标志"
                )
            names.append(evaluator_name)
        if len(set(names)) != len(names):
            raise ValueError("Evaluator 配置的 evaluator 名称不能重复")
        if sorted(metrics) != sorted(EXTERNAL_JUDGE_METRICS):
            raise ValueError("Evaluator 配置必须逐项且仅包含两个指标")
        return self


class FeedbackMetricBinding(EvaluationContract):
    metric: ExternalJudgeMetric
    feedback_key: NonEmptyText
    question_id: NonEmptyText
    question: NonEmptyText
    question_sha256: Sha256
    rubric_id: NonEmptyText | None = None
    rubric: NonEmptyText
    rubric_sha256: Sha256

    @model_validator(mode="after")
    def validate_definition(self) -> "FeedbackMetricBinding":
        expected_question = question_digest(self.question_id, self.question)
        if self.question_sha256 != expected_question:
            raise ValueError(f"question_sha256 应为 {expected_question}")
        expected_rubric = rubric_digest(self.rubric_id, self.rubric)
        if self.rubric_sha256 != expected_rubric:
            raise ValueError(f"rubric_sha256 应为 {expected_rubric}")
        return self


class FeedbackExperimentTarget(EvaluationContract):
    experiment_id: NonEmptyText
    experiment_name: NonEmptyText
    dataset_id: NonEmptyText
    dataset_name: NonEmptyText
    payload_sha256: Sha256
    rows_sha256: Sha256
    projections_sha256: Sha256


class FeedbackBinding(EvaluationContract):
    schema_version: Literal["fit-agent-external-judge-feedback-binding-langfuse-2"]
    run_id: NonEmptyText
    source_sha256: Sha256
    applicability_sha256: Sha256
    experiment: FeedbackExperimentTarget | None = None
    evaluator: FeedbackEvaluatorBinding
    state_mapping: FeedbackStateMapping
    metrics: list[FeedbackMetricBinding] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def validate_metrics(self) -> "FeedbackBinding":
        metrics = [row.metric for row in self.metrics]
        if sorted(metrics) != sorted(EXTERNAL_JUDGE_METRICS):
            raise ValueError("反馈绑定必须逐项且仅包含两个外部裁判指标")
        keys = [row.feedback_key for row in self.metrics]
        if len(set(keys)) != len(keys):
            raise ValueError("反馈绑定的 feedback key 不能重复")
        if (
            self.state_mapping.context.source != "output"
            or self.state_mapping.context.path != "context.items"
        ):
            raise ValueError("context 必须映射 output.context.items")
        return self


class FeedbackRawEvidence(EvaluationContract):
    evidence_id: NonEmptyText
    score_id: NonEmptyText
    trace_id: NonEmptyText
    score_name: NonEmptyText
    raw: dict[str, Any]
    raw_sha256: Sha256

    @model_validator(mode="after")
    def validate_evidence(self) -> "FeedbackRawEvidence":
        score_id, trace_id, name = _score_identity(self.raw)
        if (score_id, trace_id, name) != (
            self.score_id,
            self.trace_id,
            self.score_name,
        ):
            raise ValueError("原始证据身份与封装字段不一致")
        expected = sha256_text(canonical_json(self.raw))
        if self.raw_sha256 != expected:
            raise ValueError("原始证据摘要与内容不一致")
        # evidence_id 派生公式："<score_id>@<raw_sha256[:12]>"。
        if self.evidence_id != f"{self.score_id}@{self.raw_sha256[:12]}":
            raise ValueError("原始证据 ID 必须由 score ID 与摘要导出")
        return self


class FeedbackExclusion(EvaluationContract):
    evidence_id: NonEmptyText
    score_id: NonEmptyText
    metric: ExternalJudgeMetric | None = None
    reason: Literal[
        "unmapped_key",
        "source_not_decision_model",
        "invalid_score",
        "not_applicable_metric",
        "malformed_cloud_feedback",
    ]
    detail: NonEmptyText


class FeedbackDiagnostic(EvaluationContract):
    code: Literal[
        "awaiting_feedback",
        "cloud_failure",
        "checkpoint_missing_upload",
        "invalid_score",
        "source_gap",
        "feedback_conflict",
        "feedback_content_drift",
        "feedback_history_conflict",
        "malformed_cloud_feedback",
        "unexpected_not_applicable_feedback",
        "missing_cloud_trace",
        "experiment_absent",
    ]
    metric: ExternalJudgeMetric | None = None
    detail: NonEmptyText


class FeedbackCoverage(EvaluationContract):
    expected: int = Field(ge=0)
    uploadable: int = Field(ge=0)
    missing: int = Field(ge=0)
    not_applicable: int = Field(ge=0)
    pending_review: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_total(self) -> "FeedbackCoverage":
        total = (
            self.uploadable
            + self.missing
            + self.not_applicable
            + self.pending_review
        )
        if total != self.expected:
            raise ValueError("覆盖账本分类之和必须等于预期检查点数")
        return self


class CheckpointFeedbackRecord(EvaluationContract):
    checkpoint: EvidenceLink
    execution_sha256: Sha256
    row_id: NonEmptyText | None = None
    example_id: NonEmptyText | None = None
    cloud_trace_id: NonEmptyText | None = None
    projection_sha256: Sha256 | None = None
    feedback_set: ExternalJudgeFeedbackSet
    attempts: list[ExternalJudgeLangfuseScoringAttempt] = Field(
        default_factory=list
    )
    evidence: list[FeedbackRawEvidence] = Field(default_factory=list)
    exclusions: list[FeedbackExclusion] = Field(default_factory=list)
    diagnostics: list[FeedbackDiagnostic] = Field(default_factory=list)
    evidence_sha256: Sha256
    status: Literal["complete", "pending", "blocked", "not_applicable"]

    @model_validator(mode="after")
    def validate_record(self) -> "CheckpointFeedbackRecord":
        if self.feedback_set.checkpoint != self.checkpoint:
            raise ValueError("反馈集合的检查点身份不一致")
        if self.feedback_set.execution_sha256 != self.execution_sha256:
            raise ValueError("反馈集合的执行摘要不一致")
        attempt_ids = [attempt.attempt_id for attempt in self.attempts]
        if len(set(attempt_ids)) != len(attempt_ids):
            raise ValueError("评分尝试 ID 不能重复")
        evidence_ids = [item.evidence_id for item in self.evidence]
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("原始证据 ID 不能重复")
        exclusion_ids = [
            (item.evidence_id, item.reason) for item in self.exclusions
        ]
        if len(set(exclusion_ids)) != len(exclusion_ids):
            raise ValueError("排除记录不能重复")
        for attempt in self.attempts:
            if (
                attempt.checkpoint != self.checkpoint
                or attempt.execution_sha256 != self.execution_sha256
            ):
                raise ValueError("评分尝试身份与检查点不一致")
            if self.row_id is not None:
                if (
                    attempt.outbound.row_id != self.row_id
                    or attempt.source.example_id != self.example_id
                ):
                    raise ValueError("评分尝试的出站投影身份不一致")
                if self.projection_sha256 is not None and (
                    attempt.outbound.projection_sha256
                    != self.projection_sha256
                ):
                    raise ValueError("评分尝试的投影摘要不一致")
            if self.cloud_trace_id is not None and (
                attempt.source.trace_id != self.cloud_trace_id
                or attempt.cloud_score is None
                or attempt.cloud_score.trace_id != self.cloud_trace_id
            ):
                raise ValueError("评分尝试的云端运行身份不一致")
        feedback_by_metric = {
            row.metric: row for row in self.feedback_set.feedback
        }
        for metric, row in feedback_by_metric.items():
            matching = [
                attempt
                for attempt in self.attempts
                if attempt.feedback.metric == metric
            ]
            if row.status == "succeeded":
                if len(matching) != 1:
                    raise ValueError("终态反馈必须绑定唯一评分尝试")
                attempt = matching[0]
                if (
                    attempt.feedback.status != row.status
                    or attempt.feedback.score != row.score
                    or attempt.feedback.failure_evidence
                    != row.failure_evidence
                ):
                    raise ValueError("终态反馈与评分尝试内容不一致")
            if row.status == "failed" and matching:
                # 网页裁判失败不产生 score，失败依据保存在 feedback.failure_evidence。
                raise ValueError("失败反馈不得绑定评分尝试")
            if row.status == "not_applicable" and matching:
                raise ValueError("不适用指标不得绑定评分尝试")
        blocking = [
            diagnostic
            for diagnostic in self.diagnostics
            if diagnostic.code in BLOCKING_CODES
        ]
        pending = [
            row for row in self.feedback_set.feedback if row.status == "pending"
        ]
        if self.status == "not_applicable":
            if any(
                row.status != "not_applicable"
                for row in self.feedback_set.feedback
            ):
                raise ValueError("不适用检查点必须两项指标均不适用")
            if self.attempts:
                raise ValueError("不适用检查点不得携带评分尝试")
        elif self.status == "blocked":
            if not blocking:
                raise ValueError("阻断状态必须记录阻断诊断")
        elif self.status == "pending":
            if blocking:
                raise ValueError("待回收状态不得携带阻断诊断")
            if not pending:
                raise ValueError("待回收状态必须存在待评分指标")
        else:
            if blocking:
                raise ValueError("完成状态不得携带阻断诊断")
            if pending:
                raise ValueError("完成状态不得存在待评分指标")
        # evidence_sha256 派生公式：按 evidence_id/attempt_id 排序后的原始证据（score 形态）、
        # 反馈集合与评分尝试三部分的 canonical JSON 摘要。
        expected = sha256_text(
            canonical_json(
                {
                    "evidence": [
                        item.model_dump(mode="json")
                        for item in sorted(
                            self.evidence, key=lambda row: row.evidence_id
                        )
                    ],
                    "feedback_set": self.feedback_set.model_dump(mode="json"),
                    "attempts": [
                        item.model_dump(mode="json")
                        for item in sorted(
                            self.attempts, key=lambda row: row.attempt_id
                        )
                    ],
                }
            )
        )
        if self.evidence_sha256 != expected:
            raise ValueError("回收证据摘要与原始证据/归一化结果不一致")
        return self


class FeedbackSourceSummary(EvaluationContract):
    run_id: NonEmptyText
    dataset: DatasetIdentity
    source_sha256: Sha256
    payload_sha256: Sha256 | None = None
    rows_sha256: Sha256 | None = None
    projections_sha256: Sha256 | None = None
    coverage: FeedbackCoverage
    row_count: int = Field(ge=0)
    example_count: int = Field(ge=0)


class FeedbackExperimentSummary(EvaluationContract):
    experiment_id: NonEmptyText
    experiment_name: NonEmptyText
    dataset_id: NonEmptyText
    dataset_name: NonEmptyText
    submitted_at: NonEmptyText | None = None


class FeedbackCollectionBatch(EvaluationContract):
    schema_version: Literal[
        "fit-agent-external-judge-feedback-collection-langfuse-2"
    ]
    content_sha256: Sha256
    run_id: NonEmptyText
    read_at: NonEmptyText
    source_assurance: Literal["declared_binding"]
    source: FeedbackSourceSummary
    binding_sha256: Sha256
    binding: FeedbackBinding
    experiment: FeedbackExperimentSummary | None = None
    checkpoints: list[CheckpointFeedbackRecord] = Field(min_length=1)
    status: Literal["complete", "pending", "blocked"]
    blocking: list[NonEmptyText] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_batch(self) -> "FeedbackCollectionBatch":
        if self.run_id != self.source.run_id or self.run_id != self.binding.run_id:
            raise ValueError("回收批次的运行身份不一致")
        expected_binding = sha256_text(
            canonical_json(self.binding.model_dump(mode="json"))
        )
        if self.binding_sha256 != expected_binding:
            raise ValueError("回收批次的绑定摘要不一致")
        keys = [
            (
                item.checkpoint.scenario_id,
                item.checkpoint.event_id,
                item.checkpoint.checkpoint_id,
            )
            for item in self.checkpoints
        ]
        if len(set(keys)) != len(keys):
            raise ValueError("回收批次的检查点身份不能重复")
        if len(self.checkpoints) != self.source.coverage.expected:
            raise ValueError("回收批次的检查点数与覆盖账本不一致")
        if self.source.row_count != sum(
            1 for item in self.checkpoints if item.row_id is not None
        ):
            raise ValueError("回收批次的上传行数与投影行数不一致")
        if self.source.example_count != self.source.row_count:
            raise ValueError("回收批次的 Example 数与上传行数不一致")
        if self.experiment is None:
            if self.source.payload_sha256 is not None and self.source.row_count:
                raise ValueError("无实验回执的批次不得包含上传行")
            if self.binding.experiment is not None:
                raise ValueError("无实验回执的批次不得声明实验绑定")
            if any(item.attempts for item in self.checkpoints):
                raise ValueError("无实验回执的批次不得携带评分尝试")
        else:
            if self.binding.experiment is None:
                raise ValueError("存在实验回执的批次必须绑定实验身份")
            target = self.binding.experiment
            if (
                target.experiment_id != self.experiment.experiment_id
                or target.experiment_name != self.experiment.experiment_name
                or target.dataset_id != self.experiment.dataset_id
                or target.dataset_name != self.experiment.dataset_name
            ):
                raise ValueError("批次实验身份与绑定不一致")
            for field in (
                "payload_sha256",
                "rows_sha256",
                "projections_sha256",
            ):
                if getattr(self.source, field) != getattr(target, field):
                    raise ValueError(f"批次源摘要与绑定不一致: {field}")
            for item in self.checkpoints:
                for attempt in item.attempts:
                    if (
                        attempt.source.experiment.experiment_id
                        != self.experiment.experiment_id
                        or attempt.cloud_score is None
                    ):
                        raise ValueError("评分尝试的实验身份与批次不一致")
        blocked = any(item.status == "blocked" for item in self.checkpoints)
        pending = any(item.status == "pending" for item in self.checkpoints)
        if self.status == "blocked":
            if not blocked or not self.blocking:
                raise ValueError("阻断批次必须包含阻断检查点与依据")
        elif self.status == "pending":
            if blocked or not pending or self.blocking:
                raise ValueError("待回收批次不得包含阻断依据")
        else:
            if blocked or pending or self.blocking:
                raise ValueError("完成批次不得包含阻断或待回收检查点")
        expected_content = _batch_content_sha256(
            self.model_dump(mode="json")
        )
        if self.content_sha256 != expected_content:
            raise ValueError("回收批次内容摘要与正文不一致")
        return self


class FeedbackBatchReference(EvaluationContract):
    batch_id: NonEmptyText
    content_sha256: Sha256
    path: NonEmptyText
    read_at: NonEmptyText
    status: Literal["complete", "pending", "blocked"]


class FeedbackCollectionIndex(EvaluationContract):
    schema_version: Literal[
        "fit-agent-external-judge-feedback-index-langfuse-2"
    ]
    run_id: NonEmptyText
    source_sha256: Sha256
    payload_sha256: Sha256 | None = None
    applicability_sha256: Sha256
    binding_sha256: Sha256
    experiment_id: NonEmptyText | None = None
    latest: NonEmptyText
    batches: list[FeedbackBatchReference] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_index(self) -> "FeedbackCollectionIndex":
        ids = [item.batch_id for item in self.batches]
        if len(set(ids)) != len(ids):
            raise ValueError("回收索引的批次 ID 不能重复")
        paths = [item.path for item in self.batches]
        if len(set(paths)) != len(paths):
            raise ValueError("回收索引的批次路径不能重复")
        if self.latest not in ids:
            raise ValueError("回收索引的最新批次不在批次列表中")
        return self


def _batch_content_sha256(document: dict[str, Any]) -> str:
    material = {
        key: value
        for key, value in document.items()
        if key not in ("content_sha256", "read_at")
    }
    return sha256_text(canonical_json(material))


def _batch_id(content_sha256: str) -> str:
    return f"batch-{content_sha256[:16]}"


@dataclass(frozen=True)
class FeedbackContext:
    evidence: ExternalRunEvidence
    applicability: dict[tuple[str, str, str], ExternalApplicabilityEntry]
    ledger: dict[str, Any]
    payload: dict[str, Any] | None
    dataset_receipt: dict[str, Any] | None
    experiment_receipt: dict[str, Any] | None
    binding: FeedbackBinding
    binding_sha256: str
    source: FeedbackSourceSummary
    experiment: FeedbackExperimentSummary | None
    rows: dict[str, dict[str, Any]]
    rows_by_key: dict[tuple[str, str, str], dict[str, Any]]


@dataclass(frozen=True)
class FeedbackHistoryEntry:
    semantic_sha256: str
    status: str
    score: float | None


@dataclass(frozen=True)
class FeedbackHistory:
    terminal: dict[
        tuple[tuple[str, str, str], str], dict[str, list[FeedbackHistoryEntry]]
    ]


@dataclass(frozen=True)
class LoadedCollection:
    index: FeedbackCollectionIndex
    batches: dict[str, FeedbackCollectionBatch]


@dataclass(frozen=True)
class VerifiedCollection:
    context: FeedbackContext
    collection: LoadedCollection | None
    latest: FeedbackCollectionBatch | None


def _checkpoint_key(link: EvidenceLink) -> tuple[str, str, str]:
    return (link.scenario_id, link.event_id, link.checkpoint_id)


def _row_key(row: dict[str, Any]) -> tuple[str, str, str]:
    checkpoint = row["upload"]["metadata"]["checkpoint"]
    return (
        checkpoint["scenario_id"],
        checkpoint["event_id"],
        checkpoint["checkpoint_id"],
    )


def _read_dataset_receipt(
    run_dir: Path, payload: dict[str, Any]
) -> dict[str, Any]:
    path = external_dir(run_dir) / EXTERNAL_DATASET_RECEIPT_NAME
    _require(path.is_file(), "缺少检查点数据集回执，禁止回收")
    receipt = _read_json(path)
    _external_receipt_identity(
        receipt, payload, schema=EXTERNAL_DATASET_RECEIPT_SCHEMA
    )
    _require(
        receipt.get("projections_sha256") == payload["projections_sha256"],
        "数据集回执的投影摘要与 payload 不一致",
    )
    _require(
        receipt.get("dataset") == payload["dataset"],
        "数据集回执的数据身份与 payload 不一致",
    )
    _require(
        receipt.get("item_ids") == [row["row_id"] for row in payload["rows"]],
        "数据集回执的样例身份与 payload 不一致",
    )
    _require(
        receipt.get("example_count") == len(payload["rows"]),
        "数据集回执的样例数与 payload 不一致",
    )
    _require(
        isinstance(receipt.get("dataset_id"), str)
        and bool(receipt["dataset_id"]),
        "数据集回执缺少 dataset_id",
    )
    _require(
        receipt.get("web_judge")
        == _web_judge_configuration(payload, receipt["dataset_id"]),
        "数据集回执的网页裁判配置与当前投影不一致",
    )
    return receipt


def _verify_receipt_projections(
    payload: dict[str, Any], receipt: dict[str, Any]
) -> None:
    projections = receipt.get("projections")
    expected = {
        row["row_id"]: {
            "checkpoint": row["upload"]["metadata"]["checkpoint"],
            "execution_sha256": row["upload"]["metadata"]["execution_sha256"],
            "projection_sha256": row["projection_sha256"],
        }
        for row in payload["rows"]
    }
    _require(
        isinstance(projections, dict) and set(projections) == set(expected),
        "实验回执的逐行投影身份与 payload 不一致",
    )
    for row_id, document in expected.items():
        _require(
            projections[row_id] == document,
            f"实验回执的逐行投影内容不一致: {row_id}",
        )


def _read_experiment_receipt(
    run_dir: Path, payload: dict[str, Any], dataset_receipt: dict[str, Any]
) -> dict[str, Any]:
    receipt_path = external_dir(run_dir) / EXTERNAL_EXPERIMENT_RECEIPT_NAME
    pending_path = external_dir(run_dir) / EXTERNAL_EXPERIMENT_PENDING_NAME
    if pending_path.is_file():
        _require(
            not receipt_path.is_file(),
            "实验回执与提交 pending 标记并存，拒绝回收",
        )
        raise UploadBlocked(
            "实验提交未完成（存在 pending 标记），禁止回收或补提交"
        )
    _require(
        receipt_path.is_file(),
        "缺少已提交实验回执，禁止回收或补提交",
    )
    receipt = _read_json(receipt_path)
    _external_receipt_identity(
        receipt, payload, schema=EXTERNAL_EXPERIMENT_RECEIPT_SCHEMA
    )
    _require(
        receipt.get("projections_sha256") == payload["projections_sha256"],
        "实验回执的投影摘要与 payload 不一致",
    )
    _require(
        receipt.get("dataset") == payload["dataset"],
        "实验回执的数据身份与 payload 不一致",
    )
    _require(
        receipt.get("item_ids") == [row["row_id"] for row in payload["rows"]],
        "实验回执的样例身份与 payload 不一致",
    )
    _require(
        receipt.get("coverage") == payload["coverage"],
        "实验回执的覆盖账本与 payload 不一致",
    )
    _require(
        receipt.get("dataset_name") == payload["dataset_name"],
        "实验回执的数据集名称与 payload 不一致",
    )
    _require(
        receipt.get("experiment_name") == payload["experiment_name"],
        "实验回执的实验名称与 payload 不一致",
    )
    _require(
        receipt.get("dataset_id") == dataset_receipt["dataset_id"],
        "实验回执的数据集 ID 与数据集回执不一致",
    )
    _require(
        receipt.get("web_judge")
        == _web_judge_configuration(
            payload, dataset_receipt["dataset_id"]
        ),
        "实验回执的网页裁判配置与当前投影不一致",
    )
    _require(
        receipt.get("external_scoring_status") == "pending_collection",
        "实验回执的回收状态已被改写，拒绝继续",
    )
    _require(
        isinstance(receipt.get("experiment_id"), str)
        and bool(receipt["experiment_id"]),
        "实验回执缺少 experiment_id",
    )
    _verify_receipt_projections(payload, receipt)
    runs = receipt.get("runs")
    row_ids = [row["row_id"] for row in payload["rows"]]
    _require(
        isinstance(runs, dict) and set(runs) == set(row_ids),
        "实验回执的运行映射与 payload 行不一致",
    )
    for row_id in row_ids:
        _require(
            isinstance(runs[row_id], str) and bool(runs[row_id]),
            f"实验回执缺少云端 trace: {row_id}",
        )
    return receipt


def _binding_identity(
    path: Path,
    evidence: ExternalRunEvidence,
    applicability_sha256: str,
    payload: dict[str, Any] | None,
    dataset_receipt: dict[str, Any] | None,
    experiment_receipt: dict[str, Any] | None,
) -> FeedbackBinding:
    _require(path.is_file(), f"缺少显式反馈绑定文件: {path}")
    try:
        binding = FeedbackBinding.model_validate(_read_json(path))
    except ValidationError as error:
        detail = error.errors()[0]
        raise UploadBlocked(
            f"反馈绑定无法解析: {detail['loc']}: {detail['msg']}"
        ) from error
    _assert_no_secret(binding.model_dump(mode="json"), "反馈绑定")
    _require(
        binding.run_id == evidence.run_id,
        "反馈绑定 run_id 与源运行不一致",
    )
    _require(
        binding.source_sha256 == evidence.source_sha256,
        "反馈绑定 source_sha256 与源证据不一致",
    )
    _require(
        binding.applicability_sha256 == applicability_sha256,
        "反馈绑定 applicability_sha256 与已批准适用性不一致，"
        f"应为 {applicability_sha256}",
    )
    if payload is None:
        _require(
            binding.experiment is None,
            "无上传行的反馈绑定不得声明实验身份",
        )
        return binding
    _require(
        binding.experiment is not None,
        "有上传行的反馈绑定必须声明实验身份",
    )
    _require(dataset_receipt is not None, "缺少数据集回执")
    _require(experiment_receipt is not None, "缺少实验回执")
    target = binding.experiment
    assert target is not None
    _require(
        target.experiment_id == experiment_receipt["experiment_id"]
        and target.experiment_name == experiment_receipt["experiment_name"],
        "反馈绑定的实验身份与实验回执不一致",
    )
    _require(
        target.dataset_id == dataset_receipt["dataset_id"]
        and target.dataset_name == payload["dataset_name"],
        "反馈绑定的数据集身份与回执不一致",
    )
    scope = binding.evaluator.config["scope"]
    _require(
        scope.get("dataset_id") == dataset_receipt["dataset_id"]
        and scope.get("dataset_name") == payload["dataset_name"]
        and scope.get("observation_scope") == EXTERNAL_OBSERVATION_SCOPE
        and scope.get("sampling") == EXTERNAL_RULE_SAMPLING,
        "反馈绑定的 rule 作用域与检查点数据集回执不一致",
    )
    _require(
        target.payload_sha256 == payload_digest(payload)
        and target.rows_sha256 == payload["rows_sha256"]
        and target.projections_sha256 == payload["projections_sha256"],
        "反馈绑定的投影摘要与 payload 不一致",
    )
    return binding


def load_feedback_context(
    source: Path, *, dataset_dir: Path = DEFAULT_VERSION_DIR
) -> FeedbackContext:
    dataset: EvaluationDataset = load_for_evaluation(Path(dataset_dir))
    evidence = load_external_run(Path(source), dataset)
    applicability = load_external_applicability(evidence)
    ledger = _coverage_ledger(evidence, applicability)
    uploadable = ledger["counts"]["uploadable"]
    run_dir = evidence.run_dir
    payload: dict[str, Any] | None = None
    dataset_receipt: dict[str, Any] | None = None
    experiment_receipt: dict[str, Any] | None = None
    if uploadable:
        payload_path = external_dir(run_dir) / EXTERNAL_PAYLOAD_NAME
        _require(
            payload_path.is_file(),
            "缺少检查点投影 payload，请先完成任务 3 投影",
        )
        on_disk = _read_json(payload_path)
        _require(
            isinstance(on_disk, dict)
            and on_disk.get("schema") == EXTERNAL_PAYLOAD_SCHEMA,
            "检查点投影 payload 版本不受支持",
        )
        rebuilt, _ = build_external_payload(evidence, applicability)
        _require(
            on_disk == rebuilt,
            "本地投影与当前源证据不一致，拒绝基于陈旧产物回收",
        )
        _require(
            ledger["counts"] == rebuilt["coverage"],
            "覆盖账本与当前投影不一致",
        )
        payload = rebuilt
        dataset_receipt = _read_dataset_receipt(run_dir, payload)
        experiment_receipt = _read_experiment_receipt(
            run_dir, payload, dataset_receipt
        )
    else:
        payload_path = external_dir(run_dir) / EXTERNAL_PAYLOAD_NAME
        if payload_path.is_file():
            stale = _read_json(payload_path)
            _require(
                isinstance(stale, dict)
                and stale.get("schema") == EXTERNAL_PAYLOAD_SCHEMA
                and not stale.get("rows"),
                "无上传行的运行存在非空投影，拒绝回收",
            )
        _require(
            not (
                external_dir(run_dir) / EXTERNAL_EXPERIMENT_RECEIPT_NAME
            ).is_file(),
            "无上传行的运行存在实验回执，拒绝回收",
        )
        _require(
            not (
                external_dir(run_dir) / EXTERNAL_DATASET_RECEIPT_NAME
            ).is_file(),
            "无上传行的运行存在数据集回执，拒绝回收",
        )
        _require(
            not (
                external_dir(run_dir) / EXTERNAL_EXPERIMENT_PENDING_NAME
            ).is_file(),
            "无上传行的运行存在提交 pending 标记，拒绝回收",
        )
    applicability_sha256 = (
        payload["applicability_sha256"]
        if payload is not None
        else _external_applicability_sha256(evidence, applicability)
    )
    binding = _binding_identity(
        feedback_binding_path(run_dir),
        evidence,
        applicability_sha256,
        payload,
        dataset_receipt,
        experiment_receipt,
    )
    binding_sha256 = sha256_text(
        canonical_json(binding.model_dump(mode="json"))
    )
    rows = (
        {row["row_id"]: row for row in payload["rows"]}
        if payload is not None
        else {}
    )
    rows_by_key = {_row_key(row): row for row in rows.values()}
    if payload is not None:
        _require(
            len(rows_by_key) == len(rows),
            "payload 中存在重复检查点投影",
        )
        classifications = {
            (
                entry["checkpoint"]["scenario_id"],
                entry["checkpoint"]["event_id"],
                entry["checkpoint"]["checkpoint_id"],
            ): entry["classification"]
            for entry in ledger["entries"]
        }
        for checkpoint in evidence.checkpoints:
            if classifications[checkpoint.key] == "uploadable":
                _require(
                    checkpoint.key in rows_by_key,
                    f"可上传检查点缺少投影: {checkpoint.key}",
                )
    coverage = FeedbackCoverage(
        expected=ledger["counts"]["expected"],
        uploadable=ledger["counts"]["uploadable"],
        missing=ledger["counts"]["missing"],
        not_applicable=ledger["counts"]["not_applicable"],
        pending_review=ledger["counts"]["pending_review"],
    )
    source_summary = FeedbackSourceSummary(
        run_id=evidence.run_id,
        dataset=evidence.dataset_identity,
        source_sha256=evidence.source_sha256,
        payload_sha256=payload_digest(payload) if payload else None,
        rows_sha256=payload["rows_sha256"] if payload else None,
        projections_sha256=payload["projections_sha256"] if payload else None,
        coverage=coverage,
        row_count=len(rows),
        example_count=len(rows),
    )
    experiment = None
    if experiment_receipt is not None:
        experiment = FeedbackExperimentSummary(
            experiment_id=experiment_receipt["experiment_id"],
            experiment_name=experiment_receipt["experiment_name"],
            dataset_id=experiment_receipt["dataset_id"],
            dataset_name=experiment_receipt["dataset_name"],
            submitted_at=experiment_receipt.get("submitted_at"),
        )
    return FeedbackContext(
        evidence=evidence,
        applicability=applicability,
        ledger=ledger,
        payload=payload,
        dataset_receipt=dataset_receipt,
        experiment_receipt=experiment_receipt,
        binding=binding,
        binding_sha256=binding_sha256,
        source=source_summary,
        experiment=experiment,
        rows=rows,
        rows_by_key=rows_by_key,
    )


def _metric_binding(context: FeedbackContext) -> dict[str, FeedbackMetricBinding]:
    return {row.metric: row for row in context.binding.metrics}


def _probability(value: Any) -> float:
    try:
        return TypeAdapter(ProbabilityScore).validate_python(value)
    except ValidationError as error:
        raise ValueError(str(error.errors()[0]["msg"])) from error


def _evidence_row(raw: dict[str, Any]) -> FeedbackRawEvidence:
    score_id, trace_id, name = _score_identity(raw)
    raw_sha256 = sha256_text(canonical_json(raw))
    return FeedbackRawEvidence(
        evidence_id=f"{score_id}@{raw_sha256[:12]}",
        score_id=score_id,
        trace_id=trace_id,
        score_name=name,
        raw=raw,
        raw_sha256=raw_sha256,
    )


def _author_identity(raw: dict[str, Any]) -> str | None:
    author = raw.get("authorUserId")
    return author if isinstance(author, str) and author.strip() else None


def _attempt(
    context: FeedbackContext,
    checkpoint,
    metric_binding: FeedbackMetricBinding,
    row: dict[str, Any],
    raw: dict[str, Any],
    feedback: ExternalJudgeFeedback,
) -> ExternalJudgeLangfuseScoringAttempt:
    score_id, trace_id, name = _score_identity(raw)
    cloud_trace_id = context.experiment_receipt["runs"][row["row_id"]]
    _require(
        trace_id == cloud_trace_id,
        f"云端反馈的运行身份与回执不一致: {score_id}",
    )
    timestamp = raw.get("timestamp")
    _require(
        isinstance(timestamp, str) and bool(timestamp.strip()),
        f"云端反馈缺少 timestamp: {score_id}",
    )
    _require(context.experiment is not None, "缺少实验身份，无法构造评分尝试")
    _require(
        raw.get("source") == "EVAL",
        f"反馈来源不是自动裁判: {score_id}",
    )
    # attempt_id 派生公式：本地运行/检查点/执行摘要/指标 + score 身份材料
    # （score_id、name、timestamp、trace_id）经 uuid5(NAMESPACE_URL, ...) 派生。
    attempt_id = str(
        uuid5(
            NAMESPACE_URL,
            "fit-agent-external-judge-attempt-langfuse:"
            f"{checkpoint.link.run_id}:{checkpoint.link.scenario_id}:"
            f"{checkpoint.link.event_id}:{checkpoint.link.checkpoint_id}:"
            f"{checkpoint.execution_sha256}:{metric_binding.metric}:"
            f"{score_id}:{name}:{timestamp}:{trace_id}",
        )
    )
    return ExternalJudgeLangfuseScoringAttempt(
        schema_version=EXTERNAL_JUDGE_FEEDBACK_LANGFUSE_SCHEMA_VERSION,
        attempt_id=attempt_id,
        checkpoint=checkpoint.link,
        execution_sha256=checkpoint.execution_sha256,
        source=ExternalJudgeLangfuseSource(
            experiment=ExternalJudgeExperimentIdentity(
                experiment_id=context.experiment.experiment_id,
                experiment_name=context.experiment.experiment_name,
            ),
            trace_id=cloud_trace_id,
            example_id=row["row_id"],
            feedback_source=ExternalJudgeScoreSource(
                kind="eval", author_user_id=_author_identity(raw)
            ),
        ),
        model=ExternalJudgeModelIdentity(
            provider=context.binding.evaluator.provider,
            evaluator=context.binding.evaluator.evaluator,
            model_alias=context.binding.evaluator.model_alias,
            # Langfuse Score 读回不含裁判模型版本字段，一律按平台未返回记录。
            model_version=UnreportedModelVersion(kind="unreported"),
            config_sha256=context.binding.evaluator.config_sha256,
        ),
        definition=ExternalJudgeDefinitionIdentity(
            question_id=metric_binding.question_id,
            question_sha256=metric_binding.question_sha256,
            rubric_id=metric_binding.rubric_id,
            rubric_sha256=metric_binding.rubric_sha256,
        ),
        outbound=ExternalJudgeOutboundProjection(
            row_id=row["row_id"],
            projection_sha256=row["projection_sha256"],
        ),
        feedback=feedback,
        cloud_score=ExternalJudgeCloudScoreIdentity(
            score_id=score_id,
            trace_id=trace_id,
            name=name,
            timestamp=timestamp,
            source="eval",
            config_id=raw.get("configId"),
        ),
    )


def _normalize_candidate(
    raw: dict[str, Any],
    metric_binding: FeedbackMetricBinding,
) -> tuple[ExternalJudgeFeedback | None, str | None, dict[str, Any] | None]:
    score_id = raw.get("id")
    if raw.get("source") != "EVAL":
        return (
            None,
            "source_not_decision_model",
            {"source": raw.get("source"), "score_id": score_id},
        )
    if raw.get("dataType") != "NUMERIC":
        return (
            None,
            "invalid_score",
            {
                "dataType": raw.get("dataType"),
                "value": _json_safe(raw.get("value")),
                "score_id": score_id,
            },
        )
    try:
        value = _probability(raw.get("value"))
    except ValueError as error:
        return (
            None,
            "invalid_score",
            {
                "value": _json_safe(raw.get("value")),
                "score_id": score_id,
                "error": str(error),
            },
        )
    return (
        ExternalJudgeFeedback(
            metric=metric_binding.metric,
            status="succeeded",
            score=value,
        ),
        None,
        None,
    )


def _judge_failure(
    observations: Sequence[dict[str, Any]], evaluator_name: str
) -> dict[str, Any] | None:
    # 网页裁判调试 trace 根 observation 命名为 "Execute evaluator: <名称>"；
    # 只有名称可归属到本指标 evaluator 的 Error observation 才能作为失败依据。
    matches = sorted(
        (
            observation
            for observation in observations
            if observation.get("level") == "ERROR"
            and observation.get("name")
            in (evaluator_name, f"Execute evaluator: {evaluator_name}")
        ),
        key=lambda observation: str(observation.get("id") or ""),
    )
    return matches[0] if matches else None


def _failure_evidence(observation: dict[str, Any]) -> str:
    return canonical_json(
        {
            "environment": observation.get("environment"),
            "id": observation.get("id"),
            "level": observation.get("level"),
            "name": observation.get("name"),
            "start_time": observation.get("startTime"),
            "status_message": observation.get("statusMessage"),
            "trace_id": observation.get("traceId"),
        }
    )


def _missing_upload_detail(checkpoint) -> str:
    entry = checkpoint.entry
    if entry is None:
        basis = ["generation_checkpoint"]
    elif entry.runner_error is not None:
        basis = ["generation_runner_error"]
    else:
        basis = list(checkpoint.readiness.missing) or ["upload_evidence"]
    detail = (
        f"检查点未进入上传投影: {checkpoint.link.checkpoint_id}; "
        f"缺失 {', '.join(basis)}"
    )
    if checkpoint.record.execution_status != "completed":
        detail += f"; 执行状态 {checkpoint.record.execution_status}"
    if checkpoint.record.runner_error:
        detail += f"; 执行失败依据 {checkpoint.record.runner_error}"
    return detail


def _checkpoint_record(
    context: FeedbackContext,
    checkpoint,
    row: dict[str, Any] | None,
    cloud_trace_id: str | None,
    raw_items: Sequence[dict[str, Any]],
    judge_failures: Sequence[dict[str, Any]],
    history: FeedbackHistory | None,
) -> CheckpointFeedbackRecord:
    approved = context.applicability[checkpoint.key]
    approved_by_metric = {
        item.metric: item for item in approved.applicability
    }
    metric_bindings = _metric_binding(context)
    evaluator_names = _binding_evaluator_names(
        context.binding.evaluator.config
    )
    exclusions: list[FeedbackExclusion] = []
    diagnostics: list[FeedbackDiagnostic] = []
    attempts: list[ExternalJudgeLangfuseScoringAttempt] = []
    feedback_rows: list[ExternalJudgeFeedback] = []
    primary_evidence: dict[str, list[FeedbackRawEvidence]] = {}
    semantic_by_id: dict[str, set[str]] = {}
    mapped_keys = {
        item.feedback_key: item.metric for item in context.binding.metrics
    }
    for raw in raw_items:
        try:
            item = _evidence_row(raw)
        except UploadBlocked as error:
            diagnostics.append(
                FeedbackDiagnostic(
                    code="malformed_cloud_feedback", detail=str(error)
                )
            )
            continue
        primary_evidence.setdefault(item.score_id, []).append(item)
        semantic = sha256_text(canonical_json(_semantic_feedback(item.raw)))
        semantic_by_id.setdefault(item.score_id, set()).add(semantic)
        name = item.score_name
        if name not in mapped_keys:
            exclusions.append(
                FeedbackExclusion(
                    evidence_id=item.evidence_id,
                    score_id=item.score_id,
                    reason="unmapped_key",
                    detail=f"反馈 score name 未映射到外部裁判指标: {name}",
                )
            )
    unique_evidence: list[FeedbackRawEvidence] = []
    for score_id, versions in sorted(primary_evidence.items()):
        seen_semantic: set[str] = set()
        for item in sorted(versions, key=lambda row: row.raw_sha256):
            semantic = sha256_text(
                canonical_json(_semantic_feedback(item.raw))
            )
            if semantic in seen_semantic:
                continue
            seen_semantic.add(semantic)
            unique_evidence.append(item)
        if len(semantic_by_id[score_id]) > 1:
            diagnostics.append(
                FeedbackDiagnostic(
                    code="feedback_content_drift",
                    detail=(
                        "同一远端 score ID 在本次读取中出现不同内容: "
                        f"{score_id}"
                    ),
                )
            )
    for metric in EXTERNAL_JUDGE_METRICS:
        metric_binding = metric_bindings[metric]
        approval = approved_by_metric[metric]
        candidates = [
            item
            for item in unique_evidence
            if item.score_name == metric_binding.feedback_key
        ]
        if not approval.applicable:
            feedback_rows.append(
                ExternalJudgeFeedback(
                    metric=metric,
                    status="not_applicable",
                    applicability_reason=approval.reason,
                )
            )
            for item in candidates:
                exclusions.append(
                    FeedbackExclusion(
                        evidence_id=item.evidence_id,
                        score_id=item.score_id,
                        metric=metric,
                        reason="not_applicable_metric",
                        detail="指标已批准为不适用，云端分数仅作审计",
                    )
                )
            if candidates:
                diagnostics.append(
                    FeedbackDiagnostic(
                        code="unexpected_not_applicable_feedback",
                        metric=metric,
                        detail=(
                            "不适用指标收到云端反馈，已排除出成功统计: "
                            f"{', '.join(item.score_id for item in candidates)}"
                        ),
                    )
                )
            continue
        if row is None:
            feedback_rows.append(
                ExternalJudgeFeedback(metric=metric, status="pending")
            )
            if not any(
                diagnostic.code == "checkpoint_missing_upload"
                for diagnostic in diagnostics
            ):
                diagnostics.append(
                    FeedbackDiagnostic(
                        code="checkpoint_missing_upload",
                        metric=metric,
                        detail=_missing_upload_detail(checkpoint),
                    )
                )
            continue
        if cloud_trace_id is None:
            feedback_rows.append(
                ExternalJudgeFeedback(metric=metric, status="pending")
            )
            diagnostics.append(
                FeedbackDiagnostic(
                    code="missing_cloud_trace",
                    metric=metric,
                    detail=f"检查点缺少云端 trace: {row['row_id']}",
                )
            )
            continue
        historical = (
            {}
            if history is None
            else history.terminal.get((checkpoint.key, metric), {})
        )
        current_ids = {item.score_id for item in candidates}
        for score_id, entries in sorted(historical.items()):
            if score_id not in current_ids:
                diagnostics.append(
                    FeedbackDiagnostic(
                        code="feedback_history_conflict",
                        metric=metric,
                        detail=(
                            "历史评分反馈未出现在本次读取中: "
                            f"{score_id}"
                        ),
                    )
                )
                continue
            current_semantics = semantic_by_id.get(score_id, set())
            if not current_semantics or any(
                entry.semantic_sha256 not in current_semantics
                for entry in entries
            ):
                diagnostics.append(
                    FeedbackDiagnostic(
                        code="feedback_content_drift",
                        metric=metric,
                        detail=(
                            "远端反馈内容相对历史回收证据发生变化: "
                            f"{score_id}"
                        ),
                    )
                )
        if not candidates:
            failure = _judge_failure(
                judge_failures, evaluator_names[metric]
            )
            if failure is not None:
                feedback_rows.append(
                    ExternalJudgeFeedback(
                        metric=metric,
                        status="failed",
                        failure_evidence=_failure_evidence(failure),
                    )
                )
                diagnostics.append(
                    FeedbackDiagnostic(
                        code="cloud_failure",
                        metric=metric,
                        detail=(
                            "网页裁判执行失败: "
                            f"{failure.get('id')}"
                        ),
                    )
                )
                continue
            feedback_rows.append(
                ExternalJudgeFeedback(metric=metric, status="pending")
            )
            diagnostics.append(
                FeedbackDiagnostic(
                    code="awaiting_feedback",
                    metric=metric,
                    detail=(
                        "尚未读取到映射 score: "
                        f"{metric_binding.feedback_key}; "
                        f"网页裁判调试 observation（environment="
                        f"{WEB_JUDGE_ENVIRONMENT}）无可归属 Error 证据，"
                        "不推断云端失败"
                    ),
                )
            )
            continue
        terminal: list[tuple[FeedbackRawEvidence, ExternalJudgeFeedback]] = []
        for item in candidates:
            if len(semantic_by_id[item.score_id]) > 1:
                continue
            feedback, reason, audit = _normalize_candidate(
                item.raw, metric_binding
            )
            if reason is not None:
                if reason == "source_not_decision_model":
                    detail = "反馈来源不是 EVAL 网页裁判"
                    exclusions.append(
                        FeedbackExclusion(
                            evidence_id=item.evidence_id,
                            score_id=item.score_id,
                            metric=metric,
                            reason=reason,
                            detail=detail,
                        )
                    )
                    diagnostics.append(
                        FeedbackDiagnostic(
                            code="source_gap",
                            metric=metric,
                            detail=f"{detail}: {item.score_id}",
                        )
                    )
                elif reason == "invalid_score":
                    detail = canonical_json(audit)
                    exclusions.append(
                        FeedbackExclusion(
                            evidence_id=item.evidence_id,
                            score_id=item.score_id,
                            metric=metric,
                            reason="invalid_score",
                            detail=detail,
                        )
                    )
                    diagnostics.append(
                        FeedbackDiagnostic(
                            code="invalid_score",
                            metric=metric,
                            detail=(
                                "反馈分数不是 NUMERIC 的有限 0..1 数值: "
                                f"{item.score_id}; {detail}"
                            ),
                        )
                    )
                continue
            assert feedback is not None
            terminal.append((item, feedback))
        if len(terminal) > 1:
            diagnostics.append(
                FeedbackDiagnostic(
                    code="feedback_conflict",
                    metric=metric,
                    detail=(
                        "同一指标存在多个终态反馈，拒绝自动选择: "
                        f"{', '.join(item.score_id for item, _ in terminal)}"
                    ),
                )
            )
        if len(terminal) == 1:
            item, feedback = terminal[0]
            attempt = _attempt(
                context, checkpoint, metric_binding, row, item.raw, feedback
            )
            attempts.append(attempt)
            feedback_rows.append(feedback)
        else:
            feedback_rows.append(
                ExternalJudgeFeedback(metric=metric, status="pending")
            )
            for item, feedback in terminal:
                attempt = _attempt(
                    context,
                    checkpoint,
                    metric_binding,
                    row,
                    item.raw,
                    feedback,
                )
                attempts.append(attempt)
    order = {metric: index for index, metric in enumerate(EXTERNAL_JUDGE_METRICS)}
    feedback_rows.sort(key=lambda row: order[row.metric])
    if len(feedback_rows) != 2:
        raise UploadBlocked(
            f"检查点反馈集合必须包含两项指标: {checkpoint.key}"
        )
    feedback_set = ExternalJudgeFeedbackSet(
        schema_version=EXTERNAL_JUDGE_FEEDBACK_SCHEMA_VERSION,
        checkpoint=checkpoint.link,
        execution_sha256=checkpoint.execution_sha256,
        feedback=feedback_rows,
    )
    blocking = [
        diagnostic
        for diagnostic in diagnostics
        if diagnostic.code in BLOCKING_CODES
    ]
    pending = any(row.status == "pending" for row in feedback_rows)
    if all(
        row.status == "not_applicable" for row in feedback_rows
    ):
        status = "not_applicable"
    elif blocking:
        status = "blocked"
    elif pending:
        status = "pending"
    else:
        status = "complete"
    evidence_sorted = sorted(unique_evidence, key=lambda item: item.evidence_id)
    attempts_sorted = sorted(attempts, key=lambda item: item.attempt_id)
    exclusions_sorted = sorted(
        exclusions, key=lambda item: (item.evidence_id, item.reason)
    )
    diagnostics_sorted = sorted(
        diagnostics,
        key=lambda item: (item.code, item.metric or "", item.detail),
    )
    evidence_sha256 = sha256_text(
        canonical_json(
            {
                "evidence": [
                    item.model_dump(mode="json") for item in evidence_sorted
                ],
                "feedback_set": feedback_set.model_dump(mode="json"),
                "attempts": [
                    item.model_dump(mode="json") for item in attempts_sorted
                ],
            }
        )
    )
    return CheckpointFeedbackRecord(
        checkpoint=checkpoint.link,
        execution_sha256=checkpoint.execution_sha256,
        row_id=None if row is None else row["row_id"],
        example_id=None if row is None else row["row_id"],
        cloud_trace_id=cloud_trace_id,
        projection_sha256=None if row is None else row["projection_sha256"],
        feedback_set=feedback_set,
        attempts=attempts_sorted,
        evidence=evidence_sorted,
        exclusions=exclusions_sorted,
        diagnostics=diagnostics_sorted,
        evidence_sha256=evidence_sha256,
        status=status,
    )


def normalize_feedback(
    context: FeedbackContext,
    cloud_runs: dict[str, str],
    feedback_by_run: dict[str, Sequence[Any]],
    history: FeedbackHistory | None = None,
    *,
    judge_failures: dict[str, Sequence[dict[str, Any]]] | None = None,
) -> list[CheckpointFeedbackRecord]:
    failures_by_trace = judge_failures or {}
    records: list[CheckpointFeedbackRecord] = []
    for checkpoint in context.evidence.checkpoints:
        row = context.rows_by_key.get(checkpoint.key)
        cloud_trace_id = (
            None if row is None else cloud_runs.get(row["row_id"])
        )
        raw_items: list[dict[str, Any]] = []
        if cloud_trace_id is not None:
            for value in feedback_by_run.get(cloud_trace_id, ()):
                raw_items.append(_raw_feedback(value))
        records.append(
            _checkpoint_record(
                context,
                checkpoint,
                row,
                cloud_trace_id,
                raw_items,
                (
                    list(failures_by_trace.get(cloud_trace_id) or ())
                    if cloud_trace_id is not None
                    else []
                ),
                history,
            )
        )
    return records


def build_feedback_batch(
    context: FeedbackContext,
    records: Sequence[CheckpointFeedbackRecord],
    *,
    read_at: str | None = None,
) -> FeedbackCollectionBatch:
    blocked = any(record.status == "blocked" for record in records)
    pending = any(record.status == "pending" for record in records)
    if blocked:
        status = "blocked"
    elif pending:
        status = "pending"
    else:
        status = "complete"
    blocking = sorted(
        {
            diagnostic.detail
            for record in records
            for diagnostic in record.diagnostics
            if diagnostic.code in BLOCKING_CODES
        }
    )
    document = {
        "schema_version": FEEDBACK_COLLECTION_SCHEMA,
        "content_sha256": "0" * 64,
        "run_id": context.evidence.run_id,
        "read_at": read_at or datetime.now(UTC).isoformat(),
        "source_assurance": SOURCE_ASSURANCE,
        "source": context.source.model_dump(mode="json"),
        "binding_sha256": context.binding_sha256,
        "binding": context.binding.model_dump(mode="json"),
        "experiment": (
            None
            if context.experiment is None
            else context.experiment.model_dump(mode="json")
        ),
        "checkpoints": [
            record.model_dump(mode="json") for record in records
        ],
        "status": status,
        "blocking": blocking,
    }
    document = _sanitize_outbound(document)
    _assert_no_secret(document, "回收批次")
    document["content_sha256"] = _batch_content_sha256(document)
    try:
        return FeedbackCollectionBatch.model_validate(document)
    except ValidationError as error:
        detail = error.errors()[0]
        raise UploadBlocked(
            f"回收批次无法通过契约校验: {detail['loc']}: {detail['msg']}"
        ) from error


def _history_from_batches(
    context: FeedbackContext, batches: Sequence[FeedbackCollectionBatch]
) -> FeedbackHistory:
    terminal: dict[
        tuple[tuple[str, str, str], str], dict[str, list[FeedbackHistoryEntry]]
    ] = {}
    for batch in batches:
        for record in batch.checkpoints:
            evidence_by_id = {
                item.score_id: item for item in record.evidence
            }
            for attempt in record.attempts:
                if attempt.cloud_score is None:
                    continue
                item = evidence_by_id.get(attempt.cloud_score.score_id)
                _require(
                    item is not None,
                    "历史评分尝试缺少原始证据，拒绝读取",
                )
                key = (record.checkpoint.scenario_id,
                       record.checkpoint.event_id,
                       record.checkpoint.checkpoint_id)
                metric = attempt.feedback.metric
                entry = FeedbackHistoryEntry(
                    semantic_sha256=sha256_text(
                        canonical_json(_semantic_feedback(item.raw))
                    ),
                    status=attempt.feedback.status,
                    score=attempt.feedback.score,
                )
                bucket = terminal.setdefault((key, metric), {})
                bucket.setdefault(
                    attempt.cloud_score.score_id, []
                ).append(entry)
    return FeedbackHistory(terminal=terminal)


def _load_collection(context: FeedbackContext) -> LoadedCollection | None:
    path = feedback_index_path(context.evidence.run_dir)
    if not path.is_file():
        return None
    try:
        index = FeedbackCollectionIndex.model_validate(_read_json(path))
    except (ValidationError, json.JSONDecodeError) as error:
        raise UploadBlocked("回收索引损坏，拒绝继续") from error
    _require(
        index.run_id == context.evidence.run_id
        and index.source_sha256 == context.evidence.source_sha256
        and index.payload_sha256 == context.source.payload_sha256
        and index.applicability_sha256
        == context.binding.applicability_sha256
        and index.binding_sha256 == context.binding_sha256
        and index.experiment_id
        == (None if context.experiment is None else context.experiment.experiment_id),
        "回收索引与当前源/绑定身份不一致，拒绝继续",
    )
    batches: dict[str, FeedbackCollectionBatch] = {}
    expected_dir = feedback_dir(context.evidence.run_dir) / FEEDBACK_BATCH_DIR
    for reference in index.batches:
        relative = Path(reference.path)
        _require(
            relative.parent == Path(FEEDBACK_BATCH_DIR)
            and relative.name == f"{reference.batch_id}.json",
            "回收索引包含非法批次路径",
        )
        batch_path = feedback_dir(context.evidence.run_dir) / relative
        _require(
            batch_path.resolve().parent == expected_dir.resolve(),
            "回收批次路径越界",
        )
        _require(batch_path.is_file(), "回收索引指向的批次文件缺失")
        try:
            batch = FeedbackCollectionBatch.model_validate(
                _read_json(batch_path)
            )
        except (ValidationError, json.JSONDecodeError) as error:
            raise UploadBlocked("回收批次损坏，拒绝继续") from error
        _require(
            batch.content_sha256 == reference.content_sha256
            and batch.status == reference.status
            and _batch_id(batch.content_sha256) == reference.batch_id,
            "回收批次与索引记录不一致",
        )
        _require(
            batch.source == context.source
            and batch.binding_sha256 == context.binding_sha256
            and batch.binding == context.binding
            and batch.experiment == context.experiment,
            "回收批次与当前源/绑定身份不一致，拒绝继续",
        )
        batches[reference.batch_id] = batch
    _require(
        index.latest in batches,
        "回收索引的最新批次缺失",
    )
    return LoadedCollection(index=index, batches=batches)


def verify_batch(context: FeedbackContext, batch: FeedbackCollectionBatch) -> None:
    # 回收批次已按自身摘要通过契约校验；这里把它逐项绑定回当前源证据、投影与绑定声明。
    checkpoints = {item.key: item for item in context.evidence.checkpoints}
    record_keys = {
        (
            record.checkpoint.scenario_id,
            record.checkpoint.event_id,
            record.checkpoint.checkpoint_id,
        )
        for record in batch.checkpoints
    }
    _require(
        record_keys == set(checkpoints),
        "回收批次的检查点集合与当前源证据不一致",
    )
    runs = (
        {}
        if context.experiment_receipt is None
        else context.experiment_receipt.get("runs") or {}
    )
    bindings = _metric_binding(context)
    evaluator = context.binding.evaluator
    for record in batch.checkpoints:
        key = (
            record.checkpoint.scenario_id,
            record.checkpoint.event_id,
            record.checkpoint.checkpoint_id,
        )
        checkpoint = checkpoints[key]
        _require(
            record.execution_sha256 == checkpoint.execution_sha256,
            "回收批次的执行摘要与当前源证据不一致",
        )
        row = context.rows_by_key.get(key)
        if row is None:
            _require(
                record.row_id is None
                and record.example_id is None
                and record.cloud_trace_id is None
                and record.projection_sha256 is None
                and not record.attempts,
                "未上传检查点不得携带上传、运行或评分尝试身份",
            )
        else:
            _require(
                record.row_id == row["row_id"] == record.example_id,
                "回收批次的投影行身份与 payload 不一致",
            )
            _require(
                record.projection_sha256 == row["projection_sha256"],
                "回收批次的投影摘要与 payload 不一致",
            )
            _require(
                record.cloud_trace_id == runs.get(row["row_id"]),
                "回收批次的云端运行身份与实验回执不一致",
            )
        approved = context.applicability[key]
        approved_by_metric = {item.metric: item for item in approved.applicability}
        feedback_by_metric = {
            item.metric: item for item in record.feedback_set.feedback
        }
        evidence_by_id: dict[str, list[FeedbackRawEvidence]] = {}
        semantic_by_id: dict[str, set[str]] = {}
        for item in record.evidence:
            evidence_by_id.setdefault(item.score_id, []).append(item)
            semantic_by_id.setdefault(item.score_id, set()).add(
                sha256_text(canonical_json(_semantic_feedback(item.raw)))
            )
        for metric in EXTERNAL_JUDGE_METRICS:
            approval = approved_by_metric[metric]
            feedback = feedback_by_metric[metric]
            if not approval.applicable:
                _require(
                    feedback.status == "not_applicable"
                    and feedback.applicability_reason == approval.reason,
                    f"不适用指标的回收状态与批准适用性不一致: {key}/{metric}",
                )
            else:
                _require(
                    feedback.status != "not_applicable",
                    f"适用指标不得归档为不适用: {key}/{metric}",
                )
            binding = bindings[metric]
            attempt_rows = [
                attempt
                for attempt in record.attempts
                if attempt.feedback.metric == metric
            ]
            # 终态集合只由原始候选导出；保存的尝试必须逐项覆盖它。
            expected_terminal: dict[str, ExternalJudgeFeedback] = {}
            if (
                approval.applicable
                and row is not None
                and record.cloud_trace_id is not None
            ):
                for item in record.evidence:
                    if item.score_name != binding.feedback_key:
                        continue
                    if len(semantic_by_id[item.score_id]) > 1:
                        continue
                    candidate, reason, _ = _normalize_candidate(
                        item.raw, binding
                    )
                    if reason is None and candidate is not None:
                        expected_terminal[item.score_id] = candidate
            conflicts = [
                diagnostic
                for diagnostic in record.diagnostics
                if diagnostic.code == "feedback_conflict"
                and diagnostic.metric == metric
            ]
            _require(
                bool(conflicts) == (len(expected_terminal) > 1),
                f"反馈冲突诊断与原始终态候选集合不一致: {key}/{metric}",
            )
            for attempt in attempt_rows:
                _require(
                    attempt.model.provider == evaluator.provider
                    and attempt.model.evaluator == evaluator.evaluator
                    and attempt.model.model_alias == evaluator.model_alias
                    and attempt.model.config_sha256 == evaluator.config_sha256,
                    f"评分尝试的模型身份与绑定不一致: {key}/{metric}",
                )
                _require(
                    attempt.model.model_version.kind == "unreported",
                    f"评分尝试的模型版本必须为平台未返回口径: {key}/{metric}",
                )
                _require(
                    attempt.definition.model_dump(mode="json")
                    == {
                        "question_id": binding.question_id,
                        "question_sha256": binding.question_sha256,
                        "rubric_id": binding.rubric_id,
                        "rubric_sha256": binding.rubric_sha256,
                    },
                    f"评分尝试的问题或评分标准与绑定不一致: {key}/{metric}",
                )
                cloud = attempt.cloud_score
                _require(
                    cloud is not None,
                    f"评分尝试缺少云端反馈身份: {key}/{metric}",
                )
                matches = evidence_by_id.get(cloud.score_id, [])
                _require(
                    len(matches) == 1,
                    f"评分尝试缺少唯一原始反馈证据: {cloud.score_id}",
                )
                evidence = matches[0]
                _require(
                    evidence.trace_id == cloud.trace_id == attempt.source.trace_id,
                    f"评分尝试与原始反馈的运行身份不一致: {cloud.score_id}",
                )
                _require(
                    record.cloud_trace_id == attempt.source.trace_id,
                    f"评分尝试的运行不属于回收检查点: {cloud.score_id}",
                )
                _require(
                    attempt.source.example_id == record.example_id,
                    f"评分尝试的 Example 身份与回收检查点不一致: {cloud.score_id}",
                )
                _require(
                    evidence.score_name == binding.feedback_key,
                    f"评分尝试关联了错误 feedback key: {cloud.score_id}",
                )
                _require(
                    evidence.raw.get("source") == "EVAL",
                    f"评分尝试关联的反馈不是自动裁判来源: {cloud.score_id}",
                )
                normalized, reason, _ = _normalize_candidate(
                    evidence.raw, binding
                )
                _require(
                    reason is None and normalized is not None,
                    f"原始反馈无法归一化为终态评分: "
                    f"{cloud.score_id}/{reason}",
                )
                _require(
                    normalized.model_dump(mode="json")
                    == attempt.feedback.model_dump(mode="json"),
                    f"评分尝试的归一化结果与原始反馈不一致: "
                    f"{cloud.score_id}",
                )
                _require(
                    row is not None,
                    f"评分尝试缺少投影行身份: {cloud.score_id}",
                )
                expected = _attempt(
                    context, checkpoint, binding, row, evidence.raw, normalized
                )
                _require(
                    expected.model_dump(mode="json")
                    == attempt.model_dump(mode="json"),
                    f"评分尝试的来源或模型身份与原始反馈不一致: "
                    f"{cloud.score_id}",
                )
            attempt_ids = {
                attempt.cloud_score.score_id
                for attempt in attempt_rows
                if attempt.cloud_score is not None
            }
            _require(
                len(attempt_rows) == len(expected_terminal)
                and attempt_ids == set(expected_terminal),
                f"评分尝试集合未完整对应原始终态候选: {key}/{metric}",
            )
            if approval.applicable:
                if attempt_rows:
                    if len(attempt_rows) == 1:
                        _require(
                            feedback.model_dump(mode="json")
                            == attempt_rows[0].feedback.model_dump(mode="json"),
                            f"终态反馈与归一化评分尝试不一致: {key}/{metric}",
                        )
                    else:
                        _require(
                            feedback.status == "pending",
                            f"多终态反馈必须归档为待回收: {key}/{metric}",
                        )
                else:
                    # 无 score 的终态只能来自网页裁判调试 observation 的失败依据。
                    if feedback.status == "failed":
                        _require(
                            not any(
                                item.score_name == binding.feedback_key
                                for item in record.evidence
                            ),
                            f"失败反馈存在同 key 原始 score 证据: {key}/{metric}",
                        )
                        _require(
                            any(
                                diagnostic.code == "cloud_failure"
                                and diagnostic.metric == metric
                                for diagnostic in record.diagnostics
                            ),
                            f"失败反馈缺少网页裁判失败诊断: {key}/{metric}",
                        )
                    else:
                        _require(
                            feedback.status == "pending",
                            f"适用指标缺少原始反馈却归档为非待回收状态: "
                            f"{key}/{metric}",
                        )
    _require(
        (batch.experiment is None) == (context.experiment is None),
        "回收批次的实验身份与当前源证据不一致",
    )


def load_verified_collection(
    source: Path, *, dataset_dir: Path = DEFAULT_VERSION_DIR
) -> VerifiedCollection:
    context = load_feedback_context(source, dataset_dir=dataset_dir)
    loaded = _load_collection(context)
    if loaded is None:
        return VerifiedCollection(context=context, collection=None, latest=None)
    for batch in loaded.batches.values():
        verify_batch(context, batch)
    return VerifiedCollection(
        context=context,
        collection=loaded,
        latest=loaded.batches[loaded.index.latest],
    )


def _persist_batch(
    context: FeedbackContext,
    batch: FeedbackCollectionBatch,
    loaded: LoadedCollection | None,
) -> tuple[Path, bool]:
    run_dir = context.evidence.run_dir
    batch_id = _batch_id(batch.content_sha256)
    path = feedback_batch_path(run_dir, batch_id)
    references = [] if loaded is None else list(loaded.index.batches)
    existing_reference = next(
        (item for item in references if item.batch_id == batch_id), None
    )
    if existing_reference is not None:
        _require(
            existing_reference.content_sha256 == batch.content_sha256,
            "回收索引已包含不同内容的同 ID 批次",
        )
    if loaded is not None and loaded.index.latest == batch_id:
        return path, True
    reused = existing_reference is not None
    if not path.is_file():
        _write_json(path, batch.model_dump(mode="json"))
    try:
        written = FeedbackCollectionBatch.model_validate(_read_json(path))
    except (ValidationError, json.JSONDecodeError) as error:
        raise UploadBlocked("已有批次文件损坏，拒绝覆盖") from error
    _require(
        written.content_sha256 == batch.content_sha256,
        "同 ID 批次文件内容不一致，拒绝覆盖",
    )
    index_path = feedback_index_path(run_dir)
    if index_path.is_file():
        current = _read_json(index_path)
        expected = (
            None
            if loaded is None
            else loaded.index.model_dump(mode="json")
        )
        _require(
            expected is not None
            and canonical_json(current) == canonical_json(expected),
            "回收索引在本次读取期间发生变化，拒绝覆盖",
        )
    if existing_reference is None:
        references.append(
            FeedbackBatchReference(
                batch_id=batch_id,
                content_sha256=batch.content_sha256,
                path=f"{FEEDBACK_BATCH_DIR}/{batch_id}.json",
                read_at=batch.read_at,
                status=batch.status,
            )
        )
    index = FeedbackCollectionIndex(
        schema_version=FEEDBACK_INDEX_SCHEMA,
        run_id=context.evidence.run_id,
        source_sha256=context.evidence.source_sha256,
        payload_sha256=context.source.payload_sha256,
        applicability_sha256=context.binding.applicability_sha256,
        binding_sha256=context.binding_sha256,
        experiment_id=(
            None
            if context.experiment is None
            else context.experiment.experiment_id
        ),
        latest=batch_id,
        batches=references,
    )
    _write_json(index_path, index.model_dump(mode="json"))
    return path, reused


def verify_receipt_runs(
    context: FeedbackContext, runs: Sequence[Any]
) -> dict[str, str]:
    assert context.payload is not None
    assert context.experiment_receipt is not None
    experiment_id = context.experiment_receipt["experiment_id"]
    for run in runs:
        _require(
            str(run.experiment_id) == experiment_id,
            f"远端运行不属于回执实验: {run.id}",
        )
    missing, mapping = _collect_external_runs(context.payload, runs)
    _require(
        not missing,
        f"远端根运行未齐备，拒绝回收: {missing[:3]}",
    )
    _require(
        mapping == context.experiment_receipt["runs"],
        "远端运行身份与实验回执不一致，拒绝回收",
    )
    return mapping


def group_score_feedback(
    run_ids: Sequence[str],
    items: Sequence[Any],
) -> dict[str, list[Any]]:
    # Score 读回已按 experiment_id 过滤；这里再按 subject trace 归组到检查点行。
    feedback: dict[str, list[Any]] = {run_id: [] for run_id in run_ids}
    allowed = set(run_ids)
    for item in items:
        raw = _raw_feedback(item)
        score_id, trace_id, _ = _score_identity(raw)
        _require(
            trace_id in allowed,
            f"返回了回执运行之外的反馈: {score_id}",
        )
        feedback[trace_id].append(item)
    return feedback


def _run_start_time(context: FeedbackContext) -> datetime:
    return datetime.fromisoformat(context.evidence.document["started_at"])


def _experiment_scores(client: Any, experiment_id: str) -> list[Any]:
    items: list[Any] = []
    cursor = None
    while True:
        response = client.api.scores.get_many(
            experiment_id=experiment_id,
            fields="details,subject",
            limit=FEEDBACK_BATCH_SIZE,
            cursor=cursor,
        )
        items.extend(response.data)
        cursor = response.meta.cursor
        if not cursor:
            return items


def _judge_failures(
    client: Any, trace_ids: Sequence[str]
) -> dict[str, list[dict[str, Any]]]:
    # 仅按回执 trace 作用域查询网页裁判调试 observation；Langfuse 文档口径下
    # 调试 trace 独立存在，查询为空时不推断云端失败。
    failures: dict[str, list[dict[str, Any]]] = {}
    for trace_id in sorted(set(trace_ids)):
        observations: list[dict[str, Any]] = []
        cursor = None
        while True:
            response = client.api.observations.get_many(
                environment=WEB_JUDGE_ENVIRONMENT,
                trace_id=trace_id,
                limit=FEEDBACK_BATCH_SIZE,
                cursor=cursor,
            )
            observations.extend(response.data)
            cursor = response.meta.cursor
            if not cursor:
                break
        sanitized = []
        for observation in observations:
            if observation.level != "ERROR":
                continue
            document = _sanitize_outbound(
                _json_safe(observation.model_dump(mode="json"))
            )
            _assert_no_secret(document, "网页裁判调试证据")
            sanitized.append(document)
        failures[trace_id] = sanitized
    return failures


def _feedback_client(timeout_ms: int) -> Langfuse:
    # SDK 5.0.4 读路径基于 Fern/httpx：无自动重试，单次请求失败即抛错；
    # LangSmith Retry(total=0) 的对应实现即该单次请求语义，超时以秒下限 1 秒传入。
    seconds = max(1, int(timeout_ms / 1000))
    client = Langfuse(timeout=seconds)
    _require(client.auth_check(), "Langfuse 凭据校验失败")
    return client


def read_remote_feedback(
    context: FeedbackContext, *, timeout_ms: int
) -> tuple[dict[str, str], dict[str, list[Any]], dict[str, list[dict[str, Any]]]]:
    assert context.payload is not None
    assert context.experiment_receipt is not None
    client = _feedback_client(timeout_ms)
    receipt = context.experiment_receipt
    dataset_id = receipt["dataset_id"]
    experiment_id = receipt["experiment_id"]
    started_at = _run_start_time(context)
    _verify_remote_dataset(client, context.payload, dataset_id)
    _verify_experiment_project(
        client,
        experiment_id,
        receipt["experiment_name"],
        dataset_id,
        _experiment_metadata(context.payload, dataset_id),
        from_start_time=started_at,
    )
    mapping = verify_receipt_runs(
        context, _root_runs(client, experiment_id, from_start_time=started_at)
    )
    items = _experiment_scores(client, experiment_id)
    failures = _judge_failures(client, list(mapping.values()))
    return (
        mapping,
        group_score_feedback(list(mapping.values()), items),
        failures,
    )


def _awaitable(records: Sequence[CheckpointFeedbackRecord]) -> bool:
    for record in records:
        if record.status != "pending":
            continue
        if any(
            diagnostic.code == "awaiting_feedback"
            for diagnostic in record.diagnostics
        ):
            return True
    return False


def _bounded_collect(
    context: FeedbackContext,
    wait_seconds: float,
    history: FeedbackHistory | None,
    *,
    reader: Callable[
        [FeedbackContext, int],
        tuple[
            dict[str, str],
            dict[str, list[Any]],
            dict[str, list[dict[str, Any]]],
        ],
    ] = None,
) -> tuple[FeedbackCollectionBatch, int]:
    read = read_remote_feedback if reader is None else reader
    deadline = time.monotonic() + wait_seconds
    reads = 0
    batch: FeedbackCollectionBatch | None = None
    while True:
        if wait_seconds <= 0:
            timeout_ms = int(FEEDBACK_READ_TIMEOUT_SECONDS * 1000)
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            timeout_ms = max(100, int(remaining * 1000))
        cloud_runs, feedback_by_run, judge_failures = read(
            context, timeout_ms
        )
        reads += 1
        records = normalize_feedback(
            context, cloud_runs, feedback_by_run, history,
            judge_failures=judge_failures,
        )
        batch = build_feedback_batch(context, records)
        if batch.status != "pending" or not _awaitable(records):
            break
        if wait_seconds <= 0:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(FEEDBACK_POLL_INTERVAL_SECONDS, remaining))
    if batch is None:
        raise UploadBlocked("有界回收未执行任何读取")
    return batch, reads


def _counts(batch: FeedbackCollectionBatch) -> dict[str, Any]:
    checkpoint_counts: dict[str, int] = {
        "expected": len(batch.checkpoints),
        "complete": 0,
        "pending": 0,
        "blocked": 0,
        "not_applicable": 0,
    }
    metric_counts: dict[str, int] = {
        "succeeded": 0,
        "failed": 0,
        "pending": 0,
        "not_applicable": 0,
    }
    for record in batch.checkpoints:
        checkpoint_counts[record.status] += 1
        for row in record.feedback_set.feedback:
            metric_counts[row.status] += 1
    return {
        "checkpoints": checkpoint_counts,
        "metrics": metric_counts,
    }


def _result(
    context: FeedbackContext,
    batch: FeedbackCollectionBatch,
    path: Path,
    *,
    network: str,
    reads: int,
    reused: bool,
) -> dict[str, Any]:
    return {
        "status": batch.status,
        "network": network,
        "run_id": context.evidence.run_id,
        "experiment_id": (
            None if context.experiment is None else context.experiment.experiment_id
        ),
        "source_sha256": context.evidence.source_sha256,
        "payload_sha256": context.source.payload_sha256,
        "applicability_sha256": context.binding.applicability_sha256,
        "binding_sha256": context.binding_sha256,
        "source_assurance": SOURCE_ASSURANCE,
        "coverage": batch.source.coverage.model_dump(mode="json"),
        **_counts(batch),
        "blocking": batch.blocking,
        "batch_id": _batch_id(batch.content_sha256),
        "batch_path": str(path),
        "index_path": str(feedback_index_path(context.evidence.run_dir)),
        "read_at": batch.read_at,
        "reads": reads,
        "reused": reused,
    }


def check_feedback(
    source: Path, *, dataset_dir: Path = DEFAULT_VERSION_DIR
) -> dict[str, Any]:
    verified = load_verified_collection(source, dataset_dir=dataset_dir)
    context = verified.context
    loaded = verified.collection
    if loaded is None:
        return {
            "status": "pending",
            "network": "not_called",
            "reason": "collection_absent",
            "run_id": context.evidence.run_id,
            "experiment_id": (
                None
                if context.experiment is None
                else context.experiment.experiment_id
            ),
            "source_sha256": context.evidence.source_sha256,
            "payload_sha256": context.source.payload_sha256,
            "applicability_sha256": context.binding.applicability_sha256,
            "binding_sha256": context.binding_sha256,
            "source_assurance": SOURCE_ASSURANCE,
            "coverage": context.source.coverage.model_dump(mode="json"),
            "checkpoints": {
                "expected": len(context.evidence.checkpoints),
                "complete": 0,
                "pending": 0,
                "blocked": 0,
                "not_applicable": 0,
            },
            "metrics": {
                "succeeded": 0,
                "failed": 0,
                "pending": 0,
                "not_applicable": 0,
            },
        }
    latest = verified.latest
    assert latest is not None
    return _result(
        context,
        latest,
        feedback_batch_path(
            context.evidence.run_dir, loaded.index.latest
        ),
        network="not_called",
        reads=0,
        reused=True,
    )


def collect_feedback(
    source: Path,
    *,
    dataset_dir: Path = DEFAULT_VERSION_DIR,
    wait_seconds: float = 0.0,
) -> dict[str, Any]:
    _require(
        math.isfinite(wait_seconds) and wait_seconds >= 0,
        "等待时长必须为有限非负数",
    )
    context = load_feedback_context(source, dataset_dir=dataset_dir)
    loaded = _load_collection(context)
    history = (
        None
        if loaded is None
        else _history_from_batches(context, list(loaded.batches.values()))
    )
    if context.experiment is None:
        records = normalize_feedback(context, {}, {}, history)
        batch = build_feedback_batch(context, records)
        path, reused = _persist_batch(context, batch, loaded)
        return _result(
            context,
            batch,
            path,
            network="not_called",
            reads=0,
            reused=reused,
        )
    load_dotenv(BACKEND / ".env", override=False)
    _require(
        bool((os.environ.get("LANGFUSE_PUBLIC_KEY") or "").strip())
        and bool((os.environ.get("LANGFUSE_SECRET_KEY") or "").strip()),
        "请在 backend/.env 配置 LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY "
        "后再执行云端回收",
    )
    batch, reads = _bounded_collect(context, wait_seconds, history)
    path, reused = _persist_batch(context, batch, loaded)
    return _result(
        context,
        batch,
        path,
        network="called",
        reads=reads,
        reused=reused,
    )


def _wait_seconds(value: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("等待时长必须为数字") from error
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("等待时长必须为有限非负数")
    return number


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Langfuse 外部裁判反馈回收；check 不访问网络"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("check", "离线校验源绑定、绑定文件与已有回收产物"),
        ("collect", "读取指定实验的 Langfuse Score 并保存版本化证据"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--source", type=Path, required=True)
        command.add_argument(
            "--dataset", type=Path, default=DEFAULT_VERSION_DIR
        )
        if name == "collect":
            command.add_argument(
                "--wait-seconds",
                type=_wait_seconds,
                default=0.0,
                help="有界等待秒数；默认单次读取",
            )
    return parser.parse_args(argv)


def _exit_code(status: str) -> int:
    return {"complete": 0, "pending": 2, "blocked": 3}[status]


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = (
            check_feedback(args.source, dataset_dir=args.dataset)
            if args.command == "check"
            else collect_feedback(
                args.source,
                dataset_dir=args.dataset,
                wait_seconds=args.wait_seconds,
            )
        )
    except UploadBlocked as error:
        print(
            json.dumps(
                {"status": "blocked", "error": str(error)},
                ensure_ascii=False,
            )
        )
        return 3
    print(json.dumps(result, ensure_ascii=False))
    return _exit_code(result["status"])


if __name__ == "__main__":
    raise SystemExit(main())
