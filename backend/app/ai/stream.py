import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable
from concurrent.futures import CancelledError as FutureCancelledError
from contextlib import aclosing, suppress
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from random import uniform
from threading import Event
from time import monotonic
from typing import TypeVar

import anthropic
import httpx
import openai

from app.agent.budget import (
    ModelRequestLimitExceeded,
    RecoveryBudgetExhausted,
    RequestBudget,
)
from app.agent.governance import RunControl, RunDeadline
from app.ai.api import anthropic_messages, openai_completions
from app.ai.context import normalize_context
from app.ai.messages import AssistantMessage, ToolCall
from app.ai.quota import ProviderQuotaExceeded, is_provider_quota_error
from app.ai.types import (
    DEFAULT_MODEL_TIMEOUTS,
    DEFAULT_RETRY_POLICY,
    AssistantStreamEvent,
    CallObservation,
    LlmContext,
    ModelSpec,
    ModelTimeouts,
    RetryPolicy,
    StreamFunction,
    StreamOptions,
    check_cancelled,
)

ADAPTERS: dict[str, StreamFunction] = {
    "openai-completions": openai_completions.stream,
    "anthropic-messages": anthropic_messages.stream,
}

_T = TypeVar("_T")


class ModelExecutionTimeout(Exception):
    """模型执行时限：运行截止或单次尝试/有效增量超时。"""


class RunDeadlineExceeded(ModelExecutionTimeout):
    """整个运行的时间预算已耗尽。"""


class ModelAttemptTimeout(ModelExecutionTimeout):
    """单次模型尝试超过自身时限。"""


class ModelIncrementTimeout(ModelExecutionTimeout):
    """首个有效增量或有效增量间隔超过业务进度时限。"""


class ModelServiceError(Exception):
    """模型供应商持续故障：重试次数或退避时间预算已耗尽。"""


# 500 仅在结构化错误体明确表达临时上游故障时进入重试；普通 500 保持终止。
_TRANSIENT_500_TYPES = frozenset(
    {
        "api_error",
        "internal_error",
        "overloaded_error",
        "server_error",
        "service_unavailable",
        "temporarily_unavailable",
        "upstream_error",
    }
)


def _validate_request(context: LlmContext, options: StreamOptions) -> None:
    if not options.get("api_key"):
        raise ValueError("api_key 不能为空")
    normalize_context(context["messages"])


class AssistantResponse:
    def __init__(self, events: AsyncGenerator[AssistantStreamEvent, None]):
        self._source = events
        self._iterator: AsyncGenerator[AssistantStreamEvent, None] | None = None
        self._message: AssistantMessage | None = None
        self._consumed = False

    def __aiter__(self) -> AsyncIterator[AssistantStreamEvent]:
        if self._consumed:
            raise RuntimeError("响应只能消费一次")
        self._consumed = True
        self._iterator = self._consume()
        return self._iterator

    async def result(self) -> AssistantMessage:
        if self._message is None:
            raise RuntimeError("模型流尚未完整结束")
        return self._message

    async def aclose(self) -> None:
        if self._iterator is not None:
            await self._iterator.aclose()
        await self._source.aclose()

    async def _consume(self) -> AsyncGenerator[AssistantStreamEvent, None]:
        opened: dict[int, str] = {}
        next_index = 0
        started = False
        terminated = False
        async with aclosing(self._source):
            async for event in self._source:
                if terminated:
                    raise ValueError("终止事件之后禁止继续发出事件")
                if event["type"] == "done":
                    if not started:
                        raise ValueError("终止事件缺少开始事件")
                    reason = event["reason"]
                    if reason not in {"stop", "length", "toolUse", "error", "aborted"}:
                        raise ValueError(f"非法终止状态: {reason}")
                    if opened and reason != "aborted" and not (
                        reason == "length" and all(kind == "toolcall" for kind in opened.values())
                    ):
                        raise ValueError("终止事件前存在未结束的内容块")
                    message = AssistantMessage.model_validate(
                        event["message"].model_dump(exclude_unset=True)
                    )
                    if reason != message.stop_reason:
                        raise ValueError("done.reason 与 message.stop_reason 不一致")
                    if reason in {"length", "aborted"} and any(
                        isinstance(block, ToolCall) for block in message.content
                    ):
                        raise ValueError(f"{reason} 最终消息禁止包含工具调用")
                    self._message = message
                    terminated = True
                    yield {**event, "message": message}
                    continue
                if event["type"] == "start":
                    if started:
                        raise ValueError("重复的开始事件")
                    started = True
                    yield event
                    continue
                if not started:
                    raise ValueError("增量事件缺少开始事件")
                index = event["content_index"]
                kind = event["type"]
                if kind.endswith("_start"):
                    if index != next_index or index in opened:
                        raise ValueError("内容块索引不连续或重复")
                    opened[index] = kind.removesuffix("_start")
                    next_index += 1
                elif kind.endswith("_delta"):
                    if opened.get(index) != kind.removesuffix("_delta"):
                        raise ValueError("内容块增量缺少对应的开始事件")
                elif kind.endswith("_end"):
                    if opened.get(index) != kind.removesuffix("_end"):
                        raise ValueError("内容块结束缺少对应的开始事件")
                    del opened[index]
                else:
                    raise ValueError(f"未知事件类型: {kind}")
                yield event
        if not terminated:
            raise RuntimeError("模型流缺少终止事件")


