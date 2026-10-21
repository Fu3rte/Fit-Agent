import asyncio
import math
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from threading import Event
from typing import Any

import httpx

from app.ai.types import check_cancelled
from app.application.business import retrieval_text as rt
from app.application.business.retrieval_config import RerankConfig, load_rerank_config
from app.application.business.retrieval_response import CANDIDATE_POOL_LIMIT
from app.application.business.retrieval_search import RetrievalCandidate

# 重排输入上限取候选池上限；服务端文档数上限 500 与单项 30000 Token 由官方协议约束。
MAX_DOCUMENTS = CANDIDATE_POOL_LIMIT
# 查询按字符保守限定；Service 端 4000 Token 上限仍是权威校验。
MAX_QUERY_CHARS = 4000
# 与 Milvus text 字段容量一致，复用已发布语料原文，不做截断。
MAX_DOCUMENT_BYTES = 4096


class RerankInputError(ValueError):
    pass


class RerankResponseError(ValueError):
    pass


@dataclass(frozen=True)
class RerankDocument:
    id: str
    text: str


@dataclass(frozen=True)
class RerankRequest:
    query: str
    documents: tuple[RerankDocument, ...]


@dataclass(frozen=True)
class RerankItem:
    # 服务返回的原始索引与相关性分数；索引指向本次 documents 的位置。
    index: int
    score: float


@dataclass(frozen=True)
class RerankUsage:
    requests: int = 0
    prompt_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class RerankScored:
    # rrf_rank 为输入 RRF 顺序；重排排序值独立保存，不覆盖原 RRF 分数。
    id: str
    rerank_score: float
    rrf_rank: int


@dataclass(frozen=True)
class RerankResult:
    scored: tuple[RerankScored, ...]
    usage: RerankUsage
    request_id: str | None = None


@dataclass(frozen=True)
class RerankIdentity:
    # 单次检索的重排配置身份；不含 API Key 与超时，供阶段证据与后续评测使用。
    model: str
    endpoint: str
    text_version: str

    @classmethod
    def from_config(cls, config: RerankConfig) -> "RerankIdentity":
        return cls(
            model=config.model,
            endpoint=config.endpoint,
            text_version=rt.ENCODING_RULE_VERSION,
        )


def _prepare_query(query: object) -> str:
    if not isinstance(query, str):
        raise RerankInputError("重排查询必须是字符串")
    prepared = rt.normalize(query)
    if not prepared:
        raise RerankInputError("重排查询不得为空")
    if len(prepared) > MAX_QUERY_CHARS:
        raise RerankInputError(f"重排查询不得超过 {MAX_QUERY_CHARS} 个字符")
    return prepared


