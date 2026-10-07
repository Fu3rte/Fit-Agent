from asyncio import CancelledError as TaskCancelledError
from collections.abc import Awaitable, Callable
from concurrent.futures import CancelledError
from contextlib import aclosing
from sys import exception
from threading import Event
from typing import Literal, NotRequired, TypedDict
from uuid import uuid4

from app.agent.config import AgentLoopConfig
from app.agent.message_context import prepare_message_context
from app.agent.tool import AgentTool, FinalizedToolCall, ToolBatchResult, run_tool_batch
from app.ai.context import get_current_tools
from app.ai.messages import (
    AssistantMessage,
    JsonObject,
    Message,
    TextContent,
    ToolCall,
    text_projection,
)
from app.ai.stream import AssistantResponse, stream
from app.ai.types import (
    AssistantStreamEvent,
    LlmContext,
    ModelSpec,
    StreamOptions,
    check_cancelled,
)


class LoopEvent(TypedDict):
    type: Literal[
        "trace_start",
        "trace_end",
        "turn_start",
        "turn_end",
        "message_start",
        "message_update",
        "message_end",
        "tool_start",
        "tool_execution_end",
        "tool_result",
    ]
    trace_id: NotRequired[str]
    turn_id: NotRequired[str]
    status: NotRequired[Literal["completed", "failed", "cancelled"]]
    message_id: NotRequired[str]
    message: NotRequired[AssistantMessage]
    assistant_event: NotRequired[AssistantStreamEvent]
    tool_call_id: NotRequired[str]
    name: NotRequired[str]
    arguments: NotRequired[JsonObject]
    content: NotRequired[str]
    is_error: NotRequired[bool]
    duration_ms: NotRequired[float | None]
    postprocess_ms: NotRequired[float | None]
    tool_prepare_started_at: NotRequired[float | None]
    tool_first_result_at: NotRequired[float | None]
    tool_finalized_at: NotRequired[float | None]


class AgentContext(TypedDict):
    messages: list[Message]
    tools: dict[str, AgentTool]


AgentEventSink = Callable[[LoopEvent], Awaitable[None]]

StreamFn = Callable[[ModelSpec, LlmContext, StreamOptions], AssistantResponse]


def _model_spec(config: AgentLoopConfig) -> ModelSpec:
    return ModelSpec(
        api=config.model.MODEL_API,
        provider=config.model.OPENAI_PROVIDER,
        id=config.model.OPENAI_MODEL,
        base_url=config.model.OPENAI_BASE_URL,
    )


async def stream_assistant_response(
    context: AgentContext,
    config: AgentLoopConfig,
    signal: Event | None,
    node_id: str,
    emit: AgentEventSink,
    stream_fn: StreamFn,
) -> AssistantMessage:
    messages = await prepare_message_context(
        context["messages"],
        transform_context=config.transform_context,
        convert_to_llm=config.convert_to_llm,
        signal=signal,
    )
    for declaration in get_current_tools(messages):
        registered = context["tools"][declaration.name].definition()
        if (
            declaration.description != registered.description
            or declaration.parameters != registered.parameters
        ):
            raise ValueError(f"工具声明与执行注册表不一致: {declaration.name}")
    check_cancelled(signal)
    options: StreamOptions = {
        "api_key": config.model.OPENAI_API_KEY,
        "signal": signal,
        "max_tokens": 16384,
    }
    llm_context: LlmContext = {"messages": messages}
    response = stream_fn(_model_spec(config), llm_context, options)
    index: int | None = None
    async with aclosing(response):
        async for event in response:
            if event["type"] == "start":
                if index is not None:
                    raise ValueError("重复的助手消息开始事件")
                partial = event["partial"]
                index = len(context["messages"])
                context["messages"].append(partial)
                await emit({
                    "type": "message_start",
                    "message_id": node_id,
                    "message": partial.model_copy(deep=True),
                })
            elif event["type"] == "done":
                if index is None:
                    raise ValueError("助手终止事件缺少开始事件")
                context["messages"][index] = event["message"]
            else:
                if index is None:
                    raise ValueError("助手增量事件缺少开始事件")
                partial = event["partial"]
                context["messages"][index] = partial
                snapshot = partial.model_copy(deep=True)
                await emit({
                    "type": "message_update",
                    "message_id": node_id,
                    "message": snapshot,
                    "assistant_event": {**event, "partial": snapshot},
                })
        final_message = await response.result()
        if final_message.stop_reason == "pending" or (
            final_message.usage is None and final_message.stop_reason != "aborted"
        ):
            raise ValueError("模型流缺少完整的最终消息")
        if config.save_message is not None:
            await config.save_message(node_id, final_message)
        await emit({
            "type": "message_end",
            "message_id": node_id,
            "message": final_message.model_copy(deep=True),
        })
        return final_message


async def run_agent_loop(
    prompts: list[Message],
    context: AgentContext,
    config: AgentLoopConfig,
    emit: AgentEventSink,
    signal: Event | None = None,
    stream_fn: StreamFn = stream,
) -> list[Message]:
    if config.max_turns < 1:
        raise ValueError("max_turns 必须大于 0")
    if any(name != tool.name for name, tool in context["tools"].items()):
        raise ValueError("工具注册表名称不一致")
    messages: list[Message] = list(prompts)
    current_context: AgentContext = {
        **context,
        "messages": [*context["messages"], *messages],
    }
    await run_loop(current_context, messages, config, signal, emit, stream_fn)
    return messages


