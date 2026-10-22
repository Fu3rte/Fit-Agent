import asyncio
import math
from concurrent.futures import CancelledError as FuturesCancelled
from threading import Event, Thread

import httpx
import pytest

from app.application.business.retrieval_config import RerankConfig
from app.application.business.retrieval_index import RetrievalCorpus
from app.application.business.retrieval_rerank import (
    MAX_DOCUMENTS,
    RerankClient,
    RerankDocument,
    RerankInputError,
    RerankItem,
    RerankRequest,
    RerankResponseError,
    RerankUsage,
    build_request,
    parse_response,
    parse_result,
    validate_request,
)
from app.application.business.retrieval_search import RetrievalCandidate

# 端口 9 无可达服务：空池与取消路径若发起请求会直接暴露连接错误。
UNREACHABLE = (
    "https://127.0.0.1:9/api/v1/services/rerank/text-rerank/text-rerank"
)


@pytest.fixture(scope="module")
def corpus() -> RetrievalCorpus:
    return RetrievalCorpus.load()


def client_config(timeout: float = 0.5) -> RerankConfig:
    return RerankConfig(
        api_key="unit-secret",
        endpoint=UNREACHABLE,
        model="qwen3.7-text-rerank",
        timeout=timeout,
    )


def candidates(corpus: RetrievalCorpus, count: int) -> list[RetrievalCandidate]:
    return [
        RetrievalCandidate(
            id=record["id"],
            score=float(count - index),
            name=corpus.catalog.get(record["id"]).name,
            equipment=record["equipment"],
            body_part=record["body_part"],
            target=record["target"],
            muscle_group=record["muscle_group"],
        )
        for index, record in enumerate(corpus.records[:count])
    ]


def payload(
    results: list[dict],
    *,
    prompt_tokens: object = 40,
    total_tokens: object = 40,
    request_id: object = "request-1",
) -> dict:
    return {
        "output": {"results": results},
        "usage": {"prompt_tokens": prompt_tokens, "total_tokens": total_tokens},
        "request_id": request_id,
    }


def test_from_env_reuses_rerank_config_loading(monkeypatch):
    monkeypatch.setenv("RERANKER_MODEL", "qwen3.7-text-rerank")
    monkeypatch.setenv("RERANKER_BASE_URL", UNREACHABLE)
    monkeypatch.setenv("RERANKER_API_KEY", "unit-secret")
    client = RerankClient.from_env()
    assert client.config.model == "qwen3.7-text-rerank"
    assert client.config.endpoint == UNREACHABLE
    assert "unit-secret" not in repr(client.config)


def test_validate_request_preserves_published_text_and_order(corpus):
    request = build_request(" 卧推 ", candidates(corpus, 3), corpus.records)
    validated = validate_request(request)
    assert validated.query == "卧推"
    assert [document.id for document in validated.documents] == [
        document.id for document in request.documents
    ]
    assert [document.text for document in validated.documents] == [
        document.text for document in request.documents
    ]


def test_validate_request_rejects_invalid_input(corpus):
    records = corpus.records
    with pytest.raises(RerankInputError):
        validate_request("卧推")
    with pytest.raises(RerankInputError):
        validate_request(RerankRequest("", ()))
    with pytest.raises(RerankInputError):
        validate_request(RerankRequest("卧推", [RerankDocument("a", "text")]))
    with pytest.raises(RerankInputError):
        validate_request(
            RerankRequest(
                "卧推",
                (RerankDocument("a", "text"), RerankDocument("a", "other")),
            )
        )
    with pytest.raises(RerankInputError):
        validate_request(
            RerankRequest(
                "卧推",
                tuple(
                    RerankDocument(record["id"], record["text"])
                    for record in records[: MAX_DOCUMENTS + 1]
                ),
            )
        )
    with pytest.raises(RerankInputError):
        validate_request(RerankRequest("卧推", (RerankDocument("a", "   "),)))
    with pytest.raises(RerankInputError):
        validate_request(RerankRequest("卧推", (object(),)))


