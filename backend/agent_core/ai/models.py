"""Ported from pi-package/ai/src/models.ts @ pi snapshot 9841914c71a74d81abe07f751aefd271fd924e63

Pure helpers operating on a ``Model`` value: cost calculation and thinking
level utilities. The ``Model`` / ``ModelCost*`` dataclasses live in
``types.py`` (matching the TS layout where types.ts declares them).

Deliberate trims relative to models.ts: the dynamic model registry
(``Models`` / ``MutableModels`` / ``createModels`` / ``createProvider`` /
catalog refresh & publication plumbing) is non-target (plan §3.2
"动态模型目录裁剪").
"""

from __future__ import annotations

from .types import Model, ModelCostRates, ModelThinkingLevel, Usage, UsageCost

EXTENDED_THINKING_LEVELS: list[ModelThinkingLevel] = [
    "off",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
]


def has_api(model: Model, api: str) -> bool:
    """TS: ``model.api === api`` (type guard in TS; plain bool here)."""
    return model.api == api


def calculate_cost(model: Model, usage: Usage) -> UsageCost:
    """TS models.ts calculateCost. Mutates ``usage.cost`` in place and returns
    it, exactly like the TS original.

    Pricing tiers: the highest matching input threshold applies to the FULL
    request. Anthropic charges 2x base input for 1h cache writes
    (``cache_write_1h``), so the short-write portion is billed at the
    ``cacheWrite`` rate and the 1h portion at ``2 * input`` rate.
    """
    input_tokens = usage.input + usage.cache_read + usage.cache_write
    rates: ModelCostRates = model.cost
    matched_threshold = -1.0
    for tier in model.cost.tiers:
        if input_tokens > tier.input_tokens_above and tier.input_tokens_above > matched_threshold:
            rates = tier
            matched_threshold = tier.input_tokens_above

    # Anthropic charges 2x base input for 1h cache writes.
    long_write = usage.cache_write_1h if usage.cache_write_1h is not None else 0
    short_write = usage.cache_write - long_write
    usage.cost.input = (rates.input / 1000000) * usage.input
    usage.cost.output = (rates.output / 1000000) * usage.output
    usage.cost.cache_read = (rates.cache_read / 1000000) * usage.cache_read
    usage.cost.cache_write = (rates.cache_write * short_write + rates.input * 2 * long_write) / 1000000
    usage.cost.total = usage.cost.input + usage.cost.output + usage.cost.cache_read + usage.cost.cache_write
    return usage.cost


def get_supported_thinking_levels(model: Model) -> list[ModelThinkingLevel]:
    """TS models.ts getSupportedThinkingLevels. Non-reasoning models only
    support "off". ``None`` in ``thinking_level_map`` marks a level as
    unsupported; "xhigh"/"max" require an explicit mapping to exist; other
    levels are supported even when unmapped (provider default applies)."""
    if not model.reasoning:
        return ["off"]

    supported: list[ModelThinkingLevel] = []
    for level in EXTENDED_THINKING_LEVELS:
        if model.thinking_level_map is not None:
            mapped = model.thinking_level_map.get(level)
            if mapped is None and level in model.thinking_level_map:
                # Explicit null in the map: level unsupported.
                continue
            if level in ("xhigh", "max") and mapped is None:
                # xhigh/max require an explicit mapping.
                continue
        elif level in ("xhigh", "max"):
            continue
        supported.append(level)
    return supported


def clamp_thinking_level(model: Model, level: ModelThinkingLevel) -> ModelThinkingLevel:
    """TS models.ts clampThinkingLevel: snap a requested level to the closest
    supported one, searching upward first, then downward, else the first
    supported level (or "off")."""
    available = get_supported_thinking_levels(model)
    if level in available:
        return level

    requested_index = EXTENDED_THINKING_LEVELS.index(level)
    for i in range(requested_index, len(EXTENDED_THINKING_LEVELS)):
        candidate = EXTENDED_THINKING_LEVELS[i]
        if candidate in available:
            return candidate
    for i in range(requested_index - 1, -1, -1):
        candidate = EXTENDED_THINKING_LEVELS[i]
        if candidate in available:
            return candidate
    return available[0] if available else "off"


def models_are_equal(a: Model | None, b: Model | None) -> bool:
    """Check if two models are equal by comparing both their id and provider.
    Returns False if either model is None."""
    if a is None or b is None:
        return False
    return a.id == b.id and a.provider == b.provider
