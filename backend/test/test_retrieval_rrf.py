import asyncio
import inspect
import json
import math

import pytest
from pymilvus import RRFRanker

from app.application.business import retrieval_frozen
from app.application.business.retrieval_index import (
    IndexCompatibilityError,
    MilvusConnection,
    RetrievalCorpus,
    published_collection,
)
from app.application.business.retrieval_search import (
    HybridRetriever,
    SearchConditions,
    search_vectors,
    validate_parameters,
)


@pytest.mark.parametrize("rrf_k", [True, False, None, "60", 0, -1, 60.0, 16384, math.nan, math.inf])
def test_rejects_invalid_rrf_k(rrf_k):
    with pytest.raises(IndexCompatibilityError, match="rrf_k"):
        validate_parameters("杠铃卧推", rrf_k, 11, 100, 100)


@pytest.mark.parametrize("rrf_k", [1, 60, 16383])
def test_accepts_rrf_k(rrf_k):
    assert validate_parameters(" BARBELL bench press ", rrf_k, 11, 100, 100) == "barbell bench press"
    assert RRFRanker(k=rrf_k).dict() == {"strategy": "rrf", "params": {"k": rrf_k}}


def test_runtime_rrf_baseline():
    assert retrieval_frozen.frozen_values() == {
        "fusion": "rrf",
        "rrf_k": 60,
        "bm25_depth": 100,
        "dense_depth": 100,
        "threshold": 0.0,
        "candidate_pool_limit": 20,
        "final_input_limit": 5,
        "recall_limit": 20,
    }
    for function in (search_vectors, HybridRetriever.search):
        parameters = inspect.signature(function).parameters
        assert parameters["rrf_k"].default == retrieval_frozen.RRF_K
        assert "alpha" not in parameters


@pytest.mark.parametrize("name", ["limit", "bm25_depth", "dense_depth"])
def test_preserves_depth_validation(name):
    parameters = {"rrf_k": 60, "limit": 11, "bm25_depth": 100, "dense_depth": 100}
    parameters[name] = 0
    with pytest.raises(IndexCompatibilityError, match=name):
        validate_parameters("卧推", **parameters)


async def verify_published_rrf():
    # 复用真实发布向量，检查服务端 RRF 与单路排名；不调用 Embedding，不写集合。
    corpus = RetrievalCorpus.load()
    values = retrieval_frozen.frozen_values()
    async with MilvusConnection.from_env() as db:
        collection = await published_collection(db)
        exercise = corpus.catalog.all()[0]
        records = await db.call(
            "get", collection, ids=[exercise.id], output_fields=["dense"], consistency_level="Strong"
        )
        assert len(records) == 1
        vector = records[0]["dense"]
        cases = []
        for conditions in (
            SearchConditions(),
            SearchConditions(equipment=exercise.equipment, excluded_ids=(exercise.id,)),
            SearchConditions(available_equipment=()),
        ):
            rankings = {}
            for mode, depth in (("bm25", values["bm25_depth"]), ("dense", values["dense_depth"])):
                rankings[mode] = await search_vectors(
                    db, collection, corpus.catalog, exercise.name, vector,
                    conditions=conditions, rrf_k=values["rrf_k"],
                    bm25_depth=values["bm25_depth"], dense_depth=values["dense_depth"],
                    limit=depth, mode=mode,
                )
            expected = {}
            for ranking in rankings.values():
                for rank, candidate in enumerate(ranking, 1):
                    expected[candidate.id] = expected.get(candidate.id, 0.0) + 1.0 / (values["rrf_k"] + rank)
            hybrid = await search_vectors(
                db, collection, corpus.catalog, exercise.name, vector,
                conditions=conditions, rrf_k=values["rrf_k"],
                bm25_depth=values["bm25_depth"], dense_depth=values["dense_depth"],
                limit=values["recall_limit"],
            )
            assert len(hybrid) == min(len(expected), values["recall_limit"])
            expected_scores = sorted(expected.values(), reverse=True)[:len(hybrid)]
            for candidate, score in zip(hybrid, expected_scores, strict=True):
                assert conditions.matches(corpus.catalog.get(candidate.id))
                assert candidate.score == pytest.approx(expected[candidate.id], abs=1e-6)
                assert candidate.score == pytest.approx(score, abs=1e-6)
            cases.append({
                "filter": conditions.expression(corpus.catalog),
                "counts": {mode: len(items) for mode, items in rankings.items()},
                "hybrid": [{"id": item.id, "score": item.score} for item in hybrid],
            })
        assert cases[0]["hybrid"]
        assert cases[1]["hybrid"]
        assert cases[2]["hybrid"] == []
        print(json.dumps({"parameters": values, "cases": cases, "embedding_requests": 0}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(verify_published_rrf())
