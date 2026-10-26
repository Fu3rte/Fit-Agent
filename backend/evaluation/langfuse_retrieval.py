import argparse
import asyncio
import json
import math
import os
import threading
import time
from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from langfuse import Evaluation, Langfuse
from langfuse.api.commons.types.observation_level import ObservationLevel
from langfuse.api.core.api_error import ApiError

from app.application.business import retrieval_text as rt
from app.application.business.retrieval_embedding import EmbeddingClient
from app.application.business.retrieval_frozen import frozen_values
from app.application.business.retrieval_index import (
    MilvusConnection,
    RetrievalCorpus,
    json_text,
)
from app.application.business.retrieval_rerank import RerankClient, RerankIdentity
from app.application.business.retrieval_search import HybridRetriever
from evaluation import retrieval_support as support
from evaluation.contracts import RETRIEVAL_METRICS, RelevanceLabel, RetrievalInput
from evaluation.dataset import (
    DatasetError,
    EvaluationDataset,
    load_for_evaluation,
    retrieval_corpus_identity,
    unreviewed_candidates,
)
from evaluation.retrieval import identity as code_identity
from evaluation.retrieval import validate_projections
from evaluation.retrieval_metrics import evaluate, require

SCHEMA = "fit-agent-retrieval-langfuse-1"
PENDING_SCHEMA = "fit-agent-retrieval-langfuse-pending-1"
DATASET_MARKER = "fit-agent-retrieval-sdk-langfuse-1"
DATASET_METADATA_EXTRA = frozenset()
EXAMPLE_METADATA_EXTRA = frozenset()
GROUPS = ("bm25", "dense", "hybrid")
RERANK_GROUP = "hybrid-rerank"
RUN_GROUPS = (*GROUPS, RERANK_GROUP)
PURPOSES = ("lookup", "generation", "replacement")
MAX_CONCURRENCY = 6
NUM_REPETITIONS = 1
REMOTE_SCORE_TOLERANCE = 5e-5
REMOTE_VERIFY_TIMEOUT_SECONDS = 30.0
REMOTE_VERIFY_POLL_SECONDS = 1.5
SDK_ROOT = support.EVAL_ROOT.parent / "retrieval-langfuse"
DATASET_ROOT = SDK_ROOT / "datasets"
EXPERIMENT_METADATA_KEYS = (
    "local_run_id",
    "group",
    "purpose",
    "dataset",
    "parameters",
    "code_identity",
    "index_collection",
    "index_manifest_sha256",
    "vector_cache_identity_sha256",
    "max_concurrency",
    "num_repetitions",
    "sample_scope",
    "experiment",
    "rerank_identity",
)


def now() -> str:
    return datetime.now(UTC).isoformat()


def unreviewed_in_pool(
    reviewed: Mapping[str, Collection[str]],
    query_id: str,
    candidate_pool: Sequence[str],
) -> list[str]:
    return unreviewed_candidates(reviewed, {query_id: candidate_pool}).get(query_id, [])


def review_evidence(
    reviewed: Mapping[str, Collection[str]],
    query_id: str,
    candidate_pool: Sequence[str],
) -> dict:
    missing = unreviewed_in_pool(reviewed, query_id, candidate_pool)
    return {
        "status": "blocked_unreviewed_candidates" if missing else "complete",
        "query_id": query_id,
        "unreviewed_candidates": missing,
    }


def require_reviewed_candidates(
    reviewed: Mapping[str, Collection[str]],
    query_id: str,
    candidate_pool: Sequence[str],
) -> None:
    missing = unreviewed_in_pool(reviewed, query_id, candidate_pool)
    if missing:
        raise support.EvaluationBlocked(
            f"{query_id} 存在未审阅候选: {', '.join(missing)}",
            tuple(missing),
        )


def dataset_name(dataset: EvaluationDataset) -> str:
    return f"fit-agent-retrieval-sdk-langfuse-{dataset.version}-{dataset.content_sha256[:12]}"


def dataset_binding(dataset: EvaluationDataset) -> dict:
    identity = dataset.identity()
    return {
        "version": identity["version"],
        "manifest_sha256": identity["manifest_sha256"],
        "content_sha256": identity["content_sha256"],
        "corpus_sha256": identity["corpus_sha256"],
    }


def sdk_code_identity() -> dict:
    identity = code_identity()
    files = dict(identity["files"])
    files["evaluation/langfuse_retrieval.py"] = support.sha256(Path(__file__))
    return {**identity, "files": files}


def stable_example_id(name: str, query_id: str) -> str:
    # 数据集名称携带 langfuse 标识与版本/内容摘要，样例 ID 随数据身份稳定派生。
    return str(uuid5(NAMESPACE_URL, name + ":" + query_id))


def _labels(dataset: EvaluationDataset, query_id: str) -> list[dict]:
    rows = [
        {"exercise_id": item["corpus_id"], "grade": item["grade"]}
        for item in dataset.qrels
        if item["query_id"] == query_id
    ]
    return sorted(rows, key=lambda row: row["exercise_id"])


