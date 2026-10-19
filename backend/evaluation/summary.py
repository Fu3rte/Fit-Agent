import argparse
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from statistics import mean
from typing import Any, Iterable, Sequence

from app.application.business.retrieval_response import (
    CANDIDATE_POOL_LIMIT,
    SEARCH_BUDGET,
    SEARCH_LIMIT,
    encoded_length,
)
from evaluation.contracts import GENERATION_METRICS
from evaluation.dataset import BACKEND, EvaluationDataset, load_for_evaluation
from evaluation.generation_scoring import (
    GenerationMetricEvidence,
    ScoringAttemptSnapshot,
    run_judge_identity,
)
from evaluation.scenario_evidence import (
    ContextMappingError,
    RequestBoundaryEvidence,
    context_items_from_request,
    current_input_entry_ids,
    sha256_text,
)
from evaluation.scenarios import (
    AssertionCheckpointArtifact,
    EventCapture,
    ExecutionPlan,
    GenerationCheckpointArtifact,
    ScenarioArtifact,
    ScenarioPlan,
    SearchStageRecord,
    _current_input_text,
    build_execution_plan,
    code_identity,
    generation_reference,
    judge_attempt_identity,
)

SUMMARY_SCHEMA = "fit-agent-evaluation-summary-1"
REPORT_NAME = "report.md"
SUMMARY_NAME = "summary.json"
ARTIFACT_FILES = (
    "scenario.json",
    "partial.json",
    "events.jsonl",
    "assertions.jsonl",
    "generation.jsonl",
    "retrieval.jsonl",
    "context.jsonl",
    "initial-state.json",
    "blocked.json",
    "failure.json",
)
_SNAPSHOT_FIELDS = (
    "metric_class",
    "metric_parameters",
    "metric_version",
    "ragas_version",
    "dependency_versions",
    "judge_model",
    "judge_base_url",
    "judge_timeout_seconds",
    "judge_sdk_max_retries",
    "judge_temperature",
    "ragas_run_config",
    "prompt_fingerprints",
    "prompt_digest",
    "scoring_input",
    "contexts",
    "model_input_truncated",
    "model_input_omitted_chars",
    "judge_calls",
    "chain_steps",
    "score",
)


class SummaryError(RuntimeError):
    pass


@dataclass
class MetricCell:
    checkpoint_id: str
    metric: str
    status: str
    score: float | None = None
    reason: str | None = None
    error: str | None = None


@dataclass
class ScenarioAggregate:
    scenario_id: str
    state: str
    expected_events: list[str]
    expected_checkpoints: list[str]
    expected_metrics: dict[str, list[str]]
    cells: dict[tuple[str, str], MetricCell] = field(default_factory=dict)
    observed_events: list[str] = field(default_factory=list)
    observed_checkpoints: set[tuple[str, str]] = field(default_factory=set)
    assertions_expected: int = 0
    assertions_observed: int = 0
    assertions_failed: int = 0
    assertions_missing: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    artifact: ScenarioArtifact | None = None


@dataclass
class RunLayout:
    run_dir: Path
    run: dict[str, Any]
    plan: ExecutionPlan
    dataset: EvaluationDataset
    dataset_scenarios: dict[str, dict[str, Any]]
    full_plan: ExecutionPlan
    aggregates: dict[str, ScenarioAggregate]
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _add(items: list[str], message: str) -> None:
    if message not in items:
        items.append(message)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _resolve(path: str | Path) -> Path:
    value = Path(path)
    return value if value.is_absolute() else BACKEND.parent / value


def _dataset_directory(run: dict[str, Any]) -> Path:
    dataset = run.get("dataset")
    if not isinstance(dataset, dict) or not dataset.get("version_dir"):
        raise SummaryError("run.json 缺少数据版本目录")
    return _resolve(dataset["version_dir"])


def _jsonl(path: Path) -> list[Any]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            raise SummaryError(f"{path.name} 第 {number} 行为空")
        rows.append(json.loads(line))
    return rows


def _model_rows(path: Path, model) -> list[Any]:
    return [model.model_validate(row) for row in _jsonl(path)]


def _verify_jsonl(
    scenario_root: Path,
    name: str,
    expected: Sequence[Any],
    model,
    issues: list[str],
    prefix: str,
) -> None:
    path = scenario_root / name
    if not path.exists():
        if expected:
            _add(issues, f"{prefix}:缺少 {name}")
        return
    try:
        rows = _model_rows(path, model)
    except Exception as error:
        _add(issues, f"{prefix}:{name} 无法解析: {type(error).__name__}: {error}")
        return
    if [row.model_dump(mode="json") for row in rows] != [
        row.model_dump(mode="json") for row in expected
    ]:
        _add(issues, f"{prefix}:{name} 与场景产物不一致")


def _event_prefix(
    plan: ScenarioPlan, artifact: ScenarioArtifact, state: str, issues: list[str], prefix: str
) -> None:
    observed = [event.event_id for event in artifact.events]
    expected = plan.event_ids[: len(observed)]
    if observed != expected:
        _add(issues, f"{prefix}:事件顺序或身份与执行计划不一致")
    if state == "complete" and len(observed) != len(plan.event_ids):
        _add(issues, f"{prefix}:完成场景的事件覆盖不完整")
    if len(set(observed)) != len(observed):
        _add(issues, f"{prefix}:事件 ID 重复")
    for event in artifact.events:
        if event.scenario_id != plan.scenario_id:
            _add(issues, f"{prefix}:事件 {event.event_id} 的 scenario_id 不一致")
        if event.event_id not in plan.event_types:
            _add(issues, f"{prefix}:事件 {event.event_id} 不在执行计划中")
        elif event.event_type != plan.event_types[event.event_id]:
            _add(issues, f"{prefix}:事件 {event.event_id} 类型与计划不一致")
        if event.event_type == "message" and event.error is not None:
            _add(issues, f"{prefix}:消息事件 {event.event_id} 执行失败: {event.error}")
        if any(gap.ambiguous for gap in event.context_gaps):
            _add(issues, f"{prefix}:事件 {event.event_id} 存在 ambiguous 上下文")


def _expected_applicability(
    scenario: dict[str, Any], issues: list[str], prefix: str
) -> tuple[dict[tuple[str, str], dict], dict[str, str]]:
    applicability: dict[tuple[str, str], dict] = {}
    event_by_checkpoint: dict[str, str] = {}
    for row in scenario["metric_applicability"]:
        if row["metric"] not in GENERATION_METRICS:
            continue
        key = (row["checkpoint_id"], row["metric"])
        if key in applicability:
            _add(issues, f"{prefix}:检查点适用性重复 {key[0]}/{key[1]}")
        applicability[key] = row
        event_by_checkpoint.setdefault(row["checkpoint_id"], row["event_id"])
        if event_by_checkpoint[row["checkpoint_id"]] != row["event_id"]:
            _add(issues, f"{prefix}:检查点 {row['checkpoint_id']} 跨事件")
    return applicability, event_by_checkpoint


def _validate_assertions(
    scenario: dict[str, Any],
    plan: ScenarioPlan,
    artifact: ScenarioArtifact,
    state: str,
    run_id: str,
    issues: list[str],
    prefix: str,
) -> tuple[int, int, int, list[str]]:
    expected = Counter(
        (row["checkpoint_id"], row["event_id"], row["kind"])
        for row in scenario["assertions"]
    )
    observed = Counter()
    failed = 0
    for checkpoint in artifact.assertions:
        if checkpoint.checkpoint_id not in plan.checkpoint_ids:
            _add(issues, f"{prefix}:断言检查点不在计划中 {checkpoint.checkpoint_id}")
        if checkpoint.event_id not in plan.event_ids:
            _add(issues, f"{prefix}:断言事件不在计划中 {checkpoint.event_id}")
        if checkpoint.binding_error is not None:
            _add(issues, f"{prefix}:断言绑定失败 {checkpoint.checkpoint_id}: {checkpoint.binding_error}")
        if checkpoint.runner_error is not None:
            _add(issues, f"{prefix}:断言装配失败 {checkpoint.checkpoint_id}: {checkpoint.runner_error}")
        for outcome in checkpoint.outcomes:
            link = outcome.checkpoint
            if (
                link.run_id != run_id
                or link.scenario_id != plan.scenario_id
                or link.event_id != checkpoint.event_id
                or link.checkpoint_id != checkpoint.checkpoint_id
            ):
                _add(issues, f"{prefix}:断言证据身份错绑 {checkpoint.checkpoint_id}/{outcome.kind}")
            observed[(checkpoint.checkpoint_id, checkpoint.event_id, outcome.kind)] += 1
            if not outcome.passed:
                failed += 1
                _add(issues, f"{prefix}:断言失败 {checkpoint.checkpoint_id}/{outcome.kind}")
    missing = []
    for key in (expected - observed):
        missing.append("/".join(key))
        _add(issues, f"{prefix}:断言未执行 {'/'.join(key)}")
    for key in (observed - expected):
        _add(issues, f"{prefix}:断言结果不在冻结定义中 {'/'.join(key)}")
    if state == "complete" and expected != observed:
        _add(issues, f"{prefix}:完成场景的断言覆盖不完整")
    return sum(expected.values()), sum(observed.values()), failed, missing


