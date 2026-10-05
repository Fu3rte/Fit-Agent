import hashlib
import json
import re
from collections.abc import AsyncGenerator
from time import time_ns

import httpx
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessage

from app.ai.context import get_current_system_prompt, get_current_tools
from app.ai.messages import (
    AssistantMessage,
    GrammarSampling,
    ImageContent,
    JsonObject,
    JsonSchemaSampling,
    Message,
    SystemMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
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

API: str = "openai-completions"

REASONING_FIELDS = ("reasoning_content", "reasoning", "reasoning_text")


def image_part(block: ImageContent) -> dict:
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{block.mime_type};base64,{block.data}"},
    }


def to_openai_request(
    messages: list[Message], model: ModelSpec, options: StreamOptions
) -> dict:
    identifiers = {}
    used = set()
    for message in messages:
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolCall):
                    identifier = block.id
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", identifier):
                        identifier = (
                            "call_" + hashlib.sha256(block.id.encode()).hexdigest()[:32]
                        )
                    if identifier in used:
                        raise ValueError("规范化工具调用 ID 冲突")
                    used.add(identifier)
                    identifiers[block.id] = identifier
    payload = []
    prompt = get_current_system_prompt(messages)
    if prompt:
        payload.append({"role": "system", "content": prompt})
    images = []
    for message in messages:
        if isinstance(message, SystemMessage):
            continue
        if not isinstance(message, ToolResultMessage) and images:
            payload.append({"role": "user", "content": images})
            images = []
        if isinstance(message, UserMessage):
            content = message.content
            if not isinstance(content, str):
                content = [
                    {"type": "text", "text": block.text}
                    if isinstance(block, TextContent)
                    else image_part(block)
                    for block in content
                ]
            payload.append({"role": "user", "content": content})
        elif isinstance(message, AssistantMessage):
            assistant = {"role": "assistant", "content": None}
            text = "".join(
                block.text
                for block in message.content
                if isinstance(block, TextContent)
            )
            if text:
                assistant["content"] = text
            calls = []
            for block in message.content:
                if isinstance(block, ThinkingContent):
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
                    signature = block.thinking_signature
                    if signature in REASONING_FIELDS and not block.redacted:
                        assistant[signature] = (
                            assistant.get(signature, "") + block.thinking
                        )
                    elif signature is not None:
                        details = json.loads(signature)
                        if (
                            not isinstance(details, list)
                            or "reasoning_details" in assistant
                        ):
                            raise ValueError("无法回放思考签名")
                        assistant["reasoning_details"] = details
                    elif block.thinking or block.redacted:
                        raise ValueError("思考内容缺少可回放的协议签名")
                elif isinstance(block, ToolCall):
                    if (
                        block.thought_signature is not None
                        or block.namespace is not None
                    ):
                        raise ValueError(
                            "Chat Completions 无法回放工具签名或 namespace"
                        )
                    calls.append(
                        {
                            "id": identifiers[block.id],
                            "type": "function",
                            "function": {
                                "name": block.name,
                                "arguments": json.dumps(
                                    block.arguments, ensure_ascii=False, allow_nan=False
                                ),
                            },
                        }
                    )
            if calls:
                assistant["tool_calls"] = calls
            if text or calls or len(assistant) > 2:
                payload.append(assistant)
        elif isinstance(message, ToolResultMessage):
            payload.append(
                {
                    "role": "tool",
                    "tool_call_id": identifiers[message.tool_call_id],
                    "content": "\n".join(
                        block.text
                        for block in message.content
                        if isinstance(block, TextContent)
                    ),
                }
            )
            attached = [
                image_part(block)
                for block in message.content
                if isinstance(block, ImageContent)
            ]
            if attached:
                images.append(
                    {
                        "type": "text",
                        "text": f"工具 {message.tool_name}（调用 {message.tool_call_id}）返回的图片：",
                    }
                )
                images.extend(attached)
    if images:
        payload.append({"role": "user", "content": images})
    request = {"model": model.id, "messages": payload}
    tools = []
    for tool in get_current_tools(messages):
        function = tool.model_dump(
            exclude_unset=True, exclude={"constrained_sampling"}
        )
        if isinstance(tool.constrained_sampling, GrammarSampling):
            raise ValueError("当前 Chat Completions 适配未声明 grammar 工具能力")
        if isinstance(tool.constrained_sampling, JsonSchemaSampling):
            function["strict"] = True
        tools.append({"type": "function", "function": function})
    if tools:
        request["tools"] = tools
    max_tokens = options.get("max_tokens")
    if max_tokens is not None:
        request["max_tokens"] = max_tokens
    temperature = options.get("temperature")
    if temperature is not None:
        request["temperature"] = temperature
    return request


