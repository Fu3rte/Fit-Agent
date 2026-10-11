import hashlib
import json
import re
from collections.abc import AsyncGenerator
from time import time_ns

from anthropic import AsyncAnthropic

from app.ai.context import get_current_system_prompt, get_current_tools
from app.ai.messages import (
    AssistantMessage,
    GrammarSampling,
    ImageContent,
    JsonSchemaSampling,
    Message,
    StopReason,
    SystemMessage,
    TextContent,
    ThinkingContent,
    Tool,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from app.ai.overflow import build_overflow_message, is_context_overflow_error
from app.ai.types import (
    AssistantStreamEvent,
    DoneEvent,
    LlmContext,
    ModelSpec,
    StartEvent,
    StreamOptions,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    check_cancelled,
)

API: str = "anthropic-messages"


def normalize_tool_call_id(identifier: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier):
        return identifier
    return "toolu_" + hashlib.sha256(identifier.encode()).hexdigest()[:32]


def _tool_ids(messages: list[Message]) -> dict[str, str]:
    identifiers: dict[str, str] = {}
    used: set[str] = set()
    for message in messages:
        if not isinstance(message, AssistantMessage):
            continue
        for block in message.content:
            if isinstance(block, ToolCall):
                identifier = normalize_tool_call_id(block.id)
                if identifier in used:
                    raise ValueError("规范化工具调用 ID 冲突")
                used.add(identifier)
                identifiers[block.id] = identifier
    return identifiers


def _image_block(block: ImageContent) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": block.mime_type,
            "data": block.data,
        },
    }


def _user_blocks(content: list) -> list[dict]:
    blocks: list[dict] = []
    for block in content:
        if isinstance(block, TextContent):
            if block.text.strip():
                blocks.append({"type": "text", "text": block.text})
        elif isinstance(block, ImageContent):
            blocks.append(_image_block(block))
    return blocks


def _assistant_blocks(
    message: AssistantMessage, model: ModelSpec, identifiers: dict[str, str]
) -> list[dict]:
    blocks: list[dict] = []
    for block in message.content:
        if isinstance(block, TextContent):
            if block.text.strip():
                blocks.append({"type": "text", "text": block.text})
        elif isinstance(block, ThinkingContent):
            if (
                block.thinking
                or block.thinking_signature is not None
                or block.redacted
            ) and (
                message.api != API
                or message.provider != model.provider
                or message.model != model.id
            ):
                raise ValueError("思考回放的 api/provider/model 与目标不一致")
            if block.redacted:
                if not block.thinking_signature:
                    raise ValueError("无法回放思考签名")
                blocks.append(
                    {"type": "redacted_thinking", "data": block.thinking_signature}
                )
            else:
                if not block.thinking_signature:
                    raise ValueError("思考内容缺少可回放的协议签名")
                blocks.append(
                    {
                        "type": "thinking",
                        "thinking": block.thinking,
                        "signature": block.thinking_signature,
                    }
                )
        elif isinstance(block, ToolCall):
            if block.thought_signature is not None or block.namespace is not None:
                raise ValueError("Anthropic Messages 无法回放工具签名或 namespace")
            blocks.append(
                {
                    "type": "tool_use",
                    "id": identifiers[block.id],
                    "name": block.name,
                    "input": block.arguments,
                }
            )
    return blocks


def _tool_result_block(
    message: ToolResultMessage, identifiers: dict[str, str]
) -> dict:
    images = [block for block in message.content if isinstance(block, ImageContent)]
    if images:
        content: str | list[dict] = _user_blocks(message.content)
    else:
        content = "\n".join(
            block.text
            for block in message.content
            if isinstance(block, TextContent)
        )
    return {
        "type": "tool_result",
        "tool_use_id": identifiers[message.tool_call_id],
        "content": content,
        "is_error": message.is_error,
    }