def _cancelled(error: BaseException, signal: Event | None) -> bool:
    # 取消依据实际取消异常与协作信号判断：普通异常与提前关闭不属于取消。
    if isinstance(error, (asyncio.CancelledError, FutureCancelledError)):
        return True
    return signal is not None and signal.is_set()


async def _observed(
    source: AsyncGenerator[AssistantStreamEvent, None],
    observation: CallObservation | None,
    signal: Event | None,
    timed_out: list[bool] | None = None,
) -> AsyncGenerator[AssistantStreamEvent, None]:
    # 包裹适配器流：适配器在正常结局或明确错误处上报真实用量；此处兜底异常、取消与提前关闭，
    # 使已发送请求保留可核实状态，缺失的用量保持缺失。
    # 转发层持有底层流的关闭责任：消费者提前退出时同样关闭底层生成器，观测结局只写一次。
    async with aclosing(source):
        first_text_seen = False
        try:
            async for event in source:
                if not first_text_seen and observation is not None and (
                    event["type"] == "text_delta" and event["delta"]
                ):
                    # 首个非空正文增量：按模型自身起点计时，只记录一次。
                    observation.record_first_text()
                    first_text_seen = True
                yield event
        except BaseException as error:
            if observation is not None and not observation.finished:
                # 业务进度超时不是用户取消：取消由本地等待触发时记录为失败而非 aborted。
                aborted = _cancelled(error, signal) and not (
                    timed_out is not None and timed_out[0]
                )
                # 底层诊断仅采集免凭据的分类信号，不改重试与终局语义。
                error_info = {
                    "exception_type": type(error).__name__,
                    "status_code": _status_code(error),
                    "provider_code": _structured_type(error),
                    "retryable": _is_retryable(error),
                }
                observation.fail(aborted, error_info)
            raise
        else:
            if observation is not None and not observation.finished:
                observation.fail(signal is not None and signal.is_set())


def _record_stop(run_control: RunControl | None, reason: str) -> None:
    # 先记录首个停止原因再置位同一取消信号；已有原因时保持原值。
    if run_control is not None:
        run_control.request_cancel(reason)


def _timeout_error(
    now: float, attempt_limit: float, run_expires_at: float
) -> ModelExecutionTimeout:
    if now >= run_expires_at:
        return RunDeadlineExceeded("运行预算已耗尽")
    if now >= attempt_limit:
        return ModelAttemptTimeout("单次模型尝试超过时限")
    return ModelIncrementTimeout("模型有效增量超时")


def _valid_increment(event: AssistantStreamEvent) -> bool:
    # 只有实际文本、思考或工具参数增量刷新业务进度；开始/结束/空增量与原始数据块不计。
    if event["type"] not in {"text_delta", "thinking_delta", "toolcall_delta"}:
        return False
    return bool(event["delta"])


def _progress_elapsed(
    now: float,
    start_observed_at: float | None,
    last_increment: float,
    received_increment: bool,
    timeouts: ModelTimeouts,
) -> bool:
    if start_observed_at is None:
        return False
    deadline = (
        last_increment + timeouts.increment_interval_seconds
        if received_increment
        else start_observed_at + timeouts.first_increment_seconds
    )
    return now >= deadline


async def _watch_signal(signal: Event, task: asyncio.Future) -> None:
    # 同一 threading.Event 跨主事件循环与工作线程传播：轮询避免留下阻塞线程。
    while not task.done():
        if signal.is_set():
            task.cancel()
            return
        await asyncio.sleep(0.05)


async def _wait_for_next(
    awaitable: Awaitable[_T],
    timeout: float,
    signal: Event | None,
    timed_out: list[bool],
) -> _T:
    # 同时受业务进度时限与取消信号约束；超时取消底层读取并关闭其生成器。
    task = asyncio.ensure_future(awaitable)
    watcher = asyncio.create_task(_watch_signal(signal, task)) if signal is not None else None
    try:
        done, _pending = await asyncio.wait({task}, timeout=timeout)
        if task in done:
            return task.result()
        timed_out[0] = True
        task.cancel()
        with suppress(asyncio.CancelledError, StopAsyncIteration):
            await task
        raise asyncio.TimeoutError
    except asyncio.CancelledError:
        task.cancel()
        with suppress(asyncio.CancelledError, StopAsyncIteration):
            await task
        raise
    finally:
        if watcher is not None:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher


def _status_code(error: BaseException) -> int | None:
    value = getattr(error, "status_code", None)
    return value if type(value) is int else None


def _structured_type(error: BaseException) -> str | None:
    # 优先 SDK 解析的顶层 type/code，其次供应商错误体 body.error.type/code。
    for attribute in ("type", "code"):
        value = getattr(error, attribute, None)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        inner = body.get("error")
        if isinstance(inner, dict):
            for attribute in ("type", "code"):
                value = inner.get(attribute)
                if isinstance(value, str) and value.strip():
                    return value.strip().lower()
    return None


def _is_retryable(error: BaseException) -> bool:
    # 分类优先 SDK 异常类型与结构化错误码；额度、鉴权、权限、参数与配置错误直接终止。
    if isinstance(
        error,
        (
            ProviderQuotaExceeded,
            ModelRequestLimitExceeded,
            RecoveryBudgetExhausted,
            ModelExecutionTimeout,
            ModelServiceError,
        ),
    ):
        return False
    if is_provider_quota_error(error):
        return False
    if isinstance(
        error,
        (
            openai.AuthenticationError,
            openai.PermissionDeniedError,
            anthropic.AuthenticationError,
            anthropic.PermissionDeniedError,
        ),
    ):
        return False
    if isinstance(
        error,
        (
            openai.APITimeoutError,
            openai.APIConnectionError,
            anthropic.APITimeoutError,
            anthropic.APIConnectionError,
            httpx.TransportError,
            httpx.TimeoutException,
            asyncio.TimeoutError,
            TimeoutError,
        ),
    ):
        return True
    status = _status_code(error)
    if status == 429:
        return True
    if status in {502, 503, 504}:
        return True
    if status == 500:
        return _structured_type(error) in _TRANSIENT_500_TYPES
    return False


def _retry_after(error: BaseException) -> float | None:
    # 有效 Retry-After 作为最小等待时间：支持秒数与 HTTP 日期两种形式。
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        headers = getattr(error, "headers", None)
    if headers is None:
        return None
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            moment = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        seconds = (moment - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, seconds)


async def _sleep_with_cancel(
    delay: float,
    signal: Event | None,
    deadline: RunDeadline | None,
    run_control: RunControl | None,
) -> None:
    # 退避响应同一取消信号与运行截止：等待期间持续检查，不吞掉取消。
    end = monotonic() + delay
    while True:
        check_cancelled(signal)
        now = monotonic()
        if now >= end:
            return
        if deadline is not None and deadline.expired():
            _record_stop(run_control, "run_timeout")
            raise RunDeadlineExceeded("运行预算已耗尽")
        await asyncio.sleep(min(0.05, end - now))


def _backoff_cap(policy: RetryPolicy, attempt_number: int) -> float:
    index = min(attempt_number - 1, len(policy.backoff_caps) - 1)
    return policy.backoff_caps[index]