def from_openai_usage(raw: dict) -> Usage:
    prompt = raw["prompt_tokens"]
    output = raw["completion_tokens"]
    total = raw["total_tokens"]
    details = raw["prompt_tokens_details"]
    cached = details["cached_tokens"]
    written = details.get("cache_write_tokens", 0)
    if any(
        type(value) is not int or value < 0
        for value in (prompt, output, total, cached, written)
    ):
        raise ValueError("响应 token 计数必须为非负整数")
    if cached + written > prompt or total != prompt + output:
        raise ValueError("响应 token 计数不一致")
    usage = {
        "input": prompt - cached - written,
        "output": output,
        "cache_read": cached,
        "cache_write": written,
        "total_tokens": total,
    }
    if (
        "completion_tokens_details" in raw
        and "reasoning_tokens" in raw["completion_tokens_details"]
    ):
        reasoning = raw["completion_tokens_details"]["reasoning_tokens"]
        if type(reasoning) is not int or not 0 <= reasoning <= output:
            raise ValueError("reasoning 计数必须包含于 output")
        usage["reasoning"] = reasoning
    if "cache_write_1h_tokens" in details:
        one_hour = details["cache_write_1h_tokens"]
        if type(one_hour) is not int or not 0 <= one_hour <= written:
            raise ValueError("cache_write_1h 计数必须包含于 cache_write")
        usage["cache_write_1h"] = one_hour
    if "cost" in raw:
        usage["cost"] = raw["cost"]
    return Usage.model_validate(usage)


STOP_REASONS = {
    "stop": "stop",
    "tool_calls": "toolUse",
    "length": "length",
    "content_filter": "error",
}


def _assistant_message(
    *,
    model: ModelSpec,
    content: list,
    usage: Usage | None,
    finish_reason: str | None,
    response_id: str | None,
    response_model: str | None,
    timestamp: int,
    aborted: bool = False,
) -> AssistantMessage:
    reason = "aborted" if aborted else STOP_REASONS.get(finish_reason)
    if reason is None:
        raise ValueError(f"未处理的 Chat Completions finish_reason: {finish_reason}")
    values: dict = {
        "role": "assistant",
        "content": (
            [block for block in content if not isinstance(block, ToolCall)]
            if reason in {"length", "aborted"} else content
        ),
        "api": API,
        "provider": model.provider,
        "model": model.id,
        "usage": usage,
        "stop_reason": reason,
        "timestamp": timestamp,
    }
    if response_id is not None:
        values["response_id"] = response_id
    if response_model is not None:
        values["response_model"] = response_model
    if finish_reason is not None:
        values["raw_stop_reason"] = finish_reason
    if reason == "error":
        values["error_message"] = "Provider finish_reason: content_filter"
    return AssistantMessage.model_validate(values)


def _content_blocks(message: ChatCompletionMessage, finish_reason: str) -> list:
    content: list = []
    fields = message.model_dump(exclude_none=True)
    for field in REASONING_FIELDS:
        if field in fields and fields[field] != "":
            content.append(
                ThinkingContent(
                    type="thinking", thinking=fields[field], thinking_signature=field
                )
            )
            break
    if "reasoning_details" in fields:
        if not isinstance(fields["reasoning_details"], list):
            raise ValueError("reasoning_details 必须为完整 JSON 数组")
        content.append(
            ThinkingContent(
                type="thinking",
                thinking="",
                thinking_signature=json.dumps(
                    fields["reasoning_details"], ensure_ascii=False, allow_nan=False
                ),
                redacted=True,
            )
        )
    if message.content is not None:
        content.append(TextContent(type="text", text=message.content))
    if message.refusal is not None:
        content.append(TextContent(type="text", text=message.refusal))
    if message.function_call is not None:
        raise ValueError("旧式 function_call 缺少工具调用 ID")
    if message.tool_calls is not None and finish_reason != "length":
        for call in message.tool_calls:
            if call.type != "function":
                raise ValueError("当前仅接受 function 工具调用")
            content.append(
                ToolCall(
                    type="toolCall",
                    id=call.id,
                    name=call.function.name,
                    arguments=json.loads(call.function.arguments),
                )
            )
    return content


