import argparse
import hashlib
import json
import os
import re
import time
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Sequence
from uuid import NAMESPACE_URL, uuid5

from dotenv import load_dotenv
from langfuse.api import NotFoundError
from langsmith import Client
from langsmith.utils import LangSmithNotFoundError
from pydantic import Field, ValidationError, model_validator

from evaluation.contracts import (
    EXTERNAL_JUDGE_CHECKPOINT_SCHEMA_VERSION,
    EXTERNAL_JUDGE_METRICS,
    GENERATION_METRICS,
    DatasetIdentity,
    EvaluationContract,
    EvidenceLink,
    ExternalJudgeApplicability,
    ExternalJudgeCheckpointInputs,
    ExternalJudgeCheckpointOutputs,
    ExternalJudgeCheckpointUpload,
    ExternalJudgeContext,
    ExternalJudgeMetadata,
    ExternalJudgeReferenceOutputs,
    ExternalJudgeRetrievalEvidence,
    ExternalJudgeUserRequest,
    GenerationReference,
    MetricApplicability,
    NonEmptyText,
    Sha256,
)
from evaluation.dataset import EvaluationDataset, load_for_evaluation
from evaluation.langfuse_scenarios import (
    EXTERNAL_FEEDBACK_VERSION,
    EXTERNAL_PROTOCOL_VERSION,
    SDK_RUN_NAME,
    ScenarioSDKError,
    feedback_version,
    make_client,
    require_credentials,
    run_scoring_mode,
)
from evaluation.langfuse_scenarios import (
    SCHEMA as SCENARIO_SDK_SCHEMA,
)
from evaluation.scenario_evidence import (
    search_evidence_from_tool,
    sha256_text,
)
from evaluation.scenarios import (
    DEFAULT_VERSION_DIR,
    EXECUTION_FILENAME,
    EXTERNAL_SCORING_FILENAME,
    ExecutionIdentityError,
    ExternalScenarioScoring,
    ExternalScoringReadiness,
    GenerationCheckpointEvidence,
    RequestBoundaryEvidence,
    ScenarioExecution,
    SearchStageRecord,
    frozen_scenario_sha256,
    generation_reference,
    load_execution,
    load_external_scoring,
    scenario_plan,
    score_execution_external,
)
from evaluation.summary import (
    ARTIFACT_FILES,
    SummaryError,
    build_summary,
    load_run,
)

RECEIPT_SCHEMA = "fit-agent-generation-upload-receipt-1"
PENDING_SCHEMA = "fit-agent-generation-upload-pending-1"
PAYLOAD_SCHEMA = "fit-agent-generation-upload-payload-1"
RECEIPT_NAME = "langsmith-generation.json"
PENDING_NAME = "langsmith-generation.pending.json"
PAYLOAD_NAME = "generation-payload.json"
FEEDBACK_BATCH = 100
BACKEND = Path(__file__).resolve().parents[1]
EXTERNAL_APPLICABILITY_SCHEMA = "fit-agent-external-judge-applicability-1"
EXTERNAL_APPLICABILITY_REVIEW_SCHEMA = (
    "fit-agent-external-judge-applicability-review-1"
)
EXTERNAL_PAYLOAD_SCHEMA = "fit-agent-external-judge-upload-payload-langfuse-2"
EXTERNAL_LEDGER_SCHEMA = "fit-agent-external-judge-coverage-ledger-langfuse-2"
EXTERNAL_DATASET_RECEIPT_SCHEMA = (
    "fit-agent-external-judge-dataset-receipt-langfuse-2"
)
EXTERNAL_EXPERIMENT_RECEIPT_SCHEMA = (
    "fit-agent-external-judge-experiment-receipt-langfuse-2"
)
EXTERNAL_EXPERIMENT_PENDING_SCHEMA = (
    "fit-agent-external-judge-experiment-pending-langfuse-2"
)
EXTERNAL_PRESENTATION_VERSION = "fit-agent-external-judge-presentation-2"
EXTERNAL_REMOTE_PREFIX = "fit-agent-external-judge-langfuse-v2"
EXTERNAL_METRIC_METADATA_PREFIX = "judge_metric_"
EXTERNAL_OBSERVATION_SCOPE = "isExperimentItemRootSpan"
EXTERNAL_RULE_SAMPLING = 1.0
EXTERNAL_DIR = "external-judge"
APPLICABILITY_NAME = "applicability.json"
APPLICABILITY_REVIEW_NAME = "applicability.review.json"
EXTERNAL_PAYLOAD_NAME = "checkpoints-payload.json"
EXTERNAL_LEDGER_NAME = "coverage-ledger.json"
EXTERNAL_DATASET_RECEIPT_NAME = "checkpoint-dataset.json"
EXTERNAL_EXPERIMENT_RECEIPT_NAME = "checkpoint-experiment.json"
EXTERNAL_EXPERIMENT_PENDING_NAME = "checkpoint-experiment.pending.json"
EXTERNAL_RETRIEVAL_TOOL = "search_exercises"
EXTERNAL_REMOTE_VERIFY_TIMEOUT_SECONDS = 30.0
EXTERNAL_EXAMPLE_METADATA_EXTRA = frozenset({"dataset_split"})
EXTERNAL_DATASET_METADATA_EXTRA = frozenset({"runtime"})
FORBIDDEN_KEYS = {
    "api_key",
    "authorization",
    "base_url",
    "cookies",
    "endpoint",
    "headers",
    "judge_base_url",
    "judge_calls",
    "model_calls",
    "prompt_fingerprints",
    "request_path",
    "request_sha256",
    "scoring_input",
    "secret",
    "status_porcelain",
    "token",
    "chain_steps",
    "version_dir",
}


class UploadBlocked(RuntimeError):
    pass


# 出站文本仅允许会话存储与绝对盘符路径的不透明替身；数据批准文本中的仓库相对引用保持不变。
LOCAL_PATH_PATTERNS = (
    re.compile(r"(?<![\w:/-])(?:tmp[\\/])?sessions[\\/][^\s\"'<>|]+"),
    re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|]+"),
)
OUTBOUND_TOKEN_CHARS = 12


def _outbound_token(match: re.Match[str]) -> str:
    normalized = match.group(0).replace("\\", "/")
    prefix = "attachment" if "/attachments/" in normalized else "local"
    return f"{prefix}:{sha256_text(normalized)[:OUTBOUND_TOKEN_CHARS]}"


def outbound_text(value: str) -> str:
    text = value
    for pattern in LOCAL_PATH_PATTERNS:
        text = pattern.sub(_outbound_token, text)
    return text