def example_rows(dataset: EvaluationDataset) -> list[dict]:
    name = dataset_name(dataset)
    identity = dataset.identity()
    binding = dataset_binding(dataset)
    rows = []
    for query in dataset.queries:
        query_id = query["query_id"]
        inputs = {
            "query_id": query_id,
            "query": query["query"],
            "purpose": query["purpose"],
            "conditions": query["conditions"],
            "dataset": binding,
        }
        outputs = {
            "query_id": query_id,
            "scenario_id": query["scenario_id"],
            "expect_empty": query["expect_empty"],
            "labels": _labels(dataset, query_id),
        }
        metadata = {
            "query_id": query_id,
            "scenario_id": query["scenario_id"],
            "purpose": query["purpose"],
            "tags": list(query["tags"]),
            "family": query["family"],
            "expect_empty": query["expect_empty"],
            "label_count": len(outputs["labels"]),
            "dataset_version": identity["version"],
            "manifest_sha256": identity["manifest_sha256"],
            "dataset_content_sha256": identity["content_sha256"],
            "corpus_sha256": identity["corpus_sha256"],
        }
        content_sha256 = support.sha256_text(
            json_text({"inputs": inputs, "outputs": outputs, "metadata": metadata})
        )
        rows.append(
            {
                "id": stable_example_id(name, query_id),
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
        "example_count": len(dataset.queries),
    }


def receipt_path(name: str) -> Path:
    return DATASET_ROOT / f"{name}.json"


def _verify_metadata(
    actual: Any, expected: dict, allowed_extra: frozenset[str], label: str
) -> None:
    require(isinstance(actual, dict), f"远端 {label} 必须为对象")
    require(
        not set(actual) - set(expected) - allowed_extra,
        f"远端 {label} 包含未知字段: {sorted(set(actual) - set(expected) - allowed_extra)[:3]}",
    )
    require(set(expected) <= set(actual), f"远端 {label} 缺少字段")
    for key, value in expected.items():
        require(actual[key] == value, f"远端 {label} 字段不符: {key}")


def _verify_example(row: dict, example: Any) -> None:
    require(str(example.id) == row["id"], f"远端样例 ID 不符: {row['id']}")
    require(example.input == row["inputs"], f"远端样例 inputs 不符: {row['id']}")
    require(
        example.expected_output == row["outputs"],
        f"远端样例 expected_output 不符: {row['id']}",
    )
    _verify_metadata(
        example.metadata,
        row["metadata"],
        EXAMPLE_METADATA_EXTRA,
        f"样例 metadata: {row['id']}",
    )


def _get_dataset(client: Any, name: str):
    # 404 是“数据集不存在”的文档化语义；其余错误就地传播。
    try:
        return client.get_dataset(name)
    except ApiError as error:
        if error.status_code == 404:
            return None
        raise


def _fetch_examples(client: Any, name: str) -> dict[str, Any]:
    return {str(item.id): item for item in client.get_dataset(name).items}


def _resolve_dataset(client: Any, name: str, marker: dict) -> tuple[Any, str]:
    path = receipt_path(name)
    if path.is_file():
        receipt = support.load_document(path)
        if receipt.get("dataset_name") == name:
            remote = _get_dataset(client, name)
            if (
                remote is not None
                and remote.name == name
                and str(remote.id) == receipt["dataset_id"]
            ):
                _verify_metadata(
                    remote.metadata, marker, DATASET_METADATA_EXTRA, "数据集 metadata"
                )
                return remote, "reused"
    remote = _get_dataset(client, name)
    if remote is not None:
        _verify_metadata(
            remote.metadata, marker, DATASET_METADATA_EXTRA, "数据集 metadata"
        )
        return remote, "reused"
    client.create_dataset(
        name=name,
        description="Fit-Agent 批准检索查询集；稳定样例 ID 绑定数据版本与内容摘要。",
        metadata=marker,
    )
    remote = _get_dataset(client, name)
    require(remote is not None and remote.name == name, "创建后未解析到唯一数据集")
    _verify_metadata(remote.metadata, marker, DATASET_METADATA_EXTRA, "数据集 metadata")
    return remote, "created"


def sync_rows(
    client: Any,
    *,
    name: str,
    rows: list[dict],
    marker: dict,
    description: str = "Fit-Agent 批准检索查询集；稳定样例 ID 绑定数据版本与内容摘要。",
    receipt_extra: dict | None = None,
) -> dict:
    # 数据集同步核心：按 id upsert 幂等；未知样例或内容冲突阻断；缺失补齐后全量复验。
    for row in rows:
        require(
            isinstance(row.get("metadata"), dict)
            and isinstance(row["metadata"].get("query_id"), str)
            and row["metadata"]["query_id"],
            "同步样例 metadata 缺少 query_id",
        )
    expected = {row["id"]: row for row in rows}
    require(len(expected) == len(rows), "同步样例 ID 重复")
    remote, dataset_status = _resolve_dataset(client, name, marker)
    dataset_id = str(remote.id)
    existing = _fetch_examples(client, name)
    unexpected = sorted(set(existing) - set(expected))
    require(not unexpected, f"远端数据集包含未知样例: {unexpected[:3]}")
    for example_id, example in existing.items():
        _verify_example(expected[example_id], example)
    missing = [row for row in rows if row["id"] not in existing]
    for row in missing:
        client.create_dataset_item(
            dataset_name=name,
            id=row["id"],
            input=row["inputs"],
            expected_output=row["outputs"],
            metadata=row["metadata"],
        )
    remote_examples = _fetch_examples(client, name)
    require(set(remote_examples) == set(expected), "远端样例集合与批准查询集不一致")
    for example_id, example in remote_examples.items():
        _verify_example(expected[example_id], example)
    receipt = {
        "schema": SCHEMA,
        "dataset_name": name,
        "dataset_id": dataset_id,
        "dataset_status": dataset_status,
        "marker": marker,
        "example_ids": {row["metadata"]["query_id"]: row["id"] for row in rows},
        "rows_sha256": support.sha256_text(json_text(rows)),
        "synced_at": now(),
    }
    if receipt_extra is not None:
        receipt.update(receipt_extra)
    support.save_document(receipt_path(name), receipt)
    return receipt


def sync_dataset(client: Any, dataset: EvaluationDataset) -> dict:
    return sync_rows(
        client,
        name=dataset_name(dataset),
        rows=example_rows(dataset),
        marker=dataset_marker(dataset),
        receipt_extra={"dataset": dataset.identity()},
    )


def select_validation_queries(dataset: EvaluationDataset) -> list[dict]:
    return support.select_validation_queries(dataset)


def select_formal_queries(dataset: EvaluationDataset) -> list[dict]:
    require(len(dataset.queries) == 150, "正式运行必须覆盖 150 条批准查询")
    return list(dataset.queries)


def validated_parameters() -> dict:
    values = frozen_values()
    expected = {
        "fusion": "rrf",
        "rrf_k": 60,
        "bm25_depth": 100,
        "dense_depth": 100,
        "threshold": 0.0,
        "candidate_pool_limit": 20,
        "final_input_limit": 5,
        "recall_limit": 20,
    }
    require(values == expected, f"冻结检索参数与批准基线不一致: {values}")
    return values


def target_query(
    inputs: Mapping[str, Any],
    *,
    dataset_identity: dict,
    expected_vector_text: str | None,
) -> dict:
    require(isinstance(inputs, Mapping), "target inputs 必须为对象")
    query_id = inputs.get("query_id")
    require(isinstance(query_id, str) and query_id, "target inputs 缺少 query_id")
    require(inputs.get("dataset") == dataset_identity, f"target inputs 数据身份不符: {query_id}")
    purpose = inputs.get("purpose")
    require(purpose in PURPOSES, f"target inputs 用途非法: {query_id}")
    query = inputs.get("query")
    require(isinstance(query, str) and query.strip(), f"target inputs 查询文本非法: {query_id}")
    conditions = inputs.get("conditions")
    require(isinstance(conditions, Mapping), f"target inputs 条件非法: {query_id}")
    if expected_vector_text is not None:
        require(
            expected_vector_text == query,
            f"缓存查询文本与批准查询不一致: {query_id}",
        )
    return {
        "query_id": query_id,
        "query": query,
        "purpose": purpose,
        "conditions": dict(conditions),
    }


def retrieval_input(
    inputs: Mapping[str, Any],
    outputs: Mapping[str, Any],
    reference_outputs: Mapping[str, Any],
    *,
    group: str,
    dataset_identity: dict,
    reviewed: Mapping[str, Collection[str]],
) -> RetrievalInput:
    query_id = inputs.get("query_id")
    require(query_id == outputs.get("query_id"), "target 输出 query_id 与输入不一致")
    require(query_id == reference_outputs.get("query_id"), "金标 query_id 与输入不一致")
    require(inputs.get("dataset") == dataset_identity, f"输入数据身份不符: {query_id}")
    require(outputs.get("dataset_content_sha256") == dataset_identity["content_sha256"], f"输出数据摘要不符: {query_id}")
    require(outputs.get("strategy") == group, f"输出检索组不符: {query_id}")
    if group == RERANK_GROUP:
        require(outputs.get("experiment") == support.EXPERIMENT_RERANK, f"输出实验身份不符: {query_id}")
        rerank_evidence = outputs.get("rerank")
        require(isinstance(rerank_evidence, Mapping), f"输出缺少重排证据: {query_id}")
        support.rebuild_rerank(rerank_evidence)
        baseline = outputs.get("baseline")
        require(isinstance(baseline, Mapping), f"输出缺少同池基线: {query_id}")
        require(baseline.get("candidate_pool") == outputs.get("candidate_pool"), f"同池基线候选不一致: {query_id}")
        require(baseline.get("final_model_input") is not None, f"同池基线最终输入缺失: {query_id}")
    elif outputs.get("experiment") not in (None, support.EXPERIMENT_BASELINE):
        raise ValueError(f"输出实验身份不符: {query_id}")
    reference = reference_outputs.get("labels")
    require(isinstance(reference, list), f"金标 labels 非法: {query_id}")
    candidate_pool = list(outputs.get("candidate_pool", []))
    require_reviewed_candidates(reviewed, query_id, candidate_pool)
    return RetrievalInput(
        query_id=query_id,
        scenario_id=reference_outputs.get("scenario_id"),
        expect_empty=reference_outputs.get("expect_empty") is True,
        labels=[
            RelevanceLabel(exercise_id=item["exercise_id"], grade=item["grade"])
            for item in reference
        ],
        candidate_pool=candidate_pool,
        final_model_input=list(outputs.get("final_model_input", [])),
    )


def feedback_scores(result) -> dict[str, float]:
    if result.expect_empty:
        return {"false_return": float(bool(result.false_return))}
    return {metric.name: metric.value for metric in result.metrics}


def make_evaluator(
    group: str,
    dataset_identity: dict,
    reviewed: Mapping[str, Collection[str]],
):
    async def evaluator(*, input, output, expected_output, metadata, **kwargs):
        item = retrieval_input(
            input,
            output,
            expected_output,
            group=group,
            dataset_identity=dataset_identity,
            reviewed=reviewed,
        )
        return [
            Evaluation(name=name, value=value)
            for name, value in feedback_scores(evaluate(item)).items()
        ]

    return evaluator


def expected_feedback_keys(reference_outputs: Mapping[str, Any]) -> set[str]:
    if reference_outputs.get("expect_empty") is True:
        return {"false_return"}
    return set(RETRIEVAL_METRICS)


def expected_feedback(
    group: str,
    inputs: Mapping[str, Any],
    outputs: Mapping[str, Any],
    reference_outputs: Mapping[str, Any],
    dataset_identity: dict,
    reviewed: Mapping[str, Collection[str]],
) -> dict[str, float]:
    item = retrieval_input(
        inputs,
        outputs,
        reference_outputs,
        group=group,
        dataset_identity=dataset_identity,
        reviewed=reviewed,
    )
    return feedback_scores(evaluate(item))


@dataclass
class ConcurrencyRecorder:
    in_flight: int = 0
    max_in_flight: int = 0
    calls: int = 0
    intervals: list[tuple[str, int, int]] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def start(self, query_id: str) -> int:
        async with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.calls += 1
        return time.monotonic_ns()

    async def finish(self, query_id: str, started: int) -> None:
        finished = time.monotonic_ns()
        async with self._lock:
            self.in_flight -= 1
            self.intervals.append((query_id, started, finished))

    def overlap_pairs(self) -> int:
        pairs = 0
        ordered = sorted(self.intervals, key=lambda item: item[1])
        for index, (_, start, end) in enumerate(ordered):
            for _, other_start, other_end in ordered[index + 1 :]:
                if other_start < end and other_end > start:
                    pairs += 1
        return pairs

    def report(self) -> dict:
        return {
            "calls": self.calls,
            "max_in_flight": self.max_in_flight,
            "overlap_pairs": self.overlap_pairs(),
            "intervals": [
                {"query_id": query_id, "start_ns": start, "end_ns": end}
                for query_id, start, end in self.intervals
            ],
        }


@dataclass
class RpcRecorder:
    calls: int = 0
    in_flight: int = 0
    max_in_flight: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def start(self) -> None:
        async with self._lock:
            self.calls += 1
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)

    async def finish(self) -> None:
        async with self._lock:
            self.in_flight -= 1

    def report(self) -> dict:
        return {"calls": self.calls, "max_in_flight": self.max_in_flight}


