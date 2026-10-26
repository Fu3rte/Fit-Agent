import argparse
import asyncio
import json
import math
import os
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from dotenv import load_dotenv
from langfuse import Evaluation, Langfuse
from langfuse.api import NotFoundError

from app.application.business.retrieval_embedding import EmbeddingClient
from app.application.business.retrieval_index import MilvusConnection, RetrievalCorpus
from app.application.business.retrieval_search import HybridRetriever
from app.model_config import load_model_config
from evaluation import retrieval_support as support
from evaluation.contracts import GENERATION_METRICS, DatasetIdentity
from evaluation.dataset import (
    BACKEND,
    DatasetError,
    EvaluationDataset,
    load_for_evaluation,
    retrieval_corpus_identity,
    scenario_baseline,
)
from evaluation.generation_scoring import (
    JudgeConfig,
    JudgeConfigError,
    load_judge_model,
)
from evaluation.scenario_evidence import canonical_json, sha256_text
from evaluation.scenarios import (
    DEFAULT_JUDGE_TIMEOUT,
    DEFAULT_VERSION_DIR,
    EXTERNAL_SCORING_FILENAME,
    JUDGE_API_KEY_VARIABLE,
    JUDGE_BASE_URL_VARIABLE,
    RAGAS_PROTOCOL_VERSION,
    ExecutionIdentityError,
    ExternalScenarioScoring,
    ScenarioArtifact,
    ScenarioBlocked,
    ScenarioExecution,
    code_identity,
    execute_scenario,
    frozen_scenario_sha256,
    generation_identity,
    load_execution,
    load_external_scoring,
    scenario_plan,
    score_execution,
    score_execution_external,
    select_scenarios,
    write_external_scoring_result,
    write_scoring_result,
)

SCHEMA = "fit-agent-scenario-langfuse-1"
FEEDBACK_VERSION = 2
LEGACY_FEEDBACK_VERSION = 1
EXTERNAL_FEEDBACK_VERSION = 3
FEEDBACK_VERSION_FIELD = "feedback_version"
ASSERTION_FEEDBACK_FIELDS = (
    "assertions_passed",
    "assertions_failed",
    "assertions_binding_errors",
    "assertions_runner_errors",
)
DATASET_MARKER = "fit-agent-scenario-sdk-langfuse-1"
# Langfuse 服务端会自动补充的数据集/样例 metadata 字段白名单沿用旧口径。
DATASET_METADATA_EXTRA = frozenset({"runtime"})
EXAMPLE_METADATA_EXTRA = frozenset({"dataset_split"})
MAX_CONCURRENCY = 6
# Langfuse 实验假定同一 dataset item 在一次实验中只出现一次；
# 该常量表达每个场景样例单次执行的口径，evaluator 评分与远端核验按此对账。
NUM_REPETITIONS = 1
JUDGE_BASE_URL = (
    "https://ws-o2404joh7zvfydxz.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
)
REMOTE_SCORE_TOLERANCE = 5e-5
REMOTE_VERIFY_TIMEOUT_SECONDS = 30.0
REMOTE_VERIFY_INTERVAL_SECONDS = 1.5
SDK_ROOT = support.EVAL_ROOT.parent / "scenario-langfuse"
DATASET_ROOT = SDK_ROOT / "datasets"
RUNS_ROOT = SDK_ROOT / "runs"
EXECUTION_NAME = "execution.json"
SCORING_NAME = "scoring.json"
SCORING_STATE_NAME = "scoring-state.json"
EVALUATION_NAME = "evaluation.json"
EXTERNAL_SCORING_NAME = EXTERNAL_SCORING_FILENAME
EXTERNAL_SCORING_STATE_NAME = "external-scoring-state.json"
EXTERNAL_EVALUATION_NAME = "external-evaluation.json"
EXTERNAL_RUN_STATUS = "awaiting_external_scoring"
# Langfuse 平台的 external 运行协议：评分语义与旧版一致，仅换平台标识。
EXTERNAL_PROTOCOL_VERSION = "fit-agent-external-scenario-langfuse-protocol-1"
SCORING_PROTOCOLS: dict[str, str] = {
    "ragas": RAGAS_PROTOCOL_VERSION,
    "external": EXTERNAL_PROTOCOL_VERSION,
}
EXTERNAL_FEEDBACK_FIELDS = (
    "execution_completed",
    "assertion_passed",
    "assertions_passed",
    "assertions_failed",
    "assertions_binding_errors",
    "assertions_runner_errors",
    "generation_scoring_completed",
)
SDK_RUN_NAME = "sdk-run.json"
REQUIRED_SCENARIOS = (
    "s-confirmation-cancellation-state-05",
    "s-confirmation-cancellation-state-07",
    "s-confirmation-cancellation-state-08",
)


class ScenarioSDKError(RuntimeError):
    pass


class ScoringBlocked(ScenarioSDKError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat()


def dataset_name(dataset: EvaluationDataset) -> str:
    return f"fit-agent-scenario-langfuse-{dataset.version}-{dataset.content_sha256[:12]}"


def make_client() -> Langfuse:
    # 模块内唯一 Client 构造点：凭据来自 LANGFUSE_* 环境变量，构造后立即校验。
    client = Langfuse()
    _require(client.auth_check(), "Langfuse 凭据校验失败")
    return client


def require_credentials() -> None:
    _require(
        bool((os.environ.get("LANGFUSE_PUBLIC_KEY") or "").strip()),
        "缺少 LANGFUSE_PUBLIC_KEY",
    )
    _require(
        bool((os.environ.get("LANGFUSE_SECRET_KEY") or "").strip()),
        "缺少 LANGFUSE_SECRET_KEY",
    )


def scoring_protocol(scoring_mode: str) -> str:
    # ragas 协议沿用 scenarios.py 原值；external 协议使用 Langfuse 平台版本。
    try:
        return SCORING_PROTOCOLS[scoring_mode]
    except KeyError as error:
        raise ScenarioSDKError(f"未知 scoring_mode: {scoring_mode!r}") from error


def feedback_version(document: Mapping) -> int:
    """无版本字段的原生记录属于旧反馈协议，未知版本拒绝解析。"""
    value = document.get(FEEDBACK_VERSION_FIELD)
    if value is None:
        return LEGACY_FEEDBACK_VERSION
    if isinstance(value, bool) or value not in (
        LEGACY_FEEDBACK_VERSION,
        FEEDBACK_VERSION,
        EXTERNAL_FEEDBACK_VERSION,
    ):
        raise ScenarioSDKError(f"feedback 协议版本不受支持: {value!r}")
    return value


def run_scoring_mode(document: Mapping) -> str:
    # 历史运行没有模式字段：按已知 Ragas 协议解释；外部版本只由显式模式或反馈版本识别。
    value = document.get("scoring_mode")
    if value is None:
        if feedback_version(document) == EXTERNAL_FEEDBACK_VERSION:
            return "external"
        return "ragas"
    if value not in ("ragas", "external"):
        raise ScenarioSDKError(f"scoring_mode 不受支持: {value!r}")
    if feedback_version(document) == EXTERNAL_FEEDBACK_VERSION:
        if value != "external":
            raise ScenarioSDKError("外部反馈版本必须绑定 external 模式")
    elif value != "ragas":
        raise ScenarioSDKError("Ragas 反馈版本不得绑定 external 模式")
    return value


def feedback_fields(version: int) -> tuple[str, ...]:
    if version == LEGACY_FEEDBACK_VERSION:
        return (*GENERATION_METRICS, "scenario_status")
    if version == FEEDBACK_VERSION:
        return (
            *GENERATION_METRICS,
            "scenario_status",
            *ASSERTION_FEEDBACK_FIELDS,
        )
    if version == EXTERNAL_FEEDBACK_VERSION:
        return EXTERNAL_FEEDBACK_FIELDS
    raise ScenarioSDKError(f"feedback 协议版本不受支持: {version!r}")


def feedback_required_fields(version: int) -> tuple[str, ...]:
    # 生成指标只在该指标存在成功结果时落库，其余字段按协议版本必须存在。
    feedback_fields(version)
    if version == LEGACY_FEEDBACK_VERSION:
        return ("scenario_status",)
    if version == EXTERNAL_FEEDBACK_VERSION:
        return EXTERNAL_FEEDBACK_FIELDS
    return ("scenario_status", *ASSERTION_FEEDBACK_FIELDS)


def feedback_field_set_matches(version: int, fields: Iterable[str]) -> bool:
    present = set(fields)
    return set(feedback_required_fields(version)) <= present <= set(
        feedback_fields(version)
    )


def assertion_statistics(
    artifact: ScenarioArtifact | ExternalScenarioScoring,
) -> dict[str, int]:
    outcomes = [
        outcome
        for checkpoint in artifact.assertions
        for outcome in checkpoint.outcomes
    ]
    return {
        "passed": sum(1 for outcome in outcomes if outcome.passed),
        "failed": sum(1 for outcome in outcomes if not outcome.passed),
        "binding_errors": sum(
            1
            for checkpoint in artifact.assertions
            if checkpoint.binding_error is not None
        ),
        "runner_errors": sum(
            1
            for checkpoint in artifact.assertions
            if checkpoint.runner_error is not None
        ),
    }


def dataset_binding(dataset: EvaluationDataset) -> dict:
    identity = dataset.identity()
    return {
        "version": identity["version"],
        "manifest_sha256": identity["manifest_sha256"],
        "content_sha256": identity["content_sha256"],
        "corpus_sha256": identity["corpus_sha256"],
    }


def dataset_identity(dataset: EvaluationDataset) -> DatasetIdentity:
    return DatasetIdentity(**dataset_binding(dataset))


def stable_example_id(name: str, scenario_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, name + ":" + scenario_id))


def _scenario_by_id(dataset: EvaluationDataset) -> dict[str, dict]:
    return {scenario["scenario_id"]: scenario for scenario in dataset.scenarios}


def _checkpoint_rows(scenario: dict) -> dict[str, list[str]]:
    plan = scenario_plan(scenario)
    return plan.generation_metrics