def _scoring_input_matches(
    evidence: GenerationMetricEvidence, checkpoint, issues: list[str], prefix: str
) -> None:
    scoring_input = evidence.scoring_input
    if scoring_input.response != checkpoint.answer.visible_text:
        _add(issues, f"{prefix}:评分输入回答与实际回答不一致")
    if evidence.metric == "faithfulness":
        contexts = [item.content for item in checkpoint.model_input.items]
        if scoring_input.user_input != checkpoint.user_input.message:
            _add(issues, f"{prefix}:忠实性评分输入缺少当前用户输入")
        if scoring_input.retrieved_contexts != contexts:
            _add(issues, f"{prefix}:忠实性评分输入上下文与实际上下文不一致")
        if scoring_input.reference is not None:
            _add(issues, f"{prefix}:忠实性评分输入不得携带 reference")
        if evidence.contexts != checkpoint.model_input.items:
            _add(issues, f"{prefix}:忠实性证据上下文与检查点不一致")
    else:
        if checkpoint.reference is None:
            _add(issues, f"{prefix}:事实正确性缺少审阅 reference")
        elif scoring_input.reference != checkpoint.reference.answer:
            _add(issues, f"{prefix}:事实正确性评分输入与 reference 不一致")
        if scoring_input.user_input is not None or scoring_input.retrieved_contexts is not None:
            _add(issues, f"{prefix}:事实正确性评分输入不得携带用户输入或上下文")
        if evidence.contexts:
            _add(issues, f"{prefix}:事实正确性证据不得携带上下文")


def _validate_attempt(
    attempts: Sequence[ScoringAttemptSnapshot],
    metric: str,
    checkpoint_link,
    expected_status: str,
    score: float | None,
    issues: list[str],
    prefix: str,
) -> None:
    selected = [
        attempt
        for attempt in attempts
        if attempt.metric == metric and attempt.checkpoint == checkpoint_link
    ]
    if len(selected) != 1:
        _add(issues, f"{prefix}:{metric} 必须有唯一一次真实评分尝试，实际 {len(selected)}")
        return
    attempt = selected[0]
    if attempt.status != expected_status:
        _add(issues, f"{prefix}:{metric} 尝试状态与结果不一致")
    if expected_status == "succeeded" and attempt.score != score:
        _add(issues, f"{prefix}:{metric} 尝试分数与结果不一致")
    if expected_status == "failed" and attempt.error is None:
        _add(issues, f"{prefix}:{metric} 失败尝试缺少错误依据")


def _validate_generation(
    scenario: dict[str, Any],
    plan: ScenarioPlan,
    artifact: ScenarioArtifact,
    state: str,
    run_id: str,
    issues: list[str],
    prefix: str,
    all_evidence: list[GenerationMetricEvidence],
    all_attempt_models: set[str],
) -> None:
    applicability, event_by_checkpoint = _expected_applicability(
        scenario, issues, prefix
    )
    if artifact.generation_metrics != plan.generation_metrics:
        _add(issues, f"{prefix}:场景生成指标声明与执行计划不一致")
    rows: dict[str, GenerationCheckpointArtifact] = {}
    for row in artifact.generation:
        if row.checkpoint_id in rows:
            _add(issues, f"{prefix}:生成检查点产物重复 {row.checkpoint_id}")
            continue
        rows[row.checkpoint_id] = row
        if row.event_id != event_by_checkpoint.get(row.checkpoint_id):
            _add(issues, f"{prefix}:生成检查点事件错绑 {row.checkpoint_id}")
        if row.runner_error is not None:
            _add(issues, f"{prefix}:生成评分执行失败 {row.checkpoint_id}: {row.runner_error}")
        if row.incomplete_metrics:
            _add(issues, f"{prefix}:生成指标未完成 {row.checkpoint_id}/{','.join(row.incomplete_metrics)}")
        if row.checkpoint is not None:
            link = row.checkpoint.checkpoint
            if (
                link.run_id != run_id
                or link.scenario_id != plan.scenario_id
                or link.event_id != row.event_id
                or link.checkpoint_id != row.checkpoint_id
            ):
                _add(issues, f"{prefix}:生成检查点身份错绑 {row.checkpoint_id}")
        if row.result is None:
            _add(issues, f"{prefix}:生成检查点缺少结果 {row.checkpoint_id}")
            continue
        result_link = row.result.checkpoint
        if (
            result_link.run_id != run_id
            or result_link.scenario_id != plan.scenario_id
            or result_link.event_id != row.event_id
            or result_link.checkpoint_id != row.checkpoint_id
        ):
            _add(issues, f"{prefix}:生成结果身份错绑 {row.checkpoint_id}")
    for checkpoint_id, metrics in plan.generation_metrics.items():
        row = rows.get(checkpoint_id)
        result = None if row is None else row.result
        outcomes = {} if result is None else {item.metric: item for item in result.outcomes}
        if result is not None and len(outcomes) != len(result.outcomes):
            _add(issues, f"{prefix}:检查点指标结果重复 {checkpoint_id}")
        for metric in metrics:
            expected = applicability.get((checkpoint_id, metric))
            if expected is None:
                _add(issues, f"{prefix}:缺少冻结适用性 {checkpoint_id}/{metric}")
            outcome = outcomes.get(metric)
            if outcome is None:
                _add(issues, f"{prefix}:生成指标未执行 {checkpoint_id}/{metric}")
                if row is not None and row.checkpoint is not None:
                    _add(issues, f"{prefix}:生成指标缺少真实结果 {checkpoint_id}/{metric}")
                continue
            if expected is not None and not expected["applicable"]:
                if outcome.status != "not_applicable":
                    _add(issues, f"{prefix}:不适用指标出现结果 {checkpoint_id}/{metric}")
                elif outcome.reason != expected["reason"]:
                    _add(issues, f"{prefix}:不适用原因偏离冻结记录 {checkpoint_id}/{metric}")
                continue
            if expected is not None and outcome.status == "not_applicable":
                _add(issues, f"{prefix}:适用指标被标为不适用 {checkpoint_id}/{metric}")
                continue
            if outcome.status == "applicable":
                _add(issues, f"{prefix}:适用指标尚无结果 {checkpoint_id}/{metric}")
                continue
            if outcome.status not in {"succeeded", "failed"}:
                _add(issues, f"{prefix}:未知指标状态 {checkpoint_id}/{metric}/{outcome.status}")
                continue
            if row is None or row.checkpoint is None:
                _add(issues, f"{prefix}:适用指标缺少真实检查点 {checkpoint_id}/{metric}")
                continue
            if outcome.status == "failed":
                _validate_attempt(
                    row.attempts,
                    metric,
                    row.checkpoint.checkpoint,
                    "failed",
                    None,
                    issues,
                    f"{prefix}/{checkpoint_id}/{metric}",
                )
                continue
            matches = [
                evidence
                for evidence in row.evidence
                if evidence.metric == metric
                and evidence.checkpoint == row.checkpoint.checkpoint
            ]
            if len(matches) != 1:
                _add(issues, f"{prefix}:成功指标必须唯一关联评分证据 {checkpoint_id}/{metric}")
                continue
            evidence = matches[0]
            if evidence.error is not None:
                _add(issues, f"{prefix}:成功指标关联了失败证据 {checkpoint_id}/{metric}")
            if evidence.score != outcome.score:
                _add(issues, f"{prefix}:结果分数与证据分数不一致 {checkpoint_id}/{metric}")
            _scoring_input_matches(
                evidence,
                row.checkpoint,
                issues,
                f"{prefix}/{checkpoint_id}/{metric}",
            )
            _validate_attempt(
                row.attempts,
                metric,
                row.checkpoint.checkpoint,
                "succeeded",
                outcome.score,
                issues,
                f"{prefix}/{checkpoint_id}/{metric}",
            )
            selected = [
                attempt
                for attempt in row.attempts
                if attempt.metric == metric
                and attempt.checkpoint == row.checkpoint.checkpoint
            ]
            if len(selected) == 1:
                for field_name in _SNAPSHOT_FIELDS:
                    if getattr(evidence, field_name) != getattr(selected[0], field_name):
                        _add(
                            issues,
                            f"{prefix}:成功证据与评分尝试不一致 "
                            f"{checkpoint_id}/{metric}/{field_name}",
                        )
        for outcome in outcomes.values():
            if outcome.metric not in metrics:
                _add(
                    issues,
                    f"{prefix}:生成结果包含计划外指标 {checkpoint_id}/{outcome.metric}",
                )
    for row in artifact.generation:
        link = None if row.checkpoint is None else row.checkpoint.checkpoint
        succeeded = {
            outcome.metric
            for outcome in ([] if row.result is None else row.result.outcomes)
            if outcome.status == "succeeded"
        }
        for evidence in row.evidence:
            if evidence.error is not None:
                _add(issues, f"{prefix}:评分证据包含错误 {row.checkpoint_id}/{evidence.metric}")
            if (
                link is None
                or evidence.checkpoint != link
                or evidence.metric not in succeeded
            ):
                _add(
                    issues,
                    f"{prefix}:评分证据未绑定成功结果 {row.checkpoint_id}/{evidence.metric}",
                )
            all_evidence.append(evidence)
        for attempt in row.attempts:
            all_attempt_models.add(attempt.judge_model)


