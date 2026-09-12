"""S4-05b：一次 Run 的统一执行预算、墙钟与失败分类（08 8.5/8.6「Stage 4 已拍 Harness 策略」）。

一个 Run 只有一个 :class:`RunBudget` 实例，它是全部「实际发生次数」的唯一权威：模型请求
（普通、摘要、暂时故障重试、输出纠错、工具参数纠错）、工具调用、重试池、纠错池、退避等待与
两段墙钟（单次请求总时限、Run 总时限）。每个新 effect 之前都要在这里放行，因此：

- 取消后不启动新尝试（抛 ``CancelledError``，终态由执行驱动裁决）；
- Run 墙钟到期不启动新尝试（``run_timeout``）；
- 请求池（默认 20）／工具池（默认 32）用尽即终态失败（``model_request_failed``）；
- 重试池（每 Run 1 次）与纠错池（每 Run 2 次、输出与工具参数共享）各自独立计数；
- 所有额外尝试都计入请求池：框架 ``retries`` 与 SDK 隐式重试都不能绕开本层
  （SDK ``max_retries=0``：``runtime.provider``；框架 per-tool 预算取纠错池上限，
  每次纠错请求仍由本层扣减，池尽即终态失败）。

墙钟（已拍「首事件与流空闲超时 A」，2026-09-12）：**不新增首事件或流空闲计时器**，只用
「单次请求总时限 120 秒」与「Run 总时限 300 秒」两个时钟；单次请求时限取
``min(已拍值, Run 剩余时间)``，由适配层用 ``asyncio.timeout`` 包住整次请求实现，不依赖传输层
read timeout（官方保活注释会重置后者）。

分类依据只允许：框架异常类型、HTTP 状态码、``finish_reason``、本次尝试是否已向消费方产出
输出／事件（08 8.6）：

- 可重试白名单：连接建立失败与连接期超时（框架 ``ModelAPIError``／裸 ``httpx2.ConnectError``）、
  HTTP 429/500/503，且仅当本次尝试尚无输出；429 等带 ``Retry-After`` 时按其指示等待，
  超过 10 秒即结束而非提前重发，无该头用默认 1 秒。
- 永久失败（不重试，直接终态 ``model_request_failed``）：HTTP 400/401/402/404/422 及其余状态码、
  ``finish_reason`` 为 content_filter/length/insufficient_system_resource/aborted/error、
  已输出后的流中断、框架纠错预算耗尽（``UnexpectedModelBehavior``）。
- 取消（``CancelledError``）与无法分类的异常不改写、不冒充错误码：前者原样上抛，
  后者保持 S4-03 的「未分类异常不发明原因码」行为。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx2
from pydantic_ai.exceptions import (
    ContentFilterError,
    ModelAPIError,
    ModelHTTPError,
    UnexpectedModelBehavior,
)
from pydantic_ai.messages import ModelMessage, ModelResponse, RetryPromptPart
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

from runtime.compression import (
    PromptEstimator,
    ordinary_request_fits,
    request_characters,
    summary_request_fits,
    tool_definition_characters,
)
from runtime.error_codes import (
    CONTEXT_BUDGET_EXCEEDED,
    MODEL_REQUEST_FAILED,
    MODEL_REQUEST_TIMEOUT,
    RUN_TIMEOUT,
)
from runtime.run_task import ExecutionFailure

if (
    TYPE_CHECKING
):  # 仅类型检查期：runtime 不在运行期反向依赖配置模块（config 向下读目录）
    from config import EffectiveHarness

#: 暂时故障重试池：每 Run 1 次（08 2A；摘要与普通请求共用）。
TRANSIENT_RETRY_POOL = 1

#: 输出校验与工具参数校验共享的纠错池：每 Run 2 次（08 2A）。
CORRECTION_POOL = 2

#: 无 ``Retry-After`` 时的默认退避（08 已拍 1 秒）。
DEFAULT_RETRY_DELAY_SECONDS = 1.0

#: ``Retry-After`` 接受上限：超过即结束而不是提前重发（08 已拍 10 秒）。
MAX_RETRY_AFTER_SECONDS = 10.0

#: 可重试的 HTTP 状态：仅连接建立失败/连接期超时与 500/503/429（08 1A 白名单，封闭）。
RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 503})

#: ``finish_reason`` 中的永久结果：不重试、不静默接受无效结果（08 永久失败清单）。
#: 前三个是框架归一化值（`error` 与 `insufficient_system_resource` / `aborted` 同属异常结束），
#: 后两个是 Provider 原值：框架只映射 OpenAI 的五个 chat finish_reason，DeepSeek 的
#: `aborted` / `insufficient_system_resource` 会被映射成 `None` 而留在
#: `provider_details['finish_reason']`，所以两处都要查（见 `require_supported_finish_reason`）。
PERMANENT_FINISH_REASONS = frozenset(
    {"content_filter", "length", "error", "insufficient_system_resource", "aborted"}
)


@dataclass(frozen=True)
class ModelFailureDecision:
    """一次失败尝试的分类结果：是否可重试、终态原因码、服务端要求的等待。"""

    retryable: bool
    error_code: str
    retry_after: float | None = None


class RunBudget:
    """一次 Run 的执行预算与墙钟权威；每 Run 新建，不跨 Run 复用（手动重试即清零）。"""

    def __init__(
        self,
        harness: EffectiveHarness,
        *,
        cancelled: Callable[[], bool],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._harness = harness
        self._cancelled = cancelled
        self._clock = clock
        self._sleep = sleep
        self._started = clock()
        self._requests_used = 0
        self._tool_calls_used = 0
        self._retries_used = 0
        self._corrections_used = 0

    # ---------- 只读事实（证据、SSE 与测试可见；不参与调度） ----------

    @property
    def harness(self) -> EffectiveHarness:
        return self._harness

    @property
    def requests_used(self) -> int:
        return self._requests_used

    @property
    def tool_calls_used(self) -> int:
        return self._tool_calls_used

    @property
    def retries_used(self) -> int:
        return self._retries_used

    @property
    def corrections_used(self) -> int:
        return self._corrections_used

    @property
    def run_deadline(self) -> float:
        return self._started + self._harness.run_timeout_seconds

    def remaining_run_seconds(self) -> float:
        return self.run_deadline - self._clock()

    # ---------- 新 effect 的准入 ----------

    def require_running(self) -> None:
        """任何新 effect 之前：取消则不启动（``CancelledError``），Run 墙钟到期即结束。"""
        if self._cancelled():
            raise asyncio.CancelledError
        if self.remaining_run_seconds() <= 0:
            raise ExecutionFailure(RUN_TIMEOUT)

    def begin_model_request(self) -> float:
        """放行一次实际模型请求并计数；返回本次请求总时限（受 Run 剩余时间约束）。"""
        self.require_running()
        if self._requests_used >= self._harness.max_model_requests:
            raise ExecutionFailure(MODEL_REQUEST_FAILED)
        self._requests_used += 1
        return self._harness.request_timeout_bounded(self.remaining_run_seconds())

    def begin_tool_call(self) -> None:
        """放行一次工具调用并计数（含写入不确定时的状态核对查询，08 8.6）。"""
        self.require_running()
        if self._tool_calls_used >= self._harness.max_tool_calls:
            raise ExecutionFailure(MODEL_REQUEST_FAILED)
        self._tool_calls_used += 1

    def begin_retry(self) -> bool:
        """暂时故障重试池：池尽返回 ``False``（调用方按本次失败原因结束，不再重发）。"""
        self.require_running()
        if self._retries_used >= TRANSIENT_RETRY_POOL:
            return False
        self._retries_used += 1
        return True

    def begin_correction(self) -> None:
        """纠错池（输出与工具参数共享）：池尽即终态失败，不静默接受无效结果。"""
        self.require_running()
        if self._corrections_used >= CORRECTION_POOL:
            raise ExecutionFailure(MODEL_REQUEST_FAILED)
        self._corrections_used += 1

    def retry_delay(self, retry_after: float | None) -> float | None:
        """退避等待：``Retry-After`` ≤10 秒按其值，缺失或非法用 1 秒，>10 秒返回 ``None``。"""
        if retry_after is None or retry_after < 0:
            return DEFAULT_RETRY_DELAY_SECONDS
        if retry_after > MAX_RETRY_AFTER_SECONDS:
            return None
        return retry_after

    async def wait_before_retry(self, delay: float) -> None:
        """退避等待同样是 effect：取消或 Run 剩余时间不够等待即结束，不等完再发。"""
        self.require_running()
        if delay >= self.remaining_run_seconds():
            raise ExecutionFailure(MODEL_REQUEST_TIMEOUT)
        await self._sleep(delay)


def classify_model_failure(
    exc: BaseException, *, output_emitted: bool
) -> ModelFailureDecision | None:
    """按已拍依据分类一次失败的模型尝试；无法分类返回 ``None``（不发明原因码）。

    ``output_emitted``：本次尝试是否已向消费方产出输出／事件。已产出即不可重放（08 8.6），
    这与「已有输出时按流中断处理」一致；非流式请求失败时必然尚未产出，恒为 ``False``。
    """
    if isinstance(exc, TimeoutError):  # 适配层单次请求总时限（asyncio.timeout）
        return ModelFailureDecision(
            retryable=not output_emitted, error_code=MODEL_REQUEST_TIMEOUT
        )
    if isinstance(exc, ModelHTTPError):
        retryable = exc.status_code in RETRYABLE_HTTP_STATUSES and not output_emitted
        return ModelFailureDecision(
            retryable=retryable,
            error_code=MODEL_REQUEST_FAILED,
            retry_after=exc.retry_after,
        )
    if isinstance(exc, ModelAPIError):
        # 框架把连接建立失败与连接期超时（APIConnectionError/APITimeoutError）都映射到这里。
        return ModelFailureDecision(
            retryable=not output_emitted, error_code=MODEL_REQUEST_FAILED
        )
    if isinstance(exc, ContentFilterError | UnexpectedModelBehavior):
        # 内容过滤（永久结果）与「框架纠错预算耗尽」：都不能通过重试绕开。
        return ModelFailureDecision(retryable=False, error_code=MODEL_REQUEST_FAILED)
    if isinstance(exc, httpx2.ConnectError):
        # 裸传输异常（body 阶段可能不经框架映射）：连接建立失败按白名单可重试。
        return ModelFailureDecision(
            retryable=not output_emitted, error_code=MODEL_REQUEST_FAILED
        )
    if isinstance(exc, httpx2.TimeoutException):  # 已含 ConnectTimeout（其子类）
        return ModelFailureDecision(
            retryable=not output_emitted, error_code=MODEL_REQUEST_TIMEOUT
        )
    if isinstance(exc, httpx2.TransportError):
        # 其余传输层故障（如 body 阶段断流）：已输出即不可重放；未输出也不在白名单内。
        return ModelFailureDecision(retryable=False, error_code=MODEL_REQUEST_FAILED)
    return None


def require_supported_finish_reason(response: ModelResponse | StreamedResponse) -> None:
    """``finish_reason`` 属永久结果即终态失败（08 永久失败清单，不静默接受无效结果）。

    要查两处：框架归一化后的 ``finish_reason``（只含 OpenAI 的五个 chat 值）与
    ``provider_details['finish_reason']`` 里的 **Provider 原值**。DeepSeek 的
    ``aborted`` / ``insufficient_system_resource`` 不在框架映射表里，归一化结果为 ``None``，
    只看前者会把被 Provider 中断的回答当成正常结束接受（复审 P1，2026-09-12 修复）。
    """
    provider_reason = (response.provider_details or {}).get("finish_reason")
    if (
        response.finish_reason in PERMANENT_FINISH_REASONS
        or provider_reason in PERMANENT_FINISH_REASONS
    ):
        raise ExecutionFailure(MODEL_REQUEST_FAILED)


class BudgetedModel(WrapperModel):
    """把每次实际模型请求交给 :class:`RunBudget` 的适配层模型包装（S4-05b）。

    本层是 Run 级计数与分类的唯一权威：每次请求（普通、摘要、重试、纠错）都经
    :meth:`RunBudget.begin_model_request` 放行与计数；纠错请求（框架带 ``RetryPromptPart``
    的追问）额外扣减共享纠错池；失败只在「本次尝试尚无输出 + 白名单内 + 重试池仍有配额」
    时退避后重发一次。用户取消直接上抛 ``CancelledError``，绝不重试。

    同时是 Run 级容量闸（S4-06b）：每次请求发送前重算活跃 prompt 估算（消息 + 工具定义），
    普通请求要求「估算输入 ≤ 有效输入上限」且「估算输入 + 输出预留 + 安全余量 ≤ 模型窗口」，
    不成立即以 ``context_budget_exceeded`` 结束、**不实际发送**；成功请求把真实
    ``usage.input_tokens`` 与本次字符数记入 :class:`~runtime.compression.PromptEstimator` 锚点
    （摘要请求不记锚点：它的输入不是后续投影的前缀，取消与重试仍走同一条预算路径）。
    """

    def __init__(
        self,
        wrapped: Model,
        budget: RunBudget,
        *,
        estimator: PromptEstimator | None = None,
    ) -> None:
        super().__init__(wrapped)
        self.budget = budget
        self.estimator = estimator if estimator is not None else PromptEstimator()

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        self._charge_correction(messages)
        return await self._request(
            messages,
            model_settings,
            model_request_parameters,
            summary=False,
            record_anchor=True,
        )

    async def request_summary(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None = None,
    ) -> ModelResponse:
        """摘要请求（S4-06b）：同一 Run 预算、重试池、取消、墙钟与费用接缝。

        容量按摘要请求方程校验（估算输入 + 摘要输出上限 ≤ 有效输入上限），不带工具定义；
        调用方（压缩流程）已按同一方程收缩过待摘要区间。
        """
        return await self._request(
            messages,
            model_settings,
            ModelRequestParameters(),
            summary=True,
            record_anchor=False,
        )

    async def _request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        *,
        summary: bool,
        record_anchor: bool,
    ) -> ModelResponse:
        while True:
            characters = self._gate_request(
                messages, model_request_parameters, summary=summary
            )
            timeout = self.budget.begin_model_request()
            try:
                async with asyncio.timeout(timeout):
                    response = await self.wrapped.request(
                        messages, model_settings, model_request_parameters
                    )
            except asyncio.CancelledError:
                raise  # 用户取消：不重试、不映射错误码，终态由执行驱动裁决
            except BaseException as exc:  # noqa: BLE001 - 分类后按白名单决定重试或终态
                decision = classify_model_failure(exc, output_emitted=False)
                if decision is None:
                    raise  # 未分类异常不发明原因码（S4-03 语义）
                if decision.retryable and self.budget.begin_retry():
                    delay = self.budget.retry_delay(decision.retry_after)
                    if delay is None:
                        # 服务端要求等待超过上限：结束，不提前重发（08）
                        raise ExecutionFailure(MODEL_REQUEST_FAILED) from exc
                    await self.budget.wait_before_retry(delay)
                    continue
                raise ExecutionFailure(decision.error_code) from exc
            require_supported_finish_reason(response)
            if record_anchor:
                self.estimator.record(characters, response.usage.input_tokens)
            return response

    def _gate_request(
        self,
        messages: Sequence[ModelMessage],
        model_request_parameters: ModelRequestParameters,
        *,
        summary: bool,
    ) -> int:
        """发送前的容量闸：超限即 ``context_budget_exceeded``，不实际发送（08「容量、估算与溢出」）。

        取消与 Run 墙钟到期优先于容量判定（沿 S4-05b 语义）；工具定义按本请求实际携带的
        ``function_tools`` 计数，与 ``agent`` 装配侧的工具字符数同源。
        """
        self.budget.require_running()
        characters = request_characters(
            messages,
            tool_characters=tool_definition_characters(
                model_request_parameters.function_tools
            ),
        )
        estimate = self.estimator.estimate(characters)
        fits = (
            summary_request_fits(estimate, self.budget.harness)
            if summary
            else ordinary_request_fits(estimate, self.budget.harness)
        )
        if not fits:
            raise ExecutionFailure(CONTEXT_BUDGET_EXCEEDED)
        return characters

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: Any | None = None,
    ) -> AsyncIterator[StreamedResponse]:
        """流式请求：同样计数、同样墙钟、同样分类、同样容量闸。

        建流阶段（尚未向消费方 yield）的失败可按白名单退避重试一次；已向消费方产出事件后
        的断流不重放（08 8.6），按是否已产出映射终态原因码。
        """
        self._charge_correction(messages)
        started = time.perf_counter()
        stream: StreamedResponse | None = None
        yielded = False
        while True:
            characters = self._gate_request(
                messages, model_request_parameters, summary=False
            )
            timeout = self.budget.begin_model_request()
            try:
                async with asyncio.timeout(timeout):
                    async with self.wrapped.request_stream(
                        messages, model_settings, model_request_parameters, run_context
                    ) as response_stream:
                        stream = response_stream
                        yielded = (
                            True  # 走到 yield 之前就置位：此后不再重试（不可重放）
                        )
                        yield response_stream
                        require_supported_finish_reason(response_stream)
                        self.estimator.record(
                            characters, response_stream.usage.input_tokens
                        )
                        return
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - 同上
                output_emitted = yielded and (
                    stream is not None
                    and stream.time_to_first_chunk(started) is not None
                )
                decision = classify_model_failure(exc, output_emitted=output_emitted)
                if decision is None:
                    raise
                if yielded or not decision.retryable or not self.budget.begin_retry():
                    raise ExecutionFailure(decision.error_code) from exc
                delay = self.budget.retry_delay(decision.retry_after)
                if delay is None:
                    raise ExecutionFailure(MODEL_REQUEST_FAILED) from exc
                await self.budget.wait_before_retry(delay)

    def _charge_correction(self, messages: Sequence[ModelMessage]) -> None:
        """框架带 ``RetryPromptPart`` 的追问即纠错请求：输出与工具参数共享同一池。"""
        if not messages:
            return
        parts = getattr(messages[-1], "parts", ())
        if any(isinstance(part, RetryPromptPart) for part in parts):
            self.budget.begin_correction()
