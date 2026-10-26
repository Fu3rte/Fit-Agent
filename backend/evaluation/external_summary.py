import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any, Literal, Self, Sequence

from pydantic import Field, ValidationError, model_validator

from evaluation.contracts import (
    EXTERNAL_JUDGE_METRICS,
    DatasetIdentity,
    EvaluationContract,
    EvidenceLink,
    ExternalJudgeApplicability,
    ExternalJudgeFeedbackSet,
    ExternalJudgeLangfuseScoringAttempt,
    ExternalJudgeMetric,
    ExternalJudgeScoringAttempt,
    NonEmptyText,
    Sha256,
)
from evaluation.dataset import BACKEND, EvaluationDataset
from evaluation.decision_feedback import (
    BLOCKING_CODES,
    FEEDBACK_BATCH_DIR,
    FEEDBACK_INDEX_SCHEMA,
    SOURCE_ASSURANCE,
    CheckpointFeedbackRecord,
    FeedbackCollectionBatch,
    FeedbackContext,
    FeedbackCoverage,
    FeedbackExperimentSummary,
    LoadedCollection,
    UploadBlocked,
    _batch_content_sha256,
    _batch_id,
    feedback_dir,
    feedback_index_path,
    load_verified_collection,
    question_digest,
    rubric_digest,
)
from evaluation.generation_upload import (
    ExternalRunEvidence,
    ExternalScenarioEvidence,
    _coverage_ledger,
    external_dir,
    load_external_applicability,
    load_external_run,
)
from evaluation.langfuse_scenarios import (
    EXTERNAL_FEEDBACK_VERSION,
    EXTERNAL_PROTOCOL_VERSION,
    MAX_CONCURRENCY,
    NUM_REPETITIONS,
    SCORING_PROTOCOLS,
    ScenarioSDKError,
    assertion_statistics,
    feedback_version,
    run_scoring_mode,
)
from evaluation.langfuse_scenarios import (
    SCHEMA as LANGFUSE_SCENARIO_SCHEMA,
)
from evaluation.langsmith_scenarios import (
    SCHEMA as LANGSMITH_SCENARIO_SCHEMA,
)
from evaluation.scenario_evidence import canonical_json, sha256_text
from evaluation.scenarios import (
    EXTERNAL_PROTOCOL_VERSION as LEGACY_EXTERNAL_PROTOCOL_VERSION,
)
from evaluation.scenarios import (
    SCORING_PROTOCOLS as LEGACY_SCORING_PROTOCOLS,
)
from evaluation.scenarios import (
    code_identity as scenario_code_identity,
)
from evaluation.scenarios import (
    scenario_plan,
)
from evaluation.summary import (
    _answer_model_call,
    _read_request_file,
    _validate_context_projection,
)

EXTERNAL_SUMMARY_SCHEMA = "fit-agent-external-judge-summary-langfuse-1"
HEX64_LENGTH = 64
LANGFUSE_PLATFORM = "langfuse"
LANGSMITH_PLATFORM = "langsmith"
PLATFORM_LABELS = {LANGFUSE_PLATFORM: "Langfuse", LANGSMITH_PLATFORM: "LangSmith"}
SCENARIO_PLATFORMS = {
    LANGFUSE_SCENARIO_SCHEMA: LANGFUSE_PLATFORM,
    LANGSMITH_SCENARIO_SCHEMA: LANGSMITH_PLATFORM,
}
EXTERNAL_PROTOCOLS = {
    LANGFUSE_PLATFORM: EXTERNAL_PROTOCOL_VERSION,
    LANGSMITH_PLATFORM: LEGACY_EXTERNAL_PROTOCOL_VERSION,
}
HISTORICAL_COLLECTION_SCHEMA = "fit-agent-external-judge-feedback-collection-1"
HISTORICAL_INDEX_SCHEMA = "fit-agent-external-judge-feedback-index-1"
HISTORICAL_BINDING_SCHEMA = "fit-agent-external-judge-feedback-binding-1"
HISTORICAL_EXCLUSION_REASONS = Literal[
    "unmapped_key",
    "source_not_decision_model",
    "source_gap",
    "invalid_score",
    "not_applicable_metric",
    "malformed_cloud_feedback",
]
HISTORICAL_DIAGNOSTIC_CODES = Literal[
    "awaiting_feedback",
    "feedback_without_terminal_score",
    "cloud_failure",
    "checkpoint_missing_upload",
    "invalid_score",
    "source_gap",
    "feedback_conflict",
    "feedback_content_drift",
    "feedback_history_conflict",
    "malformed_cloud_feedback",
    "unexpected_not_applicable_feedback",
    "missing_cloud_run",
    "experiment_absent",
]
METRIC_DEFINITIONS = {
    "evidence_supported_v1": {
        "description": "回答事实是否得到本次请求实际证据上下文的支持。",
        "scale": "0..1 Noul 概率，不代表正确声明条目的比例。",
    },
    "reference_requirements_met_v1": {
        "description": "回答是否满足参考答案中的必须内容与禁止声明要求。",
        "scale": "0..1 Noul 概率，不代表正确声明条目的比例。",
    },
}
SOURCE_ASSURANCE_LIMITS = (
    "来源保证固定为 declared_binding：校验显式绑定、feedback key 映射、"
    "来源类型与 feedback_source.metadata.typesafe 对象。",
    "不要求 SDK Evaluator 注册表唯一匹配，不宣称服务端已唯一证明 Evaluator 身份。",
    "服务未返回具体版本时只记录 unreported，不推断可复现的模型版本。",
    "Noul 分数是 0..1 概率；局部或定向均值仅用于诊断，不构成正式发布成绩。",
)
AGGREGATION_RULES = {
    "within_scenario": "场景内为适用检查点成功分数的算术平均。",
    "across_scenarios": "场景间为有数值场景的等权宏平均。",
    "mean_including_partial": "分母为至少一项指标成功的场景。",
    "mean_complete_only": (
        "分母为该指标全部适用检查点成功且至少有一项成功数值的场景。"
    ),
    "not_applicable": "不适用项保留原因，不进入数值分母。",
    "failed_missing_pending_blocked": "失败、缺失、待回收与校验阻断项不填 0，不进入数值分母。",
    "zero": "真实 0 分属于成功分数，进入均值。",
}
CHECKPOINT_DIMENSION = (
    "检查点维度来自任务 3 覆盖账本：预期、可上传、缺失、不适用、待审阅。"
)
METRIC_DIMENSION = (
    "指标维度来自任务 4 回收批次：每项指标按已批准适用性构造 "
    "(scenario_id, event_id, checkpoint_id, metric) 键。"
)
DIMENSION_IDENTITY = {
    "checkpoint_classification": CHECKPOINT_DIMENSION,
    "metric_status": METRIC_DIMENSION,
    "metric_total": "指标 expected = applicable + not_applicable。",
    "applicable_total": (
        "指标 applicable = succeeded + failed + pending + missing + blocked + unapproved。"
    ),
    "note": (
        "检查点分类与指标状态是不同维度；blocked 诊断独立记录，"
        "不与 metric.status 互斥相加，也不为缺失项重复计数。"
    ),
}


class ExternalSummaryError(RuntimeError):
    pass


@dataclass
class MetricView:
    metric: str
    applicable: bool | None
    approved: bool
    status: str
    score: float | None = None
    detail: str | None = None
    feedback_status: str | None = None
    attempt_ids: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    cloud_feedback_ids: list[str] = field(default_factory=list)
    diagnostic_codes: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)


@dataclass
class CheckpointView:
    scenario_id: str
    event_id: str
    checkpoint_id: str
    execution_sha256: str | None
    classification: str
    evidence_missing: list[str] = field(default_factory=list)
    readiness: dict[str, Any] | None = None
    record_status: str | None = None
    entries: dict[str, MetricView] = field(default_factory=dict)
    blocked: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.scenario_id, self.event_id, self.checkpoint_id)


@dataclass
class ScenarioView:
    scenario_id: str
    state: str
    execution: dict[str, Any]
    assertions: dict[str, Any]
    checkpoints: dict[tuple[str, str, str], CheckpointView] = field(
        default_factory=dict
    )
    issues: list[str] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)


@dataclass
class ExternalScenarioSource:
    path: Path | None = None
    root: Path | None = None
    dataset: EvaluationDataset | None = None
    document: dict[str, Any] = field(default_factory=dict)
    platform: str = LANGFUSE_PLATFORM
    run_id: str | None = None
    purpose: str | None = None
    scoring_mode: str | None = None
    protocol_version: str | None = None
    feedback_version: int | None = None
    recorded_status: str | None = None
    evidence: ExternalRunEvidence | None = None
    applicability: (
        dict[tuple[str, str, str], Any] | None
    ) = None
    ledger: dict[str, Any] | None = None
    context: FeedbackContext | None = None
    collection: LoadedCollection | None = None
    latest: FeedbackCollectionBatch | None = None
    history: "HistoricalCollection | None" = None
    batch_id: str | None = None
    source_assurance: str = SOURCE_ASSURANCE
    expected_scenario_ids: list[str] = field(default_factory=list)
    scenarios: dict[str, ScenarioView] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return bool(self.document)

    @property
    def expected_checkpoints(self) -> set[tuple[str, str, str]]:
        return {
            checkpoint.key
            for scenario in self.scenarios.values()
            for checkpoint in scenario.checkpoints.values()
        }


def _add(items: list[str], message: str) -> None:
    if message not in items:
        items.append(message)