def _validate_retrieval(
    plan: ScenarioPlan,
    artifact: ScenarioArtifact,
    run_id: str,
    issues: list[str],
    prefix: str,
    records: list[SearchStageRecord],
) -> None:
    sequences = [record.sequence for record in artifact.retrieval]
    if sequences != list(range(len(sequences))):
        _add(issues, f"{prefix}:检索序号不连续")
    for record in artifact.retrieval:
        records.append(record)
        if record.run_id != run_id:
            _add(issues, f"{prefix}:检索记录 run_id 不一致 {record.sequence}")
        if record.scenario_id != plan.scenario_id:
            _add(issues, f"{prefix}:检索记录 scenario_id 不一致 {record.sequence}")
        if record.event_id not in plan.event_ids:
            _add(issues, f"{prefix}:检索记录事件不在计划中 {record.event_id}")
        if not record.index_collection.strip():
            _add(issues, f"{prefix}:检索记录缺少物理索引集合")
        if sha256_text(record.index_manifest_json) != record.index_manifest_sha256:
            _add(issues, f"{prefix}:检索索引清单摘要不一致 {record.sequence}")
        if len(record.candidate_pool) > CANDIDATE_POOL_LIMIT:
            _add(issues, f"{prefix}:候选池超过 {CANDIDATE_POOL_LIMIT} 条")
        if len(record.final_model_input) > SEARCH_LIMIT:
            _add(issues, f"{prefix}:最终输入超过 {SEARCH_LIMIT} 条")
        if len(set(record.candidate_pool)) != len(record.candidate_pool):
            _add(issues, f"{prefix}:候选池动作 ID 重复")
        if len(set(record.final_model_input)) != len(record.final_model_input):
            _add(issues, f"{prefix}:最终输入动作 ID 重复")
        if not set(record.final_model_input) <= set(record.candidate_pool):
            _add(issues, f"{prefix}:最终输入不是候选池子集")
        if len(record.scores) != len(record.candidate_pool):
            _add(issues, f"{prefix}:候选分数数量与候选池不一致")
        if any(
            type(value) not in (int, float) or not math.isfinite(value)
            for value in record.scores
        ):
            _add(issues, f"{prefix}:候选分数非有限值")
        response = record.response_json
        if set(response) != {"items", "has_more"}:
            _add(issues, f"{prefix}:公开检索响应字段非法")
            continue
        items = response["items"]
        if (
            not isinstance(items, list)
            or not isinstance(response["has_more"], bool)
            or any(not isinstance(item, dict) for item in items)
        ):
            _add(issues, f"{prefix}:公开检索响应结构非法")
            continue
        if [item.get("id") for item in items] != record.final_model_input:
            _add(issues, f"{prefix}:公开响应与最终模型输入不一致")
        if len(items) > SEARCH_LIMIT:
            _add(issues, f"{prefix}:公开响应超过 {SEARCH_LIMIT} 条")
        try:
            chars = encoded_length(response)
        except (TypeError, ValueError):
            _add(issues, f"{prefix}:公开响应无法计算字符预算")
        else:
            if chars > SEARCH_BUDGET:
                _add(issues, f"{prefix}:公开响应超出字符预算")


def _read_artifact(
    scenario_root: Path, scenario_id: str, issues: list[str], prefix: str
) -> tuple[str, ScenarioArtifact | None]:
    complete = scenario_root / "scenario.json"
    partial = scenario_root / "partial.json"
    blocked = scenario_root / "blocked.json"
    failure = scenario_root / "failure.json"
    if complete.exists() and partial.exists():
        _add(issues, f"{prefix}:同时存在完整产物与部分产物")
    path = complete if complete.exists() else partial
    if not path.exists():
        return "missing", None
    try:
        artifact = ScenarioArtifact.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as error:
        _add(issues, f"{prefix}:场景产物无法解析 {type(error).__name__}: {error}")
        return "invalid", None
    if artifact.scenario_id != scenario_id:
        _add(issues, f"{prefix}:场景产物 scenario_id 不一致")
    if complete.exists():
        if blocked.exists() or failure.exists():
            _add(issues, f"{prefix}:完整产物同时带失败标记")
        return "complete", artifact
    if blocked.exists():
        return "blocked", artifact
    if failure.exists():
        return "failed", artifact
    _add(issues, f"{prefix}:部分产物缺少 blocked/failure 标记")
    return "unmarked", artifact


def _validate_context_projection(
    checkpoint,
    boundary: RequestBoundaryEvidence | None,
    event: EventCapture,
    request: dict[str, Any],
    issues: list[str],
    prefix: str,
    checkpoint_id: str,
) -> None:
    # 沿被评分请求的真实节点身份重放生产映射：来源、当前输入与工具身份必须一致。
    if boundary is None:
        _add(issues, f"{prefix}:缺少请求节点身份证据 {checkpoint_id}")
        return
    if (
        boundary.session_id != event.session_id
        or boundary.run_id != event.run_id
        or boundary.request_entry_id != event.request_entry_id
    ):
        _add(issues, f"{prefix}:请求节点身份与事件边界不一致 {checkpoint_id}")
        return
    current_ids = current_input_entry_ids(
        boundary.nodes, boundary.run_id, boundary.request_entry_id
    )
    try:
        mapping = context_items_from_request(
            request,
            api=boundary.api,
            session_id=boundary.session_id,
            current_entry_ids=current_ids,
            nodes=boundary.nodes,
            tool_nodes=boundary.tool_nodes,
        )
    except ContextMappingError as error:
        _add(issues, f"{prefix}:请求节点无法重放上下文映射 {checkpoint_id}: {error}")
        return
    if mapping.ambiguous:
        _add(issues, f"{prefix}:请求节点存在无法绑定的上下文 {checkpoint_id}")
        return
    if mapping.tool_identity_map != event.tool_identity_map:
        _add(issues, f"{prefix}:请求工具身份与事件证据不一致 {checkpoint_id}")
    expected_items = mapping.items
    actual_items = checkpoint.model_input.items
    if len(expected_items) != len(actual_items):
        _add(issues, f"{prefix}:模型输入上下文条目数量与真实请求不一致 {checkpoint_id}")
    else:
        for expected, actual in zip(expected_items, actual_items, strict=True):
            if expected.content != actual.content:
                _add(issues, f"{prefix}:模型输入上下文顺序或正文与真实请求不一致 {checkpoint_id}")
                break
            if expected != actual:
                _add(issues, f"{prefix}:模型输入上下文来源或身份与真实请求不一致 {checkpoint_id}")
                break
    if checkpoint.user_input.message != _current_input_text(
        mapping.items, current_ids
    ):
        _add(issues, f"{prefix}:检查点当前输入与请求上下文不一致 {checkpoint_id}")


