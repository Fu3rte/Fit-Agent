"""Ported from pi-package/ai/src/utils/overflow.ts @ pi snapshot 9841914c71a74d81abe07f751aefd271fd924e63

Context overflow detection from provider error messages, silent overflow
(usage exceeds the context window), and length-stop overflow (input fills the
window leaving no room for output).

Pattern fidelity (plan §11.2): the regexes below correspond 1:1 to the TS
``OVERFLOW_PATTERNS`` / ``NON_OVERFLOW_PATTERNS`` list entries (case-insensitive
``re.search``, mirroring JS ``RegExp.test``). Patterns are pinned for golden
differential tests — do not "improve" them here; upstream changes them.
"""

from __future__ import annotations

import re

from .types import AssistantMessage

# Regex patterns to detect context overflow errors from different providers.
#
# These patterns match error messages returned when the input exceeds
# the model's context window.
#
# Provider-specific patterns (with example error messages):
#
# - Anthropic: "prompt is too long: 213462 tokens > 200000 maximum"
# - Anthropic: "413 {\"error\":{\"type\":\"request_too_large\",...}}"
# - OpenAI: "Your input exceeds the context window of this model"
# - OpenAI/LiteLLM: "Requested token count exceeds the model's maximum context length of 131072 tokens"
# - OpenAI-compatible: "Input length (265330) exceeds model's maximum context length (262144)."
# - Google: "The input token count (1196265) exceeds the maximum number of tokens allowed (1048575)"
# - xAI: "This model's maximum prompt length is 131072 but the request contains 537812 tokens"
# - Groq: "Please reduce the length of the messages or completion"
# - OpenRouter: "This endpoint's maximum context length is X tokens. However, you requested about Y tokens"
# - OpenRouter/Poolside: "Input length X exceeds the maximum allowed input length of Y tokens."
# - Together AI: "The input (X tokens) is longer than the model's context length (Y tokens)."
# - llama.cpp: "the request exceeds the available context size, try increasing it"
# - LM Studio: "tokens to keep from the initial prompt is greater than the context length"
# - GitHub Copilot: "prompt token count of X exceeds the limit of Y"
# - MiniMax: "invalid params, context window exceeds limit"
# - Kimi For Coding: "Your request exceeded model token limit: X (requested: Y)"
# - DS4: "Prompt has X tokens, but the configured context size is Y tokens"
# - Cerebras: "400/413 status code (no body)"
# - Mistral: "Prompt contains X tokens ... too large for model with Y maximum context length"
# - z.ai: Does NOT error, accepts overflow silently - handled via usage.input > contextWindow
# - Xiaomi MiMo: Truncates input to fill contextWindow exactly, then returns finish_reason "length"
#   with output=0 (no room left to generate). Detected via stopReason "length" + zero output +
#   input filling the context window.
# - DashScope/Qwen: "Range of input length should be [1, X]" (HTTP 400 invalid_parameter_error)
# - Ollama: Some deployments truncate silently, others return errors like
#   "prompt too long; exceeded max context length by X tokens"
_OVERFLOW_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"prompt is too long",  # Anthropic token overflow
        r"request_too_large",  # Anthropic request byte-size overflow (HTTP 413)
        r"input is too long for requested model",  # Amazon Bedrock
        r"exceeds the context window",  # OpenAI (Completions & Responses API)
        r"exceeds (?:the )?(?:model'?s )?maximum context length(?: of [\d,]+ tokens?|\s*\([\d,]+\))",  # OpenAI-compatible proxies (LiteLLM)
        r"input token count.*exceeds the maximum",  # Google (Gemini)
        r"maximum prompt length is \d+",  # xAI (Grok)
        r"reduce the length of the messages",  # Groq
        r"maximum context length is \d+ tokens",  # OpenRouter (most backends)
        r"exceeds (?:the )?maximum allowed input length of [\d,]+ tokens?",  # OpenRouter/Poolside
        r"input \(\d+ tokens\) is longer than the model'?s context length \(\d+ tokens\)",  # Together AI
        r"exceeds the limit of \d+",  # GitHub Copilot
        r"exceeds the available context size",  # llama.cpp server
        r"greater than the context length",  # LM Studio
        r"context window exceeds limit",  # MiniMax
        r"exceeded model token limit",  # Kimi For Coding
        r"too large for model with \d+ maximum context length",  # Mistral
        r"prompt has [\d,]+ tokens?, but the configured context size is [\d,]+ tokens?",  # DS4 server
        r"model_context_window_exceeded",  # z.ai non-standard finish_reason surfaced as error text
        r"prompt too long; exceeded (?:max )?context length",  # Ollama explicit overflow error
        r"range of input length should be",  # DashScope / Qwen Token Plan
        r"context[_ ]length[_ ]exceeded",  # Generic fallback
        r"too many tokens",  # Generic fallback
        r"token limit exceeded",  # Generic fallback
        r"^4(?:00|13)\s*(?:status code)?\s*\(no body\)",  # Cerebras: 400/413 with no body
    )
]