def example_rows(dataset: EvaluationDataset) -> list[dict]:
    name = dataset_name(dataset)
    binding = dataset_binding(dataset)
    identity = dataset.identity()
    rows = []
    for scenario in dataset.scenarios:
        scenario_id = scenario["scenario_id"]
        scenario_sha256 = frozen_scenario_sha256(scenario)
        plan = scenario_plan(scenario)
        inputs = {
            "scenario_id": scenario_id,
            "scenario_sha256": scenario_sha256,
            "dataset": binding,
        }
        outputs = {
            "scenario_id": scenario_id,
            "scenario_sha256": scenario_sha256,
            "dataset": binding,
            "event_ids": list(plan.event_ids),
            "checkpoint_ids": list(plan.checkpoint_ids),
            "generation_metrics": {
                checkpoint_id: list(metrics)
                for checkpoint_id, metrics in plan.generation_metrics.items()
            },
            "reference": [dict(row) for row in scenario["reference"]],
            "rubric": [dict(row) for row in scenario["rubric"]],
            "assertions": [dict(row) for row in scenario["assertions"]],
            "metric_applicability": [
                dict(row) for row in scenario["metric_applicability"]
            ],
        }
        applicable = sum(
            1
            for row in scenario["metric_applicability"]
            if row["metric"] in GENERATION_METRICS and row["applicable"]
        )
        metadata = {
            "scenario_id": scenario_id,
            "scenario_sha256": scenario_sha256,
            "category": scenario["tags"][0],
            "tags": list(scenario["tags"]),
            "family": scenario["family"],
            "event_ids": list(plan.event_ids),
            "checkpoint_ids": list(plan.checkpoint_ids),
            "event_count": len(plan.event_ids),
            "checkpoint_count": len(plan.checkpoint_ids),
            "generation_metrics": {
                checkpoint_id: list(metrics)
                for checkpoint_id, metrics in plan.generation_metrics.items()
            },
            "applicable_metric_count": applicable,
            "dataset_version": identity["version"],
            "manifest_sha256": identity["manifest_sha256"],
            "dataset_content_sha256": identity["content_sha256"],
            "corpus_sha256": identity["corpus_sha256"],
            "release_status": dataset.manifest["release"]["status"],
            "release_sha256": dataset.manifest["release"]["content_sha256"],
        }
        content_sha256 = sha256_text(
            canonical_json(
                {"inputs": inputs, "outputs": outputs, "metadata": metadata}
            )
        )
        rows.append(
            {
                "id": stable_example_id(name, scenario_id),
                "inputs": inputs,
                "outputs": outputs,
                "metadata": {**metadata, "content_sha256": content_sha256},
            }
        )
    return rows


def dataset_marker(dataset: EvaluationDataset) -> dict:
    identity = dataset.identity()
    return {
        "marker": DATASET_MARKER,
        "version": identity["version"],
        "manifest_sha256": identity["manifest_sha256"],
        "content_sha256": identity["content_sha256"],
        "corpus_sha256": identity["corpus_sha256"],
        "release": {
            "status": dataset.manifest["release"]["status"],
            "content_sha256": dataset.manifest["release"]["content_sha256"],
        },
        "example_count": len(dataset.scenarios),
    }


def receipt_path(name: str) -> Path:
    return DATASET_ROOT / f"{name}.json"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ScenarioSDKError(message)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _verify_metadata(
    actual: Any, expected: dict, allowed_extra: frozenset[str], label: str
) -> None:
    _require(isinstance(actual, dict), f"远端 {label} 必须为对象")
    unknown = set(actual) - set(expected) - allowed_extra
    _require(not unknown, f"远端 {label} 包含未知字段: {sorted(unknown)[:3]}")
    _require(set(expected) <= set(actual), f"远端 {label} 缺少字段")
    for key, value in expected.items():
        _require(actual[key] == value, f"远端 {label} 字段不符: {key}")


def _verify_example(row: dict, example: Any) -> None:
    _require(str(example.id) == row["id"], f"远端样例 ID 不符: {row['id']}")
    _require(example.input == row["inputs"], f"远端样例 inputs 不符: {row['id']}")
    _require(
        example.expected_output == row["outputs"],
        f"远端样例 expected_output 不符: {row['id']}",
    )
    _verify_metadata(
        example.metadata,
        row["metadata"],
        EXAMPLE_METADATA_EXTRA,
        f"样例 metadata: {row['id']}",
    )


def _fetch_examples(client: Any, dataset_name: str) -> dict[str, Any]:
    return {
        str(example.id): example
        for example in client.get_dataset(dataset_name).items
    }


def _resolve_dataset(client: Any, name: str, marker: dict) -> tuple[Any, str]:
    path = receipt_path(name)
    if path.is_file():
        receipt = _load_json(path)
        if receipt.get("dataset_name") == name:
            remote = _find_dataset(client, name)
            if remote is not None and remote.name == name:
                _verify_metadata(
                    remote.metadata, marker, DATASET_METADATA_EXTRA, "数据集 metadata"
                )
                return remote, "reused"
    remote = _find_dataset(client, name)
    if remote is not None:
        _require(
            remote.name == name, "远端同名数据集解析失败，拒绝继续"
        )
        _verify_metadata(
            remote.metadata, marker, DATASET_METADATA_EXTRA, "数据集 metadata"
        )
        return remote, "reused"
    client.create_dataset(
        name=name,
        description="Fit-Agent 批准场景集；稳定样例 ID 绑定数据版本与场景摘要。",
        metadata=marker,
    )
    remote = client.get_dataset(name)
    _require(remote.name == name, "创建后未解析到唯一数据集")
    _verify_metadata(
        remote.metadata, marker, DATASET_METADATA_EXTRA, "数据集 metadata"
    )
    return remote, "created"


def _find_dataset(client: Any, name: str) -> Any | None:
    # Langfuse 数据集按项目内唯一名称解析；缺失时 SDK 抛 NotFoundError。
    try:
        return client.get_dataset(name)
    except NotFoundError:
        return None


def sync_dataset(client: Any, dataset: EvaluationDataset) -> dict:
    name = dataset_name(dataset)
    rows = example_rows(dataset)
    expected = {row["id"]: row for row in rows}
    marker = dataset_marker(dataset)
    remote, dataset_status = _resolve_dataset(client, name, marker)
    dataset_id = str(remote.id)
    existing = _fetch_examples(client, name)
    unexpected = sorted(set(existing) - set(expected))
    _require(not unexpected, f"远端数据集包含未知样例: {unexpected[:3]}")
    for example_id, example in existing.items():
        _verify_example(expected[example_id], example)
    missing = [row for row in rows if row["id"] not in existing]
    for row in missing:
        # 显式传 id 依赖 Langfuse 的按 id upsert 幂等语义。
        client.create_dataset_item(
            dataset_name=name,
            id=row["id"],
            input=row["inputs"],
            expected_output=row["outputs"],
            metadata=row["metadata"],
        )
    remote_examples = _fetch_examples(client, name)
    _require(set(remote_examples) == set(expected), "远端样例集合与批准场景集不一致")
    for example_id, example in remote_examples.items():
        _verify_example(expected[example_id], example)
    receipt = {
        "schema": SCHEMA,
        "dataset_name": name,
        "dataset_id": dataset_id,
        "dataset_status": dataset_status,
        "dataset": dataset.identity(),
        "marker": marker,
        "example_ids": {
            row["inputs"]["scenario_id"]: row["id"] for row in rows
        },
        "rows_sha256": sha256_text(canonical_json(rows)),
        "synced_at": now(),
    }
    _write_json(receipt_path(name), receipt)
    return receipt


def selection_records(dataset: EvaluationDataset) -> list[dict]:
    scenarios = sorted(dataset.scenarios, key=lambda item: item["scenario_id"])
    chosen: dict[str, str] = {}

    def take(scenario: dict, rule: str) -> None:
        chosen.setdefault(scenario["scenario_id"], rule)

    fixed = {scenario["scenario_id"]: scenario for scenario in scenarios}
    for scenario_id in REQUIRED_SCENARIOS:
        _require(scenario_id in fixed, f"缺少固定验收场景: {scenario_id}")
        take(fixed[scenario_id], "固定验收场景")

    def simple_qa(scenario: dict) -> bool:
        if [event["type"] for event in scenario["events"]] != ["message"]:
            return False
        if scenario["events"][0]["attachments"] or scenario["input"]["attachments"]:
            return False
        kinds = {row["kind"] for row in scenario["assertions"]}
        if kinds & {
            "save_authorization",
            "snapshot_integrity",
            "saved_state",
            "state_transition",
        }:
            return False
        return all(
            row.get("evidence_kind") not in {"tool_result", "ranking", "snapshot"}
            for row in scenario["assertions"]
            if row["kind"] == "evidence_present"
        )

    def retrieval(scenario: dict) -> bool:
        kinds = {
            row.get("evidence_kind")
            for row in scenario["assertions"]
            if row["kind"] == "evidence_present"
        }
        if not kinds & {"tool_result", "ranking"}:
            return False
        if {event["type"] for event in scenario["events"]} != {"message"}:
            return False
        return not any(
            row["kind"]
            in {"save_authorization", "snapshot_integrity", "state_transition"}
            for row in scenario["assertions"]
        )

    def confirmation(scenario: dict) -> bool:
        if not any(event["type"] == "confirm" for event in scenario["events"]):
            return False
        save = [
            row for row in scenario["assertions"]
            if row["kind"] == "save_authorization"
        ]
        return any(row.get("expected_saved") is True for row in save)

    for predicate, rule in (
        (simple_qa, "稳定 ID 顺序首条单轮无工具简单问答"),
        (retrieval, "稳定 ID 顺序首条需要生产检索且无保存事件的场景"),
        (confirmation, "稳定 ID 顺序首条包含真实确认保存事件的场景"),
    ):
        matches = [scenario for scenario in scenarios if predicate(scenario)]
        _require(bool(matches), f"缺少职责场景: {rule}")
        take(next(iter(matches)), rule)

    records = []
    for scenario in scenarios:
        scenario_id = scenario["scenario_id"]
        if scenario_id not in chosen:
            continue
        plan = scenario_plan(scenario)
        records.append(
            {
                "scenario_id": scenario_id,
                "rule": chosen[scenario_id],
                "event_count": len(plan.event_ids),
                "checkpoint_count": len(plan.checkpoint_ids),
                "generation_metrics": {
                    checkpoint_id: list(metrics)
                    for checkpoint_id, metrics in plan.generation_metrics.items()
                },
                "applicable_metric_count": sum(
                    1
                    for row in scenario["metric_applicability"]
                    if row["metric"] in GENERATION_METRICS and row["applicable"]
                ),
            }
        )
    _require(len(records) == 6, "定向验收必须选择 6 个场景")
    return records