def _answer_model_call(event: EventCapture):
    # 与运行侧 _answer_call 一致：同一回答消息优先取最后一次正常结束的真实调用。
    if not event.answer_message_id:
        return None
    matching = [
        call for call in event.model_calls if call.message_id == event.answer_message_id
    ]
    if not matching:
        return None
    stopped = [call for call in matching if call.status == "stop"]
    return stopped[-1] if stopped else matching[-1]


def _read_request_file(
    calls_root: Path, call, issues: list[str], prefix: str, checkpoint_id: str
) -> dict[str, Any] | None:
    relative = PurePosixPath(call.request_path)
    if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != ("requests",):
        _add(issues, f"{prefix}:模型请求证据路径非法 {checkpoint_id}/{call.request_path}")
        return None
    path = calls_root / call.request_path
    if not path.is_file():
        _add(issues, f"{prefix}:模型请求证据文件缺失 {checkpoint_id}/{call.request_path}")
        return None
    text = path.read_text(encoding="utf-8")
    if sha256_text(text) != call.request_sha256:
        _add(issues, f"{prefix}:模型请求证据摘要不一致 {checkpoint_id}")
        return None
    payload = json.loads(text)
    if not isinstance(payload, dict):
        _add(issues, f"{prefix}:模型请求证据不是合法对象 {checkpoint_id}")
        return None
    return payload


def _frozen_applicability(scenario: dict[str, Any], checkpoint_id: str) -> list[dict[str, Any]]:
    return [
        {
            "metric": row["metric"],
            "applicable": row["applicable"],
            "reason": row["reason"],
        }
        for row in scenario["metric_applicability"]
        if row["checkpoint_id"] == checkpoint_id and row["metric"] in GENERATION_METRICS
    ]


def _validate_checkpoint_sources(
    layout: RunLayout,
    plan: ScenarioPlan,
    scenario: dict[str, Any],
    artifact: ScenarioArtifact,
    issues: list[str],
    prefix: str,
) -> None:
    events = {event.event_id: event for event in artifact.events}
    frozen_events = {row["event_id"]: row for row in scenario["events"]}
    identity_model = layout.run.get("identity", {}).get("generation", {}).get("model")
    parsed_model: Any = {}
    if isinstance(identity_model, str):
        try:
            parsed_model = json.loads(identity_model)
        except ValueError:
            parsed_model = {}
    declared = parsed_model if isinstance(parsed_model, dict) else {}
    calls_root = layout.run_dir / plan.scenario_id / "model-calls"
    for row in artifact.generation:
        checkpoint = row.checkpoint
        if checkpoint is None:
            continue
        checkpoint_id = row.checkpoint_id
        event = events.get(row.event_id)
        if event is None:
            _add(issues, f"{prefix}:生成检查点缺少事件捕获 {checkpoint_id}")
            continue
        if event.answer_text is None or event.answer_text != checkpoint.answer.visible_text:
            _add(issues, f"{prefix}:检查点回答与事件捕获不一致 {checkpoint_id}")
        if event.answer_structured != checkpoint.answer.structured_snapshot:
            _add(issues, f"{prefix}:检查点结构化回答与事件捕获不一致 {checkpoint_id}")
        frozen = (frozen_events.get(row.event_id, {}).get("content") or "").strip()
        if frozen and frozen not in checkpoint.user_input.message:
            _add(issues, f"{prefix}:检查点当前输入缺少冻结事件正文 {checkpoint_id}")
        expected_prior = [
            {"role": item["role"], "content": item["content"]}
            for item in scenario["input"]["prior_messages"]
        ]
        observed_prior = [
            {"role": item.role, "content": item.content}
            for item in checkpoint.user_input.prior_messages
        ]
        if observed_prior != expected_prior:
            _add(issues, f"{prefix}:检查点历史输入偏离冻结数据 {checkpoint_id}")
        expected_applicability = _frozen_applicability(scenario, checkpoint_id)
        observed_applicability = [
            {
                "metric": item.metric,
                "applicable": item.applicable,
                "reason": item.reason,
            }
            for item in checkpoint.applicability
        ]
        if sorted(map(_canonical, expected_applicability)) != sorted(
            map(_canonical, observed_applicability)
        ):
            _add(issues, f"{prefix}:检查点适用性偏离冻结数据 {checkpoint_id}")
        if any(item["applicable"] for item in expected_applicability):
            if checkpoint.reference != generation_reference(scenario, checkpoint_id):
                _add(issues, f"{prefix}:检查点 reference 与冻结数据不一致 {checkpoint_id}")
        call = _answer_model_call(event)
        if call is None:
            _add(issues, f"{prefix}:检查点回答未关联真实模型调用 {checkpoint_id}")
            continue
        request = _read_request_file(calls_root, call, issues, prefix, checkpoint_id)
        if request is None:
            continue
        if (call.api, call.provider, call.model) != (
            declared.get("api"),
            declared.get("provider"),
            declared.get("model"),
        ):
            _add(issues, f"{prefix}:检查点模型调用与生成身份不一致 {checkpoint_id}")
        if row.request_boundary is not None and row.request_boundary.api != call.api:
            _add(issues, f"{prefix}:请求节点身份与模型调用不一致 {checkpoint_id}")
        _validate_context_projection(
            checkpoint,
            row.request_boundary,
            event,
            request,
            issues,
            prefix,
            checkpoint_id,
        )


