import math

import pytest

from app.application.business.retrieval_conditions import EvaluatedConditions
from app.application.business.retrieval_config import (
    DEFAULT_RERANK_TIMEOUT,
    MissingRerankConfig,
    RetrievalConfigError,
    load_embedding_config,
    load_rerank_config,
)
from app.application.business.retrieval_rerank import (
    MAX_DOCUMENT_BYTES,
    MAX_DOCUMENTS,
    MAX_QUERY_CHARS,
    RerankInputError,
    RerankItem,
    RerankResponseError,
    RerankUsage,
    build_request,
    parse_result,
)
from app.application.business.retrieval_response import build_search_stage
from app.application.business.retrieval_search import (
    RetrievalCandidate,
    SearchConditions,
)

ENDPOINT = (
    "https://ws-example.cn-beijing.maas.aliyuncs.com"
    "/api/v1/services/rerank/text-rerank/text-rerank"
)
VALID = {
    "RERANKER_MODEL": "qwen3.7-text-rerank",
    "RERANKER_BASE_URL": ENDPOINT,
    "RERANKER_API_KEY": "test-secret-value",
}


def candidate(index: int) -> RetrievalCandidate:
    return RetrievalCandidate(
        id=f"{index:04d}",
        score=float(100 - index),
        name=f"动作{index:04d}",
        equipment="body weight",
        body_part="back",
        target="lats",
        muscle_group="back",
    )


def records(count: int) -> list[dict]:
    return [{"id": f"{index:04d}", "text": f"动作 {index:04d} 的已发布文本"} for index in range(1, count + 1)]


def test_rerank_config_reads_complete_request_address_and_default_timeout():
    config = load_rerank_config(VALID)
    assert config.endpoint == ENDPOINT
    assert config.model == "qwen3.7-text-rerank"
    assert config.timeout == DEFAULT_RERANK_TIMEOUT > 0
    assert "test-secret-value" not in repr(config)


def test_rerank_config_timeout_override_is_finite_positive():
    config = load_rerank_config({**VALID, "RERANKER_TIMEOUT": "2.5"})
    assert config.timeout == 2.5


@pytest.mark.parametrize("name", ["RERANKER_MODEL", "RERANKER_BASE_URL", "RERANKER_API_KEY"])
def test_rerank_config_missing_or_blank_fails_without_values(name):
    for value in (None, "", "   "):
        environ = dict(VALID)
        if value is None:
            environ.pop(name)
        else:
            environ[name] = value
        with pytest.raises(MissingRerankConfig) as error:
            load_rerank_config(environ)
        assert error.value.variables == (name,)
        assert "test-secret-value" not in str(error.value)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://ws-example.cn-beijing.maas.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank",
        "ws-example.cn-beijing.maas.aliyuncs.com/api/v1/services/rerank",
        "https://ws-example.cn-beijing.maas.aliyuncs.com",
        "https://ws-example.cn-beijing.maas.aliyuncs.com/api/v1?model=x",
        "https://ws-example.cn-beijing.maas.aliyuncs.com/api/v1#fragment",
        "https://user:pass@ws-example.cn-beijing.maas.aliyuncs.com/api/v1",
    ],
)
def test_rerank_config_rejects_invalid_request_address(endpoint):
    with pytest.raises(RetrievalConfigError) as error:
        load_rerank_config({**VALID, "RERANKER_BASE_URL": endpoint})
    assert "test-secret-value" not in str(error.value)


@pytest.mark.parametrize("timeout", ["0", "-1", "abc", "nan", "inf"])
def test_rerank_config_rejects_invalid_timeout(timeout):
    with pytest.raises(RetrievalConfigError):
        load_rerank_config({**VALID, "RERANKER_TIMEOUT": timeout})


def test_embedding_config_loading_is_unchanged():
    config = load_embedding_config(
        {
            "DASHSCOPE_API_KEY": "test-secret-value",
            "DASHSCOPE_WORKSPACE_ID": "ws-example",
        }
    )
    assert config.model == "qwen3.7-text-embedding-flash"
    assert config.dimensions == 1024


def test_build_request_preserves_rrf_order_and_published_text():
    request = build_request("  卧推  ", [candidate(2), candidate(1)], records(3))
    assert request.query == "卧推"
    assert [document.id for document in request.documents] == ["0002", "0001"]
    assert [document.text for document in request.documents] == [
        "动作 0002 的已发布文本",
        "动作 0001 的已发布文本",
    ]