def select_validation_scenarios(
    dataset: EvaluationDataset, scenario_ids: list[str] | None = None
) -> tuple[list[dict], list[dict]]:
    records = selection_records(dataset)
    chosen = [record["scenario_id"] for record in records]
    if scenario_ids is not None:
        _require(
            set(scenario_ids) == set(chosen),
            "定向验收场景必须与固定选择一致",
        )
    scenarios = select_scenarios(dataset, chosen)
    _require(len(scenarios) == 6, "定向验收必须选择 6 个场景")
    return scenarios, records


def select_formal_scenarios(dataset: EvaluationDataset) -> list[dict]:
    scenarios = select_scenarios(dataset, None)
    expected = scenario_baseline(dataset.version)[0]
    _require(len(scenarios) == expected, f"正式运行必须覆盖 {expected} 个批准场景")
    return scenarios


def judge_config(environ: Mapping[str, str] | None = None) -> JudgeConfig:
    source = os.environ if environ is None else environ
    try:
        model = load_judge_model(source)
        base_url = (source.get(JUDGE_BASE_URL_VARIABLE) or "").strip()
        _require(base_url == JUDGE_BASE_URL, "裁判服务地址与批准配置不一致")
        api_key = (source.get(JUDGE_API_KEY_VARIABLE) or "").strip()
        _require(bool(api_key), f"缺少裁判凭据: {JUDGE_API_KEY_VARIABLE}")
        return JudgeConfig(model=model, base_url=base_url, api_key=api_key)
    except JudgeConfigError as error:
        raise ScenarioSDKError("裁判模型配置无效") from error


async def preflight_index(dataset: EvaluationDataset) -> dict:
    corpus = RetrievalCorpus.load()
    _require(
        dataset.corpus_sha256 == retrieval_corpus_identity(corpus),
        "评测语料快照与真实目录不一致",
    )
    async with MilvusConnection.from_env() as db, EmbeddingClient.from_env() as embedding:
        retriever = await HybridRetriever.open(db, corpus, embedding)
        collection, manifest_json = retriever.validation_identity()
    manifest = json.loads(manifest_json)
    return {
        "collection": collection,
        "manifest_sha256": sha256_text(manifest_json),
        "build_id": manifest.get("build_id"),
        "publisher": manifest.get("publisher"),
    }


def resolve_inputs(
    inputs: Any,
    dataset: EvaluationDataset,
    rows: dict[str, dict],
) -> tuple[dict, dict]:
    _require(isinstance(inputs, Mapping), "target inputs 必须为对象")
    _require(
        set(inputs) == {"scenario_id", "scenario_sha256", "dataset"},
        "target inputs 包含未声明字段",
    )
    scenario_id = inputs.get("scenario_id")
    _require(
        isinstance(scenario_id, str) and bool(scenario_id),
        "target inputs 缺少 scenario_id",
    )
    _require(scenario_id in rows, f"target inputs 引用未知场景: {scenario_id}")
    row = rows[scenario_id]
    _require(
        dataset_binding(dataset) == inputs.get("dataset"),
        f"target inputs 数据身份不符: {scenario_id}",
    )
    scenario = next(
        item
        for item in dataset.scenarios
        if item["scenario_id"] == scenario_id
    )
    _require(
        frozen_scenario_sha256(scenario) == inputs.get("scenario_sha256"),
        f"场景摘要与本地批准内容不一致: {scenario_id}",
    )
    _require(
        dict(inputs) == row["inputs"],
        f"target inputs 与批准样例不一致: {scenario_id}",
    )
    return scenario, row


def expected_execution_path(example_id: str, scenario_id: str) -> PurePosixPath:
    return PurePosixPath("scenarios") / example_id / scenario_id / EXECUTION_NAME


def _safe_relative(value: Any, label: str) -> PurePosixPath:
    _require(isinstance(value, str) and bool(value), f"{label} 缺少相对引用")
    path = PurePosixPath(value)
    _require(not path.is_absolute(), f"{label} 不得使用绝对路径")
    _require(".." not in path.parts, f"{label} 不得越出受控根目录")
    return path


@dataclass
class ScenarioConcurrencyRecorder:
    in_flight: int = 0
    max_in_flight: int = 0
    calls: int = 0
    intervals: list[dict] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def start(self, example_id: str, scenario_id: str) -> int:
        async with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.calls += 1
        return time.monotonic_ns()

    async def finish(
        self, example_id: str, scenario_id: str, started: int, status: str
    ) -> None:
        finished = time.monotonic_ns()
        async with self._lock:
            self.in_flight -= 1
            self.intervals.append(
                {
                    "example_id": example_id,
                    "scenario_id": scenario_id,
                    "start_ns": started,
                    "end_ns": finished,
                    "status": status,
                }
            )

    def overlap_pairs(self) -> int:
        ordered = sorted(self.intervals, key=lambda item: item["start_ns"])
        pairs = 0
        for index, row in enumerate(ordered):
            for other in ordered[index + 1 :]:
                if other["start_ns"] < row["end_ns"] and other["end_ns"] > row["start_ns"]:
                    pairs += 1
        return pairs

    def report(self) -> dict:
        return {
            "calls": self.calls,
            "max_in_flight": self.max_in_flight,
            "overlap_pairs": self.overlap_pairs(),
            "intervals": list(self.intervals),
        }


def execution_summary(
    execution: ScenarioExecution,
    *,
    run_id: str,
    example_id: str,
    scenario_id: str,
    dataset: EvaluationDataset,
) -> dict:
    relative = expected_execution_path(example_id, scenario_id)
    return {
        "run_id": run_id,
        "example_id": example_id,
        "scenario_id": scenario_id,
        "scenario_sha256": execution.scenario_sha256,
        "execution_sha256": execution.execution_sha256,
        "status": execution.status,
        "dataset": dataset_binding(dataset),
        "evidence": {
            "execution": str(relative),
            "scenario_root": str(relative.parent),
        },
    }


class ScenarioTarget:
    def __init__(
        self,
        *,
        dataset: EvaluationDataset,
        rows: dict[str, dict],
        run_id: str,
        run_dir: Path,
    ) -> None:
        self.dataset = dataset
        self.rows = rows
        self.run_id = run_id
        self.run_dir = run_dir
        self.recorder = ScenarioConcurrencyRecorder()

    async def __call__(self, *, item: Any) -> dict:
        # Langfuse runner 以 item 关键字调用 task：hosted item 取 .input 字段。
        inputs = item["input"] if isinstance(item, Mapping) else item.input
        scenario, row = resolve_inputs(inputs, self.dataset, self.rows)
        scenario_id = scenario["scenario_id"]
        example_id = row["id"]
        started = await self.recorder.start(example_id, scenario_id)
        status = "failed"
        try:
            try:
                execution = await execute_scenario(
                    self.dataset,
                    scenario,
                    run_id=self.run_id,
                    root=self.run_dir / "scenarios" / example_id,
                    enable_model=True,
                )
            except ScenarioBlocked as error:
                execution = error.execution
                _require(execution is not None, f"场景阻断缺少执行结果: {scenario_id}")
                status = execution.status
                return execution_summary(
                    execution,
                    run_id=self.run_id,
                    example_id=example_id,
                    scenario_id=scenario_id,
                    dataset=self.dataset,
                )
            status = execution.status
            return execution_summary(
                execution,
                run_id=self.run_id,
                example_id=example_id,
                scenario_id=scenario_id,
                dataset=self.dataset,
            )
        except ScenarioSDKError:
            raise
        except Exception as error:
            status = "failed"
            raise ScenarioSDKError(
                f"{type(error).__name__}: 场景执行失败（{scenario_id}），证据保留在本地运行目录"
            ) from error
        finally:
            await self.recorder.finish(example_id, scenario_id, started, status)


def scenario_feedback(
    execution: ScenarioExecution,
    artifact: ScenarioArtifact,
    *,
    version: int = FEEDBACK_VERSION,
) -> dict[str, float]:
    if version == EXTERNAL_FEEDBACK_VERSION:
        raise ScenarioSDKError("外部协议必须使用 external_scenario_feedback")
    feedback_fields(version)
    feedback: dict[str, float] = {}
    for metric in GENERATION_METRICS:
        scores = [
            float(outcome.score)
            for checkpoint in artifact.generation
            if checkpoint.result is not None
            for outcome in checkpoint.result.outcomes
            if outcome.metric == metric and outcome.status == "succeeded"
        ]
        if scores:
            feedback[metric] = sum(scores) / len(scores)
    complete = (
        execution.status == "completed"
        and artifact.blocked is None
        and artifact.runner_error is None
    )
    feedback["scenario_status"] = 1.0 if complete else 0.0
    if version == FEEDBACK_VERSION:
        for key, value in assertion_statistics(artifact).items():
            feedback[f"assertions_{key}"] = float(value)
    return feedback


def external_scenario_feedback(
    record: ExternalScenarioScoring,
) -> dict[str, float]:
    # 只表达执行与确定性断言的真实状态；生成评分在外部模式下始终未完成。
    statistics = assertion_statistics(record)
    return {
        "execution_completed": (
            1.0 if record.execution_status == "completed" else 0.0
        ),
        "assertion_passed": 1.0 if record.assertion_status == "passed" else 0.0,
        "assertions_passed": float(statistics["passed"]),
        "assertions_failed": float(statistics["failed"]),
        "assertions_binding_errors": float(statistics["binding_errors"]),
        "assertions_runner_errors": float(statistics["runner_errors"]),
        "generation_scoring_completed": 0.0,
    }