def _validate_scenario(
    layout: RunLayout,
    plan: ScenarioPlan,
    state: str,
    artifact: ScenarioArtifact | None,
    evidence: list[GenerationMetricEvidence],
    attempt_models: set[str],
    retrieval_records: list[SearchStageRecord],
) -> ScenarioAggregate:
    scenario = layout.dataset_scenarios[plan.scenario_id]
    aggregate = ScenarioAggregate(
        scenario_id=plan.scenario_id,
        state=state,
        expected_events=list(plan.event_ids),
        expected_checkpoints=list(plan.checkpoint_ids),
        expected_metrics={
            checkpoint_id: list(metrics)
            for checkpoint_id, metrics in plan.generation_metrics.items()
        },
        artifact=artifact,
    )
    prefix = f"scenario:{plan.scenario_id}"
    if artifact is None:
        _add(aggregate.issues, f"{prefix}:缺少场景产物")
        aggregate.assertions_missing = [
            f"{row['checkpoint_id']}/{row['event_id']}/{row['kind']}"
            for row in scenario["assertions"]
        ]
        return aggregate
    run_id = layout.plan.run_id
    if artifact.run_id != run_id:
        _add(aggregate.issues, f"{prefix}:场景 run_id 不一致")
    if artifact.blocked is not None:
        _add(aggregate.issues, f"{prefix}:场景 blocked: {artifact.blocked}")
    if artifact.runner_error is not None:
        _add(aggregate.issues, f"{prefix}:场景执行失败: {artifact.runner_error}")
    if state != "complete":
        _add(aggregate.issues, f"{prefix}:场景状态为 {state}")
    _event_prefix(plan, artifact, state, aggregate.issues, prefix)
    aggregate.observed_events = [event.event_id for event in artifact.events]
    _verify_jsonl(
        layout.run_dir / plan.scenario_id,
        "events.jsonl",
        artifact.events,
        EventCapture,
        aggregate.issues,
        prefix,
    )
    _verify_jsonl(
        layout.run_dir / plan.scenario_id,
        "assertions.jsonl",
        artifact.assertions,
        AssertionCheckpointArtifact,
        aggregate.issues,
        prefix,
    )
    _verify_jsonl(
        layout.run_dir / plan.scenario_id,
        "generation.jsonl",
        artifact.generation,
        GenerationCheckpointArtifact,
        aggregate.issues,
        prefix,
    )
    _verify_jsonl(
        layout.run_dir / plan.scenario_id,
        "retrieval.jsonl",
        artifact.retrieval,
        SearchStageRecord,
        aggregate.issues,
        prefix,
    )
    context_path = layout.run_dir / plan.scenario_id / "context.jsonl"
    if context_path.exists():
        try:
            context_rows = _jsonl(context_path)
        except Exception as error:
            _add(aggregate.issues, f"{prefix}:context.jsonl 无法解析: {type(error).__name__}: {error}")
            context_rows = []
        expected_context = [
            {
                "event_id": event.event_id,
                "tool_identity_map": event.tool_identity_map,
                "context_gaps": [
                    gap.model_dump(mode="json") for gap in event.context_gaps
                ],
            }
            for event in artifact.events
        ]
        if context_rows != expected_context:
            _add(aggregate.issues, f"{prefix}:context.jsonl 与事件证据不一致")
    elif artifact.events:
        _add(aggregate.issues, f"{prefix}:缺少 context.jsonl")
    (
        aggregate.assertions_expected,
        aggregate.assertions_observed,
        aggregate.assertions_failed,
        aggregate.assertions_missing,
    ) = _validate_assertions(
        scenario, plan, artifact, state, run_id, aggregate.issues, prefix
    )
    _validate_generation(
        scenario,
        plan,
        artifact,
        state,
        run_id,
        aggregate.issues,
        prefix,
        evidence,
        attempt_models,
    )
    _validate_checkpoint_sources(layout, plan, scenario, artifact, aggregate.issues, prefix)
    _validate_retrieval(
        plan, artifact, run_id, aggregate.issues, prefix, retrieval_records
    )
    for row in artifact.generation:
        if row.result is None:
            continue
        for outcome in row.result.outcomes:
            checkpoint_id = row.checkpoint_id
            aggregate.cells[(checkpoint_id, outcome.metric)] = MetricCell(
                checkpoint_id=checkpoint_id,
                metric=outcome.metric,
                status=outcome.status,
                score=outcome.score,
                reason=outcome.reason,
                error=outcome.error,
            )
            aggregate.observed_checkpoints.add((row.event_id, checkpoint_id))
    for checkpoint in artifact.assertions:
        if checkpoint.outcomes:
            aggregate.observed_checkpoints.add(
                (checkpoint.event_id, checkpoint.checkpoint_id)
            )
    return aggregate


def _compare_dataset(run: dict[str, Any], dataset: EvaluationDataset, issues: list[str]) -> None:
    recorded = run.get("dataset")
    if not isinstance(recorded, dict):
        _add(issues, "identity:run.json 缺少数据身份")
        return
    actual = dataset.identity()
    for key in ("version", "manifest_sha256", "content_sha256", "corpus_sha256"):
        if recorded.get(key) != actual[key]:
            _add(issues, f"identity:数据身份不一致 {key}")


def _compare_plan(layout: RunLayout) -> None:
    expected = build_execution_plan(
        layout.dataset,
        [
            scenario
            for scenario in layout.dataset.scenarios
            if scenario["scenario_id"] in {
                item.scenario_id for item in layout.plan.scenarios
            }
        ],
        layout.plan.run_id,
    )
    if layout.plan != expected:
        _add(layout.issues, "coverage:执行计划与冻结数据不一致")
    full = build_execution_plan(
        layout.dataset, list(layout.dataset.scenarios), layout.plan.run_id
    )
    if layout.plan != full:
        _add(
            layout.warnings,
            "scope:定向验证范围，仅用于诊断，不能作为有效发布",
        )
        for item in layout.plan.scenarios:
            if item.scenario_id not in layout.dataset_scenarios:
                _add(layout.issues, f"coverage:未知场景 {item.scenario_id}")


def load_run(run_dir: Path, version_dir: Path | None = None) -> RunLayout:
    run_dir = Path(run_dir).resolve()
    if not run_dir.is_dir():
        raise SummaryError(f"运行目录不存在: {run_dir}")
    run_path = run_dir / "run.json"
    plan_path = run_dir / "plan.json"
    if not run_path.is_file() or not plan_path.is_file():
        raise SummaryError("运行目录缺少 run.json 或 plan.json")
    run = _read_json(run_path)
    plan = ExecutionPlan.model_validate(_read_json(plan_path))
    if run.get("run_id") != plan.run_id:
        raise SummaryError("run.json 与 plan.json 的 run_id 不一致")
    dataset = load_for_evaluation(
        Path(version_dir).resolve() if version_dir is not None else _dataset_directory(run)
    )
    dataset_scenarios = {
        scenario["scenario_id"]: scenario for scenario in dataset.scenarios
    }
    layout = RunLayout(
        run_dir=run_dir,
        run=run,
        plan=plan,
        dataset=dataset,
        dataset_scenarios=dataset_scenarios,
        full_plan=build_execution_plan(
            dataset, list(dataset.scenarios), plan.run_id
        ),
        aggregates={},
    )
    _compare_dataset(run, dataset, layout.issues)
    _compare_plan(layout)
    planned = {item.scenario_id for item in plan.scenarios}
    for child in sorted(run_dir.iterdir()):
        if child.is_dir() and child.name not in planned:
            _add(layout.issues, f"coverage:未登记的场景目录 {child.name}")
    evidence: list[GenerationMetricEvidence] = []
    attempt_models: set[str] = set()
    retrieval_records: list[SearchStageRecord] = []
    for plan_item in plan.scenarios:
        scenario_root = run_dir / plan_item.scenario_id
        state, artifact = _read_artifact(
            scenario_root, plan_item.scenario_id, layout.issues, f"scenario:{plan_item.scenario_id}"
        )
        layout.aggregates[plan_item.scenario_id] = _validate_scenario(
            layout,
            plan_item,
            state,
            artifact,
            evidence,
            attempt_models,
            retrieval_records,
        )
    layout.run["_all_evidence"] = evidence
    layout.run["_attempt_models"] = attempt_models
    layout.run["_retrieval_records"] = retrieval_records
    _validate_identity(layout, evidence, attempt_models, retrieval_records)
    return layout