async def _recovering(
    model: ModelSpec,
    context: LlmContext,
    options: StreamOptions,
    adapter: StreamFunction,
) -> AsyncGenerator[AssistantStreamEvent, None]:
    # 公共模型消费边界：每个模型步骤最多三次尝试；首个公开事件发布后关闭网络重试窗口。
    deadline = options.get("deadline")
    budget: RequestBudget | None = options.get("budget")
    run_control = options.get("run_control")
    signal = options.get("signal")
    timeouts: ModelTimeouts = options.get("timeouts") or DEFAULT_MODEL_TIMEOUTS
    policy: RetryPolicy = options.get("retry_policy") or DEFAULT_RETRY_POLICY
    observation_factory = options.get("observation_factory")
    direct_observation = options.get("observation")
    request_recovery = bool(options.get("recovery"))
    internal = bool(options.get("internal"))
    attempt_number = 0
    while True:
        check_cancelled(signal)
        if deadline is not None and deadline.expired():
            _record_stop(run_control, "run_timeout")
            raise RunDeadlineExceeded("运行预算已耗尽")
        if attempt_number >= policy.max_attempts:
            raise ModelServiceError("模型供应商持续故障，重试次数已耗尽")
        attempt_number += 1
        # 首次请求按调用用途计数；同一步骤内的后续尝试同时消耗共享恢复额度。
        recovery = request_recovery or attempt_number > 1
        if budget is not None:
            # 每次真实发送前在同一位置检查并原子扣除；失败时不部分扣款也不发送。
            budget.reserve(recovery=recovery)
        attempt_start = monotonic()
        attempt_limit = attempt_start + timeouts.attempt_seconds
        run_expires_at = deadline.expires_at if deadline is not None else float("inf")
        # 首个有效增量期限从请求建立（公开 start）起计：本地客户端初始化不消耗模型进度预算。
        start_observed_at: float | None = None
        last_increment = attempt_start
        received_increment = False
        published = False
        buffered: list[AssistantStreamEvent] = []
        timed_out = [False]
        observation = (
            observation_factory()
            if observation_factory is not None
            else direct_observation if attempt_number == 1 else None
        )
        attempt_options: StreamOptions = {**options, "observation": observation}
        source = _observed(
            adapter(model, context, attempt_options), observation, signal, timed_out
        )
        try:
            iterator = source.__aiter__()
            while True:
                now = monotonic()
                if start_observed_at is None:
                    # 请求尚未建立：建连/响应头由 HTTP 分项超时与单次尝试上限约束。
                    progress = attempt_limit
                elif received_increment:
                    progress = last_increment + timeouts.increment_interval_seconds
                else:
                    progress = start_observed_at + timeouts.first_increment_seconds
                wait = min(progress, attempt_limit, run_expires_at) - now
                if wait <= 0:
                    raise _timeout_error(now, attempt_limit, run_expires_at)
                try:
                    event = await _wait_for_next(
                        iterator.__anext__(), wait, signal, timed_out
                    )
                except StopAsyncIteration:
                    return
                except asyncio.TimeoutError:
                    raise _timeout_error(
                        monotonic(), attempt_limit, run_expires_at
                    ) from None
                if event["type"] == "start" and start_observed_at is None:
                    start_observed_at = monotonic()
                if _valid_increment(event):
                    received_increment = True
                    last_increment = monotonic()
                if internal:
                    # 内部摘要不对外发布：先缓冲整次尝试；失败重试时整体丢弃，避免半截摘要被消费。
                    buffered.append(event)
                    if event["type"] == "done":
                        for pending_event in buffered:
                            yield pending_event
                        return
                    continue
                # 任一公开事件（含 message_start）发布后不再自动重试。
                published = True
                yield event
                if event["type"] == "done":
                    return
        except ModelExecutionTimeout as error:
            _record_stop(
                run_control,
                "run_timeout" if isinstance(error, RunDeadlineExceeded) else "model_timeout",
            )
            raise
        except (ModelRequestLimitExceeded, RecoveryBudgetExhausted, ProviderQuotaExceeded):
            raise
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, FutureCancelledError)):
                raise
            retryable = _is_retryable(error)
            if retryable and deadline is not None and deadline.expired():
                # HTTP 读超时可能与运行截止同时到达：截止到期优先归为 run_timeout。
                _record_stop(run_control, "run_timeout")
                raise RunDeadlineExceeded("运行预算已耗尽") from error
            if retryable and _progress_elapsed(
                monotonic(),
                start_observed_at,
                last_increment,
                received_increment,
                timeouts,
            ):
                # 60s 读取超时与业务进度期限相等时，传输错误按模型有效增量超时裁决。
                _record_stop(run_control, "model_timeout")
                raise ModelIncrementTimeout("模型有效增量超时") from error
            if published or attempt_number >= policy.max_attempts or not retryable:
                raise
            delay = uniform(0.0, _backoff_cap(policy, attempt_number))
            retry_after = _retry_after(error)
            if retry_after is not None:
                delay = max(delay, retry_after)
            if delay > policy.max_wait_seconds or (
                deadline is not None and delay > deadline.remaining()
            ):
                raise ModelServiceError("模型供应商恢复等待超过时间预算") from error
            with suppress(Exception):
                # 失败尝试的响应流在退避前关闭，避免占用连接等待下一次发送。
                await source.aclose()
            await _sleep_with_cancel(delay, signal, deadline, run_control)
        finally:
            # 正常结束、异常、超时与提前关闭都由转发层关闭底层流与客户端。
            await source.aclose()


def stream(
    model: ModelSpec, context: LlmContext, options: StreamOptions
) -> AssistantResponse:
    adapter = ADAPTERS.get(model.api)
    if adapter is None:
        raise ValueError(f"未注册的协议: {model.api}")
    _validate_request(context, options)
    return AssistantResponse(_recovering(model, context, options, adapter))


async def complete(
    model: ModelSpec, context: LlmContext, options: StreamOptions
) -> AssistantMessage:
    # 摘要等内部消费没有公开消息边界：整次尝试成功后再转发事件，网络失败可有限恢复。
    internal_options: StreamOptions = {**options, "internal": True}
    response = stream(model, context, internal_options)
    async with aclosing(response):
        async for _ in response:
            pass
    return await response.result()
