import argparse
import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from statistics import mean
from typing import Any, Sequence

from evaluation import external_summary as es
from evaluation import langsmith_retrieval as lr
from evaluation import langsmith_scenarios as ls
from evaluation import summary as sm
from evaluation.contracts import RETRIEVAL_METRICS
from evaluation.dataset import (
    BACKEND,
    DatasetError,
    EvaluationDataset,
    load_for_evaluation,
)
from evaluation.generation_scoring import run_judge_identity
from evaluation.scenario_evidence import sha256_text
from evaluation.scenarios import (
    ScenarioArtifact,
    ScenarioExecution,
    build_execution_plan,
    frozen_scenario_sha256,
    load_execution,
    scenario_plan,
    select_scenarios,
)
from evaluation.scenarios import (
    code_identity as scenario_code_identity,
)

SUMMARY_SCHEMA = sm.SUMMARY_SCHEMA
RETRIEVAL_SCHEMA = lr.SCHEMA
SCENARIO_SCHEMA = ls.SCHEMA
START_MARKER = "<!-- sdk-summary:start -->"
END_MARKER = "<!-- sdk-summary:end -->"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
GROUP_ORDER = tuple(lr.GROUPS)
OPTIONAL_GROUPS = (lr.RERANK_GROUP,)


class SDKSummaryError(RuntimeError):
    pass


@dataclass
class RetrievalGroupResult:
    group: str
    expected_ids: list[str] = field(default_factory=list)
    samples: dict[str, dict] = field(default_factory=dict)
    duplicate_ids: list[str] = field(default_factory=list)
    missing_ids: list[str] = field(default_factory=list)
    extra_ids: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    recorded_status: str | None = None
    recomputed_status: str = "incomplete"
    coverage: dict = field(default_factory=dict)
    metrics: dict | None = None
    engine: dict | None = None
    index: dict = field(default_factory=dict)
    vector_cache_identity_sha256: str | None = None
    vector_cache_required: bool | None = None
    experiment: dict = field(default_factory=dict)
    concurrency: dict = field(default_factory=dict)
    milvus_rpc: dict = field(default_factory=dict)
    latency: dict = field(default_factory=dict)
    remote_feedback: dict[str, dict] = field(default_factory=dict)
    local_scores: dict[str, dict] = field(default_factory=dict)
    unknown_candidates: dict[str, list[str]] = field(default_factory=dict)
    error: str | None = None


@dataclass
class RetrievalSource:
    path: Path | None = None
    root: Path | None = None
    available: bool = False
    document: dict = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    run_id: str | None = None
    purpose: str | None = None
    recorded_status: str | None = None
    recomputed_status: str = "missing"
    dataset_identity: dict = field(default_factory=dict)
    code_identity: dict = field(default_factory=dict)
    parameters: dict = field(default_factory=dict)
    expected_ids: list[str] = field(default_factory=list)
    groups: dict[str, RetrievalGroupResult] = field(default_factory=dict)
    started_at: str | None = None
    finished_at: str | None = None


@dataclass
class ScenarioSample:
    scenario_id: str
    example_id: str
    recorded_status: str | None = None
    recomputed_state: str = "missing"
    execution: ScenarioExecution | None = None
    artifact: ScenarioArtifact | None = None
    feedback: dict[str, float] = field(default_factory=dict)
    expected_feedback: dict[str, float] = field(default_factory=dict)
    incomplete: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    run_start_time: str | None = None
    run_end_time: str | None = None
    scoring_relative: str | None = None
    scoring_sha256: str | None = None
    execution_sha256: str | None = None
    execution_status: str | None = None
    run_error: str | None = None
    sdk_run_id: str | None = None
    checkpoints: dict = field(default_factory=dict)
    assertions: dict = field(default_factory=dict)


@dataclass
class ScenarioSource:
    path: Path | None = None
    root: Path | None = None
    available: bool = False
    document: dict = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    run_id: str | None = None
    purpose: str | None = None
    recorded_status: str | None = None
    recomputed_status: str = "missing"
    dataset_identity: dict = field(default_factory=dict)
    code_identity: dict = field(default_factory=dict)
    generation: dict = field(default_factory=dict)
    judge: dict = field(default_factory=dict)
    index: dict = field(default_factory=dict)
    parameters: dict = field(default_factory=dict)
    expected_ids: list[str] = field(default_factory=list)
    samples: dict[str, ScenarioSample] = field(default_factory=dict)
    aggregates: dict[str, sm.ScenarioAggregate] = field(default_factory=dict)
    evidence: list = field(default_factory=list)
    attempt_models: set[str] = field(default_factory=set)
    retrieval_records: list = field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None
    experiment: dict = field(default_factory=dict)
    concurrency: dict = field(default_factory=dict)
    coverage_recorded: dict = field(default_factory=dict)
    scenario_records: dict[str, dict] = field(default_factory=dict)
    feedback_version: int | None = None
    run_judge_identity: dict | None = None
    metric_identities: dict[str, list[dict]] = field(default_factory=dict)


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


def _resolve_source(value: Path, filename: str) -> tuple[Path | None, Path | None, str | None]:
    path = Path(value)
    if path.is_dir():
        root = path.resolve()
        file = root / filename
        if not file.is_file():
            return root, None, f"来源目录缺少 {filename}: {_display_path(root)}"
        return root, file, None
    if path.is_file():
        if path.name != filename:
            return None, None, f"来源文件名必须为 {filename}: {_display_path(path)}"
        file = path.resolve()
        return file.parent, file, None
    return None, None, f"来源不存在: {_display_path(path)}"