def external_incomplete_items(record: ExternalScenarioScoring) -> list[str]:
    return list(record.incomplete)


def incomplete_items(
    artifact: ScenarioArtifact,
    *,
    version: int = FEEDBACK_VERSION,
) -> list[str]:
    if version == EXTERNAL_FEEDBACK_VERSION:
        raise ScenarioSDKError("外部协议必须使用 external_incomplete_items")
    feedback_fields(version)
    items: list[str] = []
    for checkpoint in artifact.assertions:
        if checkpoint.binding_error is not None:
            items.append(f"{checkpoint.checkpoint_id}:binding_error")
        if checkpoint.runner_error is not None:
            items.append(f"{checkpoint.checkpoint_id}:runner_error")
        if version == FEEDBACK_VERSION:
            for index, outcome in enumerate(checkpoint.outcomes):
                if not outcome.passed:
                    items.append(
                        f"{checkpoint.event_id}/{checkpoint.checkpoint_id}"
                        f":assertion_failed:{outcome.kind}:{index}"
                    )
    for checkpoint in artifact.generation:
        if checkpoint.runner_error is not None:
            items.append(f"{checkpoint.checkpoint_id}:scoring_error")
        if checkpoint.result is not None:
            for outcome in checkpoint.result.outcomes:
                if outcome.status == "failed":
                    items.append(f"{checkpoint.checkpoint_id}/{outcome.metric}:failed")
                elif outcome.status == "applicable":
                    items.append(
                        f"{checkpoint.checkpoint_id}/{outcome.metric}:applicable"
                    )
    actual = {
        (checkpoint.checkpoint_id, outcome.metric)
        for checkpoint in artifact.generation
        if checkpoint.result is not None
        for outcome in checkpoint.result.outcomes
    }
    for checkpoint_id, metrics in artifact.generation_metrics.items():
        for metric in metrics:
            if (checkpoint_id, metric) not in actual:
                items.append(f"{checkpoint_id}/{metric}:missing")
    return items


def checkpoint_records(artifact: ScenarioArtifact) -> dict:
    records: dict[str, dict] = {}
    for checkpoint in artifact.generation:
        entry = records.setdefault(
            checkpoint.checkpoint_id, {"event_id": checkpoint.event_id, "metrics": {}}
        )
        if checkpoint.result is not None:
            for outcome in checkpoint.result.outcomes:
                entry["metrics"][outcome.metric] = {
                    "status": outcome.status,
                    "score": outcome.score,
                    "error": outcome.error,
                    "reason": outcome.reason,
                }
        entry["attempts"] = [item.model_dump(mode="json") for item in checkpoint.attempts]
        entry["incomplete_metrics"] = list(checkpoint.incomplete_metrics)
        entry["runner_error"] = checkpoint.runner_error
    return records


def verify_existing_scoring(
    execution: ScenarioExecution, path: Path
) -> ScenarioArtifact:
    _require(path.is_file(), "评分产物缺失")
    try:
        artifact = ScenarioArtifact.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except Exception as error:
        raise ScoringBlocked(
            f"已有评分产物无法解析: {type(error).__name__}"
        ) from error
    _require(
        artifact.run_id == execution.run_id
        and artifact.scenario_id == execution.scenario_id
        and artifact.session_ids == execution.session_ids
        and [event.event_id for event in artifact.events]
        == [event.event_id for event in execution.events],
        "已有评分产物身份与执行结果不一致",
    )
    return artifact


def verify_existing_external_scoring(
    execution: ScenarioExecution, path: Path
) -> ExternalScenarioScoring:
    _require(path.is_file(), "外部评分产物缺失")
    try:
        record = load_external_scoring(
            path, expected_execution_sha256=execution.execution_sha256
        )
    except Exception as error:
        raise ScoringBlocked(
            f"已有外部评分产物无法解析: {type(error).__name__}"
        ) from error
    _require(
        record.run_id == execution.run_id
        and record.scenario_id == execution.scenario_id,
        "已有外部评分产物身份与执行结果不一致",
    )
    return record


def _claim_state(path: Path, payload: dict) -> None:
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError as error:
        raise ScoringBlocked(
            "评分状态已存在，拒绝重复评分或隐式重评"
        ) from error
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, allow_nan=False)


def scoring_guard(
    scoring_path: Path,
    state_path: Path,
    *,
    scoring_mode: str = "ragas",
) -> tuple[bool, str]:
    state = _load_json(state_path) if state_path.is_file() else None
    if state is not None:
        recorded_mode = state.get("scoring_mode")
        if recorded_mode is None and scoring_mode != "ragas":
            raise ScoringBlocked("评分状态缺少模式身份，禁止跨模式复用")
        if recorded_mode is not None and recorded_mode != scoring_mode:
            raise ScoringBlocked("评分状态属于其他模式，禁止复用")
        status = state.get("status")
        if status == "completed":
            _require(scoring_path.is_file(), "评分状态为 completed 但评分产物缺失")
            return True, "state_completed"
        if status == "running":
            raise ScoringBlocked("评分正在进行，禁止重复评分")
        raise ScoringBlocked(f"评分状态为 {status}，禁止同一执行隐式重评")
    if scoring_path.is_file():
        return True, "artifact_identity_only"
    return False, "initial"


def _evaluator_execution(
    *,
    inputs: Any,
    outputs: Any,
    reference_outputs: Any,
    metadata: Any,
    rows: dict[str, dict],
    run_id: str,
    binding: dict,
    expected_dataset: Any,
    run_dir: Path,
) -> tuple[dict, ScenarioExecution, PurePosixPath, Path]:
    _require(isinstance(inputs, Mapping), "evaluator inputs 必须为对象")
    scenario_id = inputs.get("scenario_id")
    _require(
        isinstance(scenario_id, str) and scenario_id in rows,
        "evaluator inputs 引用未知场景",
    )
    row = rows[scenario_id]
    _require(dict(inputs) == row["inputs"], "evaluator inputs 与批准样例不一致")
    _require(
        isinstance(reference_outputs, Mapping)
        and dict(reference_outputs) == row["outputs"],
        "evaluator expected_output 与批准样例不一致",
    )
    _require(
        metadata == row["metadata"],
        f"evaluator metadata 与批准样例不一致: {scenario_id}",
    )
    _require(isinstance(outputs, Mapping), "evaluator outputs 必须为对象")
    _require(
        outputs.get("run_id") == run_id, "target 输出 run_id 不一致"
    )
    _require(
        outputs.get("scenario_id") == scenario_id,
        "target 输出 scenario_id 不一致",
    )
    _require(
        outputs.get("scenario_sha256") == row["inputs"]["scenario_sha256"],
        "target 输出场景摘要不一致",
    )
    _require(
        outputs.get("dataset") == binding, "target 输出数据身份不一致"
    )
    _require(
        outputs.get("status") in {"completed", "blocked", "failed"},
        "target 输出状态非法",
    )
    evidence = outputs.get("evidence")
    _require(isinstance(evidence, Mapping), "target 输出缺少证据引用")
    relative = _safe_relative(evidence.get("execution"), "execution")
    expected = expected_execution_path(row["id"], scenario_id)
    _require(relative == expected, "execution 引用与批准目录不一致")
    path = (run_dir / relative).resolve()
    _require(path.is_relative_to(run_dir.resolve()), "execution 路径越界")
    _require(path.is_file(), "execution 文件不存在")
    try:
        execution = load_execution(
            path,
            expected_run_id=run_id,
            expected_scenario_id=scenario_id,
            expected_dataset=expected_dataset,
            expected_scenario_sha256=row["inputs"]["scenario_sha256"],
        )
    except ExecutionIdentityError as error:
        raise ScenarioSDKError(
            f"执行结果身份校验失败（{scenario_id}）"
        ) from error
    _require(
        execution.execution_sha256 == outputs.get("execution_sha256"),
        "execution_sha256 与 target 输出不一致",
    )
    _require(
        execution.status == outputs.get("status"),
        "target 输出状态与执行结果不一致",
    )
    return row, execution, relative, path.parent