def _validate_identity(
    layout: RunLayout,
    evidence: Sequence[GenerationMetricEvidence],
    attempt_models: Iterable[str],
    retrieval_records: Sequence[SearchStageRecord],
) -> None:
    run = layout.run
    identity = run.get("identity")
    if not isinstance(identity, dict):
        _add(layout.issues, "identity:缺少运行身份")
        return
    for key in ("dataset", "code", "retrieval", "generation", "judge"):
        if key not in identity:
            _add(layout.issues, f"identity:缺少 {key} 身份")
    missing = identity.get("missing")
    if missing:
        _add(layout.issues, f"identity:运行身份缺失 {','.join(sorted(set(missing)))}")
    if identity.get("status") != "complete":
        _add(layout.issues, "identity:运行身份未完成")
    incomplete = identity.get("incomplete")
    if incomplete:
        _add(layout.issues, f"identity:运行仍不完整 {','.join(sorted(set(incomplete)))}")
    code = identity.get("code")
    if isinstance(code, dict):
        current = code_identity()
        for key in ("commit", "lock_sha256", "source_sha256"):
            if code.get(key) != current.get(key):
                _add(layout.issues, f"identity:运行代码身份与当前工作区不一致 {key}")
    generation = identity.get("generation")
    if isinstance(generation, dict):
        model = generation.get("model")
        if not isinstance(model, str) or not model.strip():
            _add(layout.issues, "identity:缺少生成模型身份")
        else:
            try:
                parsed = json.loads(model)
            except ValueError:
                _add(layout.issues, "identity:生成模型身份不是合法 JSON")
            else:
                for key in ("api", "provider", "model"):
                    value = parsed.get(key)
                    if not isinstance(value, str) or not value.strip():
                        _add(layout.issues, f"identity:生成模型身份缺少 {key}")
        prompt_version = generation.get("prompt_version")
        if not isinstance(prompt_version, str) or not prompt_version.strip():
            _add(layout.issues, "identity:缺少生成提示身份")
    judge = identity.get("judge")
    if isinstance(judge, dict):
        if not judge.get("model"):
            _add(layout.issues, "identity:缺少裁判模型身份")
        if not judge.get("prompt_version") or not judge.get("metric_version"):
            _add(layout.issues, "identity:缺少裁判提示或指标版本")
    elif judge is not None:
        _add(layout.issues, "identity:裁判身份结构非法")
    if isinstance(generation, dict) and isinstance(judge, dict):
        generation_model = None
        try:
            generation_model = json.loads(generation.get("model", "{}")).get("model")
        except ValueError:
            pass
        fields, missing_fields = judge_attempt_identity(
            attempt_models, generation_model, judge.get("model")
        )
        if missing_fields:
            _add(layout.issues, f"identity:裁判尝试模型冲突 {','.join(missing_fields)}")
        for key, value in fields.items():
            if identity.get(key) != value:
                _add(layout.issues, f"identity:裁判尝试身份不一致 {key}")
    if evidence:
        try:
            computed = run_judge_identity(evidence).model_dump(mode="json")
        except Exception as error:
            _add(layout.issues, f"identity:裁判身份无法生成 {type(error).__name__}: {error}")
        else:
            for key in ("model", "prompt_version", "metric_version"):
                if isinstance(judge, dict) and judge.get(key) != computed.get(key):
                    _add(layout.issues, f"identity:裁判身份与证据不一致 {key}")
        by_metric: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
        judge_models = set()
        for item in evidence:
            by_metric[item.metric].add(
                (item.metric_class, item.metric_version, item.prompt_digest)
            )
            judge_models.add(item.judge_model)
        for metric, values in by_metric.items():
            if len(values) > 1:
                _add(layout.issues, f"identity:同一指标存在多个配置或提示身份 {metric}")
        if len(judge_models) > 1:
            _add(layout.issues, "identity:同一运行存在多个裁判模型身份")
    elif judge is not None:
        _add(layout.issues, "identity:缺少真实裁判评分证据")
    retrieval = identity.get("retrieval")
    if isinstance(retrieval, dict):
        if retrieval.get("index_conflict"):
            _add(layout.issues, "identity:检索物理索引身份冲突")
        if retrieval.get("missing"):
            _add(layout.issues, f"identity:检索身份缺失 {','.join(retrieval['missing'])}")
        index = retrieval.get("index")
        distinct = {
            (record.index_collection, record.index_manifest_sha256)
            for record in retrieval_records
        }
        if len(distinct) > 1:
            _add(layout.issues, "identity:检索记录存在多个物理索引身份")
        elif distinct:
            if not index:
                _add(layout.issues, "identity:检索记录存在但运行身份缺少物理索引")
            else:
                try:
                    recorded = json.loads(index)
                except ValueError:
                    _add(layout.issues, "identity:物理索引身份不是合法 JSON")
                else:
                    collection, manifest_sha256 = next(iter(distinct))
                    if (
                        recorded.get("collection") != collection
                        or recorded.get("manifest_sha256") != manifest_sha256
                    ):
                        _add(layout.issues, "identity:物理索引身份与检索记录不一致")
        elif index:
            _add(layout.issues, "identity:未执行检索但运行身份包含物理索引")


def _aggregate(layout: RunLayout) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for metric in GENERATION_METRICS:
        expected_checkpoints = 0
        succeeded = failed = not_applicable = applicable = missing = 0
        available: list[float] = []
        complete: list[float] = []
        scenario_values: dict[str, Any] = {}
        for aggregate in layout.aggregates.values():
            expected = [
                checkpoint_id
                for checkpoint_id, metrics in aggregate.expected_metrics.items()
                if metric in metrics
            ]
            if not expected:
                continue
            expected_checkpoints += len(expected)
            values = []
            complete_scenario = True
            for checkpoint_id in expected:
                cell = aggregate.cells.get((checkpoint_id, metric))
                if cell is None:
                    missing += 1
                    complete_scenario = False
                elif cell.status == "succeeded":
                    succeeded += 1
                    values.append(cell.score)
                elif cell.status == "failed":
                    failed += 1
                    complete_scenario = False
                elif cell.status == "not_applicable":
                    not_applicable += 1
                elif cell.status == "applicable":
                    applicable += 1
                    complete_scenario = False
                else:
                    missing += 1
                    complete_scenario = False
            if values:
                value = mean(values)
                available.append(value)
                if complete_scenario:
                    complete.append(value)
                scenario_values[aggregate.scenario_id] = {
                    "value": value,
                    "complete": complete_scenario,
                    "succeeded_checkpoints": len(values),
                    "expected_checkpoints": len(expected),
                }
            elif complete_scenario:
                scenario_values[aggregate.scenario_id] = {
                    "value": None,
                    "complete": True,
                    "succeeded_checkpoints": 0,
                    "expected_checkpoints": len(expected),
                }
            else:
                scenario_values[aggregate.scenario_id] = {
                    "value": None,
                    "complete": False,
                    "succeeded_checkpoints": 0,
                    "expected_checkpoints": len(expected),
                }
        metrics[metric] = {
            "expected_checkpoints": expected_checkpoints,
            "succeeded_checkpoints": succeeded,
            "failed_checkpoints": failed,
            "not_applicable_checkpoints": not_applicable,
            "applicable_checkpoints": applicable,
            "missing_checkpoints": missing,
            "available_scenarios": len(available),
            "complete_scenarios": len(complete),
            "mean_including_partial": mean(available) if available else None,
            "mean_complete_only": mean(complete) if complete else None,
            "scenario_values": scenario_values,
        }
    return {
        "aggregation": {
            "within_scenario": "mean_of_succeeded_checkpoints",
            "across_scenarios": "macro_mean",
            "denominators": {
                "mean_including_partial": "scenarios with at least one succeeded checkpoint",
                "mean_complete_only": "scenarios with no failed/applicable/missing checkpoints and at least one succeeded checkpoint",
            },
            "not_applicable": "excluded from denominator; retained as reason",
            "failed": "excluded from denominator; never replaced by zero",
        },
        "metrics": metrics,
    }


def _coverage(layout: RunLayout) -> dict[str, Any]:
    expected_metric_keys = {
        (item.scenario_id, checkpoint_id, metric)
        for item in layout.plan.scenarios
        for checkpoint_id, metrics in item.generation_metrics.items()
        for metric in metrics
    }
    observed_keys = set()
    status_counts: Counter[str] = Counter()
    for aggregate in layout.aggregates.values():
        for (checkpoint_id, metric), cell in aggregate.cells.items():
            key = (aggregate.scenario_id, checkpoint_id, metric)
            if key in expected_metric_keys:
                observed_keys.add(key)
                status_counts[cell.status] += 1
    observed_checkpoints = set()
    observed_events = set()
    for aggregate in layout.aggregates.values():
        observed_events.update(
            (aggregate.scenario_id, event_id)
            for event_id in aggregate.observed_events
        )
        observed_checkpoints.update(
            (aggregate.scenario_id, event_id, checkpoint_id)
            for event_id, checkpoint_id in aggregate.observed_checkpoints
        )
    expected_checkpoints = {
        (item.scenario_id, event_id, checkpoint_id)
        for item in layout.plan.scenarios
        for event_id, checkpoint_id in _checkpoint_events(
            layout.dataset_scenarios[item.scenario_id]
        )
    }
    expected_events = {
        (item.scenario_id, event_id)
        for item in layout.plan.scenarios
        for event_id in item.event_ids
    }
    complete = {
        scenario_id
        for scenario_id, aggregate in layout.aggregates.items()
        if aggregate.state == "complete"
    }
    blocked = [
        scenario_id
        for scenario_id, aggregate in layout.aggregates.items()
        if aggregate.state == "blocked"
    ]
    failed = [
        scenario_id
        for scenario_id, aggregate in layout.aggregates.items()
        if aggregate.state == "failed"
    ]
    unmarked = [
        scenario_id
        for scenario_id, aggregate in layout.aggregates.items()
        if aggregate.state not in {"complete", "blocked", "failed"}
    ]
    return {
        "expected_scenarios": len(layout.plan.scenarios),
        "observed_scenarios": len(complete),
        "blocked_scenarios": sorted(blocked),
        "failed_scenarios": sorted(failed),
        "unmarked_scenarios": sorted(unmarked),
        "missing_scenarios": sorted(
            set(layout.aggregates) - complete - set(blocked) - set(failed)
        ),
        "expected_events": len(expected_events),
        "observed_events": len(observed_events),
        "missing_events": sorted(
            "/".join(item) for item in expected_events - observed_events
        ),
        "expected_checkpoints": len(expected_checkpoints),
        "observed_checkpoints": len(observed_checkpoints),
        "missing_checkpoints": sorted(
            "/".join(item) for item in expected_checkpoints - observed_checkpoints
        ),
        "expected_metrics": len(expected_metric_keys),
        "observed_metrics": len(observed_keys),
        "succeeded_metrics": status_counts["succeeded"],
        "failed_metrics": status_counts["failed"],
        "not_applicable_metrics": status_counts["not_applicable"],
        "applicable_metrics": status_counts["applicable"],
        "missing_metrics": sorted(
            "/".join(item) for item in expected_metric_keys - observed_keys
        ),
        "unexecuted_metrics": len(expected_metric_keys - observed_keys),
    }


