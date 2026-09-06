# Fit-Agent PydanticAI spike：usage 归一层。
# 原则：缺字段 ≠ 0（用原始证据判证），miss ≠ write，同一 token 不重复计费。
# 两条独立视图：
# 1) normalize_raw_usage：从 wire 原始 usage dict 归一（计费与证据用，guard transport 采集）。
# 2) normalize_usage：从 pydantic-ai RequestUsage 归一（框架视图，可传 raw 消歧）。
# 计费不走本模块；本模块只做可观测归一与一致性标注（矛盾交由 FeeGuard 停止处理）。

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pydantic_ai.usage import RequestUsage


class CacheReadSource(str, Enum):
    RAW_HIT = "prompt_cache_hit_tokens"
    GENAI_CACHED = "prompt_tokens_details.cached_tokens(genai-prices)"
    GENAI_CACHED_RAW_EXPLICIT = "prompt_tokens_details.cached_tokens(raw,explicit)"
    NOT_PROVIDED = "not_provided"


def _get_int(raw: dict, *keys: str) -> int | None:
    """按路径取整数字段；字段缺失或非整数返回 None（缺 ≠ 0）。"""
    value: object = raw
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


@dataclass
class RawUsageView:
    """wire 原始 usage 的归一视图：区分"显式 0"与"未提供"。"""
    prompt_tokens: int | None
    completion_tokens: int | None
    cache_read: int | None  # None = 服务端未提供任何命中证据（≠ 0）
    cache_read_explicit: bool  # True = 命中值是服务端显式提供的（含显式 0）
    cache_read_source: CacheReadSource | None
    cache_write: int | None  # DeepSeek OpenAI 端点无 cache write 计费类目，恒为 None
    raw_hit: int | None
    raw_miss: int | None
    dual_source_consistent: bool | None  # raw hit 与 cached_tokens 同提供时是否一致；None = 单来源


def normalize_raw_usage(raw: dict) -> RawUsageView:
    hit = _get_int(raw, "prompt_cache_hit_tokens")
    cached = _get_int(raw, "prompt_tokens_details", "cached_tokens")
    miss = _get_int(raw, "prompt_cache_miss_tokens")

    if hit is not None:
        cache_read, source, explicit = hit, CacheReadSource.RAW_HIT, True
    elif cached is not None:
        cache_read, source, explicit = cached, CacheReadSource.GENAI_CACHED_RAW_EXPLICIT, True
    else:
        cache_read, source, explicit = None, None, False

    dual: bool | None
    if hit is not None and cached is not None:
        dual = hit == cached
    else:
        dual = None

    return RawUsageView(
        prompt_tokens=_get_int(raw, "prompt_tokens"),
        completion_tokens=_get_int(raw, "completion_tokens"),
        cache_read=cache_read,
        cache_read_explicit=explicit,
        cache_read_source=source,
        cache_write=None,  # DeepSeek OpenAI 端点不存在该计费类目；miss 不冒充 write
        raw_hit=hit,
        raw_miss=miss,
        dual_source_consistent=dual,
    )


@dataclass
class NormalizedUsage:
    input_tokens: int
    output_tokens: int
    cache_read: int | None  # None = 服务端未提供任何命中证据（≠ 0）
    cache_read_source: CacheReadSource
    cache_write: int | None  # DeepSeek OpenAI 端点无 cache write 计费类目，恒为 None
    raw_hit: int | None
    raw_miss: int | None
    raw_details: dict[str, int]


def normalize_usage(usage: RequestUsage, raw: dict | None = None) -> NormalizedUsage:
    """从 pydantic-ai 归一 usage 还原可观测指标；缺失字段返回 None 而非 0。
    框架层 cache_read_tokens==0 无法区分"显式 0"与"缺失"；传入 wire raw 时可用
    details.cached_tokens 显式 0 消歧（GENAI_CACHED_RAW_EXPLICIT）。"""
    details = dict(usage.details)
    hit = details.get("prompt_cache_hit_tokens")
    miss = details.get("prompt_cache_miss_tokens")

    raw_cached = None
    if raw is not None:
        raw_cached = _get_int(raw, "prompt_tokens_details", "cached_tokens")

    if hit is not None:
        cache_read, source = hit, CacheReadSource.RAW_HIT
    elif usage.cache_read_tokens > 0:
        # 无原始 hit 字段，但 genai-prices 从 cached_tokens 提取到了命中
        cache_read, source = usage.cache_read_tokens, CacheReadSource.GENAI_CACHED
    elif raw_cached is not None:
        # wire 原始侧显式提供 cached_tokens（含显式 0）；框架归一后归零，用 raw 消歧
        cache_read, source = raw_cached, CacheReadSource.GENAI_CACHED_RAW_EXPLICIT
    else:
        cache_read, source = None, CacheReadSource.NOT_PROVIDED

    # DeepSeek OpenAI 端点不存在 cache write 计费类目（官方价格表仅 hit/miss input + output）。
    # 框架 cache_write_tokens 在 DeepSeek 语义下无来源，恒报 None，不得将 miss 冒充 write。
    cache_write = None
    return NormalizedUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read=cache_read,
        cache_read_source=source,
        cache_write=cache_write,
        raw_hit=hit,
        raw_miss=miss,
        raw_details=details,
    )