def make_evaluator(
    *,
    dataset: EvaluationDataset,
    rows: dict[str, dict],
    run_id: str,
    run_dir: Path,
    judge: JudgeConfig | None,
    judge_timeout: float = DEFAULT_JUDGE_TIMEOUT,
    scoring_mode: str = "ragas",
):
    _require(scoring_mode in ("ragas", "external"), "evaluator scoring_mode 非法")
    binding = dataset_binding(dataset)
    expected_dataset = dataset_identity(dataset)

    # Langfuse runner 以关键字调用 evaluator：input/output/expected_output/metadata。
    async def evaluator(*, input, output, expected_output, metadata):
        row, execution, relative, scenario_root = _evaluator_execution(
            inputs=input,
            outputs=output,
            reference_outputs=expected_output,
            metadata=metadata,
            rows=rows,
            run_id=run_id,
            binding=binding,
            expected_dataset=expected_dataset,
            run_dir=run_dir,
        )
        scenario_id = execution.scenario_id
        if scoring_mode == "external":
            scoring_path = scenario_root / EXTERNAL_SCORING_NAME
            state_path = scenario_root / EXTERNAL_SCORING_STATE_NAME
            evaluation_path = scenario_root / EXTERNAL_EVALUATION_NAME
            reused, binding_mode = scoring_guard(
                scoring_path, state_path, scoring_mode="external"
            )
            if reused:
                if binding_mode == "state_completed":
                    state = _load_json(state_path)
                    _require(
                        state.get("execution_sha256")
                        == execution.execution_sha256,
                        "已有评分状态与当前执行摘要不一致",
                    )
                record = verify_existing_external_scoring(
                    execution, scoring_path
                )
            else:
                _claim_state(
                    state_path,
                    {
                        "status": "running",
                        "scoring_mode": "external",
                        "protocol_version": EXTERNAL_PROTOCOL_VERSION,
                        "run_id": run_id,
                        "scenario_id": scenario_id,
                        "execution_sha256": execution.execution_sha256,
                        "started_at": now(),
                    },
                )
                try:
                    record = score_execution_external(
                        execution, row["outputs"]["assertions"]
                    )
                    write_external_scoring_result(
                        execution, record, path=scoring_path
                    )
                except BaseException as error:
                    _write_json(
                        state_path,
                        {
                            "status": "failed",
                            "scoring_mode": "external",
                            "run_id": run_id,
                            "scenario_id": scenario_id,
                            "execution_sha256": execution.execution_sha256,
                            "error_type": type(error).__name__,
                            "error": str(error)[:2000],
                            "finished_at": now(),
                        },
                    )
                    raise ScenarioSDKError(
                        f"{type(error).__name__}: 外部场景评分失败（{scenario_id}）"
                    ) from error
                _write_json(
                    state_path,
                    {
                        "status": "completed",
                        "scoring_mode": "external",
                        "protocol_version": EXTERNAL_PROTOCOL_VERSION,
                        "run_id": run_id,
                        "scenario_id": scenario_id,
                        "execution_sha256": execution.execution_sha256,
                        "scoring_sha256": sha256_text(
                            scoring_path.read_text(encoding="utf-8")
                        ),
                        "finished_at": now(),
                    },
                )
            feedback = external_scenario_feedback(record)
            evaluation = {
                "schema": SCHEMA,
                "feedback_version": EXTERNAL_FEEDBACK_VERSION,
                "scoring_mode": "external",
                "protocol_version": EXTERNAL_PROTOCOL_VERSION,
                "run_id": run_id,
                "example_id": row["id"],
                "scenario_id": scenario_id,
                "execution_sha256": execution.execution_sha256,
                "execution_relative": str(relative),
                "scoring_relative": str(
                    PurePosixPath(str(relative)).parent
                    / EXTERNAL_SCORING_NAME
                ),
                "scoring_sha256": sha256_text(
                    scoring_path.read_text(encoding="utf-8")
                ),
                "reused": reused,
                "scoring_binding": binding_mode,
                "execution_status": record.execution_status,
                "assertion_status": record.assertion_status,
                "generation_scoring_status": record.generation_scoring_status,
                "applicability_status": record.applicability_status,
                "feedback": feedback,
                "generation": [
                    item.model_dump(mode="json") for item in record.generation
                ],
                "incomplete": external_incomplete_items(record),
                "written_at": now(),
            }
            if evaluation_path.is_file():
                existing = _load_json(evaluation_path)
                _require(
                    existing.get("scoring_sha256")
                    == evaluation["scoring_sha256"],
                    "已有外部评分摘要与当前评分产物不一致",
                )
            _write_json(evaluation_path, evaluation)
            return [
                Evaluation(name=key, value=value)
                for key, value in feedback.items()
            ]
        scoring_path = scenario_root / SCORING_NAME
        state_path = scenario_root / SCORING_STATE_NAME
        evaluation_path = scenario_root / EVALUATION_NAME
        reused, binding_mode = scoring_guard(
            scoring_path, state_path, scoring_mode="ragas"
        )
        if reused:
            if binding_mode == "state_completed":
                state = _load_json(state_path)
                _require(
                    state.get("execution_sha256") == execution.execution_sha256,
                    "已有评分状态与当前执行摘要不一致",
                )
            artifact = verify_existing_scoring(execution, scoring_path)
        else:
            _claim_state(
                state_path,
                {
                    "status": "running",
                    "scoring_mode": "ragas",
                    "run_id": run_id,
                    "scenario_id": scenario_id,
                    "execution_sha256": execution.execution_sha256,
                    "started_at": now(),
                },
            )
            try:
                artifact = await score_execution(
                    execution, judge=judge, judge_timeout=judge_timeout
                )
                write_scoring_result(execution, artifact, path=scoring_path)
            except BaseException as error:
                _write_json(
                    state_path,
                    {
                        "status": "failed",
                        "scoring_mode": "ragas",
                        "run_id": run_id,
                        "scenario_id": scenario_id,
                        "execution_sha256": execution.execution_sha256,
                        "error_type": type(error).__name__,
                        "error": str(error)[:2000],
                        "finished_at": now(),
                    },
                )
                raise ScenarioSDKError(
                    f"{type(error).__name__}: 场景评分失败（{scenario_id}），证据保留在本地运行目录"
                ) from error
            _write_json(
                state_path,
                {
                    "status": "completed",
                    "scoring_mode": "ragas",
                    "run_id": run_id,
                    "scenario_id": scenario_id,
                    "execution_sha256": execution.execution_sha256,
                    "scoring_sha256": sha256_text(
                        scoring_path.read_text(encoding="utf-8")
                    ),
                    "finished_at": now(),
                },
            )
        feedback = scenario_feedback(execution, artifact)
        evaluation = {
            "schema": SCHEMA,
            "feedback_version": FEEDBACK_VERSION,
            "scoring_mode": "ragas",
            "protocol_version": RAGAS_PROTOCOL_VERSION,
            "run_id": run_id,
            "example_id": row["id"],
            "scenario_id": scenario_id,
            "execution_sha256": execution.execution_sha256,
            "execution_relative": str(relative),
            "scoring_relative": str(
                PurePosixPath(str(relative)).parent / SCORING_NAME
            ),
            "scoring_sha256": sha256_text(scoring_path.read_text(encoding="utf-8")),
            "reused": reused,
            "scoring_binding": binding_mode,
            "execution_status": execution.status,
            "feedback": feedback,
            "checkpoints": checkpoint_records(artifact),
            "incomplete": incomplete_items(artifact),
            "written_at": now(),
        }
        if evaluation_path.is_file():
            existing = _load_json(evaluation_path)
            _require(
                existing.get("scoring_sha256") == evaluation["scoring_sha256"],
                "已有评分摘要与当前评分产物不一致",
            )
        _write_json(evaluation_path, evaluation)
        return [
            Evaluation(name=key, value=value)
            for key, value in feedback.items()
        ]

    return evaluator


def experiment_metadata(
    *,
    run_id: str,
    purpose: str,
    dataset: EvaluationDataset,
    selections: list[dict],
    model_identity: dict,
    judge: JudgeConfig | None,
    index_identity: dict,
    code: dict,
    scoring_mode: str = "ragas",
) -> dict:
    _require(scoring_mode in ("ragas", "external"), "experiment scoring_mode 非法")
    if scoring_mode == "external":
        feedback_version = EXTERNAL_FEEDBACK_VERSION
        judge_metadata = {
            # 裁判由 Langfuse 外部管理；具体 Evaluator 身份由后续上传与回收证据记录。
            "ownership": "langfuse",
            "configuration": "external",
        }
    else:
        feedback_version = FEEDBACK_VERSION
        _require(judge is not None, "Ragas 模式缺少裁判配置")
        judge_metadata = {
            "model": judge.model,
            "endpoint_sha256": sha256_text(judge.base_url),
        }
    return {
        "local_run_id": run_id,
        "purpose": purpose,
        "feedback_version": feedback_version,
        "scoring_mode": scoring_mode,
        "protocol_version": scoring_protocol(scoring_mode),
        "dataset": dataset_binding(dataset),
        "dataset_name": dataset_name(dataset),
        "code_identity": {
            "commit": code["commit"],
            "dirty": code["dirty"],
            "lock_sha256": code["lock_sha256"],
            "source_sha256": code["source_sha256"],
        },
        "sdk_module_sha256": sha256_text(Path(__file__).read_text(encoding="utf-8")),
        "generation": model_identity,
        "judge": judge_metadata,
        "index": index_identity,
        "max_concurrency": MAX_CONCURRENCY,
        "num_repetitions": NUM_REPETITIONS,
        "blocking": True,
        "sample_scope": {
            "scenario_ids": [row["scenario_id"] for row in selections],
            "count": len(selections),
            "rule": (
                "固定取消/重启场景 3 条，另按稳定 ID 顺序选择简单问答、生产检索与真实确认保存各 1 条"
                if purpose == "validation"
                else f"批准全量 {scenario_baseline(dataset.version)[0]} 个场景"
            ),
            "selection": selections,
        },
    }


def _list_experiments(
    client: Any, *, experiment_id: str, from_start_time: datetime, fields: str
) -> list[Any]:
    experiments = []
    cursor = None
    while True:
        response = client.api.experiments.list(
            from_start_time=from_start_time,
            id=experiment_id,
            fields=fields,
            cursor=cursor,
        )
        experiments.extend(response.data)
        cursor = response.meta.cursor
        if not cursor:
            return experiments


def verify_experiment_metadata(
    client: Any,
    experiment_id: str,
    expected: dict,
    *,
    from_start_time: datetime,
) -> None:
    matches = [
        experiment
        for experiment in _list_experiments(
            client,
            experiment_id=experiment_id,
            from_start_time=from_start_time,
            fields="metadata",
        )
        if experiment.id == experiment_id
    ]
    _require(len(matches) == 1, "远端实验不存在或身份不唯一")
    remote = matches[0].metadata or {}
    for key in (
        "local_run_id",
        "purpose",
        "feedback_version",
        "scoring_mode",
        "protocol_version",
        "dataset",
        "code_identity",
        "sdk_module_sha256",
        "generation",
        "judge",
        "index",
        "max_concurrency",
        "num_repetitions",
        "sample_scope",
    ):
        _require(
            remote.get(key) == expected[key],
            f"远端实验 metadata 不符: {key}",
        )


def _list_experiment_items(
    client: Any, *, experiment_id: str, from_start_time: datetime
) -> list[Any]:
    items = []
    cursor = None
    while True:
        response = client.api.experiments.list_items(
            from_start_time=from_start_time,
            experiment_id=experiment_id,
            fields="io",
            cursor=cursor,
        )
        items.extend(response.data)
        cursor = response.meta.cursor
        if not cursor:
            return items


def _list_experiment_scores(client: Any, *, experiment_id: str) -> list[Any]:
    scores = []
    cursor = None
    while True:
        response = client.api.scores.get_many(
            experiment_id=experiment_id,
            fields="details,subject",
            limit=100,
            cursor=cursor,
        )
        scores.extend(response.data)
        cursor = response.meta.cursor
        if not cursor:
            return scores