def _checkpoint_events(scenario: dict[str, Any]) -> list[tuple[str, str]]:
    pairs = {
        (row["event_id"], row["checkpoint_id"])
        for key in ("reference", "rubric", "assertions", "metric_applicability")
        for row in scenario[key]
    }
    return sorted(pairs)


def _coverage_issues(layout: RunLayout, coverage: dict[str, Any]) -> None:
    if coverage["observed_scenarios"] != coverage["expected_scenarios"]:
        _add(layout.issues, "coverage:场景覆盖不完整")
    if coverage["observed_events"] != coverage["expected_events"]:
        _add(layout.issues, "coverage:事件覆盖不完整")
    if coverage["observed_checkpoints"] != coverage["expected_checkpoints"]:
        _add(layout.issues, "coverage:检查点覆盖不完整")
    if coverage["observed_metrics"] != coverage["expected_metrics"]:
        _add(layout.issues, "coverage:指标覆盖不完整")
    if coverage["failed_metrics"]:
        _add(layout.issues, "coverage:存在失败生成指标")
    if coverage["applicable_metrics"]:
        _add(layout.issues, "coverage:存在尚无结果的适用指标")
    if coverage["missing_metrics"]:
        _add(layout.issues, "coverage:存在未执行生成指标")
    if coverage["blocked_scenarios"]:
        _add(layout.issues, "coverage:存在 blocked 场景")
    if coverage["failed_scenarios"]:
        _add(layout.issues, "coverage:存在 failed 场景")
    if coverage["unmarked_scenarios"]:
        _add(layout.issues, "coverage:存在状态不明的场景")
    recorded = layout.run.get("coverage")
    if not isinstance(recorded, dict):
        _add(layout.issues, "coverage:run.json 缺少覆盖统计")
        return
    comparisons = {
        "expected_scenarios": coverage["expected_scenarios"],
        "observed_scenarios": coverage["observed_scenarios"],
        "expected_events": coverage["expected_events"],
        "observed_events": coverage["observed_events"],
        "expected_checkpoints": coverage["expected_checkpoints"],
        "observed_checkpoints": coverage["observed_checkpoints"],
        "expected_metrics": coverage["expected_metrics"],
        "observed_metrics": coverage["observed_metrics"],
        "succeeded_metrics": coverage["succeeded_metrics"],
        "failed_metrics": coverage["failed_metrics"],
        "not_applicable_metrics": coverage["not_applicable_metrics"],
        "unexecuted_metrics": coverage["unexecuted_metrics"],
    }
    for key, value in comparisons.items():
        if recorded.get(key) != value:
            _add(layout.issues, f"coverage:run.json 覆盖统计不一致 {key}")


def _assertion_summary(layout: RunLayout) -> dict[str, Any]:
    expected = sum(
        len(layout.dataset_scenarios[item.scenario_id]["assertions"])
        for item in layout.plan.scenarios
    )
    observed = sum(
        aggregate.assertions_observed for aggregate in layout.aggregates.values()
    )
    failed = sum(
        aggregate.assertions_failed for aggregate in layout.aggregates.values()
    )
    missing = [
        f"{aggregate.scenario_id}/{item}"
        for aggregate in layout.aggregates.values()
        for item in aggregate.assertions_missing
    ]
    return {
        "expected": expected,
        "observed": observed,
        "failed": failed,
        "missing": missing,
    }


def _retrieval_summary(
    layout: RunLayout, records: Sequence[SearchStageRecord]
) -> dict[str, Any]:
    identity = layout.run.get("identity", {}).get("retrieval", {})
    index = None
    if isinstance(identity, dict) and identity.get("index"):
        try:
            index = json.loads(identity["index"])
        except ValueError:
            index = None
    return {
        "searches": len(records),
        "scenarios_with_search": len(
            {aggregate.scenario_id for aggregate in layout.aggregates.values() if aggregate.artifact and aggregate.artifact.retrieval}
        ),
        "candidate_pool_limit": CANDIDATE_POOL_LIMIT,
        "final_input_limit": SEARCH_LIMIT,
        "character_budget": SEARCH_BUDGET,
        "index": index,
        "index_conflict": identity.get("index_conflict") if isinstance(identity, dict) else None,
        "missing": identity.get("missing", []) if isinstance(identity, dict) else [],
        "metrics": {
            "recall@20": "candidate_pool",
            "hit@5": "final_model_input",
            "mrr@5": "final_model_input",
            "ndcg@5": "final_model_input",
            "precision@5": "final_model_input",
            "false_return": "final_model_input",
        },
    }


def _scope(layout: RunLayout, requested: str) -> tuple[str, bool]:
    formal = layout.plan == layout.full_plan
    if requested == "formal":
        if not formal:
            _add(layout.issues, "scope:定向验证范围不能作为正式发布")
        return "formal", formal
    if requested == "validation":
        _add(layout.issues, "scope:定向验证范围仅用于诊断")
        return "validation", False
    return ("formal" if formal else "validation"), formal


def _scenario_rows(layout: RunLayout) -> dict[str, Any]:
    rows = {}
    for scenario_id, aggregate in layout.aggregates.items():
        metrics: dict[str, Any] = {}
        for checkpoint_id, expected_metrics in aggregate.expected_metrics.items():
            for metric in expected_metrics:
                cell = aggregate.cells.get((checkpoint_id, metric))
                metrics.setdefault(metric, []).append({
                    "checkpoint_id": checkpoint_id,
                    "status": "missing" if cell is None else cell.status,
                    "score": None if cell is None else cell.score,
                    "reason": None if cell is None else cell.reason,
                    "error": None if cell is None else cell.error,
                })
        rows[scenario_id] = {
            "state": aggregate.state,
            "expected_events": len(aggregate.expected_events),
            "observed_events": len(aggregate.observed_events),
            "expected_checkpoints": len(aggregate.expected_checkpoints),
            "observed_checkpoints": len(aggregate.observed_checkpoints),
            "assertions": {
                "expected": aggregate.assertions_expected,
                "observed": aggregate.assertions_observed,
                "failed": aggregate.assertions_failed,
            },
            "metrics": metrics,
            "issues": list(aggregate.issues),
        }
    return rows