async def run_loop(
    context: AgentContext,
    messages: list[Message],
    config: AgentLoopConfig,
    signal: Event | None,
    emit: AgentEventSink,
    stream_fn: StreamFn,
) -> None:
    trace_id = uuid4().hex
    await emit({"type": "trace_start", "trace_id": trace_id})

    async def emit_tool_start(call: ToolCall) -> None:
        await emit({
            "type": "tool_start",
            "tool_call_id": call.id,
            "name": call.name,
            "arguments": call.arguments,
        })

    async def emit_tool_finalized(finalized: FinalizedToolCall) -> None:
        message = finalized.message
        await emit({
            "type": "tool_execution_end",
            "tool_call_id": message.tool_call_id,
            "name": message.tool_name,
            "content": text_projection(message.content),
            "is_error": message.is_error,
            "duration_ms": finalized.duration_ms,
            "postprocess_ms": finalized.postprocess_ms,
        })

    trace_completed = False
    try:
        check_cancelled(signal)
        pending_messages = (
            await config.get_steering_messages()
            if config.get_steering_messages is not None
            else []
        )
        model_calls = 0
        has_more_tool_calls = True
        while has_more_tool_calls or pending_messages:
            check_cancelled(signal)
            if model_calls >= config.max_turns:
                raise RuntimeError(f"Agent 达到 {config.max_turns} 次模型调用上限")
            turn_id = uuid4().hex
            message_id = str(uuid4())
            await emit({
                "type": "turn_start",
                "trace_id": trace_id,
                "turn_id": turn_id,
            })
            turn_completed = False
            tool_timing: ToolBatchResult | None = None
            try:
                context["messages"].extend(pending_messages)
                messages.extend(pending_messages)
                if pending_messages and config.on_steering_consumed is not None:
                    await config.on_steering_consumed(pending_messages)
                pending_messages = []
                message = await stream_assistant_response(
                    context, config, signal, message_id, emit, stream_fn
                )
                model_calls += 1
                seen = {
                    block.id
                    for item in context["messages"][:-1]
                    if isinstance(item, AssistantMessage)
                    for block in item.content
                    if isinstance(block, ToolCall)
                }
                messages.append(message)
                if message.stop_reason == "aborted":
                    raise CancelledError("模型响应已取消")
                if message.stop_reason == "error":
                    raise RuntimeError(message.error_message)
                if message.stop_reason not in {"stop", "length", "toolUse"}:
                    raise ValueError(f"未知模型终止状态: {message.stop_reason}")
                check_cancelled(signal)
                tool_calls = [
                    block for block in message.content if isinstance(block, ToolCall)
                ]
                if message.stop_reason == "stop":
                    if tool_calls:
                        raise ValueError("stop 响应包含工具调用")
                    text = "".join(
                        block.text
                        for block in message.content
                        if isinstance(block, TextContent)
                    )
                    if not text:
                        raise RuntimeError("模型返回了空的最终回答")
                elif message.stop_reason == "length":
                    if tool_calls:
                        raise ValueError("length 最终消息包含工具调用")
                elif not tool_calls:
                    raise ValueError("toolUse 响应缺少工具调用")
                has_more_tool_calls = message.stop_reason == "toolUse"
                active = {
                    tool.name: tool for tool in get_current_tools(context["messages"])
                }
                for call in tool_calls:
                    if call.id in seen:
                        raise ValueError(f"重复工具调用 ID: {call.id}")
                    seen.add(call.id)
                # 助手消息已持久化：按发起本批调用的助手节点绑定业务工具执行实例，
                # 工具声明保持稳定，执行实例按批绑定。
                batch_tools = context["tools"]
                if config.bind_tools is not None:
                    batch_tools = config.bind_tools(message_id)
                batch = await run_tool_batch(
                    tool_calls,
                    tools=batch_tools,
                    declared=active,
                    before_tool_call=config.before_tool_call,
                    after_tool_call=config.after_tool_call,
                    signal=signal,
                    execution_mode=config.tool_execution,
                    on_tool_start=emit_tool_start,
                    on_tool_finalized=emit_tool_finalized,
                    on_tool_update=config.on_tool_update,
                    contains_credentials=config.contains_credentials,
                )
                tool_timing = batch
                for tool_result in batch.messages:
                    context["messages"].append(tool_result)
                    messages.append(tool_result)
                    tool_node_id = str(uuid4())
                    if config.save_message is not None:
                        await config.save_message(tool_node_id, tool_result)
                    await emit({
                        "type": "tool_result",
                        "message_id": tool_node_id,
                        "tool_call_id": tool_result.tool_call_id,
                        "content": text_projection(tool_result.content),
                        "is_error": tool_result.is_error,
                    })
                if batch.failure is not None:
                    # 已取得的安全结果按调用顺序保存后，再传播框架/取消异常。
                    raise batch.failure
                check_cancelled(signal)
                turn_completed = True
            finally:
                await emit({
                    "type": "turn_end",
                    "trace_id": trace_id,
                    "turn_id": turn_id,
                    "status": (
                        "cancelled" if isinstance(exception(), (CancelledError, TaskCancelledError))
                        else "completed" if turn_completed else "failed"
                    ),
                    "tool_prepare_started_at": (
                        tool_timing.prepare_started_at if tool_timing is not None else None
                    ),
                    "tool_first_result_at": (
                        tool_timing.first_result_at if tool_timing is not None else None
                    ),
                    "tool_finalized_at": (
                        tool_timing.finalized_at if tool_timing is not None else None
                    ),
                })
            check_cancelled(signal)
            get_steering = (
                config.get_steering_messages_or_close
                if not has_more_tool_calls and config.get_steering_messages_or_close is not None
                else config.get_steering_messages
            )
            pending_messages = await get_steering() if get_steering is not None else []
            check_cancelled(signal)
        trace_completed = True
    finally:
        await emit({
            "type": "trace_end",
            "trace_id": trace_id,
            "status": (
                "cancelled" if isinstance(exception(), (CancelledError, TaskCancelledError))
                else "completed" if trace_completed else "failed"
            ),
        })