def _score_trace_id(score: Any) -> str | None:
    # evaluator 分数挂在 item 执行 trace 上；无法归属 trace 的分数按意外反馈处理。
    subject = getattr(score, "subject", None)
    kind = getattr(subject, "kind", None)
    if kind == "trace":
        return subject.id
    if kind == "observation":
        return subject.trace_id
    return None


def _trace_feedback(
    by_trace: dict[str, list[Any]], trace_id: str
) -> tuple[dict[str, float], int, int]:
    # 返回数值反馈、非数值分数个数与分数总行数，供样本级比较逐项判定。
    rows = by_trace.get(trace_id, [])
    feedback: dict[str, float] = {}
    non_numeric = 0
    for score in rows:
        value = score.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            non_numeric += 1
            continue
        feedback[score.name] = float(value)
    return feedback, non_numeric, len(rows)


def verify_runs(
    client: Any,
    *,
    experiment_id: str,
    examples: list[Any],
    rows_by_example: dict[str, dict],
    run_dir: Path,
    scoring_mode: str = "ragas",
    from_start_time: datetime | None = None,
) -> tuple[list[dict], dict]:
    _require(from_start_time is not None, "远端核验缺少 from_start_time")
    items = _list_experiment_items(
        client, experiment_id=experiment_id, from_start_time=from_start_time
    )
    scores = _list_experiment_scores(client, experiment_id=experiment_id)
    by_example: dict[str, list[Any]] = {}
    for item in items:
        _require(
            item.experiment_item_id is not None,
            f"实验条目缺少样例身份: {item.id}",
        )
        by_example.setdefault(str(item.experiment_item_id), []).append(item)
    by_trace: dict[str, list[Any]] = {}
    unmapped: list[str] = []
    for score in scores:
        trace_id = _score_trace_id(score)
        if trace_id is None:
            unmapped.append(str(score.name))
            continue
        by_trace.setdefault(trace_id, []).append(score)
    if scoring_mode == "external":
        return _verify_external_runs(
            items,
            by_example,
            by_trace,
            unmapped,
            examples=examples,
            rows_by_example=rows_by_example,
            run_dir=run_dir,
        )
    return _verify_ragas_runs(
        items,
        by_example,
        by_trace,
        unmapped,
        examples=examples,
        rows_by_example=rows_by_example,
        run_dir=run_dir,
    )


def _run_sample_error(item: Any) -> str | None:
    if item.level == "ERROR":
        output = item.output
        if isinstance(output, str):
            return output
        return f"experiment item {item.id} marked ERROR"
    return None


def _run_times(item: Any) -> tuple[str | None, str | None]:
    start = item.start_time.isoformat() if item.start_time else None
    end = item.end_time.isoformat() if item.end_time else None
    return start, end


def _verify_ragas_runs(
    items: list[Any],
    by_example: dict[str, list[Any]],
    by_trace: dict[str, list[Any]],
    unmapped_scores: list[str],
    *,
    examples: list[Any],
    rows_by_example: dict[str, dict],
    run_dir: Path,
) -> tuple[list[dict], dict]:
    samples = []
    for example in examples:
        example_id = str(example.id)
        row = rows_by_example[example_id]
        scenario_id = row["inputs"]["scenario_id"]
        matched = by_example.get(example_id, [])
        if len(matched) != NUM_REPETITIONS:
            samples.append(
                {
                    "example_id": example_id,
                    "scenario_id": scenario_id,
                    "status": "missing" if not matched else "duplicate_runs",
                    "run_ids": [str(item.trace_id) for item in matched],
                    "error": f"期望 {NUM_REPETITIONS} 次运行，实际 {len(matched)} 次",
                }
            )
            continue
        item = matched[0]
        trace_id = str(item.trace_id)
        scenario_root = run_dir / "scenarios" / example_id / scenario_id
        evaluation_path = scenario_root / EVALUATION_NAME
        start_time, end_time = _run_times(item)
        sample = {
            "example_id": example_id,
            "scenario_id": scenario_id,
            "sdk_run_id": trace_id,
            "trace_id": trace_id,
            "feedback_version": FEEDBACK_VERSION,
            "run_error": _run_sample_error(item),
            "run_start_time": start_time,
            "run_end_time": end_time,
        }
        if sample["run_error"]:
            sample["status"] = "failed"
            execution_path = scenario_root / EXECUTION_NAME
            if execution_path.is_file():
                execution = load_execution(execution_path)
                sample["execution_status"] = execution.status
                sample["execution_sha256"] = execution.execution_sha256
            samples.append(sample)
            continue
        if not evaluation_path.is_file():
            sample["status"] = "failed"
            sample["error"] = "缺少本地评分结果"
            samples.append(sample)
            continue
        evaluation = _load_json(evaluation_path)
        version = feedback_version(evaluation)
        expected_feedback = {
            key: float(value) for key, value in evaluation["feedback"].items()
        }
        try:
            _require(
                feedback_field_set_matches(version, expected_feedback),
                "本地反馈字段集合与协议版本不一致",
            )
        except ScenarioSDKError as error:
            sample["status"] = "failed"
            sample["error"] = str(error)
            samples.append(sample)
            continue
        feedback, non_numeric, score_rows = _trace_feedback(by_trace, trace_id)
        sample.update(
            {
                "execution_sha256": evaluation["execution_sha256"],
                "execution_status": evaluation["execution_status"],
                "scoring_relative": evaluation["scoring_relative"],
                "scoring_sha256": evaluation["scoring_sha256"],
                "scoring_reused": evaluation["reused"],
                "feedback_version": version,
                "incomplete": list(evaluation["incomplete"]),
                "feedback": feedback,
                "expected_feedback": expected_feedback,
                "outputs": item.output,
            }
        )
        try:
            _require(non_numeric == 0, "远端存在非数值反馈")
            _require(
                len(feedback) == score_rows, "反馈指标重复"
            )
            _require(
                set(feedback) == set(expected_feedback),
                "远端反馈指标集合与本地评分不一致",
            )
            differences = {
                key: abs(feedback[key] - expected_feedback[key])
                for key in feedback
            }
            _require(
                all(
                    math.isclose(
                        feedback[key],
                        expected_feedback[key],
                        rel_tol=0.0,
                        abs_tol=REMOTE_SCORE_TOLERANCE,
                    )
                    for key in feedback
                ),
                "远端反馈与本地评分不一致",
            )
            _require(
                isinstance(item.output, Mapping),
                "运行缺少 outputs",
            )
            _require(
                item.output.get("execution_sha256")
                == evaluation["execution_sha256"],
                "远端运行 execution_sha256 与本地不一致",
            )
            _require(
                item.output.get("scenario_id") == scenario_id,
                "远端运行 scenario_id 与本地不一致",
            )
            sample["status"] = "complete"
            sample["max_feedback_difference"] = max(
                differences.values(), default=0.0
            )
        except ScenarioSDKError as error:
            sample["status"] = "failed"
            sample["error"] = str(error)
        samples.append(sample)
    extra = sorted(set(by_example) - {str(example.id) for example in examples})
    status = (
        "complete"
        if not extra
        and not unmapped_scores
        and all(item["status"] == "complete" for item in samples)
        else "incomplete"
    )
    return samples, {
        "status": status,
        "expected_runs": len(examples),
        "observed_runs": len(items),
        "extra_example_ids": extra,
        "unmapped_scores": sorted(unmapped_scores),
    }


def _verify_external_runs(
    items: list[Any],
    by_example: dict[str, list[Any]],
    by_trace: dict[str, list[Any]],
    unmapped_scores: list[str],
    *,
    examples: list[Any],
    rows_by_example: dict[str, dict],
    run_dir: Path,
) -> tuple[list[dict], dict]:
    samples = []
    for example in examples:
        example_id = str(example.id)
        row = rows_by_example[example_id]
        scenario_id = row["inputs"]["scenario_id"]
        matched = by_example.get(example_id, [])
        if len(matched) != NUM_REPETITIONS:
            samples.append(
                {
                    "example_id": example_id,
                    "scenario_id": scenario_id,
                    "status": "missing" if not matched else "duplicate_runs",
                    "run_ids": [str(item.trace_id) for item in matched],
                    "error": f"期望 {NUM_REPETITIONS} 次运行，实际 {len(matched)} 次",
                }
            )
            continue
        item = matched[0]
        trace_id = str(item.trace_id)
        scenario_root = run_dir / "scenarios" / example_id / scenario_id
        evaluation_path = scenario_root / EXTERNAL_EVALUATION_NAME
        start_time, end_time = _run_times(item)
        sample = {
            "example_id": example_id,
            "scenario_id": scenario_id,
            "sdk_run_id": trace_id,
            "trace_id": trace_id,
            "feedback_version": EXTERNAL_FEEDBACK_VERSION,
            "scoring_mode": "external",
            "run_error": _run_sample_error(item),
            "run_start_time": start_time,
            "run_end_time": end_time,
        }
        if sample["run_error"]:
            sample["status"] = "failed"
            execution_path = scenario_root / EXECUTION_NAME
            if execution_path.is_file():
                execution = load_execution(execution_path)
                sample["execution_status"] = execution.status
                sample["execution_sha256"] = execution.execution_sha256
            samples.append(sample)
            continue
        if not evaluation_path.is_file():
            sample["status"] = "failed"
            sample["error"] = "缺少本地外部评分结果"
            samples.append(sample)
            continue
        evaluation = _load_json(evaluation_path)
        try:
            _require(
                run_scoring_mode(evaluation) == "external",
                "本地评分结果不是外部模式",
            )
            version = feedback_version(evaluation)
            _require(
                version == EXTERNAL_FEEDBACK_VERSION,
                "本地外部评分反馈版本不受支持",
            )
            expected_feedback = {
                key: float(value)
                for key, value in evaluation["feedback"].items()
            }
            _require(
                feedback_field_set_matches(version, expected_feedback),
                "本地外部反馈字段集合与协议版本不一致",
            )
        except (ScenarioSDKError, KeyError, TypeError) as error:
            sample["status"] = "failed"
            sample["error"] = str(error)
            samples.append(sample)
            continue
        feedback, non_numeric, score_rows = _trace_feedback(by_trace, trace_id)
        sample.update(
            {
                "execution_sha256": evaluation["execution_sha256"],
                "execution_status": evaluation["execution_status"],
                "assertion_status": evaluation["assertion_status"],
                "generation_scoring_status": evaluation[
                    "generation_scoring_status"
                ],
                "scoring_relative": evaluation["scoring_relative"],
                "scoring_sha256": evaluation["scoring_sha256"],
                "scoring_reused": evaluation["reused"],
                "feedback_version": version,
                "incomplete": list(evaluation["incomplete"]),
                "feedback": feedback,
                "expected_feedback": expected_feedback,
                "outputs": item.output,
            }
        )
        try:
            _require(non_numeric == 0, "远端存在非数值反馈")
            _require(
                len(feedback) == score_rows, "反馈指标重复"
            )
            _require(
                set(feedback) == set(expected_feedback),
                "远端反馈指标集合与本地外部评分不一致",
            )
            differences = {
                key: abs(feedback[key] - expected_feedback[key])
                for key in feedback
            }
            _require(
                all(
                    math.isclose(
                        feedback[key],
                        expected_feedback[key],
                        rel_tol=0.0,
                        abs_tol=REMOTE_SCORE_TOLERANCE,
                    )
                    for key in feedback
                ),
                "远端反馈与本地外部评分不一致",
            )
            _require(
                isinstance(item.output, Mapping),
                "运行缺少 outputs",
            )
            _require(
                item.output.get("execution_sha256")
                == evaluation["execution_sha256"],
                "远端运行 execution_sha256 与本地不一致",
            )
            _require(
                item.output.get("scenario_id") == scenario_id,
                "远端运行 scenario_id 与本地不一致",
            )
            sample["status"] = "complete"
            sample["max_feedback_difference"] = max(
                differences.values(), default=0.0
            )
        except ScenarioSDKError as error:
            sample["status"] = "failed"
            sample["error"] = str(error)
        samples.append(sample)
    extra = sorted(set(by_example) - {str(example.id) for example in examples})
    status = (
        "complete"
        if not extra
        and not unmapped_scores
        and all(item["status"] == "complete" for item in samples)
        else "incomplete"
    )
    return samples, {
        "status": status,
        "expected_runs": len(examples),
        "observed_runs": len(items),
        "extra_example_ids": extra,
        "unmapped_scores": sorted(unmapped_scores),
    }


