"""Contract tests for agent_core.ai.models (plan §4.1 / §11.2).

Focus: calculateCost per-million pricing, tier selection (highest matching
input threshold applies to the full request), the Anthropic 1h cache-write
rule (cache_write_1h billed at 2 * input rate, subset of cache_write, never
double-counted), and thinking-level helpers.
"""

import math

from agent_core.ai.models import (
    calculate_cost,
    clamp_thinking_level,
    get_supported_thinking_levels,
    has_api,
    models_are_equal,
)
from agent_core.ai.types import Model, ModelCost, ModelCostTier, Usage


def make_model(**cost_kwargs) -> Model:
    cost_kwargs.setdefault("input", 1.0)
    cost_kwargs.setdefault("output", 2.0)
    cost_kwargs.setdefault("cache_read", 0.1)
    cost_kwargs.setdefault("cache_write", 1.25)
    return Model(
        id="m",
        name="Model",
        api="openai-completions",
        provider="deepseek",
        base_url="https://example.invalid/v1",
        reasoning=True,
        input=["text"],
        cost=ModelCost(**cost_kwargs),
        context_window=65536,
        max_tokens=8192,
    )


def make_usage(
    *,
    input: int = 1000000,
    output: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
    cache_write_1h: int | None = None,
    reasoning: int | None = None,
    total_tokens: int | None = None,
) -> Usage:
    return Usage(
        input=input,
        output=output,
        cache_read=cache_read,
        cache_write=cache_write,
        cache_write_1h=cache_write_1h,
        reasoning=reasoning,
        total_tokens=(input + output + cache_read + cache_write) if total_tokens is None else total_tokens,
    )


class TestCalculateCost:
    def test_basic_per_million_pricing(self):
        model = make_model()
        usage = make_usage(output=500000, cache_read=250000, cache_write=100000)
        usage.total_tokens = 1850000
        cost = calculate_cost(model, usage)
        assert cost.input == 1.0
        assert cost.output == 1.0
        assert cost.cache_read == 0.025
        assert cost.cache_write == 0.125
        assert cost.total == 1.0 + 1.0 + 0.025 + 0.125
        # mutates usage.cost in place like the TS original
        assert usage.cost is cost

    def test_no_cache_write_1h_means_all_short_write(self):
        model = make_model()
        usage = make_usage(cache_write=1000000)
        cost = calculate_cost(model, usage)
        assert cost.cache_write == 1.25

    def test_cost1h_billed_at_two_times_input_rate(self):
        # Anthropic charges 2x base input for 1h cache writes; cache_write_1h
        # is a subset of cache_write, the remainder billed at cacheWrite rate.
        model = make_model()
        usage = make_usage(cache_write=1000000, cache_write_1h=400000)
        usage.total_tokens = 2000000
        cost = calculate_cost(model, usage)
        expected = (1.25 * 600000 + 1.0 * 2 * 400000) / 1000000
        assert cost.cache_write == expected

    def test_cache_write_1h_never_added_into_total_twice(self):
        model = make_model()
        usage = make_usage(cache_write=1000000, cache_write_1h=400000)
        cost = calculate_cost(model, usage)
        assert cost.total == cost.input + cost.output + cost.cache_read + cost.cache_write

    def test_highest_matching_tier_applies_to_full_request(self):
        model = make_model(
            tiers=[
                ModelCostTier(input=0.5, output=1.0, cache_read=0.05, cache_write=0.5, input_tokens_above=1000000),
                ModelCostTier(input=0.25, output=0.5, cache_read=0.01, cache_write=0.25, input_tokens_above=2000000),
            ]
        )
        # inputTokens total = input + cacheRead + cacheWrite = 1.5M -> first tier for the WHOLE request
        usage = make_usage(input=1000000, cache_read=250000, cache_write=250000)
        cost = calculate_cost(model, usage)
        assert cost.input == 0.5  # tier1 rate 0.5 $/M x 1.0M tokens

        # 2.1M total -> second (highest matching) tier for the whole request
        usage2 = make_usage(input=2000000, cache_read=50000, cache_write=50000)
        cost2 = calculate_cost(model, usage2)
        assert cost2.input == 0.5  # tier2 rate 0.25 $/M x 2.0M tokens
        assert cost2.cache_read == 0.0005  # tier2 rate 0.01 $/M x 50000 tokens
        assert cost2.cache_write == 0.0125  # tier2 rate 0.25 $/M x 50000 tokens

        # below first threshold -> base rates
        usage3 = make_usage(input=999999)
        assert math.isclose(calculate_cost(model, usage3).input, 0.999999)


class TestHasApiAndEquality:
    def test_has_api(self):
        model = make_model()
        assert has_api(model, "openai-completions") is True
        assert has_api(model, "anthropic-messages") is False

    def test_models_are_equal_none_cases(self):
        model = make_model()
        assert models_are_equal(model, None) is False
        assert models_are_equal(None, model) is False

    def test_models_are_equal_id_and_provider(self):
        a = make_model()
        b = make_model()
        assert models_are_equal(a, b) is True
        b.provider = "other"
        assert models_are_equal(a, b) is False


class TestThinkingLevels:
    def test_non_reasoning_model_only_off(self):
        model = make_model()
        model.reasoning = False
        assert get_supported_thinking_levels(model) == ["off"]

    def test_unmapped_levels_use_provider_defaults(self):
        model = make_model()
        model.thinking_level_map = {"low": "low"}
        levels = get_supported_thinking_levels(model)
        assert levels == ["off", "minimal", "low", "medium", "high"]

    def test_null_marks_level_unsupported(self):
        model = make_model()
        model.thinking_level_map = {"minimal": None}
        assert "minimal" not in get_supported_thinking_levels(model)

    def test_xhigh_max_require_explicit_mapping(self):
        model = make_model()
        assert "xhigh" not in get_supported_thinking_levels(model)
        model.thinking_level_map = {"xhigh": "xhigh"}
        assert "xhigh" in get_supported_thinking_levels(model)
        assert "max" not in get_supported_thinking_levels(model)

    def test_clamp_exact_and_snapping(self):
        model = make_model()
        model.thinking_level_map = {"minimal": None, "low": "low", "high": "high"}
        assert clamp_thinking_level(model, "low") == "low"
        # "minimal" explicitly null -> snap upward past it to "low"
        assert clamp_thinking_level(model, "minimal") == "low"
        # "xhigh" unmapped -> upward fails, snap downward to "high"
        assert clamp_thinking_level(model, "xhigh") == "high"
        # unmapped non-xhigh levels pass through (provider default applies)
        assert clamp_thinking_level(model, "medium") == "medium"
        # "off" is always supported for reasoning models unless explicitly null
        assert clamp_thinking_level(model, "off") == "off"

    def test_clamp_fallback_first_supported(self):
        model = make_model()
        model.reasoning = False
        assert clamp_thinking_level(model, "high") == "off"
