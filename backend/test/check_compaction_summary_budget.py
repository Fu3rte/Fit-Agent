import asyncio
import json
from concurrent.futures import CancelledError
from pathlib import Path
from threading import Event

import pytest

from app.agent import compaction as C
from app.ai.types import ModelSpec
from test import check_compaction_core as T

TEMP_ROOT = Path(__file__).resolve().parents[2] / "tmp" / "agent-compaction-fix-c"
EVIDENCE = TEMP_ROOT / "budget-checks.json"
DEFAULT_MAX_TOKENS = 16384
SMALL_MAX_TOKENS = 1024
TIGHT_WINDOW = 40000

RECORDS: dict[str, dict] = {}


def model(context_window: int, max_tokens: int = DEFAULT_MAX_TOKENS) -> ModelSpec:
    return ModelSpec(
        api="openai-completions",
        provider="test",
        id="test",
        base_url="https://example.invalid/v1",
        context_window=context_window,
        max_tokens=max_tokens,
    )


def preparation(
    messages_to_summarize=(),
    turn_prefix_messages=(),
    *,
    previous_summary: str | None = None,
    is_split_turn: bool = False,
) -> C.CompactionPreparation:
    return C.CompactionPreparation(
        first_kept_entry_id="kept-1",
        messages_to_summarize=list(messages_to_summarize),
        turn_prefix_messages=list(turn_prefix_messages),
        is_split_turn=is_split_turn,
        tokens_before=0,
        previous_summary=previous_summary,
        attachments=[],
    )


def test_history_boundary_equal_and_over_one_token() -> None:
    messages = [T.user("历史问题" * 200)]
    prompt = C._summary_prompt(
        messages, previous_summary="旧摘要" * 50, turn_prefix=False
    )
    input_tokens = C.estimate_summary_input_tokens(
        messages, previous_summary="旧摘要" * 50
    )
    assert input_tokens == C._summary_input_tokens(prompt)
    output_tokens = C._summary_max_tokens(
        C.RESERVE_TOKENS, DEFAULT_MAX_TOKENS, C.HISTORY_SUMMARY_RATIO
    )
    C._check_summary_budget(
        prompt, model(input_tokens + output_tokens), output_tokens, "历史摘要"
    )
    with pytest.raises(C.ContextBudgetExceeded):
        C._check_summary_budget(
            prompt, model(input_tokens + output_tokens - 1), output_tokens, "历史摘要"
        )
    RECORDS["history_boundary"] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "window_equal": input_tokens + output_tokens,
    }


def test_turn_prefix_boundary_equal_and_over_one_token() -> None:
    messages = [T.user("任务前缀" * 100)]
    prompt = C._summary_prompt(messages, previous_summary=None, turn_prefix=True)
    input_tokens = C.estimate_summary_input_tokens(messages, turn_prefix=True)
    assert input_tokens == C._summary_input_tokens(prompt)
    output_tokens = C._summary_max_tokens(
        C.RESERVE_TOKENS, DEFAULT_MAX_TOKENS, C.TURN_PREFIX_SUMMARY_RATIO
    )
    C._check_summary_budget(
        prompt, model(input_tokens + output_tokens), output_tokens, "任务前缀摘要"
    )
    with pytest.raises(C.ContextBudgetExceeded):
        C._check_summary_budget(
            prompt, model(input_tokens + output_tokens - 1), output_tokens, "任务前缀摘要"
        )
    RECORDS["turn_prefix_boundary"] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "window_equal": input_tokens + output_tokens,
    }


