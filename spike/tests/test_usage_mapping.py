# Item 2：原始 usage 与归一化指标的离线检查（桩 transport，无真实调用）。
# 两条独立视图：normalize_raw_usage（wire 原始 dict，计费/证据用）与 normalize_usage（pydantic-ai 视图）。
# 缺字段 ≠ 0；显式 0 ≠ 缺失；miss ≠ write；wire 原始与框架归一在重叠字段上一致。

import asyncio

from pydantic_ai import Agent
from spike_lib.capture import ScriptedTransport, deepseek_usage
from spike_lib.fee_guard import FeeGuard
from spike_lib.real_runner import build_spike_agent
from spike_lib.usage_norm import CacheReadSource, normalize_raw_usage, normalize_usage

MODEL = "deepseek-v4-flash"
DUMMY_KEY = "dummy-not-a-credential"  # 合成占位符（非凭据）；真实调用走环境变量


def _agent(transport: ScriptedTransport, captured: list) -> Agent:
    return build_spike_agent(
        MODEL,
        guard=FeeGuard(),
        api_key=DUMMY_KEY,
        instructions="常驻层：usage 归一检查。",
        inner_transport=transport,
        captured=captured,
    )


def test_hit_and_miss_fields_surface_in_normalized_and_raw():
    transport = ScriptedTransport(
        script=[
            {
                "usage": deepseek_usage(
                    prompt_tokens=1000,
                    completion_tokens=16,
                    cached_tokens=800,
                    hit_tokens=800,
                    miss_tokens=200,
                ),
                "content": "ok",
            }
        ]
    )
    captured: list = []
    result = asyncio.run(_agent(transport, captured).run("hello"))
    usage = result.usage
    raw = transport.captured[
        0
    ].body  # 非流式响应不在请求里；raw usage 取自 wire 响应解析
    # wire 原始响应侧：由 guard transport 结算路径解析（此处用桩响应体直接构造视图）
    raw_view = normalize_raw_usage(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 16,
            "prompt_cache_hit_tokens": 800,
            "prompt_cache_miss_tokens": 200,
            "prompt_tokens_details": {"cached_tokens": 800},
        }
    )
    assert raw_view.prompt_tokens == 1000 and raw_view.completion_tokens == 16
    assert raw_view.cache_read == 800 and raw_view.cache_read_explicit is True
    assert raw_view.cache_read_source == CacheReadSource.RAW_HIT
    assert raw_view.dual_source_consistent is True
    assert raw_view.cache_write is None  # DeepSeek OpenAI 端点无 write 类目
    assert raw_view.raw_miss == 200

    norm = normalize_usage(usage)
    assert norm.input_tokens == 1000
    assert norm.output_tokens == 16
    assert norm.cache_read == 800
    assert norm.raw_hit == 800 and norm.raw_miss == 200
    # 原始字段保留在 details（pydantic-ai _map_usage 顶层整数字段进 details）
    assert norm.raw_details.get("prompt_cache_hit_tokens") == 800
    assert norm.raw_details.get("prompt_cache_miss_tokens") == 200
    # 框架归一 cache_read_tokens 与原始 hit 一致（genai-prices deepseek 提取）
    assert usage.cache_read_tokens == 800
    assert norm.cache_read_source == CacheReadSource.RAW_HIT
    # wire 原始与框架归一在重叠字段一致
    assert raw_view.raw_hit == norm.raw_hit and raw_view.raw_miss == norm.raw_miss
    _ = raw, captured


def test_missing_cache_fields_is_not_zero():
    # 桩用量量级注：伪造 prompt_tokens 必须低于护栏按请求体估的输入上界（~500），
    # 否则 peak 时段结算会超预留触发 StopSpike（2026-09-07 教训：结算价随时钟峰谷切换）。
    transport = ScriptedTransport(
        script=[
            {
                "usage": deepseek_usage(
                    prompt_tokens=300,
                    completion_tokens=16,
                    cached_tokens=None,
                    hit_tokens=None,
                    miss_tokens=None,
                ),
                "content": "ok",
            }
        ]
    )
    captured: list = []
    result = asyncio.run(_agent(transport, captured).run("hello"))
    usage = result.usage
    norm = normalize_usage(usage)

    assert usage.cache_read_tokens == 0  # 框架归一值是 0（默认）
    assert norm.cache_read is None  # 归一层判定为“未提供”，≠ 0
    assert norm.cache_read_source == CacheReadSource.NOT_PROVIDED
    assert norm.raw_hit is None and norm.raw_miss is None

    raw_view = normalize_raw_usage({"prompt_tokens": 1000, "completion_tokens": 16})
    assert raw_view.cache_read is None and raw_view.cache_read_explicit is False
    assert raw_view.cache_read_source is None


