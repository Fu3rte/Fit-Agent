from asyncio import CancelledError as TaskCancelledError
from collections.abc import Awaitable, Callable
from concurrent.futures import CancelledError
from contextlib import aclosing
from sys import exception
from threading import Event
from typing import Literal, NotRequired, TypedDict
from uuid import uuid4

from app.agent.cache_observation import NORMAL_CALL
from app.agent.compaction import ContextBudgetExceeded, should_compact
from app.agent.config import AgentLoopConfig, CompactionHooks
from app.agent.message_context import prepare_message_context
from app.agent.telemetry import TraceSpan
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
from app.ai.model_capabilities import resolve_model_spec
from app.ai.overflow import ContextOverflowError
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
        "compaction_start",
        "compaction_end",
        "custom_message",
    ]
    trace_id: NotRequired[str]
    turn_id: NotRequired[str]
    status: NotRequired[Literal["completed", "failed", "cancelled"]]
    compaction_id: NotRequired[str]
    reason: NotRequired[Literal["threshold", "overflow"]]
    entry_id: NotRequired[str]
    parent_id: NotRequired[str]
    run_id: NotRequired[str]
    created_at: NotRequired[int]
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


class ModelOutputLimitExceeded(Exception):
    # 普通响应达到输出上限：已生成文本与持久化消息保留，运行进入截断终态。
    pass


DEFAULT_MAX_OUTPUT_TOKENS = 16384
# 单次模型调用前的压缩次数上限：避免重复生成相同摘要导致无界循环。
MAX_COMPACTIONS_PER_CALL = 4


def _model_spec(config: AgentLoopConfig) -> ModelSpec:
    # 运行开始时固定一份能力快照：模型请求与摘要调用共用同一份。
    return config.model_spec or resolve_model_spec(config.model)


async def stream_assistant_response(
    context: AgentContext,
    config: AgentLoopConfig,
    signal: Event | None,
    node_id: str,
    emit: AgentEventSink,
    stream_fn: StreamFn,
    parent: TraceSpan,
) -> AssistantMessage:
    messages = await prepare_message_context(
        context["messages"],
        transform_context=config.transform_context,
        convert_to_llm=config.convert_to_llm,
        signal=signal,
    )
    for declaration in get_current_tools(messages):
        # 历史会话可能保留已移除工具的声明；这类声明不进入当前执行注册表，
        # 对应调用在单次调用生命周期内按“工具不存在”拒绝，不阻断运行。
        tool = context["tools"].get(declaration.name)
        if tool is None:
            continue
        registered = tool.definition()
        if (
            declaration.description != registered.description
            or declaration.parameters != registered.parameters
        ):
            raise ValueError(f"工具声明与执行注册表不一致: {declaration.name}")
    check_cancelled(signal)
    spec = _model_spec(config)
    # 共享请求预算的检查与占用先于模型 Span 创建：预算拒绝时不产生虚假模型调用记录。
    if config.request_budget is None:
        raise RuntimeError("正式运行必须装配共享模型请求预算")
    config.request_budget.reserve()
    attributes = {"provider": spec.provider, "model": spec.id, "call_kind": NORMAL_CALL}

    async def model_operation(model_span: TraceSpan) -> AssistantMessage:
        options: StreamOptions = {
            "api_key": config.model.api_key,
            "signal": signal,
            "max_tokens": min(DEFAULT_MAX_OUTPUT_TOKENS, spec.max_tokens),
        }
        if config.observation is not None:
            # 逐次真实调用绑定本回合助手节点身份；观测按实际发送的最终投影记录,
            # 并把真实结局与用量补充到本模型 Span。
            options["observation"] = config.observation.start_call(
                NORMAL_CALL, message_id=node_id, span=model_span
            )
        llm_context: LlmContext = {"messages": messages}
        response = stream_fn(spec, llm_context, options)
        index: int | None = None
        # 模型计时覆盖真实请求与底层响应流关闭：提前关闭、异常与取消同样在此收尾。
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
        return final_message

    final_message = await parent.start_span("model", attributes, model_operation)
    if config.save_message is not None:
        await config.save_message(node_id, final_message)
    await emit({
        "type": "message_end",
        "message_id": node_id,
        "message": final_message.model_copy(deep=True),
    })
    return final_message


async def compact_if_needed(
    context: AgentContext,
    hooks: CompactionHooks,
    signal: Event | None,
    emit: AgentEventSink,
    parent: TraceSpan,
) -> None:
    # 每次模型调用前的阈值压缩：最多 4 次，每次压缩后重估完整请求。
    # 回到预算内即继续；仍超预算且估算输入没有减少时立即终止；第 4 次后同样复核。
    tokens = await hooks.estimate_request()
    for _ in range(MAX_COMPACTIONS_PER_CALL):
        check_cancelled(signal)
        if not should_compact(tokens, hooks.context_window, hooks.reserve_tokens):
            return
        before = tokens
        await run_compaction(context, hooks, signal, emit, "threshold", parent)
        tokens = await hooks.estimate_request()
        if not should_compact(tokens, hooks.context_window, hooks.reserve_tokens):
            return
        if tokens >= before:
            raise ContextBudgetExceeded("压缩未减少估算输入，无法满足当前请求预算")
    raise ContextBudgetExceeded("压缩未释放足够的上下文空间，无法满足当前请求预算")


