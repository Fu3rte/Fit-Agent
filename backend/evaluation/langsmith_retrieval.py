import argparse
import asyncio
import json
import math
import os
import time
from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from langsmith import Client
from langsmith.schemas import Example
from langsmith.utils import LangSmithNotFoundError

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

SCHEMA = "fit-agent-retrieval-langsmith-1"
DATASET_MARKER = "fit-agent-retrieval-sdk-1"
DATASET_METADATA_EXTRA = frozenset({"runtime"})
EXAMPLE_METADATA_EXTRA = frozenset({"dataset_split"})
GROUPS = ("bm25", "dense", "hybrid")
RERANK_GROUP = "hybrid-rerank"
RUN_GROUPS = (*GROUPS, RERANK_GROUP)
PURPOSES = ("lookup", "generation", "replacement")
MAX_CONCURRENCY = 6
NUM_REPETITIONS = 1
REMOTE_SCORE_TOLERANCE = 5e-5
REMOTE_VERIFY_TIMEOUT_SECONDS = 30.0
SDK_ROOT = support.EVAL_ROOT.parent / "retrieval-langsmith"
DATASET_ROOT = SDK_ROOT / "datasets"


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
    return f"fit-agent-retrieval-sdk-{dataset.version}-{dataset.content_sha256[:12]}"


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
    files["evaluation/langsmith_retrieval.py"] = support.sha256(Path(__file__))
    return {**identity, "files": files}


def stable_example_id(name: str, query_id: str) -> str:
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
    require(example.inputs == row["inputs"], f"远端样例 inputs 不符: {row['id']}")
    require(example.outputs == row["outputs"], f"远端样例 outputs 不符: {row['id']}")
    _verify_metadata(
        example.metadata,
        row["metadata"],
        EXAMPLE_METADATA_EXTRA,
        f"样例 metadata: {row['id']}",
    )


def _fetch_examples(client: Any, dataset_id: str) -> dict[str, Any]:
    return {str(example.id): example for example in client.list_examples(dataset_id=dataset_id)}


def _resolve_dataset(client: Any, name: str, marker: dict) -> tuple[Any, str]:
    path = receipt_path(name)
    if path.is_file():
        receipt = support.load_document(path)
        if receipt.get("dataset_name") == name:
            try:
                remote = client.read_dataset(dataset_id=receipt["dataset_id"])
            except LangSmithNotFoundError:
                remote = None
            if remote is not None and remote.name == name:
                _verify_metadata(
                    remote.metadata, marker, DATASET_METADATA_EXTRA, "数据集 metadata"
                )
                return remote, "reused"
    matches = list(client.list_datasets(dataset_name=name))
    require(len(matches) <= 1, "远端存在多个同名数据集，拒绝继续")
    if matches:
        _verify_metadata(
            matches[0].metadata, marker, DATASET_METADATA_EXTRA, "数据集 metadata"
        )
        return matches[0], "reused"
    client.create_dataset(
        name,
        description="Fit-Agent 批准检索查询集；稳定样例 ID 绑定数据版本与内容摘要。",
        metadata=marker,
    )
    remote = client.read_dataset(dataset_name=name)
    require(remote.name == name, "创建后未解析到唯一数据集")
    _verify_metadata(
        remote.metadata, marker, DATASET_METADATA_EXTRA, "数据集 metadata"
    )
    return remote, "created"


