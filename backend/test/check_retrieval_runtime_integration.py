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
        candidates = (
            await runtime.search(QUERY, limit=retrieval_frozen.RECALL_LIMIT)
        ).candidates
        assert len(candidates) > 5, len(candidates)
        assert len(candidates) <= retrieval_frozen.RECALL_LIMIT, len(candidates)
        scores = [item.score for item in candidates]
        assert all(math.isfinite(score) and score > retrieval_frozen.THRESHOLD for score in scores), scores
        assert all(left >= right for left, right in zip(scores, scores[1:], strict=False)), scores

        evaluated = EvaluatedConditions(SearchConditions(), (), ())
        stage = build_search_stage(candidates, evaluated, threshold=retrieval_frozen.THRESHOLD)
        assert len(stage.candidate_pool) == len(candidates)
        assert len(stage.final_input) <= 5
        assert len(stage.final_input) < len(stage.candidate_pool)

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
        runtime_ids = [item.id for item in candidates]
        direct_ids = [item.id for item in direct]
        assert retrieval_frozen.RRF_K == 60
        assert retrieval_frozen.BM25_DEPTH == retrieval_frozen.DENSE_DEPTH == 100
        assert retrieval_frozen.RECALL_LIMIT == 20
        # 独立真实编码存在浮点非确定性，近邻并列只允许名次抖动，不允许候选集合差异。
        assert set(runtime_ids) == set(direct_ids), (runtime_ids, direct_ids)

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