def from_openai_response(response: ChatCompletion, model: ModelSpec) -> AssistantMessage:
    if len(response.choices) != 1:
        raise ValueError("需要且仅接受一个完整助手响应")
    if response.usage is None:
        raise ValueError("响应缺少真实 usage")
    choice = response.choices[0]
    if choice.message.role != "assistant":
        raise ValueError("Chat Completions 响应角色必须为 assistant")
    return _assistant_message(
        model=model,
        content=_content_blocks(choice.message, choice.finish_reason),
        usage=from_openai_usage(response.usage.model_dump(exclude_unset=True)),
        finish_reason=choice.finish_reason,
        response_id=response.id,
        response_model=response.model,
        timestamp=response.created * 1000,
    )


def _parse_tool_arguments(block: ToolCall, raw: str) -> JsonObject:
    if not raw:
        raise ValueError("工具调用参数不完整")
    return ToolCall.model_validate(
        {
            "type": "toolCall",
            "id": block.id,
            "name": block.name,
            "arguments": json.loads(raw),
        }
    ).arguments


def _text_block(blocks: list, index: int) -> TextContent:
    block = blocks[index]
    if not isinstance(block, TextContent):
        raise ValueError("文本事件缺少对应文本块")
    return block


def _thinking_block(blocks: list, index: int) -> ThinkingContent:
    block = blocks[index]
    if not isinstance(block, ThinkingContent):
        raise ValueError("思考事件缺少对应思考块")
    return block


def _tool_call_block(blocks: list, index: int) -> ToolCall:
    block = blocks[index]
    if not isinstance(block, ToolCall):
        raise ValueError("工具事件缺少对应工具块")
    return block