def _tool_declaration(tool: Tool) -> dict:
    if isinstance(tool.constrained_sampling, GrammarSampling):
        raise ValueError("Anthropic Messages 适配未声明 grammar 工具能力")
    declaration = {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.parameters,
    }
    if isinstance(tool.constrained_sampling, JsonSchemaSampling):
        declaration["strict"] = True
    return declaration


def to_anthropic_request(
    messages: list[Message], model: ModelSpec, options: StreamOptions
) -> dict:
    max_tokens = options.get("max_tokens")
    if max_tokens is None or max_tokens <= 0:
        raise ValueError("Anthropic 请求必须提供正整数 max_tokens")
    identifiers = _tool_ids(messages)
    converted: list[dict] = []
    for message in messages:
        if isinstance(message, SystemMessage):
            continue
        if isinstance(message, UserMessage):
            content = message.content
            if isinstance(content, str):
                if content.strip():
                    converted.append({"role": "user", "content": content})
            else:
                blocks = _user_blocks(content)
                if blocks:
                    converted.append({"role": "user", "content": blocks})
        elif isinstance(message, AssistantMessage):
            blocks = _assistant_blocks(message, model, identifiers)
            if blocks:
                converted.append({"role": "assistant", "content": blocks})
        elif isinstance(message, ToolResultMessage):
            converted.append(
                {
                    "role": "user",
                    "content": [_tool_result_block(message, identifiers)],
                }
            )
    request: dict = {
        "model": model.id,
        "max_tokens": max_tokens,
        "messages": converted,
    }
    prompt = get_current_system_prompt(messages)
    if prompt:
        request["system"] = prompt
    tools = get_current_tools(messages)
    if tools:
        request["tools"] = [_tool_declaration(tool) for tool in tools]
    temperature = options.get("temperature")
    if temperature is not None:
        request["temperature"] = temperature
    return request


def map_stop_reason(reason: str, stop_details) -> tuple[StopReason, str | None]:
    if reason == "end_turn":
        return ("stop", None)
    if reason == "max_tokens":
        return ("length", None)
    if reason == "tool_use":
        return ("toolUse", None)
    if reason == "pause_turn":
        return ("stop", None)
    if reason == "stop_sequence":
        return ("stop", None)
    if reason == "model_context_window_exceeded":
        return ("length", None)
    if reason == "refusal":
        explanation = stop_details.explanation if stop_details is not None else None
        return ("error", explanation or "The model refused to complete the request")
    if reason == "sensitive":
        return ("error", "Provider stopped with: sensitive")
    raise ValueError(f"未处理的 Anthropic stop_reason: {reason}")


def from_anthropic_usage(start, delta) -> Usage:
    input_tokens = start.input_tokens
    cache_read = start.cache_read_input_tokens or 0
    cache_write = start.cache_creation_input_tokens or 0
    if delta.input_tokens is not None:
        input_tokens = delta.input_tokens
    if delta.cache_read_input_tokens is not None:
        cache_read = delta.cache_read_input_tokens
    if delta.cache_creation_input_tokens is not None:
        cache_write = delta.cache_creation_input_tokens
    output = delta.output_tokens
    values = (input_tokens, cache_read, cache_write, output)
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError("响应 token 计数必须为非负整数")
    usage = {
        "input": input_tokens,
        "output": output,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "total_tokens": input_tokens + cache_read + cache_write + output,
    }
    if start.cache_creation is not None:
        one_hour = start.cache_creation.ephemeral_1h_input_tokens
        if type(one_hour) is not int or not 0 <= one_hour <= cache_write:
            raise ValueError("cache_write_1h 计数必须包含于 cache_write")
        usage["cache_write_1h"] = one_hour
    if delta.output_tokens_details is not None:
        reasoning = delta.output_tokens_details.thinking_tokens
        if type(reasoning) is not int or not 0 <= reasoning <= output:
            raise ValueError("reasoning 计数必须包含于 output")
        usage["reasoning"] = reasoning
    return Usage.model_validate(usage)