def test_previous_summary_and_full_tool_result_counted() -> None:
    short = [T.user("短")]
    assert C.estimate_summary_input_tokens(
        short, previous_summary="旧摘要" * 500
    ) > C.estimate_summary_input_tokens(short)

    payload = "完整工具结果" * 40000
    messages = [T.tool_result("call-1", payload)]
    serialized = C.serialize_conversation(messages)
    assert payload in serialized
    input_tokens = C.estimate_summary_input_tokens(messages)
    assert input_tokens > len(payload) // 4
    output_tokens = C._summary_max_tokens(
        C.RESERVE_TOKENS, SMALL_MAX_TOKENS, C.HISTORY_SUMMARY_RATIO
    )
    prompt = C._summary_prompt(messages, previous_summary=None, turn_prefix=False)
    with pytest.raises(C.ContextBudgetExceeded):
        C._check_summary_budget(
            prompt, model(TIGHT_WINDOW, SMALL_MAX_TOKENS), output_tokens, "历史摘要"
        )
    RECORDS["long_tool_result"] = {
        "payload_chars": len(payload),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "window": TIGHT_WINDOW,
    }


def test_generate_rejects_history_over_budget_before_request() -> None:
    prep = preparation(
        [T.tool_result("call-1", "完整工具结果" * 40000)], previous_summary="旧摘要"
    )
    with pytest.raises(C.ContextBudgetExceeded):
        asyncio.run(
            C.generate_compaction_summary(
                prep, model(TIGHT_WINDOW, SMALL_MAX_TOKENS), api_key="k"
            )
        )


def test_generate_split_rejects_history_over_budget_before_request() -> None:
    prep = preparation(
        [T.tool_result("call-1", "完整工具结果" * 40000)],
        [T.user("任务前缀" * 100)],
        previous_summary="旧摘要",
        is_split_turn=True,
    )
    with pytest.raises(C.ContextBudgetExceeded):
        asyncio.run(
            C.generate_compaction_summary(
                prep, model(TIGHT_WINDOW, SMALL_MAX_TOKENS), api_key="k"
            )
        )


def test_generate_split_rejects_turn_prefix_over_budget_before_request() -> None:
    prep = preparation([], [T.user("任务前缀" * 60000)], is_split_turn=True)
    with pytest.raises(C.ContextBudgetExceeded):
        asyncio.run(
            C.generate_compaction_summary(
                prep, model(TIGHT_WINDOW, SMALL_MAX_TOKENS), api_key="k"
            )
        )


def test_cancellation_precedes_budget_check() -> None:
    prep = preparation([T.tool_result("call-1", "完整工具结果" * 40000)])
    signal = Event()
    signal.set()
    with pytest.raises(CancelledError):
        asyncio.run(
            C.generate_compaction_summary(
                prep,
                model(TIGHT_WINDOW, SMALL_MAX_TOKENS),
                api_key="k",
                signal=signal,
            )
        )


def test_empty_api_key_rejected_before_budget() -> None:
    prep = preparation([T.user("短")])
    with pytest.raises(ValueError, match="api_key"):
        asyncio.run(
            C.generate_compaction_summary(prep, model(128000), api_key="")
        )


def check() -> None:
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    test_history_boundary_equal_and_over_one_token()
    test_turn_prefix_boundary_equal_and_over_one_token()
    test_previous_summary_and_full_tool_result_counted()
    test_generate_rejects_history_over_budget_before_request()
    test_generate_split_rejects_history_over_budget_before_request()
    test_generate_split_rejects_turn_prefix_over_budget_before_request()
    test_cancellation_precedes_budget_check()
    test_empty_api_key_rejected_before_budget()
    evidence = {
        **RECORDS,
        "entries_rejected_before_request": [
            "generate: 历史摘要超预算（非切分）",
            "generate: 历史摘要超预算（切分任务）",
            "generate: 任务前缀摘要超预算（切分任务）",
        ],
        "cancellation": "请求前信号优先于预算检查",
        "invalid_parameter": "空 api_key 在供应商请求前拒绝",
    }
    EVIDENCE.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "摘要预算：历史与任务前缀边界相等允许、超 1 token 拒绝；"
        "旧摘要与完整工具结果计入；两次调用均在供应商请求前校验；"
        "取消与非法参数保持原行为"
    )


if __name__ == "__main__":
    check()