def _sanitize_outbound(value: Any) -> Any:
    if isinstance(value, str):
        return outbound_text(value)
    if isinstance(value, dict):
        return {key: _sanitize_outbound(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_sanitize_outbound(child) for child in value]
    return value


def _local_path_residue(text: str) -> str | None:
    for pattern in LOCAL_PATH_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            return match.group(0)
    return None


@dataclass
class RemoteExperiment:
    experiment_id: str
    experiment_name: str
    dataset_id: str
    dataset_name: str
    example_count: int
    run_count: int


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise UploadBlocked(message)


def _required(summary: dict[str, Any]) -> None:
    blocking = summary["release"]["blocking_issues"]
    _require(summary["release"]["effective"], "有效发布门禁未通过: " + "; ".join(blocking))
    _require(summary["formal_scope"], "仅正式全量范围允许上传")
    started = summary["run"].get("started_at")
    finished = summary["run"].get("finished_at")
    _require(bool(started) and bool(finished), "运行缺少真实起止时间，禁止上传")


def _dataset_identity(run: dict[str, Any]) -> dict[str, Any]:
    dataset = run.get("dataset")
    _require(isinstance(dataset, dict), "运行缺少数据身份")
    return {
        key: dataset.get(key)
        for key in ("version", "manifest_sha256", "content_sha256", "corpus_sha256")
    }


def _code_identity(run: dict[str, Any]) -> dict[str, Any]:
    code = run.get("identity", {}).get("code")
    _require(isinstance(code, dict), "运行缺少代码身份")
    return {
        key: code.get(key)
        for key in ("commit", "lock_sha256", "dirty", "source_sha256")
    }


def _generation_identity(run: dict[str, Any]) -> dict[str, Any]:
    generation = run.get("identity", {}).get("generation")
    _require(isinstance(generation, dict), "运行缺少生成模型身份")
    model = generation.get("model")
    _require(isinstance(model, str) and bool(model.strip()), "生成模型身份非法")
    parsed = json.loads(model)
    identity = {key: parsed.get(key) for key in ("api", "provider", "model")}
    for key, value in identity.items():
        _require(isinstance(value, str) and bool(value.strip()), f"生成模型身份缺少 {key}")
    prompt_version = generation.get("prompt_version")
    _require(
        isinstance(prompt_version, str) and bool(prompt_version.strip()),
        "运行缺少生成提示身份",
    )
    return identity | {"prompt_version": prompt_version}


def _judge_identity(run: dict[str, Any]) -> dict[str, Any]:
    judge = run.get("identity", {}).get("judge")
    _require(isinstance(judge, dict), "运行缺少裁判身份")
    return {
        key: judge.get(key)
        for key in ("model", "prompt_version", "metric_version")
    }


def _index_identity(run: dict[str, Any]) -> dict[str, Any]:
    retrieval = run.get("identity", {}).get("retrieval")
    _require(isinstance(retrieval, dict), "运行缺少检索身份")
    index = retrieval.get("index")
    _require(isinstance(index, str) and bool(index.strip()), "运行缺少物理索引身份")
    return json.loads(index)


def _context_item(item) -> dict[str, Any]:
    return {
        "source": item.source,
        "content": item.content,
        "content_sha256": sha256_text(item.content),
        "fact_origin": item.fact_origin,
        "tool_call_id": item.tool_call_id,
        "tool_name": item.tool_name,
        "truncated": item.truncated,
        "omitted_chars": item.omitted_chars,
        "original_chars": item.original_chars,
    }


def _metric_identity(evidence) -> dict[str, Any]:
    return {
        "metric_class": evidence.metric_class,
        "metric_version": evidence.metric_version,
        "prompt_digest": evidence.prompt_digest,
        "metric_parameters": evidence.metric_parameters.model_dump(mode="json"),
        "ragas_version": evidence.ragas_version,
        "dependency_versions": dict(evidence.dependency_versions),
        "judge_model": evidence.judge_model,
        "judge_timeout_seconds": evidence.judge_timeout_seconds,
        "judge_sdk_max_retries": evidence.judge_sdk_max_retries,
        "judge_temperature": evidence.judge_temperature,
        "ragas_run_config": evidence.ragas_run_config.model_dump(mode="json"),
    }


def _assertion_scores(artifact, checkpoint_id: str, event_id: str) -> list[dict[str, Any]]:
    rows = []
    for checkpoint in artifact.assertions:
        if checkpoint.checkpoint_id != checkpoint_id or checkpoint.event_id != event_id:
            continue
        rows.extend(
            {"kind": outcome.kind, "passed": outcome.passed}
            for outcome in checkpoint.outcomes
        )
    return rows


def _row_metadata(
    layout,
    scenario: dict[str, Any],
    checkpoint_id: str,
    event_id: str,
    evidence,
    summary: dict[str, Any],
) -> dict[str, Any]:
    identity = layout.run.get("identity", {})
    return {
        "run_id": layout.plan.run_id,
        "scenario_id": scenario["scenario_id"],
        "checkpoint_id": checkpoint_id,
        "event_id": event_id,
        "tags": list(scenario["tags"]),
        "family": scenario["family"],
        "scope": summary["scope"],
        "dataset": _dataset_identity(layout.run),
        "code": _code_identity(layout.run),
        "generation": _generation_identity(layout.run),
        "judge": _judge_identity(layout.run),
        "retrieval_index": _index_identity(layout.run),
        "judge_attempt_model": identity.get("judge_attempt_model"),
        "judge_same_as_generation": identity.get("judge_same_as_generation"),
        "metric_identity": {
            metric: _metric_identity(item) for metric, item in evidence.items()
        },
        "aggregation": summary["generation"]["aggregation"],
    }


def build_upload_payload(
    run_dir: Path,
    *,
    version_dir: Path | None = None,
    scope: str = "auto",
) -> tuple[dict[str, Any], dict[str, Any], Any]:
    layout = load_run(run_dir, version_dir)
    summary = build_summary(run_dir, version_dir=version_dir, scope=scope)
    _required(summary)
    dataset_identity = _dataset_identity(layout.run)
    dataset_name = (
        f"fit-agent-generation-{dataset_identity['version']}-"
        f"{dataset_identity['content_sha256'][:12]}"
    )
    experiment_name = f"{summary['run_id']}-generation"
    dataset_scenarios = layout.dataset_scenarios
    planned = {item.scenario_id for item in layout.plan.scenarios}
    rows = []
    for scenario_id in layout.dataset_scenarios:
        if scenario_id not in planned:
            continue
        aggregate = layout.aggregates[scenario_id]
        artifact = aggregate.artifact
        if artifact is None:
            continue
        scenario = dataset_scenarios[scenario_id]
        for generation in artifact.generation:
            checkpoint = generation.checkpoint
            if checkpoint is None or generation.result is None:
                continue
            checkpoint_id = generation.checkpoint_id
            link = checkpoint.checkpoint
            evidence = {
                item.metric: item
                for item in generation.evidence
                if item.checkpoint == link
            }
            scores = []
            statuses = []
            for outcome in generation.result.outcomes:
                status = {
                    "key": outcome.metric,
                    "status": outcome.status,
                }
                if outcome.reason is not None:
                    status["reason"] = outcome.reason
                statuses.append(status)
                if outcome.score is not None:
                    scores.append({
                        "key": outcome.metric,
                        "score": round(outcome.score, 4),
                    })
            applicability = [
                {
                    "metric": item["metric"],
                    "applicable": item["applicable"],
                    "reason": item["reason"],
                }
                for item in scenario["metric_applicability"]
                if item["checkpoint_id"] == checkpoint_id
                and item["metric"] in GENERATION_METRICS
            ]
            rows.append({
                "row_id": str(
                    uuid5(
                        NAMESPACE_URL,
                        f"{dataset_name}:{scenario['scenario_id']}:{checkpoint_id}",
                    )
                ),
                "inputs": {
                    "scenario_id": scenario["scenario_id"],
                    "checkpoint_id": checkpoint_id,
                    "event_id": generation.event_id,
                    "user_input": checkpoint.user_input.model_dump(mode="json")
                    | {
                        "message_sha256": sha256_text(
                            checkpoint.user_input.message
                        )
                    },
                    "model_input": {
                        "items": [
                            _context_item(item)
                            for item in checkpoint.model_input.items
                        ],
                        "truncated": checkpoint.model_input.truncated,
                        "omitted_chars": checkpoint.model_input.omitted_chars,
                    },
                },
                "expected_outputs": {
                    "reference": (
                        None
                        if checkpoint.reference is None
                        else checkpoint.reference.model_dump(mode="json")
                    ),
                    "applicability": applicability,
                    "assertions": _assertion_scores(
                        artifact, checkpoint_id, generation.event_id
                    ),
                },
                "actual_outputs": {
                    "answer": {
                        "visible_text": checkpoint.answer.visible_text,
                        "structured_snapshot": checkpoint.answer.structured_snapshot,
                    },
                    "metric_statuses": statuses,
                },
                "evaluation_scores": scores,
                "start_time": summary["run"]["started_at"],
                "end_time": summary["run"]["finished_at"],
                "run_name": f"{scenario['scenario_id']}:{checkpoint_id}",
                "run_metadata": _row_metadata(
                    layout,
                    scenario,
                    checkpoint_id,
                    generation.event_id,
                    evidence,
                    summary,
                ),
            })
    _require(bool(rows), "没有可上传的生成检查点")
    experiment_scores = []
    for metric in GENERATION_METRICS:
        value = summary["generation"]["metrics"][metric]["mean_complete_only"]
        _require(
            value is not None,
            f"{metric} 缺少仅完整成功口径的汇总分数",
        )
        experiment_scores.append({"key": metric, "score": round(value, 4)})
    metric_identity = {}
    for aggregate in layout.aggregates.values():
        if aggregate.artifact is None:
            continue
        for row in aggregate.artifact.generation:
            for item in row.evidence:
                metric_identity.setdefault(item.metric, _metric_identity(item))
    payload = {
        "dataset_name": dataset_name,
        "experiment_name": experiment_name,
        "experiment_start_time": summary["run"]["started_at"],
        "experiment_end_time": summary["run"]["finished_at"],
        "experiment_metadata": {
            "upload_schema": PAYLOAD_SCHEMA,
            "source_sha256": source_digest(Path(run_dir)),
            "run_id": summary["run_id"],
            "scope": summary["scope"],
            "dataset": dataset_identity,
            "code": _code_identity(layout.run),
            "generation": _generation_identity(layout.run),
            "judge": _judge_identity(layout.run),
            "retrieval_index": _index_identity(layout.run),
            "judge_attempt_model": layout.run.get("identity", {}).get(
                "judge_attempt_model"
            ),
            "judge_same_as_generation": layout.run.get("identity", {}).get(
                "judge_same_as_generation"
            ),
            "aggregation": summary["generation"]["aggregation"],
            "coverage": summary["coverage"],
            "metric_identity": metric_identity,
        },
        "summary_experiment_scores": experiment_scores,
        "results": rows,
    }
    payload = _sanitize_outbound(payload)
    payload["experiment_metadata"]["rows_sha256"] = sha256_text(
        _canonical(payload["results"])
    )
    assert_upload_whitelist(payload)
    return payload, summary, layout


def _walk_keys(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _walk_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_strings(child)


def assert_upload_whitelist(payload: dict[str, Any]) -> None:
    forbidden = sorted(set(_walk_keys(payload)) & FORBIDDEN_KEYS)
    _require(not forbidden, f"上传 payload 包含禁止字段: {','.join(forbidden)}")
    text = _canonical(payload)
    residue = _local_path_residue(text)
    _require(residue is None, f"上传 payload 包含本地路径: {residue}")
    secret = os.environ.get("LANGSMITH_API_KEY")
    if secret:
        _require(secret not in text, "上传 payload 包含 LangSmith 凭据")
    for marker in ("Bearer ", "Authorization:", "LANGSMITH_API_KEY"):
        _require(marker not in text, f"上传 payload 包含凭据标记: {marker}")


def source_digest(run_dir: Path) -> str:
    entries = []
    for name in ("run.json", "plan.json"):
        path = run_dir / name
        _require(path.is_file(), f"运行目录缺少 {name}")
        entries.append({"path": name, "sha256": _sha256_file(path)})
    plan = _read_json(run_dir / "plan.json")
    for scenario in plan["scenarios"]:
        scenario_id = scenario["scenario_id"]
        for name in ARTIFACT_FILES:
            path = run_dir / scenario_id / name
            if path.is_file():
                entries.append({
                    "path": f"{scenario_id}/{name}",
                    "sha256": _sha256_file(path),
                })
    return sha256_text(_canonical(entries))


def payload_digest(payload: dict[str, Any]) -> str:
    return sha256_text(_canonical(payload))


def _rounded_scores(scores: list[dict[str, Any]]) -> list[list[Any]]:
    return sorted(
        [str(item["key"]), round(float(item["score"]), 6)] for item in scores
    )


def payload_expectation(payload: dict[str, Any]) -> dict[str, Any]:
    # 本地 payload 即远端回读的唯一对照基准：实验身份、汇总分数与逐行摘要。
    metadata = payload["experiment_metadata"]
    return {
        "rows": len(payload["results"]),
        "source_sha256": metadata["source_sha256"],
        "rows_sha256": metadata["rows_sha256"],
        "experiment_metadata": metadata,
        "summary_scores": _rounded_scores(payload["summary_experiment_scores"]),
        "examples": {
            row["row_id"]: {
                "inputs": sha256_text(_canonical(row["inputs"])),
                "expected_outputs": sha256_text(_canonical(row["expected_outputs"])),
                "actual_outputs": sha256_text(_canonical(row["actual_outputs"])),
                "scores": sha256_text(_canonical(_rounded_scores(row["evaluation_scores"]))),
                "run_metadata": sha256_text(_canonical(row["run_metadata"])),
            }
            for row in payload["results"]
        },
    }


def remote_snapshot(
    examples,
    runs,
    feedback,
    *,
    experiment_metadata: dict[str, Any],
    summary_feedback: Sequence[Any] = (),
) -> dict[str, Any]:
    snapshot = {
        str(example.id): {
            "inputs": sha256_text(_canonical(example.inputs)),
            "expected_outputs": sha256_text(_canonical(example.outputs)),
        }
        for example in examples
    }
    scores: dict[str, list[list[Any]]] = {}
    for item in feedback:
        scores.setdefault(str(item.run_id), []).append(
            [str(item.key), round(float(item.score), 6)]
        )
    for run in runs:
        if run.reference_example_id is None:
            raise UploadBlocked(f"远端运行缺少样例绑定: {run.id}")
        row = snapshot.setdefault(str(run.reference_example_id), {})
        if "actual_outputs" in row:
            raise UploadBlocked(
                f"远端样例绑定了多个根运行: {run.reference_example_id}"
            )
        row["actual_outputs"] = sha256_text(_canonical(run.outputs))
        row["scores"] = sha256_text(_canonical(sorted(scores.get(str(run.id), []))))
        row["run_metadata"] = sha256_text(_canonical(run.metadata))
    return {
        "experiment_metadata": experiment_metadata,
        "summary_scores": sorted(
            [str(item.key), round(float(item.score), 6)]
            for item in summary_feedback
        ),
        "examples": snapshot,
    }


def _metadata_mismatches(
    expected: dict[str, Any], actual: Any
) -> list[str]:
    if not isinstance(actual, dict):
        return ["<missing>"]
    return sorted(
        key
        for key in set(expected) | set(actual)
        if expected.get(key) != actual.get(key)
    )


def require_remote_matches(
    expectation: dict[str, Any], snapshot: dict[str, Any]
) -> None:
    remote_metadata = snapshot.get("experiment_metadata")
    for key in ("source_sha256", "rows_sha256"):
        if not isinstance(remote_metadata, dict) or remote_metadata.get(key) != expectation[key]:
            raise UploadBlocked(f"远端实验 {key} 与本地 payload 不一致")
    mismatched_metadata = _metadata_mismatches(
        expectation["experiment_metadata"], remote_metadata
    )
    if mismatched_metadata:
        raise UploadBlocked(
            "远端实验元数据与本地 payload 不一致: "
            + ", ".join(mismatched_metadata[:3])
        )
    if snapshot.get("summary_scores") != expectation["summary_scores"]:
        raise UploadBlocked("远端实验汇总分数与本地 payload 不一致")
    expected_rows = expectation["examples"]
    actual_rows = snapshot.get("examples")
    if not isinstance(actual_rows, dict):
        raise UploadBlocked("远端回读缺少样例")
    missing = sorted(set(expected_rows) - set(actual_rows))
    extra = sorted(set(actual_rows) - set(expected_rows))
    mismatched = sorted(
        f"{row_id}/{field}"
        for row_id, fields in expected_rows.items()
        for field, digest in fields.items()
        if row_id in actual_rows and actual_rows[row_id].get(field) != digest
    )
    if missing or extra or mismatched:
        raise UploadBlocked(
            "远端回读与本地 payload 不一致: "
            f"缺少 {missing[:3]} 多余 {extra[:3]} 字段 {mismatched[:3]}"
        )


def _remote_snapshot(
    client: Client,
    dataset_id: str,
    experiment_id: str,
    experiment_metadata: dict[str, Any],
) -> dict[str, Any]:
    runs = list(client.list_runs(project_id=experiment_id, is_root=True))
    run_ids = [str(run.id) for run in runs]
    feedback = []
    for start in range(0, len(run_ids), FEEDBACK_BATCH):
        feedback.extend(
            client.list_feedback(run_ids=run_ids[start : start + FEEDBACK_BATCH])
        )
    summary_feedback = list(
        client.list_feedback(session=experiment_id, level="session")
    )
    return remote_snapshot(
        list(client.list_examples(dataset_id=dataset_id)),
        runs,
        feedback,
        experiment_metadata=experiment_metadata,
        summary_feedback=summary_feedback,
    )


def receipt_matches(
    receipt: dict[str, Any], source_sha256: str, payload_sha256: str
) -> bool:
    return (
        receipt.get("source_sha256") == source_sha256
        and receipt.get("payload_sha256") == payload_sha256
    )


def pending_matches(
    pending: dict[str, Any], source_sha256: str, payload_sha256: str
) -> bool:
    return (
        pending.get("source_sha256") == source_sha256
        and pending.get("payload_sha256") == payload_sha256
    )


def resolve_pending(
    pending: dict[str, Any],
    remote: RemoteExperiment | None,
    *,
    rows: int,
) -> str:
    if remote is None:
        return "resubmit"
    if (
        remote.experiment_name != pending.get("experiment_name")
        or remote.dataset_name != pending.get("dataset_name")
        or remote.example_count != rows
        or remote.run_count != rows
    ):
        return "conflict"
    return "recovered"


def _remote_from_project(
    client: Client, project, dataset_name: str, rows: int
) -> RemoteExperiment:
    dataset_id = str(project.reference_dataset_id)
    dataset = client.read_dataset(dataset_id=dataset_id)
    _require(dataset.name == dataset_name, "远端数据集名称与 payload 不一致")
    _require(
        dataset.example_count == rows,
        f"远端样例数与 payload 不一致: {dataset.example_count} != {rows}",
    )
    _require(
        project.run_count == rows,
        f"远端实验运行数与 payload 不一致: {project.run_count} != {rows}",
    )
    return RemoteExperiment(
        experiment_id=str(project.id),
        experiment_name=project.name,
        dataset_id=dataset_id,
        dataset_name=dataset.name,
        example_count=dataset.example_count,
        run_count=project.run_count,
    )


def verify_remote(
    client: Client,
    experiment_id: str,
    dataset_name: str,
    expectation: dict[str, Any],
) -> RemoteExperiment:
    project = client.read_project(project_id=experiment_id, include_stats=True)
    remote = _remote_from_project(client, project, dataset_name, expectation["rows"])
    require_remote_matches(
        expectation,
        _remote_snapshot(
            client,
            remote.dataset_id,
            remote.experiment_id,
            project.metadata,
        ),
    )
    return remote


def find_remote(
    client: Client,
    experiment_name: str,
    dataset_name: str,
    expectation: dict[str, Any],
) -> RemoteExperiment | None:
    try:
        project = client.read_project(
            project_name=experiment_name, include_stats=True
        )
    except LangSmithNotFoundError:
        return None
    if project is None:
        return None
    remote = _remote_from_project(client, project, dataset_name, expectation["rows"])
    require_remote_matches(
        expectation,
        _remote_snapshot(
            client,
            remote.dataset_id,
            remote.experiment_id,
            project.metadata,
        ),
    )
    return remote


def _receipt(
    remote: RemoteExperiment,
    source_sha256: str,
    payload_sha256: str,
    rows_sha256: str,
) -> dict[str, Any]:
    return {
        "schema": RECEIPT_SCHEMA,
        "source_sha256": source_sha256,
        "payload_sha256": payload_sha256,
        "rows_sha256": rows_sha256,
        "dataset_name": remote.dataset_name,
        "dataset_id": remote.dataset_id,
        "experiment_name": remote.experiment_name,
        "experiment_id": remote.experiment_id,
        "rows": remote.example_count,
        "remote_run_count": remote.run_count,
        "uploaded_at": datetime.now(UTC).isoformat(),
    }


def check_payload(
    run_dir: Path,
    *,
    version_dir: Path | None = None,
    scope: str = "auto",
) -> dict[str, Any]:
    payload, summary, _ = build_upload_payload(
        run_dir, version_dir=version_dir, scope=scope
    )
    source_sha256 = source_digest(Path(run_dir))
    payload_sha256 = payload_digest(payload)
    path = Path(run_dir) / PAYLOAD_NAME
    _write_json(path, payload)
    return {
        "status": "offline_payload_checked",
        "network": "not_called",
        "source_sha256": source_sha256,
        "payload_sha256": payload_sha256,
        "rows_sha256": payload["experiment_metadata"]["rows_sha256"],
        "rows": len(payload["results"]),
        "dataset_name": payload["dataset_name"],
        "experiment_name": payload["experiment_name"],
        "effective_release": summary["release"]["effective"],
        "payload_path": str(path),
    }


def upload_payload(
    run_dir: Path,
    *,
    version_dir: Path | None = None,
    scope: str = "auto",
    client: Client | None = None,
) -> dict[str, Any]:
    _require(
        bool(os.environ.get("LANGSMITH_API_KEY")),
        "请在 backend/.env 配置 LANGSMITH_API_KEY",
    )
    payload, summary, _ = build_upload_payload(
        run_dir, version_dir=version_dir, scope=scope
    )
    run_dir = Path(run_dir)
    source_sha256 = source_digest(run_dir)
    payload_sha256 = payload_digest(payload)
    expectation = payload_expectation(payload)
    rows = expectation["rows"]
    receipt_path = run_dir / RECEIPT_NAME
    pending_path = run_dir / PENDING_NAME
    client = client or Client(auto_batch_tracing=False)
    if receipt_path.exists():
        receipt = _read_json(receipt_path)
        _require(
            receipt_matches(receipt, source_sha256, payload_sha256),
            "上传回执与当前源文件或 payload 不一致",
        )
        remote = verify_remote(
            client,
            receipt["experiment_id"],
            payload["dataset_name"],
            expectation,
        )
        _require(
            receipt.get("experiment_name") == remote.experiment_name
            and receipt.get("dataset_id") == remote.dataset_id,
            "远端实验或数据集与上传回执不一致",
        )
        return {
            "status": "verified_existing",
            "experiment_id": remote.experiment_id,
            "dataset_id": remote.dataset_id,
            "rows": rows,
            "source_sha256": source_sha256,
            "payload_sha256": payload_sha256,
            "rows_sha256": expectation["rows_sha256"],
        }
    if pending_path.exists():
        pending = _read_json(pending_path)
        _require(
            pending_matches(pending, source_sha256, payload_sha256),
            "提交中断标记与当前源文件或 payload 不一致",
        )
        remote = find_remote(
            client, payload["experiment_name"], payload["dataset_name"], expectation
        )
        decision = resolve_pending(pending, remote, rows=rows)
        if decision == "conflict":
            raise UploadBlocked("远端已提交的实验与当前 payload 不一致")
        if decision == "recovered":
            receipt = _receipt(
                remote,
                source_sha256,
                payload_sha256,
                expectation["rows_sha256"],
            )
            _write_json(receipt_path, receipt)
            pending_path.unlink()
            return {
                "status": "recovered_interrupted",
                "experiment_id": remote.experiment_id,
                "dataset_id": remote.dataset_id,
                "rows": rows,
                "source_sha256": source_sha256,
                "payload_sha256": payload_sha256,
                "rows_sha256": expectation["rows_sha256"],
            }
    _write_json(pending_path, {
        "schema": PENDING_SCHEMA,
        "source_sha256": source_sha256,
        "payload_sha256": payload_sha256,
        "rows_sha256": expectation["rows_sha256"],
        "dataset_name": payload["dataset_name"],
        "experiment_name": payload["experiment_name"],
        "rows": rows,
        "started_at": datetime.now(UTC).isoformat(),
    })
    response = client.request_with_retries(
        "POST",
        "/datasets/upload-experiment",
        request_kwargs={"json": payload},
        stop_after_attempt=1,
    )
    response.raise_for_status()
    body = response.json()
    _require(
        bool(body.get("experiment", {}).get("id"))
        and bool(body.get("dataset", {}).get("id")),
        "LangSmith 未返回实验和数据集 ID",
    )
    remote = verify_remote(
        client,
        body["experiment"]["id"],
        payload["dataset_name"],
        expectation,
    )
    receipt = _receipt(
        remote, source_sha256, payload_sha256, expectation["rows_sha256"]
    )
    _write_json(receipt_path, receipt)
    pending_path.unlink()
    return {
        "status": "uploaded",
        "experiment_id": remote.experiment_id,
        "dataset_id": remote.dataset_id,
        "rows": rows,
        "source_sha256": source_sha256,
        "payload_sha256": payload_sha256,
        "rows_sha256": expectation["rows_sha256"],
        "effective_release": summary["release"]["effective"],
    }


# ---- Langfuse 外部裁判：检查点投影、数据集同步与实验提交 ----
# 外部分支只读取已封存的执行证据；不调用旧 load_run/build_summary，也不要求 Ragas 评分。
# 旧 ragas check/upload 链路（LangSmith REST 上传与 langsmith-generation.json 回执）原样保留。


class ExternalApplicabilityEntry(EvaluationContract):
    execution_sha256: Sha256
    checkpoint: EvidenceLink
    applicability: list[ExternalJudgeApplicability] = Field(
        min_length=2, max_length=2
    )

    @model_validator(mode="after")
    def validate_metrics(self) -> "ExternalApplicabilityEntry":
        metrics = [row.metric for row in self.applicability]
        if len(set(metrics)) != len(metrics) or set(metrics) != set(
            EXTERNAL_JUDGE_METRICS
        ):
            raise ValueError("外部裁判适用性必须逐项且仅包含两个指标")
        return self


class ExternalApplicabilityApproval(EvaluationContract):
    status: Literal["approved"]
    reviewer: NonEmptyText
    reviewer_type: Literal["user"]
    reviewed_at: NonEmptyText
    comment: NonEmptyText | None = None
    content_sha256: Sha256
    decision_ref: NonEmptyText


class ExternalApplicabilityInput(EvaluationContract):
    schema_version: Literal["fit-agent-external-judge-applicability-1"]
    run_id: NonEmptyText
    dataset: DatasetIdentity
    entries: list[ExternalApplicabilityEntry] = Field(min_length=1)
    approval: ExternalApplicabilityApproval

    @model_validator(mode="after")
    def validate_entries(self) -> "ExternalApplicabilityInput":
        keys = [
            (
                entry.checkpoint.scenario_id,
                entry.checkpoint.event_id,
                entry.checkpoint.checkpoint_id,
            )
            for entry in self.entries
        ]
        if len(set(keys)) != len(keys):
            raise ValueError("外部裁判适用性检查点不能重复")
        return self

    def content_material(self) -> dict[str, Any]:
        entries = sorted(
            self.entries,
            key=lambda entry: (
                entry.checkpoint.scenario_id,
                entry.checkpoint.event_id,
                entry.checkpoint.checkpoint_id,
            ),
        )
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "dataset": self.dataset.model_dump(mode="json"),
            "entries": [entry.model_dump(mode="json") for entry in entries],
        }

    def content_sha256(self) -> str:
        return sha256_text(_canonical(self.content_material()))


def _checkpoint_key(link: EvidenceLink) -> tuple[str, str, str]:
    return (link.scenario_id, link.event_id, link.checkpoint_id)


@dataclass(frozen=True)
class ExternalScenarioEvidence:
    scenario: dict[str, Any]
    example_id: str
    execution: ScenarioExecution
    record: ExternalScenarioScoring
    sample: dict[str, Any]
    root: Path


@dataclass(frozen=True)
class ExternalCheckpointEvidence:
    scenario: dict[str, Any]
    execution: ScenarioExecution
    record: ExternalScenarioScoring
    entry: GenerationCheckpointEvidence | None
    link: EvidenceLink
    readiness: ExternalScoringReadiness
    retrieval: tuple[SearchStageRecord, ...]

    @property
    def execution_sha256(self) -> str:
        return self.execution.execution_sha256

    @property
    def key(self) -> tuple[str, str, str]:
        return _checkpoint_key(self.link)


@dataclass(frozen=True)
class ExternalRunEvidence:
    run_dir: Path
    document: dict[str, Any]
    dataset: EvaluationDataset
    dataset_identity: DatasetIdentity
    run_id: str
    source_sha256: str
    scenarios: tuple[ExternalScenarioEvidence, ...]
    checkpoints: tuple[ExternalCheckpointEvidence, ...]


def external_dir(run_dir: Path) -> Path:
    return Path(run_dir) / EXTERNAL_DIR


def applicability_path(run_dir: Path) -> Path:
    return external_dir(run_dir) / APPLICABILITY_NAME


def applicability_review_path(run_dir: Path) -> Path:
    return external_dir(run_dir) / APPLICABILITY_REVIEW_NAME


def _evaluation_dataset_identity(
    dataset: EvaluationDataset,
) -> DatasetIdentity:
    identity = dataset.identity()
    return DatasetIdentity(
        version=identity["version"],
        manifest_sha256=identity["manifest_sha256"],
        content_sha256=identity["content_sha256"],
        corpus_sha256=identity["corpus_sha256"],
    )


def _document_dataset_identity(document: dict[str, Any]) -> DatasetIdentity:
    dataset = document.get("dataset")
    _require(isinstance(dataset, dict), "源运行缺少数据身份")
    try:
        return DatasetIdentity.model_validate(
            {
                key: dataset.get(key)
                for key in (
                    "version",
                    "manifest_sha256",
                    "content_sha256",
                    "corpus_sha256",
                )
            }
        )
    except ValidationError as error:
        raise UploadBlocked("源运行数据身份无效") from error


def _scenario_event_ids(scenario: dict[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for key in ("reference", "rubric", "assertions", "metric_applicability"):
        for row in scenario[key]:
            checkpoint_id = row["checkpoint_id"]
            event_id = row["event_id"]
            if mapping.setdefault(checkpoint_id, event_id) != event_id:
                raise UploadBlocked(
                    f"冻结场景检查点绑定了多个事件: {checkpoint_id}"
                )
    return mapping


def _external_scenario_evidence(
    run_dir: Path,
    run_id: str,
    dataset: EvaluationDataset,
    dataset_identity: DatasetIdentity,
    scenario: dict[str, Any],
    example_id: str,
    sample: dict[str, Any],
) -> ExternalScenarioEvidence:
    scenario_id = scenario["scenario_id"]
    expected_root = run_dir / "scenarios" / example_id / scenario_id
    _require(
        expected_root.resolve().is_relative_to(run_dir.resolve()),
        f"场景证据路径越界: {scenario_id}",
    )
    execution_path = expected_root / EXECUTION_FILENAME
    _require(execution_path.is_file(), f"场景执行证据缺失: {scenario_id}")
    try:
        execution = load_execution(
            execution_path,
            expected_run_id=run_id,
            expected_scenario_id=scenario_id,
            expected_dataset=dataset_identity,
            expected_scenario_sha256=frozen_scenario_sha256(scenario),
        )
    except ExecutionIdentityError as error:
        raise UploadBlocked(
            f"场景执行身份校验失败（{scenario_id}）: {error}"
        ) from error
    plan = scenario_plan(scenario)
    _require(
        execution.generation_metrics == plan.generation_metrics,
        f"执行生成指标与冻结场景不一致: {scenario_id}",
    )
    scoring_path = expected_root / EXTERNAL_SCORING_FILENAME
    _require(scoring_path.is_file(), f"外部评分证据缺失: {scenario_id}")
    try:
        record = load_external_scoring(
            scoring_path, expected_execution_sha256=execution.execution_sha256
        )
    except (ExecutionIdentityError, ValueError) as error:
        raise UploadBlocked(
            f"外部评分身份校验失败（{scenario_id}）: {error}"
        ) from error
    _require(
        record.run_id == run_id and record.scenario_id == scenario_id,
        f"外部评分证据身份不一致: {scenario_id}",
    )
    try:
        recomputed = score_execution_external(execution, scenario["assertions"])
    except Exception as error:
        raise UploadBlocked(
            f"外部评分证据无法按生产逻辑复核（{scenario_id}）: "
            f"{type(error).__name__}: {error}"
        ) from error
    _require(
        record.model_dump(mode="json") == recomputed.model_dump(mode="json"),
        f"外部评分证据与执行结果不一致: {scenario_id}",
    )
    sample_scoring = sample.get("scoring_relative")
    expected_relative = (
        f"scenarios/{example_id}/{scenario_id}/{EXTERNAL_SCORING_FILENAME}"
    )
    _require(
        sample_scoring == expected_relative,
        f"运行样本评分路径与批准目录不一致: {scenario_id}",
    )
    _require(
        sample.get("scoring_sha256")
        == sha256_text(scoring_path.read_text(encoding="utf-8")),
        f"运行样本评分摘要与外部评分证据不一致: {scenario_id}",
    )
    _require(
        sample.get("execution_sha256") == execution.execution_sha256,
        f"运行样本执行摘要与执行证据不一致: {scenario_id}",
    )
    _require(
        sample.get("execution_status") == execution.status,
        f"运行样本执行状态与执行证据不一致: {scenario_id}",
    )
    try:
        sample_version = feedback_version(sample)
    except ScenarioSDKError as error:
        raise UploadBlocked(str(error)) from error
    _require(
        sample.get("scoring_mode") == "external"
        and sample_version == EXTERNAL_FEEDBACK_VERSION,
        f"运行样本不是外部评分协议: {scenario_id}",
    )
    return ExternalScenarioEvidence(
        scenario=scenario,
        example_id=example_id,
        execution=execution,
        record=record,
        sample=sample,
        root=expected_root,
    )


def _checkpoint_retrieval_records(
    execution: ScenarioExecution,
    event_id: str,
    boundary: RequestBoundaryEvidence | None,
    event_positions: dict[str, int],
) -> tuple[SearchStageRecord, ...]:
    if boundary is None:
        return tuple(
            record
            for record in execution.retrieval
            if record.event_id == event_id
        )
    request_position = event_positions.get(event_id)
    _require(
        request_position is not None,
        f"生成检查点事件不在场景中: {event_id}",
    )
    visible_calls = {node.tool_call_id for node in boundary.tool_nodes}
    return tuple(
        record
        for record in execution.retrieval
        if record.tool_call_id in visible_calls
        and event_positions.get(record.event_id, request_position + 1)
        <= request_position
    )


def load_external_run(
    source: Path, dataset: EvaluationDataset
) -> ExternalRunEvidence:
    # 外部入口只接受显式 external 运行摘要与逐场景封存证据，旧 Ragas 运行不进入此分支。
    run_dir = Path(source)
    _require(run_dir.is_dir(), f"源运行目录不存在: {run_dir}")
    sdk_path = run_dir / SDK_RUN_NAME
    _require(sdk_path.is_file(), f"源运行目录缺少 {SDK_RUN_NAME}")
    document = _read_json(sdk_path)
    _require(isinstance(document, dict), "运行摘要必须为对象")
    _require(
        document.get("schema") == SCENARIO_SDK_SCHEMA,
        "源运行摘要版本不受支持",
    )
    try:
        scoring_mode = run_scoring_mode(document)
        version = feedback_version(document)
    except ScenarioSDKError as error:
        raise UploadBlocked(str(error)) from error
    _require(scoring_mode == "external", "源运行不是 external 评分模式")
    _require(
        version == EXTERNAL_FEEDBACK_VERSION,
        "源运行不是外部反馈协议版本",
    )
    _require(
        document.get("protocol_version") == EXTERNAL_PROTOCOL_VERSION,
        "源运行外部协议版本不一致",
    )
    _require(
        document.get("status") != "running",
        "源运行仍在进行，拒绝读取未终结证据",
    )
    run_id = document.get("run_id")
    _require(
        isinstance(run_id, str) and bool(run_id), "运行摘要缺少 run_id"
    )
    _require(run_dir.name == run_id, "源运行目录名称与 run_id 不一致")
    dataset_identity = _document_dataset_identity(document)
    _require(
        dataset_identity == _evaluation_dataset_identity(dataset),
        "源运行数据身份与当前数据集不一致",
    )
    samples = document.get("samples")
    _require(isinstance(samples, list) and bool(samples), "运行摘要缺少样本")
    scope = document.get("sample_scope") or {}
    scope_ids = scope.get("scenario_ids")
    _require(
        isinstance(scope_ids, list) and bool(scope_ids),
        "运行摘要缺少场景范围",
    )
    _require(
        len(set(scope_ids)) == len(scope_ids), "运行摘要场景范围重复"
    )
    example_ids = document.get("example_ids")
    _require(isinstance(example_ids, dict), "运行摘要缺少样例身份")
    scenarios_by_id = {
        scenario["scenario_id"]: scenario for scenario in dataset.scenarios
    }
    _require(
        set(scope_ids) <= {str(key) for key in example_ids},
        "运行摘要缺少场景范围内的样例身份",
    )
    evidence_rows: list[ExternalScenarioEvidence] = []
    seen_scenarios: set[str] = set()
    for sample in samples:
        _require(isinstance(sample, dict), "运行样本必须为对象")
        scenario_id = sample.get("scenario_id")
        example_id = sample.get("example_id")
        _require(
            isinstance(scenario_id, str)
            and isinstance(example_id, str)
            and scenario_id in scenarios_by_id,
            "运行样本引用未知场景",
        )
        _require(
            scenario_id not in seen_scenarios, "运行样本场景重复"
        )
        seen_scenarios.add(scenario_id)
        _require(
            example_ids.get(scenario_id) == example_id,
            f"运行样本样例身份不一致: {scenario_id}",
        )
        evidence_rows.append(
            _external_scenario_evidence(
                run_dir,
                run_id,
                dataset,
                dataset_identity,
                scenarios_by_id[scenario_id],
                example_id,
                sample,
            )
        )
    _require(
        seen_scenarios == set(scope_ids),
        "运行样本未覆盖声明场景，拒绝缩小评分分母",
    )
    scenarios = tuple(
        sorted(evidence_rows, key=lambda row: row.scenario["scenario_id"])
    )
    checkpoints: list[ExternalCheckpointEvidence] = []
    for scenario_row in scenarios:
        scenario = scenario_row.scenario
        scenario_id = scenario["scenario_id"]
        execution = scenario_row.execution
        record = scenario_row.record
        entries = {
            entry.checkpoint_id: entry
            for entry in execution.generation_checkpoints
        }
        readiness = {
            item.checkpoint_id: item for item in record.generation
        }
        planned = set(execution.generation_metrics)
        _require(
            set(entries) <= planned,
            f"执行存在计划外生成检查点: {scenario_id}",
        )
        _require(
            set(readiness) == planned,
            f"外部评分就绪记录与执行检查点不一致: {scenario_id}",
        )
        event_ids = _scenario_event_ids(scenario)
        event_positions = {
            event["event_id"]: index
            for index, event in enumerate(scenario["events"])
        }
        for checkpoint_id in sorted(planned):
            entry = entries.get(checkpoint_id)
            ready = readiness[checkpoint_id]
            if entry is not None:
                event_id = entry.event_id
                link = EvidenceLink(
                    run_id=run_id,
                    scenario_id=scenario_id,
                    event_id=event_id,
                    checkpoint_id=checkpoint_id,
                )
                if entry.checkpoint is not None:
                    _require(
                        entry.checkpoint.checkpoint == link,
                        f"生成检查点身份与执行证据不一致: {scenario_id}/{checkpoint_id}",
                    )
            else:
                event_id = ready.event_id or event_ids.get(checkpoint_id)
                _require(
                    event_id is not None,
                    f"缺失生成检查点没有事件身份: {scenario_id}/{checkpoint_id}",
                )
                link = EvidenceLink(
                    run_id=run_id,
                    scenario_id=scenario_id,
                    event_id=event_id,
                    checkpoint_id=checkpoint_id,
                )
            retrieval = _checkpoint_retrieval_records(
                execution,
                event_id,
                None if entry is None else entry.request_boundary,
                event_positions,
            )
            checkpoints.append(
                ExternalCheckpointEvidence(
                    scenario=scenario,
                    execution=execution,
                    record=scenario_row.record,
                    entry=entry,
                    link=link,
                    readiness=ready,
                    retrieval=retrieval,
                )
            )
    checkpoints.sort(
        key=lambda item: (
            item.link.scenario_id,
            item.link.event_id,
            item.link.checkpoint_id,
        )
    )
    digest_entries: list[dict[str, str]] = [
        {
            "path": SDK_RUN_NAME,
            "sha256": _sha256_file(sdk_path),
        }
    ]
    for scenario_row in scenarios:
        scenario_id = scenario_row.scenario["scenario_id"]
        for name in (EXECUTION_FILENAME, EXTERNAL_SCORING_FILENAME):
            path = scenario_row.root / name
            digest_entries.append(
                {
                    "path": (
                        f"scenarios/{scenario_row.example_id}/"
                        f"{scenario_id}/{name}"
                    ),
                    "sha256": _sha256_file(path),
                }
            )
    return ExternalRunEvidence(
        run_dir=run_dir,
        document=document,
        dataset=dataset,
        dataset_identity=dataset_identity,
        run_id=run_id,
        source_sha256=sha256_text(_canonical(digest_entries)),
        scenarios=scenarios,
        checkpoints=tuple(checkpoints),
    )


def _planned_reference(
    scenario: dict[str, Any], checkpoint_id: str
) -> GenerationReference | None:
    try:
        return generation_reference(scenario, checkpoint_id)
    except StopIteration:
        return None


def _previous_applicability(
    scenario: dict[str, Any], checkpoint_id: str
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


def _evidence_state(
    checkpoint: ExternalCheckpointEvidence,
    approved: ExternalApplicabilityEntry | None,
) -> tuple[str, list[str]]:
    entry = checkpoint.entry
    if entry is None:
        return "missing", ["generation_checkpoint"]
    if entry.runner_error is not None:
        return "missing", ["generation_runner_error"]
    if entry.checkpoint is None:
        return "no_generation_evidence", ["generation_checkpoint"]
    missing = []
    if entry.request_boundary is None:
        missing.append("request_boundary")
    if not entry.checkpoint.answer.visible_text.strip():
        missing.append("answer")
    if (
        approved is not None
        and any(row.applicable for row in approved.applicability)
        and entry.checkpoint.reference is None
    ):
        missing.append("reference")
    return ("ready" if not missing else "missing"), missing


def _classification(
    checkpoint: ExternalCheckpointEvidence,
    approved: ExternalApplicabilityEntry | None,
) -> str:
    if approved is not None and not any(
        row.applicable for row in approved.applicability
    ):
        return "not_applicable"
    state, _ = _evidence_state(checkpoint, approved)
    if state != "ready":
        return "missing"
    if approved is None:
        return "pending_review"
    return "uploadable"


def _coverage_ledger(
    evidence: ExternalRunEvidence,
    applicability: dict[
        tuple[str, str, str], ExternalApplicabilityEntry
    ]
    | None,
) -> dict[str, Any]:
    entries = []
    counts = {
        "expected": len(evidence.checkpoints),
        "uploadable": 0,
        "missing": 0,
        "not_applicable": 0,
        "pending_review": 0,
    }
    for checkpoint in evidence.checkpoints:
        approved = (
            None if applicability is None else applicability.get(checkpoint.key)
        )
        state, missing = _evidence_state(checkpoint, approved)
        classification = _classification(checkpoint, approved)
        counts[classification] += 1
        entries.append(
            {
                "checkpoint": checkpoint.link.model_dump(mode="json"),
                "execution_sha256": checkpoint.execution_sha256,
                "classification": classification,
                "evidence_status": state,
                "evidence_missing": missing,
                "readiness": checkpoint.readiness.model_dump(mode="json"),
                "approved_metrics": (
                    None
                    if approved is None
                    else [
                        row.model_dump(mode="json")
                        for row in approved.applicability
                    ]
                ),
            }
        )
    scenario_rows = []
    for scenario_row in evidence.scenarios:
        scenario_id = scenario_row.scenario["scenario_id"]
        planned = set(scenario_row.execution.generation_metrics)
        ready = sum(
            1
            for checkpoint in evidence.checkpoints
            if checkpoint.link.scenario_id == scenario_id
            and _classification(
                checkpoint,
                None
                if applicability is None
                else applicability.get(checkpoint.key),
            )
            == "uploadable"
        )
        scenario_rows.append(
            {
                "scenario_id": scenario_id,
                "example_id": scenario_row.example_id,
                "execution_status": scenario_row.execution.status,
                "assertion_status": scenario_row.record.assertion_status,
                "generation_scoring_status": (
                    scenario_row.record.generation_scoring_status
                ),
                "expected_generation_checkpoints": len(planned),
                "uploadable_generation_checkpoints": ready,
                "incomplete": list(scenario_row.record.incomplete),
                "run_error": scenario_row.sample.get("run_error"),
            }
        )
    return {
        "schema": EXTERNAL_LEDGER_SCHEMA,
        "run_id": evidence.run_id,
        "dataset": evidence.dataset_identity.model_dump(mode="json"),
        "source_sha256": evidence.source_sha256,
        "applicability_sha256": (
            None
            if applicability is None
            else _external_applicability_sha256(evidence, applicability)
        ),
        "counts": counts,
        "entries": entries,
        "scenarios": scenario_rows,
    }


def build_external_review(evidence: ExternalRunEvidence) -> dict[str, Any]:
    entries = []
    for checkpoint in evidence.checkpoints:
        entry = checkpoint.entry
        answer = (
            None
            if entry is None or entry.checkpoint is None
            else entry.checkpoint.answer.model_dump(mode="json")
        )
        context_items = (
            []
            if entry is None or entry.checkpoint is None
            else entry.checkpoint.model_input.items
        )
        retrieval = [
            {
                "tool_call_id": record.tool_call_id,
                "tool_name": EXTERNAL_RETRIEVAL_TOOL,
                "final_model_input": list(record.final_model_input),
                "rerank_mode": (
                    None if record.rerank is None else record.rerank.mode
                ),
            }
            for record in checkpoint.retrieval
        ]
        planned_reference = _planned_reference(
            checkpoint.scenario, checkpoint.link.checkpoint_id
        )
        question = (
            entry.checkpoint.user_input.message
            if entry is not None and entry.checkpoint is not None
            else checkpoint.scenario["input"]["message"]
        )
        entries.append(
            {
                "checkpoint": checkpoint.link.model_dump(mode="json"),
                "execution_sha256": checkpoint.execution_sha256,
                "display": _checkpoint_display(
                    checkpoint.scenario, checkpoint.link, question
                ),
                "scenario": {
                    "family": checkpoint.scenario["family"],
                    "tags": list(checkpoint.scenario["tags"]),
                    "message": checkpoint.scenario["input"]["message"],
                },
                "evidence": {
                    "checkpoint_present": entry is not None
                    and entry.checkpoint is not None
                    and entry.runner_error is None,
                    "runner_error": (
                        None if entry is None else entry.runner_error
                    ),
                    "answer_present": bool(
                        answer is not None and answer["visible_text"].strip()
                    ),
                    "request_boundary_present": entry is not None
                    and entry.request_boundary is not None,
                    "context_item_count": len(context_items),
                    "tool_result_count": sum(
                        1
                        for item in context_items
                        if item.source == "tool_result"
                    ),
                    "retrieval": retrieval,
                    "readiness": checkpoint.readiness.model_dump(
                        mode="json"
                    ),
                },
                "reference": (
                    None
                    if planned_reference is None
                    else planned_reference.model_dump(mode="json")
                ),
                "previous_applicability": [
                    row.model_dump(mode="json")
                    for row in _previous_applicability(
                        checkpoint.scenario,
                        checkpoint.link.checkpoint_id,
                    )
                ],
                "metrics": [
                    {"metric": metric, "applicable": None, "reason": None}
                    for metric in EXTERNAL_JUDGE_METRICS
                ],
            }
        )
    return {
        "schema_version": EXTERNAL_APPLICABILITY_REVIEW_SCHEMA,
        "status": "draft_requires_user_approval",
        "run_id": evidence.run_id,
        "dataset": evidence.dataset_identity.model_dump(mode="json"),
        "source_sha256": evidence.source_sha256,
        "approval_schema": EXTERNAL_APPLICABILITY_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "entries": entries,
    }


def load_external_applicability(
    evidence: ExternalRunEvidence,
) -> dict[tuple[str, str, str], ExternalApplicabilityEntry]:
    path = applicability_path(evidence.run_dir)
    _require(
        path.is_file(),
        f"缺少已批准的外部适用性输入文件: {path}",
    )
    raw = _read_json(path)
    _require(isinstance(raw, dict), "外部适用性输入必须为对象")
    _require(
        raw.get("schema_version") != EXTERNAL_APPLICABILITY_REVIEW_SCHEMA,
        "待审阅清单不是已批准输入，禁止直接提交",
    )
    try:
        parsed = ExternalApplicabilityInput.model_validate(raw)
    except ValidationError as error:
        detail = error.errors()[0]
        raise UploadBlocked(
            f"外部适用性输入无法解析: {detail['loc']}: {detail['msg']}"
        ) from error
    _require(
        parsed.run_id == evidence.run_id,
        "外部适用性输入 run_id 与源运行不一致",
    )
    _require(
        parsed.dataset == evidence.dataset_identity,
        "外部适用性输入数据身份与源运行不一致",
    )
    expected_approval_sha256 = parsed.content_sha256()
    _require(
        parsed.approval.content_sha256 == expected_approval_sha256,
        "外部适用性批准摘要与当前条目不一致，"
        f"approval.content_sha256 应为 {expected_approval_sha256}",
    )
    expected = {
        checkpoint.key: checkpoint for checkpoint in evidence.checkpoints
    }
    actual = {
        _checkpoint_key(entry.checkpoint): entry for entry in parsed.entries
    }
    unknown = sorted(set(actual) - set(expected))
    missing = sorted(set(expected) - set(actual))
    _require(not unknown, f"外部适用性包含未知检查点: {unknown[:3]}")
    _require(not missing, f"外部适用性缺少检查点: {missing[:3]}")
    for key, entry in actual.items():
        checkpoint = expected[key]
        _require(
            entry.checkpoint == checkpoint.link,
            f"外部适用性检查点身份冲突: {key}",
        )
        _require(
            entry.execution_sha256 == checkpoint.execution_sha256,
            f"外部适用性执行摘要冲突: {key}",
        )
    return actual


def _context_within_boundary(
    items: Sequence[Any], boundary: RequestBoundaryEvidence
) -> None:
    node_ids = {node.entry_id for node in boundary.nodes}
    tool_pairs = {
        (node.tool_call_id, node.tool_name) for node in boundary.tool_nodes
    }
    for item in items:
        if item.message_entry_id is not None:
            _require(
                item.message_entry_id in node_ids,
                "上下文条目不属于本次请求边界",
            )
        if item.source == "tool_result":
            _require(
                (item.tool_call_id, item.tool_name) in tool_pairs,
                "上下文工具结果不属于本次请求边界",
            )


def _external_retrieval_evidence(
    checkpoint: ExternalCheckpointEvidence,
    boundary: RequestBoundaryEvidence,
    items: Sequence[Any],
) -> tuple[list[ExternalJudgeRetrievalEvidence], list[dict[str, Any]]]:
    visible: dict[str, Any] = {}
    for item in items:
        if item.source != "tool_result":
            continue
        _require(
            item.tool_call_id not in visible,
            f"上下文包含重复工具结果: {item.tool_call_id}",
        )
        visible[item.tool_call_id] = item
    # 失败的工具调用没有检索阶段记录，仍作为真实上下文投影，只跳过检索证据绑定。
    errored = {
        node.tool_call_id for node in boundary.tool_nodes if node.is_error
    }
    records: dict[str, SearchStageRecord] = {}
    event_positions = {
        event["event_id"]: index
        for index, event in enumerate(checkpoint.scenario["events"])
    }
    request_position = event_positions.get(checkpoint.link.event_id)
    _require(
        request_position is not None,
        f"检查点事件不在场景中: {checkpoint.link.event_id}",
    )
    for record in checkpoint.retrieval:
        _require(
            record.run_id == checkpoint.link.run_id
            and record.scenario_id == checkpoint.link.scenario_id,
            "检索记录身份不属于当前检查点",
        )
        record_position = event_positions.get(record.event_id)
        _require(
            record_position is not None and record_position <= request_position,
            "检索记录不属于当前请求之前的场景事件",
        )
        _require(
            record.tool_call_id not in records,
            f"同一工具调用存在多条检索记录: {record.tool_call_id}",
        )
        records[record.tool_call_id] = record
    for item in items:
        if (
            item.source == "tool_result"
            and item.tool_name == EXTERNAL_RETRIEVAL_TOOL
            and item.tool_call_id not in errored
        ):
            _require(
                item.tool_call_id in records,
                f"检索工具结果缺少检索证据: {item.tool_call_id}",
            )
    evidence: list[ExternalJudgeRetrievalEvidence] = []
    audit: list[dict[str, Any]] = []
    boundary_tools = {
        (node.tool_call_id, node.tool_name) for node in boundary.tool_nodes
    }
    for tool_call_id in sorted(records):
        _require(
            tool_call_id not in errored,
            f"检索记录绑定到失败的工具调用: {tool_call_id}",
        )
        item = visible.get(tool_call_id)
        if item is None:
            # 该检索结果不在本次请求的真实上下文中，禁止进入投影。
            continue
        _require(
            item.tool_name == EXTERNAL_RETRIEVAL_TOOL,
            f"检索记录对应的工具结果名称不一致: {tool_call_id}",
        )
        _require(
            (tool_call_id, item.tool_name) in boundary_tools,
            f"检索工具结果不在请求边界: {tool_call_id}",
        )
        _require(
            not item.truncated,
            f"检索工具结果被裁剪，无法构造完整搜索证据: {tool_call_id}",
        )
        record = records[tool_call_id]
        try:
            search = search_evidence_from_tool(item.content, tool_call_id)
            search.require_scored_consistency(record.final_model_input)
        except Exception as error:
            raise UploadBlocked(
                f"检索工具结果与检索证据不一致（{tool_call_id}）: "
                f"{type(error).__name__}: {error}"
            ) from error
        _require(
            search.response_json == record.response_json,
            f"检索工具结果与检索证据响应不一致: {tool_call_id}",
        )
        rerank = None
        if record.rerank is not None:
            rerank_payload = record.rerank.model_dump(mode="json")
            rerank_payload["identity"]["endpoint"] = sha256_text(
                record.rerank.identity.endpoint
            )
            rerank = rerank_payload
        evidence.append(
            ExternalJudgeRetrievalEvidence(
                tool_call_id=tool_call_id,
                tool_name=item.tool_name,
                search=search,
                rerank=rerank,
            )
        )
        audit.append(
            {
                "tool_call_id": tool_call_id,
                "tool_name": item.tool_name,
                "event_id": record.event_id,
                "candidate_pool": list(record.candidate_pool),
                "final_model_input": list(record.final_model_input),
                "rerank_scores": (
                    []
                    if record.rerank is None
                    else [
                        row.model_dump(mode="json")
                        for row in record.rerank.scored
                    ]
                ),
                "search_query": record.arguments,
            }
        )
    return evidence, audit


def build_external_projection(
    checkpoint: ExternalCheckpointEvidence,
    approved: ExternalApplicabilityEntry,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _require(
        checkpoint.entry is not None
        and checkpoint.entry.checkpoint is not None
        and checkpoint.entry.runner_error is None,
        f"检查点缺少可投影证据: {checkpoint.key}",
    )
    source = checkpoint.entry.checkpoint
    boundary = checkpoint.entry.request_boundary
    _require(boundary is not None, f"检查点缺少请求边界: {checkpoint.key}")
    answer = source.answer
    _require(
        bool(answer.visible_text.strip()),
        f"检查点缺少真实回答: {checkpoint.key}",
    )
    _context_within_boundary(source.model_input.items, boundary)
    retrieval, audit = _external_retrieval_evidence(
        checkpoint, boundary, source.model_input.items
    )
    applicable = any(row.applicable for row in approved.applicability)
    if applicable:
        _require(
            source.reference is not None,
            f"适用指标缺少参考事实与任务要求: {checkpoint.key}",
        )
    upload = ExternalJudgeCheckpointUpload(
        schema_version=EXTERNAL_JUDGE_CHECKPOINT_SCHEMA_VERSION,
        metadata=ExternalJudgeMetadata(
            dataset=checkpoint.execution.dataset,
            checkpoint=checkpoint.link,
            execution_sha256=checkpoint.execution_sha256,
        ),
        inputs=ExternalJudgeCheckpointInputs(
            user_request=ExternalJudgeUserRequest(
                checkpoint=checkpoint.link,
                message=source.user_input.message,
                prior_messages=list(source.user_input.prior_messages),
            )
        ),
        outputs=ExternalJudgeCheckpointOutputs(
            answer=source.answer,
            context=ExternalJudgeContext(
                items=list(source.model_input.items),
                truncated=source.model_input.truncated,
                omitted_chars=source.model_input.omitted_chars,
                request_boundary=boundary,
                retrieval=retrieval,
            ),
        ),
        reference_outputs=ExternalJudgeReferenceOutputs(
            reference=source.reference,
            applicability=list(approved.applicability),
        ),
    )
    sanitized = _sanitize_outbound(upload.model_dump(mode="json"))
    try:
        validated = ExternalJudgeCheckpointUpload.model_validate(sanitized)
    except ValidationError as error:
        detail = error.errors()[0]
        raise UploadBlocked(
            f"脱敏后的检查点投影无法通过契约校验: "
            f"{detail['loc']}: {detail['msg']}"
        ) from error
    sanitized = validated.model_dump(mode="json")
    _require(
        sanitized["metadata"]["dataset"]
        == checkpoint.execution.dataset.model_dump(mode="json"),
        f"投影数据身份不一致: {checkpoint.key}",
    )
    _require(
        sanitized["metadata"]["execution_sha256"]
        == checkpoint.execution_sha256,
        f"投影执行摘要不一致: {checkpoint.key}",
    )
    _require(
        sanitized["reference_outputs"]["applicability"]
        == [row.model_dump(mode="json") for row in approved.applicability],
        f"投影适用性与批准输入不一致: {checkpoint.key}",
    )
    return sanitized, {
        "scenario_id": checkpoint.link.scenario_id,
        "event_id": checkpoint.link.event_id,
        "checkpoint_id": checkpoint.link.checkpoint_id,
        "execution_sha256": checkpoint.execution_sha256,
        "retrieval": _sanitize_outbound(audit),
    }


def _assert_external_upload_whitelist(payload: dict[str, Any]) -> None:
    # 复用旧白名单；外部契约的 endpoint 字段只承载地址摘要，按既有 endpoint_sha256 约定复核。
    probe = deepcopy(payload)
    for row in probe["rows"]:
        for item in row["upload"]["outputs"]["context"]["retrieval"]:
            if item["rerank"] is None:
                continue
            identity = item["rerank"]["identity"]
            endpoint = identity.pop("endpoint")
            _require(
                isinstance(endpoint, str)
                and re.fullmatch(r"[0-9a-f]{64}", endpoint) is not None,
                "重排地址必须为 SHA-256 摘要",
            )
            identity["endpoint_sha256"] = endpoint
    forbidden = sorted(set(_walk_keys(probe)) & FORBIDDEN_KEYS)
    _require(not forbidden, f"上传 payload 包含禁止字段: {','.join(forbidden)}")
    # 逐字符串扫描：canonical JSON 会把正文换行转义成 \n，序列化后扫描会把 "字:\n\n1." 误判成盘符路径。
    for variable in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        secret = os.environ.get(variable)
        for text in _walk_strings(probe):
            if secret:
                _require(secret not in text, "上传 payload 包含 Langfuse 凭据")
    for text in _walk_strings(probe):
        residue = _local_path_residue(text)
        _require(residue is None, f"上传 payload 包含本地路径: {residue}")
        for marker in (
            "Bearer ",
            "Authorization:",
            "LANGFUSE_PUBLIC_KEY",
            "LANGFUSE_SECRET_KEY",
        ):
            _require(marker not in text, f"上传 payload 包含凭据标记: {marker}")


def _row_id(link: EvidenceLink, execution_sha256: str) -> str:
    # 新展示投影使用独立 item id 种子，避免复用旧投影的远端身份。
    return str(
        uuid5(
            NAMESPACE_URL,
            f"{EXTERNAL_REMOTE_PREFIX}:"
            f"{link.run_id}:{link.scenario_id}:{link.event_id}:"
            f"{link.checkpoint_id}:{execution_sha256}",
        )
    )


def build_external_payload(
    evidence: ExternalRunEvidence,
    applicability: dict[
        tuple[str, str, str], ExternalApplicabilityEntry
    ],
) -> tuple[dict[str, Any], dict[str, Any]]:
    ledger = _coverage_ledger(evidence, applicability)
    rows = []
    for checkpoint in evidence.checkpoints:
        approved = applicability[checkpoint.key]
        if _classification(checkpoint, approved) != "uploadable":
            continue
        upload, audit = build_external_projection(checkpoint, approved)
        projection_sha256 = sha256_text(_canonical(upload))
        rows.append(
            {
                "row_id": _row_id(checkpoint.link, checkpoint.execution_sha256),
                "projection_sha256": projection_sha256,
                "dataset_input": _dataset_input(
                    upload, checkpoint.scenario, checkpoint.link
                ),
                "upload": upload,
                "audit": audit,
            }
        )
    _require(bool(rows), "没有可上传的外部检查点")
    rows.sort(key=lambda row: row["row_id"])
    payload = {
        "schema": EXTERNAL_PAYLOAD_SCHEMA,
        "presentation_version": EXTERNAL_PRESENTATION_VERSION,
        "run_id": evidence.run_id,
        "dataset": evidence.dataset_identity.model_dump(mode="json"),
        "dataset_name": (
            f"{EXTERNAL_REMOTE_PREFIX}-{evidence.run_id}-"
            f"{_external_applicability_sha256(evidence, applicability)[:12]}"
        ),
        "experiment_name": "",
        "source_sha256": evidence.source_sha256,
        "applicability_sha256": _external_applicability_sha256(
            evidence, applicability
        ),
        "rows": rows,
        "coverage": ledger["counts"],
    }
    payload = _sanitize_outbound(payload)
    payload["rows_sha256"] = sha256_text(_canonical(payload["rows"]))
    payload["projections_sha256"] = sha256_text(
        _canonical(
            [
                {
                    "row_id": row["row_id"],
                    "projection_sha256": row["projection_sha256"],
                }
                for row in payload["rows"]
            ]
        )
    )
    payload["experiment_name"] = (
        f"{EXTERNAL_REMOTE_PREFIX}-{evidence.run_id}-"
        f"{payload['projections_sha256'][:12]}"
    )
    _assert_external_upload_whitelist(payload)
    return payload, ledger


def _external_applicability_sha256(
    evidence: ExternalRunEvidence,
    applicability: dict[
        tuple[str, str, str], ExternalApplicabilityEntry
    ],
) -> str:
    # 与 ExternalApplicabilityInput.content_sha256() 使用同一材料，批准身份可逐字复核。
    entries = [
        applicability[key].model_dump(mode="json")
        for key in sorted(applicability)
    ]
    return sha256_text(
        _canonical(
            {
                "schema_version": EXTERNAL_APPLICABILITY_SCHEMA,
                "run_id": evidence.run_id,
                "dataset": evidence.dataset_identity.model_dump(mode="json"),
                "entries": entries,
            }
        )
    )


def _external_documents(
    source: Path, dataset_dir: Path
) -> tuple[
    ExternalRunEvidence,
    dict[tuple[str, str, str], ExternalApplicabilityEntry],
    dict[str, Any],
    dict[str, Any],
]:
    load_dotenv(BACKEND / ".env", override=False)
    dataset = load_for_evaluation(Path(dataset_dir))
    evidence = load_external_run(Path(source), dataset)
    applicability = load_external_applicability(evidence)
    payload, ledger = build_external_payload(evidence, applicability)
    return evidence, applicability, payload, ledger


def _write_payload_documents(
    run_dir: Path, payload: dict[str, Any], ledger: dict[str, Any]
) -> tuple[Path, Path]:
    directory = external_dir(run_dir)
    payload_path = directory / EXTERNAL_PAYLOAD_NAME
    ledger_path = directory / EXTERNAL_LEDGER_NAME
    _write_json(payload_path, payload)
    _write_json(ledger_path, ledger)
    return payload_path, ledger_path


def check_external_projection(
    source: Path, *, dataset_dir: Path = DEFAULT_VERSION_DIR
) -> dict[str, Any]:
    evidence, _, payload, ledger = _external_documents(source, dataset_dir)
    payload_path, ledger_path = _write_payload_documents(
        evidence.run_dir, payload, ledger
    )
    return {
        "status": "offline_projection_checked",
        "network": "not_called",
        "run_id": evidence.run_id,
        "source_sha256": evidence.source_sha256,
        "applicability_sha256": payload["applicability_sha256"],
        "payload_sha256": payload_digest(payload),
        "rows_sha256": payload["rows_sha256"],
        "projections_sha256": payload["projections_sha256"],
        "rows": len(payload["rows"]),
        "coverage": payload["coverage"],
        "dataset_name": payload["dataset_name"],
        "experiment_name": payload["experiment_name"],
        "payload_path": str(payload_path),
        "ledger_path": str(ledger_path),
    }


def review_external_source(
    source: Path, *, dataset_dir: Path = DEFAULT_VERSION_DIR
) -> dict[str, Any]:
    dataset = load_for_evaluation(Path(dataset_dir))
    evidence = load_external_run(Path(source), dataset)
    review = build_external_review(evidence)
    review_path = applicability_review_path(evidence.run_dir)
    if review_path.is_file():
        existing = _read_json(review_path)
        _require(
            isinstance(existing, dict)
            and existing.get("run_id") == evidence.run_id
            and existing.get("source_sha256") == evidence.source_sha256,
            "已有适用性审阅清单来自其他源证据，拒绝覆盖",
        )
        review = existing
        reused = True
    else:
        _write_json(review_path, review)
        reused = False
    ledger = _coverage_ledger(evidence, None)
    ledger_path = external_dir(evidence.run_dir) / EXTERNAL_LEDGER_NAME
    _write_json(ledger_path, ledger)
    return {
        "status": "review_ready",
        "network": "not_called",
        "reused": reused,
        "run_id": evidence.run_id,
        "source_sha256": evidence.source_sha256,
        "counts": ledger["counts"],
        "review_path": str(review_path),
        "ledger_path": str(ledger_path),
        "approval_path": str(applicability_path(evidence.run_dir)),
    }


def _dataset_marker(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": EXTERNAL_PAYLOAD_SCHEMA,
        "presentation_version": EXTERNAL_PRESENTATION_VERSION,
        "run_id": payload["run_id"],
        "dataset": payload["dataset"],
        "source_sha256": payload["source_sha256"],
        "applicability_sha256": payload["applicability_sha256"],
        "rows_sha256": payload["rows_sha256"],
        "projections_sha256": payload["projections_sha256"],
        "example_count": len(payload["rows"]),
    }


def _metric_flags(applicability: list[dict[str, Any]]) -> dict[str, str]:
    # 网页 rule 的 metadata 过滤只支持字符串值；适用性标志使用明确字符串。
    return {
        _metric_metadata_key(row["metric"]): (
            "applicable" if row["applicable"] else "not_applicable"
        )
        for row in applicability
    }


def _example_metadata(
    payload: dict[str, Any], row: dict[str, Any]
) -> dict[str, Any]:
    checkpoint = row["upload"]["metadata"]["checkpoint"]
    return {
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
        **_metric_flags(row["upload"]["reference_outputs"]["applicability"]),
    }


def _example_document(
    payload: dict[str, Any], row: dict[str, Any]
) -> dict[str, Any]:
    return {
        "id": row["row_id"],
        "input": row["dataset_input"],
        "expected_output": row["upload"]["reference_outputs"],
        "metadata": _example_metadata(payload, row),
    }


def _verify_metadata_subset(
    actual: Any,
    expected: dict[str, Any],
    allowed_extra: frozenset[str],
    label: str,
) -> None:
    _require(isinstance(actual, dict), f"远端 {label} 必须为对象")
    unknown = set(actual) - set(expected) - set(allowed_extra)
    _require(not unknown, f"远端 {label} 包含未知字段: {sorted(unknown)[:3]}")
    for key, value in expected.items():
        _require(
            actual.get(key) == value,
            f"远端 {label} 字段不符: {key}",
        )


def _verify_example_document(
    expected: dict[str, Any], actual: Any
) -> None:
    _require(
        str(actual.id) == expected["id"],
        f"远端样例 ID 不符: {expected['id']}",
    )
    _require(
        actual.input == expected["input"],
        f"远端样例 input 不符: {expected['id']}",
    )
    _require(
        actual.expected_output == expected["expected_output"],
        f"远端样例 expected_output 不符: {expected['id']}",
    )
    _verify_metadata_subset(
        actual.metadata,
        expected["metadata"],
        EXTERNAL_EXAMPLE_METADATA_EXTRA,
        f"样例 metadata: {expected['id']}",
    )


def _fetch_examples(client: Any, dataset_name: str) -> dict[str, Any]:
    return {
        str(example.id): example
        for example in client.get_dataset(dataset_name).items
    }


def _verify_external_examples(
    payload: dict[str, Any], examples: dict[str, Any]
) -> None:
    expected = {
        row["row_id"]: _example_document(payload, row)
        for row in payload["rows"]
    }
    unexpected = sorted(set(examples) - set(expected))
    missing = sorted(set(expected) - set(examples))
    _require(
        not unexpected,
        f"远端检查点数据集包含未知样例: {unexpected[:3]}",
    )
    _require(
        not missing,
        f"远端检查点数据集缺少样例: {missing[:3]}",
    )
    for example_id, document in expected.items():
        _verify_example_document(document, examples[example_id])


def _dataset_page_url(client: Any, dataset: Any) -> str | None:
    project_id = getattr(dataset, "project_id", None)
    base_url = getattr(client, "_base_url", None)
    if not project_id or not base_url:
        return None
    return f"{str(base_url).rstrip('/')}/project/{project_id}/datasets/{dataset.id}"


def _external_receipt_identity(
    receipt: dict[str, Any],
    payload: dict[str, Any],
    *,
    schema: str,
) -> None:
    _require(receipt.get("schema") == schema, "回执版本不受支持")
    _require(
        receipt.get("run_id") == payload["run_id"]
        and receipt.get("source_sha256") == payload["source_sha256"]
        and receipt.get("applicability_sha256")
        == payload["applicability_sha256"]
        and receipt.get("rows_sha256") == payload["rows_sha256"]
        and receipt.get("payload_sha256") == payload_digest(payload),
        "已有回执与当前提交身份不一致",
    )


def _resolve_remote_dataset(
    client: Any, payload: dict[str, Any]
) -> tuple[Any, bool]:
    # Langfuse 数据集按项目内唯一名称解析；缺失时 SDK 抛 NotFoundError。
    name = payload["dataset_name"]
    try:
        remote = client.get_dataset(name)
    except NotFoundError:
        remote = None
    if remote is not None:
        return remote, False
    marker = _dataset_marker(payload)
    client.create_dataset(
        name=name,
        description="Fit-Agent 外部裁判检查点；输入为用户请求，输出为参考要求与显式适用性。",
        metadata=marker,
    )
    remote = client.get_dataset(name)
    _require(remote.name == name, "创建后未解析到唯一检查点数据集")
    return remote, True


def _verify_remote_dataset(
    client: Any,
    payload: dict[str, Any],
    dataset_id: str,
) -> Any:
    try:
        dataset = client.get_dataset(payload["dataset_name"])
    except NotFoundError as error:
        raise UploadBlocked(
            "回执记录的检查点数据集在远端不存在，拒绝自动重建"
        ) from error
    _require(
        str(dataset.id) == dataset_id,
        "远端检查点数据集与回执身份不一致",
    )
    _require(
        dataset.name == payload["dataset_name"],
        "远端检查点数据集名称与 payload 不一致",
    )
    _verify_metadata_subset(
        dataset.metadata,
        _dataset_marker(payload),
        EXTERNAL_DATASET_METADATA_EXTRA,
        "检查点数据集 metadata",
    )
    examples = _fetch_examples(client, payload["dataset_name"])
    _verify_external_examples(payload, examples)
    _require(
        len(examples) == len(payload["rows"]),
        "远端检查点数据集样例数与 payload 不一致",
    )
    return dataset


JUDGE_COMMON_INSTRUCTIONS = (
    "只依据本检查点提供的 user_request、answer、context、reference 判断，"
    "不补充外部知识。"
    "answer 是对象：以 answer.visible_text 为用户可见回答正文；"
    "answer.structured_snapshot 只在包含实际展示的计划或业务内容时作为回答的一部分，"
    "忽略 thinking、签名与用量等内部字段。"
    "context 是模型实际可见的上下文条目数组，只按 content 判断，"
    "source、tool_call_id、tool_name 与 fact_origin 仅用于定位来源。"
    "reference 是已审阅参考要求对象。"
)
JUDGE_SYSTEM_PROMPTS = {
    "evidence_supported_v1": (
        "判断回答中的事实陈述是否全部由提供的证据上下文支持。"
        "只评估动作名称、器械、目标肌群、重量口径、步骤与用户已有事实；"
        "忽略寒暄、训练建议、计划安排和没有事实内容的表述。"
        "回答中的任一具体事实若在证据上下文中没有依据，或与证据上下文矛盾，"
        "则整条回答为否。"
        "真：全部事实陈述都能在证据上下文中找到依据，且没有与证据矛盾的事实。"
        "假：至少包含一条没有依据或与证据矛盾的具体事实。"
        "输出 NUMERIC 分数，范围 0 到 1；1 表示真，0 表示假；保留判断理由。"
    ),
    "reference_requirements_met_v1": (
        "判断回答是否满足参考答案中的任务要求。"
        "必须同时检查：reference.must_include 列出的内容全部出现；"
        "reference.forbidden_claims 列出的任一表述都没有出现；"
        "reference.allowed_variation 只用于允许的替代表述，不能替代必须内容。"
        "参考事实只用于核对事实，不要求逐字复述。"
        "缺少任一必须内容或出现任一禁止声明，则整条回答为否。"
        "真：全部必须内容出现且没有禁止声明。"
        "假：缺少至少一项必须内容或出现至少一项禁止声明。"
        "输出 NUMERIC 分数，范围 0 到 1；1 表示真，0 表示假；保留判断理由。"
    ),
}
JUDGE_USER_PROMPT = (
    "user_request:\n{{user_request}}\n\n"
    "answer:\n{{answer}}\n\n"
    "context:\n{{context}}\n\n"
    "reference:\n{{reference}}"
)
EXPECTED_VARIABLE_MAPPING = {
    "user_request": {"source": "input", "path": "user_request"},
    "answer": {"source": "output", "path": "answer"},
    "context": {"source": "output", "path": "context.items"},
    "reference": {"source": "expected_output", "path": "reference"},
}


def _metric_metadata_key(metric: str) -> str:
    return f"{EXTERNAL_METRIC_METADATA_PREFIX}{metric}"


def _short_question(question: str, limit: int = 120) -> str:
    text = " ".join(question.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _checkpoint_display(
    scenario: dict[str, Any], link: EvidenceLink, question: str
) -> dict[str, Any]:
    tags = [tag for tag in scenario["tags"] if tag]
    family = scenario["family"]
    parts = [link.scenario_id]
    if family:
        parts.append(family)
    if tags:
        parts.append("/".join(tags))
    scenario_label = " · ".join(parts)
    return {
        "title": (
            f"{scenario_label} · {link.event_id}/{link.checkpoint_id} · "
            f"{_short_question(question)}"
        ),
        "scenario_id": link.scenario_id,
        "scenario_label": scenario_label,
        "family": family,
        "tags": tags,
        "event_id": link.event_id,
        "checkpoint_id": link.checkpoint_id,
        "question": question,
    }


def _dataset_input(
    upload: dict[str, Any],
    scenario: dict[str, Any],
    link: EvidenceLink,
) -> dict[str, Any]:
    request = upload["inputs"]["user_request"]
    return {
        "display": _checkpoint_display(
            scenario, link, request["message"]
        ),
        "user_request": {
            "message": request["message"],
            "prior_messages": list(request["prior_messages"]),
        },
    }


def external_rule_filters(
    metric: str, dataset_id: str
) -> list[dict[str, Any]]:
    # Langfuse evaluation rule 的 API 结构性约束：datasetId 与 root observation
    # 限定作用域，metadata 过滤只支持字符串值。
    return [
        {
            "type": "boolean",
            "column": EXTERNAL_OBSERVATION_SCOPE,
            "operator": "=",
            "value": True,
        },
        {
            "type": "stringOptions",
            "column": "datasetId",
            "operator": "any of",
            "value": [dataset_id],
        },
        {
            "type": "stringObject",
            "column": "metadata",
            "key": _metric_metadata_key(metric),
            "operator": "=",
            "value": "applicable",
        },
    ]


def _web_judge_configuration(
    payload: dict[str, Any], dataset_id: str
) -> dict[str, Any]:
    # 网页端配置要求：两指标各一个 evaluator；rule 以数据集 id 与 root observation
    # 限定作用域，再按字符串 metadata 适用性标志过滤；本地无法验证网页规则。
    return {
        "ownership": "langfuse",
        "scope": {
            "dataset_id": dataset_id,
            "dataset_name": payload["dataset_name"],
            "observation_scope": EXTERNAL_OBSERVATION_SCOPE,
            "sampling": EXTERNAL_RULE_SAMPLING,
        },
        "evaluators": [
            {
                "metric": metric,
                "evaluator_name": metric,
                "score_name": metric,
                "score_type": "NUMERIC",
                "value_range": [0.0, 1.0],
                "sampling": EXTERNAL_RULE_SAMPLING,
                "rule_filters": external_rule_filters(metric, dataset_id),
                "system_prompt": (
                    JUDGE_COMMON_INSTRUCTIONS + JUDGE_SYSTEM_PROMPTS[metric]
                ),
                "user_prompt": JUDGE_USER_PROMPT,
            }
            for metric in EXTERNAL_JUDGE_METRICS
        ],
        "variable_mapping": EXPECTED_VARIABLE_MAPPING,
        "collection_guard": (
            "每个适用检查点每指标恰好一条终态 score；"
            "不适用指标出现对应 score 按意外反馈阻断"
        ),
    }


def sync_external_dataset(
    source: Path,
    *,
    dataset_dir: Path = DEFAULT_VERSION_DIR,
    client: Any | None = None,
) -> dict[str, Any]:
    load_dotenv(BACKEND / ".env", override=False)
    require_credentials()
    evidence, _, payload, ledger = _external_documents(source, dataset_dir)
    payload_path, ledger_path = _write_payload_documents(
        evidence.run_dir, payload, ledger
    )
    receipt_path = external_dir(evidence.run_dir) / EXTERNAL_DATASET_RECEIPT_NAME
    client = client or make_client()
    if receipt_path.is_file():
        receipt = _read_json(receipt_path)
        _external_receipt_identity(
            receipt, payload, schema=EXTERNAL_DATASET_RECEIPT_SCHEMA
        )
        _require(
            receipt.get("web_judge")
            == _web_judge_configuration(payload, receipt["dataset_id"]),
            "已有数据集回执的网页裁判配置与当前投影不一致",
        )
        _verify_remote_dataset(client, payload, receipt["dataset_id"])
        return {
            "status": "verified_existing",
            "dataset_name": payload["dataset_name"],
            "dataset_id": receipt["dataset_id"],
            "dataset_url": receipt.get("dataset_url"),
            "example_count": len(payload["rows"]),
            "payload_sha256": payload_digest(payload),
            "receipt_path": str(receipt_path),
            "payload_path": str(payload_path),
            "ledger_path": str(ledger_path),
        }
    remote, created = _resolve_remote_dataset(client, payload)
    dataset_id = str(remote.id)
    _verify_metadata_subset(
        remote.metadata,
        _dataset_marker(payload),
        EXTERNAL_DATASET_METADATA_EXTRA,
        "检查点数据集 metadata",
    )
    expected = {
        row["row_id"]: _example_document(payload, row)
        for row in payload["rows"]
    }
    existing = _fetch_examples(client, payload["dataset_name"])
    unexpected = sorted(set(existing) - set(expected))
    _require(
        not unexpected,
        f"远端检查点数据集包含未知样例: {unexpected[:3]}",
    )
    for example_id, example in existing.items():
        _verify_example_document(expected[example_id], example)
    missing = [
        expected[example_id]
        for example_id in expected
        if example_id not in existing
    ]
    for document in missing:
        # 显式传 id 依赖 Langfuse 的按 id upsert 幂等语义。
        client.create_dataset_item(
            dataset_name=payload["dataset_name"],
            id=document["id"],
            input=document["input"],
            expected_output=document["expected_output"],
            metadata=document["metadata"],
        )
    remote_examples = _fetch_examples(client, payload["dataset_name"])
    _verify_external_examples(payload, remote_examples)
    dataset_url = _dataset_page_url(client, remote)
    receipt = {
        "schema": EXTERNAL_DATASET_RECEIPT_SCHEMA,
        "presentation_version": EXTERNAL_PRESENTATION_VERSION,
        "run_id": payload["run_id"],
        "dataset": payload["dataset"],
        "source_sha256": payload["source_sha256"],
        "payload_sha256": payload_digest(payload),
        "applicability_sha256": payload["applicability_sha256"],
        "rows_sha256": payload["rows_sha256"],
        "projections_sha256": payload["projections_sha256"],
        "dataset_name": payload["dataset_name"],
        "dataset_id": dataset_id,
        "dataset_url": dataset_url,
        "item_ids": [row["row_id"] for row in payload["rows"]],
        "example_count": len(payload["rows"]),
        "created": created,
        "web_judge": _web_judge_configuration(payload, dataset_id),
        "synced_at": datetime.now(UTC).isoformat(),
    }
    _write_json(receipt_path, receipt)
    return {
        "status": "created" if created else "reused",
        "dataset_name": receipt["dataset_name"],
        "dataset_id": dataset_id,
        "dataset_url": dataset_url,
        "example_count": receipt["example_count"],
        "payload_sha256": receipt["payload_sha256"],
        "receipt_path": str(receipt_path),
        "payload_path": str(payload_path),
        "ledger_path": str(ledger_path),
    }


def _experiment_metadata(
    payload: dict[str, Any], dataset_id: str
) -> dict[str, Any]:
    return {
        "schema": EXTERNAL_PAYLOAD_SCHEMA,
        "presentation_version": EXTERNAL_PRESENTATION_VERSION,
        "local_run_id": payload["run_id"],
        "dataset": payload["dataset"],
        "dataset_id": dataset_id,
        "dataset_name": payload["dataset_name"],
        "source_sha256": payload["source_sha256"],
        "payload_sha256": payload_digest(payload),
        "applicability_sha256": payload["applicability_sha256"],
        "rows_sha256": payload["rows_sha256"],
        "projections_sha256": payload["projections_sha256"],
        "external_scoring_status": "pending_collection",
        "coverage": payload["coverage"],
        "row_count": len(payload["rows"]),
        "generation_quality_scope": {
            "dataset_id": dataset_id,
            "observation_scope": EXTERNAL_OBSERVATION_SCOPE,
            "sampling": EXTERNAL_RULE_SAMPLING,
        },
    }


def _list_remote_experiments(
    client: Any,
    *,
    from_start_time: datetime,
    experiment_id: str | None = None,
    name: str | None = None,
) -> list[Any]:
    experiments = []
    cursor = None
    while True:
        response = client.api.experiments.list(
            from_start_time=from_start_time,
            id=experiment_id,
            name=name,
            fields="core,metadata",
            cursor=cursor,
        )
        experiments.extend(response.data)
        cursor = response.meta.cursor
        if not cursor:
            return experiments


def _verify_experiment_project(
    client: Any,
    project_id: str,
    experiment_name: str,
    dataset_id: str,
    metadata: dict[str, Any],
    *,
    from_start_time: datetime,
) -> Any:
    matches = [
        experiment
        for experiment in _list_remote_experiments(
            client, from_start_time=from_start_time, experiment_id=project_id
        )
        if experiment.id == project_id
    ]
    if not matches:
        raise UploadBlocked("回执记录的检查点实验在远端不存在，拒绝自动重提")
    _require(len(matches) == 1, "远端检查点实验身份不唯一，拒绝继续")
    experiment = matches[0]
    _require(
        experiment.name == experiment_name,
        "远端实验名称与提交身份不一致",
    )
    _require(
        str(experiment.dataset_id) == dataset_id,
        "远端实验绑定的数据集与提交身份不一致",
    )
    remote_metadata = experiment.metadata or {}
    for key, value in metadata.items():
        _require(
            remote_metadata.get(key) == value,
            f"远端实验 metadata 不符: {key}",
        )
    return experiment


def _find_remote_experiment(
    client: Any,
    experiment_name: str,
    *,
    from_start_time: datetime,
) -> Any | None:
    matches = [
        experiment
        for experiment in _list_remote_experiments(
            client, from_start_time=from_start_time, name=experiment_name
        )
        if experiment.name == experiment_name
    ]
    _require(
        len(matches) <= 1,
        "远端存在多个同名检查点实验，拒绝继续",
    )
    return matches[0] if matches else None


def _frozen_run_output(row: dict[str, Any]) -> dict[str, Any]:
    # 实验 Run outputs：冻结回答与上下文，reference 一并放入供网页裁判变量映射。
    outputs = row["upload"]["outputs"]
    document = {
        "answer": outputs["answer"],
        "context": outputs["context"],
        "reference": row["upload"]["reference_outputs"]["reference"],
    }
    _assert_external_upload_whitelist(
        {"rows": [{"upload": {"outputs": document}}]}
    )
    return document


class _FrozenCheckpointTarget:
    rows: dict[str, dict[str, Any]]

    def __init__(self, payload: dict[str, Any]) -> None:
        self.rows = {}
        for row in payload["rows"]:
            key = sha256_text(_canonical(row["dataset_input"]))
            _require(key not in self.rows, "检查点样例输入重复")
            self.rows[key] = row

    def __call__(self, *, item: Any) -> dict[str, Any]:
        # Langfuse runner 以 item 关键字调用 task；hosted item 取 .input 字段。
        inputs = item["input"] if isinstance(item, Mapping) else item.input
        key = sha256_text(_canonical(inputs))
        row = self.rows.get(key)
        _require(
            row is not None,
            "外部裁判 target 只返回冻结检查点输出，未知输入被拒绝",
        )
        return deepcopy(_frozen_run_output(row))


def _root_runs(
    client: Any,
    experiment_id: str,
    *,
    from_start_time: datetime,
) -> list[Any]:
    items = []
    cursor = None
    while True:
        response = client.api.experiments.list_items(
            from_start_time=from_start_time,
            experiment_id=experiment_id,
            fields="core,dataset,io",
            cursor=cursor,
        )
        items.extend(response.data)
        cursor = response.meta.cursor
        if not cursor:
            return items


def _verify_run_row(row: dict[str, Any], item: Any) -> None:
    row_id = row["row_id"]
    _require(
        item.level != "ERROR",
        f"远端运行失败: {row_id}",
    )
    _require(
        item.input == row["dataset_input"],
        f"远端运行 inputs 与冻结样例不一致: {row_id}",
    )
    _require(
        item.output == _frozen_run_output(row),
        f"远端运行 outputs 与冻结回答/上下文不一致: {row_id}",
    )


def _collect_external_runs(
    payload: dict[str, Any], runs: Sequence[Any]
) -> tuple[list[str], dict[str, str]]:
    expected = {row["row_id"]: row for row in payload["rows"]}
    by_example: dict[str, list[Any]] = {}
    for item in runs:
        _require(
            item.experiment_item_id is not None,
            f"实验条目缺少样例身份: {item.id}",
        )
        by_example.setdefault(str(item.experiment_item_id), []).append(item)
    unexpected = sorted(set(by_example) - set(expected))
    _require(
        not unexpected, f"远端实验包含未知样例: {unexpected[:3]}"
    )
    mapping: dict[str, str] = {}
    missing: list[str] = []
    for row_id, row in expected.items():
        matched = by_example.get(row_id, [])
        _require(
            len(matched) <= 1,
            f"远端同一样例存在多个实验条目: {row_id}",
        )
        if not matched:
            missing.append(row_id)
            continue
        _verify_run_row(row, matched[0])
        mapping[row_id] = str(matched[0].trace_id)
    return missing, mapping


def _await_external_runs(
    client: Any,
    experiment_id: str,
    payload: dict[str, Any],
    *,
    from_start_time: datetime,
) -> dict[str, str]:
    # Langfuse 落库异步：有界轮询远端实验条目，直至每行恰好绑定一个 trace。
    deadline = time.monotonic() + EXTERNAL_REMOTE_VERIFY_TIMEOUT_SECONDS
    while True:
        missing, mapping = _collect_external_runs(
            payload,
            _root_runs(
                client, experiment_id, from_start_time=from_start_time
            ),
        )
        if not missing:
            return mapping
        if time.monotonic() >= deadline:
            raise UploadBlocked(
                f"远端检查点运行未在时限内齐备: {missing[:3]}"
            )
        time.sleep(1.5)


def _read_dataset_receipt(
    run_dir: Path, payload: dict[str, Any]
) -> dict[str, Any]:
    path = external_dir(run_dir) / EXTERNAL_DATASET_RECEIPT_NAME
    _require(path.is_file(), "提交前必须先同步检查点数据集")
    receipt = _read_json(path)
    _external_receipt_identity(
        receipt, payload, schema=EXTERNAL_DATASET_RECEIPT_SCHEMA
    )
    return receipt


def _experiment_pending_document(
    payload: dict[str, Any],
    dataset_id: str,
    experiment_name: str,
    payload_sha256: str,
) -> dict[str, Any]:
    return {
        "schema": EXTERNAL_EXPERIMENT_PENDING_SCHEMA,
        "run_id": payload["run_id"],
        "source_sha256": payload["source_sha256"],
        "payload_sha256": payload_sha256,
        "applicability_sha256": payload["applicability_sha256"],
        "rows_sha256": payload["rows_sha256"],
        "projections_sha256": payload["projections_sha256"],
        "dataset_name": payload["dataset_name"],
        "dataset_id": dataset_id,
        "experiment_name": experiment_name,
        "started_at": datetime.now(UTC).isoformat(),
    }


def _experiment_page_url(
    client: Any, project_id: str | None, experiment_id: str
) -> str | None:
    base_url = getattr(client, "_base_url", None)
    if not project_id or not base_url:
        return None
    return (
        f"{str(base_url).rstrip('/')}/project/{project_id}"
        f"/experiments/results?baseline={experiment_id}"
    )


def _run_external_experiment(
    client: Any,
    *,
    payload: dict[str, Any],
    experiment_name: str,
    metadata: dict[str, Any],
    dataset_id: str,
    missing: Sequence[str] | None,
    experiment_id: str | None = None,
) -> Any:
    # 经 SDK 实验 runner 提交：hosted item 携带 dataset_id，同 dataset 同 run_name
    # 归并同一实验；本地不挂任何 evaluator，评分只来自网页 Evaluator。
    examples = _fetch_examples(client, payload["dataset_name"])
    expected = {
        row["row_id"]: _example_document(payload, row)
        for row in payload["rows"]
    }
    selected_ids = (
        [row["row_id"] for row in payload["rows"]]
        if missing is None
        else list(missing)
    )
    selected = []
    for row_id in selected_ids:
        _require(
            row_id in examples,
            f"远端数据集缺少待提交样例: {row_id}",
        )
        _verify_example_document(expected[row_id], examples[row_id])
        selected.append(examples[row_id])
    target = _FrozenCheckpointTarget(payload)
    return client.run_experiment(
        name=experiment_name,
        run_name=experiment_name,
        description=(
            "Fit-Agent 外部裁判检查点实验；target 只回放冻结回答与上下文。"
        ),
        data=selected,
        task=target,
        evaluators=[],
        max_concurrency=3,
        metadata=metadata,
        experiment_id=experiment_id,
    )


def submit_external_experiment(
    source: Path,
    *,
    dataset_dir: Path = DEFAULT_VERSION_DIR,
    confirm_cost: bool,
    client: Any | None = None,
) -> dict[str, Any]:
    load_dotenv(BACKEND / ".env", override=False)
    _require(
        confirm_cost,
        "提交外部裁判实验会触发 Langfuse 网页 Evaluator 的真实模型调用与费用，"
        "需传入 --confirm-external-judge-cost",
    )
    require_credentials()
    evidence, _, payload, ledger = _external_documents(source, dataset_dir)
    payload_path, ledger_path = _write_payload_documents(
        evidence.run_dir, payload, ledger
    )
    run_dir = evidence.run_dir
    dataset_receipt = _read_dataset_receipt(run_dir, payload)
    payload_sha256 = payload_digest(payload)
    client = client or make_client()
    dataset_id = dataset_receipt["dataset_id"]
    _require(
        dataset_receipt.get("web_judge")
        == _web_judge_configuration(payload, dataset_id),
        "数据集回执的网页裁判配置与当前投影不一致",
    )
    dataset = _verify_remote_dataset(client, payload, dataset_id)
    started_at = datetime.fromisoformat(evidence.document["started_at"])
    experiment_name = payload["experiment_name"]
    metadata = _experiment_metadata(payload, dataset_id)
    receipt_path = (
        external_dir(run_dir) / EXTERNAL_EXPERIMENT_RECEIPT_NAME
    )
    if receipt_path.is_file():
        receipt = _read_json(receipt_path)
        _external_receipt_identity(
            receipt, payload, schema=EXTERNAL_EXPERIMENT_RECEIPT_SCHEMA
        )
        _require(
            receipt.get("web_judge")
            == _web_judge_configuration(payload, dataset_id),
            "已有实验回执的网页裁判配置与当前投影不一致",
        )
        _verify_experiment_project(
            client,
            receipt["experiment_id"],
            experiment_name,
            dataset_id,
            metadata,
            from_start_time=started_at,
        )
        missing, mapping = _collect_external_runs(
            payload,
            _root_runs(
                client,
                receipt["experiment_id"],
                from_start_time=started_at,
            ),
        )
        _require(
            not missing,
            f"已有实验回执但远端运行不齐备: {missing[:3]}",
        )
        return {
            "status": "verified_existing",
            "experiment_name": receipt["experiment_name"],
            "experiment_id": receipt["experiment_id"],
            "experiment_url": receipt.get("experiment_url"),
            "dataset_name": payload["dataset_name"],
            "dataset_id": dataset_id,
            "dataset_url": dataset_receipt.get("dataset_url"),
            "rows": len(payload["rows"]),
            "runs": mapping,
            "coverage": payload["coverage"],
            "coverage_complete": receipt.get("coverage_complete"),
            "external_scoring_status": "pending_collection",
            "receipt_path": str(receipt_path),
            "payload_path": str(payload_path),
            "ledger_path": str(ledger_path),
        }
    pending_path = (
        external_dir(run_dir) / EXTERNAL_EXPERIMENT_PENDING_NAME
    )
    if pending_path.is_file():
        pending = _read_json(pending_path)
        _external_receipt_identity(
            pending, payload, schema=EXTERNAL_EXPERIMENT_PENDING_SCHEMA
        )
    remote_experiment = _find_remote_experiment(
        client, experiment_name, from_start_time=started_at
    )
    if remote_experiment is not None:
        experiment_id = str(remote_experiment.id)
        _verify_experiment_project(
            client,
            experiment_id,
            experiment_name,
            dataset_id,
            metadata,
            from_start_time=started_at,
        )
    else:
        experiment_id = None
    if not pending_path.is_file():
        _write_json(
            pending_path,
            _experiment_pending_document(
                payload, dataset_id, experiment_name, payload_sha256
            ),
        )
    if experiment_id is None:
        results = _run_external_experiment(
            client,
            payload=payload,
            experiment_name=experiment_name,
            metadata=metadata,
            dataset_id=dataset_id,
            missing=None,
        )
        experiment_id = str(results.experiment_id)
        experiment_url = results.experiment_url
        mapping = _await_external_runs(
            client,
            experiment_id,
            payload,
            from_start_time=started_at,
        )
    else:
        missing, mapping = _collect_external_runs(
            payload,
            _root_runs(
                client, experiment_id, from_start_time=started_at
            ),
        )
        if missing:
            results = _run_external_experiment(
                client,
                payload=payload,
                experiment_name=experiment_name,
                metadata=metadata,
                dataset_id=dataset_id,
                missing=missing,
                experiment_id=experiment_id,
            )
            _require(
                str(results.experiment_id) == experiment_id,
                "补齐提交后的实验身份与既有实验不一致",
            )
            mapping = _await_external_runs(
                client,
                experiment_id,
                payload,
                from_start_time=started_at,
            )
        experiment_url = _experiment_page_url(
            client, getattr(dataset, "project_id", None), experiment_id
        )
    coverage_complete = ledger["counts"]["missing"] == 0
    receipt = {
        "schema": EXTERNAL_EXPERIMENT_RECEIPT_SCHEMA,
        "presentation_version": EXTERNAL_PRESENTATION_VERSION,
        "run_id": payload["run_id"],
        "dataset": payload["dataset"],
        "source_sha256": payload["source_sha256"],
        "payload_sha256": payload_sha256,
        "applicability_sha256": payload["applicability_sha256"],
        "rows_sha256": payload["rows_sha256"],
        "projections_sha256": payload["projections_sha256"],
        "dataset_name": payload["dataset_name"],
        "dataset_id": dataset_id,
        "dataset_url": dataset_receipt.get("dataset_url"),
        "experiment_name": experiment_name,
        "experiment_id": experiment_id,
        "experiment_url": experiment_url,
        "item_ids": [row["row_id"] for row in payload["rows"]],
        "runs": mapping,
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
        "coverage_complete": coverage_complete,
        "external_scoring_status": "pending_collection",
        "web_judge": _web_judge_configuration(payload, dataset_id),
        "submitted_at": datetime.now(UTC).isoformat(),
    }
    _write_json(receipt_path, receipt)
    if pending_path.is_file():
        pending_path.unlink()
    return {
        "status": "submitted",
        "experiment_name": experiment_name,
        "experiment_id": experiment_id,
        "experiment_url": experiment_url,
        "dataset_name": payload["dataset_name"],
        "dataset_id": dataset_id,
        "dataset_url": dataset_receipt.get("dataset_url"),
        "rows": len(payload["rows"]),
        "runs": mapping,
        "coverage": payload["coverage"],
        "coverage_complete": coverage_complete,
        "external_scoring_status": "pending_collection",
        "receipt_path": str(receipt_path),
        "payload_path": str(payload_path),
        "ledger_path": str(ledger_path),
    }


def _external_command(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "external-review":
        return review_external_source(
            args.source, dataset_dir=args.dataset
        )
    if args.command == "external-project":
        return check_external_projection(
            args.source, dataset_dir=args.dataset
        )
    if args.command == "external-sync":
        return sync_external_dataset(
            args.source, dataset_dir=args.dataset
        )
    return submit_external_experiment(
        args.source,
        dataset_dir=args.dataset,
        confirm_cost=args.confirm_external_judge_cost,
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "生成评测显式上传入口；外部裁判链路使用 Langfuse，默认命令不发起网络请求"
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "upload"):
        command = commands.add_parser(name)
        command.add_argument("--source", type=Path, required=True)
        command.add_argument("--dataset", type=Path, default=None)
        command.add_argument(
            "--scope",
            choices=("auto", "formal", "validation"),
            default="auto",
        )
    for name, help_text in (
        ("external-review", "校验外部执行证据并导出适用性审阅清单"),
        ("external-project", "构造并离线校验外部检查点 Langfuse 投影"),
        ("external-sync", "同步外部检查点数据集到 Langfuse，不触发评分"),
        ("external-submit", "提交外部检查点 Langfuse 实验并触发网页 Evaluator"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--source", type=Path, required=True)
        command.add_argument(
            "--dataset", type=Path, default=DEFAULT_VERSION_DIR
        )
        if name == "external-submit":
            command.add_argument(
                "--confirm-external-judge-cost",
                action="store_true",
                help=(
                    "确认提交后会由 Langfuse 网页 Evaluator 发起真实模型"
                    "调用并产生费用"
                ),
            )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command.startswith("external-"):
        result = _external_command(args)
    elif args.command == "check":
        result = check_payload(
            args.source, version_dir=args.dataset, scope=args.scope
        )
    else:
        result = upload_payload(
            args.source, version_dir=args.dataset, scope=args.scope
        )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (UploadBlocked, SummaryError) as error:
        raise SystemExit(str(error)) from None