def test_zero_hit_with_explicit_fields_is_zero_not_missing():
    # 显式 0 命中（真实提供）与字段缺失必须可区分
    transport = ScriptedTransport(
        script=[
            {
                "usage": deepseek_usage(
                    prompt_tokens=300,
                    completion_tokens=16,
                    cached_tokens=0,
                    hit_tokens=0,
                    miss_tokens=300,
                ),
                "content": "ok",
            }
        ]
    )
    captured: list = []
    result = asyncio.run(_agent(transport, captured).run("hello"))
    norm = normalize_usage(result.usage)
    assert norm.cache_read == 0
    assert norm.cache_read_source == CacheReadSource.RAW_HIT

    raw_view = normalize_raw_usage(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 16,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 1000,
            "prompt_tokens_details": {"cached_tokens": 0},
        }
    )
    assert raw_view.cache_read == 0 and raw_view.cache_read_explicit is True


def test_genai_prices_cached_tokens_fallback_without_raw_hit():
    # 只有嵌套 cached_tokens、无顶层 hit/miss：genai-prices 提取进 cache_read_tokens
    usage_dict = {
        "prompt_tokens": 1000,
        "completion_tokens": 16,
        "total_tokens": 1016,
        "prompt_tokens_details": {"cached_tokens": 640},
    }
    transport = ScriptedTransport(script=[{"usage": usage_dict, "content": "ok"}])
    captured: list = []
    result = asyncio.run(_agent(transport, captured).run("hello"))
    usage = result.usage
    norm = normalize_usage(usage)
    assert usage.cache_read_tokens == 640
    assert norm.cache_read == 640
    assert norm.cache_read_source == CacheReadSource.GENAI_CACHED
    assert norm.raw_hit is None  # 原始字段确实不存在

    raw_view = normalize_raw_usage(usage_dict)
    assert raw_view.cache_read == 640
    assert raw_view.cache_read_source == CacheReadSource.GENAI_CACHED_RAW_EXPLICIT
    assert raw_view.raw_hit is None


def test_nested_cached_tokens_zero_without_hit_is_explicit_zero_not_missing():
    """审查修复：嵌套 cached_tokens=0 且无顶层 hit——wire 原始侧显式 0，不得判成缺失。"""
    raw = {
        "prompt_tokens": 300,
        "completion_tokens": 16,
        "total_tokens": 316,
        "prompt_tokens_details": {"cached_tokens": 0},
    }
    raw_view = normalize_raw_usage(raw)
    assert raw_view.cache_read == 0
    assert raw_view.cache_read_explicit is True
    assert raw_view.cache_read_source == CacheReadSource.GENAI_CACHED_RAW_EXPLICIT

    # 框架视图：无 raw 时 0 归一值不可消歧 → NOT_PROVIDED；传入 raw 后消歧为显式 0
    transport = ScriptedTransport(script=[{"usage": raw, "content": "ok"}])
    captured: list = []
    result = asyncio.run(_agent(transport, captured).run("hello"))
    norm_no_raw = normalize_usage(result.usage)
    norm_with_raw = normalize_usage(result.usage, raw=raw)
    assert norm_no_raw.cache_read_source == CacheReadSource.NOT_PROVIDED
    assert norm_with_raw.cache_read == 0
    assert norm_with_raw.cache_read_source == CacheReadSource.GENAI_CACHED_RAW_EXPLICIT


def test_raw_view_detects_dual_source_inconsistency_flag():
    raw = {
        "prompt_tokens": 1000,
        "completion_tokens": 16,
        "prompt_cache_hit_tokens": 700,
        "prompt_tokens_details": {"cached_tokens": 300},
    }
    raw_view = normalize_raw_usage(raw)
    assert (
        raw_view.dual_source_consistent is False
    )  # 归一层标注矛盾；计费侧由 FeeGuard 停止处理


def test_deepseek_has_no_cache_write_category():
    transport = ScriptedTransport(
        script=[
            {
                "usage": deepseek_usage(
                    prompt_tokens=300,
                    completion_tokens=16,
                    cached_tokens=0,
                    hit_tokens=0,
                    miss_tokens=300,
                ),
                "content": "ok",
            }
        ]
    )
    captured: list = []
    result = asyncio.run(_agent(transport, captured).run("hello"))
    usage = result.usage
    norm = normalize_usage(usage)
    # DeepSeek OpenAI 端点：miss 不冒充 write；归一 cache_write 恒为 None
    assert norm.cache_write is None
    assert usage.cache_write_tokens == 0
    assert norm.raw_miss == 300  # miss 保留在原始字段，不复用为 write
