import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence
from uuid import NAMESPACE_URL, uuid5

from langsmith import Client
from langsmith.utils import LangSmithNotFoundError

from evaluation.contracts import GENERATION_METRICS
from evaluation.scenario_evidence import sha256_text
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


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="生成评测显式上传入口；默认命令不发起网络请求"
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
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "check":
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