def _prepare_document(exercise_id: object, text: object) -> RerankDocument:
    if not isinstance(exercise_id, str) or not exercise_id.strip():
        raise RerankInputError("重排候选 ID 必须为非空字符串")
    if not isinstance(text, str) or not text.strip():
        raise RerankInputError("重排候选文本必须为非空字符串")
    if len(text.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise RerankInputError(f"重排候选文本不得超过 {MAX_DOCUMENT_BYTES} 字节")
    return RerankDocument(exercise_id, text)


def build_request(
    query: object,
    candidates: Sequence[RetrievalCandidate],
    records: Sequence[Mapping[str, Any]],
) -> RerankRequest:
    # 候选顺序即原 RRF 顺序；动作文本只取已发布语料记录，缺失或重复直接失败。
    prepared = _prepare_query(query)
    if len(candidates) > MAX_DOCUMENTS:
        raise RerankInputError(f"重排候选不得超过 {MAX_DOCUMENTS} 条")
    texts = {record["id"]: record["text"] for record in records}
    documents: list[RerankDocument] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.id in seen:
            raise RerankInputError(f"重排候选 ID 重复: {candidate.id}")
        if candidate.id not in texts:
            raise RerankInputError(f"重排候选不在已发布语料中: {candidate.id}")
        seen.add(candidate.id)
        documents.append(_prepare_document(candidate.id, texts[candidate.id]))
    return RerankRequest(prepared, tuple(documents))


def _require_usage(usage: RerankUsage) -> RerankUsage:
    for name in ("requests", "prompt_tokens", "total_tokens"):
        value = getattr(usage, name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RerankResponseError(f"重排 usage.{name} 必须为非负整数")
    return usage


def parse_result(
    request: RerankRequest,
    items: Sequence[RerankItem],
    usage: RerankUsage,
    request_id: str | None,
) -> RerankResult:
    # 空候选池不调用模型；非空请求要求返回索引唯一、在范围内并完整覆盖输入。
    if not request.documents:
        if items:
            raise RerankResponseError("空候选池不得返回重排结果")
        return RerankResult((), RerankUsage(), None)

    if len(items) != len(request.documents):
        raise RerankResponseError(
            f"重排返回数量 {len(items)} 与输入数量 {len(request.documents)} 不一致"
        )
    slots: list[RerankScored | None] = [None] * len(request.documents)
    for item in items:
        if isinstance(item.index, bool) or not isinstance(item.index, int):
            raise RerankResponseError("重排索引必须为整数")
        if item.index < 0 or item.index >= len(request.documents):
            raise RerankResponseError(f"重排索引越界: {item.index}")
        if slots[item.index] is not None:
            raise RerankResponseError(f"重排索引重复: {item.index}")
        if isinstance(item.score, bool) or not isinstance(item.score, (int, float)):
            raise RerankResponseError("重排分数必须为数值")
        score = float(item.score)
        if not math.isfinite(score):
            raise RerankResponseError("重排分数必须为有限值")
        if not 0.0 <= score <= 1.0:
            raise RerankResponseError("重排分数必须位于 0 到 1 之间")
        slots[item.index] = RerankScored(
            id=request.documents[item.index].id,
            rerank_score=score,
            rrf_rank=item.index,
        )
    missing = [index for index, slot in enumerate(slots) if slot is None]
    if missing:
        raise RerankResponseError(f"缺少重排索引: {missing}")

    scored = [slot for slot in slots if slot is not None]
    # 分数降序；同分保持输入 RRF 相对顺序。
    scored.sort(key=lambda row: (-row.rerank_score, row.rrf_rank))
    return RerankResult(tuple(scored), _require_usage(usage), request_id)


def validate_request(request: object) -> RerankRequest:
    # 客户端入口校验：复用既有纯校验，拒绝非元组候选、重复 ID 与超限输入。
    if not isinstance(request, RerankRequest):
        raise RerankInputError("重排请求类型必须为 RerankRequest")
    query = _prepare_query(request.query)
    if not isinstance(request.documents, tuple):
        raise RerankInputError("重排候选必须为元组")
    if len(request.documents) > MAX_DOCUMENTS:
        raise RerankInputError(f"重排候选不得超过 {MAX_DOCUMENTS} 条")
    documents: list[RerankDocument] = []
    seen: set[str] = set()
    for document in request.documents:
        if not isinstance(document, RerankDocument):
            raise RerankInputError("重排候选类型必须为 RerankDocument")
        if document.id in seen:
            raise RerankInputError(f"重排候选 ID 重复: {document.id}")
        seen.add(document.id)
        documents.append(_prepare_document(document.id, document.text))
    return RerankRequest(query, tuple(documents))


def parse_response(request: RerankRequest, payload: object) -> RerankResult:
    # 响应纯校验：结构缺失或类型非法明确失败；索引与分数保留原始类型，交由 parse_result 校验。
    if not isinstance(payload, Mapping):
        raise RerankResponseError("重排响应必须为 JSON 对象")
    output = payload.get("output")
    if not isinstance(output, Mapping):
        raise RerankResponseError("重排响应 output 必须为对象")
    results = output.get("results")
    if not isinstance(results, list):
        raise RerankResponseError("重排响应 output.results 必须为数组")
    items: list[RerankItem] = []
    for row in results:
        if not isinstance(row, Mapping):
            raise RerankResponseError("重排响应结果项必须为对象")
        if "index" not in row or "relevance_score" not in row:
            raise RerankResponseError("重排响应结果项缺少 index 或 relevance_score")
        items.append(RerankItem(row["index"], row["relevance_score"]))

    usage_payload = payload.get("usage")
    if not isinstance(usage_payload, Mapping):
        raise RerankResponseError("重排响应 usage 必须为对象")
    for name in ("prompt_tokens", "total_tokens"):
        value = usage_payload.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise RerankResponseError(f"重排响应 usage.{name} 必须为整数")

    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        raise RerankResponseError("重排响应 request_id 必须为非空字符串")

    usage = RerankUsage(
        requests=1,
        prompt_tokens=usage_payload["prompt_tokens"],
        total_tokens=usage_payload["total_tokens"],
    )
    return parse_result(request, items, usage, request_id)


# threading.Event 无异步等待；与编码路径相同的轮询间隔用于尽早中止在途请求。
RERANK_CANCEL_POLL_SECONDS = 0.05


class RerankClient:
    # 异步生命周期：创建、请求与关闭属于同一事件循环；跨循环调用在触碰资源前失败。
    def __init__(self, config: RerankConfig) -> None:
        if not isinstance(config, RerankConfig):
            raise TypeError("RerankClient 需要 RerankConfig")
        self._config = config
        self._client: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @classmethod
    def from_env(cls) -> "RerankClient":
        return cls(load_rerank_config())

    async def __aenter__(self) -> "RerankClient":
        if self._client is not None:
            raise RuntimeError("RerankClient 已在上下文中")
        loop = asyncio.get_running_loop()
        self._client = httpx.AsyncClient(
            timeout=self._config.timeout,
            transport=httpx.AsyncHTTPTransport(retries=0),
        )
        self._loop = loop
        return self

    async def __aexit__(self, exc_type, exc, exc_tb) -> None:
        client = self._client
        if client is None:
            return
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("RerankClient 必须在所属事件循环中关闭")
        await client.aclose()
        self._client = None
        self._loop = None

    @property
    def config(self) -> RerankConfig:
        return self._config

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("RerankClient 必须在 async with 上下文中使用")
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("RerankClient 必须在所属事件循环中使用")
        return self._client

    async def rerank(
        self, request: RerankRequest, *, signal: Event | None = None
    ) -> RerankResult:
        # 顺序：输入校验、上下文、空池、取消；非空请求才构造并发送 HTTP。
        validated = validate_request(request)
        client = self._require_client()
        if not validated.documents:
            return parse_result(validated, (), RerankUsage(), None)
        check_cancelled(signal)
        payload = {
            "model": self._config.model,
            "input": {
                "query": validated.query,
                "documents": [document.text for document in validated.documents],
            },
        }
        response = await self._post(client, payload, signal)
        return parse_response(validated, response)

    async def _post(
        self, client: httpx.AsyncClient, payload: dict, signal: Event | None
    ) -> object:
        async def send() -> object:
            response = await client.post(
                self._config.endpoint,
                headers={
                    "Authorization": f"Bearer {self._config.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            return response.json()

        if signal is None:
            return await send()
        task = asyncio.ensure_future(send())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {task}, timeout=RERANK_CANCEL_POLL_SECONDS
                )
                if task in done:
                    return task.result()
                if signal.is_set():
                    task.cancel()
                    # 收尾期间的请求异常不覆盖取消语义。
                    with suppress(BaseException):
                        await task
                    check_cancelled(signal)
        except asyncio.CancelledError:
            task.cancel()
            with suppress(BaseException):
                await task
            raise