async def verify_runs_until_complete(client: Any, **kwargs) -> tuple[list[dict], dict]:
    deadline = time.monotonic() + REMOTE_VERIFY_TIMEOUT_SECONDS
    while True:
        samples, coverage = verify_runs(client, **kwargs)
        if coverage["status"] == "complete" or time.monotonic() >= deadline:
            return samples, coverage
        await asyncio.sleep(REMOTE_VERIFY_INTERVAL_SECONDS)


def _write_group_evidence(run_dir: Path, samples: list[dict]) -> None:
    for sample in samples:
        _write_json(
            run_dir / "samples" / f"{sample['example_id']}.json", sample
        )


def _run_status(
    coverage: dict,
    samples: list[dict],
    issues: list[str],
    scoring_mode: str = "ragas",
) -> str:
    if issues or coverage["status"] != "complete":
        return "incomplete"
    if any(item.get("execution_status") != "completed" for item in samples):
        return "incomplete"
    if any(item.get("incomplete") for item in samples):
        return "incomplete"
    if any(
        (item.get("feedback") or {}).get("assertions_failed", 0) > 0
        for item in samples
    ):
        return "incomplete"
    if scoring_mode == "external":
        if any(
            (item.get("feedback") or {}).get("generation_scoring_completed", 0)
            > 0
            for item in samples
        ):
            return "incomplete"
        if any(
            item.get("generation_scoring_status") != "pending_external"
            for item in samples
        ):
            return "incomplete"
        return EXTERNAL_RUN_STATUS
    return "complete"


def _finalize_sample(
    run_dir: Path,
    sample: dict,
    run_id: str | None,
    scoring_mode: str = "ragas",
) -> tuple[dict, list[str] | None]:
    # 评分引用先核验再参与状态重算；原始 feedback 与 incomplete 保持记录原样。
    scoring = sample.get("scoring_relative")
    current_incomplete = None
    if isinstance(scoring, str):
        path = (run_dir / score_relative_path(scoring)).resolve()
        _require(path.is_relative_to(run_dir.resolve()), "评分路径越界")
        _require(path.is_file(), "评分产物缺失")
        _require(
            sha256_text(path.read_text(encoding="utf-8"))
            == sample.get("scoring_sha256"),
            "评分摘要与样本不一致",
        )
        evaluation_name = (
            EXTERNAL_EVALUATION_NAME if scoring_mode == "external" else EVALUATION_NAME
        )
        evaluation_path = path.parent / evaluation_name
        _require(evaluation_path.is_file(), "评分摘要缺失")
        evaluation = _load_json(evaluation_path)
        _require(
            evaluation.get("scoring_sha256") == sample.get("scoring_sha256"),
            "评分摘要记录的评分身份与样本不一致",
        )
        _require(
            evaluation.get("execution_sha256") == sample.get("execution_sha256"),
            "评分摘要记录的执行身份与样本不一致",
        )
        execution_path = path.parent / EXECUTION_NAME
        _require(execution_path.is_file(), "执行结果缺失")
        execution = load_execution(
            execution_path,
            expected_run_id=run_id,
            expected_scenario_id=sample.get("scenario_id"),
        )
        _require(
            execution.execution_sha256 == sample.get("execution_sha256"),
            "执行摘要与样本不一致",
        )
        _require(
            execution.status == sample.get("execution_status"),
            "执行状态与样本不一致",
        )
        if scoring_mode == "external":
            record = verify_existing_external_scoring(execution, path)
            current_incomplete = external_incomplete_items(record)
        else:
            artifact = verify_existing_scoring(execution, path)
            current_incomplete = incomplete_items(artifact)
    return (
        scenario_record(run_dir, sample, scoring_mode=scoring_mode),
        current_incomplete,
    )


def finalize_run(run_dir: Path) -> dict:
    # 只重算本地运行摘要，不触发任何模型、裁判或 Langfuse 调用。
    document = _load_json(run_dir / SDK_RUN_NAME)
    _require(
        document.get("schema") == SCHEMA, "运行摘要版本不受支持"
    )
    scoring_mode = run_scoring_mode(document)
    version = feedback_version(document)
    samples = document.get("samples") or []
    coverage = document.get("coverage") or {}
    _require(bool(samples), "运行摘要缺少样本")
    _require(bool(coverage), "运行摘要缺少覆盖结果")
    for sample in samples:
        _require(
            feedback_version(sample) == version,
            "样本反馈协议版本与运行摘要不一致",
        )
        if scoring_mode == "external":
            _require(
                run_scoring_mode(sample) == "external",
                "样本评分模式与运行摘要不一致",
            )
    records = []
    status_samples = []
    for sample in samples:
        record, current_incomplete = _finalize_sample(
            run_dir, sample, document.get("run_id"), scoring_mode
        )
        records.append(record)
        if current_incomplete is None:
            status_samples.append(sample)
        else:
            status_samples.append({**sample, "incomplete": current_incomplete})
    document["scoring_mode"] = scoring_mode
    document["protocol_version"] = scoring_protocol(scoring_mode)
    document["status"] = _run_status(
        coverage, status_samples, document.get("issues") or [], scoring_mode
    )
    document["scenarios"] = records
    document.pop("error_type", None)
    document.pop("error", None)
    document["finalized_at"] = now()
    _write_json(run_dir / SDK_RUN_NAME, document)
    return document