def test_parse_response_maps_indices_and_usage(corpus):
    request = build_request("卧推", candidates(corpus, 3), corpus.records)
    result = parse_response(
        request,
        payload(
            [
                {"index": 2, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.4},
                {"index": 1, "relevance_score": 0.7},
            ],
            prompt_tokens=31,
            total_tokens=57,
            request_id="request-abc",
        ),
    )
    documents = request.documents
    assert [row.id for row in result.scored] == [
        documents[2].id,
        documents[1].id,
        documents[0].id,
    ]
    assert [row.rrf_rank for row in result.scored] == [2, 1, 0]
    assert result.usage.requests == 1
    assert result.usage.prompt_tokens == 31
    assert result.usage.total_tokens == 57
    assert result.request_id == "request-abc"


def test_parse_response_accepts_missing_prompt_tokens(corpus):
    request = build_request("卧推", candidates(corpus, 2), corpus.records)
    result = parse_response(
        request,
        {
            "output": {
                "results": [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 1, "relevance_score": 0.2},
                ]
            },
            "usage": {"total_tokens": 229},
            "request_id": "request-missing-prompt",
        },
    )
    assert result.usage.prompt_tokens is None
    assert result.usage.total_tokens == 229
    assert result.usage.requests == 1
    assert result.request_id == "request-missing-prompt"
    assert [row.rrf_rank for row in result.scored] == [0, 1]


def test_parse_response_keeps_rrf_order_on_ties(corpus):
    request = build_request("卧推", candidates(corpus, 3), corpus.records)
    result = parse_response(
        request,
        payload(
            [
                {"index": 2, "relevance_score": 0.5},
                {"index": 1, "relevance_score": 0.5},
                {"index": 0, "relevance_score": 0.5},
            ]
        ),
    )
    assert [row.rrf_rank for row in result.scored] == [0, 1, 2]
    assert [row.id for row in result.scored] == [
        document.id for document in request.documents
    ]


@pytest.mark.parametrize(
    "response",
    [
        [],
        {"output": {"results": []}, "usage": {}, "request_id": "request-1"},
        {"output": [], "usage": {"prompt_tokens": 1, "total_tokens": 1}, "request_id": "request-1"},
        {"output": {"results": {}}, "usage": {"prompt_tokens": 1, "total_tokens": 1}, "request_id": "request-1"},
        {"output": {"results": [1]}, "usage": {"prompt_tokens": 1, "total_tokens": 1}, "request_id": "request-1"},
        {"output": {"results": [{"index": 0}]}, "usage": {"prompt_tokens": 1, "total_tokens": 1}, "request_id": "request-1"},
        {"output": {"results": [{"relevance_score": 0.5}]}, "usage": {"prompt_tokens": 1, "total_tokens": 1}, "request_id": "request-1"},
        {"output": {"results": []}, "usage": [], "request_id": "request-1"},
        {"output": {"results": []}, "usage": {"prompt_tokens": 1}, "request_id": "request-1"},
        {"output": {"results": []}, "usage": {"prompt_tokens": None, "total_tokens": 1}, "request_id": "request-1"},
        {"output": {"results": []}, "usage": {"prompt_tokens": 1.0, "total_tokens": 1}, "request_id": "request-1"},
        {"output": {"results": []}, "usage": {"prompt_tokens": True, "total_tokens": 1}, "request_id": "request-1"},
        {"output": {"results": []}, "usage": {"prompt_tokens": 1, "total_tokens": "1"}, "request_id": "request-1"},
        {"output": {"results": []}, "usage": {"prompt_tokens": 1, "total_tokens": 1}},
        {"output": {"results": []}, "usage": {"prompt_tokens": 1, "total_tokens": 1}, "request_id": ""},
        {"output": {"results": []}, "usage": {"prompt_tokens": 1, "total_tokens": 1}, "request_id": 7},
    ],
)
def test_parse_response_rejects_invalid_structure(corpus, response):
    request = build_request("卧推", [], corpus.records)
    with pytest.raises(RerankResponseError):
        parse_response(request, response)


