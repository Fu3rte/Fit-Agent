import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from concurrent.futures import CancelledError as FutureCancelledError
from contextlib import aclosing
from threading import Event

from app.ai.api import anthropic_messages, openai_completions
from app.ai.context import normalize_context
from app.ai.messages import AssistantMessage, ToolCall
from app.ai.types import (
    AssistantStreamEvent,
    CallObservation,
    LlmContext,
    ModelSpec,
    StreamFunction,
    StreamOptions,
)

ADAPTERS: dict[str, StreamFunction] = {
    "openai-completions": openai_completions.stream,
    "anthropic-messages": anthropic_messages.stream,
}


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
                observation.fail(_cancelled(error, signal))
            raise
        else:
            if observation is not None and not observation.finished:
                observation.fail(signal is not None and signal.is_set())


def stream(
    model: ModelSpec, context: LlmContext, options: StreamOptions
) -> AssistantResponse:
    adapter = ADAPTERS.get(model.api)
    if adapter is None:
        raise ValueError(f"未注册的协议: {model.api}")
    _validate_request(context, options)
    return AssistantResponse(
        _observed(
            adapter(model, context, options),
            options.get("observation"),
            options.get("signal"),
        )
    )


async def complete(
    model: ModelSpec, context: LlmContext, options: StreamOptions
) -> AssistantMessage:
    response = stream(model, context, options)
    async with aclosing(response):
        async for _ in response:
            pass
    return await response.result()