class ObservedMilvus:
    # 观测包装只统计真实 RPC；底层 MilvusConnection 仍按既有锁串行执行。
    def __init__(self, inner: MilvusConnection) -> None:
        self.inner = inner
        self.recorder = RpcRecorder()

    async def call(self, method: str, *args, **kwargs):
        await self.recorder.start()
        try:
            return await self.inner.call(method, *args, **kwargs)
        finally:
            await self.recorder.finish()

    def report(self) -> dict:
        return self.recorder.report() | {"serialized": True}


class RetrievalTarget:
    def __init__(
        self,
        *,
        group: str,
        db: ObservedMilvus,
        collection: str,
        index_manifest_sha256: str,
        corpus: RetrievalCorpus,
        mappings: dict,
        parameters: dict,
        vectors: dict[str, list[float]],
        vector_texts: dict[str, str],
        vector_identity_sha256: str,
        dataset_identity: dict,
        reviewed: Mapping[str, Collection[str]],
        rerank_client: RerankClient | None = None,
        rerank_identity: RerankIdentity | None = None,
    ) -> None:
        self.group = group
        self.db = db
        self.collection = collection
        self.index_manifest_sha256 = index_manifest_sha256
        self.corpus = corpus
        self.mappings = mappings
        self.parameters = parameters
        self.vectors = vectors
        self.vector_texts = vector_texts
        self.vector_identity_sha256 = vector_identity_sha256
        self.dataset_identity = dataset_identity
        self.reviewed = reviewed
        self.rerank_client = rerank_client
        self.rerank_identity = rerank_identity
        self.recorder = ConcurrencyRecorder()

    async def __call__(self, inputs: dict) -> dict:
        query_id = inputs.get("query_id")
        expected_text = self.vector_texts.get(query_id) if self.group != "bm25" else None
        query = target_query(
            inputs,
            dataset_identity=self.dataset_identity,
            expected_vector_text=expected_text,
        )
        started_iso = now()
        started = await self.recorder.start(query_id)
        baseline = None
        try:
            vector = self.vectors[query_id] if self.group != "bm25" else []
            if self.group == RERANK_GROUP:
                require(
                    self.rerank_client is not None and self.rerank_identity is not None,
                    f"重排实验缺少客户端: {query_id}",
                )
                comparison = await support.execute_comparison_query(
                    self.db,
                    self.collection,
                    self.corpus,
                    self.mappings,
                    query,
                    vector,
                    group="hybrid",
                    rrf_k=self.parameters["rrf_k"],
                    bm25_depth=self.parameters["bm25_depth"],
                    dense_depth=self.parameters["dense_depth"],
                    threshold=self.parameters["threshold"],
                    rerank_client=self.rerank_client,
                    rerank_identity=self.rerank_identity,
                )
                projection = comparison[support.EXPERIMENT_RERANK]
                baseline = comparison[support.EXPERIMENT_BASELINE]
            else:
                projection = await support.execute_query(
                    self.db,
                    self.collection,
                    self.corpus,
                    self.mappings,
                    query,
                    vector,
                    group=self.group,
                    rrf_k=self.parameters["rrf_k"],
                    bm25_depth=self.parameters["bm25_depth"],
                    dense_depth=self.parameters["dense_depth"],
                    threshold=self.parameters["threshold"],
                )
        finally:
            await self.recorder.finish(query_id, started)
        require(not projection["hard_constraint_violations"], f"硬约束违规: {query_id}")
        require(projection["budget_compliant"] is True, f"响应预算违规: {query_id}")
        payload = {
            **projection,
            "query_id": query_id,
            "strategy": self.group,
            "parameters": self.parameters,
            "index_collection": self.collection,
            "index_manifest_sha256": self.index_manifest_sha256,
            "dataset_content_sha256": self.dataset_identity["content_sha256"],
            "vector_cache_identity_sha256": self.vector_identity_sha256,
            "start_time": started_iso,
            "end_time": now(),
        }
        if self.group == RERANK_GROUP:
            payload["rerank"] = support.public_rerank_evidence(projection["rerank"])
            payload["baseline"] = {
                "candidate_pool": baseline["candidate_pool"],
                "final_model_input": baseline["final_model_input"],
                "response": baseline["response"],
                "scores": baseline["scores"],
            }
        payload["review"] = review_evidence(
            self.reviewed, query_id, projection["candidate_pool"]
        )
        return payload