def build_summary(
    run_dir: Path,
    *,
    version_dir: Path | None = None,
    scope: str = "auto",
) -> dict[str, Any]:
    layout = load_run(run_dir, version_dir)
    scope_name, formal = _scope(layout, scope)
    evidence = layout.run.get("_all_evidence", [])
    attempt_models = layout.run.get("_attempt_models", set())
    retrieval_records = layout.run.get("_retrieval_records", [])
    generation = _aggregate(layout)
    coverage = _coverage(layout)
    _coverage_issues(layout, coverage)
    assertions = _assertion_summary(layout)
    if assertions["failed"]:
        _add(layout.issues, "assertion:存在失败的确定性断言")
    if assertions["missing"]:
        _add(layout.issues, "assertion:存在未执行的确定性断言")
    for metric, row in generation["metrics"].items():
        if row["failed_checkpoints"]:
            _add(layout.issues, f"generation:{metric} 存在失败检查点")
        if row["applicable_checkpoints"]:
            _add(layout.issues, f"generation:{metric} 存在尚无结果的适用检查点")
        if row["missing_checkpoints"]:
            _add(layout.issues, f"generation:{metric} 存在未执行检查点")
    if not formal:
        _add(layout.issues, "release:定向验证范围不能作为有效发布")
    for aggregate in layout.aggregates.values():
        for issue in aggregate.issues:
            _add(layout.issues, issue)
    blocking = sorted(set(layout.issues))
    return {
        "schema": SUMMARY_SCHEMA,
        "run_id": layout.plan.run_id,
        "scope": scope_name,
        "formal_scope": formal,
        "generated_at": datetime.now(UTC).isoformat(),
        "run": {
            "purpose": layout.run.get("purpose"),
            "started_at": layout.run.get("started_at"),
            "finished_at": layout.run.get("finished_at"),
            "dataset": layout.run.get("dataset"),
            "identity": layout.run.get("identity"),
            "blocked": layout.run.get("blocked", []),
            "failed": layout.run.get("failed", []),
            "incomplete": layout.run.get("incomplete", []),
            "context_gaps": layout.run.get("context_gaps", []),
        },
        "coverage": coverage,
        "generation": generation,
        "assertions": assertions,
        "retrieval": _retrieval_summary(layout, retrieval_records),
        "scenarios": _scenario_rows(layout),
        "evidence": {
            "judge_records": len(evidence),
            "attempt_models": sorted(attempt_models),
        },
        "warnings": sorted(set(layout.warnings)),
        "release": {
            "effective": formal and not blocking,
            "blocking_issues": blocking,
        },
    }


def _format(value: float | None) -> str:
    return "null" if value is None else f"{value:.6f}"


def report_lines(summary: dict[str, Any]) -> list[str]:
    coverage = summary["coverage"]
    release = summary["release"]
    lines = [
        f"# 评测运行汇总 `{summary['run_id']}`",
        "",
        f"- 范围：{'正式全量' if summary['formal_scope'] else '定向验证（仅诊断）'}。",
        f"- 有效发布：{'是' if release['effective'] else '否'}。",
        f"- 运行身份：`{summary['run'].get('identity', {}).get('code', {}).get('commit', '缺失')}`。",
        f"- 数据版本：`{summary['run'].get('dataset', {}).get('version', '缺失')}`。",
        f"- 起止时间：`{summary['run'].get('started_at') or '缺失'}` 至 `{summary['run'].get('finished_at') or '缺失'}`。",
        "",
        "## 覆盖",
        "",
        "| 项目 | 预期 | 已完成 | 缺失 |",
        "|---|---|---|---|",
        f"| 场景 | {coverage['expected_scenarios']} | {coverage['observed_scenarios']} | {len(coverage['missing_scenarios'])} |",
        f"| 事件 | {coverage['expected_events']} | {coverage['observed_events']} | {len(coverage['missing_events'])} |",
        f"| 检查点 | {coverage['expected_checkpoints']} | {coverage['observed_checkpoints']} | {len(coverage['missing_checkpoints'])} |",
        f"| 生成指标 | {coverage['expected_metrics']} | {coverage['observed_metrics']} | {coverage['unexecuted_metrics']} |",
        "",
        f"- 指标状态：succeeded {coverage['succeeded_metrics']}；failed {coverage['failed_metrics']}；"
        f"not_applicable {coverage['not_applicable_metrics']}；applicable {coverage['applicable_metrics']}。",
        f"- blocked 场景：{len(coverage['blocked_scenarios'])}；failed 场景：{len(coverage['failed_scenarios'])}。",
        "",
        "## 生成指标",
        "",
        "| 指标 | 预期检查点 | succeeded | failed | N/A | applicable | 未执行 | 可用场景 | 完整场景 | 含部分成功均值 | 仅完整成功均值 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for metric, row in summary["generation"]["metrics"].items():
        lines.append(
            f"| {metric} | {row['expected_checkpoints']} | {row['succeeded_checkpoints']} | "
            f"{row['failed_checkpoints']} | {row['not_applicable_checkpoints']} | "
            f"{row['applicable_checkpoints']} | {row['missing_checkpoints']} | "
            f"{row['available_scenarios']} | {row['complete_scenarios']} | "
            f"{_format(row['mean_including_partial'])} | {_format(row['mean_complete_only'])} |"
        )
    assertions = summary["assertions"]
    lines.extend([
        "",
        "## 确定性断言",
        "",
        f"- 预期 {assertions['expected']}；已执行 {assertions['observed']}；失败 {assertions['failed']}；未执行 {len(assertions['missing'])}。",
        "",
        "## 检索证据",
        "",
        f"- 真实搜索调用 {summary['retrieval']['searches']} 次，涉及 {summary['retrieval']['scenarios_with_search']} 个场景。",
        f"- 候选池上限 {summary['retrieval']['candidate_pool_limit']}；最终模型输入上限 {summary['retrieval']['final_input_limit']}；字符预算 {summary['retrieval']['character_budget']}。",
        "- 五项阶段指标：Recall@20 取候选池；Hit@5、MRR@5、nDCG@5、Precision@5 取最终模型输入；无答案误返回按最终模型输入统计。",
    ])
    index = summary["retrieval"].get("index")
    if index:
        lines.append(
            f"- 物理索引：`{index.get('collection')}`；清单摘要 `{index.get('manifest_sha256')}`。"
        )
    else:
        lines.append("- 物理索引：缺失。")
    lines.extend(["", "## 场景明细", "", "| 场景 | 状态 | 事件 | 检查点 | 断言失败 |", "|---|---|---|---|---|"])
    for scenario_id, row in summary["scenarios"].items():
        lines.append(
            f"| {scenario_id} | {row['state']} | {row['observed_events']}/{row['expected_events']} | "
            f"{row['observed_checkpoints']}/{row['expected_checkpoints']} | {row['assertions']['failed']} |"
        )
    lines.extend(["", "## 发布门禁", ""])
    if release["blocking_issues"]:
        for issue in release["blocking_issues"]:
            lines.append(f"- {issue}")
    else:
        lines.append("- 无阻塞项。")
    if summary["warnings"]:
        lines.append("")
        lines.append("## 警告")
        lines.append("")
        for warning in summary["warnings"]:
            lines.append(f"- {warning}")
    return lines


def write_summary(
    run_dir: Path,
    summary: dict[str, Any],
    *,
    output: Path | None = None,
) -> dict[str, str]:
    directory = Path(output) if output is not None else Path(run_dir)
    directory.mkdir(parents=True, exist_ok=True)
    summary_path = directory / SUMMARY_NAME
    report_path = directory / REPORT_NAME
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    report_path.write_text("\n".join(report_lines(summary)) + "\n", encoding="utf-8")
    return {"summary": str(summary_path), "report": str(report_path)}


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="评测运行汇总、诊断报告与有效发布门禁"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("report", "check"):
        command = commands.add_parser(name)
        command.add_argument("--source", type=Path, required=True, help="运行目录")
        command.add_argument("--dataset", type=Path, default=None, help="数据版本目录")
        command.add_argument(
            "--scope",
            choices=("auto", "formal", "validation"),
            default="auto",
        )
        command.add_argument("--output", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    summary = build_summary(
        args.source, version_dir=args.dataset, scope=args.scope
    )
    if args.command == "report":
        paths = write_summary(args.source, summary, output=args.output)
        print(json.dumps({
            "run_id": summary["run_id"],
            "scope": summary["scope"],
            "effective_release": summary["release"]["effective"],
            "blocking_issues": summary["release"]["blocking_issues"],
            **paths,
        }, ensure_ascii=False))
    else:
        print(json.dumps({
            "run_id": summary["run_id"],
            "scope": summary["scope"],
            "effective_release": summary["release"]["effective"],
            "blocking_issues": summary["release"]["blocking_issues"],
        }, ensure_ascii=False))
    return 0 if summary["release"]["effective"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SummaryError as error:
        raise SystemExit(str(error)) from None
