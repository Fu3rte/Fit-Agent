from app.application.business.retrieval_response import (
    CANDIDATE_POOL_LIMIT,
    SEARCH_LIMIT,
)

# 正式 runtime 与评测入口共用的 RRF 基线参数。
FUSION = "rrf"
RRF_K = 60
BM25_DEPTH = 100
DENSE_DEPTH = 100
THRESHOLD: float | None = 0.0

# 线上向检索实例请求的候选数与阶段上限相互独立。
RECALL_LIMIT = CANDIDATE_POOL_LIMIT


def frozen_values() -> dict:
    return {
        "fusion": FUSION,
        "rrf_k": RRF_K,
        "bm25_depth": BM25_DEPTH,
        "dense_depth": DENSE_DEPTH,
        "threshold": THRESHOLD,
        "candidate_pool_limit": CANDIDATE_POOL_LIMIT,
        "final_input_limit": SEARCH_LIMIT,
        "recall_limit": RECALL_LIMIT,
    }