class RetrievalEngine:
    # 独立线程事件循环内持有 Milvus 与重排连接；实验 runner 自建循环执行 target，
    # 资源循环与其解耦，避免跨事件循环触碰连接，关闭语义保持确定性。

    def __init__(
        self,
        *,
        group: str,
        corpus: RetrievalCorpus,
        mappings: dict,
        parameters: dict,
        vectors: dict[str, list[float]],
        vector_texts: dict[str, str],
        vector_identity_sha256: str,
        dataset_identity: dict,
        reviewed: Mapping[str, Collection[str]],
    ) -> None:
        self.group = group
        self.corpus = corpus
        self.mappings = mappings
        self.parameters = parameters
        self.vectors = vectors
        self.vector_texts = vector_texts
        self.vector_identity_sha256 = vector_identity_sha256
        self.dataset_identity = dataset_identity
        self.reviewed = reviewed
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._opened = threading.Event()
        self._terminal = threading.Event()
        self._stop: asyncio.Event | None = None
        self._failure: BaseException | None = None
        self._startup_failed = False
        self.target: RetrievalTarget | None = None
        self.observed: ObservedMilvus | None = None
        self.collection: str | None = None
        self.index_manifest_sha256: str | None = None
        self.manifest: dict | None = None
        self.rerank_public: dict | None = None

    def open(self) -> None:
        require(self._thread is None, "检索引擎重复打开")
        self._thread = threading.Thread(
            target=self._run_thread, name="fit-agent-retrieval-engine"
        )
        self._thread.start()
        self._opened.wait()
        if self._failure is not None:
            self._startup_failed = True
            failure = self._failure
            self._thread.join()
            self._thread = None
            raise failure

    def _run_thread(self) -> None:
        asyncio.run(self._engine_main())

    async def _engine_main(self) -> None:
        self._stop = asyncio.Event()
        try:
            async with AsyncExitStack() as stack:
                await self._open(stack)
                self._opened.set()
                await self._stop.wait()
        except BaseException as exc:
            self._failure = exc
            raise
        finally:
            self._opened.set()
            self._terminal.set()

    async def _open(self, stack: AsyncExitStack) -> None:
        db = await stack.enter_async_context(MilvusConnection.from_env())
        rerank_client = None
        rerank_identity = None
        if self.group == RERANK_GROUP:
            rerank_client = await stack.enter_async_context(RerankClient.from_env())
            rerank_identity = RerankIdentity.from_config(rerank_client.config)
            self.rerank_public = {
                "model": rerank_identity.model,
                "text_version": rerank_identity.text_version,
                "endpoint_sha256": support.sha256_text(rerank_identity.endpoint),
            }
        retriever = await HybridRetriever.open(db, self.corpus, EmbeddingClient.from_env())
        collection, manifest_json = retriever.validation_identity()
        self.collection = collection
        self.manifest = json.loads(manifest_json)
        self.index_manifest_sha256 = support.sha256_text(manifest_json)
        self.observed = ObservedMilvus(db)
        self.target = RetrievalTarget(
            group=self.group,
            db=self.observed,
            collection=collection,
            index_manifest_sha256=self.index_manifest_sha256,
            corpus=self.corpus,
            mappings=self.mappings,
            parameters=self.parameters,
            vectors=self.vectors,
            vector_texts=self.vector_texts,
            vector_identity_sha256=self.vector_identity_sha256,
            dataset_identity=self.dataset_identity,
            reviewed=self.reviewed,
            rerank_client=rerank_client,
            rerank_identity=rerank_identity,
        )
        self._loop = asyncio.get_running_loop()

    def execute(self, inputs: dict) -> dict:
        # 供实验循环内的同步桥接线程调用：把 target 协程调度回资源循环执行。
        require(self._loop is not None and self.target is not None, "检索引擎未打开")
        future = asyncio.run_coroutine_threadsafe(self.target(inputs), self._loop)
        return future.result()

    def close(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._thread = None
        if not self._terminal.is_set():
            future = asyncio.run_coroutine_threadsafe(self._stop_setter(), self._loop)
            future.result()
        thread.join()
        if not self._startup_failed and self._failure is not None:
            raise self._failure

    async def _stop_setter(self) -> None:
        self._stop.set()


def make_task(engine: RetrievalEngine):
    async def task(*, item, **kwargs):
        inputs = item["input"] if isinstance(item, dict) else item.input
        # to_thread 让实验循环在 target 阻塞等待资源循环时继续调度其他样例。
        return await asyncio.to_thread(engine.execute, inputs)

    return task


def load_vectors(
    dataset: EvaluationDataset,
    selected: list[dict],
    corpus: RetrievalCorpus,
    group: str,
) -> tuple[dict[str, list[float]], dict[str, str], dict, str]:
    identity = support.query_vector_identity(
        EmbeddingClient.from_env().config, corpus, dataset.identity()
    )
    identity_sha256 = support.sha256_text(json_text(identity))
    if group == "bm25":
        return {}, {}, identity, identity_sha256
    queries = [
        {"id": item["query_id"], "query": item["query"]} for item in selected
    ]
    try:
        vectors, entries = support.read_query_cache(identity, queries)
    except support.EvaluationBlocked as exc:
        raise support.EvaluationBlocked(
            f"{group} 缺少可核验的查询向量缓存: {exc.reason}",
            exc.missing,
        ) from exc
    return vectors, {qid: entry["query"] for qid, entry in entries.items()}, identity, identity_sha256


def experiment_metadata(
    *,
    run_id: str,
    group: str,
    purpose: str,
    dataset: EvaluationDataset,
    parameters: dict,
    collection: str,
    index_manifest_sha256: str,
    vector_identity_sha256: str,
    vector_required: bool,
    selected: list[dict],
    identity: dict,
    rerank_identity: dict | None = None,
) -> dict:
    payload = {
        "local_run_id": run_id,
        "group": group,
        "purpose": purpose,
        "dataset": dataset.identity(),
        "dataset_name": dataset_name(dataset),
        "parameters": parameters,
        "code_identity": identity,
        "index_collection": collection,
        "index_manifest_sha256": index_manifest_sha256,
        "vector_cache_identity_sha256": vector_identity_sha256,
        "vector_cache_required": vector_required,
        "max_concurrency": MAX_CONCURRENCY,
        "num_repetitions": NUM_REPETITIONS,
        "blocking": True,
        "experiment": (
            support.EXPERIMENT_RERANK if group == RERANK_GROUP else support.EXPERIMENT_BASELINE
        ),
        "sample_scope": {
            "query_ids": [item["query_id"] for item in selected],
            "count": len(selected),
            "rule": (
                "每个用途各取第一条有答案与第一条无答案查询，按 query_id 升序"
                if purpose == "validation"
                else "批准全量 150 条查询"
            ),
        },
        "scoring": {
            "engine": "mteb",
            "relevance": "grade>=1",
            "stages": {
                "recall@20": "candidate_pool",
                "hit@5": "final_model_input",
                "mrr@5": "final_model_input",
                "ndcg@5": "final_model_input",
                "precision@5": "final_model_input",
            },
        },
    }
    if rerank_identity is not None:
        payload["rerank_identity"] = rerank_identity
    return payload


def _load_items(
    client: Any, name: str, rows: dict[str, dict], selected_ids: list[str]
) -> list[Any]:
    remote = _fetch_examples(client, name)
    items = []
    for item_id in selected_ids:
        require(item_id in remote, f"远端缺少验收样例: {item_id}")
        _verify_example(rows[item_id], remote[item_id])
        items.append(remote[item_id])
    return items


def _list_experiment_items(client: Any, experiment_id: str, from_start_time) -> list:
    items = []
    cursor = None
    while True:
        response = client.api.experiments.list_items(
            from_start_time=from_start_time,
            experiment_id=experiment_id,
            fields="io,scores,itemMetadata",
            limit=100,
            cursor=cursor,
        )
        items.extend(response.data)
        cursor = response.meta.cursor
        if not cursor:
            return items


def _item_scores(item: Any) -> dict[str, float]:
    scores = {}
    for score in item.scores or []:
        value = getattr(score, "value", None)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        scores[score.name] = float(value)
    return scores


def verify_group(
    client: Any,
    *,
    group: str,
    dataset_identity: dict,
    reviewed: Mapping[str, Collection[str]],
    rows: dict[str, dict],
    selected_example_ids: list[str],
    experiment_id: str,
    from_start_time,
) -> tuple[list[dict], dict]:
    items = _list_experiment_items(client, experiment_id, from_start_time)
    by_example: dict[str, list[Any]] = defaultdict(list)
    for item in items:
        by_example[str(item.experiment_item_id)].append(item)
    samples = []
    for example_id in selected_example_ids:
        row = rows[example_id]
        query_id = row["inputs"]["query_id"]
        matched = by_example.get(example_id, [])
        if len(matched) != NUM_REPETITIONS:
            samples.append(
                {
                    "query_id": query_id,
                    "example_id": example_id,
                    "status": "missing" if not matched else "duplicate_items",
                    "trace_ids": [item.trace_id for item in matched],
                    "error": f"期望 {NUM_REPETITIONS} 条实验条目，实际 {len(matched)} 条",
                }
            )
            continue
        item = matched[0]
        outputs = item.output
        candidate_pool = (
            outputs.get("candidate_pool") if isinstance(outputs, dict) else None
        )
        unknown = (
            unreviewed_in_pool(reviewed, query_id, candidate_pool)
            if isinstance(candidate_pool, list)
            else []
        )
        if unknown:
            samples.append(
                {
                    "query_id": query_id,
                    "example_id": example_id,
                    "trace_id": item.trace_id,
                    "status": "blocked_unreviewed_candidates",
                    "outputs": outputs,
                    "reference_outputs": row["outputs"],
                    "unknown_candidates": unknown,
                    "error": f"{query_id} 存在未审阅候选: {', '.join(unknown)}",
                    "run_start_time": item.start_time.isoformat() if item.start_time else None,
                    "run_end_time": item.end_time.isoformat() if item.end_time else None,
                }
            )
            continue
        if item.level == ObservationLevel.ERROR:
            samples.append(
                {
                    "query_id": query_id,
                    "example_id": example_id,
                    "trace_id": item.trace_id,
                    "status": "failed",
                    "error": outputs if isinstance(outputs, str) else "实验条目标记为 ERROR",
                }
            )
            continue
        if not isinstance(outputs, dict):
            samples.append(
                {
                    "query_id": query_id,
                    "example_id": example_id,
                    "trace_id": item.trace_id,
                    "status": "failed",
                    "error": "实验条目缺少 outputs",
                }
            )
            continue
        feedback = _item_scores(item)
        try:
            require(
                len(feedback) == len(item.scores or []),
                f"反馈指标重复: {query_id}",
            )
            require(
                set(feedback) == expected_feedback_keys(row["outputs"]),
                f"反馈指标集合不符: {query_id}",
            )
            expected = expected_feedback(
                group,
                row["inputs"],
                outputs,
                row["outputs"],
                dataset_identity,
                reviewed,
            )
            for name, value in expected.items():
                require(
                    math.isclose(
                        feedback[name],
                        value,
                        rel_tol=0.0,
                        abs_tol=REMOTE_SCORE_TOLERANCE,
                    ),
                    f"远端反馈与本地评分不一致: {query_id}/{name}",
                )
        except (KeyError, TypeError, ValueError) as exc:
            samples.append(
                {
                    "query_id": query_id,
                    "example_id": example_id,
                    "trace_id": item.trace_id,
                    "status": "failed",
                    "outputs": outputs,
                    "reference_outputs": row["outputs"],
                    "feedback": feedback,
                    "error": f"{type(exc).__name__}: {exc}",
                    "run_start_time": item.start_time.isoformat() if item.start_time else None,
                    "run_end_time": item.end_time.isoformat() if item.end_time else None,
                }
            )
            continue
        samples.append(
            {
                "query_id": query_id,
                "example_id": example_id,
                "trace_id": item.trace_id,
                "status": "complete",
                "outputs": outputs,
                "reference_outputs": row["outputs"],
                "feedback": feedback,
                "run_start_time": item.start_time.isoformat() if item.start_time else None,
                "run_end_time": item.end_time.isoformat() if item.end_time else None,
            }
        )
    extra = sorted(set(by_example) - set(selected_example_ids))
    status = "complete" if not extra and all(item["status"] == "complete" for item in samples) else "incomplete"
    coverage = {
        "status": status,
        "expected_runs": len(selected_example_ids),
        "observed_runs": len(items),
        "extra_example_ids": extra,
    }
    unknown = {
        item["query_id"]: item["unknown_candidates"]
        for item in samples
        if item.get("unknown_candidates")
    }
    if unknown:
        coverage["unknown_candidates"] = unknown
    return samples, coverage


def parity_report(
    dataset: EvaluationDataset,
    selected: list[dict],
    samples: list[dict],
    corpus: RetrievalCorpus,
    group: str,
    parameters: dict,
) -> dict:
    unknown = dataset.unreviewed_candidates(
        {
            sample["query_id"]: sample["outputs"].get("candidate_pool", [])
            for sample in samples
            if isinstance(sample.get("outputs"), dict)
        }
    )
    if unknown:
        raise support.EvaluationBlocked(
            "评分一致性核验存在未审阅候选: "
            + "；".join(
                f"{query_id}: {', '.join(ids)}" for query_id, ids in unknown.items()
            ),
            tuple(
                sorted(
                    {
                        corpus_id
                        for ids in unknown.values()
                        for corpus_id in ids
                    }
                )
            ),
        )
    projections = {
        sample["query_id"]: sample["outputs"]
        for sample in samples
        if sample["status"] == "complete"
    }
    require(len(projections) == len(selected), "评分一致性核验缺少完整样本")
    mappings = rt.load_mappings()
    evaluated = {
        item["query_id"]: support.dataset_conditions(item, corpus.catalog, mappings)
        for item in selected
    }
    scoring = [
        item for item in dataset.scoring_queries() if item["id"] in projections
    ]
    metrics = validate_projections(
        scoring,
        projections,
        corpus.catalog,
        evaluated,
        parameters["threshold"] if group in ("hybrid", RERANK_GROUP) else None,
    )
    for sample in samples:
        query_id = sample["query_id"]
        local = metrics["per_query"][query_id]["scores"]
        require(set(local) == set(sample["feedback"]), f"新旧指标集合不一致: {query_id}")
        for name, value in local.items():
            require(
                math.isclose(
                    sample["feedback"][name],
                    value,
                    rel_tol=0.0,
                    abs_tol=REMOTE_SCORE_TOLERANCE,
                ),
                f"新旧评分不一致: {query_id}/{name}",
            )
    return {
        "status": "complete",
        "metrics": metrics,
        "query_ids": [sample["query_id"] for sample in samples],
        "remote_score_tolerance": REMOTE_SCORE_TOLERANCE,
    }


def comparison_report(samples: list[dict]) -> dict:
    # 同池对照只核对保存的候选身份与 RRF 分数；Recall@20 由同一候选池直接决定。
    rows = []
    for sample in samples:
        outputs = sample.get("outputs")
        require(isinstance(outputs, dict), f"重排样本缺少输出: {sample.get('query_id')}")
        baseline = outputs.get("baseline")
        require(isinstance(baseline, dict), f"重排样本缺少同池基线: {sample.get('query_id')}")
        require(
            outputs.get("candidate_pool") == baseline.get("candidate_pool"),
            f"同池候选不一致: {sample.get('query_id')}",
        )
        require(
            outputs.get("scores") == baseline.get("scores"),
            f"同池 RRF 分数不一致: {sample.get('query_id')}",
        )
        rows.append(
            {
                "query_id": sample.get("query_id"),
                "candidate_pool_identical": True,
                "baseline_final_model_input": baseline.get("final_model_input"),
                "rerank_final_model_input": outputs.get("final_model_input"),
                "order_changed": baseline.get("final_model_input")
                != outputs.get("final_model_input"),
            }
        )
    return {
        "status": "complete",
        "queries": len(rows),
        "order_changed": sum(1 for row in rows if row["order_changed"]),
        "rows": rows,
    }


def read_experiment(client: Any, experiment_id: str, from_start_time) -> Any:
    response = client.api.experiments.list(
        from_start_time=from_start_time,
        id=experiment_id,
        fields="metadata",
    )
    matched = [item for item in response.data if item.id == experiment_id]
    require(len(matched) == 1, "远端实验不存在或不唯一")
    return matched[0]


def verify_experiment_metadata(
    client: Any, experiment_id: str, expected: dict, from_start_time
) -> None:
    remote = read_experiment(client, experiment_id, from_start_time)
    metadata = remote.metadata or {}
    for key in EXPERIMENT_METADATA_KEYS:
        if key not in expected:
            continue
        require(
            metadata.get(key) == expected[key],
            f"远端实验 metadata 不符: {key}",
        )


def verify_uploaded_experiment(
    client: Any,
    *,
    experiment_id: str,
    dataset_id: str,
    run_name: str,
    from_start_time,
) -> None:
    # 回执短路核验：远端实验仍存在且身份与回执一致。
    remote = read_experiment(client, experiment_id, from_start_time)
    require(str(remote.dataset_id) == dataset_id, "远端实验数据集与上传回执不一致")
    require(remote.name == run_name, "远端实验名称与上传回执不一致")


def verify_group_until_complete(client: Any, **kwargs) -> tuple[list[dict], dict]:
    deadline = time.monotonic() + REMOTE_VERIFY_TIMEOUT_SECONDS
    while True:
        samples, coverage = verify_group(client, **kwargs)
        if (
            coverage["status"] == "complete"
            or coverage.get("unknown_candidates")
            or time.monotonic() >= deadline
        ):
            return samples, coverage
        time.sleep(REMOTE_VERIFY_POLL_SECONDS)


def write_group_evidence(run_dir: Path, group: str, samples: list[dict], result: dict) -> None:
    group_dir = run_dir / group
    sample_dir = group_dir / "samples"
    for sample in samples:
        support.save_document(sample_dir / f"{sample['query_id']}.json", sample)
    support.save_document(group_dir / "result.json", result)


def run_group(
    client: Any,
    *,
    dataset: EvaluationDataset,
    receipt: dict,
    rows: dict[str, dict],
    selected: list[dict],
    group: str,
    run_id: str,
    purpose: str,
    identity: dict,
) -> dict:
    parameters = validated_parameters()
    corpus = RetrievalCorpus.load()
    require(
        dataset.corpus_sha256 == retrieval_corpus_identity(corpus),
        "评测语料快照与真实目录不一致",
    )
    mappings = rt.load_mappings()
    reviewed = dataset.reviewed_corpus_ids()
    vectors, vector_texts, _, vector_identity_sha256 = load_vectors(
        dataset, selected, corpus, group
    )
    selected_example_ids = [
        receipt["example_ids"][item["query_id"]] for item in selected
    ]
    items = _load_items(client, receipt["dataset_name"], rows, selected_example_ids)
    metadata = experiment_metadata(
        run_id=run_id,
        group=group,
        purpose=purpose,
        dataset=dataset,
        parameters=parameters,
        collection="",
        index_manifest_sha256="",
        vector_identity_sha256=vector_identity_sha256,
        vector_required=group != "bm25",
        selected=selected,
        identity=identity,
    )
    engine = RetrievalEngine(
        group=group,
        corpus=corpus,
        mappings=mappings,
        parameters=parameters,
        vectors=vectors,
        vector_texts=vector_texts,
        vector_identity_sha256=vector_identity_sha256,
        dataset_identity=dataset_binding(dataset),
        reviewed=reviewed,
    )
    run_started = datetime.now(UTC)
    try:
        engine.open()
        metadata.update(
            index_collection=engine.collection,
            index_manifest_sha256=engine.index_manifest_sha256,
            index_build_id=engine.manifest.get("build_id"),
            index_publisher=engine.manifest.get("publisher"),
        )
        if engine.rerank_public is not None:
            metadata["rerank_identity"] = engine.rerank_public
        results = client.run_experiment(
            name=f"{run_id}-{group}-{purpose}",
            run_name=f"{run_id}-{group}-{purpose}",
            description=f"Fit-Agent {group} 检索实验（{purpose}）",
            data=items,
            task=make_task(engine),
            evaluators=[make_evaluator(group, dataset_binding(dataset), reviewed)],
            max_concurrency=MAX_CONCURRENCY,
            metadata=metadata,
        )
    finally:
        engine.close()
    experiment_id = str(results.experiment_id)
    samples, coverage = verify_group_until_complete(
        client,
        group=group,
        dataset_identity=dataset_binding(dataset),
        reviewed=reviewed,
        rows=rows,
        selected_example_ids=selected_example_ids,
        experiment_id=experiment_id,
        from_start_time=run_started,
    )
    concurrency = engine.target.recorder.report()
    require(
        concurrency["max_in_flight"] <= MAX_CONCURRENCY,
        f"在途 target 超过并发上限: {concurrency['max_in_flight']}",
    )
    verify_experiment_metadata(client, experiment_id, metadata, run_started)
    unknown = coverage.get("unknown_candidates") or {}
    result = {
        "status": (
            "blocked_unreviewed_candidates" if unknown else coverage["status"]
        ),
        "purpose": purpose,
        "group": group,
        "experiment_id": experiment_id,
        "experiment_name": results.run_name,
        "experiment_url": results.experiment_url,
        "coverage": coverage,
        "concurrency": concurrency,
        "milvus_rpc": engine.observed.report(),
        "parameters": parameters,
        "index_collection": engine.collection,
        "index_manifest_sha256": engine.index_manifest_sha256,
        "index_build_id": engine.manifest.get("build_id"),
        "vector_cache_identity_sha256": vector_identity_sha256,
        "vector_cache_required": group != "bm25",
        "experiment": metadata["experiment"],
        "samples": samples,
    }
    if engine.rerank_public is not None:
        result["rerank_identity"] = engine.rerank_public
    if unknown:
        result["unknown_candidates"] = unknown
        result["error"] = "存在未审阅候选，已保留候选与阻断证据"
        result["parity"] = {"status": "not_run"}
    elif coverage["status"] == "complete":
        result["parity"] = parity_report(
            dataset, selected, samples, corpus, group, parameters
        )
        if group == RERANK_GROUP:
            result["comparison"] = comparison_report(samples)
        if concurrency["overlap_pairs"] <= 0 or concurrency["max_in_flight"] <= 1:
            result["status"] = "incomplete"
            result["error"] = "未观察到查询重叠执行"
    else:
        result["parity"] = {"status": "not_run"}
    return result


def _group_failure(group: str, error: Exception) -> dict:
    result = {
        "status": "failed",
        "group": group,
        "error": f"{type(error).__name__}: {error}",
        "samples": [],
        "coverage": {"status": "failed", "expected_runs": 0, "observed_runs": 0},
        "parity": {"status": "not_run"},
    }
    if isinstance(error, support.EvaluationBlocked):
        result["reason"] = error.reason
        result["missing"] = list(error.missing)
    return result


def run_experiments(args) -> None:
    client = make_client()
    dataset = load_for_evaluation(Path(args.dataset))
    selected = (
        select_validation_queries(dataset)
        if args.purpose == "validation"
        else select_formal_queries(dataset)
    )
    identity = sdk_code_identity()
    receipt = sync_dataset(client, dataset)
    rows = {row["id"]: row for row in example_rows(dataset)}
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    run_dir = SDK_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    document = {
        "schema": SCHEMA,
        "run_id": run_id,
        "purpose": args.purpose,
        "status": "running",
        "started_at": now(),
        "dataset": dataset.identity(),
        "dataset_name": dataset_name(dataset),
        "dataset_id": receipt["dataset_id"],
        "example_ids": receipt["example_ids"],
        "parameters": validated_parameters(),
        "code_identity": identity,
        "max_concurrency": MAX_CONCURRENCY,
        "num_repetitions": NUM_REPETITIONS,
        "sample_scope": {
            "query_ids": [item["query_id"] for item in selected],
            "rule": (
                "每个用途各取第一条有答案与第一条无答案查询，按 query_id 升序"
                if args.purpose == "validation"
                else "批准全量 150 条查询"
            ),
        },
        "groups": {},
    }
    support.save_document(run_dir / "run.json", document)
    failures = []
    for group in args.groups:
        try:
            result = run_group(
                client,
                dataset=dataset,
                receipt=receipt,
                rows=rows,
                selected=selected,
                group=group,
                run_id=run_id,
                purpose=args.purpose,
                identity=identity,
            )
        except Exception as exc:
            result = _group_failure(group, exc)
            failures.append(group)
        document["groups"][group] = result
        support.save_document(run_dir / "run.json", document)
        write_group_evidence(run_dir, group, result.get("samples", []), result)
    document["finished_at"] = now()
    document["status"] = (
        "complete"
        if not failures
        and all(item["status"] == "complete" for item in document["groups"].values())
        else "incomplete"
    )
    support.save_document(run_dir / "run.json", document)
    print(
        json.dumps(
            {
                "run_id": run_id,
                "artifact": str(run_dir / "run.json"),
                "status": document["status"],
                "groups": {
                    group: {
                        "status": item["status"],
                        "experiment_id": item.get("experiment_id"),
                        "coverage": item.get("coverage"),
                        "unknown_candidates": item.get("unknown_candidates"),
                        "error": item.get("error"),
                    }
                    for group, item in document["groups"].items()
                },
            },
            ensure_ascii=False,
        )
    )
    if document["status"] != "complete":
        raise support.EvaluationBlocked(
            "存在失败或不完整检索组，证据已保留在 " + str(run_dir)
        )


def make_client() -> Langfuse:
    # 模块内唯一 Client 构造点：测试注入内存实现，生产在此读取环境凭据并快速校验。
    require(bool(os.environ.get("LANGFUSE_PUBLIC_KEY")), "缺少 LANGFUSE_PUBLIC_KEY")
    require(bool(os.environ.get("LANGFUSE_SECRET_KEY")), "缺少 LANGFUSE_SECRET_KEY")
    client = Langfuse(
        public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
        secret_key=os.environ["LANGFUSE_SECRET_KEY"],
        base_url=os.environ.get("LANGFUSE_BASE_URL"),
    )
    client.auth_check()
    return client


def submit_frozen_experiment(
    client: Any,
    *,
    payload: dict,
    verify_from,
    pending_path: Path,
    source_sha256: str,
    pending: dict | None = None,
) -> dict:
    # 冻结证据上传：先落盘 pending 身份再提交，run_experiment 返回后把实验 id 并入 pending；
    # 数据集同步稳定 id 幂等；target 只返回已校验冻结输出，evaluator 产出冻结分数（完整精度）。
    # 恢复时按 pending 解析既有实验（含 experiment_id 时以 id 为主），已核验通过的样例禁止重复提交，
    # 仅对确认缺失的样例以同 name/run_name/metadata 再次 run_experiment 合并进同一实验；
    # 提交后按既有有界轮询回读核验。成功仅返回回执，pending 保留由调用方在持久化终态回执后删除；
    # 超时就地失败并保留 pending 供重跑恢复。
    name = payload["dataset_name"]
    run_name = payload["run_name"]
    rows = payload["results"]
    query_ids = [row["run_metadata"]["query_id"] for row in rows]
    if pending is not None:
        require(pending.get("schema") == PENDING_SCHEMA, "pending 记录 schema 非法")
        require(pending["source_sha256"] == source_sha256, "pending 记录与源证据不一致")
        require(pending["dataset_name"] == name, "pending 数据集与当前证据不一致")
        require(pending["run_name"] == run_name, "pending 实验名与当前证据不一致")
        require(pending["query_ids"] == query_ids, "pending 样例覆盖与当前证据不一致")
        verify_from = datetime.fromisoformat(pending["verify_from"])
    prepared = [
        {
            "id": row["row_id"],
            "inputs": row["inputs"],
            "outputs": row["expected_outputs"],
            "metadata": row["run_metadata"],
        }
        for row in rows
    ]
    if pending is None:
        # pending 必须先于 run_experiment 落盘：提交中断后凭它解析既有实验身份。
        pending_record = {
            "schema": PENDING_SCHEMA,
            "dataset_name": name,
            "run_name": run_name,
            "verify_from": verify_from.isoformat(),
            "source_sha256": source_sha256,
            "query_ids": query_ids,
            "status": "pending",
        }
        support.save_document(pending_path, pending_record)
    else:
        pending_record = pending
    receipt = sync_rows(
        client,
        name=name,
        rows=prepared,
        marker=payload["dataset_marker"],
        description="Fit-Agent 冻结检索证据；稳定样例 ID 绑定数据身份与内容摘要。",
    )
    outputs_by_qid = {row["run_metadata"]["query_id"]: row["actual_outputs"] for row in rows}
    scores_by_qid = {
        row["run_metadata"]["query_id"]: {
            item["key"]: item["score"] for item in row["evaluation_scores"]
        }
        for row in rows
    }
    ordered_ids = [row["id"] for row in prepared]
    items = _load_items(client, name, {row["id"]: row for row in prepared}, ordered_ids)
    frozen_outputs = {str(row["row_id"]): row["actual_outputs"] for row in rows}

    def task(*, item, **kwargs):
        item_id = str(item["id"] if isinstance(item, dict) else item.id)
        return frozen_outputs[item_id]

    def evaluator(*, input, output, expected_output, metadata, **kwargs):
        query_id = metadata["query_id"]
        require(output == outputs_by_qid[query_id], f"冻结输出回放不符: {query_id}")
        return [
            Evaluation(name=key, value=value)
            for key, value in scores_by_qid[query_id].items()
        ]

    experiment = None
    if pending is not None:
        if pending.get("experiment_id"):
            # 已提交过的实验以 id 为主有界等待可读：列表读取滞后也不得误判为从未提交
            # 而全量重放（会向同一实验追加重复条目）。
            experiment = _await_pending_experiment(
                client, pending=pending, dataset_id=receipt["dataset_id"]
            )
        else:
            experiment = _resolve_pending_experiment(
                client, pending=pending, dataset_id=receipt["dataset_id"]
            )
    submit_items = items
    if experiment is not None:
        existing = _list_experiment_items(client, str(experiment.id), verify_from)
        verified = _verified_frozen_query_ids(
            existing, query_ids=query_ids, scores_by_qid=scores_by_qid
        )
        row_id_by_qid = {row["metadata"]["query_id"]: row["id"] for row in prepared}
        missing_ids = {
            row_id_by_qid[query_id]
            for query_id in query_ids
            if query_id not in verified
        }
        submit_items = [item for item in items if str(item.id) in missing_ids]
    if submit_items or experiment is None:
        results = client.run_experiment(
            name=run_name,
            run_name=run_name,
            description="Fit-Agent 冻结检索证据回放上传",
            data=submit_items,
            task=task,
            evaluators=[evaluator],
            max_concurrency=MAX_CONCURRENCY,
            metadata=payload["experiment_metadata"],
        )
        experiment_id = str(results.experiment_id)
        experiment_url = results.experiment_url
        if experiment is not None:
            require(
                experiment_id == str(experiment.id),
                "补投实验未合并进 pending 对应实验",
            )
        # run_experiment 已返回：立即把实验身份并入 pending 落盘，此后中断可按 id 恢复。
        pending_record = _record_pending_experiment(
            pending_path,
            pending_record,
            experiment_id=experiment_id,
            experiment_url=experiment_url,
        )
    else:
        # 恢复路径且远端已含全部样例：不再触发 task，直接进入全量核验。
        experiment_id = str(experiment.id)
        experiment_url = None
    verify_frozen_until_complete(
        client,
        experiment_id=experiment_id,
        expected=scores_by_qid,
        expected_metadata=payload["experiment_metadata"],
        from_start_time=verify_from,
    )
    # pending 不在此删除：由调用方在终态回执持久化之后删除，消除回执落盘前的崩溃窗口。
    return {
        "experiment_id": experiment_id,
        "experiment_url": experiment_url,
        "dataset_id": receipt["dataset_id"],
        "dataset_name": name,
        "run_name": run_name,
        "verify_from": verify_from.isoformat(),
    }


def verify_frozen_scores(
    client: Any,
    *,
    experiment_id: str,
    expected: dict[str, dict[str, float]],
    from_start_time,
) -> None:
    # 逐样例核验冻结分数：实验条目恰好覆盖全部样例且分数与本地一致（容差 5e-5）。
    items = _list_experiment_items(client, experiment_id, from_start_time)
    by_item: dict[str, list[Any]] = defaultdict(list)
    for item in items:
        by_item[str(item.experiment_item_id)].append(item)
    for item in items:
        matched = by_item[str(item.experiment_item_id)]
        require(len(matched) == 1, f"实验条目重复: {item.experiment_item_id}")
        metadata = item.experiment_item_metadata or {}
        query_id = metadata.get("query_id")
        require(query_id in expected, f"实验条目缺少冻结样例身份: {item.experiment_item_id}")
        feedback = _item_scores(item)
        require(
            set(feedback) == set(expected[query_id]),
            f"冻结反馈指标集合不符: {query_id}",
        )
        for name, value in expected[query_id].items():
            require(
                math.isclose(
                    feedback[name],
                    value,
                    rel_tol=0.0,
                    abs_tol=REMOTE_SCORE_TOLERANCE,
                ),
                f"远端冻结反馈与本地评分不一致: {query_id}/{name}",
            )
    require(
        len(by_item) == len(expected),
        "实验条目覆盖与冻结样例数量不一致",
    )


def _list_experiments(
    client: Any, *, from_start_time, name: str, dataset_id: str
) -> list:
    # experiments.list 必填 from_start_time；按 name/dataset_id 过滤并游标翻页，fields 取 core。
    experiments = []
    cursor = None
    while True:
        response = client.api.experiments.list(
            from_start_time=from_start_time,
            name=name,
            dataset_id=dataset_id,
            fields="core",
            limit=100,
            cursor=cursor,
        )
        experiments.extend(response.data)
        cursor = response.meta.cursor
        if not cursor:
            return experiments


def _resolve_pending_experiment(client: Any, *, pending: dict, dataset_id: str):
    # 以 pending 身份解析既有实验；from_start_time 以 pending.verify_from 为准，
    # 解析不到视为从未提交，由调用方走全量提交。
    matched = [
        experiment
        for experiment in _list_experiments(
            client,
            from_start_time=datetime.fromisoformat(pending["verify_from"]),
            name=pending["run_name"],
            dataset_id=dataset_id,
        )
        if experiment.name == pending["run_name"]
        and str(experiment.dataset_id) == dataset_id
    ]
    require(len(matched) <= 1, "pending 对应实验解析到多条记录")
    return matched[0] if matched else None


def _record_pending_experiment(
    pending_path: Path,
    pending: dict,
    *,
    experiment_id: str,
    experiment_url,
) -> dict:
    # run_experiment 返回后立即把实验身份并入 pending 重新落盘（status 保持 pending）：
    # 核验或回执写入之前中断时，恢复路径可按 id 直接解析，规避列表读取滞后的误判。
    recorded = {
        **pending,
        "experiment_id": experiment_id,
        "experiment_url": experiment_url,
        "status": "pending",
    }
    support.save_document(pending_path, recorded)
    return recorded


def _await_pending_experiment(client: Any, *, pending: dict, dataset_id: str):
    # pending 已含 experiment_id：条目必然已投递过，重放即重复条目；
    # 以 id 为主有界等待实验记录与条目可读（复用既有 30s/1.5s 常量），
    # 超时就地失败（远端暂不可读、稍后重跑），禁止降级为全量提交。
    experiment_id = str(pending["experiment_id"])
    from_start_time = datetime.fromisoformat(pending["verify_from"])
    deadline = time.monotonic() + REMOTE_VERIFY_TIMEOUT_SECONDS
    while True:
        response = client.api.experiments.list(
            from_start_time=from_start_time,
            id=experiment_id,
            fields="core",
            limit=100,
        )
        matched = [
            item for item in response.data if str(item.id) == experiment_id
        ]
        require(len(matched) <= 1, "pending 对应实验解析到多条记录")
        if matched:
            experiment = matched[0]
            # 解析结果必须仍与 pending 的 dataset_name/run_name 身份一致
            require(
                experiment.name == pending["run_name"],
                "pending experiment_id 对应实验名与身份不符",
            )
            require(
                str(experiment.dataset_id) == dataset_id,
                "pending experiment_id 对应数据集与身份不符",
            )
            items = _list_experiment_items(client, experiment_id, from_start_time)
            if experiment.item_count > 0 and len(items) >= experiment.item_count:
                return experiment
        if time.monotonic() >= deadline:
            raise ValueError(
                f"pending 对应实验 {experiment_id} 远端暂不可读，"
                "上传身份已保留在 pending 记录，稍后重跑上传命令可恢复"
            )
        time.sleep(REMOTE_VERIFY_POLL_SECONDS)


def _verified_frozen_query_ids(
    items: list,
    *,
    query_ids: Sequence[str],
    scores_by_qid: Mapping[str, Mapping[str, float]],
) -> set[str]:
    # 按 experiment_item_metadata.query_id 归组既有条目：重复或未知条目就地失败；
    # 分数逐项核验（容差 5e-5）通过的样例视为已落库，其余为待补缺失样例。
    grouped: dict[str, list[Any]] = defaultdict(list)
    for item in items:
        metadata = item.experiment_item_metadata or {}
        query_id = metadata.get("query_id")
        require(
            query_id in scores_by_qid,
            f"实验条目缺少冻结样例身份: {item.experiment_item_id}",
        )
        grouped[query_id].append(item)
    verified = set()
    for query_id in query_ids:
        matched = grouped.get(query_id, [])
        require(len(matched) <= 1, f"实验条目重复: {query_id}")
        if not matched:
            continue
        feedback = _item_scores(matched[0])
        expected = scores_by_qid[query_id]
        require(set(feedback) == set(expected), f"冻结反馈指标集合不符: {query_id}")
        for key, value in expected.items():
            require(
                math.isclose(
                    feedback[key],
                    value,
                    rel_tol=0.0,
                    abs_tol=REMOTE_SCORE_TOLERANCE,
                ),
                f"远端冻结反馈与本地评分不一致: {query_id}/{key}",
            )
        verified.add(query_id)
    return verified


def _frozen_items_covered(items: list, expected: Mapping[str, Any]) -> bool:
    # 落库就绪判断：条目按 query_id 恰好覆盖全部期望样例（无缺失、无重复、无未知）。
    counts: dict[str, int] = defaultdict(int)
    for item in items:
        metadata = item.experiment_item_metadata or {}
        query_id = metadata.get("query_id")
        if query_id not in expected:
            return False
        counts[query_id] += 1
    return set(counts) == set(expected) and all(count == 1 for count in counts.values())


def _experiment_readable(client: Any, experiment_id: str, from_start_time) -> bool:
    # 实验记录可读性探测：list 读不到视为尚未落库，由调用方继续轮询。
    try:
        read_experiment(client, experiment_id, from_start_time)
    except ValueError:
        return False
    return True


def verify_frozen_until_complete(
    client: Any,
    *,
    experiment_id: str,
    expected: dict[str, dict[str, float]],
    expected_metadata: dict,
    from_start_time,
) -> None:
    # 提交后有界回读：复用实时路径的时限/间隔常量；条目覆盖与实验记录可读视为就绪，
    # 就绪前按节奏轮询，超时就地失败（pending 已在磁盘，重跑上传命令可恢复）；
    # 就绪后逐项核验内容，分数或 metadata 不符立即失败。
    deadline = time.monotonic() + REMOTE_VERIFY_TIMEOUT_SECONDS
    while True:
        items = _list_experiment_items(client, experiment_id, from_start_time)
        if _frozen_items_covered(items, expected) and _experiment_readable(
            client, experiment_id, from_start_time
        ):
            verify_frozen_scores(
                client,
                experiment_id=experiment_id,
                expected=expected,
                from_start_time=from_start_time,
            )
            verify_experiment_metadata(
                client, experiment_id, expected_metadata, from_start_time
            )
            return
        if time.monotonic() >= deadline:
            raise ValueError(
                f"冻结实验落库超时（{REMOTE_VERIFY_TIMEOUT_SECONDS}s 内回读未就绪），"
                "上传身份已保留在 pending 记录，重跑上传命令可恢复"
            )
        time.sleep(REMOTE_VERIFY_POLL_SECONDS)


def sync_command(args) -> None:
    client = make_client()
    dataset = load_for_evaluation(Path(args.dataset))
    receipt = sync_dataset(client, dataset)
    print(
        json.dumps(
            {
                "dataset_name": receipt["dataset_name"],
                "dataset_id": receipt["dataset_id"],
                "dataset_status": receipt["dataset_status"],
                "example_count": len(receipt["example_ids"]),
                "receipt": str(receipt_path(receipt["dataset_name"])),
            },
            ensure_ascii=False,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Langfuse SDK 检索评测入口")
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync", help="同步批准查询集到 Langfuse")
    sync.add_argument("--dataset", type=Path, required=True)
    run = commands.add_parser("run", help="按检索组执行 Langfuse SDK 实验")
    run.add_argument("--dataset", type=Path, required=True)
    run.add_argument(
        "--groups", choices=RUN_GROUPS, nargs="+", default=list(GROUPS)
    )
    run.add_argument(
        "--purpose", choices=("validation", "formal"), default="validation"
    )
    args = parser.parse_args()
    if args.command == "sync":
        sync_command(args)
        return
    require(len(set(args.groups)) == len(args.groups), "检索组重复")
    run_experiments(args)


if __name__ == "__main__":
    try:
        main()
    except DatasetError as exc:
        raise SystemExit(f"评测数据未就绪: {exc}") from None
    except support.EvaluationBlocked as exc:
        raise SystemExit(f"评测被阻止: {exc}") from None
