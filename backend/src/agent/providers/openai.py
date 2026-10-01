import hashlib
import json
import re

from openai import OpenAI
from openai.types.chat import ChatCompletion

from src.agent.message_context import (
    get_current_system_prompt,
    get_current_tools,
    normalize_context,
)
from src.agent.messages import (
    AssistantMessage,
    GrammarSampling,
    ImageContent,
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
from src.model_config import ModelConfig

REASONING_FIELDS = ("reasoning_content", "reasoning", "reasoning_text")


def image_part(block: ImageContent) -> dict:
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{block.mimeType};base64,{block.data}"},
    }


def to_openai_request(messages: list[Message], config: ModelConfig) -> dict:
    projected = normalize_context(messages)
    identifiers = {}
    used = set()
    for message in projected:
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
    prompt = get_current_system_prompt(projected)
    if prompt:
        payload.append({"role": "system", "content": prompt})
    images = []
    for message in projected:
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
                        or block.thinkingSignature is not None
                        or block.redacted
                    ) and (
                        message.api != "openai-completions"
                        or message.provider != config.OPENAI_PROVIDER
                        or message.model != config.OPENAI_MODEL
                    ):
                        raise ValueError("思考回放的 api/provider/model 与目标不一致")
                    signature = block.thinkingSignature
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
                        block.thoughtSignature is not None
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
                    "tool_call_id": identifiers[message.toolCallId],
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
                        "text": f"工具 {message.toolName}（调用 {message.toolCallId}）返回的图片：",
                    }
                )
                images.extend(attached)
    if images:
        payload.append({"role": "user", "content": images})
    request = {"model": config.OPENAI_MODEL, "messages": payload}
    tools = []
    for tool in get_current_tools(projected):
        function = tool.model_dump(exclude_unset=True, exclude={"constrainedSampling"})
        if isinstance(tool.constrainedSampling, GrammarSampling):
            raise ValueError("当前 Chat Completions 适配未声明 grammar 工具能力")
        if isinstance(tool.constrainedSampling, JsonSchemaSampling):
            function["strict"] = True
        tools.append({"type": "function", "function": function})
    if tools:
        request["tools"] = tools
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
        "cacheRead": cached,
        "cacheWrite": written,
        "totalTokens": total,
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
            raise ValueError("cacheWrite1h 计数必须包含于 cacheWrite")
        usage["cacheWrite1h"] = one_hour
    if "cost" in raw:
        usage["cost"] = raw["cost"]
    return Usage.model_validate(usage)


def from_openai_response(
    response: ChatCompletion, config: ModelConfig
) -> AssistantMessage:
    if len(response.choices) != 1:
        raise ValueError("需要且仅接受一个完整助手响应")
    choice = response.choices[0]
    reasons = {
        "stop": "stop",
        "tool_calls": "toolUse",
        "length": "length",
        "content_filter": "error",
    }
    reason = reasons[choice.finish_reason]
    if response.usage is None:
        raise ValueError("响应缺少真实 usage")
    usage = from_openai_usage(response.usage.model_dump(exclude_unset=True))
    content = []
    message = choice.message
    if message.role != "assistant":
        raise ValueError("Chat Completions 响应角色必须为 assistant")
    fields = message.model_dump(exclude_none=True)
    for field in REASONING_FIELDS:
        if field in fields and fields[field] != "":
            content.append(
                ThinkingContent(
                    type="thinking", thinking=fields[field], thinkingSignature=field
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
                thinkingSignature=json.dumps(
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
    if message.tool_calls is not None:
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
    values = {
        "role": "assistant",
        "content": content,
        "api": "openai-completions",
        "provider": config.OPENAI_PROVIDER,
        "model": config.OPENAI_MODEL,
        "responseModel": response.model,
        "responseId": response.id,
        "timestamp": response.created * 1000,
        "usage": usage,
        "stopReason": reason,
        "rawStopReason": choice.finish_reason,
    }
    if reason == "error":
        values["errorMessage"] = "Provider finish_reason: content_filter"
    return AssistantMessage.model_validate(values)


def complete(
    client: OpenAI, config: ModelConfig, messages: list[Message]
) -> AssistantMessage:
    response = client.chat.completions.create(**to_openai_request(messages, config))
    return from_openai_response(response, config)