def test_build_request_rejects_incomplete_or_duplicated_candidates():
    with pytest.raises(RerankInputError):
        build_request("卧推", [candidate(1), candidate(1)], records(3))
    with pytest.raises(RerankInputError):
        build_request("卧推", [candidate(4)], records(3))
    with pytest.raises(RerankInputError):
        build_request("卧推", [candidate(index) for index in range(1, MAX_DOCUMENTS + 2)], records(30))


@pytest.mark.parametrize("query", ["", "   ", "x" * (MAX_QUERY_CHARS + 1)])
def test_build_request_rejects_invalid_query(query):
    with pytest.raises(RerankInputError):
        build_request(query, [candidate(1)], records(3))


def test_build_request_rejects_invalid_document_text():
    broken = [{"id": "0001", "text": "   "}]
    with pytest.raises(RerankInputError):
        build_request("卧推", [candidate(1)], broken)
    oversized = [{"id": "0001", "text": "a" * (MAX_DOCUMENT_BYTES + 1)}]
    with pytest.raises(RerankInputError):
        build_request("卧推", [candidate(1)], oversized)


def test_parse_result_orders_descending_and_keeps_rrf_order_on_ties():
    request = build_request("卧推", [candidate(1), candidate(2), candidate(3)], records(3))
    result = parse_result(
        request,
        [RerankItem(0, 0.5), RerankItem(1, 0.5), RerankItem(2, 0.9)],
        RerankUsage(requests=1, prompt_tokens=30, total_tokens=30),
        "request-id",
    )
    assert [row.id for row in result.scored] == ["0003", "0001", "0002"]
    assert [row.rrf_rank for row in result.scored] == [2, 0, 1]
    assert result.request_id == "request-id"
    assert result.usage.requests == 1


@pytest.mark.parametrize(
    "items",
    [
        [RerankItem(0, 0.9), RerankItem(0, 0.8), RerankItem(2, 0.7)],
        [RerankItem(0, 0.9), RerankItem(1, 0.8)],
        [RerankItem(0, 0.9), RerankItem(1, 0.8), RerankItem(3, 0.7)],
        [RerankItem(0, 0.9), RerankItem(1, 0.8), RerankItem(2, math.nan)],
        [RerankItem(0, 0.9), RerankItem(1, 0.8), RerankItem(2, 1.1)],
        [RerankItem(0, 0.9), RerankItem(1, 0.8), RerankItem(2, -0.1)],
    ],
)
def test_parse_result_rejects_invalid_indices_or_scores(items):
    request = build_request("卧推", [candidate(index) for index in range(1, 4)], records(3))
    with pytest.raises(RerankResponseError):
        parse_result(request, items, RerankUsage(1, 30, 30), "request-id")


def test_parse_result_accepts_unreported_prompt_tokens():
    request = build_request("卧推", [candidate(1)], records(3))
    result = parse_result(
        request,
        [RerankItem(0, 0.9)],
        RerankUsage(requests=1, prompt_tokens=None, total_tokens=10),
        "request-id",
    )
    assert result.usage.prompt_tokens is None
    assert result.usage.total_tokens == 10
    assert result.usage.requests == 1
    assert result.request_id == "request-id"


@pytest.mark.parametrize(
    "usage",
    [
        RerankUsage(requests=-1, prompt_tokens=30, total_tokens=30),
        RerankUsage(requests=1, prompt_tokens=-1, total_tokens=30),
        RerankUsage(requests=1, prompt_tokens=30, total_tokens=-1),
        RerankUsage(requests=1, prompt_tokens=True, total_tokens=30),
    ],
)
def test_parse_result_rejects_invalid_usage(usage):
    request = build_request("卧推", [candidate(1)], records(3))
    with pytest.raises(RerankResponseError):
        parse_result(request, [RerankItem(0, 0.9)], usage, "request-id")


def test_empty_pool_skips_model_result():
    request = build_request("卧推", [], records(3))
    result = parse_result(request, [], RerankUsage(), None)
    assert result.scored == ()
    assert result.usage == RerankUsage()
    with pytest.raises(RerankResponseError):
        parse_result(request, [RerankItem(0, 0.9)], RerankUsage(), None)


def test_existing_stage_construction_remains_compatible():
    stage = build_search_stage(
        [candidate(index) for index in range(1, 26)],
        EvaluatedConditions(SearchConditions(), (), ()),
        rerank=None,
        threshold=0.0,
    )
    assert len(stage.candidate_pool) == MAX_DOCUMENTS
    assert len(stage.final_input) == 5