async def stream(
    model: ModelSpec, context: LlmContext, options: StreamOptions
) -> AsyncGenerator[AssistantStreamEvent, None]:
    signal = options.get("signal")
    check_cancelled(signal)
    request = to_openai_request(context["messages"], model, options)
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
    tool_index: dict[int, int] = {}
    tool_arguments: dict[int, str] = {}
    open_blocks: dict[int, str] = {}
    text_index: int | None = None
    thinking_index: int | None = None
    thinking_field: str | None = None
    reasoning_details: list | None = None
    usage: Usage | None = None
    response_id: str | None = None
    response_model: str | None = None
    created: int | None = None
    finish_reason: str | None = None
    headers = {**(model.headers or {}), **(options.get("headers") or {})}
    async with httpx.AsyncClient() as http_client:
        async with AsyncOpenAI(
            api_key=options["api_key"],
            base_url=model.base_url,
            max_retries=0,
            timeout=120,
            default_headers=headers or None,
            http_client=http_client,
        ) as client:
            check_cancelled(signal)
            raw = await client.chat.completions.create(
                **request, stream=True, stream_options={"include_usage": True}
            )
            async with raw:
                yield StartEvent(type="start", partial=output)
                async for chunk in raw:
                    if signal is not None and signal.is_set():
                        break
                    if response_id is None and chunk.id:
                        response_id = chunk.id
                    if response_model is None and chunk.model:
                        response_model = chunk.model
                    if created is None and chunk.created is not None:
                        created = chunk.created * 1000
                    if chunk.usage is not None:
                        usage = from_openai_usage(
                            chunk.usage.model_dump(exclude_unset=True)
                        )
                    for choice in chunk.choices:
                        role = choice.delta.role
                        if role is not None and role != "assistant":
                            raise ValueError(f"模型流角色不一致：{role}")
                        if choice.finish_reason is not None:
                            if choice.finish_reason not in STOP_REASONS:
                                raise ValueError(
                                    f"未处理的 Chat Completions finish_reason: {choice.finish_reason}"
                                )
                            finish_reason = choice.finish_reason
                        delta = choice.delta
                        fields = delta.model_dump(exclude_none=True)
                        reasoning = next(
                            (
                                fields[field]
                                for field in REASONING_FIELDS
                                if isinstance(fields.get(field), str) and fields[field]
                            ),
                            None,
                        )
                        if reasoning is not None:
                            if thinking_index is None:
                                thinking_index = len(blocks)
                                blocks.append(
                                    ThinkingContent(type="thinking", thinking="")
                                )
                                open_blocks[thinking_index] = "thinking"
                                yield ThinkingStartEvent(
                                    type="thinking_start",
                                    content_index=thinking_index,
                                    partial=output,
                                )
                            if thinking_field is None:
                                thinking_field = next(
                                    field
                                    for field in REASONING_FIELDS
                                    if fields.get(field)
                                )
                            _thinking_block(blocks, thinking_index).thinking += reasoning
                            yield ThinkingDeltaEvent(
                                type="thinking_delta",
                                content_index=thinking_index,
                                delta=reasoning,
                                partial=output,
                            )
                        if "reasoning_details" in fields:
                            details = fields["reasoning_details"]
                            if not isinstance(details, list):
                                raise ValueError("reasoning_details 必须为完整 JSON 数组")
                            reasoning_details = details
                        piece = (
                            delta.content if delta.content is not None else delta.refusal
                        )
                        if piece is not None:
                            if text_index is None:
                                text_index = len(blocks)
                                blocks.append(TextContent(type="text", text=""))
                                open_blocks[text_index] = "text"
                                yield TextStartEvent(
                                    type="text_start",
                                    content_index=text_index,
                                    partial=output,
                                )
                            _text_block(blocks, text_index).text += piece
                            yield TextDeltaEvent(
                                type="text_delta",
                                content_index=text_index,
                                delta=piece,
                                partial=output,
                            )
                        if delta.function_call is not None:
                            raise ValueError("旧式 function_call 缺少工具调用 ID")
                        for tool_delta in delta.tool_calls or []:
                            index = tool_index.get(tool_delta.index)
                            if index is None:
                                if (
                                    tool_delta.id is None
                                    or tool_delta.function is None
                                    or tool_delta.function.name is None
                                ):
                                    raise ValueError("工具调用增量缺少 id 或 name")
                                index = len(blocks)
                                blocks.append(
                                    ToolCall(
                                        type="toolCall",
                                        id=tool_delta.id,
                                        name=tool_delta.function.name,
                                        arguments={},
                                    )
                                )
                                tool_index[tool_delta.index] = index
                                tool_arguments[index] = ""
                                open_blocks[index] = "toolcall"
                                yield ToolCallStartEvent(
                                    type="toolcall_start",
                                    content_index=index,
                                    partial=output,
                                )
                            fragment = (
                                tool_delta.function.arguments
                                if tool_delta.function is not None
                                else None
                            )
                            if fragment:
                                tool_arguments[index] += fragment
                                yield ToolCallDeltaEvent(
                                    type="toolcall_delta",
                                    content_index=index,
                                    delta=fragment,
                                    partial=output,
                                )
    aborted = signal is not None and signal.is_set()
    if thinking_index is not None:
        if thinking_field is None and not aborted:
            raise ValueError("思考内容缺少可回放的协议签名")
        if thinking_field is not None:
            _thinking_block(blocks, thinking_index).thinking_signature = thinking_field
    if not aborted:
        for index, kind in open_blocks.items():
            if signal is not None and signal.is_set():
                break
            if kind == "text":
                yield TextEndEvent(
                    type="text_end", content_index=index,
                    content=_text_block(blocks, index).text, partial=output,
                )
            elif kind == "thinking":
                yield ThinkingEndEvent(
                    type="thinking_end", content_index=index,
                    content=_thinking_block(blocks, index).thinking, partial=output,
                )
            elif finish_reason != "length":
                block = _tool_call_block(blocks, index)
                block.arguments = _parse_tool_arguments(block, tool_arguments[index])
                yield ToolCallEndEvent(
                    type="toolcall_end", content_index=index,
                    tool_call=block, partial=output,
                )
    if reasoning_details is not None:
        detail_index = len(blocks)
        blocks.append(
            ThinkingContent(
                type="thinking", thinking="",
                thinking_signature=json.dumps(
                    reasoning_details, ensure_ascii=False, allow_nan=False
                ),
                redacted=True,
            )
        )
        if signal is None or not signal.is_set():
            yield ThinkingStartEvent(
                type="thinking_start", content_index=detail_index, partial=output,
            )
            yield ThinkingEndEvent(
                type="thinking_end", content_index=detail_index, content="", partial=output,
            )
    aborted = signal is not None and signal.is_set()
    if usage is None and not aborted:
        raise ValueError("响应缺少真实 usage")
    final_message = _assistant_message(
        model=model, content=blocks, usage=usage, finish_reason=finish_reason,
        response_id=response_id, response_model=response_model,
        timestamp=created if created is not None else output.timestamp,
        aborted=aborted,
    )
    yield DoneEvent(type="done", reason=final_message.stop_reason, message=final_message)