def _read_json(path: Path) -> tuple[Any, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as error:
        return None, f"{_display_path(path)} 无法解析: {type(error).__name__}: {error}"


def _safe_relative(value: Any, root: Path, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise SDKSummaryError(f"{label} 缺少相对引用")
    if "\\" in value:
        raise SDKSummaryError(f"{label} 不得包含反斜杠: {value}")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise SDKSummaryError(f"{label} 越出受控根目录: {value}")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise SDKSummaryError(f"{label} 越出受控根目录: {value}")
    return path


def _identity_mismatches(recorded: Any, actual: dict, label: str) -> list[str]:
    if not isinstance(recorded, dict):
        return [f"{label} 缺少身份"]
    issues = []
    for key, value in actual.items():
        if recorded.get(key) != value:
            issues.append(f"{label} 不一致: {key}")
    return issues


def _code_identity_mismatches(recorded: Any, current: dict, label: str, keys: Sequence[str]) -> list[str]:
    if not isinstance(recorded, dict):
        return [f"{label} 缺少代码身份"]
    return [
        f"{label} 与当前工作区不一致: {key}"
        for key in keys
        if recorded.get(key) != current.get(key)
    ]


def _duration_seconds(started: str | None, finished: str | None) -> float | None:
    if not started or not finished:
        return None
    try:
        start = datetime.fromisoformat(started.replace("Z", "+00:00"))
        end = datetime.fromisoformat(finished.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (end - start).total_seconds()


def _latency_report(samples: Sequence[dict]) -> dict:
    values = []
    for sample in samples:
        value = _duration_seconds(
            sample.get("run_start_time"), sample.get("run_end_time")
        )
        if value is not None:
            values.append(value)
    if not values:
        return {"count": 0, "min_seconds": None, "mean_seconds": None, "max_seconds": None}
    return {
        "count": len(values),
        "min_seconds": min(values),
        "mean_seconds": mean(values),
        "max_seconds": max(values),
    }


def _query_row(dataset: EvaluationDataset, query_id: str) -> dict:
    return next(item for item in dataset.queries if item["query_id"] == query_id)


def _expected_labels(dataset: EvaluationDataset, query_id: str) -> list[dict]:
    rows = [
        {"exercise_id": item["corpus_id"], "grade": item["grade"]}
        for item in dataset.qrels
        if item["query_id"] == query_id
    ]
    return sorted(rows, key=lambda row: row["exercise_id"])


def _validate_retrieval_sample(
    *,
    dataset: EvaluationDataset,
    group: str,
    query_id: str,
    sample: dict,
    group_data: dict,
    expected_example_id: str | None,
    reviewed: dict[str, set[str]],
) -> tuple[dict, list[str]]:
    issues = []
    if sample.get("status") != "complete":
        return {}, [f"样本状态为 {sample.get('status')}"]
    outputs = sample.get("outputs")
    reference = sample.get("reference_outputs")
    if not isinstance(outputs, dict) or not isinstance(reference, dict):
        return {}, ["样本缺少 outputs 或 reference_outputs"]
    if outputs.get("query_id") != query_id:
        issues.append("outputs query_id 不一致")
    if reference.get("query_id") != query_id:
        issues.append("reference_outputs query_id 不一致")
    if sample.get("example_id") != expected_example_id:
        issues.append("样例 ID 与 SDK 数据集映射不一致")
    if outputs.get("strategy") != group:
        issues.append("outputs 检索组不一致")
    if group == lr.RERANK_GROUP:
        if outputs.get("experiment") != lr.support.EXPERIMENT_RERANK:
            issues.append("outputs 重排实验身份不一致")
        baseline = outputs.get("baseline")
        if not isinstance(baseline, dict):
            issues.append("outputs 缺少同池基线")
        elif baseline.get("candidate_pool") != outputs.get("candidate_pool"):
            issues.append("outputs 同池候选不一致")
        if not isinstance(outputs.get("rerank"), dict):
            issues.append("outputs 缺少重排证据")
    if outputs.get("dataset_content_sha256") != dataset.content_sha256:
        issues.append("outputs 数据摘要不一致")
    if outputs.get("parameters") != group_data.get("parameters"):
        issues.append("outputs 检索参数不一致")
    if outputs.get("index_collection") != group_data.get("index_collection"):
        issues.append("outputs 物理索引集合不一致")
    if outputs.get("index_manifest_sha256") != group_data.get("index_manifest_sha256"):
        issues.append("outputs 物理索引清单不一致")
    if outputs.get("vector_cache_identity_sha256") != group_data.get(
        "vector_cache_identity_sha256"
    ):
        issues.append("outputs 查询向量缓存身份不一致")
    query = _query_row(dataset, query_id)
    if reference.get("labels") != _expected_labels(dataset, query_id):
        issues.append("reference_outputs 金标与批准数据不一致")
    if reference.get("expect_empty") is not query["expect_empty"]:
        issues.append("reference_outputs 空答案标记与批准数据不一致")
    if reference.get("scenario_id") != query["scenario_id"]:
        issues.append("reference_outputs 场景 ID 与批准数据不一致")
    try:
        expected = lr.expected_feedback(
            group,
            {
                "query_id": query_id,
                "dataset": lr.dataset_binding(dataset),
            },
            outputs,
            reference,
            lr.dataset_binding(dataset),
            reviewed,
        )
    except Exception as error:
        return {}, [f"评分重验失败: {type(error).__name__}: {error}"]
    recorded = sample.get("feedback")
    if not isinstance(recorded, dict):
        issues.append("样本缺少 feedback")
        recorded = {}
    expected_keys = set(expected)
    if set(recorded) != expected_keys:
        issues.append("feedback 指标集合与重验结果不一致")
    differences = {
        key: abs(float(recorded[key]) - expected[key])
        for key in expected_keys & set(recorded)
    }
    for key, value in differences.items():
        if value > lr.REMOTE_SCORE_TOLERANCE:
            issues.append(f"feedback 与重验结果不一致: {key}")
    return {
        "local": expected,
        "remote": {key: float(value) for key, value in recorded.items()},
        "max_feedback_difference": max(differences.values(), default=0.0),
        "outputs": outputs,
        "reference": reference,
        "sample": sample,
    }, issues


def _summarize_retrieval_rows(rows: Sequence[dict]) -> dict:
    positives = [row for row in rows if not row["expect_empty"]]
    empties = [row for row in rows if row["expect_empty"]]
    summary = {
        "queries": len(rows),
        "with_labels": len(positives),
        "empty_expectation_queries": len(empties),
        "empty_returned": sum(
            1 for row in empties if row["scores"].get("false_return") == 1.0
        ),
    }
    for metric in RETRIEVAL_METRICS:
        summary[metric] = (
            mean(row["scores"][metric] for row in positives) if positives else None
        )
    summary["false_return_rate"] = (
        mean(row["scores"]["false_return"] for row in empties) if empties else None
    )
    return summary


def _group_rows(rows: Sequence[dict], key: str) -> dict[str, dict]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        value = row.get(key)
        if isinstance(value, list):
            for item in value:
                grouped.setdefault(str(item), []).append(row)
        else:
            grouped.setdefault(str(value or "unknown"), []).append(row)
    return {
        name: _summarize_retrieval_rows(selected)
        for name, selected in sorted(grouped.items())
    }


def _retrieval_group(
    *,
    group: str,
    data: Any,
    dataset: EvaluationDataset,
    expected_rows: list[dict],
    run_parameters: dict,
    run_purpose: str | None,
    example_ids: dict,
    source_issues: list[str],
) -> RetrievalGroupResult:
    result = RetrievalGroupResult(
        group=group, expected_ids=[row["query_id"] for row in expected_rows]
    )
    result.recorded_status = data.get("status") if isinstance(data, dict) else None
    if not isinstance(data, dict):
        result.error = "检索组产物缺失"
        _add(result.issues, result.error)
        _add(source_issues, f"检索组 {group}: {result.error}")
        return result
    result.error = data.get("error")
    if data.get("group") != group:
        _add(result.issues, "group 字段不一致")
    if data.get("purpose") != run_purpose:
        _add(result.issues, "purpose 与检索运行不一致")
    if data.get("parameters") != run_parameters:
        _add(result.issues, "检索参数与运行记录不一致")
    result.index = {
        "collection": data.get("index_collection"),
        "manifest_sha256": data.get("index_manifest_sha256"),
        "build_id": data.get("index_build_id"),
    }
    if not result.index["collection"]:
        _add(result.issues, "缺少物理索引集合")
    if not isinstance(result.index["manifest_sha256"], str) or not HEX64.fullmatch(
        result.index["manifest_sha256"]
    ):
        _add(result.issues, "物理索引清单摘要非法")
    result.vector_cache_identity_sha256 = data.get("vector_cache_identity_sha256")
    result.vector_cache_required = data.get("vector_cache_required")
    if result.vector_cache_required and (
        not isinstance(result.vector_cache_identity_sha256, str)
        or not HEX64.fullmatch(result.vector_cache_identity_sha256)
    ):
        _add(result.issues, "查询向量缓存身份缺失或非法")
    result.experiment = {
        key: data.get(key)
        for key in ("experiment_id", "experiment_name", "experiment_url")
    }
    if not result.experiment["experiment_id"]:
        _add(result.issues, "缺少 LangSmith 实验 ID")
    result.concurrency = data.get("concurrency") or {}
    result.milvus_rpc = data.get("milvus_rpc") or {}
    max_in_flight = result.concurrency.get("max_in_flight")
    if not isinstance(max_in_flight, int) or max_in_flight > lr.MAX_CONCURRENCY:
        _add(result.issues, "并发在途数超过冻结上限")
    raw_sample_count = len(data.get("samples") or []) if isinstance(data.get("samples"), list) else 0
    if raw_sample_count > 1 and max_in_flight is not None and max_in_flight <= 1:
        _add(result.issues, "未观察到查询重叠执行")
    if result.concurrency.get("overlap_pairs") is not None and result.concurrency[
        "overlap_pairs"
    ] <= 0:
        _add(result.issues, "并发重叠对为 0")

    raw_samples = data.get("samples")
    if not isinstance(raw_samples, list):
        result.error = "检索组缺少样本"
        _add(result.issues, result.error)
        _add(source_issues, f"检索组 {group}: {result.error}")
        result.recomputed_status = "incomplete"
        return result
    by_query: dict[str, dict] = {}
    duplicates = set()
    for sample in raw_samples:
        query_id = sample.get("query_id") if isinstance(sample, dict) else None
        if not isinstance(query_id, str) or not query_id:
            _add(result.issues, "样本缺少 query_id")
            continue
        if query_id in by_query:
            duplicates.add(query_id)
            continue
        by_query[query_id] = sample
    result.samples = by_query
    result.duplicate_ids = sorted(duplicates)
    reviewed = dataset.reviewed_corpus_ids()
    result.unknown_candidates = dataset.unreviewed_candidates(
        {
            query_id: sample["outputs"]["candidate_pool"]
            for query_id, sample in by_query.items()
            if isinstance(sample.get("outputs"), dict)
            and isinstance(sample["outputs"].get("candidate_pool"), list)
        }
    )
    expected_set = set(result.expected_ids)
    result.missing_ids = sorted(expected_set - set(by_query))
    result.extra_ids = sorted(set(by_query) - expected_set)
    if result.duplicate_ids:
        _add(result.issues, f"样本 query_id 重复: {result.duplicate_ids[:3]}")
    if result.missing_ids:
        _add(result.issues, f"样本缺失: {result.missing_ids[:3]}")
    if result.extra_ids:
        _add(result.issues, f"样本包含未知 query_id: {result.extra_ids[:3]}")
    if len(by_query) != len(result.expected_ids):
        _add(result.issues, "样本数量与批准查询集不一致")

    complete_samples = []
    per_query = {}
    for query_id in result.expected_ids:
        sample = by_query.get(query_id)
        if sample is None:
            continue
        if query_id in result.unknown_candidates:
            continue
        expected_example_id = example_ids.get(query_id)
        checked, sample_issues = _validate_retrieval_sample(
            dataset=dataset,
            group=group,
            query_id=query_id,
            sample=sample,
            group_data=data,
            expected_example_id=expected_example_id,
            reviewed=reviewed,
        )
        for issue in sample_issues:
            _add(result.issues, f"{query_id}: {issue}")
        if checked:
            complete_samples.append(sample)
            per_query[query_id] = checked
            result.local_scores[query_id] = checked["local"]
            result.remote_feedback[query_id] = checked["remote"]
            query = _query_row(dataset, query_id)
            checked["row"] = {
                "query_id": query_id,
                "purpose": query["purpose"],
                "tags": list(query["tags"]),
                "family": query["family"],
                "expect_empty": query["expect_empty"],
                "scores": checked["local"],
                "feedback": checked["remote"],
                "max_feedback_difference": checked["max_feedback_difference"],
            }

    for query_id, ids in result.unknown_candidates.items():
        _add(result.issues, f"{query_id}: 存在未审阅候选: {', '.join(ids)}")
    result.coverage = {
        "expected": len(result.expected_ids),
        "observed": len(complete_samples),
        "missing": result.missing_ids,
        "extra": result.extra_ids,
        "duplicates": result.duplicate_ids,
        "status": (
            "complete"
            if len(complete_samples) == len(result.expected_ids)
            and not result.missing_ids
            and not result.extra_ids
            and not result.duplicate_ids
            and not result.issues
            else "incomplete"
        ),
    }
    rows = [item["row"] for item in per_query.values()]
    metrics = None
    if result.coverage["status"] == "complete":
        try:
            corpus = lr.RetrievalCorpus.load()
            parity = lr.parity_report(
                dataset,
                expected_rows,
                complete_samples,
                corpus,
                group,
                run_parameters,
            )
            metrics = parity["metrics"]
            result.engine = metrics.get("engine")
            recomputed_overall = _summarize_retrieval_rows(rows)
            metrics["overall"]["empty_returned"] = recomputed_overall["empty_returned"]
            parity_rows = []
            for row in rows:
                query_id = row["query_id"]
                parity_row = metrics["per_query"][query_id]
                parity_rows.append(
                    {
                        **row,
                        "scores": parity_row["scores"],
                    }
                )
            rows = parity_rows
            recomputed_overall = _summarize_retrieval_rows(rows)
            for key, value in recomputed_overall.items():
                if key not in metrics["overall"]:
                    continue
                recorded_value = metrics["overall"].get(key)
                if isinstance(value, float) and isinstance(recorded_value, float):
                    if not math.isclose(value, recorded_value, rel_tol=0.0, abs_tol=1e-12):
                        _add(result.issues, f"聚合指标与重算结果不一致: {key}")
                elif value != recorded_value:
                    _add(result.issues, f"聚合指标与重算结果不一致: {key}")
        except Exception as error:
            _add(result.issues, f"语义投影校验失败: {type(error).__name__}: {error}")
    if metrics is None and not result.unknown_candidates:
        overall = _summarize_retrieval_rows(rows)
        metrics = {
            "engine": {"name": "mteb", "identity": "not_revalidated"},
            "overall": overall,
            "by_purpose": _group_rows(rows, "purpose"),
            "by_tag": _group_rows(rows, "tags"),
            "by_family": _group_rows(rows, "family"),
            "per_query": {
                row["query_id"]: {
                    **row,
                    "id": row["query_id"],
                    "expect_empty": row["expect_empty"],
                    "expected_ids": {},
                }
                for row in rows
            },
        }
    elif metrics is not None:
        metrics["by_purpose"] = _group_rows(rows, "purpose")
        metrics["by_family"] = _group_rows(rows, "family")
        metrics["per_query"] = {
            row["query_id"]: {
                **metrics["per_query"][row["query_id"]],
                "feedback": row["feedback"],
                "max_feedback_difference": row["max_feedback_difference"],
            }
            for row in rows
        }
    result.metrics = metrics
    result.latency = _latency_report(
        [sample for sample in complete_samples if isinstance(sample, dict)]
    )
    result.recomputed_status = (
        "blocked_unreviewed_candidates"
        if result.unknown_candidates
        else "complete"
        if result.coverage["status"] == "complete" and not result.issues
        else "incomplete"
    )
    recorded_coverage = data.get("coverage")
    if isinstance(recorded_coverage, dict) and recorded_coverage.get("status") != result.coverage[
        "status"
    ]:
        _add(result.issues, "来源覆盖状态与重算结果不一致")
    if result.recorded_status != result.recomputed_status:
        _add(result.issues, "来源组状态与重算结果不一致")
    if result.issues:
        for issue in result.issues:
            _add(source_issues, f"检索组 {group}: {issue}")
    return result


def load_retrieval_source(path: Path, dataset: EvaluationDataset) -> RetrievalSource:
    source = RetrievalSource(path=Path(path))
    root, file, error = _resolve_source(path, "run.json")
    source.root = root
    if error:
        _add(source.issues, error)
        return source
    document, error = _read_json(file)
    if error:
        _add(source.issues, error)
        return source
    if not isinstance(document, dict):
        _add(source.issues, "检索来源必须为 JSON 对象")
        return source
    source.document = document
    source.available = True
    source.run_id = document.get("run_id")
    source.purpose = document.get("purpose")
    source.recorded_status = document.get("status")
    source.started_at = document.get("started_at")
    source.finished_at = document.get("finished_at")
    if document.get("schema") != RETRIEVAL_SCHEMA:
        _add(source.issues, "检索来源 Schema 不符")
        source.available = False
        return source
    if not isinstance(source.run_id, str) or not source.run_id:
        _add(source.issues, "检索来源缺少 run_id")
    if source.purpose not in {"validation", "formal"}:
        _add(source.issues, "检索来源 purpose 非法")
    source.dataset_identity = document.get("dataset") if isinstance(document.get("dataset"), dict) else {}
    for message in _identity_mismatches(
        source.dataset_identity, dataset.identity(), "检索数据身份"
    ):
        _add(source.issues, message)
    source.parameters = document.get("parameters") if isinstance(document.get("parameters"), dict) else {}
    try:
        expected_parameters = lr.validated_parameters()
    except Exception as error:
        expected_parameters = None
        _add(source.issues, f"冻结检索参数无法核验: {type(error).__name__}: {error}")
    if expected_parameters is not None and source.parameters != expected_parameters:
        _add(source.issues, "检索参数与冻结基线不一致")
    source.code_identity = document.get("code_identity") if isinstance(document.get("code_identity"), dict) else {}
    try:
        current_code = lr.sdk_code_identity()
    except Exception as error:
        current_code = None
        _add(source.issues, f"当前检索代码身份无法生成: {type(error).__name__}: {error}")
    if current_code is not None:
        for message in _code_identity_mismatches(
            source.code_identity, current_code, "检索代码身份", ("head", "files", "versions")
        ):
            _add(source.issues, message)
    if document.get("dataset_name") != lr.dataset_name(dataset):
        _add(source.issues, "检索数据集名称与批准数据不一致")
    if not document.get("dataset_id"):
        _add(source.issues, "检索来源缺少 dataset_id")
    example_ids = document.get("example_ids")
    if not isinstance(example_ids, dict):
        example_ids = {}
        _add(source.issues, "检索来源缺少 example_ids")
    else:
        expected_example_ids = {
            query["query_id"]: lr.stable_example_id(lr.dataset_name(dataset), query["query_id"])
            for query in dataset.queries
        }
        if example_ids != expected_example_ids:
            _add(source.issues, "SDK 样例 ID 映射与批准数据不一致")
    if document.get("max_concurrency") != lr.MAX_CONCURRENCY:
        _add(source.issues, "检索并发配置与冻结上限不一致")
    if document.get("num_repetitions") != lr.NUM_REPETITIONS:
        _add(source.issues, "检索重复次数与冻结配置不一致")
    try:
        expected_rows = (
            lr.select_validation_queries(dataset)
            if source.purpose == "validation"
            else lr.select_formal_queries(dataset)
        )
    except Exception as error:
        _add(source.issues, f"检索样本范围无法生成: {type(error).__name__}: {error}")
        expected_rows = list(dataset.queries)
    source.expected_ids = [row["query_id"] for row in expected_rows]
    sample_scope = document.get("sample_scope")
    if not isinstance(sample_scope, dict):
        _add(source.issues, "检索来源缺少 sample_scope")
    else:
        scope_ids = sample_scope.get("query_ids")
        if not isinstance(scope_ids, list) or len(set(scope_ids)) != len(scope_ids):
            _add(source.issues, "检索 sample_scope 查询 ID 非法或重复")
        elif set(scope_ids) != set(source.expected_ids):
            _add(source.issues, "检索 sample_scope 与批准查询集不一致")
        if sample_scope.get("count") is not None and sample_scope.get("count") != len(
            source.expected_ids
        ):
            _add(source.issues, "检索 sample_scope 数量不一致")
    groups = document.get("groups")
    if not isinstance(groups, dict):
        _add(source.issues, "检索来源缺少 groups")
        groups = {}
    unknown_groups = sorted(set(groups) - set(GROUP_ORDER) - set(OPTIONAL_GROUPS))
    if unknown_groups:
        _add(source.issues, f"检索来源包含未知组: {unknown_groups}")
    for group in (*GROUP_ORDER, *OPTIONAL_GROUPS):
        if group not in groups:
            continue
        source.groups[group] = _retrieval_group(
            group=group,
            data=groups[group],
            dataset=dataset,
            expected_rows=expected_rows,
            run_parameters=source.parameters,
            run_purpose=source.purpose,
            example_ids=example_ids,
            source_issues=source.issues,
        )
    missing_groups = set(GROUP_ORDER) - set(source.groups)
    if missing_groups:
        _add(source.issues, f"检索来源未覆盖全部批准组: {sorted(missing_groups)}")
    present = [item.recomputed_status for item in source.groups.values()]
    source.recomputed_status = (
        "complete"
        if source.available and present and all(value == "complete" for value in present)
        else "incomplete"
    )
    if source.recorded_status != source.recomputed_status:
        _add(source.issues, "检索来源状态与重算结果不一致")
    return source


def _scenario_expected_ids(
    dataset: EvaluationDataset, purpose: str | None, issues: list[str]
) -> list[str]:
    try:
        if purpose == "validation":
            scenarios, _ = ls.select_validation_scenarios(dataset)
        else:
            scenarios = ls.select_formal_scenarios(dataset)
    except Exception as error:
        _add(issues, f"场景样本范围无法生成: {type(error).__name__}: {error}")
        scenarios = list(dataset.scenarios)
    return [scenario["scenario_id"] for scenario in scenarios]


def _empty_aggregate(
    scenario: dict, state: str, issue: str
) -> sm.ScenarioAggregate:
    plan = scenario_plan(scenario)
    aggregate = sm.ScenarioAggregate(
        scenario_id=scenario["scenario_id"],
        state=state,
        expected_events=list(plan.event_ids),
        expected_checkpoints=list(plan.checkpoint_ids),
        expected_metrics={
            checkpoint_id: list(metrics)
            for checkpoint_id, metrics in plan.generation_metrics.items()
        },
    )
    _add(aggregate.issues, issue)
    aggregate.assertions_missing = [
        f"{row['checkpoint_id']}/{row['event_id']}/{row['kind']}"
        for row in scenario["assertions"]
    ]
    return aggregate




def _load_scenario_sample(
    *,
    dataset: EvaluationDataset,
    source: ScenarioSource,
    sample: dict,
    scenario: dict,
    record: dict | None,
) -> tuple[ScenarioSample, sm.ScenarioAggregate]:
    scenario_id = scenario["scenario_id"]
    plan = scenario_plan(scenario)
    example_id = sample.get("example_id")
    if not isinstance(example_id, str) or not example_id:
        example_id = ""
    checked = ScenarioSample(
        scenario_id=scenario_id,
        example_id=example_id,
        recorded_status=sample.get("status"),
        run_start_time=sample.get("run_start_time"),
        run_end_time=sample.get("run_end_time"),
        scoring_relative=sample.get("scoring_relative"),
        scoring_sha256=sample.get("scoring_sha256"),
        execution_sha256=sample.get("execution_sha256"),
        execution_status=sample.get("execution_status"),
        run_error=sample.get("run_error"),
        sdk_run_id=sample.get("sdk_run_id"),
    )
    issues: list[str] = []
    prefix = f"scenario:{scenario_id}"
    expected_example_id = source.document.get("example_ids", {}).get(scenario_id)
    if example_id != expected_example_id:
        _add(issues, f"{prefix}: 样例 ID 与 SDK 数据集映射不一致")
    expected_execution = ls.expected_execution_path(example_id, scenario_id)
    expected_scoring = expected_execution.parent / ls.SCORING_NAME
    if sample.get("scoring_relative") != str(expected_scoring):
        _add(issues, f"{prefix}: scoring_relative 与批准目录不一致")
    try:
        scoring_path = _safe_relative(sample.get("scoring_relative"), source.root, "scoring_relative")
    except SDKSummaryError as error:
        _add(issues, f"{prefix}: {error}")
        scoring_path = None
    if scoring_path is not None and not scoring_path.is_file():
        _add(issues, f"{prefix}: 评分产物缺失")
        scoring_path = None
    try:
        execution_path = _safe_relative(str(expected_execution), source.root, "execution")
    except SDKSummaryError as error:
        _add(issues, f"{prefix}: {error}")
        execution_path = None
    execution = None
    if execution_path is not None and execution_path.is_file():
        try:
            execution = load_execution(
                execution_path,
                expected_run_id=source.run_id,
                expected_scenario_id=scenario_id,
                expected_dataset=ls.dataset_identity(dataset),
                expected_scenario_sha256=frozen_scenario_sha256(scenario),
            )
        except Exception as error:
            _add(issues, f"{prefix}: 执行结果身份校验失败: {type(error).__name__}: {error}")
    else:
        _add(issues, f"{prefix}: 执行结果缺失")
    checked.execution = execution
    if execution is not None:
        checked.execution_sha256 = execution.execution_sha256
        checked.execution_status = execution.status
        if sample.get("execution_sha256") != execution.execution_sha256:
            _add(issues, f"{prefix}: execution_sha256 与执行结果不一致")
        if sample.get("execution_status") != execution.status:
            _add(issues, f"{prefix}: 执行状态与执行结果不一致")
    evaluation = None
    evaluation_path = expected_execution.parent / ls.EVALUATION_NAME
    try:
        resolved_evaluation = _safe_relative(str(evaluation_path), source.root, "evaluation")
    except SDKSummaryError as error:
        _add(issues, f"{prefix}: {error}")
        resolved_evaluation = None
    if resolved_evaluation is not None and resolved_evaluation.is_file():
        evaluation, error = _read_json(resolved_evaluation)
        if error:
            _add(issues, f"{prefix}: {error}")
            evaluation = None
    else:
        _add(issues, f"{prefix}: 缺少 evaluation.json")
    artifact = None
    if scoring_path is not None:
        scoring_text = scoring_path.read_text(encoding="utf-8")
        scoring_sha = sha256_text(scoring_text)
        if sample.get("scoring_sha256") != scoring_sha:
            _add(issues, f"{prefix}: scoring_sha256 与评分产物不一致")
        if isinstance(evaluation, dict) and evaluation.get("scoring_sha256") != scoring_sha:
            _add(issues, f"{prefix}: evaluation.scoring_sha256 与评分产物不一致")
        try:
            artifact = ScenarioArtifact.model_validate_json(scoring_text)
        except Exception as error:
            _add(issues, f"{prefix}: 评分产物不符合当前契约: {type(error).__name__}: {str(error)[:300]}")
    checked.artifact = artifact
    if artifact is not None and execution is not None:
        if artifact.run_id != execution.run_id:
            _add(issues, f"{prefix}: 评分 run_id 与执行结果不一致")
        if artifact.scenario_id != execution.scenario_id:
            _add(issues, f"{prefix}: 评分 scenario_id 与执行结果不一致")
        if artifact.session_ids != execution.session_ids:
            _add(issues, f"{prefix}: 评分会话与执行结果不一致")
        if [event.event_id for event in artifact.events] != [
            event.event_id for event in execution.events
        ]:
            _add(issues, f"{prefix}: 评分事件与执行结果不一致")
        if artifact.generation_metrics != plan.generation_metrics:
            _add(issues, f"{prefix}: 评分生成指标与冻结计划不一致")
    state = "missing"
    if execution is not None:
        state = (
            "blocked"
            if execution.status == "blocked"
            else "failed"
            if execution.status == "failed"
            else "complete"
        )
    if artifact is not None:
        if artifact.blocked is not None:
            state = "blocked"
        elif artifact.runner_error is not None:
            state = "failed"
        elif sample.get("status") != "complete":
            state = "failed"
    else:
        state = "failed"
    aggregate = sm.ScenarioAggregate(
        scenario_id=scenario_id,
        state=state,
        expected_events=list(plan.event_ids),
        expected_checkpoints=list(plan.checkpoint_ids),
        expected_metrics={
            checkpoint_id: list(metrics)
            for checkpoint_id, metrics in plan.generation_metrics.items()
        },
        artifact=artifact,
    )
    for issue in issues:
        _add(checked.issues, issue)
        _add(aggregate.issues, issue)
    if artifact is None or execution is None:
        aggregate.assertions_missing = [
            f"{row['checkpoint_id']}/{row['event_id']}/{row['kind']}"
            for row in scenario["assertions"]
        ]
        checked.incomplete = list(sample.get("incomplete") or [])
        checked.recomputed_state = aggregate.state
        return checked, aggregate
    sm._event_prefix(plan, artifact, state, aggregate.issues, prefix)
    (
        aggregate.assertions_expected,
        aggregate.assertions_observed,
        aggregate.assertions_failed,
        aggregate.assertions_missing,
    ) = sm._validate_assertions(
        scenario, plan, artifact, state, execution.run_id, aggregate.issues, prefix
    )
    sm._validate_generation(
        scenario,
        plan,
        artifact,
        state,
        execution.run_id,
        aggregate.issues,
        prefix,
        source.evidence,
        source.attempt_models,
    )
    sm._validate_retrieval(
        plan,
        artifact,
        execution.run_id,
        aggregate.issues,
        prefix,
        source.retrieval_records,
    )
    version = None
    try:
        version = ls.feedback_version(sample)
        if (
            source.feedback_version is not None
            and version != source.feedback_version
        ):
            message = f"{prefix}: sample feedback 协议版本与运行不一致"
            _add(aggregate.issues, message)
            _add(checked.issues, message)
        if isinstance(evaluation, dict):
            evaluation_version = ls.feedback_version(evaluation)
            if evaluation_version != version:
                message = f"{prefix}: feedback 协议版本与 evaluation 不一致"
                _add(aggregate.issues, message)
                _add(checked.issues, message)
            if (
                source.feedback_version is not None
                and evaluation_version != source.feedback_version
            ):
                message = f"{prefix}: evaluation feedback 协议版本与运行不一致"
                _add(aggregate.issues, message)
                _add(checked.issues, message)
    except ls.ScenarioSDKError as error:
        _add(aggregate.issues, f"{prefix}: {error}")
        _add(checked.issues, f"{prefix}: {error}")
    checked.incomplete = ls.incomplete_items(artifact)
    recorded_incomplete = sample.get("incomplete")
    recorded_feedback = sample.get("feedback")
    if not isinstance(recorded_feedback, dict):
        _add(aggregate.issues, f"{prefix}: 样本缺少 feedback")
        recorded_feedback = {}
    if version is not None:
        checked.feedback = ls.scenario_feedback(
            execution, artifact, version=version
        )
        recorded_incomplete = ls.incomplete_items(artifact, version=version)
        if set(recorded_feedback) != set(checked.feedback):
            _add(aggregate.issues, f"{prefix}: feedback 指标集合与重算结果不一致")
        for key in set(recorded_feedback) & set(checked.feedback):
            if key == "scenario_status":
                if recorded_feedback[key] != checked.feedback[key]:
                    _add(aggregate.issues, f"{prefix}: scenario_status 与重算结果不一致")
            elif not math.isclose(
                float(recorded_feedback[key]),
                checked.feedback[key],
                rel_tol=0.0,
                abs_tol=ls.REMOTE_SCORE_TOLERANCE,
            ):
                _add(aggregate.issues, f"{prefix}: feedback 与重算结果不一致: {key}")
    if checked.incomplete:
        _add(aggregate.issues, f"{prefix}: 评分不完整 {checked.incomplete[:3]}")
        if state == "complete":
            aggregate.state = "failed"
    if recorded_incomplete != sample.get("incomplete"):
        _add(aggregate.issues, f"{prefix}: 来源 incomplete 与评分产物不一致")
    if (
        version is not None
        and checked.feedback.get("scenario_status") == 1.0
        and checked.incomplete
    ):
        _add(aggregate.issues, f"{prefix}: scenario_status 成功但评分不完整")
    if sample.get("status") != "complete" or state != "complete" or checked.incomplete:
        if state == "complete":
            aggregate.state = "failed"
    if record is not None:
        try:
            record_version = ls.feedback_version(record)
        except ls.ScenarioSDKError as error:
            _add(aggregate.issues, f"{prefix}: {error}")
            _add(checked.issues, f"{prefix}: {error}")
        else:
            if (
                source.feedback_version is not None
                and record_version != source.feedback_version
            ):
                message = f"{prefix}: 场景记录反馈协议版本与运行不一致"
                _add(aggregate.issues, message)
                _add(checked.issues, message)
        for key in (
            "status",
            "execution_status",
            "execution_sha256",
            "scoring_relative",
            "scoring_sha256",
            "feedback_version",
            "feedback",
            "incomplete",
            "sdk_run_id",
            "run_error",
        ):
            if record.get(key) != sample.get(key):
                _add(aggregate.issues, f"{prefix}: SDK 场景记录不一致: {key}")
        if record.get("checkpoints") != ls.checkpoint_records(artifact):
            _add(aggregate.issues, f"{prefix}: SDK 检查点记录与评分产物不一致")
        stats = ls.assertion_statistics(artifact)
        if record.get("assertions") != stats:
            _add(aggregate.issues, f"{prefix}: SDK 断言统计与评分产物不一致")
    if isinstance(evaluation, dict):
        for key, expected in (
            ("run_id", source.run_id),
            ("scenario_id", scenario_id),
            ("execution_sha256", execution.execution_sha256),
            ("scoring_relative", str(expected_scoring)),
            ("scoring_sha256", sha256_text(scoring_path.read_text(encoding="utf-8"))),
            ("execution_status", execution.status),
            ("feedback", checked.feedback),
            ("incomplete", recorded_incomplete),
        ):
            if evaluation.get(key) != expected:
                _add(aggregate.issues, f"{prefix}: evaluation 记录不一致: {key}")
        if evaluation.get("checkpoints") != ls.checkpoint_records(artifact):
            _add(aggregate.issues, f"{prefix}: evaluation 检查点记录与评分产物不一致")
        if (
            version == ls.FEEDBACK_VERSION
            and evaluation.get("feedback_version") != version
        ):
            _add(aggregate.issues, f"{prefix}: evaluation 缺少反馈协议版本")
    else:
        _add(aggregate.issues, f"{prefix}: 缺少 evaluation.json")
    for row in artifact.generation:
        if row.result is None:
            continue
        for outcome in row.result.outcomes:
            aggregate.cells[(row.checkpoint_id, outcome.metric)] = sm.MetricCell(
                checkpoint_id=row.checkpoint_id,
                metric=outcome.metric,
                status=outcome.status,
                score=outcome.score,
                reason=outcome.reason,
                error=outcome.error,
            )
            aggregate.observed_checkpoints.add((row.event_id, row.checkpoint_id))
    for checkpoint in artifact.assertions:
        if checkpoint.outcomes:
            aggregate.observed_checkpoints.add(
                (checkpoint.event_id, checkpoint.checkpoint_id)
            )
    aggregate.observed_events = [event.event_id for event in artifact.events]
    checked.recomputed_state = aggregate.state
    return checked, aggregate


def load_scenario_source(path: Path, dataset: EvaluationDataset) -> ScenarioSource:
    source = ScenarioSource(path=Path(path))
    root, file, error = _resolve_source(path, "sdk-run.json")
    source.root = root
    if error:
        _add(source.issues, error)
        return source
    document, error = _read_json(file)
    if error:
        _add(source.issues, error)
        return source
    if not isinstance(document, dict):
        _add(source.issues, "场景来源必须为 JSON 对象")
        return source
    source.document = document
    source.available = True
    source.run_id = document.get("run_id")
    source.purpose = document.get("purpose")
    source.recorded_status = document.get("status")
    source.started_at = document.get("started_at")
    source.finished_at = document.get("finished_at")
    if document.get("schema") != SCENARIO_SCHEMA:
        _add(source.issues, "场景来源 Schema 不符")
        source.available = False
        return source
    try:
        source.feedback_version = ls.feedback_version(document)
    except ls.ScenarioSDKError as error:
        _add(source.issues, str(error))
        source.available = False
        return source
    if source.feedback_version == ls.EXTERNAL_FEEDBACK_VERSION:
        _add(
            source.issues,
            "外部裁判运行由 evaluation.sdk_summary 协议分发读取，"
            "旧场景加载器不解析该反馈协议",
        )
        source.available = False
        return source
    if not isinstance(source.run_id, str) or not source.run_id:
        _add(source.issues, "场景来源缺少 run_id")
    if source.purpose not in {"validation", "formal"}:
        _add(source.issues, "场景来源 purpose 非法")
    source.dataset_identity = document.get("dataset") if isinstance(document.get("dataset"), dict) else {}
    for message in _identity_mismatches(
        source.dataset_identity, dataset.identity(), "场景数据身份"
    ):
        _add(source.issues, message)
    source.generation = document.get("generation") if isinstance(document.get("generation"), dict) else {}
    source.judge = document.get("judge") if isinstance(document.get("judge"), dict) else {}
    source.index = document.get("index") if isinstance(document.get("index"), dict) else {}
    source.code_identity = document.get("code_identity") if isinstance(document.get("code_identity"), dict) else {}
    source.experiment = document.get("experiment") if isinstance(document.get("experiment"), dict) else {}
    source.concurrency = document.get("concurrency") if isinstance(document.get("concurrency"), dict) else {}
    source.coverage_recorded = document.get("coverage") if isinstance(document.get("coverage"), dict) else {}
    if document.get("dataset_name") != ls.dataset_name(dataset):
        _add(source.issues, "场景数据集名称与批准数据不一致")
    if not document.get("dataset_id"):
        _add(source.issues, "场景来源缺少 dataset_id")
    example_ids = document.get("example_ids")
    if not isinstance(example_ids, dict):
        example_ids = {}
        _add(source.issues, "场景来源缺少 example_ids")
    else:
        expected_example_ids = {
            scenario["scenario_id"]: ls.stable_example_id(
                ls.dataset_name(dataset), scenario["scenario_id"]
            )
            for scenario in dataset.scenarios
        }
        if example_ids != expected_example_ids:
            _add(source.issues, "SDK 场景样例 ID 映射与批准数据不一致")
    if document.get("max_concurrency") != ls.MAX_CONCURRENCY:
        _add(source.issues, "场景并发配置与冻结上限不一致")
    if document.get("num_repetitions") != ls.NUM_REPETITIONS:
        _add(source.issues, "场景重复次数与冻结配置不一致")
    try:
        current_code = scenario_code_identity()
    except Exception as error:
        current_code = None
        _add(source.issues, f"当前场景代码身份无法生成: {type(error).__name__}: {error}")
    if current_code is not None:
        for message in _code_identity_mismatches(
            source.code_identity,
            current_code,
            "场景代码身份",
            ("commit", "dirty", "lock_sha256", "source_sha256"),
        ):
            _add(source.issues, message)
    model_value = source.generation.get("model")
    try:
        model_identity = json.loads(model_value) if isinstance(model_value, str) else None
    except ValueError:
        model_identity = None
    if not isinstance(model_identity, dict) or any(
        not isinstance(model_identity.get(key), str) or not model_identity.get(key)
        for key in ("api", "provider", "model")
    ):
        _add(source.issues, "生成模型身份缺失或非法")
    if not isinstance(source.generation.get("prompt_version"), str) or not source.generation.get(
        "prompt_version"
    ):
        _add(source.issues, "生成提示身份缺失")
    if not isinstance(source.judge.get("model"), str) or not source.judge.get("model"):
        _add(source.issues, "裁判模型身份缺失")
    if not isinstance(source.judge.get("endpoint_sha256"), str) or not HEX64.fullmatch(
        source.judge.get("endpoint_sha256")
    ):
        _add(source.issues, "裁判端点身份缺失或非法")
    if not source.index.get("collection") or not source.index.get("manifest_sha256"):
        _add(source.issues, "场景物理索引身份缺失")
    if not source.experiment.get("id"):
        _add(source.issues, "场景来源缺少 LangSmith 实验 ID")
    expected_ids = _scenario_expected_ids(dataset, source.purpose, source.issues)
    source.expected_ids = expected_ids
    scope = document.get("sample_scope")
    if not isinstance(scope, dict):
        _add(source.issues, "场景来源缺少 sample_scope")
    else:
        scope_ids = scope.get("scenario_ids")
        if not isinstance(scope_ids, list) or len(set(scope_ids)) != len(scope_ids):
            _add(source.issues, "场景 sample_scope ID 非法或重复")
        elif set(scope_ids) != set(expected_ids):
            _add(source.issues, "场景 sample_scope 与批准场景集不一致")
        if scope.get("count") is not None and scope.get("count") != len(source.expected_ids):
            _add(source.issues, "场景 sample_scope 数量不一致")
    scenarios = {scenario["scenario_id"]: scenario for scenario in dataset.scenarios}
    raw_samples = document.get("samples")
    if not isinstance(raw_samples, list):
        _add(source.issues, "场景来源缺少 samples")
        raw_samples = []
    by_scenario: dict[str, dict] = {}
    duplicate_scenarios = set()
    for sample in raw_samples:
        scenario_id = sample.get("scenario_id") if isinstance(sample, dict) else None
        if not isinstance(scenario_id, str) or not scenario_id:
            _add(source.issues, "场景样本缺少 scenario_id")
            continue
        if scenario_id in by_scenario:
            duplicate_scenarios.add(scenario_id)
            continue
        by_scenario[scenario_id] = sample
    if duplicate_scenarios:
        _add(source.issues, f"场景样本重复: {sorted(duplicate_scenarios)[:3]}")
    expected_set = set(expected_ids)
    missing = expected_set - set(by_scenario)
    extra = set(by_scenario) - expected_set
    if missing:
        _add(source.issues, f"场景样本缺失: {sorted(missing)[:3]}")
    if extra:
        _add(source.issues, f"场景样本包含未知 ID: {sorted(extra)[:3]}")
    records = document.get("scenarios")
    if not isinstance(records, list):
        _add(source.issues, "场景来源缺少 scenarios 记录")
        records = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("scenario_id"), str):
            _add(source.issues, "场景记录非法")
            continue
        scenario_id = record["scenario_id"]
        if scenario_id in source.scenario_records:
            _add(source.issues, f"场景记录重复: {scenario_id}")
        source.scenario_records[scenario_id] = record
    if set(source.scenario_records) != expected_set:
        _add(source.issues, "场景记录集合与批准场景集不一致")
    for scenario_id in expected_ids:
        scenario = scenarios[scenario_id]
        sample = by_scenario.get(scenario_id)
        if sample is None:
            aggregate = _empty_aggregate(scenario, "missing", f"scenario:{scenario_id}: 缺少场景样本")
            source.aggregates[scenario_id] = aggregate
            source.samples[scenario_id] = ScenarioSample(
                scenario_id=scenario_id, example_id="", recomputed_state="missing"
            )
            continue
        checked, aggregate = _load_scenario_sample(
            dataset=dataset,
            source=source,
            sample=sample,
            scenario=scenario,
            record=source.scenario_records.get(scenario_id),
        )
        source.samples[scenario_id] = checked
        source.aggregates[scenario_id] = aggregate
        for issue in checked.issues:
            _add(source.issues, issue)
    selected = select_scenarios(dataset, expected_ids)
    plan = build_execution_plan(dataset, selected, source.run_id or "")
    layout = sm.RunLayout(
        run_dir=source.root or BACKEND,
        run={},
        plan=plan,
        dataset=dataset,
        dataset_scenarios=scenarios,
        full_plan=build_execution_plan(dataset, list(dataset.scenarios), source.run_id or ""),
        aggregates=source.aggregates,
    )
    coverage = sm._coverage(layout)
    coverage["status"] = (
        "complete"
        if coverage["observed_scenarios"] == coverage["expected_scenarios"]
        and coverage["observed_events"] == coverage["expected_events"]
        and coverage["observed_checkpoints"] == coverage["expected_checkpoints"]
        and coverage["observed_metrics"] == coverage["expected_metrics"]
        and not coverage["blocked_scenarios"]
        and not coverage["failed_scenarios"]
        and not coverage["unmarked_scenarios"]
        and not coverage["failed_metrics"]
        and not coverage["applicable_metrics"]
        and not coverage["unexecuted_metrics"]
        else "incomplete"
    )
    layout.run["coverage"] = coverage
    sm._coverage_issues(layout, coverage)
    for issue in layout.issues:
        _add(source.issues, issue)
    if source.coverage_recorded.get("status") != coverage["status"]:
        _add(source.issues, "场景来源覆盖状态与重算结果不一致")
    observed = len(
        [
            checked
            for checked in source.samples.values()
            if checked.recomputed_state == "complete"
            and checked.recorded_status == "complete"
            and not checked.incomplete
        ]
    )
    source.recomputed_status = (
        "complete"
        if source.available
        and len(source.samples) == len(expected_ids)
        and observed == len(expected_ids)
        and not [issue for issue in source.issues if _is_blocking_scenario_issue(issue)]
        else "incomplete"
    )
    if source.recorded_status != source.recomputed_status:
        _add(source.issues, "场景来源状态与重算结果不一致")
    try:
        if source.evidence:
            identity = run_judge_identity(source.evidence).model_dump(mode="json")
            source.run_judge_identity = identity
            if source.judge.get("model") != identity.get("model"):
                _add(source.issues, "裁判身份与真实评分证据不一致")
    except Exception as error:
        _add(source.issues, f"裁判身份无法从证据生成: {type(error).__name__}: {error}")
    metric_identities: dict[str, list[dict]] = {}
    for evidence in source.evidence:
        metric_identities.setdefault(evidence.metric, []).append(
            {
                "metric_class": evidence.metric_class,
                "metric_version": evidence.metric_version,
                "prompt_digest": evidence.prompt_digest,
                "judge_model": evidence.judge_model,
            }
        )
    source.metric_identities = {
        metric: sorted(
            {json.dumps(item, ensure_ascii=False, sort_keys=True) for item in rows}
        )
        for metric, rows in metric_identities.items()
    }
    for metric, identities in source.metric_identities.items():
        if len(identities) != 1:
            _add(source.issues, f"指标 {metric} 存在多个评分身份")
    return source


def _is_blocking_scenario_issue(issue: str) -> bool:
    return not issue.startswith("场景代码身份 与当前工作区不一致")


def _retrieval_formal_issues(source: RetrievalSource, dataset: EvaluationDataset) -> list[str]:
    issues = []
    if not source.available:
        return ["正式检索来源不可用"]
    if source.purpose != "formal":
        issues.append("正式检索来源必须为 formal")
    all_ids = {query["query_id"] for query in dataset.queries}
    if set(source.expected_ids) != all_ids or len(source.expected_ids) != len(dataset.queries):
        issues.append("正式检索必须覆盖全部批准查询")
    if set(source.groups) != set(GROUP_ORDER):
        issues.append("正式检索必须包含 BM25、dense、Hybrid 三组")
    for group in GROUP_ORDER:
        result = source.groups.get(group)
        if result is None:
            issues.append(f"正式检索缺少 {group} 组")
            continue
        if result.recomputed_status != "complete":
            issues.append(f"{group} 组不完整")
        if result.coverage.get("status") != "complete":
            issues.append(f"{group} 组样本覆盖不完整")
        if result.metrics is None:
            issues.append(f"{group} 组缺少指标")
        if result.unknown_candidates:
            issues.append(f"{group} 组存在未审阅候选")
        if not result.index.get("collection") or not result.index.get("manifest_sha256"):
            issues.append(f"{group} 组缺少物理索引身份")
        if result.vector_cache_required and not result.vector_cache_identity_sha256:
            issues.append(f"{group} 组缺少查询向量缓存身份")
        if not result.experiment.get("experiment_id"):
            issues.append(f"{group} 组缺少 LangSmith 实验")
        if result.concurrency.get("max_in_flight") is None or result.concurrency[
            "max_in_flight"
        ] > lr.MAX_CONCURRENCY:
            issues.append(f"{group} 组并发证据非法")
        if not result.concurrency.get("overlap_pairs"):
            issues.append(f"{group} 组未观察到并发重叠")
    try:
        expected_parameters = lr.validated_parameters()
    except Exception:
        expected_parameters = None
    if expected_parameters is not None and source.parameters != expected_parameters:
        issues.append("正式检索参数与冻结基线不一致")
    return issues


def _scenario_formal_issues(
    source: ScenarioSource,
    dataset: EvaluationDataset,
    coverage: dict,
    generation: dict,
    assertions: dict,
) -> list[str]:
    issues = []
    if not source.available:
        return ["正式场景来源不可用"]
    if source.purpose != "formal":
        issues.append("正式场景来源必须为 formal")
    if len(source.expected_ids) != len(dataset.scenarios) or set(source.expected_ids) != {
        scenario["scenario_id"] for scenario in dataset.scenarios
    }:
        issues.append("正式场景必须覆盖全部批准场景")
    if source.coverage_recorded.get("status") != coverage.get("status"):
        issues.append("正式场景来源覆盖状态与重算结果不一致")
    if coverage["observed_scenarios"] != coverage["expected_scenarios"]:
        issues.append("正式场景覆盖不完整")
    if coverage["observed_events"] != coverage["expected_events"]:
        issues.append("正式事件覆盖不完整")
    if coverage["observed_checkpoints"] != coverage["expected_checkpoints"]:
        issues.append("正式检查点覆盖不完整")
    if coverage["observed_metrics"] != coverage["expected_metrics"]:
        issues.append("正式指标覆盖不完整")
    if coverage["blocked_scenarios"]:
        issues.append("正式场景存在 blocked")
    if coverage["failed_scenarios"]:
        issues.append("正式场景存在 failed")
    if coverage["unmarked_scenarios"]:
        issues.append("正式场景存在状态不明")
    if assertions["failed"]:
        issues.append("正式场景存在失败断言")
    if assertions["missing"]:
        issues.append("正式场景存在未执行断言")
    for metric, row in generation["metrics"].items():
        if row["failed_checkpoints"]:
            issues.append(f"正式生成指标 {metric} 存在失败检查点")
        if row["applicable_checkpoints"]:
            issues.append(f"正式生成指标 {metric} 存在尚无结果的适用检查点")
        if row["missing_checkpoints"]:
            issues.append(f"正式生成指标 {metric} 存在未执行检查点")
        if row["succeeded_checkpoints"] + row["not_applicable_checkpoints"] != row[
            "expected_checkpoints"
        ]:
            issues.append(f"正式生成指标 {metric} 分母不完整")
    for scenario_id, checked in source.samples.items():
        if checked.recomputed_state != "complete":
            issues.append(f"正式场景 {scenario_id} 未完成")
        if checked.incomplete:
            issues.append(f"正式场景 {scenario_id} 评分不完整")
        if checked.recorded_status != "complete":
            issues.append(f"正式场景 {scenario_id} 来源状态不为 complete")
        if checked.run_error:
            issues.append(f"正式场景 {scenario_id} 存在运行错误")
        if checked.execution_status != "completed":
            issues.append(f"正式场景 {scenario_id} 执行未完成")
        if checked.feedback.get("scenario_status") != 1.0:
            issues.append(f"正式场景 {scenario_id} scenario_status 不为成功")
    if source.run_judge_identity is None:
        issues.append("正式场景缺少真实裁判身份证据")
    for metric, identities in source.metric_identities.items():
        if len(identities) != 1:
            issues.append(f"正式场景指标 {metric} 存在身份冲突")
    if not source.index.get("collection") or not source.index.get("manifest_sha256"):
        issues.append("正式场景缺少物理索引身份")
    if not source.experiment.get("id"):
        issues.append("正式场景缺少 LangSmith 实验")
    if source.concurrency.get("max_in_flight") is None or source.concurrency[
        "max_in_flight"
    ] > ls.MAX_CONCURRENCY:
        issues.append("正式场景并发证据非法")
    if not source.concurrency.get("overlap_pairs"):
        issues.append("正式场景未观察到并发重叠")
    generation_model = source.generation.get("model")
    try:
        parsed_model = json.loads(generation_model) if isinstance(generation_model, str) else None
    except ValueError:
        parsed_model = None
    if not isinstance(parsed_model, dict) or not parsed_model.get("model"):
        issues.append("正式场景缺少生成模型身份")
    return issues


def _combined_issues(
    retrieval: RetrievalSource, scenario: ScenarioSource
) -> list[str]:
    issues = []
    if retrieval.available and scenario.available:
        if retrieval.dataset_identity != scenario.dataset_identity:
            issues.append("检索与场景数据身份不一致")
        if retrieval.code_identity.get("head") != scenario.code_identity.get("commit"):
            issues.append("检索与场景代码提交不一致")
        if retrieval.parameters.get("threshold") != 0.0:
            issues.append("检索门槛与冻结基线不一致")
        scenario_index = {
            "collection": scenario.index.get("collection"),
            "manifest_sha256": scenario.index.get("manifest_sha256"),
        }
        for group, result in retrieval.groups.items():
            group_index = {
                "collection": result.index.get("collection"),
                "manifest_sha256": result.index.get("manifest_sha256"),
            }
            if group_index != scenario_index:
                issues.append(f"{group} 组物理索引与场景运行不一致")
    if retrieval.available and scenario.available:
        if retrieval.document.get("max_concurrency") != scenario.document.get(
            "max_concurrency"
        ):
            issues.append("检索与场景并发配置不一致")
        if retrieval.document.get("num_repetitions") != scenario.document.get(
            "num_repetitions"
        ):
            issues.append("检索与场景重复次数不一致")
    return issues


def _scenario_protocol(path: Path) -> tuple[int, str] | None:
    root, file, error = _resolve_source(path, "sdk-run.json")
    if error:
        return None
    document, error = _read_json(file)
    if error or not isinstance(document, dict):
        return None
    try:
        version = ls.feedback_version(document)
        mode = ls.run_scoring_mode(document)
    except ls.ScenarioSDKError as error:
        raise SDKSummaryError(str(error)) from None
    if mode == "external" and version != ls.EXTERNAL_FEEDBACK_VERSION:
        raise SDKSummaryError("外部评分模式与反馈版本不一致")
    return version, mode


def _external_combined_issues(
    retrieval: RetrievalSource, source: es.ExternalScenarioSource
) -> list[str]:
    issues = []
    if not retrieval.available or source.root is None:
        return issues
    document = source.document
    if retrieval.dataset_identity != document.get("dataset"):
        issues.append("检索与场景数据身份不一致")
    code = document.get("code_identity") or {}
    if retrieval.code_identity.get("head") != code.get("commit"):
        issues.append("检索与场景代码提交不一致")
    if retrieval.parameters.get("threshold") != 0.0:
        issues.append("检索门槛与冻结基线不一致")
    scenario_index = {
        "collection": (document.get("index") or {}).get("collection"),
        "manifest_sha256": (document.get("index") or {}).get("manifest_sha256"),
    }
    for group, result in retrieval.groups.items():
        group_index = {
            "collection": result.index.get("collection"),
            "manifest_sha256": result.index.get("manifest_sha256"),
        }
        if group_index != scenario_index:
            issues.append(f"{group} 组物理索引与场景运行不一致")
    if retrieval.document.get("max_concurrency") != document.get(
        "max_concurrency"
    ):
        issues.append("检索与场景并发配置不一致")
    if retrieval.document.get("num_repetitions") != document.get(
        "num_repetitions"
    ):
        issues.append("检索与场景重复次数不一致")
    return issues


def _external_retrieval_view(
    retrieval: RetrievalSource,
    dataset: EvaluationDataset,
    source: es.ExternalScenarioSource,
) -> tuple[dict, list[str]]:
    selected_ids = [
        scenario_id
        for scenario_id in source.expected_scenario_ids
        if scenario_id
        in {scenario["scenario_id"] for scenario in dataset.scenarios}
    ]
    selected = select_scenarios(dataset, selected_ids)
    run_id = source.run_id or ""
    layout = sm.RunLayout(
        run_dir=source.root or BACKEND,
        run={},
        plan=build_execution_plan(dataset, selected, run_id),
        dataset=dataset,
        dataset_scenarios={
            item["scenario_id"]: item for item in dataset.scenarios
        },
        full_plan=build_execution_plan(dataset, list(dataset.scenarios), run_id),
        aggregates={},
    )
    summary = sm._retrieval_summary(layout, [])
    first_group = next(iter(retrieval.groups.values()), None)
    searches = 0
    with_search = 0
    if source.evidence is not None:
        for row in source.evidence.scenarios:
            searches += len(row.execution.retrieval)
            if row.execution.retrieval:
                with_search += 1
    summary.update(
        {
            "searches": searches,
            "scenarios_with_search": with_search,
            "experiment": retrieval.document.get("experiment"),
            "rerank": retrieval.document.get("rerank_identity"),
            "index": None if first_group is None else first_group.index,
            "groups": {
                group: _group_summary(result)
                for group, result in retrieval.groups.items()
            },
            "overall_status": retrieval.recomputed_status,
            "all_groups_complete": (
                set(retrieval.groups) >= set(GROUP_ORDER)
                and all(
                    result.recomputed_status == "complete"
                    for group, result in retrieval.groups.items()
                    if group in GROUP_ORDER or group in OPTIONAL_GROUPS
                )
            ),
            "expected_groups": [*GROUP_ORDER, *OPTIONAL_GROUPS],
        }
    )
    blocking = list(retrieval.issues)
    if source.purpose == "formal":
        if not retrieval.available:
            blocking.append("正式检索来源不可用")
        elif retrieval.purpose != "formal":
            blocking.append("正式检索来源必须为 formal")
        else:
            blocking.extend(_retrieval_formal_issues(retrieval, dataset))
    blocking.extend(_external_combined_issues(retrieval, source))
    return summary, sorted(set(blocking))


def build_summary(
    retrieval_path: Path,
    scenario_path: Path,
    dataset_path: Path,
    *,
    scope: str = "auto",
) -> dict:
    dataset = load_for_evaluation(dataset_path)
    retrieval = load_retrieval_source(retrieval_path, dataset)
    protocol = _scenario_protocol(scenario_path)
    if protocol is not None and protocol[1] == "external":
        source_path = (
            scenario_path
            if scenario_path.is_dir()
            else scenario_path.parent
        )
        source = es.load_external_scenario_source(source_path, dataset)
        retrieval_summary, retrieval_blocking = _external_retrieval_view(
            retrieval, dataset, source
        )
        return es.build_summary(
            source,
            scope=scope,
            retrieval=retrieval_summary,
            retrieval_blocking=retrieval_blocking,
        )
    scenario = load_scenario_source(scenario_path, dataset)
    selected = select_scenarios(dataset, scenario.expected_ids)
    run_id = scenario.run_id or ""
    plan = build_execution_plan(dataset, selected, run_id)
    full_plan = build_execution_plan(dataset, list(dataset.scenarios), run_id)
    layout = sm.RunLayout(
        run_dir=scenario.root or BACKEND,
        run={},
        plan=plan,
        dataset=dataset,
        dataset_scenarios={
            item["scenario_id"]: item for item in dataset.scenarios
        },
        full_plan=full_plan,
        aggregates=scenario.aggregates,
    )
    for issue in retrieval.issues + scenario.issues:
        _add(layout.issues, issue)
    generation = sm._aggregate(layout)
    coverage = sm._coverage(layout)
    coverage["status"] = (
        "complete"
        if coverage["observed_scenarios"] == coverage["expected_scenarios"]
        and coverage["observed_events"] == coverage["expected_events"]
        and coverage["observed_checkpoints"] == coverage["expected_checkpoints"]
        and coverage["observed_metrics"] == coverage["expected_metrics"]
        and not coverage["blocked_scenarios"]
        and not coverage["failed_scenarios"]
        and not coverage["unmarked_scenarios"]
        and not coverage["failed_metrics"]
        and not coverage["applicable_metrics"]
        and not coverage["unexecuted_metrics"]
        else "incomplete"
    )
    layout.run["coverage"] = coverage
    sm._coverage_issues(layout, coverage)
    assertions = sm._assertion_summary(layout)
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
    scope_name, plan_formal = sm._scope(layout, scope)
    formal = (
        plan_formal
        and scope_name == "formal"
        and retrieval.purpose == "formal"
        and scenario.purpose == "formal"
    )
    if not formal:
        _add(layout.issues, "release:来源未满足正式全量范围")
    if scenario.available and retrieval.available:
        for issue in _combined_issues(retrieval, scenario):
            _add(layout.issues, issue)
    if retrieval.available and retrieval.purpose == "formal":
        for issue in _retrieval_formal_issues(retrieval, dataset):
            _add(layout.issues, issue)
    if scenario.available and scenario.purpose == "formal":
        for issue in _scenario_formal_issues(
            scenario, dataset, coverage, generation, assertions
        ):
            _add(layout.issues, issue)
    for aggregate in layout.aggregates.values():
        for issue in aggregate.issues:
            _add(layout.issues, issue)
    blocking = sorted(set(layout.issues))
    all_groups_complete = (
        set(retrieval.groups) >= set(GROUP_ORDER)
        and all(
            result.recomputed_status == "complete"
            for group, result in retrieval.groups.items()
            if group in GROUP_ORDER or group in OPTIONAL_GROUPS
        )
    )
    scenario_retrieval = sm._retrieval_summary(
        layout, scenario.retrieval_records
    )
    scenario_retrieval.update(
        {
            "groups": {
                group: _group_summary(result)
                for group, result in retrieval.groups.items()
            },
            "overall_status": retrieval.recomputed_status,
            "all_groups_complete": all_groups_complete,
            "expected_groups": [*GROUP_ORDER, *OPTIONAL_GROUPS],
        }
    )
    scenario_rows = sm._scenario_rows(layout)
    for scenario_id, checked in scenario.samples.items():
        if checked.artifact is None:
            continue
        scenario_rows[scenario_id]["assertion_checkpoints"] = [
            {
                "checkpoint_id": checkpoint.checkpoint_id,
                "event_id": checkpoint.event_id,
                "passed": sum(1 for outcome in checkpoint.outcomes if outcome.passed),
                "failed": sum(1 for outcome in checkpoint.outcomes if not outcome.passed),
                "binding_error": checkpoint.binding_error,
                "runner_error": checkpoint.runner_error,
            }
            for checkpoint in checked.artifact.assertions
        ]
    sdk = _sdk_evidence(retrieval, scenario, dataset)
    summary = {
        "schema": SUMMARY_SCHEMA,
        "run_id": f"{retrieval.run_id or 'missing'}|{scenario.run_id or 'missing'}",
        "scope": scope_name,
        "formal_scope": formal,
        "generated_at": datetime.now(UTC).isoformat(),
        "sources": {
            "retrieval": _source_summary(retrieval, dataset),
            "scenario": _source_summary(scenario, dataset),
        },
        "run": {
            "purpose": scenario.purpose,
            "started_at": scenario.started_at,
            "finished_at": scenario.finished_at,
            "dataset": scenario.dataset_identity,
            "identity": {
                "code": scenario.code_identity,
                "retrieval_code": retrieval.code_identity,
                "generation": scenario.generation,
                "judge": scenario.judge,
                "judge_evidence": scenario.run_judge_identity,
                "metric_identities": scenario.metric_identities,
                "index": scenario.index,
                "retrieval_index": {
                    group: result.index for group, result in retrieval.groups.items()
                },
            },
            "blocked": coverage["blocked_scenarios"],
            "failed": coverage["failed_scenarios"],
            "incomplete": [
                f"{scenario_id}:{item}"
                for scenario_id, checked in scenario.samples.items()
                for item in checked.incomplete
            ],
            "context_gaps": [
                f"{scenario_id}:{gap.reason}"
                for scenario_id, checked in scenario.samples.items()
                if checked.artifact is not None
                for gap in checked.artifact.context_gaps
            ],
        },
        "coverage": coverage,
        "generation": generation,
        "assertions": assertions,
        "retrieval": scenario_retrieval,
        "scenarios": scenario_rows,
        "evidence": {
            "judge_records": len(scenario.evidence),
            "attempt_models": sorted(scenario.attempt_models),
            "scoring_artifacts": len(
                [checked for checked in scenario.samples.values() if checked.artifact]
            ),
            "execution_artifacts": len(
                [checked for checked in scenario.samples.values() if checked.execution]
            ),
            "retrieval_groups": len(retrieval.groups),
            "retrieval_samples": sum(
                len(result.samples) for result in retrieval.groups.values()
            ),
        },
        "sdk": sdk,
        "warnings": sorted(set(layout.warnings)),
        "release": {
            "effective": formal and not blocking,
            "blocking_issues": blocking,
        },
    }
    return summary


def _group_summary(result: RetrievalGroupResult) -> dict:
    return {
        "status": (
            "blocked_unreviewed_candidates"
            if result.unknown_candidates
            else result.recomputed_status
        ),
        "recomputed_status": result.recomputed_status,
        "recorded_status": result.recorded_status,
        "error": result.error,
        "coverage": result.coverage,
        "metrics": result.metrics,
        "index": result.index,
        "vector_cache_identity_sha256": result.vector_cache_identity_sha256,
        "vector_cache_required": result.vector_cache_required,
        "experiment": result.experiment,
        "concurrency": result.concurrency,
        "milvus_rpc": result.milvus_rpc,
        "latency": result.latency,
        "unknown_candidates": result.unknown_candidates,
        "issues": result.issues,
    }


def _source_summary(source: RetrievalSource | ScenarioSource, dataset: EvaluationDataset) -> dict:
    kind = "retrieval" if isinstance(source, RetrievalSource) else "scenario"
    groups = {}
    if isinstance(source, RetrievalSource):
        groups = {
            group: {
                "status": result.recomputed_status,
                "recorded_status": result.recorded_status,
                "coverage": result.coverage,
                "error": result.error,
                "experiment": result.experiment,
            }
            for group, result in source.groups.items()
        }
    return {
        "kind": kind,
        "path": _display_path(source.path),
        "root": _display_path(source.root),
        "available": source.available,
        "run_id": source.run_id,
        "purpose": source.purpose,
        "recorded_status": source.recorded_status,
        "recomputed_status": source.recomputed_status,
        "started_at": source.started_at,
        "finished_at": source.finished_at,
        "experiment": source.experiment if isinstance(source, ScenarioSource) else None,
        "parameters": source.parameters if isinstance(source, RetrievalSource) else None,
        "max_concurrency": source.document.get("max_concurrency"),
        "num_repetitions": source.document.get("num_repetitions"),
        "feedback_version": (
            source.feedback_version
            if isinstance(source, ScenarioSource)
            else None
        ),
        "dataset_identity": source.dataset_identity,
        "code_identity": source.code_identity,
        "groups": groups,
        "issues": source.issues,
    }


def _sdk_evidence(
    retrieval: RetrievalSource, scenario: ScenarioSource, dataset: EvaluationDataset
) -> dict:
    group_evidence = {}
    for group, result in retrieval.groups.items():
        group_evidence[group] = {
            "latency": result.latency,
            "requests": {
                "target_calls": result.concurrency.get("calls"),
                "milvus_rpc_calls": result.milvus_rpc.get("calls"),
            },
            "concurrency": {
                key: result.concurrency.get(key)
                for key in ("calls", "max_in_flight", "overlap_pairs")
            },
            "experiment": result.experiment,
            "remote_feedback_tolerance": lr.REMOTE_SCORE_TOLERANCE,
            "max_feedback_difference": max(
                (
                    abs(result.remote_feedback[qid][key] - value)
                    for qid, values in result.local_scores.items()
                    for key, value in values.items()
                    if key in result.remote_feedback.get(qid, {})
                ),
                default=None,
            ),
        }
    model_calls = 0
    tool_calls = 0
    input_tokens = 0
    output_tokens = 0
    token_seen = False
    for checked in scenario.samples.values():
        if checked.artifact is None:
            continue
        for event in checked.artifact.events:
            model_calls += len(event.model_calls)
            tool_calls += len(event.tool_calls)
            for call in event.model_calls:
                if call.input_tokens is not None:
                    input_tokens += call.input_tokens
                    token_seen = True
                if call.output_tokens is not None:
                    output_tokens += call.output_tokens
                    token_seen = True
    judge_calls = sum(len(evidence.judge_calls) for evidence in scenario.evidence)
    chain_steps = sum(len(evidence.chain_steps) for evidence in scenario.evidence)
    return {
        "retrieval": {
            "started_at": retrieval.started_at,
            "finished_at": retrieval.finished_at,
            "duration_seconds": _duration_seconds(
                retrieval.started_at, retrieval.finished_at
            ),
            "groups": group_evidence,
            "remote_verification": "来源记录 coverage/parity；本次未重连 LangSmith",
        },
        "scenario": {
            "started_at": scenario.started_at,
            "finished_at": scenario.finished_at,
            "duration_seconds": _duration_seconds(
                scenario.started_at, scenario.finished_at
            ),
            "latency": _latency_report(
                [
                    {
                        "run_start_time": checked.run_start_time,
                        "run_end_time": checked.run_end_time,
                    }
                    for checked in scenario.samples.values()
                    if checked.run_start_time and checked.run_end_time
                ]
            ),
            "requests": {
                "target_calls": scenario.concurrency.get("calls"),
                "model_calls": model_calls,
                "tool_calls": tool_calls,
                "judge_calls": judge_calls,
                "judge_chain_steps": chain_steps,
            },
            "usage": {
                "input_tokens": input_tokens if token_seen else None,
                "output_tokens": output_tokens if token_seen else None,
            },
            "concurrency": {
                key: scenario.concurrency.get(key)
                for key in ("calls", "max_in_flight", "overlap_pairs")
            },
            "experiment": scenario.experiment,
            "remote_verification": "来源记录 coverage/与本地评分一致性；本次未重连 LangSmith",
        },
        "dataset": dataset.identity(),
    }


def _value(value: Any) -> str:
    if value is None:
        return "未采集"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "---|" * len(headers),
    ]
    lines.extend("| " + " | ".join(_value(cell) for cell in row) + " |" for row in rows)
    return lines


def _metric_cells(stats: dict | None) -> list[Any]:
    stats = stats or {}
    return [
        stats.get("with_labels", 0),
        stats.get("empty_expectation_queries", 0),
        stats.get("recall@20"),
        stats.get("hit@5"),
        stats.get("mrr@5"),
        stats.get("ndcg@5"),
        stats.get("precision@5"),
        stats.get("false_return_rate"),
    ]


def _metric_row(name: str, stats: dict | None, status: str | None = None) -> list[Any]:
    cells = _metric_cells(stats)
    if status is None:
        return [name, *cells]
    return [name, status, *cells, (stats or {}).get("empty_returned")]


def report_lines(summary: dict) -> list[str]:
    if summary.get("schema") == es.EXTERNAL_SUMMARY_SCHEMA:
        return es.report_lines(summary)
    sources = summary["sources"]
    release = summary["release"]
    run = summary["run"]
    lines = [
        f"# SDK 评测汇总 `{summary['run_id']}`",
        "",
        f"- 范围：{'正式全量' if summary['formal_scope'] else '定向验证（仅诊断）'}。",
        f"- 有效发布：{'是' if release['effective'] else '否'}。",
        f"- 数据版本：`{run.get('dataset', {}).get('version', '未采集')}`。",
        f"- 检索来源：`{sources['retrieval']['run_id'] or '未采集'}`，状态 `{sources['retrieval']['recomputed_status']}`。",
        f"- 场景来源：`{sources['scenario']['run_id'] or '未采集'}`，状态 `{sources['scenario']['recomputed_status']}`。",
        f"- 起止时间：`{sources['scenario']['started_at'] or '未采集'}` 至 `{sources['scenario']['finished_at'] or '未采集'}`。",
        "",
        "## 检索指标",
        "",
    ]
    retrieval_groups = summary["retrieval"].get("groups", {})
    group_rows = []
    for group in (*GROUP_ORDER, *OPTIONAL_GROUPS):
        result = retrieval_groups.get(group)
        if result is None:
            group_rows.append(
                [group, "未执行", "未执行", "未执行", "未执行", "未执行", "未执行", "未执行", "未执行", "未执行", "未执行"]
            )
        else:
            group_rows.append(
                _metric_row(
                    group,
                    (result.get("metrics") or {}).get("overall"),
                    result.get("status"),
                )
            )
    lines.extend(
        _table(
            [
                "组",
                "状态",
                "有答案",
                "无答案",
                "Recall@20",
                "Hit@5",
                "MRR@5",
                "nDCG@5",
                "Precision@5",
                "误返回率",
                "空返回数",
            ],
            group_rows,
        )
    )
    lines.append(
        f"- 三组完整：{'是' if summary['retrieval'].get('all_groups_complete') else '否'}；"
        f"检索来源状态：`{summary['retrieval'].get('overall_status')}`。"
    )
    lines.append("")
    for group in GROUP_ORDER:
        result = retrieval_groups.get(group)
        if result is None:
            lines.append(f"- `{group}`：未执行或被阻断。")
            continue
        if result.get("error"):
            lines.append(f"- `{group}`：{result['error']}。")
        metrics = result.get("metrics") or {}
        if metrics.get("by_purpose"):
            lines.extend(["", f"### {group} 按用途", ""])
            lines.extend(
                _table(
                    ["用途", "有答案", "无答案", "Recall@20", "Hit@5", "MRR@5", "nDCG@5", "Precision@5", "误返回率"],
                    [
                        _metric_row(name, stats)
                        for name, stats in metrics["by_purpose"].items()
                    ],
                )
            )
        if metrics.get("by_tag"):
            lines.extend(["", f"### {group} 按标签", ""])
            lines.extend(
                _table(
                    ["标签", "有答案", "无答案", "Recall@20", "Hit@5", "MRR@5", "nDCG@5", "Precision@5", "误返回率"],
                    [
                        _metric_row(name, stats)
                        for name, stats in metrics["by_tag"].items()
                    ],
                )
            )
    lines.extend(["", "## 生成指标", ""])
    generation = summary["generation"]["metrics"]
    lines.extend(
        _table(
            [
                "指标",
                "预期检查点",
                "succeeded",
                "failed",
                "N/A",
                "applicable",
                "未执行",
                "可用场景",
                "完整场景",
                "含部分成功均值",
                "仅完整成功均值",
            ],
            [
                [
                    metric,
                    row["expected_checkpoints"],
                    row["succeeded_checkpoints"],
                    row["failed_checkpoints"],
                    row["not_applicable_checkpoints"],
                    row["applicable_checkpoints"],
                    row["missing_checkpoints"],
                    row["available_scenarios"],
                    row["complete_scenarios"],
                    row["mean_including_partial"],
                    row["mean_complete_only"],
                ]
                for metric, row in generation.items()
            ],
        )
    )
    lines.extend(["", "## 覆盖与断言", ""])
    coverage = summary["coverage"]
    assertions = summary["assertions"]
    lines.extend(
        _table(
            ["项目", "预期", "已完成", "缺失"],
            [
                ["场景", coverage["expected_scenarios"], coverage["observed_scenarios"], len(coverage["missing_scenarios"])],
                ["事件", coverage["expected_events"], coverage["observed_events"], len(coverage["missing_events"])],
                ["检查点", coverage["expected_checkpoints"], coverage["observed_checkpoints"], len(coverage["missing_checkpoints"])],
                ["生成指标", coverage["expected_metrics"], coverage["observed_metrics"], coverage["unexecuted_metrics"]],
                ["确定性断言", assertions["expected"], assertions["observed"], len(assertions["missing"])],
            ],
        )
    )
    lines.append(
        f"- 指标状态：succeeded {coverage['succeeded_metrics']}；failed {coverage['failed_metrics']}；"
        f"not_applicable {coverage['not_applicable_metrics']}；applicable {coverage['applicable_metrics']}。"
    )
    lines.append(
        f"- 断言失败 {assertions['failed']}；未执行 {len(assertions['missing'])}。"
    )
    for label, values in (
        ("blocked 场景", coverage["blocked_scenarios"]),
        ("failed 场景", coverage["failed_scenarios"]),
        ("状态不明场景", coverage["unmarked_scenarios"]),
        ("未执行指标", coverage["missing_metrics"]),
    ):
        if values:
            lines.append(f"- {label}：{', '.join(values) if isinstance(values, list) else values}。")
    gaps = run.get("context_gaps", [])
    lines.append(f"- 上下文缺口：{len(gaps)}。")
    if gaps:
        lines.extend(f"  - {gap}" for gap in gaps[:20])
    lines.extend(["", "## 场景与检查点", ""])
    for scenario_id, row in summary["scenarios"].items():
        lines.append(
            f"- `{scenario_id}`：状态 {row['state']}；事件 {row['observed_events']}/{row['expected_events']}；"
            f"检查点 {row['observed_checkpoints']}/{row['expected_checkpoints']}；"
            f"断言失败 {row['assertions']['failed']}。"
        )
        metrics = row.get("metrics", {})
        if metrics:
            lines.extend(
                _table(
                    ["指标", "检查点", "状态", "分数", "原因/错误"],
                    [
                        [metric, cell["checkpoint_id"], cell["status"], cell["score"], cell["reason"] or cell["error"] or "无"]
                        for metric, cells in metrics.items()
                        for cell in cells
                    ],
                )
            )
        assertions = row.get("assertion_checkpoints")
        if assertions:
            lines.extend(
                _table(
                    ["断言检查点", "事件", "通过", "失败", "绑定错误", "装配错误"],
                    [
                        [
                            item["checkpoint_id"],
                            item["event_id"],
                            item["passed"],
                            item["failed"],
                            item["binding_error"] or "无",
                            item["runner_error"] or "无",
                        ]
                        for item in assertions
                    ],
                )
            )
    lines.extend(["", "## 证据采集", ""])
    sdk = summary["sdk"]
    for group, evidence in sdk["retrieval"].get("groups", {}).items():
        latency = evidence.get("latency") or {}
        requests = evidence.get("requests") or {}
        concurrency = evidence.get("concurrency") or {}
        lines.append(
            f"- 检索 `{group}`：样本延迟 min/mean/max "
            f"{_value(latency.get('min_seconds'))}/{_value(latency.get('mean_seconds'))}/"
            f"{_value(latency.get('max_seconds'))} 秒；target 调用 {_value(requests.get('target_calls'))}；"
            f"Milvus RPC {_value(requests.get('milvus_rpc_calls'))}；并发上限 "
            f"{_value(concurrency.get('max_in_flight'))}；重叠对 {_value(concurrency.get('overlap_pairs'))}。"
        )
        lines.append(
            f"- 检索 `{group}` 远端 feedback 容差 {_value(evidence.get('remote_feedback_tolerance'))}；"
            f"最大差异 {_value(evidence.get('max_feedback_difference'))}。"
        )
    scenario_sdk = sdk["scenario"]
    latency = scenario_sdk.get("latency") or {}
    requests = scenario_sdk.get("requests") or {}
    usage = scenario_sdk.get("usage") or {}
    concurrency = scenario_sdk.get("concurrency") or {}
    lines.append(
        f"- 场景：样本延迟 min/mean/max {_value(latency.get('min_seconds'))}/"
        f"{_value(latency.get('mean_seconds'))}/{_value(latency.get('max_seconds'))} 秒；"
        f"target 调用 {_value(requests.get('target_calls'))}；模型调用 {_value(requests.get('model_calls'))}；"
        f"工具调用 {_value(requests.get('tool_calls'))}；裁判调用 {_value(requests.get('judge_calls'))}；"
        f"裁判链步骤 {_value(requests.get('judge_chain_steps'))}。"
    )
    lines.append(
        f"- 场景用量：输入 tokens {_value(usage.get('input_tokens'))}；输出 tokens "
        f"{_value(usage.get('output_tokens'))}。并发上限 {_value(concurrency.get('max_in_flight'))}；"
        f"重叠对 {_value(concurrency.get('overlap_pairs'))}。"
    )
    lines.append(
        f"- SDK 配置：检索 max_concurrency {_value(sources['retrieval'].get('max_concurrency'))}，"
        f"num_repetitions {_value(sources['retrieval'].get('num_repetitions'))}；"
        f"场景 max_concurrency {_value(sources['scenario'].get('max_concurrency'))}，"
        f"num_repetitions {_value(sources['scenario'].get('num_repetitions'))}。"
    )
    for kind in ("retrieval", "scenario"):
        experiment = None
        if kind == "retrieval":
            experiment = next(
                (
                    result.get("experiment")
                    for result in retrieval_groups.values()
                    if result.get("experiment", {}).get("experiment_id")
                ),
                None,
            )
        else:
            experiment = sources[kind].get("experiment") or summary["sdk"]["scenario"].get("experiment")
        if experiment:
            lines.append(
                f"- LangSmith {kind}：ID `{experiment.get('experiment_id') or experiment.get('id') or '未采集'}`；"
                f"链接 `{experiment.get('experiment_url') or experiment.get('url') or '未采集'}`。"
            )
    lines.append(f"- 远端核验：{sdk['retrieval']['remote_verification']}；{sdk['scenario']['remote_verification']}。")
    lines.extend(["", "## 身份与来源", ""])
    dataset_identity = sources["scenario"]["dataset_identity"] or {}
    lines.append(f"- 检索来源目录：`{sources['retrieval']['root'] or '未采集'}`。")
    lines.append(f"- 场景来源目录：`{sources['scenario']['root'] or '未采集'}`。")
    lines.append(
        f"- 批准数据：version `{dataset_identity.get('version', '未采集')}`；"
        f"manifest `{dataset_identity.get('manifest_sha256', '未采集')}`；"
        f"content `{dataset_identity.get('content_sha256', '未采集')}`；"
        f"corpus `{dataset_identity.get('corpus_sha256', '未采集')}`。"
    )
    lines.append(f"- 检索参数：`{_value(sources['retrieval'].get('parameters'))}`。")
    lines.append(f"- 场景代码提交：`{run.get('identity', {}).get('code', {}).get('commit', '未采集')}`；"
                 f"dirty `{_value(run.get('identity', {}).get('code', {}).get('dirty'))}`。")
    lines.append(f"- 场景物理索引：`{run.get('identity', {}).get('index', {}).get('collection', '未采集')}`；"
                 f"清单 `{run.get('identity', {}).get('index', {}).get('manifest_sha256', '未采集')}`。")
    lines.append(f"- 生成模型：`{run.get('identity', {}).get('generation', {}).get('model', '未采集')}`。")
    lines.append(f"- 生成提示：`{run.get('identity', {}).get('generation', {}).get('prompt_version', '未采集')}`。")
    lines.append(f"- 裁判模型：`{run.get('identity', {}).get('judge', {}).get('model', '未采集')}`。")
    judge_evidence = run.get("identity", {}).get("judge_evidence") or {}
    lines.append(
        f"- 裁判证据身份：model `{judge_evidence.get('model', '未采集')}`；"
        f"prompt `{judge_evidence.get('prompt_version', '未采集')}`；"
        f"metric `{judge_evidence.get('metric_version', '未采集')}`。"
    )
    for metric, identities in sorted(
        (run.get("identity", {}).get("metric_identities") or {}).items()
    ):
        if len(identities) == 1:
            lines.append(f"- 指标身份 `{metric}`：{identities[0]}。")
        else:
            lines.append(f"- 指标身份 `{metric}`：身份冲突 {len(identities)} 项。")
    lines.extend(["", "## 门禁", ""])
    if release["blocking_issues"]:
        lines.extend(f"- {issue}" for issue in release["blocking_issues"])
    else:
        lines.append("- 无阻塞项。")
    if summary["warnings"]:
        lines.extend(["", "## 警告", ""])
        lines.extend(f"- {warning}" for warning in summary["warnings"])
    return lines


def write_summary_files(output: Path, summary: dict) -> dict[str, str]:
    output.mkdir(parents=True, exist_ok=True)
    summary_path = output / sm.SUMMARY_NAME
    report_path = output / sm.REPORT_NAME
    summary_text = json.dumps(
        summary, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
    ) + "\n"
    report_text = "\n".join(report_lines(summary)) + "\n"
    _atomic_write(summary_path, summary_text)
    _atomic_write(report_path, report_text)
    return {"summary": str(summary_path), "report": str(report_path)}


def _atomic_write(path: Path, text: str) -> None:
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def write_v3(path: Path, summary: dict) -> Path:
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    block = "\n".join([START_MARKER, *report_lines(summary), END_MARKER])
    starts = current.count(START_MARKER)
    ends = current.count(END_MARKER)
    if starts or ends:
        if starts != 1 or ends != 1:
            raise SDKSummaryError("v3 汇总标记不唯一，拒绝覆盖")
        start = current.index(START_MARKER)
        end = current.index(END_MARKER)
        if end <= start:
            raise SDKSummaryError("v3 汇总标记顺序非法，拒绝覆盖")
        candidate = current[:start] + block + current[end + len(END_MARKER):]
    else:
        candidate = (
            current.rstrip() + "\n\n" + block + "\n"
            if current.strip()
            else block + "\n"
        )
    if candidate.count(START_MARKER) != 1 or candidate.count(END_MARKER) != 1:
        raise SDKSummaryError("v3 候选内容标记校验失败")
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, candidate)
    return path


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SDK 评测汇总、诊断报告与正式门禁")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("report", "check", "write-v3"):
        command = commands.add_parser(name)
        command.add_argument("--retrieval", type=Path, required=True, help="检索 SDK run.json 或运行目录")
        command.add_argument("--scenarios", type=Path, required=True, help="场景 SDK sdk-run.json 或运行目录")
        command.add_argument("--dataset", type=Path, required=True, help="批准数据版本目录")
        command.add_argument(
            "--scope",
            choices=("auto", "formal", "validation"),
            default="auto",
            help="发布范围；定向范围始终不能作为有效发布",
        )
        if name in {"report", "write-v3"}:
            command.add_argument("--output", type=Path, required=True, help="汇总输出目录")
        else:
            command.add_argument("--output", type=Path, default=None)
        if name == "write-v3":
            command.add_argument("--v3", type=Path, required=True, help="v3 文档路径")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    summary = build_summary(
        args.retrieval, args.scenarios, args.dataset, scope=args.scope
    )
    if args.command == "report":
        paths = write_summary_files(args.output, summary)
        print(
            json.dumps(
                {
                    "run_id": summary["run_id"],
                    "scope": summary["scope"],
                    "effective_release": summary["release"]["effective"],
                    "blocking_issues": summary["release"]["blocking_issues"],
                    **paths,
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.command == "write-v3":
        paths = write_summary_files(args.output, summary)
        v3_path = write_v3(args.v3, summary)
        print(
            json.dumps(
                {
                    "run_id": summary["run_id"],
                    "scope": summary["scope"],
                    "effective_release": summary["release"]["effective"],
                    "blocking_issues": summary["release"]["blocking_issues"],
                    "v3": str(v3_path),
                    **paths,
                },
                ensure_ascii=False,
            )
        )
        return 0
    print(
        json.dumps(
            {
                "run_id": summary["run_id"],
                "scope": summary["scope"],
                "effective_release": summary["release"]["effective"],
                "blocking_issues": summary["release"]["blocking_issues"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if summary["release"]["effective"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DatasetError as error:
        raise SystemExit(f"评测数据未就绪: {error}") from None
    except (SDKSummaryError, es.ExternalSummaryError) as error:
        raise SystemExit(str(error)) from None