async def run_compaction(
    context: AgentContext,
    hooks: CompactionHooks,
    signal: Event | None,
    emit: AgentEventSink,
    reason: str,
    parent: TraceSpan | None = None,
) -> None:
    # 阈值压缩与溢出恢复共用同一次提交与阶段事件，只压缩一次。
    # Compaction Span 包住准备、摘要与检查点提交的整个操作，并作为摘要 model 子 Span 的父对象。
    compaction_id = str(uuid4())

    async def compaction_operation(compaction_span: TraceSpan) -> None:
        await emit({
            "type": "compaction_start",
            "compaction_id": compaction_id,
            "reason": reason,
        })
        result = await hooks.compact(compaction_id, signal, compaction_span)
        if result.entry_id != compaction_id or not result.parent_id:
            raise ValueError("压缩提交节点身份与阶段事件不一致")
        context["messages"] = result.messages
        await emit({
            "type": "compaction_end",
            "compaction_id": compaction_id,
            "entry_id": result.entry_id,
            "parent_id": result.parent_id,
        })
        # 节点对提交成功后先公开 compaction_end，再公开新基线 custom_message，随后才进入下一次模型消息。
        if result.baseline is not None:
            baseline = result.baseline
            await emit({
                "type": "custom_message",
                "entry_id": baseline.id,
                "parent_id": baseline.parent_id,
                "run_id": baseline.run_id,
                "created_at": baseline.created_at,
            })

    scope = parent if parent is not None else TraceSpan()
    await scope.start_span("compaction", {"reason": reason}, compaction_operation)


async def recover_context_overflow(
    context: AgentContext,
    hooks: CompactionHooks | None,
    signal: Event | None,
    emit: AgentEventSink,
    parent: TraceSpan | None = None,
) -> None:
    # 明确上下文溢出的一次恢复：复用现有切点与摘要调用，只压缩一次并提交检查点。
    if hooks is None:
        raise ContextBudgetExceeded("未配置压缩，无法恢复上下文溢出")
    await run_compaction(context, hooks, signal, emit, "overflow", parent)
    # 检查点提交后按完整请求复核预算：合法切点只保证保留消息完整，不保证仍满足容量。
    tokens = await hooks.estimate_request()
    if should_compact(tokens, hooks.context_window, hooks.reserve_tokens):
        raise ContextBudgetExceeded("恢复压缩未释放足够的上下文空间，无法满足当前请求预算")


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
    # 正式运行的内部 Trace 身份由入口提供的根上下文统一提供；独立调用沿用本地身份。
    trace_id = config.trace_span.trace_id if config.trace_span else uuid4().hex
    # 显式父对象：正式运行挂在入口根 Span 下，独立调用使用无记录上下文。
    parent = config.trace_span if config.trace_span is not None else TraceSpan()
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
        # 同一连续失败过程最多压缩恢复一次；成功响应后重置，恢复后的续接轮跳过阈值压缩。
        recovery_used = False
        skip_threshold = False
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
            # Turn Span 包住本轮输入处理、模型、工具与本轮收尾；显式传给本轮全部子操作。
            turn_span = parent.open("turn", {})
            turn_completed = False
            tool_timing: ToolBatchResult | None = None
            try:
                projected_pending = (
                    config.project_steering(pending_messages)
                    if pending_messages and config.project_steering is not None
                    else pending_messages
                )
                context["messages"].extend(projected_pending)
                messages.extend(projected_pending)
                if config.observation is not None and projected_pending:
                    config.observation.note_event("steering")
                if pending_messages and config.on_steering_consumed is not None:
                    await config.on_steering_consumed(pending_messages)
                pending_messages = []
                if config.compaction is not None and not skip_threshold:
                    await compact_if_needed(
                        context, config.compaction, signal, emit, turn_span
                    )
                skip_threshold = False
                message = await stream_assistant_response(
                    context, config, signal, message_id, emit, stream_fn, turn_span
                )
                model_calls += 1
                seen = {
                    block.id
                    for item in context["messages"][:-1]
                    if isinstance(item, AssistantMessage)
                    for block in item.content
                    if isinstance(block, ToolCall)
                }
                if message.stop_reason == "error" and message.context_overflow:
                    # 失败消息已由 stream_assistant_response 落库并公开；这里只做恢复调度。
                    if recovery_used:
                        raise ContextOverflowError(
                            message.error_message or "上下文容量溢出"
                        )
                    recovery_used = True
                    skip_threshold = True
                    await recover_context_overflow(
                        context, config.compaction, signal, emit, turn_span
                    )
                    if config.observation is not None:
                        config.observation.note_event("overflow_recovery")
                    # 恢复后必须重试一次：无论本回合由工具续接还是 Steering 触发，都要求循环再入。
                    has_more_tool_calls = True
                    continue
                messages.append(message)
                if message.stop_reason == "aborted":
                    raise CancelledError("模型响应已取消")
                if message.stop_reason == "error":
                    raise RuntimeError(message.error_message)
                if message.stop_reason not in {"stop", "length", "toolUse"}:
                    raise ValueError(f"未知模型终止状态: {message.stop_reason}")
                recovery_used = False
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
                    # 输出截断：截断的工具参数禁止执行，运行进入输出上限终态。
                    raise ModelOutputLimitExceeded()
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
                    telemetry_span=turn_span,
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
                if config.observation is not None:
                    config.observation.note_event("tool_batch")
                if batch.failure is not None:
                    # 已取得的安全结果按调用顺序保存后，再传播框架/取消异常。
                    raise batch.failure
                check_cancelled(signal)
                turn_completed = True
            finally:
                # 真实业务结局同时驱动公开事件与 Turn Span 状态；所有子操作已收尾后才结算。
                turn_status = (
                    "cancelled" if isinstance(exception(), (CancelledError, TaskCancelledError))
                    else "completed" if turn_completed else "failed"
                )
                turn_span.set_status(turn_status)
                try:
                    await emit({
                        "type": "turn_end",
                        "trace_id": trace_id,
                        "turn_id": turn_id,
                        "status": turn_status,
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
                finally:
                    turn_span.settle(exception())
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