def _display_path(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return Path(path).resolve().relative_to(BACKEND.parent.resolve()).as_posix()
    except ValueError:
        return str(path)


def _read_document(source: Path) -> tuple[Path, dict[str, Any]]:
    root = Path(source)
    if not root.is_dir():
        raise ExternalSummaryError(
            "外部裁判汇总仅支持 SDK 目录布局 "
            "(<run_id>/sdk-run.json)，不推断旧本地运行路径"
        )
    path = root / "sdk-run.json"
    if not path.is_file():
        raise ExternalSummaryError(
            f"外部运行目录缺少 sdk-run.json: {_display_path(root)}"
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:
        raise ExternalSummaryError(
            f"sdk-run.json 无法解析: {type(error).__name__}: {error}"
        ) from error
    if not isinstance(document, dict):
        raise ExternalSummaryError("sdk-run.json 必须为 JSON 对象")
    return root, document


HISTORICAL_BLOCKING_CODES = BLOCKING_CODES | {"missing_cloud_run"}


def source_platform(document: dict[str, Any]) -> str:
    # 布局分发包判定：sdk-run.json 的 schema 字符串唯一决定平台，不猜模式。
    platform = SCENARIO_PLATFORMS.get(document.get("schema"))
    if platform is None:
        raise ExternalSummaryError(
            "场景运行 Schema 不受支持: " f"{document.get('schema')!r}"
        )
    return platform


def _platform_protocols(platform: str) -> dict[str, str]:
    if platform == LANGFUSE_PLATFORM:
        return SCORING_PROTOCOLS
    return LEGACY_SCORING_PROTOCOLS


def protocol(document: dict[str, Any]) -> tuple[int, str]:
    try:
        version = feedback_version(document)
        mode = run_scoring_mode(document)
    except ScenarioSDKError as error:
        raise ExternalSummaryError(str(error)) from error
    recorded = document.get("protocol_version")
    if recorded is not None and recorded != _platform_protocols(
        source_platform(document)
    )[mode]:
        raise ExternalSummaryError(
            f"评分模式与协议版本不一致: {mode!r}/{recorded!r}"
        )
    return version, mode


def _require_external(document: dict[str, Any]) -> str:
    platform = source_platform(document)
    version, mode = protocol(document)
    if mode != "external" or version != EXTERNAL_FEEDBACK_VERSION:
        raise ExternalSummaryError(
            "外部汇总拒绝非 external 评分模式或非外部反馈版本"
        )
    expected_protocol = EXTERNAL_PROTOCOLS[platform]
    if document.get("protocol_version") != expected_protocol:
        raise ExternalSummaryError(
            "外部运行必须显式声明协议版本 "
            f"{expected_protocol}"
        )
    return platform


def _scope_ids(document: dict[str, Any]) -> list[str] | None:
    scope = document.get("sample_scope")
    if not isinstance(scope, dict):
        return None
    ids = scope.get("scenario_ids")
    if not isinstance(ids, list) or not ids:
        return None
    if any(not isinstance(item, str) or not item for item in ids):
        return None
    if len(set(ids)) != len(ids):
        return None
    return list(ids)


def _planned_checkpoint_events(scenario: dict[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for key in ("reference", "rubric", "assertions", "metric_applicability"):
        for row in scenario[key]:
            checkpoint_id = row["checkpoint_id"]
            event_id = row["event_id"]
            if mapping.setdefault(checkpoint_id, event_id) != event_id:
                raise ExternalSummaryError(
                    f"冻结场景检查点绑定了多个事件: {checkpoint_id}"
                )
    return mapping


def _approved_by_metric(
    approved: ExternalJudgeApplicability | None,
) -> dict[str, ExternalJudgeApplicability]:
    if approved is None:
        return {}
    return {item.metric: item for item in approved.applicability}


def _record_blocking_codes(record: Any) -> set[str]:
    # 历史批次的阻断码集合保持 LangSmith 期口径（missing_cloud_run）。
    if isinstance(record, CheckpointFeedbackRecord):
        return set(BLOCKING_CODES)
    return set(HISTORICAL_BLOCKING_CODES)


def _attempt_cloud_identity(attempt: Any) -> str | None:
    # 按 attempt 契约类型分发云端身份：Langfuse 为 Score id，历史批次为 feedback id。
    if isinstance(attempt, ExternalJudgeLangfuseScoringAttempt):
        return (
            None if attempt.cloud_score is None else attempt.cloud_score.score_id
        )
    if isinstance(attempt, ExternalJudgeScoringAttempt):
        return (
            None
            if attempt.cloud_feedback is None
            else attempt.cloud_feedback.feedback_id
        )
    raise ExternalSummaryError(f"未知评分尝试契约: {type(attempt).__name__}")


def _evidence_key(item: Any) -> str | None:
    key = getattr(item, "score_name", None)
    if key is None:
        key = getattr(item, "key", None)
    return key


def _record_diagnostics(record: Any, metric: str) -> list[Any]:
    codes = _record_blocking_codes(record)
    return [
        diagnostic
        for diagnostic in record.diagnostics
        if diagnostic.code in codes
        and diagnostic.metric in (None, metric)
    ]


def _metric_view(
    *,
    metric: str,
    checkpoint: CheckpointView,
    approved: ExternalJudgeApplicability | None,
    # record 为 Langfuse CheckpointFeedbackRecord 或历史 _HistoryRecord。
    record: Any,
    collection_present: bool,
    feedback_keys: dict[str, str],
) -> MetricView:
    approval = _approved_by_metric(approved).get(metric)
    if approval is None:
        return MetricView(
            metric=metric,
            applicable=None,
            approved=False,
            status="unapproved",
            detail="缺少已批准的适用性条目",
        )
    if not approval.applicable:
        return MetricView(
            metric=metric,
            applicable=False,
            approved=True,
            status="not_applicable",
            detail=approval.reason,
        )
    if checkpoint.execution_sha256 is None:
        return MetricView(
            metric=metric,
            applicable=True,
            approved=True,
            status="blocked",
            detail="检查点缺少可核验的执行证据",
        )
    if record is None:
        if not collection_present:
            if checkpoint.classification == "missing":
                return MetricView(
                    metric=metric,
                    applicable=True,
                    approved=True,
                    status="missing",
                    detail="检查点未进入上传投影: "
                    + (", ".join(checkpoint.evidence_missing) or "证据缺失"),
                )
            if checkpoint.classification == "pending_review":
                return MetricView(
                    metric=metric,
                    applicable=True,
                    approved=True,
                    status="unapproved",
                    detail="检查点尚未完成适用性批准",
                )
            return MetricView(
                metric=metric,
                applicable=True,
                approved=True,
                status="pending",
                detail="尚未回收云端反馈",
            )
        return MetricView(
            metric=metric,
            applicable=True,
            approved=True,
            status="blocked",
            detail="回收批次缺少该检查点记录",
        )
    feedback = next(
        item
        for item in record.feedback_set.feedback
        if item.metric == metric
    )
    attempts = [
        attempt
        for attempt in record.attempts
        if attempt.feedback.metric == metric
    ]
    wanted_key = feedback_keys.get(metric)
    evidence_ids = [
        item.evidence_id
        for item in record.evidence
        if wanted_key is not None and _evidence_key(item) == wanted_key
    ]
    evidence_ids.extend(
        item.evidence_id
        for item in record.exclusions
        if item.metric == metric
    )
    diagnostics = _record_diagnostics(record, metric)
    blocking = [diagnostic.detail for diagnostic in diagnostics]
    status = feedback.status
    detail: str | None = None
    if feedback.status == "failed":
        # Langfuse 网页裁判失败不产生 score，终态行允许无评分尝试。
        detail = feedback.failure_evidence
    elif feedback.status == "not_applicable":
        detail = feedback.applicability_reason
    elif feedback.status == "pending":
        if any(
            diagnostic.code == "checkpoint_missing_upload"
            for diagnostic in record.diagnostics
        ):
            status = "missing"
        detail = next(
            (
                diagnostic.detail
                for diagnostic in record.diagnostics
                if diagnostic.metric in (None, metric)
            ),
            "等待云端反馈",
        )
    if blocking:
        status = "blocked"
        detail = "; ".join(blocking)
    return MetricView(
        metric=metric,
        applicable=True,
        approved=True,
        status=status,
        score=(
            feedback.score
            if status == "succeeded" and feedback.score is not None
            else None
        ),
        detail=detail,
        feedback_status=feedback.status,
        attempt_ids=[attempt.attempt_id for attempt in attempts],
        evidence_ids=sorted(set(evidence_ids)),
        cloud_feedback_ids=sorted(
            {
                identity
                for identity in (
                    _attempt_cloud_identity(attempt) for attempt in attempts
                )
                if identity is not None
            }
        ),
        diagnostic_codes=sorted(
            {diagnostic.code for diagnostic in diagnostics}
        ),
        blocked=sorted(set(blocking)),
    )


def _scenario_assertions(
    scenario: dict[str, Any], record: ExternalScenarioEvidence | None
) -> dict[str, Any]:
    expected = len(scenario["assertions"])
    if record is None:
        return {
            "expected": expected,
            "observed": 0,
            "passed": 0,
            "failed": 0,
            "binding_errors": 0,
            "runner_errors": 0,
            "missing_checkpoints": sorted(
                {
                    f"{row['event_id']}/{row['checkpoint_id']}"
                    for row in scenario["assertions"]
                }
            ),
            "status": "missing",
            "complete": False,
        }
    statistics = assertion_statistics(record.record)
    observed = statistics["passed"] + statistics["failed"]
    missing = sorted(
        set(record.record.missing_assertion_checkpoint_ids)
    )
    complete = (
        observed == expected
        and statistics["binding_errors"] == 0
        and statistics["runner_errors"] == 0
        and not missing
    )
    return {
        "expected": expected,
        "observed": observed,
        **statistics,
        "missing_checkpoints": missing,
        "status": record.record.assertion_status,
        "complete": complete,
    }


def _scenario_execution(
    scenario: dict[str, Any], record: ExternalScenarioEvidence | None
) -> dict[str, Any]:
    plan = scenario_plan(scenario)
    if record is None:
        return {
            "status": "missing",
            "error": None,
            "expected_events": list(plan.event_ids),
            "observed_events": [],
            "expected_checkpoints": list(plan.checkpoint_ids),
            "observed_checkpoints": [],
            "generation_scoring_status": None,
            "incomplete": [],
            "context_gaps": [],
            "sample_status": None,
            "run_error": None,
        }
    execution = record.execution
    gaps = [
        gap.model_dump(mode="json") for gap in execution.context_gaps
    ]
    for event in execution.events:
        gaps.extend(
            gap.model_dump(mode="json") for gap in event.context_gaps
        )
    deduped = {
        canonical_json(gap): gap for gap in gaps
    }
    return {
        "status": execution.status,
        "error": execution.error,
        "expected_events": list(execution.expected_event_ids),
        "observed_events": [event.event_id for event in execution.events],
        "expected_checkpoints": list(execution.expected_checkpoint_ids),
        "observed_checkpoints": sorted(
            {
                entry.checkpoint_id
                for entry in execution.generation_checkpoints
            }
            | {
                entry.checkpoint_id
                for entry in execution.assertion_checkpoints
            }
        ),
        "generation_scoring_status": record.record.generation_scoring_status,
        "incomplete": list(record.record.incomplete),
        "context_gaps": list(deduped.values()),
        "sample_status": record.sample.get("status"),
        "run_error": record.sample.get("run_error"),
    }


def _build_scenario_view(
    *,
    scenario: dict[str, Any],
    record: ExternalScenarioEvidence | None,
    evidence_keys: Sequence[tuple[str, str, str]],
    evidence_execution: dict[tuple[str, str, str], str],
    approved: dict[tuple[str, str, str], ExternalJudgeApplicability] | None,
    ledger_entries: dict[tuple[str, str, str], dict[str, Any]],
    # records 的值为 Langfuse CheckpointFeedbackRecord 或历史 _HistoryRecord。
    records: dict[tuple[str, str, str], Any],
    collection_present: bool,
    feedback_keys: dict[str, str],
    source_issues: list[str],
) -> ScenarioView:
    scenario_id = scenario["scenario_id"]
    execution = _scenario_execution(scenario, record)
    assertions = _scenario_assertions(scenario, record)
    view = ScenarioView(
        scenario_id=scenario_id,
        state=execution["status"],
        execution=execution,
        assertions=assertions,
    )
    if record is None:
        _add(view.blocking, f"{scenario_id}: 缺少场景执行证据")
    else:
        if execution["status"] != "completed":
            _add(
                view.blocking,
                f"{scenario_id}: 执行状态为 {execution['status']}: "
                f"{execution['error'] or '无错误依据'}",
            )
        if assertions["status"] != "passed":
            _add(
                view.blocking,
                f"{scenario_id}: 确定性断言状态为 {assertions['status']}",
            )
        if assertions["missing_checkpoints"]:
            _add(
                view.blocking,
                f"{scenario_id}: 确定性断言缺失 "
                + ", ".join(assertions["missing_checkpoints"]),
            )
        if execution["run_error"]:
            _add(view.blocking, f"{scenario_id}: 运行错误 {execution['run_error']}")
        if execution["sample_status"] != "complete":
            _add(
                view.blocking,
                f"{scenario_id}: 样本状态为 {execution['sample_status']}",
            )
        if execution["generation_scoring_status"] == "incomplete":
            _add(view.blocking, f"{scenario_id}: 生成证据不完整")
        ambiguous = [
            gap
            for gap in execution["context_gaps"]
            if gap.get("ambiguous")
        ]
        if ambiguous:
            _add(
                view.blocking,
                f"{scenario_id}: 存在 {len(ambiguous)} 项无法绑定的上下文",
            )
        for item in execution["incomplete"]:
            _add(view.blocking, f"{scenario_id}: {item}")
    plan = scenario_plan(scenario)
    event_by_checkpoint = _planned_checkpoint_events(scenario)
    planned = set(plan.generation_metrics)
    keys = {
        (scenario_id, event_by_checkpoint.get(checkpoint_id, ""), checkpoint_id)
        for checkpoint_id in planned
    }
    keys |= set(evidence_keys)
    if approved is not None:
        keys |= {
            key for key in approved
            if key[0] == scenario_id
        }
    for key in sorted(keys):
        _, event_id, checkpoint_id = key
        checkpoint_record = records.get(key)
        approved_entry = None if approved is None else approved.get(key)
        ledger_entry = ledger_entries.get(key)
        classification = (
            "unknown"
            if ledger_entry is None
            else ledger_entry["classification"]
        )
        checkpoint_view = CheckpointView(
            scenario_id=scenario_id,
            event_id=event_id,
            checkpoint_id=checkpoint_id,
            execution_sha256=(
                checkpoint_record.execution_sha256
                if checkpoint_record is not None
                else evidence_execution.get(key)
            ),
            classification=classification,
            evidence_missing=(
                [] if ledger_entry is None else ledger_entry["evidence_missing"]
            ),
            readiness=(
                None if ledger_entry is None else ledger_entry["readiness"]
            ),
            record_status=(
                None if checkpoint_record is None else checkpoint_record.status
            ),
        )
        for metric in EXTERNAL_JUDGE_METRICS:
            checkpoint_view.entries[metric] = _metric_view(
                metric=metric,
                checkpoint=checkpoint_view,
                approved=approved_entry,
                record=checkpoint_record,
                collection_present=collection_present,
                feedback_keys=feedback_keys,
            )
        for entry in checkpoint_view.entries.values():
            checkpoint_view.blocked.extend(entry.blocked)
        if (
            checkpoint_record is not None
            and checkpoint_record.status == "blocked"
            and not checkpoint_view.blocked
        ):
            checkpoint_view.blocked.extend(
                diagnostic.detail
                for diagnostic in checkpoint_record.diagnostics
                if diagnostic.code in _record_blocking_codes(checkpoint_record)
            )
        view.checkpoints[key] = checkpoint_view
    for checkpoint in view.checkpoints.values():
        if checkpoint.blocked and not any(
            entry.status == "blocked"
            for entry in checkpoint.entries.values()
        ):
            _add(
                view.blocking,
                f"{scenario_id}/{checkpoint.checkpoint_id}: "
                + "; ".join(checkpoint.blocked),
            )
        for entry in checkpoint.entries.values():
            if entry.status == "blocked":
                _add(
                    view.blocking,
                    f"{scenario_id}/{checkpoint.checkpoint_id}/"
                    f"{entry.metric}: {entry.detail or '校验阻断'}",
                )
            elif entry.status == "unapproved":
                _add(
                    view.blocking,
                    f"{scenario_id}/{checkpoint.checkpoint_id}/"
                    f"{entry.metric}: {entry.detail}",
                )
    for issue in source_issues:
        _add(view.issues, issue)
    return view


def _declared_generation_model(document: dict[str, Any]) -> dict[str, Any]:
    generation = document.get("generation")
    value = generation.get("model") if isinstance(generation, dict) else None
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _request_issues(
    evidence: ExternalRunEvidence, document: dict[str, Any]
) -> list[str]:
    # 回答消息 -> 真实模型调用 -> 请求全文；上下文按生产映射逐项重放。
    issues: list[str] = []
    declared = _declared_generation_model(document)
    for scenario in evidence.scenarios:
        scenario_id = scenario.scenario["scenario_id"]
        events = {event.event_id: event for event in scenario.execution.events}
        calls_root = scenario.root / "model-calls"
        for entry in scenario.execution.generation_checkpoints:
            if entry.checkpoint is None:
                continue
            checkpoint_id = entry.checkpoint_id
            event = events.get(entry.event_id)
            if event is None:
                _add(
                    issues,
                    f"{scenario_id}:生成检查点缺少事件捕获 {checkpoint_id}",
                )
                continue
            if (
                event.answer_text is None
                or event.answer_text != entry.checkpoint.answer.visible_text
            ):
                _add(
                    issues,
                    f"{scenario_id}:检查点回答与事件捕获不一致 {checkpoint_id}",
                )
            if (
                event.answer_structured
                != entry.checkpoint.answer.structured_snapshot
            ):
                _add(
                    issues,
                    f"{scenario_id}:检查点结构化回答与事件捕获不一致 "
                    f"{checkpoint_id}",
                )
            call = _answer_model_call(event)
            if call is None:
                _add(
                    issues,
                    f"{scenario_id}:检查点回答未关联真实模型调用 {checkpoint_id}",
                )
                continue
            request = _read_request_file(
                calls_root, call, issues, scenario_id, checkpoint_id
            )
            if request is None:
                continue
            if (call.api, call.provider, call.model) != (
                declared.get("api"),
                declared.get("provider"),
                declared.get("model"),
            ):
                _add(
                    issues,
                    f"{scenario_id}:检查点模型调用与生成身份不一致 "
                    f"{checkpoint_id}",
                )
            if (
                entry.request_boundary is not None
                and entry.request_boundary.api != call.api
            ):
                _add(
                    issues,
                    f"{scenario_id}:请求节点身份与模型调用不一致 "
                    f"{checkpoint_id}",
                )
            _validate_context_projection(
                entry.checkpoint,
                entry.request_boundary,
                event,
                request,
                issues,
                scenario_id,
                checkpoint_id,
            )
    return issues


def _metadata_issues(
    document: dict[str, Any],
    dataset: EvaluationDataset,
    purpose: str,
    platform: str,
) -> list[str]:
    issues: list[str] = []
    label = PLATFORM_LABELS[platform]
    recorded_dataset = document.get("dataset")
    if not isinstance(recorded_dataset, dict):
        _add(issues, "运行摘要缺少数据身份")
    else:
        actual = dataset.identity()
        for key in ("version", "manifest_sha256", "content_sha256", "corpus_sha256"):
            if recorded_dataset.get(key) != actual[key]:
                _add(issues, f"数据身份不一致: {key}")
    code = document.get("code_identity")
    try:
        current = scenario_code_identity()
    except Exception as error:
        current = None
        _add(
            issues,
            f"当前场景代码身份无法生成: {type(error).__name__}: {error}",
        )
    if not isinstance(code, dict):
        _add(issues, "运行摘要缺少场景代码身份")
    elif current is not None:
        for key in ("commit", "dirty", "lock_sha256", "source_sha256"):
            if code.get(key) != current.get(key):
                _add(issues, f"场景代码身份与当前工作区不一致: {key}")
    generation = document.get("generation")
    model = _declared_generation_model(document)
    if not isinstance(model, dict) or any(
        not isinstance(model.get(key), str) or not model.get(key)
        for key in ("api", "provider", "model")
    ):
        _add(issues, "生成模型身份缺失或非法")
    if not isinstance(generation, dict) or not generation.get("prompt_version"):
        _add(issues, "生成提示身份缺失")
    index = document.get("index")
    if (
        not isinstance(index, dict)
        or not index.get("collection")
        or not isinstance(index.get("manifest_sha256"), str)
        or len(index.get("manifest_sha256", "")) != HEX64_LENGTH
    ):
        _add(issues, "场景物理索引身份缺失或非法")
    experiment = document.get("experiment")
    if not isinstance(experiment, dict) or not experiment.get("id"):
        _add(issues, f"场景来源缺少 {label} 实验 ID")
    if document.get("max_concurrency") != MAX_CONCURRENCY:
        _add(issues, "场景并发配置与冻结上限不一致")
    if document.get("num_repetitions") != NUM_REPETITIONS:
        _add(issues, "场景重复次数与冻结配置不一致")
    if purpose == "formal":
        concurrency = document.get("concurrency")
        if not isinstance(concurrency, dict):
            _add(issues, "场景并发证据缺失")
        else:
            maximum = concurrency.get("max_in_flight")
            if maximum is None or maximum > MAX_CONCURRENCY:
                _add(issues, "场景并发证据非法")
            if not concurrency.get("overlap_pairs"):
                _add(issues, "场景未观察到并发重叠")
    return issues


def _scenario_record_issues(
    document: dict[str, Any],
    evidence: ExternalRunEvidence | None,
    expected_ids: Sequence[str],
) -> list[str]:
    records = document.get("scenarios")
    if records is None:
        return []
    if not isinstance(records, list):
        return ["运行摘要 scenarios 记录非法"]
    by_id: dict[str, dict[str, Any]] = {}
    issues: list[str] = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(
            record.get("scenario_id"), str
        ):
            _add(issues, "运行摘要包含非法场景记录")
            continue
        scenario_id = record["scenario_id"]
        if scenario_id in by_id:
            _add(issues, f"运行摘要场景记录重复: {scenario_id}")
        by_id[scenario_id] = record
    if set(by_id) != set(expected_ids):
        _add(issues, "运行摘要场景记录集合与批准场景集不一致")
    samples = {
        sample.get("scenario_id"): sample
        for sample in document.get("samples") or []
        if isinstance(sample, dict)
    }
    records_by_scenario = (
        {}
        if evidence is None
        else {
            row.scenario["scenario_id"]: row for row in evidence.scenarios
        }
    )
    for scenario_id, record in by_id.items():
        sample = samples.get(scenario_id)
        if sample is None:
            _add(issues, f"场景记录缺少运行样本: {scenario_id}")
            continue
        for key in (
            "status",
            "execution_status",
            "execution_sha256",
            "scoring_relative",
            "scoring_sha256",
            "feedback_version",
            "scoring_mode",
            "incomplete",
            "sdk_run_id",
            "run_error",
        ):
            if record.get(key) != sample.get(key):
                _add(issues, f"场景记录与运行样本不一致: {scenario_id}/{key}")
        row = records_by_scenario.get(scenario_id)
        if row is not None:
            if record.get("assertions") != assertion_statistics(row.record):
                _add(
                    issues,
                    f"场景记录断言统计与外部评分证据不一致: {scenario_id}",
                )
            if record.get("scoring_mode") != "external":
                _add(issues, f"场景记录评分模式非法: {scenario_id}")
    return issues


def _run_status(evidence: ExternalRunEvidence | None) -> str:
    if evidence is None:
        return "incomplete"
    if any(
        row.execution.status != "completed"
        or row.record.assertion_status != "passed"
        or row.record.incomplete
        for row in evidence.scenarios
    ):
        return "incomplete"
    return "awaiting_external_scoring"


# ---- 历史 LangSmith 回收批次直读 ----
# T4a 起回收链路只产 Langfuse 批次；历史 fit-agent-external-judge-feedback-*-1 批次
# 在此按原契约形状解析消费，身份字段（run_id/feedback_id/cloud_run_id）保持原值。


class _HistoryStateMapping(EvaluationContract):
    user_request: Literal["inputs.user_request"]
    answer: Literal["outputs.answer"]
    context: Literal["outputs.context.items"]
    reference: Literal["reference_outputs.reference"]


class _HistoryEvaluatorBinding(EvaluationContract):
    provider: NonEmptyText
    evaluator: NonEmptyText
    evaluator_id: NonEmptyText
    model_alias: NonEmptyText
    config: dict[str, Any]
    config_sha256: Sha256

    @model_validator(mode="after")
    def validate_config(self) -> Self:
        expected = sha256_text(canonical_json(self.config))
        if self.config_sha256 != expected:
            raise ValueError(f"config_sha256 应为 {expected}")
        return self


class _HistoryMetricBinding(EvaluationContract):
    metric: ExternalJudgeMetric
    feedback_key: NonEmptyText
    question_id: NonEmptyText
    question: NonEmptyText
    question_sha256: Sha256
    rubric_id: NonEmptyText | None = None
    rubric: NonEmptyText
    rubric_sha256: Sha256

    @model_validator(mode="after")
    def validate_definition(self) -> Self:
        if self.question_sha256 != question_digest(
            self.question_id, self.question
        ):
            raise ValueError("question_sha256 与问题内容不一致")
        if self.rubric_sha256 != rubric_digest(self.rubric_id, self.rubric):
            raise ValueError("rubric_sha256 与评分标准不一致")
        return self


class _HistoryExperimentTarget(EvaluationContract):
    experiment_id: NonEmptyText
    experiment_name: NonEmptyText
    dataset_id: NonEmptyText
    dataset_name: NonEmptyText
    payload_sha256: Sha256
    rows_sha256: Sha256
    projections_sha256: Sha256


class _HistoryBinding(EvaluationContract):
    schema_version: Literal["fit-agent-external-judge-feedback-binding-1"]
    run_id: NonEmptyText
    source_sha256: Sha256
    applicability_sha256: Sha256
    experiment: _HistoryExperimentTarget | None = None
    evaluator: _HistoryEvaluatorBinding
    state_mapping: _HistoryStateMapping
    metrics: list[_HistoryMetricBinding] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def validate_metrics(self) -> Self:
        metrics = [row.metric for row in self.metrics]
        if sorted(metrics) != sorted(EXTERNAL_JUDGE_METRICS):
            raise ValueError("反馈绑定必须逐项且仅包含两个外部裁判指标")
        keys = [row.feedback_key for row in self.metrics]
        if len(set(keys)) != len(keys):
            raise ValueError("反馈绑定的 feedback key 不能重复")
        if self.state_mapping.context != "outputs.context.items":
            raise ValueError("context 必须映射 outputs.context.items")
        return self


class _HistoryRawEvidence(EvaluationContract):
    evidence_id: NonEmptyText
    feedback_id: NonEmptyText
    run_id: NonEmptyText
    key: NonEmptyText
    raw: dict[str, Any]
    raw_sha256: Sha256

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        expected = sha256_text(canonical_json(self.raw))
        if self.raw_sha256 != expected:
            raise ValueError("原始证据摘要与内容不一致")
        if self.evidence_id != f"{self.feedback_id}@{self.raw_sha256[:12]}":
            raise ValueError("原始证据 ID 必须由 feedback ID 与摘要导出")
        return self


class _HistoryExclusion(EvaluationContract):
    evidence_id: NonEmptyText
    feedback_id: NonEmptyText
    metric: ExternalJudgeMetric | None = None
    reason: HISTORICAL_EXCLUSION_REASONS
    detail: NonEmptyText


class _HistoryDiagnostic(EvaluationContract):
    code: HISTORICAL_DIAGNOSTIC_CODES
    metric: ExternalJudgeMetric | None = None
    detail: NonEmptyText


class _HistoryRecord(EvaluationContract):
    checkpoint: EvidenceLink
    execution_sha256: Sha256
    row_id: NonEmptyText | None = None
    example_id: NonEmptyText | None = None
    cloud_run_id: NonEmptyText | None = None
    projection_sha256: Sha256 | None = None
    feedback_set: ExternalJudgeFeedbackSet
    attempts: list[ExternalJudgeScoringAttempt] = Field(default_factory=list)
    evidence: list[_HistoryRawEvidence] = Field(default_factory=list)
    exclusions: list[_HistoryExclusion] = Field(default_factory=list)
    diagnostics: list[_HistoryDiagnostic] = Field(default_factory=list)
    evidence_sha256: Sha256
    status: Literal["complete", "pending", "blocked", "not_applicable"]

    @model_validator(mode="after")
    def validate_record(self) -> Self:
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
            if self.cloud_run_id is not None and (
                attempt.source.run_id != self.cloud_run_id
                or attempt.cloud_feedback is None
                or attempt.cloud_feedback.run_id != self.cloud_run_id
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
            if row.status in ("succeeded", "failed"):
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
            if row.status == "not_applicable" and matching:
                raise ValueError("不适用指标不得绑定评分尝试")
        blocking = [
            diagnostic
            for diagnostic in self.diagnostics
            if diagnostic.code in HISTORICAL_BLOCKING_CODES
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


class _HistorySourceSummary(EvaluationContract):
    run_id: NonEmptyText
    dataset: DatasetIdentity
    source_sha256: Sha256
    payload_sha256: Sha256 | None = None
    rows_sha256: Sha256 | None = None
    projections_sha256: Sha256 | None = None
    coverage: FeedbackCoverage
    row_count: int = Field(ge=0)
    example_count: int = Field(ge=0)


class _HistoryBatch(EvaluationContract):
    schema_version: Literal["fit-agent-external-judge-feedback-collection-1"]
    content_sha256: Sha256
    run_id: NonEmptyText
    read_at: NonEmptyText
    source_assurance: Literal["declared_binding"]
    source: _HistorySourceSummary
    binding_sha256: Sha256
    binding: _HistoryBinding
    experiment: FeedbackExperimentSummary | None = None
    checkpoints: list[_HistoryRecord] = Field(min_length=1)
    status: Literal["complete", "pending", "blocked"]
    blocking: list[NonEmptyText] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_batch(self) -> Self:
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
                        or attempt.cloud_feedback is None
                        or attempt.cloud_feedback.experiment_id
                        != self.experiment.experiment_id
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
        expected_content = _batch_content_sha256(self.model_dump(mode="json"))
        if self.content_sha256 != expected_content:
            raise ValueError("回收批次内容摘要与正文不一致")
        return self


class _HistoryBatchReference(EvaluationContract):
    batch_id: NonEmptyText
    content_sha256: Sha256
    path: NonEmptyText
    read_at: NonEmptyText
    status: Literal["complete", "pending", "blocked"]


class _HistoryIndex(EvaluationContract):
    schema_version: Literal["fit-agent-external-judge-feedback-index-1"]
    run_id: NonEmptyText
    source_sha256: Sha256
    payload_sha256: Sha256 | None = None
    applicability_sha256: Sha256
    binding_sha256: Sha256
    experiment_id: NonEmptyText | None = None
    latest: NonEmptyText
    batches: list[_HistoryBatchReference] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_index(self) -> Self:
        ids = [item.batch_id for item in self.batches]
        if len(set(ids)) != len(ids):
            raise ValueError("回收索引的批次 ID 不能重复")
        paths = [item.path for item in self.batches]
        if len(set(paths)) != len(paths):
            raise ValueError("回收索引的批次路径不能重复")
        if self.latest not in ids:
            raise ValueError("回收索引的最新批次不在批次列表中")
        return self


@dataclass(frozen=True)
class HistoricalCollection:
    index: _HistoryIndex
    batch: _HistoryBatch

    @property
    def latest_batch_id(self) -> str:
        return self.index.latest

    @property
    def batch_count(self) -> int:
        return len(self.index.batches)


def _load_historical_collection(
    run_dir: Path, run_id: str
) -> HistoricalCollection | None:
    index_path = feedback_index_path(run_dir)
    if not index_path.is_file():
        return None
    try:
        document = json.loads(index_path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise UploadBlocked(f"回收索引无法解析: {error}") from error
    schema = (
        document.get("schema_version") if isinstance(document, dict) else None
    )
    if schema == FEEDBACK_INDEX_SCHEMA:
        # Langfuse 索引归常规回收链路消费，直读路径不处理。
        return None
    if schema != HISTORICAL_INDEX_SCHEMA:
        raise UploadBlocked(f"回收索引版本不受支持: {schema!r}")
    try:
        index = _HistoryIndex.model_validate(document)
    except ValidationError as error:
        raise UploadBlocked("历史回收索引损坏，拒绝继续") from error
    if index.run_id != run_id:
        raise UploadBlocked("历史回收索引 run_id 与源运行不一致")
    reference = next(
        item for item in index.batches if item.batch_id == index.latest
    )
    relative = Path(reference.path)
    if (
        relative.parent != Path(FEEDBACK_BATCH_DIR)
        or relative.name != f"{reference.batch_id}.json"
    ):
        raise UploadBlocked("回收索引包含非法批次路径")
    batch_path = feedback_dir(run_dir) / relative
    expected_dir = (feedback_dir(run_dir) / FEEDBACK_BATCH_DIR).resolve()
    if batch_path.resolve().parent != expected_dir:
        raise UploadBlocked("回收批次路径越界")
    if not batch_path.is_file():
        raise UploadBlocked("回收索引指向的批次文件缺失")
    try:
        batch_document = json.loads(batch_path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise UploadBlocked(f"回收批次无法解析: {error}") from error
    try:
        batch = _HistoryBatch.model_validate(batch_document)
    except ValidationError as error:
        raise UploadBlocked("历史回收批次损坏，拒绝继续") from error
    if (
        batch.content_sha256 != reference.content_sha256
        or batch.status != reference.status
        or _batch_id(batch.content_sha256) != reference.batch_id
    ):
        raise UploadBlocked("回收批次与索引记录不一致")
    if batch.run_id != run_id:
        raise UploadBlocked("历史回收批次 run_id 与源运行不一致")
    return HistoricalCollection(index=index, batch=batch)


def load_external_scenario_source(
    source: Path, dataset: EvaluationDataset
) -> ExternalScenarioSource:
    root, document = _read_document(source)
    platform = _require_external(document)
    run_id = document.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ExternalSummaryError("运行摘要缺少 run_id")
    if root.name != run_id:
        raise ExternalSummaryError("源运行目录名称与 run_id 不一致")
    purpose = document.get("purpose")
    if purpose not in {"formal", "validation"}:
        raise ExternalSummaryError("运行摘要 purpose 非法")
    if document.get("status") == "running":
        raise ExternalSummaryError("源运行仍在进行，拒绝读取未终结证据")
    result = ExternalScenarioSource(
        path=root / "sdk-run.json",
        root=root,
        dataset=dataset,
        document=document,
        platform=platform,
        run_id=run_id,
        purpose=purpose,
        scoring_mode=document.get("scoring_mode"),
        protocol_version=document.get("protocol_version"),
        feedback_version=document.get("feedback_version"),
        recorded_status=document.get("status"),
    )
    all_ids = [scenario["scenario_id"] for scenario in dataset.scenarios]
    scope_ids = _scope_ids(document)
    if purpose == "formal":
        result.expected_scenario_ids = list(all_ids)
        if scope_ids is None:
            _add(result.blocking, "正式运行缺少场景范围")
        elif set(scope_ids) != set(all_ids):
            _add(result.blocking, "正式场景必须覆盖全部批准场景")
    else:
        if scope_ids is None:
            _add(result.blocking, "定向运行缺少显式场景范围")
            result.expected_scenario_ids = list(all_ids)
        else:
            result.expected_scenario_ids = list(scope_ids)
    unknown = sorted(set(result.expected_scenario_ids) - set(all_ids))
    if unknown:
        _add(result.blocking, f"运行范围包含未知场景: {unknown[:3]}")
    for issue in _metadata_issues(document, dataset, purpose, platform):
        _add(result.blocking, issue)
    evidence: ExternalRunEvidence | None = None
    try:
        evidence = load_external_run(root, dataset)
    except UploadBlocked as error:
        _add(result.blocking, f"源证据校验失败: {error}")
    result.evidence = evidence
    if evidence is not None:
        for issue in _request_issues(evidence, document):
            _add(result.blocking, issue)
    applicability = None
    if evidence is not None:
        observed = {row.scenario["scenario_id"] for row in evidence.scenarios}
        if observed != set(result.expected_scenario_ids):
            _add(
                result.blocking,
                "运行样本未覆盖声明场景或包含范围外场景",
            )
        try:
            applicability = load_external_applicability(evidence)
        except UploadBlocked as error:
            _add(result.blocking, f"适用性批准校验失败: {error}")
    result.applicability = applicability
    ledger = None
    if evidence is not None:
        ledger = _coverage_ledger(evidence, applicability)
    result.ledger = ledger
    verified = None
    if evidence is not None and applicability is not None:
        try:
            verified = load_verified_collection(
                root, dataset_dir=dataset.version_dir
            )
        except UploadBlocked as error:
            _add(result.blocking, f"反馈回收证据校验失败: {error}")
    if verified is not None:
        result.context = verified.context
        result.collection = verified.collection
        result.latest = verified.latest
        if verified.collection is not None:
            result.batch_id = verified.collection.index.latest
    if verified is None:
        # Langfuse 回收链路未消费时尝试历史 LangSmith 批次：仅认历史索引 schema，直接本地读取。
        try:
            result.history = _load_historical_collection(root, run_id)
        except UploadBlocked as error:
            _add(result.blocking, f"反馈回收证据校验失败: {error}")
        if result.history is not None:
            result.batch_id = result.history.latest_batch_id
    recomputed_status = _run_status(evidence)
    if result.recorded_status not in ("awaiting_external_scoring", "incomplete"):
        _add(result.blocking, f"运行状态非法: {result.recorded_status!r}")
    elif result.recorded_status != recomputed_status:
        _add(result.blocking, "运行状态与执行/断言重算结果不一致")
    for issue in _scenario_record_issues(
        document, evidence, result.expected_scenario_ids
    ):
        _add(result.blocking, issue)
    result.issues = list(result.blocking)
    _build_views(result)
    return result


def _build_views(source: ExternalScenarioSource) -> None:
    scenario_by_id = {
        scenario["scenario_id"]: scenario
        for scenario in source.dataset.scenarios
    }
    evidence_by_scenario = (
        {}
        if source.evidence is None
        else {
            row.scenario["scenario_id"]: row for row in source.evidence.scenarios
        }
    )
    records: dict[tuple[str, str, str], Any] = {}
    batch_status: str | None = None
    batch_blocking: list[str] = []
    if source.latest is not None:
        for record in source.latest.checkpoints:
            records[
                (
                    record.checkpoint.scenario_id,
                    record.checkpoint.event_id,
                    record.checkpoint.checkpoint_id,
                )
            ] = record
        batch_status = source.latest.status
        batch_blocking = list(source.latest.blocking)
    elif source.history is not None:
        for record in source.history.batch.checkpoints:
            records[
                (
                    record.checkpoint.scenario_id,
                    record.checkpoint.event_id,
                    record.checkpoint.checkpoint_id,
                )
            ] = record
        batch_status = source.history.batch.status
        batch_blocking = list(source.history.batch.blocking)
    ledger_entries = (
        {}
        if source.ledger is None
        else {
            (
                entry["checkpoint"]["scenario_id"],
                entry["checkpoint"]["event_id"],
                entry["checkpoint"]["checkpoint_id"],
            ): entry
            for entry in source.ledger["entries"]
        }
    )
    feedback_keys: dict[str, str] = {}
    if source.context is not None:
        feedback_keys = {
            item.metric: item.feedback_key
            for item in source.context.binding.metrics
        }
    elif source.history is not None:
        feedback_keys = {
            item.metric: item.feedback_key
            for item in source.history.batch.binding.metrics
        }
    for scenario_id in source.expected_scenario_ids:
        scenario = scenario_by_id.get(scenario_id)
        if scenario is None:
            source.scenarios[scenario_id] = ScenarioView(
                scenario_id=scenario_id,
                state="missing",
                execution={},
                assertions={},
                blocking=[f"{scenario_id}: 场景不在批准数据集中"],
            )
            continue
        view = _build_scenario_view(
            scenario=scenario,
            record=evidence_by_scenario.get(scenario_id),
            evidence_keys=(
                []
                if source.evidence is None
                else [
                    checkpoint.key
                    for checkpoint in source.evidence.checkpoints
                    if checkpoint.link.scenario_id == scenario_id
                ]
            ),
            evidence_execution=(
                {}
                if source.evidence is None
                else {
                    checkpoint.key: checkpoint.execution_sha256
                    for checkpoint in source.evidence.checkpoints
                }
            ),
            approved=source.applicability,
            ledger_entries=ledger_entries,
            records=records,
            collection_present=(
                source.latest is not None or source.history is not None
            ),
            feedback_keys=feedback_keys,
            source_issues=[],
        )
        source.scenarios[scenario_id] = view
        for issue in view.blocking:
            _add(source.blocking, issue)
    if batch_status is None:
        if source.collection is None and source.history is None:
            _add(
                source.blocking,
                "外部反馈尚未回收: 缺少 "
                "external-judge/decision-feedback/index.json",
            )
        elif source.collection is not None:
            _add(source.blocking, "外部反馈回收索引缺失最新批次")
    elif batch_status == "pending":
        _add(source.blocking, "外部反馈最新批次仍待回收")
    elif batch_status == "blocked":
        _add(
            source.blocking,
            "外部反馈最新批次校验阻断: " + "; ".join(batch_blocking),
        )
    source.issues = sorted(set(source.blocking))


def _mean(values: Sequence[float]) -> float | None:
    return mean(values) if values else None


def _scenario_metric_summary(
    metric: str, scenario: ScenarioView
) -> dict[str, Any]:
    entries = [
        checkpoint.entries[metric]
        for checkpoint in scenario.checkpoints.values()
    ]
    applicable = [
        entry for entry in entries if entry.applicable is not False
    ]
    not_applicable = len(entries) - len(applicable)
    counts = Counter(entry.status for entry in applicable)
    values = [
        entry.score
        for entry in applicable
        if entry.status == "succeeded" and entry.score is not None
    ]
    value = _mean(values)
    succeeded = counts.get("succeeded", 0)
    complete = (
        bool(values)
        and succeeded == len(applicable)
        and len(values) == len(applicable)
    )
    missing_keys = [
        f"{checkpoint.event_id}/{checkpoint.checkpoint_id}"
        for checkpoint in scenario.checkpoints.values()
        if checkpoint.entries[metric].applicable is not False
        and checkpoint.entries[metric].status
        in {"failed", "pending", "missing", "blocked", "unapproved"}
    ]
    blocking = sorted(
        {
            detail
            for entry in applicable
            for detail in entry.blocked
        }
    )
    return {
        "expected": len(entries),
        "applicable": len(applicable),
        "not_applicable": not_applicable,
        "succeeded": succeeded,
        "failed": counts.get("failed", 0),
        "pending": counts.get("pending", 0),
        "missing": counts.get("missing", 0),
        "blocked": counts.get("blocked", 0),
        "unapproved": counts.get("unapproved", 0),
        "value": value,
        "complete": complete,
        "all_not_applicable": bool(entries) and not applicable,
        "missing_keys": missing_keys,
        "blocking": blocking,
        "entries": {
            checkpoint.checkpoint_id: {
                "event_id": checkpoint.event_id,
                "status": checkpoint.entries[metric].status,
                "score": checkpoint.entries[metric].score,
                "applicable": checkpoint.entries[metric].applicable,
                "approved": checkpoint.entries[metric].approved,
                "detail": checkpoint.entries[metric].detail,
                "feedback_status": checkpoint.entries[
                    metric
                ].feedback_status,
                "attempt_ids": checkpoint.entries[metric].attempt_ids,
                "evidence_ids": checkpoint.entries[metric].evidence_ids,
                "cloud_feedback_ids": checkpoint.entries[
                    metric
                ].cloud_feedback_ids,
                "diagnostic_codes": checkpoint.entries[
                    metric
                ].diagnostic_codes,
                "blocked": checkpoint.entries[metric].blocked,
            }
            for checkpoint in scenario.checkpoints.values()
        },
    }


def _aggregate_metric(
    metric: str,
    expected_ids: Sequence[str],
    scenarios: dict[str, ScenarioView],
) -> dict[str, Any]:
    totals = Counter()
    scenario_values: dict[str, Any] = {}
    available: list[float] = []
    complete: list[float] = []
    all_not_applicable: list[str] = []
    for scenario_id in expected_ids:
        scenario = scenarios.get(scenario_id)
        if scenario is None or not scenario.checkpoints:
            row = {
                "expected": 0,
                "applicable": 0,
                "not_applicable": 0,
                "succeeded": 0,
                "failed": 0,
                "pending": 0,
                "missing": 0,
                "blocked": 0,
                "unapproved": 0,
                "value": None,
                "complete": False,
                "all_not_applicable": False,
                "missing_keys": [],
                "blocking": [],
                "entries": {},
            }
        else:
            row = _scenario_metric_summary(metric, scenario)
        for key in (
            "expected",
            "applicable",
            "not_applicable",
            "succeeded",
            "failed",
            "pending",
            "missing",
            "blocked",
            "unapproved",
        ):
            totals[key] += row[key]
        if row["value"] is not None:
            available.append(row["value"])
        if row["complete"] and row["value"] is not None:
            complete.append(row["value"])
        if row["all_not_applicable"]:
            all_not_applicable.append(scenario_id)
        scenario_values[scenario_id] = {
            "value": row["value"],
            "complete": row["complete"],
            "all_not_applicable": row["all_not_applicable"],
            "expected": row["expected"],
            "applicable": row["applicable"],
            "not_applicable": row["not_applicable"],
            "succeeded": row["succeeded"],
            "failed": row["failed"],
            "pending": row["pending"],
            "missing": row["missing"],
            "blocked": row["blocked"],
            "unapproved": row["unapproved"],
            "missing_keys": row["missing_keys"],
            "blocking": row["blocking"],
            "entries": row["entries"],
        }
    return {
        "metric": metric,
        "definition": METRIC_DEFINITIONS[metric],
        "expected_checkpoints": totals["expected"],
        "applicable_checkpoints": totals["applicable"],
        "not_applicable_checkpoints": totals["not_applicable"],
        "succeeded_checkpoints": totals["succeeded"],
        "failed_checkpoints": totals["failed"],
        "pending_checkpoints": totals["pending"],
        "missing_checkpoints": totals["missing"],
        "blocked_checkpoints": totals["blocked"],
        "unapproved_checkpoints": totals["unapproved"],
        "expected_scenarios": len(expected_ids),
        "available_scenarios": len(available),
        "complete_scenarios": len(complete),
        "all_not_applicable_scenarios": sorted(all_not_applicable),
        "mean_including_partial": _mean(available),
        "mean_complete_only": _mean(complete),
        "scenario_values": scenario_values,
    }


def _execution_summary(source: ExternalScenarioSource) -> dict[str, Any]:
    observed = 0
    completed = 0
    expected_events = 0
    observed_events = 0
    context_gaps: list[dict[str, Any]] = []
    ambiguous = 0
    for scenario in source.scenarios.values():
        if scenario.state != "missing":
            observed += 1
        if scenario.state == "completed":
            completed += 1
        expected_events += len(scenario.execution.get("expected_events", []))
        observed_events += len(scenario.execution.get("observed_events", []))
        for gap in scenario.execution.get("context_gaps", []):
            context_gaps.append(
                {"scenario_id": scenario.scenario_id, **gap}
            )
            if gap.get("ambiguous"):
                ambiguous += 1
    return {
        "expected_scenarios": len(source.expected_scenario_ids),
        "observed_scenarios": observed,
        "completed_scenarios": completed,
        "expected_events": expected_events,
        "observed_events": observed_events,
        "complete": (
            observed == len(source.expected_scenario_ids)
            and completed == len(source.expected_scenario_ids)
        ),
        "context_gaps": context_gaps,
        "ambiguous_context_gaps": ambiguous,
    }


def _assertion_summary(source: ExternalScenarioSource) -> dict[str, Any]:
    totals = Counter()
    missing: list[str] = []
    statuses: dict[str, str] = {}
    for scenario in source.scenarios.values():
        row = scenario.assertions
        statuses[scenario.scenario_id] = row.get("status", "missing")
        for key in ("expected", "observed", "passed", "failed", "binding_errors", "runner_errors"):
            totals[key] += row.get(key, 0)
        for item in row.get("missing_checkpoints", []):
            missing.append(f"{scenario.scenario_id}/{item}")
    complete = (
        totals["observed"] == totals["expected"]
        and totals["binding_errors"] == 0
        and totals["runner_errors"] == 0
        and not missing
    )
    return {
        **dict(totals),
        "missing_checkpoints": missing,
        "statuses": statuses,
        "complete": complete,
        "passed_all": complete and totals["failed"] == 0,
    }


def _coverage_summary(
    source: ExternalScenarioSource,
    metrics: dict[str, dict[str, Any]],
    execution: dict[str, Any],
) -> dict[str, Any]:
    ledger_counts = (
        {}
        if source.ledger is None
        else dict(source.ledger["counts"])
    )
    observed = {
        key for key in source.expected_checkpoints
        if key in {
            row.key for row in (source.evidence.checkpoints if source.evidence else ())
        }
    }
    return {
        "expected_scenarios": len(source.expected_scenario_ids),
        "observed_scenarios": execution["observed_scenarios"],
        "completed_scenarios": execution["completed_scenarios"],
        "scenario_ids": list(source.expected_scenario_ids),
        "missing_scenarios": sorted(
            scenario_id
            for scenario_id in source.expected_scenario_ids
            if source.scenarios.get(scenario_id) is None
            or source.scenarios[scenario_id].state == "missing"
        ),
        "expected_events": execution["expected_events"],
        "observed_events": execution["observed_events"],
        "expected_checkpoints": len(source.expected_checkpoints),
        "observed_checkpoints": len(observed),
        "missing_checkpoints": sorted(
            "/".join(key) for key in source.expected_checkpoints - observed
        ),
        "checkpoint_classification": ledger_counts,
        "metrics": {
            metric: {
                key: row[f"{key}_checkpoints"]
                if f"{key}_checkpoints" in row
                else row[key]
                for key in (
                    "expected",
                    "applicable",
                    "not_applicable",
                    "succeeded",
                    "failed",
                    "pending",
                    "missing",
                    "blocked",
                    "unapproved",
                )
            }
            for metric, row in metrics.items()
        },
        "dimension_identity": DIMENSION_IDENTITY,
    }


def _iter_batch_records(
    source: ExternalScenarioSource,
) -> list[Any]:
    if source.latest is not None:
        return list(source.latest.checkpoints)
    if source.history is not None:
        return list(source.history.batch.checkpoints)
    return []


def _judge_summary(source: ExternalScenarioSource) -> dict[str, Any]:
    context = source.context
    history = source.history
    if context is None and history is None:
        return {
            "source_assurance": SOURCE_ASSURANCE,
            "limitations": list(SOURCE_ASSURANCE_LIMITS),
            "declared": None,
            "model_versions": {},
            "batch_read_at": None,
        }
    if context is not None:
        evaluator = context.binding.evaluator
        state_mapping = context.binding.state_mapping.model_dump(mode="json")
        metrics = context.binding.metrics
    else:
        assert history is not None
        evaluator = history.batch.binding.evaluator
        state_mapping = history.batch.binding.state_mapping.model_dump(
            mode="json"
        )
        metrics = history.batch.binding.metrics
    versions: dict[str, dict[str, Any]] = {}
    for metric in EXTERNAL_JUDGE_METRICS:
        reported: set[str] = set()
        unreported = 0
        attempts = 0
        for record in _iter_batch_records(source):
            for attempt in record.attempts:
                if attempt.feedback.metric != metric:
                    continue
                attempts += 1
                if attempt.model.model_version.kind == "reported":
                    reported.add(attempt.model.model_version.version)
                else:
                    unreported += 1
        versions[metric] = {
            "attempts": attempts,
            "reported": sorted(reported),
            "unreported": unreported,
        }
    read_at = None
    if source.latest is not None:
        read_at = source.latest.read_at
    elif history is not None:
        read_at = history.batch.read_at
    return {
        "source_assurance": SOURCE_ASSURANCE,
        "limitations": list(SOURCE_ASSURANCE_LIMITS),
        "declared": {
            "provider": evaluator.provider,
            "evaluator": evaluator.evaluator,
            "evaluator_id": evaluator.evaluator_id,
            "model_alias": evaluator.model_alias,
            "config_sha256": evaluator.config_sha256,
            "state_mapping": state_mapping,
            "metrics": [
                {
                    "metric": item.metric,
                    "feedback_key": item.feedback_key,
                    "question_id": item.question_id,
                    "question_sha256": item.question_sha256,
                    "rubric_id": item.rubric_id,
                    "rubric_sha256": item.rubric_sha256,
                }
                for item in metrics
            ],
        },
        "model_versions": versions,
        "batch_read_at": read_at,
    }


def _identity_summary(source: ExternalScenarioSource) -> dict[str, Any]:
    document = source.document
    paths = {
        "source": _display_path(source.root),
        "sdk_run": _display_path(
            None if source.root is None else source.root / "sdk-run.json"
        ),
    }
    if source.root is not None:
        directory = external_dir(source.root)
        paths.update(
            {
                "applicability": _display_path(
                    directory / "applicability.json"
                ),
                "payload": _display_path(
                    directory / "checkpoints-payload.json"
                ),
                "coverage_ledger": _display_path(
                    directory / "coverage-ledger.json"
                ),
                "dataset_receipt": _display_path(
                    directory / "checkpoint-dataset.json"
                ),
                "experiment_receipt": _display_path(
                    directory / "checkpoint-experiment.json"
                ),
                "feedback_index": _display_path(
                    directory / "decision-feedback" / "index.json"
                ),
                "feedback_batch": _display_path(
                    None
                    if source.batch_id is None
                    else directory
                    / "decision-feedback"
                    / "batches"
                    / f"{source.batch_id}.json"
                ),
            }
        )
    collection = source.collection
    history = source.history
    if source.context is not None:
        payload_sha256 = source.context.source.payload_sha256
        rows_sha256 = source.context.source.rows_sha256
        projections_sha256 = source.context.source.projections_sha256
        applicability_sha256 = source.context.binding.applicability_sha256
        binding_sha256 = source.context.binding_sha256
        projection_rows = source.context.source.row_count
        projection_examples = source.context.source.example_count
        experiment_summary = (
            None
            if source.context.experiment is None
            else source.context.experiment.model_dump(mode="json")
        )
    else:
        payload_sha256 = None if history is None else history.batch.source.payload_sha256
        rows_sha256 = None if history is None else history.batch.source.rows_sha256
        projections_sha256 = (
            None if history is None else history.batch.source.projections_sha256
        )
        applicability_sha256 = (
            None if history is None else history.batch.binding.applicability_sha256
        )
        binding_sha256 = None if history is None else history.batch.binding_sha256
        projection_rows = 0 if history is None else history.batch.source.row_count
        projection_examples = (
            0 if history is None else history.batch.source.example_count
        )
        experiment_summary = (
            None
            if history is None or history.batch.experiment is None
            else history.batch.experiment.model_dump(mode="json")
        )
    if source.latest is not None:
        batch_status = source.latest.status
        batch_content = source.latest.content_sha256
        batch_read_at = source.latest.read_at
    elif history is not None:
        batch_status = history.batch.status
        batch_content = history.batch.content_sha256
        batch_read_at = history.batch.read_at
    else:
        batch_status = None
        batch_content = None
        batch_read_at = None
    batches_count = 0 if collection is None else len(collection.batches)
    if collection is None and history is not None:
        batches_count = history.batch_count
    return {
        "dataset": document.get("dataset"),
        "code": document.get("code_identity"),
        "generation": document.get("generation"),
        "index": document.get("index"),
        "source_sha256": (
            None if source.evidence is None else source.evidence.source_sha256
        ),
        "payload_sha256": payload_sha256,
        "rows_sha256": rows_sha256,
        "projections_sha256": projections_sha256,
        "applicability_sha256": applicability_sha256,
        "binding_sha256": binding_sha256,
        "projections": {
            "rows": projection_rows,
            "examples": projection_examples,
        },
        "experiments": {
            "scenario": document.get("experiment"),
            "checkpoint_judge": experiment_summary,
        },
        "collection": {
            "index_present": collection is not None or history is not None,
            "latest": (
                None
                if collection is None
                else collection.index.latest
            ),
            "batch_id": source.batch_id,
            "batch_status": batch_status,
            "content_sha256": batch_content,
            "read_at": batch_read_at,
            "batches": batches_count,
        },
        "paths": paths,
    }


def _release(
    *,
    source: ExternalScenarioSource,
    scope_name: str,
    formal: bool,
    execution: dict[str, Any],
    assertions: dict[str, Any],
    metrics: dict[str, dict[str, Any]],
    retrieval_blocking: Sequence[str],
) -> dict[str, Any]:
    applicable = [
        value
        for row in metrics.values()
        for scenario in row["scenario_values"].values()
        for value in scenario["entries"].values()
        if value["applicable"] is not False
    ]
    succeeded = sum(1 for item in applicable if item["status"] == "succeeded")
    failed = sum(1 for item in applicable if item["status"] == "failed")
    pending = sum(1 for item in applicable if item["status"] == "pending")
    missing = sum(1 for item in applicable if item["status"] == "missing")
    blocked = sum(1 for item in applicable if item["status"] == "blocked")
    unapproved = sum(1 for item in applicable if item["status"] == "unapproved")
    not_applicable = sum(
        1
        for row in metrics.values()
        for scenario in row["scenario_values"].values()
        for value in scenario["entries"].values()
        if value["status"] == "not_applicable"
    )
    if source.latest is not None:
        latest_status: str | None = source.latest.status
    elif source.history is not None:
        latest_status = source.history.batch.status
    else:
        latest_status = None
    feedback_complete = (
        latest_status == "complete"
        and failed + succeeded == len(applicable)
        and blocked == 0
        and pending == 0
        and missing == 0
        and unapproved == 0
    )
    generation_succeeded = (
        bool(applicable)
        and succeeded == len(applicable)
        and blocked == 0
        and pending == 0
        and missing == 0
        and unapproved == 0
    )
    all_not_applicable = (
        not applicable and not_applicable > 0 and latest_status == "complete"
    )
    checks = {
        "scope_formal": formal,
        "execution_complete": execution["complete"],
        "assertions_complete": assertions["complete"],
        "assertions_passed": assertions["passed_all"],
        "feedback_evidence_complete": feedback_complete,
        "generation_scoring_succeeded": generation_succeeded,
        "generation_all_not_applicable": all_not_applicable,
        "retrieval_gates_passed": not retrieval_blocking,
    }
    blocking = list(source.blocking)
    blocking.extend(retrieval_blocking)
    if not formal:
        _add(blocking, "release:定向验证范围不能作为有效发布")
    if latest_status is None:
        _add(blocking, "外部生成评分尚未回收")
    elif latest_status == "pending":
        _add(blocking, "外部生成评分最新批次仍待回收")
    elif latest_status == "blocked":
        _add(blocking, "外部生成评分最新批次校验阻断")
    if failed:
        _add(blocking, f"外部生成评分存在失败检查点: {failed}")
    if pending:
        _add(blocking, f"外部生成评分待回收检查点: {pending}")
    if missing:
        _add(blocking, f"外部生成评分缺失检查点: {missing}")
    if blocked:
        _add(blocking, f"外部生成评分校验阻断检查点: {blocked}")
    if unapproved:
        _add(blocking, f"外部适用性未批准检查点: {unapproved}")
    if not execution["complete"]:
        _add(blocking, "执行未完整完成")
    if not assertions["complete"]:
        _add(blocking, "确定性断言未完整执行")
    if not assertions["passed_all"]:
        _add(blocking, "确定性断言未全部通过")
    effective = formal and not blocking
    return {
        "effective": effective,
        "scope": scope_name,
        "formal_scope": formal,
        "blocking_issues": sorted(set(blocking)),
        "checks": checks,
        "quality_reviewed": False,
        "generation_counts": {
            "applicable": len(applicable),
            "succeeded": succeeded,
            "failed": failed,
            "pending": pending,
            "missing": missing,
            "blocked": blocked,
            "unapproved": unapproved,
            "not_applicable": not_applicable,
        },
    }


def build_summary(
    source: ExternalScenarioSource,
    *,
    scope: str = "auto",
    retrieval: dict[str, Any] | None = None,
    retrieval_blocking: Sequence[str] = (),
) -> dict[str, Any]:
    if scope not in ("auto", "formal", "validation"):
        raise ExternalSummaryError(f"未知 scope: {scope!r}")
    dataset = source.dataset
    assert dataset is not None
    full = (
        source.purpose == "formal"
        and set(source.expected_scenario_ids)
        == {scenario["scenario_id"] for scenario in dataset.scenarios}
    )
    if scope == "formal":
        scope_name = "formal"
        formal = full
        if not full:
            _add(
                source.blocking,
                "scope:定向验证范围不能作为正式发布",
            )
    elif scope == "validation":
        scope_name = "validation"
        formal = False
        _add(source.warnings, "scope:定向验证范围仅用于诊断")
    else:
        scope_name = "formal" if full else "validation"
        formal = full
    metrics = {
        metric: _aggregate_metric(
            metric, source.expected_scenario_ids, source.scenarios
        )
        for metric in EXTERNAL_JUDGE_METRICS
    }
    execution = _execution_summary(source)
    assertions = _assertion_summary(source)
    coverage = _coverage_summary(source, metrics, execution)
    release = _release(
        source=source,
        scope_name=scope_name,
        formal=formal,
        execution=execution,
        assertions=assertions,
        metrics=metrics,
        retrieval_blocking=retrieval_blocking,
    )
    scenario_rows = {}
    for scenario_id in source.expected_scenario_ids:
        view = source.scenarios.get(scenario_id)
        if view is None:
            scenario_rows[scenario_id] = {
                "state": "missing",
                "execution": {},
                "assertions": {},
                "metrics": {},
                "checkpoints": {},
                "issues": [f"{scenario_id}: 场景不在批准数据集中"],
                "blocking": [f"{scenario_id}: 场景不在批准数据集中"],
            }
            continue
        scenario_rows[scenario_id] = {
            "state": view.state,
            "execution": view.execution,
            "assertions": view.assertions,
            "metrics": {
                metric: _scenario_metric_summary(metric, view)
                for metric in EXTERNAL_JUDGE_METRICS
            },
            "checkpoints": {
                key[2]: {
                    "event_id": key[1],
                    "classification": checkpoint.classification,
                    "execution_sha256": checkpoint.execution_sha256,
                    "evidence_missing": checkpoint.evidence_missing,
                    "record_status": checkpoint.record_status,
                    "blocked": checkpoint.blocked,
                }
                for key, checkpoint in view.checkpoints.items()
            },
            "issues": list(view.issues),
            "blocking": list(view.blocking),
        }
    return {
        "schema": EXTERNAL_SUMMARY_SCHEMA,
        "platform": source.platform,
        "scoring_mode": "external",
        "protocol_version": EXTERNAL_PROTOCOLS[source.platform],
        "feedback_version": EXTERNAL_FEEDBACK_VERSION,
        "run_id": source.run_id,
        "scope": scope_name,
        "formal_scope": formal,
        "generated_at": datetime.now(UTC).isoformat(),
        "definitions": {
            "metrics": METRIC_DEFINITIONS,
            "aggregation": AGGREGATION_RULES,
            "score_semantics": (
                "两项分数均为 Noul 概率，不能解释为正确声明比例。"
            ),
            "source_assurance": {
                "kind": SOURCE_ASSURANCE,
                "limitations": list(SOURCE_ASSURANCE_LIMITS),
            },
        },
        "sources": {
            "scenario": {
                "path": _display_path(source.path),
                "root": _display_path(source.root),
                "run_id": source.run_id,
                "platform": source.platform,
                "purpose": source.purpose,
                "scoring_mode": source.scoring_mode,
                "protocol_version": source.protocol_version,
                "feedback_version": source.feedback_version,
                "recorded_status": source.recorded_status,
                "dataset_identity": source.document.get("dataset"),
                "issues": list(source.issues),
            },
            "retrieval": retrieval or {},
        },
        "identity": _identity_summary(source),
        "judge": _judge_summary(source),
        "execution": execution,
        "assertions": assertions,
        "coverage": coverage,
        "generation": {
            "status": (
                "all_not_applicable"
                if release["checks"]["generation_all_not_applicable"]
                else "succeeded"
                if release["checks"]["generation_scoring_succeeded"]
                else "incomplete"
            ),
            "metrics": metrics,
        },
        "retrieval": retrieval or {},
        "scenarios": scenario_rows,
        "source_assurance": SOURCE_ASSURANCE,
        "warnings": sorted(set(source.warnings)),
        "release": release,
        "feedback_status": (
            "absent"
            if source.latest is None and source.history is None
            else (
                source.latest.status
                if source.latest is not None
                else source.history.batch.status
            )
        ),
    }


def _format(value: float | None) -> str:
    return "null" if value is None else f"{value:.6f}"


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines.extend("| " + " | ".join(str(cell) for cell in row) + " |" for row in rows)
    return lines


def report_lines(summary: dict[str, Any]) -> list[str]:
    release = summary["release"]
    checks = release["checks"]
    identity = summary["identity"]
    judge = summary["judge"]
    execution = summary["execution"]
    assertions = summary["assertions"]
    scenarios = summary["scenarios"]
    lines = [
        f"# 外部裁判汇总 `{summary['run_id']}`",
        "",
        f"- 协议：`{summary['scoring_mode']}` / `{summary['protocol_version']}` / feedback v{summary['feedback_version']}。",
        f"- 范围：{'正式全量' if summary['formal_scope'] else '定向验证（仅诊断）'}。",
        f"- 有效发布：{'是' if release['effective'] else '否'}。",
        f"- 执行完成：{'是' if checks['execution_complete'] else '否'}；"
        f"反馈证据齐备：{'是' if checks['feedback_evidence_complete'] else '否'}；"
        f"生成评分成功：{'是' if checks['generation_scoring_succeeded'] else '否'}；"
        f"全 N/A：{'是' if checks['generation_all_not_applicable'] else '否'}。",
        f"- 反馈批次：`{identity['collection']['batch_id'] or '未回收'}`；"
        f"状态 `{summary['feedback_status']}`；"
        f"content `{identity['collection']['content_sha256'] or '未采集'}`。",
        f"- 来源保证：`{summary['source_assurance']}`。",
        "",
        "## 外部指标",
        "",
    ]
    rows = []
    for metric, row in summary["generation"]["metrics"].items():
        rows.append(
            [
                metric,
                row["expected_checkpoints"],
                row["applicable_checkpoints"],
                row["not_applicable_checkpoints"],
                row["succeeded_checkpoints"],
                row["failed_checkpoints"],
                row["pending_checkpoints"],
                row["missing_checkpoints"],
                row["blocked_checkpoints"],
                row["unapproved_checkpoints"],
                row["available_scenarios"],
                row["complete_scenarios"],
                row["expected_scenarios"],
                _format(row["mean_including_partial"]),
                _format(row["mean_complete_only"]),
            ]
        )
    lines.extend(
        _table(
            [
                "指标",
                "预期",
                "适用",
                "N/A",
                "成功",
                "失败",
                "待回收",
                "缺失",
                "阻断",
                "未批准",
                "有值场景",
                "完整场景",
                "预期场景",
                "含部分成功均值",
                "仅完整成功均值",
            ],
            rows,
        )
    )
    lines.extend(
        [
            "",
            "- 场景内为该指标成功检查点均值；场景间为有值场景等权宏平均。",
            "- 分数为 Noul 概率；真实 0 分进入均值，失败、缺失、待回收、阻断与 N/A 不进入数值分母。",
            "",
            "## 覆盖",
            "",
            f"- 场景：预期 {summary['coverage']['expected_scenarios']}；"
            f"已观察 {execution['observed_scenarios']}；执行完成 {execution['completed_scenarios']}。",
            f"- 事件：预期 {execution['expected_events']}；"
            f"已观察 {execution['observed_events']}。",
            f"- 检查点：预期 {summary['coverage']['expected_checkpoints']}；"
            f"已观察 {summary['coverage']['observed_checkpoints']}；"
            f"缺失 {len(summary['coverage']['missing_checkpoints'])}。",
        ]
    )
    classification = summary["coverage"]["checkpoint_classification"]
    if classification:
        lines.append(
            "- 检查点分类："
            + "；".join(f"{key} {value}" for key, value in sorted(classification.items()))
            + "。"
        )
    lines.extend(
        [
            "- 检查点分类与指标状态是不同维度；指标 expected = applicable + N/A，"
            "applicable = succeeded + failed + pending + missing + blocked + unapproved。",
            "",
            "## 确定性断言与执行",
            "",
            f"- 断言：预期 {assertions.get('expected', 0)}；"
            f"已执行 {assertions.get('observed', 0)}；"
            f"通过 {assertions.get('passed', 0)}；"
            f"失败 {assertions.get('failed', 0)}；"
            f"绑定错误 {assertions.get('binding_errors', 0)}；"
            f"运行器错误 {assertions.get('runner_errors', 0)}；"
            f"缺失检查点 {len(assertions.get('missing_checkpoints', []))}。",
            f"- 上下文缺口：{len(execution.get('context_gaps', []))}；"
            f"无法绑定 {execution.get('ambiguous_context_gaps', 0)}。",
            f"- 生成证据状态：{summary['generation']['status']}。",
            "",
            "## 场景明细",
            "",
        ]
    )
    for scenario_id, row in scenarios.items():
        lines.extend(
            [
                f"### `{scenario_id}`",
                "",
                f"- 状态：{row['state']}；断言 "
                f"{row['assertions'].get('status', 'missing')}。",
                "",
            ]
        )
        metric_rows = []
        for metric, metric_row in row["metrics"].items():
            metric_rows.append(
                [
                    metric,
                    _format(metric_row["value"]),
                    metric_row["applicable"],
                    metric_row["succeeded"],
                    metric_row["failed"],
                    metric_row["pending"],
                    metric_row["missing"],
                    metric_row["blocked"],
                    metric_row["unapproved"],
                    "是" if metric_row["complete"] else "否",
                    ", ".join(metric_row["missing_keys"]) or "无",
                ]
            )
        lines.extend(
            _table(
                [
                    "指标",
                    "场景分数",
                    "适用",
                    "成功",
                    "失败",
                    "待回收",
                    "缺失",
                    "阻断",
                    "未批准",
                    "完整",
                    "缺失键",
                ],
                metric_rows,
            )
        )
        if row["blocking"]:
            lines.append("")
            lines.extend(f"- 阻断：{item}" for item in row["blocking"])
        lines.append("")
    lines.extend(
        [
            "## 检索证据",
            "",
            f"- 检索来源状态：`{summary['retrieval'].get('overall_status', '未采集')}`；"
            f"三组完整：{'是' if summary['retrieval'].get('all_groups_complete') else '否'}。",
        ]
    )
    for group, result in (summary["retrieval"].get("groups") or {}).items():
        lines.append(
            f"- `{group}`：状态 `{result.get('status')}`；"
            f"指标 {json.dumps(result.get('metrics'), ensure_ascii=False, sort_keys=True)}。"
        )
    lines.extend(
        [
            "",
            "## 身份与来源",
            "",
            f"- 源运行目录：`{identity['paths'].get('source')}`。",
            f"- 批准数据：version `{(identity.get('dataset') or {}).get('version', '未采集')}`；"
            f"manifest `{(identity.get('dataset') or {}).get('manifest_sha256', '未采集')}`。",
            f"- 场景代码：`{(identity.get('code') or {}).get('commit', '未采集')}`；"
            f"dirty `{(identity.get('code') or {}).get('dirty')}`。",
            f"- 物理索引：`{(identity.get('index') or {}).get('collection', '未采集')}`；"
            f"清单 `{(identity.get('index') or {}).get('manifest_sha256', '未采集')}`。",
            f"- 源证据：source `{identity.get('source_sha256') or '未采集'}`；"
            f"payload `{identity.get('payload_sha256') or '未采集'}`；"
            f"applicability `{identity.get('applicability_sha256') or '未采集'}`；"
            f"binding `{identity.get('binding_sha256') or '未采集'}`。",
            f"- 原始场景实验：`{(identity['experiments'].get('scenario') or {}).get('id', '未采集')}`。",
            f"- 检查点裁判实验：`{(identity['experiments'].get('checkpoint_judge') or {}).get('experiment_id', '未采集')}`。",
            f"- Evaluator 声明：provider `{((judge.get('declared') or {}).get('provider')) or '未采集'}`；"
            f"evaluator `{((judge.get('declared') or {}).get('evaluator')) or '未采集'}`；"
            f"别名 `{((judge.get('declared') or {}).get('model_alias')) or '未采集'}`。",
        ]
    )
    for metric, versions in judge.get("model_versions", {}).items():
        lines.append(
            f"- `{metric}` 模型版本：reported {versions['reported'] or '无'}；"
            f"unreported {versions['unreported']}。"
        )
    lines.extend(["", "## 来源限制", ""])
    lines.extend(f"- {item}" for item in judge["limitations"])
    lines.extend(["", "## 发布门禁", ""])
    if release["blocking_issues"]:
        lines.extend(f"- {item}" for item in release["blocking_issues"])
    else:
        lines.append("- 无阻塞项。")
    lines.extend(
        [
            "",
            "- 发布有效仅表示证据与流程门禁满足；没有人工质量审阅时，裁判准确性未被验收。",
        ]
    )
    if summary["warnings"]:
        lines.extend(["", "## 警告", ""])
        lines.extend(f"- {item}" for item in summary["warnings"])
    return lines
