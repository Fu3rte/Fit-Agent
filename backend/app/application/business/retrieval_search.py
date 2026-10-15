import asyncio
import math
from contextlib import suppress
from dataclasses import dataclass
from threading import Event
from typing import Literal

from pymilvus import AnnSearchRequest, WeightedRanker

from app.ai.types import check_cancelled
from app.application.business import retrieval_text as rt
from app.application.business.catalog import Catalog
from app.application.business.retrieval_embedding import EmbeddingClient
from app.application.business.retrieval_index import (
    MilvusConnection,
    RetrievalCorpus,
    json_text,
    published_collection,
    require,
    validate_index,
    validated_manifest,
)


@dataclass(frozen=True)
class SearchConditions:
    equipment: str | None = None
    body_part: str | None = None
    target: str | None = None
    muscle_group: str | None = None
    available_equipment: tuple[str, ...] | None = None
    excluded_ids: tuple[str, ...] = ()

    def expression(self, catalog: Catalog) -> str:
        terms = []
        for field in rt.MAPPING_FIELDS:
            value = getattr(self, field)
            if value is not None:
                allowed = {getattr(item, field) for item in catalog.all()}
                require(isinstance(value, str) and value in allowed, f"非法字段值: {field}")
                terms.append(f"{field} == {json_text(value)}")
        if self.available_equipment is not None:
            require(isinstance(self.available_equipment, (list, tuple, set, frozenset)), "有效器械集合必须为序列")
            require(all(isinstance(value, str) and value in catalog.equipment for value in self.available_equipment), "有效器械集合包含非法值")
            terms.append("equipment in " + json_text(sorted(self.available_equipment)))
        require(isinstance(self.excluded_ids, (list, tuple, set, frozenset)), "排除 ID 必须为序列")
        require(all(isinstance(value, str) and value in catalog.exercise_ids for value in self.excluded_ids), "排除 ID 引用目录外动作")
        require(len(self.excluded_ids) == len(set(self.excluded_ids)), "排除 ID 重复")
        if self.excluded_ids:
            terms.append("id not in " + json_text(sorted(self.excluded_ids)))
        return " and ".join(terms)

    def matches(self, exercise) -> bool:
        return (
            all(getattr(self, field) is None or getattr(exercise, field) == getattr(self, field) for field in rt.MAPPING_FIELDS)
            and (self.available_equipment is None or exercise.equipment in self.available_equipment)
            and exercise.id not in self.excluded_ids
        )


@dataclass(frozen=True)
class RetrievalCandidate:
    id: str
    score: float
    name: str
    equipment: str
    body_part: str
    target: str
    muscle_group: str


def validate_parameters(query: str, alpha: float, limit: int, bm25_depth: int, dense_depth: int) -> str:
    require(isinstance(query, str) and bool(rt.normalize(query)), "查询文本不得为空")
    require(not isinstance(alpha, bool) and isinstance(alpha, (int, float)) and 0 <= alpha <= 1 and math.isfinite(alpha), "alpha 必须为有限的 [0,1] 数值")
    for name, value in (("limit", limit), ("bm25_depth", bm25_depth), ("dense_depth", dense_depth)):
        require(not isinstance(value, bool) and isinstance(value, int) and 0 < value <= 16384, f"{name} 必须为 1..16384 的整数")
    return rt.normalize(query)


# threading.Event 无异步等待；小步轮询在不遗留等待线程的前提下尽早中止在途请求。
ENCODE_CANCEL_POLL_SECONDS = 0.05


async def _encode_cancellable(
    embedding: EmbeddingClient, text: str, signal: Event | None
) -> list[float]:
    # 编码在途可取消：取消真实编码任务以中止 AsyncOpenAI 请求；取消后不返回结果。
    if signal is None:
        return await embedding.encode_query(text)
    task = asyncio.ensure_future(embedding.encode_query(text))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=ENCODE_CANCEL_POLL_SECONDS)
            if task in done:
                return task.result()
            if signal.is_set():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                check_cancelled(signal)
    except asyncio.CancelledError:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        raise