def sync_dataset(client: Any, dataset: EvaluationDataset) -> dict:
    name = dataset_name(dataset)
    rows = example_rows(dataset)
    expected = {row["id"]: row for row in rows}
    marker = dataset_marker(dataset)
    remote, dataset_status = _resolve_dataset(client, name, marker)
    dataset_id = str(remote.id)
    existing = _fetch_examples(client, dataset_id)
    unexpected = sorted(set(existing) - set(expected))
    require(not unexpected, f"远端数据集包含未知样例: {unexpected[:3]}")
    for example_id, example in existing.items():
        _verify_example(expected[example_id], example)
    missing = [row for row in rows if row["id"] not in existing]
    if missing:
        client.create_examples(
            dataset_id=dataset_id, examples=missing, max_concurrency=3
        )
    remote_examples = _fetch_examples(client, dataset_id)
    require(set(remote_examples) == set(expected), "远端样例集合与批准查询集不一致")
    for example_id, example in remote_examples.items():
        _verify_example(expected[example_id], example)
    receipt = {
        "schema": SCHEMA,
        "dataset_name": name,
        "dataset_id": dataset_id,
        "dataset_status": dataset_status,
        "dataset": dataset.identity(),
        "marker": marker,
        "example_ids": {row["inputs"]["query_id"]: row["id"] for row in rows},
        "rows_sha256": support.sha256_text(json_text(rows)),
        "synced_at": now(),
    }
    support.save_document(receipt_path(name), receipt)
    return receipt


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


def feedback_results(result) -> dict:
    if result.expect_empty:
        return {
            "results": [
                {"key": "false_return", "score": float(bool(result.false_return))}
            ]
        }
    values = {metric.name: metric.value for metric in result.metrics}
    return {
        "results": [
            {"key": name, "score": float(values[name])} for name in RETRIEVAL_METRICS
        ]
    }


def make_evaluator(
    group: str,
    dataset_identity: dict,
    reviewed: Mapping[str, Collection[str]],
):
    async def evaluator(inputs, outputs, reference_outputs):
        item = retrieval_input(
            inputs,
            outputs,
            reference_outputs,
            group=group,
            dataset_identity=dataset_identity,
            reviewed=reviewed,
        )
        return feedback_results(evaluate(item))

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
    result = evaluate(item)
    if result.expect_empty:
        return {"false_return": float(bool(result.false_return))}
    return {metric.name: metric.value for metric in result.metrics}


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


def _load_examples(
    client: Any, dataset_id: str, rows: dict[str, dict], selected_ids: list[str]
) -> list[Example]:
    remote = _fetch_examples(client, dataset_id)
    examples = []
    for example_id in selected_ids:
        require(example_id in remote, f"远端缺少验收样例: {example_id}")
        _verify_example(rows[example_id], remote[example_id])
        examples.append(remote[example_id])
    return examples