@pytest.mark.parametrize(
    "row",
    [
        {"index": True, "relevance_score": 0.5},
        {"index": "0", "relevance_score": 0.5},
        {"index": 3, "relevance_score": 0.5},
        {"index": 0, "relevance_score": True},
        {"index": 0, "relevance_score": "0.5"},
        {"index": 0, "relevance_score": math.nan},
        {"index": 0, "relevance_score": 1.5},
        {"index": 0, "relevance_score": -0.1},
    ],
)
def test_parse_response_rejects_invalid_index_or_score(corpus, row):
    request = build_request("卧推", candidates(corpus, 3), corpus.records)
    with pytest.raises(RerankResponseError):
        parse_response(
            request,
            payload([{"index": 1, "relevance_score": 0.5}, {"index": 2, "relevance_score": 0.5}, row]),
        )


def test_parse_response_rejects_duplicate_or_missing_indices(corpus):
    request = build_request("卧推", candidates(corpus, 3), corpus.records)
    with pytest.raises(RerankResponseError):
        parse_response(
            request,
            payload(
                [
                    {"index": 0, "relevance_score": 0.5},
                    {"index": 0, "relevance_score": 0.6},
                    {"index": 2, "relevance_score": 0.7},
                ]
            ),
        )
    with pytest.raises(RerankResponseError):
        parse_response(
            request,
            payload(
                [
                    {"index": 0, "relevance_score": 0.5},
                    {"index": 1, "relevance_score": 0.6},
                ]
            ),
        )


def test_same_score_ordering_is_enforced_by_parse_result(corpus):
    request = build_request("卧推", candidates(corpus, 3), corpus.records)
    result = parse_result(
        request,
        [RerankItem(1, 0.8), RerankItem(0, 0.8), RerankItem(2, 0.9)],
        RerankUsage(1, 10, 10),
        "request-1",
    )
    assert [row.rrf_rank for row in result.scored] == [2, 0, 1]


async def test_empty_pool_returns_without_request(corpus):
    request = build_request("卧推", [], corpus.records)
    async with RerankClient(client_config()) as client:
        result = await client.rerank(request)
    assert result.scored == ()
    assert result.usage.requests == 0
    assert result.usage.prompt_tokens == 0
    assert result.usage.total_tokens == 0
    assert result.request_id is None


async def test_connection_failure_propagates_instead_of_empty_result(corpus):
    request = build_request("卧推", candidates(corpus, 1), corpus.records)
    async with RerankClient(client_config(timeout=1.0)) as client:
        with pytest.raises(httpx.TransportError):
            await client.rerank(request)


async def test_pre_cancelled_signal_fails_before_request(corpus):
    request = build_request("卧推", candidates(corpus, 1), corpus.records)
    signal = Event()
    signal.set()
    async with RerankClient(client_config()) as client:
        with pytest.raises(FuturesCancelled):
            await client.rerank(request, signal=signal)


async def test_client_requires_context_and_single_entry(corpus):
    client = RerankClient(client_config())
    request = build_request("卧推", [], corpus.records)
    with pytest.raises(RuntimeError):
        await client.rerank(request)
    async with client:
        with pytest.raises(RuntimeError):
            await client.__aenter__()
        assert await client.rerank(request) == parse_result(
            request, [], RerankUsage(), None
        )


async def test_client_can_reenter_after_close(corpus):
    client = RerankClient(client_config())
    request = build_request("卧推", [], corpus.records)
    async with client:
        assert client._client is not None
    assert client._client is None
    async with client:
        assert (await client.rerank(request)).usage.requests == 0
    assert client._client is None


async def test_foreign_loop_use_and_close_leave_original_resources(corpus):
    client = RerankClient(client_config())
    request = build_request("卧推", [], corpus.records)
    async with client:
        original = client._client
        outcome: dict[str, str] = {}

        def foreign() -> None:
            async def run() -> None:
                try:
                    await client.rerank(request)
                    outcome["rerank"] = "allowed"
                except RuntimeError:
                    outcome["rerank"] = "rejected"
                try:
                    await client.__aexit__(None, None, None)
                    outcome["close"] = "allowed"
                except RuntimeError:
                    outcome["close"] = "rejected"

            asyncio.run(run())

        thread = Thread(target=foreign)
        thread.start()
        thread.join()
        assert outcome == {"rerank": "rejected", "close": "rejected"}
        assert client._client is original
        assert (await client.rerank(request)).usage.requests == 0
    assert client._client is None
