import asyncio
import math
import sys
from concurrent.futures import CancelledError
from pathlib import Path
from threading import Event

from dotenv import load_dotenv

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.application.business import retrieval_frozen  # noqa: E402
from app.application.business.retrieval_conditions import (  # noqa: E402
    EvaluatedConditions,
)
from app.application.business.retrieval_embedding import EmbeddingClient  # noqa: E402
from app.application.business.retrieval_index import (  # noqa: E402
    MilvusConnection,
    RetrievalCorpus,
)
from app.application.business.retrieval_response import build_search_stage  # noqa: E402
from app.application.business.retrieval_runtime import (  # noqa: E402
    ActionRetrievalRuntime,
)
from app.application.business.retrieval_search import (  # noqa: E402
    HybridRetriever,
    SearchConditions,
)

QUERY = "卧推"


async def check() -> None:
    load_dotenv(BACKEND / ".env")
    runtime = ActionRetrievalRuntime()
    try:
        execution = await runtime.search(
            QUERY, limit=retrieval_frozen.RECALL_LIMIT
        )
        candidates = execution.candidates
        assert len(candidates) > 5, len(candidates)
        assert len(candidates) <= retrieval_frozen.RECALL_LIMIT, len(candidates)
        scores = [item.score for item in candidates]
        assert all(math.isfinite(score) and score > retrieval_frozen.THRESHOLD for score in scores), scores
        assert all(left >= right for left, right in zip(scores, scores[1:], strict=False)), scores
        assert [row.id for row in execution.rerank.scored] == [
            candidates[row.rrf_rank].id for row in execution.rerank.scored
        ]
        assert execution.rerank.usage.requests == 1
        assert execution.rerank.request_id

        evaluated = EvaluatedConditions(SearchConditions(), (), ())
        stage = build_search_stage(
            candidates,
            evaluated,
            rerank=execution.rerank,
            rerank_identity=execution.rerank_identity,
            threshold=retrieval_frozen.THRESHOLD,
        )
        assert len(stage.candidate_pool) == len(candidates)
        assert len(stage.final_input) <= 5
        assert len(stage.final_input) < len(stage.candidate_pool)
        assert [item.id for item in stage.final_input] == [
            item.id for item in stage.response.items
        ]

        async with MilvusConnection.from_env() as db:
            corpus = RetrievalCorpus.load()
            async with EmbeddingClient.from_env() as embedding:
                direct = (await (await HybridRetriever.open(db, corpus, embedding)).search(
                    QUERY,
                    rrf_k=retrieval_frozen.RRF_K,
                    bm25_depth=retrieval_frozen.BM25_DEPTH,
                    dense_depth=retrieval_frozen.DENSE_DEPTH,
                    limit=retrieval_frozen.RECALL_LIMIT,
                )).candidates
        direct_ids = [item.id for item in direct]
        direct_scores = [item.score for item in direct]
        assert 5 < len(direct) <= retrieval_frozen.RECALL_LIMIT, direct_ids
        assert len(set(direct_ids)) == len(direct_ids), direct_ids
        assert all(
            math.isfinite(score) and score > retrieval_frozen.THRESHOLD
            for score in direct_scores
        ), direct_scores
        assert all(
            left >= right
            for left, right in zip(direct_scores, direct_scores[1:], strict=False)
        ), direct_scores
        assert retrieval_frozen.RRF_K == 60
        assert retrieval_frozen.BM25_DEPTH == retrieval_frozen.DENSE_DEPTH == 100
        assert retrieval_frozen.RECALL_LIMIT == 20
        # 两次独立编码的近并列名次在 Top-20 截断处存在真实抖动，两条路径各自校验字段与约束。

        signal = Event()
        signal.set()
        try:
            await runtime.search(QUERY, signal=signal)
        except CancelledError:
            pass
        else:
            raise AssertionError("已初始化运行时的取消未传播")

        fresh = ActionRetrievalRuntime()
        try:
            try:
                await fresh.search(QUERY, signal=signal)
            except CancelledError:
                pass
            else:
                raise AssertionError("初始化阶段的取消未传播")
        finally:
            await fresh.close()
    finally:
        await runtime.close()
    print(
        f"runtime integration ok: candidates={len(candidates)} "
        f"stage_pool={len(stage.candidate_pool)} stage_final={len(stage.final_input)}"
    )


if __name__ == "__main__":
    asyncio.run(check())
