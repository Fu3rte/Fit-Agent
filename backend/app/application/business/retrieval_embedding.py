import asyncio
import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from openai import AsyncOpenAI

from app.application.business.retrieval_config import (
    EmbeddingConfig,
    load_embedding_config,
    validate_timeout,
)
from app.application.business.retrieval_text import normalize

# 契约约定的单次请求批量上限；超出时按固定顺序简单分批，不引入并发队列。
MAX_BATCH_SIZE = 20


class EmbeddingResponseError(Exception):
    # 返回结构不满足契约时的明确失败；调用方据此放弃整个批次，不接收不完整结果。
    pass


@dataclass(frozen=True)
class EmbeddingUsage:
    # 本任务验证数据：实际请求次数与真实 API usage；不接入现有聊天/摘要用量统计。
    requests: int = 0
    prompt_tokens: int = 0
    total_tokens: int = 0


def _chunks(items: Sequence[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def _prepare_text(text: object) -> str:
    # 输入为空或仅含空白时在发请求前明确失败；文档与查询共用 T2 的 normalize()。
    if not isinstance(text, str):
        raise TypeError("编码输入必须是字符串")
    if not text.strip():
        raise ValueError("编码文本不得为空或仅含空白")
    prepared = normalize(text)
    if not prepared:
        raise ValueError("编码文本规范化后为空")
    return prepared


def _validate_vector(vector: object, dimensions: int) -> list[float]:
    if not isinstance(vector, (list, tuple)):
        raise EmbeddingResponseError("向量必须是序列")
    if len(vector) != dimensions:
        raise EmbeddingResponseError(
            f"向量维度 {len(vector)} 与期望 {dimensions} 不符"
        )
    cleaned: list[float] = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EmbeddingResponseError("向量元素必须是数值")
        number = float(value)
        if not math.isfinite(number):
            raise EmbeddingResponseError("向量元素必须是有限数值")
        cleaned.append(number)
    norm = math.sqrt(sum(value * value for value in cleaned))
    if norm == 0.0:
        raise EmbeddingResponseError("向量范数为零")
    return cleaned


def validate_embedding_data(
    items: Sequence[object], expected_count: int, dimensions: int
) -> list[list[float]]:
    # 独立纯返回校验函数：数量一致、index 恰覆盖 0..n-1、无重复/缺失/越界，
    # 按 index 对齐输入而非依赖服务端顺序，逐向量校验维度、有限值与非零范数。
    if isinstance(expected_count, bool) or not isinstance(expected_count, int):
        raise EmbeddingResponseError("期望数量必须是整数")
    if expected_count <= 0:
        raise EmbeddingResponseError("期望数量必须为正整数")

    data = list(items)
    if len(data) != expected_count:
        raise EmbeddingResponseError(
            f"返回数量 {len(data)} 与该批输入数量 {expected_count} 不一致"
        )

    slots: list[list[float] | None] = [None] * expected_count
    for item in data:
        index = getattr(item, "index", None)
        if isinstance(index, bool) or not isinstance(index, int):
            raise EmbeddingResponseError("index 必须是整数")
        if index < 0 or index >= expected_count:
            raise EmbeddingResponseError(f"index {index} 越界")
        if slots[index] is not None:
            raise EmbeddingResponseError(f"index {index} 重复")
        slots[index] = _validate_vector(getattr(item, "embedding", None), dimensions)

    missing = [index for index, slot in enumerate(slots) if slot is None]
    if missing:
        raise EmbeddingResponseError(f"缺少 index: {missing}")
    return [slot for slot in slots if slot is not None]


class EmbeddingClient:
    # 异步生命周期：创建、请求与关闭在同一事件循环内完成，禁止跨线程/事件循环复用。
    def __init__(self, config: EmbeddingConfig, *, timeout: float | None = None) -> None:
        self._config = config
        self._timeout = validate_timeout(config.timeout if timeout is None else timeout)
        self._client: AsyncOpenAI | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._usage = EmbeddingUsage()

    @classmethod
    def from_env(
        cls, *, timeout: float | None = None
    ) -> "EmbeddingClient":
        # 编码入口：配置加载与校验在此或客户端创建时执行，模块导入不受影响。
        return cls(load_embedding_config(), timeout=timeout)

    async def __aenter__(self) -> "EmbeddingClient":
        if self._client is not None:
            raise RuntimeError("EmbeddingClient 已在上下文中")
        kwargs: dict[str, object] = {
            "api_key": self._config.api_key,
            "base_url": self._config.base_url,
            "max_retries": 0,
        }
        # 未显式指定超时时省略该参数，沿用 SDK 自身默认值。
        if self._timeout is not None:
            kwargs["timeout"] = self._timeout
        loop = asyncio.get_running_loop()
        self._client = AsyncOpenAI(**kwargs)
        self._loop = loop
        return self

    async def __aexit__(self, exc_type, exc, exc_tb) -> None:
        if self._client is not None:
            client = self.client
            await client.close()
            self._client = None
            self._loop = None

    @property
    def config(self) -> EmbeddingConfig:
        return self._config

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            raise RuntimeError("EmbeddingClient 必须在 async with 上下文中使用")
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("EmbeddingClient 必须在所属事件循环中使用")
        return self._client

    @property
    def effective_timeout(self):
        # 回报实际生效的有限超时（显式值或 SDK 默认值）。
        return self.client.timeout

    @property
    def usage(self) -> EmbeddingUsage:
        return self._usage

    async def _embed(self, texts: list[str]):
        response = await self.client.embeddings.create(
            model=self._config.model,
            input=texts,
            dimensions=self._config.dimensions,
            encoding_format="float",
        )
        usage = getattr(response, "usage", None)
        self._usage = EmbeddingUsage(
            requests=self._usage.requests + 1,
            prompt_tokens=self._usage.prompt_tokens
            + int(getattr(usage, "prompt_tokens", 0) or 0),
            total_tokens=self._usage.total_tokens
            + int(getattr(usage, "total_tokens", 0) or 0),
        )
        return response

    async def encode_query(self, text: str) -> list[float]:
        # 查询编码：接收一条文本，返回一个经过校验的向量。
        vectors = await self.encode_batch([text])
        return vectors[0]

    async def encode_batch(self, texts: Sequence[str]) -> list[list[float]]:
        # 批量编码：按输入顺序输出向量；超过批量上限时按固定顺序简单分批。
        prepared = [_prepare_text(text) for text in texts]
        if not prepared:
            raise ValueError("批量编码输入不得为空")
        results: list[list[float]] = []
        for batch in _chunks(prepared, MAX_BATCH_SIZE):
            response = await self._embed(batch)
            results.extend(
                validate_embedding_data(
                    response.data, len(batch), self._config.dimensions
                )
            )
        return results