def verify_group(
    client: Any,
    *,
    group: str,
    dataset_identity: dict,
    reviewed: Mapping[str, Collection[str]],
    rows: dict[str, dict],
    selected_example_ids: list[str],
    experiment_id: str,
) -> tuple[list[dict], dict]:
    runs = list(client.list_runs(project_id=experiment_id, is_root=True))
    by_example: dict[str, list[Any]] = defaultdict(list)
    for run in runs:
        require(run.reference_example_id is not None, f"运行缺少样例身份: {run.id}")
        by_example[str(run.reference_example_id)].append(run)
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
                    "status": "missing" if not matched else "duplicate_runs",
                    "run_ids": [str(run.id) for run in matched],
                    "error": f"期望 {NUM_REPETITIONS} 次运行，实际 {len(matched)} 次",
                }
            )
            continue
        run = matched[0]
        outputs = run.outputs
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
                    "run_id": str(run.id),
                    "status": "blocked_unreviewed_candidates",
                    "outputs": outputs,
                    "reference_outputs": row["outputs"],
                    "unknown_candidates": unknown,
                    "error": f"{query_id} 存在未审阅候选: {', '.join(unknown)}",
                    "run_start_time": run.start_time.isoformat() if run.start_time else None,
                    "run_end_time": run.end_time.isoformat() if run.end_time else None,
                }
            )
            continue
        if run.error:
            samples.append(
                {
                    "query_id": query_id,
                    "example_id": example_id,
                    "status": "failed",
                    "run_id": str(run.id),
                    "error": run.error,
                }
            )
            continue
        if not isinstance(outputs, dict):
            samples.append(
                {
                    "query_id": query_id,
                    "example_id": example_id,
                    "status": "failed",
                    "run_id": str(run.id),
                    "error": "运行缺少 outputs",
                }
            )
            continue
        feedback_rows = [
            item
            for item in client.list_feedback(run_ids=[str(run.id)])
            if item.score is not None
        ]
        try:
            require(
                len({item.key for item in feedback_rows}) == len(feedback_rows),
                f"反馈指标重复: {query_id}",
            )
            feedback = {item.key: float(item.score) for item in feedback_rows}
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
                    "run_id": str(run.id),
                    "status": "failed",
                    "outputs": run.outputs,
                    "reference_outputs": row["outputs"],
                    "feedback": {
                        item.key: item.score for item in feedback_rows
                    },
                    "error": f"{type(exc).__name__}: {exc}",
                    "run_start_time": run.start_time.isoformat() if run.start_time else None,
                    "run_end_time": run.end_time.isoformat() if run.end_time else None,
                }
            )
            continue
        samples.append(
            {
                "query_id": query_id,
                "example_id": example_id,
                "run_id": str(run.id),
                "status": "complete",
                "outputs": run.outputs,
                "reference_outputs": row["outputs"],
                "feedback": feedback,
                "run_start_time": run.start_time.isoformat() if run.start_time else None,
                "run_end_time": run.end_time.isoformat() if run.end_time else None,
            }
        )
    extra = sorted(set(by_example) - set(selected_example_ids))
    status = "complete" if not extra and all(item["status"] == "complete" for item in samples) else "incomplete"
    coverage = {
        "status": status,
        "expected_runs": len(selected_example_ids),
        "observed_runs": len(runs),
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


def verify_experiment_metadata(
    client: Any, experiment_id: str, expected: dict
) -> None:
    project = client.read_project(project_id=experiment_id)
    remote = (project.extra or {}).get("metadata") or {}
    keys = (
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
    for key in keys:
        if key not in expected:
            continue
        require(
            remote.get(key) == expected[key],
            f"远端实验 metadata 不符: {key}",
        )


async def verify_group_until_complete(client: Any, **kwargs) -> tuple[list[dict], dict]:
    deadline = time.monotonic() + REMOTE_VERIFY_TIMEOUT_SECONDS
    while True:
        samples, coverage = verify_group(client, **kwargs)
        if (
            coverage["status"] == "complete"
            or coverage.get("unknown_candidates")
            or time.monotonic() >= deadline
        ):
            return samples, coverage
        await asyncio.sleep(1.5)


def write_group_evidence(run_dir: Path, group: str, samples: list[dict], result: dict) -> None:
    group_dir = run_dir / group
    sample_dir = group_dir / "samples"
    for sample in samples:
        support.save_document(sample_dir / f"{sample['query_id']}.json", sample)
    support.save_document(group_dir / "result.json", result)


async def run_group(
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
    examples = _load_examples(
        client, receipt["dataset_id"], rows, selected_example_ids
    )
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
    rerank_public = None
    async with AsyncExitStack() as stack:
        db = await stack.enter_async_context(MilvusConnection.from_env())
        rerank_client = None
        rerank_identity = None
        if group == RERANK_GROUP:
            rerank_client = await stack.enter_async_context(RerankClient.from_env())
            rerank_identity = RerankIdentity.from_config(rerank_client.config)
            rerank_public = {
                "model": rerank_identity.model,
                "text_version": rerank_identity.text_version,
                "endpoint_sha256": support.sha256_text(rerank_identity.endpoint),
            }
            metadata["rerank_identity"] = rerank_public
        retriever = await HybridRetriever.open(db, corpus, EmbeddingClient.from_env())
        collection, manifest_json = retriever.validation_identity()
        manifest = json.loads(manifest_json)
        index_manifest_sha256 = support.sha256_text(manifest_json)
        observed = ObservedMilvus(db)
        target = RetrievalTarget(
            group=group,
            db=observed,
            collection=collection,
            index_manifest_sha256=index_manifest_sha256,
            corpus=corpus,
            mappings=mappings,
            parameters=parameters,
            vectors=vectors,
            vector_texts=vector_texts,
            vector_identity_sha256=vector_identity_sha256,
            dataset_identity=dataset_binding(dataset),
            reviewed=reviewed,
            rerank_client=rerank_client,
            rerank_identity=rerank_identity,
        )
        metadata.update(
            index_collection=collection,
            index_manifest_sha256=index_manifest_sha256,
            index_build_id=manifest.get("build_id"),
            index_publisher=manifest.get("publisher"),
        )
        results = await client.aevaluate(
            target.__call__,
            data=examples,
            evaluators=[make_evaluator(group, dataset_binding(dataset), reviewed)],
            metadata=metadata,
            experiment_prefix=f"{run_id}-{group}-{purpose}",
            description=f"Fit-Agent {group} 检索实验（{purpose}）",
            max_concurrency=MAX_CONCURRENCY,
            num_repetitions=NUM_REPETITIONS,
            blocking=True,
            disable_evaluator_tracing=True,
        )
        await results.wait()
        experiment_id = str(results.experiment_id)
        samples, coverage = await verify_group_until_complete(
            client,
            group=group,
            dataset_identity=dataset_binding(dataset),
            reviewed=reviewed,
            rows=rows,
            selected_example_ids=selected_example_ids,
            experiment_id=experiment_id,
        )
        concurrency = target.recorder.report()
        require(
            concurrency["max_in_flight"] <= MAX_CONCURRENCY,
            f"在途 target 超过并发上限: {concurrency['max_in_flight']}",
        )
        verify_experiment_metadata(client, experiment_id, metadata)
        unknown = coverage.get("unknown_candidates") or {}
        result = {
            "status": (
                "blocked_unreviewed_candidates" if unknown else coverage["status"]
            ),
            "purpose": purpose,
            "group": group,
            "experiment_id": experiment_id,
            "experiment_name": results.experiment_name,
            "experiment_url": results.url,
            "coverage": coverage,
            "concurrency": concurrency,
            "milvus_rpc": observed.report(),
            "parameters": parameters,
            "index_collection": collection,
            "index_manifest_sha256": index_manifest_sha256,
            "index_build_id": manifest.get("build_id"),
            "vector_cache_identity_sha256": vector_identity_sha256,
            "vector_cache_required": group != "bm25",
            "experiment": metadata["experiment"],
            "samples": samples,
        }
        if rerank_public is not None:
            result["rerank_identity"] = rerank_public
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


async def run_experiments(args) -> None:
    require(bool(os.environ.get("LANGSMITH_API_KEY")), "缺少 LANGSMITH_API_KEY")
    dataset = load_for_evaluation(Path(args.dataset))
    selected = (
        select_validation_queries(dataset)
        if args.purpose == "validation"
        else select_formal_queries(dataset)
    )
    identity = sdk_code_identity()
    client = Client(auto_batch_tracing=False)
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
            result = await run_group(
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


def sync_command(args) -> None:
    require(bool(os.environ.get("LANGSMITH_API_KEY")), "缺少 LANGSMITH_API_KEY")
    dataset = load_for_evaluation(Path(args.dataset))
    client = Client(auto_batch_tracing=False)
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
    parser = argparse.ArgumentParser(description="LangSmith SDK 检索评测入口")
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync", help="同步批准查询集到 LangSmith")
    sync.add_argument("--dataset", type=Path, required=True)
    run = commands.add_parser("run", help="按检索组执行 LangSmith SDK 实验")
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
    asyncio.run(run_experiments(args))


if __name__ == "__main__":
    try:
        main()
    except DatasetError as exc:
        raise SystemExit(f"评测数据未就绪: {exc}") from None
    except support.EvaluationBlocked as exc:
        raise SystemExit(f"评测被阻止: {exc}") from None