async def search_vectors(
    db: MilvusConnection,
    collection: str,
    catalog: Catalog,
    query: str,
    vector: list[float],
    *,
    conditions: SearchConditions = SearchConditions(),
    alpha: float = 0.5,
    bm25_depth: int = 50,
    dense_depth: int = 50,
    limit: int = 10,
    mode: Literal["bm25", "dense", "hybrid"] = "hybrid",
) -> list[RetrievalCandidate]:
    query = validate_parameters(query, alpha, limit, bm25_depth, dense_depth)
    expression = conditions.expression(catalog)
    require(mode in ("bm25", "dense", "hybrid"), "非法检索路径")
    output = list(rt.MAPPING_FIELDS)
    if mode == "hybrid":
        requests = [
            AnnSearchRequest(data=[query], anns_field="sparse", param={"metric_type": "BM25", "params": {}}, limit=bm25_depth, expr=expression),
            AnnSearchRequest(data=[vector], anns_field="dense", param={"metric_type": "COSINE", "params": {}}, limit=dense_depth, expr=expression),
        ]
        result = await db.call("hybrid_search", collection, reqs=requests, ranker=WeightedRanker(alpha, 1 - alpha, norm_score=True), limit=limit, output_fields=output, consistency_level="Strong")
    else:
        result = await db.call("search", collection, data=[query] if mode == "bm25" else [vector], anns_field="sparse" if mode == "bm25" else "dense", search_params={"metric_type": "BM25" if mode == "bm25" else "COSINE", "params": {}}, filter=expression, limit=limit, output_fields=output, consistency_level="Strong")
    require(len(result) == 1 and len(result[0]) <= limit, "检索结果数量不符")
    candidates = []
    seen = set()
    previous = math.inf
    for hit in result[0]:
        exercise_id = hit["id"]
        exercise = catalog.get(exercise_id) if isinstance(exercise_id, str) else None
        require(exercise is not None and exercise_id not in seen, "候选目录身份错误或重复")
        require(conditions.matches(exercise), "候选违反硬约束")
        require(all(hit["entity"][field] == getattr(exercise, field) for field in rt.MAPPING_FIELDS), "候选真实字段不符")
        score = hit["distance"]
        require(isinstance(score, (int, float)) and math.isfinite(score) and score <= previous, "候选分数或排序不符")
        previous = score
        seen.add(exercise_id)
        candidates.append(RetrievalCandidate(id=exercise_id, score=score, name=exercise.name, **{field: getattr(exercise, field) for field in rt.MAPPING_FIELDS}))
    return candidates


class HybridRetriever:
    # T6 持有同循环的 EmbeddingClient 与 MilvusConnection 上下文；关闭前等待检索取消收尾。
    # 实例内记录已成功全量验证的集合身份，稳定集合查询跳过全量回读。
    def __init__(self, db: MilvusConnection, corpus: RetrievalCorpus, embedding: EmbeddingClient) -> None:
        self.db, self.corpus, self.embedding = db, corpus, embedding
        self._validated: tuple[str, str] | None = None
        self._validation_lock = asyncio.Lock()

    @classmethod
    async def open(cls, db: MilvusConnection, corpus: RetrievalCorpus, embedding: EmbeddingClient) -> "HybridRetriever":
        retriever = cls(db, corpus, embedding)
        await retriever._validate_published()
        return retriever

    async def _manifest_identity(self, collection: str) -> tuple[tuple[str, str], dict]:
        # 轻量核验：源快照、服务/SDK/模型/维度/编码/索引兼容与清单格式身份；返回身份与期待配置。
        self.corpus.validate_sources()
        expected = self.corpus.compatibility(self.embedding, await self.db.call("get_server_version"))
        manifest = await validated_manifest(self.db, collection, expected)
        return (collection, json_text(manifest)), expected

    async def _ensure_collection(self, collection: str) -> str:
        # 按实际物理集合执行的核验入口；轻量核验通过且身份命中时跳过完整校验。
        # 成功记录仅取 validate_index 实际成功返回的清单，失败或取消不写入凭证。
        async with self._validation_lock:
            identity, expected = await self._manifest_identity(collection)
            if identity == self._validated:
                return collection
            manifest = await validate_index(self.db, collection, self.corpus, expected)
            self._validated = (collection, json_text(manifest))
            return collection

    async def _validate_published(self) -> str:
        # 保留正式 alias 解析入口；按解析出的实际集合执行核验，查询沿用本次解析结果。
        collection = await published_collection(self.db)
        return await self._ensure_collection(collection)

    def validation_identity(self) -> tuple[str, str]:
        # 已完成校验的物理集合名与清单 JSON 文本；供评测入口记录并核验实际发布身份。
        require(self._validated is not None, "检索实例尚未完成发布校验")
        return self._validated

    async def search(
        self,
        query: str,
        *,
        conditions: SearchConditions = SearchConditions(),
        alpha: float = 0.5,
        bm25_depth: int = 50,
        dense_depth: int = 50,
        limit: int = 10,
        signal: Event | None = None,
    ) -> list[RetrievalCandidate]:
        # 校验发布索引后排除互斥硬条件；阶段之间传播取消，编码在途可被真实任务取消中止。
        check_cancelled(signal)
        query = validate_parameters(query, alpha, limit, bm25_depth, dense_depth)
        conditions.expression(self.corpus.catalog)
        collection = await self._validate_published()
        check_cancelled(signal)
        has_eligible_exercises = any(
            conditions.matches(exercise) for exercise in self.corpus.catalog.all()
        )
        check_cancelled(signal)
        if not has_eligible_exercises:
            return []
        vector = await _encode_cancellable(self.embedding, query, signal)
        check_cancelled(signal)
        return await search_vectors(self.db, collection, self.corpus.catalog, query, vector, conditions=conditions, alpha=alpha, bm25_depth=bm25_depth, dense_depth=dense_depth, limit=limit)