# Patterns that indicate non-overflow errors (e.g. rate limiting, server errors).
# Error messages matching any of these are excluded from overflow detection
# even if they also match an OVERFLOW_PATTERN.
#
# Example: Bedrock formats throttling errors as "ThrottlingException: Too many tokens,
# please wait before trying again." which would match the /too many tokens/i overflow
# pattern without this exclusion.
_NON_OVERFLOW_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^(Throttling error|Service unavailable):",  # AWS Bedrock non-overflow errors (human-readable prefixes from formatBedrockError)
        r"rate limit",  # Generic rate limiting
        r"too many requests",  # Generic HTTP 429 style
    )
]


def is_context_overflow(message: AssistantMessage, context_window: int | None = None) -> bool:
    """Check if an assistant message represents a context overflow error.

    This handles three cases:

    1. Error-based overflow: Most providers return stopReason "error" with a
       specific error message pattern.
    2. Silent overflow: Some providers accept overflow requests and return
       successfully. For these, we check if usage.input exceeds the context window.
    3. Length-stop overflow: Xiaomi MiMo can return "length" with zero output when
       the input fills the context window.

    Unreliable detection (kept for parity with TS): z.ai sometimes accepts
    overflow silently and sometimes returns rate limit errors; Ollama may
    truncate silently, which cannot be detected here at all.

    Args:
        message: The assistant message to check.
        context_window: Optional context window size for detecting silent
            overflow (z.ai) / length-stop overflow (Xiaomi MiMo).
    """
    # Case 1: Check error message patterns
    if message.stop_reason == "error" and message.error_message:
        # Skip messages matching known non-overflow patterns (e.g. throttling / rate-limit)
        is_non_overflow = any(p.search(message.error_message) for p in _NON_OVERFLOW_PATTERNS)
        if not is_non_overflow and any(p.search(message.error_message) for p in _OVERFLOW_PATTERNS):
            return True

    # Case 2: Silent overflow (z.ai style) - successful but usage exceeds context.
    # TS truthiness: a 0/None contextWindow skips this case.
    if context_window and message.stop_reason == "stop":
        input_tokens = message.usage.input + message.usage.cache_read
        if input_tokens > context_window:
            return True

    # Case 3: Length-stop overflow (Xiaomi MiMo style) - server truncates oversized input
    # to fit the context window, leaving no room for output. Returns stopReason "length"
    # with output=0 and input+cacheRead filling the context window.
    if context_window and message.stop_reason == "length" and message.usage.output == 0:
        input_tokens = message.usage.input + message.usage.cache_read
        if input_tokens >= context_window * 0.99:
            return True

    return False


def is_recoverable_length(message: AssistantMessage, desired_max_output: int) -> bool:
    """Check whether a length stop ended below the caller or model's intended output limit.

    Such responses may be caused by context pressure or provider-side truncation, so
    callers can make one bounded compact-and-retry attempt. ``desired_max_output``
    must be the original limit before any context-based clamping.
    """
    return (
        message.stop_reason == "length"
        and desired_max_output > 0
        and message.usage.output < desired_max_output
    )


def get_overflow_patterns() -> list[re.Pattern[str]]:
    """Get the overflow patterns for testing purposes."""
    return list(_OVERFLOW_PATTERNS)