async def run_experiment(args) -> dict:
    load_dotenv(BACKEND / ".env", override=False)
    scoring_mode = getattr(args, "scoring_mode", "external")
    _require(scoring_mode in ("ragas", "external"), "scoring_mode 非法")
    dataset = load_for_evaluation(Path(args.dataset))
    if args.purpose == "validation":
        scenarios, selections = select_validation_scenarios(
            dataset, args.scenario_id
        )
    else:
        _require(not args.scenario_id, "正式运行不接受场景子集")
        scenarios = select_formal_scenarios(dataset)
        formal_rule = f"批准全量 {scenario_baseline(dataset.version)[0]} 个场景"
        selections = [
            {
                "scenario_id": scenario["scenario_id"],
                "rule": formal_rule,
            }
            for scenario in scenarios
        ]
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    run_dir = RUNS_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    document: dict[str, Any] = {
        "schema": SCHEMA,
        "run_id": run_id,
        "purpose": args.purpose,
        "feedback_version": (
            EXTERNAL_FEEDBACK_VERSION
            if scoring_mode == "external"
            else FEEDBACK_VERSION
        ),
        "scoring_mode": scoring_mode,
        "protocol_version": scoring_protocol(scoring_mode),
        "status": "running",
        "started_at": now(),
        "dataset": dataset.identity(),
        "dataset_name": dataset_name(dataset),
        "max_concurrency": MAX_CONCURRENCY,
        "num_repetitions": NUM_REPETITIONS,
        "sample_scope": {
            "scenario_ids": [row["scenario_id"] for row in selections],
            "count": len(selections),
            "selection": selections,
        },
        "scenarios": [],
        "samples": [],
        "coverage": {},
        "issues": [],
    }
    _write_json(run_dir / SDK_RUN_NAME, document)
    execution_started = False
    try:
        require_credentials()
        # 外部模式的裁判由 Langfuse 管理，本地不读取任何裁判配置。
        judge = judge_config() if scoring_mode == "ragas" else None
        model = load_model_config()
        model_identity = generation_identity(model)
        index_identity = await preflight_index(dataset)
        code = code_identity()
        client = make_client()
        receipt = sync_dataset(client, dataset)
        document.update(
            dataset_id=receipt["dataset_id"],
            example_ids=receipt["example_ids"],
            code_identity={
                "commit": code["commit"],
                "dirty": code["dirty"],
                "lock_sha256": code["lock_sha256"],
                "source_sha256": code["source_sha256"],
            },
            generation=model_identity,
            judge=(
                {
                    "ownership": "langfuse",
                    "configuration": "external",
                }
                if judge is None
                else {
                    "model": judge.model,
                    "endpoint_sha256": sha256_text(judge.base_url),
                }
            ),
            index=index_identity,
        )
        rows_list = example_rows(dataset)
        rows = rows_by_scenario(rows_list)
        rows_by_example = {row["id"]: row for row in rows_list}
        selected_ids = [
            receipt["example_ids"][item["scenario_id"]] for item in scenarios
        ]
        remote_examples = _fetch_examples(client, receipt["dataset_name"])
        examples = []
        for example_id in selected_ids:
            _require(
                example_id in remote_examples,
                f"远端缺少验收样例: {example_id}",
            )
            _verify_example(
                rows_by_example[example_id], remote_examples[example_id]
            )
            examples.append(remote_examples[example_id])
        metadata = experiment_metadata(
            run_id=run_id,
            purpose=args.purpose,
            dataset=dataset,
            selections=selections,
            model_identity=model_identity,
            judge=judge,
            index_identity=index_identity,
            code=code,
            scoring_mode=scoring_mode,
        )
        _write_json(run_dir / SDK_RUN_NAME, document)
        target = ScenarioTarget(
            dataset=dataset,
            rows=rows,
            run_id=run_id,
            run_dir=run_dir,
        )
        evaluator = make_evaluator(
            dataset=dataset,
            rows=rows,
            run_id=run_id,
            run_dir=run_dir,
            judge=judge,
            judge_timeout=args.judge_timeout,
            scoring_mode=scoring_mode,
        )
        execution_started = True
        experiment_name = f"{run_id}-{args.purpose}"
        results = client.run_experiment(
            name=experiment_name,
            run_name=experiment_name,
            description=f"Fit-Agent 场景评测（{args.purpose}）",
            data=examples,
            task=target.__call__,
            evaluators=[evaluator],
            max_concurrency=MAX_CONCURRENCY,
            metadata=metadata,
        )
        experiment_id = str(results.experiment_id)
        document["experiment"] = {
            "id": experiment_id,
            "name": results.run_name,
            "url": results.experiment_url,
        }
        _write_json(run_dir / SDK_RUN_NAME, document)
        samples, coverage = await verify_runs_until_complete(
            client,
            experiment_id=experiment_id,
            examples=examples,
            rows_by_example=rows_by_example,
            run_dir=run_dir,
            scoring_mode=scoring_mode,
            from_start_time=datetime.fromisoformat(document["started_at"]),
        )
        issues: list[str] = []
        try:
            verify_experiment_metadata(
                client,
                experiment_id,
                metadata,
                from_start_time=datetime.fromisoformat(document["started_at"]),
            )
        except ScenarioSDKError as error:
            issues.append(str(error))
        concurrency = target.recorder.report()
        _require(
            concurrency["max_in_flight"] <= MAX_CONCURRENCY,
            f"在途 target 超过并发上限: {concurrency['max_in_flight']}",
        )
        document.update(
            {
                "finished_at": now(),
                "status": _run_status(
                    coverage, samples, issues, scoring_mode
                ),
                "concurrency": concurrency,
                "coverage": coverage,
                "samples": samples,
                "scenarios": [
                    scenario_record(
                        run_dir, sample, scoring_mode=scoring_mode
                    )
                    for sample in samples
                ],
                "issues": issues,
            }
        )
        _write_json(run_dir / SDK_RUN_NAME, document)
        _write_group_evidence(run_dir, samples)
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "artifact": str(
                        (run_dir / SDK_RUN_NAME).relative_to(BACKEND)
                    ),
                    "status": document["status"],
                    "experiment_id": experiment_id,
                    "experiment_url": results.experiment_url,
                    "coverage": coverage,
                    "concurrency": {
                        key: concurrency[key]
                        for key in ("calls", "max_in_flight", "overlap_pairs")
                    },
                    "samples": [
                        {
                            "scenario_id": sample["scenario_id"],
                            "status": sample["status"],
                            "execution_status": sample.get("execution_status"),
                            "assertion_status": sample.get("assertion_status"),
                            "generation_scoring_status": sample.get(
                                "generation_scoring_status"
                            ),
                            "feedback": sample.get("feedback"),
                            "incomplete": sample.get("incomplete"),
                            "error": sample.get("error")
                            or sample.get("run_error"),
                        }
                        for sample in samples
                    ],
                },
                ensure_ascii=False,
            )
        )
        expected_status = (
            EXTERNAL_RUN_STATUS if scoring_mode == "external" else "complete"
        )
        if document["status"] != expected_status:
            raise ScenarioSDKError(
                "场景定向验收存在失败或不完整项，证据已保留在 "
                + str(run_dir.relative_to(BACKEND))
            )
        return document
    except BaseException as error:
        # 已完成汇总的 incomplete 结果保留自身状态；异常只标记尚未终结的运行。
        if document.get("status") == "running":
            document.update(
                finished_at=now(),
                status="failed" if execution_started else "blocked",
                error_type=type(error).__name__,
                error=str(error)[:2000],
            )
            _write_json(run_dir / SDK_RUN_NAME, document)
        raise


def rows_by_scenario(rows: list[dict]) -> dict[str, dict]:
    return {row["inputs"]["scenario_id"]: row for row in rows}


def scenario_record(
    run_dir: Path, sample: dict, scoring_mode: str = "ragas"
) -> dict:
    record = {
        "example_id": sample["example_id"],
        "scenario_id": sample["scenario_id"],
        "scoring_mode": scoring_mode,
        "status": sample.get("status"),
        "execution_status": sample.get("execution_status"),
        "execution_sha256": sample.get("execution_sha256"),
        "scoring_relative": sample.get("scoring_relative"),
        "scoring_sha256": sample.get("scoring_sha256"),
        "scoring_reused": sample.get("scoring_reused"),
        "feedback": sample.get("feedback"),
        "expected_feedback": sample.get("expected_feedback"),
        "incomplete": sample.get("incomplete", []),
        "sdk_run_id": sample.get("sdk_run_id"),
        "run_error": sample.get("run_error"),
        "error": sample.get("error"),
    }
    if scoring_mode == "external":
        record["assertion_status"] = sample.get("assertion_status")
        record["generation_scoring_status"] = sample.get(
            "generation_scoring_status"
        )
    if "feedback_version" in sample:
        record["feedback_version"] = sample["feedback_version"]
    scoring = sample.get("scoring_relative")
    if isinstance(scoring, str):
        path = (run_dir / score_relative_path(scoring)).resolve()
        if path.is_relative_to(run_dir.resolve()) and path.is_file():
            evaluation_name = (
                EXTERNAL_EVALUATION_NAME
                if scoring_mode == "external"
                else EVALUATION_NAME
            )
            evaluation = _load_json(path.parent / evaluation_name)
            record["scoring_binding"] = evaluation.get("scoring_binding") or (
                "initial" if evaluation.get("reused") is False else ""
            )
            if scoring_mode == "external":
                record_file = ExternalScenarioScoring.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
                record["assertions"] = assertion_statistics(record_file)
                record["generation"] = evaluation.get("generation", [])
            else:
                record["checkpoints"] = evaluation.get("checkpoints", {})
                artifact = ScenarioArtifact.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
                record["assertions"] = assertion_statistics(artifact)
    return record


def score_relative_path(value: str) -> PurePosixPath:
    return PurePosixPath(value)


def sync_command(args) -> None:
    load_dotenv(BACKEND / ".env", override=False)
    require_credentials()
    dataset = load_for_evaluation(Path(args.dataset))
    client = make_client()
    receipt = sync_dataset(client, dataset)
    print(
        json.dumps(
            {
                "dataset_name": receipt["dataset_name"],
                "dataset_id": receipt["dataset_id"],
                "dataset_status": receipt["dataset_status"],
                "example_count": len(receipt["example_ids"]),
                "receipt": str(receipt_path(receipt["dataset_name"]).relative_to(BACKEND)),
            },
            ensure_ascii=False,
        )
    )


def finalize_command(args) -> None:
    run_dir = Path(args.run).resolve()
    document = finalize_run(run_dir)
    artifact = run_dir / SDK_RUN_NAME
    print(
        json.dumps(
            {
                "run_id": document["run_id"],
                "status": document["status"],
                "coverage": document["coverage"],
                "artifact": str(artifact.relative_to(BACKEND))
                if artifact.is_relative_to(BACKEND)
                else str(artifact),
            },
            ensure_ascii=False,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Langfuse 场景评测入口")
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync", help="同步批准场景集到 Langfuse")
    sync.add_argument("--dataset", type=Path, default=DEFAULT_VERSION_DIR)
    run = commands.add_parser("run", help="按场景执行 Langfuse SDK 实验")
    run.add_argument("--dataset", type=Path, default=DEFAULT_VERSION_DIR)
    run.add_argument(
        "--purpose", choices=("validation", "formal"), default="validation"
    )
    run.add_argument("--scenario-id", action="append", default=None)
    run.add_argument("--judge-timeout", type=float, default=DEFAULT_JUDGE_TIMEOUT)
    run.add_argument(
        "--scoring-mode",
        choices=("ragas", "external"),
        default="external",
        help="external 只执行确定性断言并等待 Langfuse 外部评分（默认）；"
        "ragas 使用本地 Ragas 裁判，需显式指定",
    )
    finalize = commands.add_parser(
        "finalize", help="从本地样本重算运行摘要状态，不发起外部调用"
    )
    finalize.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "sync":
        sync_command(args)
        return
    if args.command == "finalize":
        finalize_command(args)
        return
    asyncio.run(run_experiment(args))


if __name__ == "__main__":
    try:
        main()
    except DatasetError as exc:
        raise SystemExit(f"评测数据未就绪: {exc}") from None
    except ScenarioSDKError as exc:
        raise SystemExit(f"场景评测被阻止: {exc}") from None
