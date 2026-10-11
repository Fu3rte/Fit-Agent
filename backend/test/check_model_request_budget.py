import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.agent.budget import (
    CHARS_PER_TOKEN,
    MAX_MODEL_REQUESTS,
    SAFETY_MARGIN_TOKENS,
    ModelRequestLimitExceeded,
    RequestBudget,
)
from app.agent.config import AgentLoopConfig
from app.ai.quota import ProviderQuotaExceeded
from app.model_config import ModelConfig

BACKEND = Path(__file__).resolve().parents[1]
EVIDENCE = BACKEND / "temp" / "model-request-budget" / "budget-checks.json"

RECORDS: dict[str, object] = {}


def model_config() -> ModelConfig:
    return ModelConfig(
        api="openai-completions",
        base_url="https://example.invalid/v1",
        model="test",
        provider="test",
        api_key="secret",
    )


def test_shared_constants() -> None:
    assert MAX_MODEL_REQUESTS == 64
    assert SAFETY_MARGIN_TOKENS == 4096
    assert CHARS_PER_TOKEN == 3.5
    RECORDS["constants"] = {
        "max_model_requests": MAX_MODEL_REQUESTS,
        "safety_margin_tokens": SAFETY_MARGIN_TOKENS,
        "chars_per_token": CHARS_PER_TOKEN,
    }


def test_sixty_four_reservations_allowed() -> None:
    budget = RequestBudget()
    ordinals = [budget.reserve() for _ in range(MAX_MODEL_REQUESTS)]
    assert ordinals == list(range(1, MAX_MODEL_REQUESTS + 1))
    assert budget.used == MAX_MODEL_REQUESTS
    assert budget.limit == MAX_MODEL_REQUESTS
    RECORDS["sixty_four_allowed"] = {"used": budget.used}


def test_sixty_fifth_rejected_before_send() -> None:
    budget = RequestBudget()
    for _ in range(MAX_MODEL_REQUESTS):
        budget.reserve()
    with pytest.raises(ModelRequestLimitExceeded):
        budget.reserve()
    RECORDS["sixty_fifth_rejected"] = {"used": budget.used}


def test_rejection_keeps_count_and_reserved_slots_persist() -> None:
    budget = RequestBudget()
    for _ in range(MAX_MODEL_REQUESTS):
        budget.reserve()
    for _ in range(5):
        with pytest.raises(ModelRequestLimitExceeded):
            budget.reserve()
    # 被拒绝的占用不改变计数；已占用名额不因失败请求释放。
    assert budget.used == MAX_MODEL_REQUESTS
    RECORDS["count_after_rejection"] = {"used": budget.used}


def test_independent_budgets_do_not_interfere() -> None:
    first = RequestBudget()
    second = RequestBudget()
    for _ in range(MAX_MODEL_REQUESTS):
        first.reserve()
    assert second.used == 0
    assert second.reserve() == 1
    assert first.used == MAX_MODEL_REQUESTS
    RECORDS["independent"] = {"first": first.used, "second": second.used}


def test_shared_budget_accumulates_across_entrypoints() -> None:
    # 普通请求、摘要、恢复重试与 Steering 触发的请求共享同一计数入口。
    budget = RequestBudget()
    callers = {
        "normal": 40,
        "summary": 10,
        "recovery": 8,
        "steering": 6,
    }
    assert sum(callers.values()) == MAX_MODEL_REQUESTS
    for count in callers.values():
        for _ in range(count):
            budget.reserve()
    assert budget.used == MAX_MODEL_REQUESTS
    with pytest.raises(ModelRequestLimitExceeded):
        budget.reserve()
    RECORDS["shared_entrypoints"] = {"used": budget.used, "callers": callers}


def test_concurrent_reservations_stop_at_limit() -> None:
    budget = RequestBudget()
    successes: list[int] = []
    rejections: list[int] = []
    lock = threading.Lock()

    def attempt() -> None:
        try:
            ordinal = budget.reserve()
        except ModelRequestLimitExceeded:
            with lock:
                rejections.append(1)
            return
        with lock:
            successes.append(ordinal)

    attempts = 200
    with ThreadPoolExecutor(max_workers=32) as pool:
        list(pool.map(lambda _: attempt(), range(attempts)))

    assert len(successes) == MAX_MODEL_REQUESTS
    assert sorted(successes) == list(range(1, MAX_MODEL_REQUESTS + 1))
    assert len(rejections) == attempts - MAX_MODEL_REQUESTS
    assert budget.used == MAX_MODEL_REQUESTS
    RECORDS["concurrent"] = {
        "attempts": attempts,
        "successes": len(successes),
        "rejections": len(rejections),
    }


def test_invalid_limit_rejected() -> None:
    for invalid in (0, -1, "64", 1.5, True):
        with pytest.raises(ValueError):
            RequestBudget(invalid)


def test_agent_loop_config_carries_shared_budget() -> None:
    budget = RequestBudget()
    config = AgentLoopConfig(model=model_config(), max_turns=64, request_budget=budget)
    assert config.request_budget is budget
    default = AgentLoopConfig(model=model_config(), max_turns=64)
    assert default.request_budget is None
    RECORDS["config_budget"] = {"default_is_none": default.request_budget is None}


def test_exception_contract_types() -> None:
    assert issubclass(ModelRequestLimitExceeded, Exception)
    assert issubclass(ProviderQuotaExceeded, Exception)
    assert ProviderQuotaExceeded is not ModelRequestLimitExceeded
    RECORDS["exceptions"] = {
        "request_limit": ModelRequestLimitExceeded.__name__,
        "provider_quota": ProviderQuotaExceeded.__name__,
    }


def check() -> None:
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    test_shared_constants()
    test_sixty_four_reservations_allowed()
    test_sixty_fifth_rejected_before_send()
    test_rejection_keeps_count_and_reserved_slots_persist()
    test_independent_budgets_do_not_interfere()
    test_shared_budget_accumulates_across_entrypoints()
    test_concurrent_reservations_stop_at_limit()
    test_invalid_limit_rejected()
    test_agent_loop_config_carries_shared_budget()
    test_exception_contract_types()
    EVIDENCE.write_text(
        json.dumps(RECORDS, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "运行预算：前 64 次占用成功，第 65 次发送前拒绝；拒绝与失败请求保持计数；"
        "独立预算互不影响；多入口共享累计；并发占用不超上限；"
        "config 传递共享对象；共享常量与异常契约可用"
    )


if __name__ == "__main__":
    check()