async def stream(
    model: ModelSpec, context: LlmContext, options: StreamOptions
) -> AsyncGenerator[AssistantStreamEvent, None]:
    signal = options.get("signal")
    check_cancelled(signal)
    request = to_anthropic_request(context["messages"], model, options)
    output = AssistantMessage(
        role="assistant",
        content=[],
        api=API,
        provider=model.provider,
        model=model.id,
        usage=None,
        stop_reason="pending",
        timestamp=time_ns() // 1_000_000,
    )
    blocks = output.content
    block_index: dict[int, int] = {}
    partial_json: dict[int, str] = {}
    opened: set[int] = set()
    stopped_tools: set[int] = set()
    start_usage = None
    delta_usage = None
    headers = {**(model.headers or {}), **(options.get("headers") or {})}
    async with AsyncAnthropic(
        api_key=options["api_key"],
        base_url=model.base_url,
        max_retries=0,
        timeout=60,
        default_headers=headers or None,
    ) as client:
        check_cancelled(signal)
        try:
            raw = await client.messages.create(**request, stream=True)
        except Exception as error:
            # 助手 start 前的明确容量拒绝：转换为 error 助手消息，交给上层做一次恢复。
            if not is_context_overflow_error(error):
                raise
            yield StartEvent(type="start", partial=output)
            yield DoneEvent(
                type="done",
                reason="error",
                message=build_overflow_message(output, error),
            )
            return
        async with raw:
            yield StartEvent(type="start", partial=output)
            async for event in raw:
                if signal is not None and signal.is_set():
                    break
                if event.type == "message_start":
                    output.response_id = event.message.id
                    if event.message.model != model.id:
                        output.response_model = event.message.model
                    start_usage = event.message.usage
                elif event.type == "content_block_start":
                    block = event.content_block
                    if event.index in block_index:
                        raise ValueError("Anthropic 内容块索引重复")
                    index = len(blocks)
                    block_index[event.index] = index
                    opened.add(index)
                    if block.type == "text":
                        blocks.append(TextContent(type="text", text=block.text or ""))
                        yield TextStartEvent(
                            type="text_start", content_index=index, partial=output
                        )
                    elif block.type == "thinking":
                        blocks.append(
                            ThinkingContent(
                                type="thinking",
                                thinking=block.thinking or "",
                                thinking_signature=block.signature or "",
                            )
                        )
                        yield ThinkingStartEvent(
                            type="thinking_start", content_index=index, partial=output
                        )
                    elif block.type == "redacted_thinking":
                        blocks.append(
                            ThinkingContent(
                                type="thinking",
                                thinking="",
                                thinking_signature=block.data,
                                redacted=True,
                            )
                        )
                        yield ThinkingStartEvent(
                            type="thinking_start", content_index=index, partial=output
                        )
                    elif block.type == "tool_use":
                        blocks.append(
                            ToolCall.model_validate(
                                {
                                    "type": "toolCall",
                                    "id": block.id,
                                    "name": block.name,
                                    "arguments": block.input,
                                }
                            )
                        )
                        partial_json[index] = ""
                        yield ToolCallStartEvent(
                            type="toolcall_start", content_index=index, partial=output
                        )
                    else:
                        raise ValueError(f"不支持的 Anthropic 内容块: {block.type}")
                elif event.type == "content_block_delta":
                    index = block_index[event.index]
                    if index not in opened:
                        raise ValueError("Anthropic 增量属于已结束的内容块")
                    block = blocks[index]
                    delta = event.delta
                    if delta.type == "text_delta":
                        if not isinstance(block, TextContent):
                            raise ValueError("Anthropic 文本增量缺少文本块")
                        block.text += delta.text
                        yield TextDeltaEvent(
                            type="text_delta",
                            content_index=index,
                            delta=delta.text,
                            partial=output,
                        )
                    elif delta.type == "thinking_delta":
                        if not isinstance(block, ThinkingContent):
                            raise ValueError("Anthropic 思考增量缺少思考块")
                        block.thinking += delta.thinking
                        yield ThinkingDeltaEvent(
                            type="thinking_delta",
                            content_index=index,
                            delta=delta.thinking,
                            partial=output,
                        )
                    elif delta.type == "signature_delta":
                        if not isinstance(block, ThinkingContent):
                            raise ValueError("Anthropic 签名增量缺少思考块")
                        block.thinking_signature = (
                            block.thinking_signature or ""
                        ) + delta.signature
                    elif delta.type == "input_json_delta":
                        if not isinstance(block, ToolCall):
                            raise ValueError("Anthropic 工具增量缺少工具块")
                        partial_json[index] += delta.partial_json
                        yield ToolCallDeltaEvent(
                            type="toolcall_delta",
                            content_index=index,
                            delta=delta.partial_json,
                            partial=output,
                        )
                    else:
                        raise ValueError(f"不支持的 Anthropic 增量: {delta.type}")
                elif event.type == "content_block_stop":
                    index = block_index[event.index]
                    opened.remove(index)
                    block = blocks[index]
                    if isinstance(block, TextContent):
                        yield TextEndEvent(
                            type="text_end",
                            content_index=index,
                            content=block.text,
                            partial=output,
                        )
                    elif isinstance(block, ThinkingContent):
                        yield ThinkingEndEvent(
                            type="thinking_end",
                            content_index=index,
                            content=block.thinking,
                            partial=output,
                        )
                    elif isinstance(block, ToolCall):
                        stopped_tools.add(index)
                elif event.type == "message_delta":
                    delta_usage = event.usage
                    if event.delta.stop_reason is not None:
                        output.raw_stop_reason = event.delta.stop_reason
                        reason, message = map_stop_reason(
                            event.delta.stop_reason, event.delta.stop_details
                        )
                        output.stop_reason = reason
                        if message is not None:
                            output.error_message = message
                elif event.type == "message_stop":
                    continue
                else:
                    raise ValueError(f"不支持的 Anthropic 事件: {event.type}")
    aborted = signal is not None and signal.is_set()
    if not aborted:
        if output.stop_reason == "pending":
            raise RuntimeError("Anthropic 流未返回结束原因")
        for index in opened:
            block = blocks[index]
            if output.stop_reason != "length":
                raise ValueError("Anthropic 终止前存在未结束的内容块")
            if isinstance(block, TextContent):
                yield TextEndEvent(
                    type="text_end", content_index=index, content=block.text, partial=output,
                )
            elif isinstance(block, ThinkingContent):
                yield ThinkingEndEvent(
                    type="thinking_end", content_index=index,
                    content=block.thinking, partial=output,
                )
        if output.stop_reason != "length":
            for index in sorted(stopped_tools):
                if signal is not None and signal.is_set():
                    break
                block = blocks[index]
                if partial_json[index]:
                    block.arguments = json.loads(partial_json[index])
                block = ToolCall.model_validate(block.model_dump(exclude_unset=True))
                blocks[index] = block
                yield ToolCallEndEvent(
                    type="toolcall_end", content_index=index, tool_call=block, partial=output,
                )
    aborted = signal is not None and signal.is_set()
    if start_usage is not None and delta_usage is not None:
        output.usage = from_anthropic_usage(start_usage, delta_usage)
    elif not aborted:
        raise RuntimeError("Anthropic 流缺少真实 usage")
    if aborted:
        output.stop_reason = "aborted"
    if output.stop_reason in {"length", "aborted"}:
        output.content = [block for block in blocks if not isinstance(block, ToolCall)]
    if output.stop_reason == "length":
        output.content = [
            block for block in output.content
            if not isinstance(block, ThinkingContent) or block.thinking_signature
        ]
    final_message = AssistantMessage.model_validate(output.model_dump(exclude_unset=True))
    yield DoneEvent(type="done", reason=final_message.stop_reason, message=final_message)
