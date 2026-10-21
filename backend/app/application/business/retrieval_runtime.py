import asyncio
from contextlib import AsyncExitStack
from threading import Event

from app.ai.types import check_cancelled
from app.application.business import retrieval_frozen
from app.application.business.retrieval_embedding import EmbeddingClient
from app.application.business.retrieval_index import (
    IndexCompatibilityError,
    MilvusConnection,
    RetrievalCorpus,
)
from app.application.business.retrieval_rerank import (
    RerankClient,
    RerankIdentity,
    build_request,
)
from app.application.business.retrieval_response import CANDIDATE_POOL_LIMIT
from app.application.business.retrieval_search import (
    HybridRetriever,
    SearchConditions,
    SearchExecution,
)


class ActionRetrievalRuntime:
    # 检索资源生命周期持有者：按需初始化，跨查询复用同一 HybridRetriever 与 RerankClient。
    # 初始化、复用与关闭必须发生在记录下来的所属事件循环内；异循环调用直接失败并保留原资源。
    def __init__(self) -> None:
        self._retriever: HybridRetriever | None = None
        self._rerank: RerankClient | None = None
        self._rerank_identity: RerankIdentity | None = None
        self._stack: AsyncExitStack | None = None
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def _bind_loop(self) -> None:
        # 首次使用时记录所属循环；此后任何变更资源状态或消费 AsyncExitStack 的操作都在该循环内。
        # 异循环调用在触碰状态与锁之前失败，原有客户端与关闭入口保持可用。
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise IndexCompatibilityError("检索运行时属于其它事件循环")

    async def search(
        self,
        query: str,
        *,
        conditions: SearchConditions = SearchConditions(),
        limit: int = retrieval_frozen.RECALL_LIMIT,
        signal: Event | None = None,
    ) -> SearchExecution:
        # 检索管线与取消语义由 HybridRetriever 承载；运行时只负责所属循环、冻结参数应用与资源生命周期。
        # 门槛属于阶段构造，不在返回候选前过滤，保证调用方能够保留门槛前的真实候选池。
        # 本次调用的原 RRF 候选池只确定一次，重排结果与物理集合身份随执行结果返回。
        self._bind_loop()
        retriever = await self._ensure(signal)
        rerank = self._rerank
        identity = self._rerank_identity
        if rerank is None or identity is None:
            raise RuntimeError("检索运行时未完整初始化")
        recall = await retriever.search(
            query,
            conditions=conditions,
            rrf_k=retrieval_frozen.RRF_K,
            bm25_depth=retrieval_frozen.BM25_DEPTH,
            dense_depth=retrieval_frozen.DENSE_DEPTH,
            limit=limit,
            signal=signal,
        )
        check_cancelled(signal)
        pool = recall.candidates[:CANDIDATE_POOL_LIMIT]
        request = build_request(query, pool, retriever.corpus.records)
        result = await rerank.rerank(request, signal=signal)
        check_cancelled(signal)
        return SearchExecution(
            candidates=pool,
            collection=recall.collection,
            manifest_json=recall.manifest_json,
            rerank=result,
            rerank_identity=identity,
        )

    async def _ensure(self, signal: Event | None = None) -> HybridRetriever:
        # 首次或上一次失败后的初始化：整个上下文栈构建成功后才记录实例。
        # 取消在初始化各阶段之间检查；中途取消释放已建立的临时资源，不记录伪就绪。
        self._bind_loop()
        async with self._lock:
            if self._retriever is not None:
                return self._retriever
            check_cancelled(signal)
            stack = AsyncExitStack()
            try:
                db = await stack.enter_async_context(MilvusConnection.from_env())
                check_cancelled(signal)
                embedding = await stack.enter_async_context(EmbeddingClient.from_env())
                check_cancelled(signal)
                rerank = await stack.enter_async_context(RerankClient.from_env())
                check_cancelled(signal)
                corpus = RetrievalCorpus.load()
                retriever = await HybridRetriever.open(db, corpus, embedding)
                check_cancelled(signal)
                identity = RerankIdentity.from_config(rerank.config)
            except BaseException:
                await stack.aclose()
                raise
            self._stack = stack
            self._retriever = retriever
            self._rerank = rerank
            self._rerank_identity = identity
            return retriever

    async def close(self) -> None:
        # 异循环调用在消费资源之前失败并保留状态；锁覆盖实际关闭过程；
        # 成功关闭后才清空实例与资源状态，正确循环内重复关闭与并发关闭安全。
        self._bind_loop()
        async with self._lock:
            if self._stack is None:
                self._retriever = None
                self._rerank = None
                self._rerank_identity = None
                return
            await self._stack.aclose()
            self._stack = None
            self._retriever = None
            self._rerank = None
            self._rerank_identity = None
