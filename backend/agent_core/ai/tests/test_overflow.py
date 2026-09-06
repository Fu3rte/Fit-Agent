"""Tests for agent_core.ai.overflow (plan §11.2).

Pattern fixtures use the provider error examples documented in the TS source;
classification boundaries (error patterns, exclusions, silent overflow,
length-stop overflow) are pinned for golden differential tests.
"""
from agent_core.ai.overflow import get_overflow_patterns, is_context_overflow, is_recoverable_length
from agent_core.ai.types import AssistantMessage, Usage


def make_message(stop_reason: str, usage: Usage | None = None, error_message: str | None = None) -> AssistantMessage:
    return AssistantMessage(
        content=[],
        api="openai-completions",
        provider="p",
        model="m",
        usage=usage or Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2),
        stop_reason=stop_reason,  # type: ignore[arg-type]
        timestamp=0,
        error_message=error_message,
    )


# --- Case 1: error-message patterns (one fixture per provider example) -------


OVERFLOW_ERROR_SAMPLES = [

        "prompt is too long: 213462 tokens > 200000 maximum",  # Anthropic
        '413 {"error":{"type":"request_too_large","message":"Request exceeds the maximum size"}}',  # Anthropic 413
        "Your input exceeds the context window of this model",  # OpenAI
        "Requested token count exceeds the model's maximum context length of 131072 tokens",  # LiteLLM
        "Input length (265330) exceeds model's maximum context length (262144).",  # OpenAI-compatible
        "The input token count (1196265) exceeds the maximum number of tokens allowed (1048575)",  # Google
        "This model's maximum prompt length is 131072 but the request contains 537812 tokens",  # xAI
        "Please reduce the length of the messages or completion",  # Groq
        "This endpoint's maximum context length is 65536 tokens. However, you requested about 80000 tokens",  # OpenRouter
        "Input length 30000 exceeds the maximum allowed input length of 26214 tokens.",  # OpenRouter/Poolside
        "The input (20000 tokens) is longer than the model's context length (8192 tokens).",  # Together AI
        "prompt token count of 300000 exceeds the limit of 200000",  # GitHub Copilot
        "the request exceeds the available context size, try increasing it",  # llama.cpp
        "tokens to keep from the initial prompt is greater than the context length",  # LM Studio
        "invalid params, context window exceeds limit",  # MiniMax
        "Your request exceeded model token limit: 300000 (requested: 100000)",  # Kimi For Coding
        "Prompt contains 200000 tokens ... too large for model with 131072 maximum context length",  # Mistral
        "Prompt has 100,000 tokens, but the configured context size is 81,920 tokens",  # DS4
        "model_context_window_exceeded",  # z.ai finish reason as error text
        "prompt too long; exceeded max context length by 2048 tokens",  # Ollama
        "Range of input length should be [1, 32768]",  # DashScope/Qwen
        "context_length_exceeded",  # generic
        "context length exceeded",  # generic (underscore variant)
        "Too many tokens",  # generic
        "token limit exceeded",  # generic
        "400 (no body)",  # Cerebras
        "413 status code (no body)",  # Cerebras
    "400 (no body)",  # Cerebras
]


def test_overflow_error_patterns_match():
    for error_message in OVERFLOW_ERROR_SAMPLES[:-1]:
        assert is_context_overflow(make_message("error", error_message=error_message)) is True


def test_overflow_error_pattern_case_insensitive():
    assert is_context_overflow(make_message("error", error_message="PROMPT IS TOO LONG")) is True


def test_error_without_matching_pattern_is_not_overflow():
    assert is_context_overflow(make_message("error", error_message="invalid api key")) is False


def test_non_error_stop_reason_is_not_overflow_even_with_message():
    assert is_context_overflow(make_message("stop", error_message="prompt is too long")) is False


def test_error_without_message_is_not_overflow():
    assert is_context_overflow(make_message("error", error_message=None)) is False


# --- Non-overflow exclusions --------------------------------------------------


def test_throttling_prefix_excluded():
    # Would match /too many tokens/ without the exclusion
    message = make_message("error", error_message="Throttling error: Too many tokens, please wait before trying again.")
    assert is_context_overflow(message) is False


def test_service_unavailable_prefix_excluded():
    message = make_message("error", error_message="Service unavailable: too many tokens in flight.")
    assert is_context_overflow(message) is False


def test_rate_limit_excluded():
    assert is_context_overflow(make_message("error", error_message="rate limit exceeded for tokens")) is False


def test_too_many_requests_excluded():
    assert is_context_overflow(make_message("error", error_message="429 Too Many Requests")) is False


# --- Case 2: silent overflow (z.ai style) -------------------------------------


def test_silent_overflow_usage_exceeds_window():
    message = make_message("stop", usage=Usage(input=900, output=10, cache_read=200, cache_write=0, total_tokens=1110))
    assert is_context_overflow(message, context_window=1000) is True


def test_silent_overflow_at_exactly_window_is_not_overflow():
    message = make_message("stop", usage=Usage(input=900, output=10, cache_read=100, cache_write=0, total_tokens=1010))
    assert is_context_overflow(message, context_window=1000) is False


def test_silent_overflow_requires_context_window():
    message = make_message("stop", usage=Usage(input=900, output=10, cache_read=200, cache_write=0, total_tokens=1110))
    assert is_context_overflow(message) is False


def test_silent_overflow_zero_window_is_falsy_like_ts():
    message = make_message("stop", usage=Usage(input=900, output=10, cache_read=200, cache_write=0, total_tokens=1110))
    assert is_context_overflow(message, context_window=0) is False


# --- Case 3: length-stop overflow (Xiaomi MiMo style) -------------------------


def test_length_stop_overflow_at_99_percent():
    message = make_message("length", usage=Usage(input=990, output=0, cache_read=0, cache_write=0, total_tokens=990))
    assert is_context_overflow(message, context_window=1000) is True


def test_length_stop_overflow_below_99_percent():
    message = make_message("length", usage=Usage(input=989, output=0, cache_read=0, cache_write=0, total_tokens=989))
    assert is_context_overflow(message, context_window=1000) is False


def test_length_stop_with_output_is_not_overflow():
    message = make_message("length", usage=Usage(input=990, output=5, cache_read=0, cache_write=0, total_tokens=995))
    assert is_context_overflow(message, context_window=1000) is False


def test_length_stop_requires_context_window():
    message = make_message("length", usage=Usage(input=990, output=0, cache_read=0, cache_write=0, total_tokens=990))
    assert is_context_overflow(message) is False


# --- isRecoverableLength ------------------------------------------------------


def test_recoverable_length_basic():
    message = make_message("length", usage=Usage(input=1, output=10, cache_read=0, cache_write=0, total_tokens=11))
    assert is_recoverable_length(message, desired_max_output=100) is True
    assert is_recoverable_length(message, desired_max_output=10) is False  # met the limit
    assert is_recoverable_length(message, desired_max_output=0) is False


def test_recoverable_length_requires_length_stop_reason():
    message = make_message("stop", usage=Usage(input=1, output=10, cache_read=0, cache_write=0, total_tokens=11))
    assert is_recoverable_length(message, desired_max_output=100) is False


def test_get_overflow_patterns_returns_pinned_list():
    patterns = get_overflow_patterns()
    assert len(patterns) == 25
    # spot-check a pinned pattern survives a clone
    assert patterns[0].search("Prompt is too long: 100 tokens")
