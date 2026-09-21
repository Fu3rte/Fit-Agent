# 模型请求边界的瞬时失败重试：唯一的物理尝试循环固定延迟重试一次，每个物理尝试各登记一次请求预算，
# 非瞬时失败立即上抛且只尝试一次。重试边界只识别统一类型 TransientModelError（Provider SDK 异常
# 到该类型的分类在 infrastructure/llm/gateway.py，由 test_model_gateway.py 覆盖）；取消、配置、
# Schema 与预算耗尽一律不重试。

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import BaseModel

from app.application.agent.budget import (
    MODEL_RETRY_DELAY_SECONDS,
    ModelRequestBudget,
    ModelRequestBudgetExceeded,
    request_model,
    request_structured_model,
)
from app.application.ports import (
    MODEL_CALL_FAILED_MESSAGE,
    InvalidModelResponse,
    ModelCallFailed,
    ModelGateway,
    TransientModelError,
)

#: SDK 的传输请求／响应替身：两个 SDK 都用 httpx2，状态码是分类的唯一来源。
_REQUEST = httpx2.Request("POST", "https://provider.test/v1/chat/completions")


class Answer(BaseModel):
    """结构化请求的目标 Schema 替身。"""

    ok: bool


def _wrapped_failure(cause: Exception) -> ModelCallFailed:
    """按生产入口的方式包装：``ModelCallFailed`` 的显式 cause 是 Provider 的原始失败。"""
    try:
        raise ModelCallFailed(MODEL_CALL_FAILED_MESSAGE) from cause
    except ModelCallFailed as wrapped:
        return wrapped


#: 可重试的瞬时失败：入口已把 Provider 的瞬时故障映射为该统一类型。
TRANSIENT_FAILURES: list[Callable[[], Exception]] = [
    lambda: TransientModelError(MODEL_CALL_FAILED_MESSAGE),
]

#: 不可重试的失败：产品错误、Schema 与预算耗尽都只尝试一次。
NON_TRANSIENT_FAILURES: list[Callable[[], Exception]] = [
    lambda: ModelCallFailed(MODEL_CALL_FAILED_MESSAGE),
    lambda: InvalidModelResponse("模型响应不符合目标 Schema"),
    lambda: ModelRequestBudgetExceeded("单次 Run 的模型请求预算已用尽"),
]

#: 物理尝试的结果脚本：异常即抛出，其余即返回值。
ANSWER = "固定替身答复"


class ScriptedModel:
    """脚本化模型入口：按出队顺序决定每个物理尝试抛错还是返回。

    重试边界只用 ``text``／``structured`` 两个形态；``tools`` 形态在两者上均不得被调用。
    """

    def __init__(self, outcomes: Sequence[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    async def text(self, system_prompt: str, user_payload: str) -> str:
        return self._next()

    async def structured(
        self, system_prompt: str, user_payload: str, schema: type[BaseModel]
    ) -> BaseModel:
        return self._next()

    async def tools(self, messages: Any, offered_tools: Any) -> Any:
        raise AssertionError("重试边界不调用 tools 形态")

    def _next(self) -> Any:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def gateway(self) -> ModelGateway:
        return ModelGateway(
            text=self.text, structured=self.structured, tools=self.tools
        )


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """固定延迟替身：记录延迟秒数后立即返回，测试不等真实的 1 秒。"""
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return recorded


@pytest.mark.parametrize("failure", TRANSIENT_FAILURES)
async def test_transient_failure_retries_once_after_the_fixed_delay(
    failure: Callable[[], Exception], sleeps: list[float]
) -> None:
    """瞬时失败固定延迟重试一次：第二次的答复照常返回，两个物理尝试各扣一次预算。"""
    model = ScriptedModel([failure(), ANSWER])
    budget = ModelRequestBudget()

    assert await request_model(model.gateway(), "系统提示", {}, budget) == ANSWER

    assert model.calls == 2
    assert budget.used == 2
    assert sleeps == [MODEL_RETRY_DELAY_SECONDS] == [1.0]


@pytest.mark.parametrize("failure", NON_TRANSIENT_FAILURES)
async def test_non_transient_failure_is_attempted_exactly_once(
    failure: Callable[[], Exception], sleeps: list[float]
) -> None:
    """非瞬时失败只尝试一次：原异常原样上抛，不等待、不发起第二次物理请求。"""
    error = failure()
    model = ScriptedModel([error, ANSWER])
    budget = ModelRequestBudget()

    with pytest.raises(type(error)) as raised:
        await request_model(model.gateway(), "系统提示", {}, budget)

    assert raised.value is error
    assert model.calls == 1
    assert budget.used == 1
    assert sleeps == []


async def test_wrapped_sdk_failure_cause_is_not_inspected_at_the_boundary(
    sleeps: list[float],
) -> None:
    """重试边界只看统一类型、不再看 ``__cause__``：入口已把 SDK 失败分类成产品类型。"""
    model = ScriptedModel([
        _wrapped_failure(anthropic.APITimeoutError(_REQUEST)),
        ANSWER,
    ])
    budget = ModelRequestBudget()

    with pytest.raises(ModelCallFailed):
        await request_model(model.gateway(), "系统提示", {}, budget)

    assert model.calls == 1
    assert budget.used == 1
    assert sleeps == []


async def test_structured_request_shares_the_same_retry(
    sleeps: list[float],
) -> None:
    """结构化请求与文本请求共用同一重试边界：瞬时失败后返回第二次的 Schema 实例。"""
    expected = Answer(ok=True)
    model = ScriptedModel(
        [TransientModelError(MODEL_CALL_FAILED_MESSAGE), expected]
    )
    budget = ModelRequestBudget()

    result = await request_structured_model(
        model.gateway(), "系统提示", {}, budget, Answer
    )

    assert result is expected
    assert model.calls == 2
    assert budget.used == 2
    assert sleeps == [1.0]


async def test_retry_is_refused_when_the_request_budget_is_exhausted(
    sleeps: list[float],
) -> None:
    """预算耗尽时重试不得绕过预算：第二次物理请求在登记预算处失败，模型只被调用一次。"""
    model = ScriptedModel([TransientModelError(MODEL_CALL_FAILED_MESSAGE), ANSWER])
    budget = ModelRequestBudget(max_requests=1)

    with pytest.raises(ModelRequestBudgetExceeded):
        await request_model(model.gateway(), "系统提示", {}, budget)

    assert model.calls == 1
    assert budget.used == 1
