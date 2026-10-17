from app.application.business.retrieval_response import SEARCH_LIMIT

# T8 冻结参数接线：正式 runtime 的权重、两路召回深度与融合门槛。
# 本文件为声明式运行配置，不计入 run_identity 源码快照；取值由
# backend/temp/retrieval-evaluation/frozen-parameters.json 显式绑定，冻结前后核对取值与 SHA-256。
# 取值来自 T8 开发集网格选定（alpha=0.1、两路深度=100、门槛=0.0），由冻结账本绑定核验。
ALPHA = 0.1
BM25_DEPTH = 100
DENSE_DEPTH = 100
THRESHOLD: float | None = 0.0

# 线上向检索实例请求的候选数：最终返回 SEARCH_LIMIT 条，另留一条用于 has_more。
RECALL_LIMIT = SEARCH_LIMIT + 1


def frozen_values() -> dict:
    return {
        "alpha": ALPHA,
        "bm25_depth": BM25_DEPTH,
        "dense_depth": DENSE_DEPTH,
        "threshold": THRESHOLD,
        "recall_limit": RECALL_LIMIT,
    }
