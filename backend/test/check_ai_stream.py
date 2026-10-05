import asyncio
from concurrent.futures import CancelledError
from copy import deepcopy
from pathlib import Path
from threading import Event

import pytest
from dotenv import dotenv_values
from openai import APIConnectionError

from app.ai.api import anthropic_messages, openai_completions
from app.ai.messages import (
    AssistantMessage,
    JsonSchemaSampling,
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
from app.ai.stream import ADAPTERS, AssistantResponse, complete, stream
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
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)


def pending() -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=[],
        api="test",
        provider="test",
        model="test",
        usage=None,
        stop_reason="pending",
        timestamp=0,
    )


def final(text: str = "ok", reason: StopReason = "stop") -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=[TextContent(type="text", text=text)],
        api="test",
        provider="test",
        model="test",
        usage=Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2),
        stop_reason=reason,
        timestamp=0,
    )


async def emit(events: list[AssistantStreamEvent]):
    for event in events:
        yield event


async def contract(
    events: list[AssistantStreamEvent],
) -> tuple[list[str], AssistantMessage]:
    response = AssistantResponse(emit(events))
    kinds: list[str] = []
    async for event in response:
        kinds.append(event["type"])
    return kinds, await response.result()


def text_events() -> list[AssistantStreamEvent]:
    return [
        StartEvent(type="start", partial=pending()),
        TextStartEvent(type="text_start", content_index=0, partial=pending()),
        TextDeltaEvent(
            type="text_delta", content_index=0, delta="你好", partial=pending()
        ),
        TextEndEvent(
            type="text_end", content_index=0, content="你好", partial=pending()
        ),
        DoneEvent(type="done", reason="stop", message=final("你好")),
    ]


async def expect_error(
    events: list[AssistantStreamEvent], error: type, match: str
) -> None:
    with pytest.raises(error, match=match):
        await contract(events)


async def check_container() -> None:
    kinds, message = await contract(text_events())
    assert kinds == ["start", "text_start", "text_delta", "text_end", "done"]
    block = message.content[0]
    assert isinstance(block, TextContent) and block.text == "你好"
    assert message.usage is not None and message.stop_reason == "stop"

    await expect_error(text_events()[:-1], RuntimeError, "模型流缺少终止事件")
    await expect_error(
        [
            TextDeltaEvent(
                type="text_delta", content_index=0, delta="x", partial=pending()
            ),
        ],
        ValueError,
        "增量事件缺少开始事件",
    )
    await expect_error(
        [
            StartEvent(type="start", partial=pending()),
            StartEvent(type="start", partial=pending()),
        ],
        ValueError,
        "重复的开始事件",
    )
    await expect_error(
        [
            StartEvent(type="start", partial=pending()),
            TextStartEvent(type="text_start", content_index=1, partial=pending()),
        ],
        ValueError,
        "内容块索引不连续或重复",
    )
    await expect_error(
        [
            StartEvent(type="start", partial=pending()),
            TextStartEvent(type="text_start", content_index=0, partial=pending()),
            DoneEvent(type="done", reason="length", message=final("x")),
        ],
        ValueError,
        "终止事件前存在未结束的内容块",
    )
    await expect_error(
        [
            StartEvent(type="start", partial=pending()),
            TextStartEvent(type="text_start", content_index=0, partial=pending()),
            TextEndEvent(
                type="text_end", content_index=0, content="a", partial=pending()
            ),
            TextEndEvent(
                type="text_end", content_index=0, content="a", partial=pending()
            ),
            DoneEvent(type="done", reason="stop", message=final()),
        ],
        ValueError,
        "内容块结束缺少对应的开始事件",
    )
    await expect_error(
        [
            StartEvent(type="start", partial=pending()),
            DoneEvent(type="done", reason="stop", message=final()),
            TextStartEvent(type="text_start", content_index=0, partial=pending()),
        ],
        ValueError,
        "终止事件之后禁止继续发出事件",
    )

    async def broken():
        yield StartEvent(type="start", partial=pending())
        raise RuntimeError("source failure")

    with pytest.raises(RuntimeError, match="source failure"):
        async for _ in AssistantResponse(broken()):
            pass

    response = AssistantResponse(emit(text_events()))
    with pytest.raises(RuntimeError, match="模型流尚未完整结束"):
        await response.result()

    response = AssistantResponse(emit(text_events()))
    response.__aiter__()
    with pytest.raises(RuntimeError, match="响应只能消费一次"):
        response.__aiter__()
    await response.aclose()

    response = AssistantResponse(emit(text_events()))
    async for _ in response:
        break
    await response.aclose()

    async def synthetic(*_args, **_kwargs):
        for event in text_events():
            yield event

    ADAPTERS["test-api"] = synthetic
    spec = ModelSpec(api="test-api", provider="test", id="test", base_url="http://test")
    context: LlmContext = {
        "messages": [
            SystemMessage(role="system", content="", timestamp=0),
            UserMessage(role="user", content="hi", timestamp=0),
        ]
    }
    options: StreamOptions = {"api_key": "k"}
    response = stream(spec, context, options)
    async for _ in response:
        pass
    streamed = await response.result()
    completed = await complete(spec, context, options)
    assert streamed == completed and streamed.usage is not None

    with pytest.raises(ValueError, match="未注册的协议"):
        stream(
            ModelSpec(api="missing", provider="t", id="t", base_url="http://t"),
            context,
            options,
        )

    with pytest.raises(ValueError, match="api_key 不能为空"):
        stream(spec, context, {"api_key": ""})

    print("响应容器顺序校验、生命周期、complete 等价与入口校验通过")


def tool_contract_events() -> list[AssistantStreamEvent]:
    events: list[AssistantStreamEvent] = [
        StartEvent(type="start", partial=pending()),
        TextStartEvent(type="text_start", content_index=0, partial=pending()),
        TextDeltaEvent(
            type="text_delta", content_index=0, delta="调用", partial=pending()
        ),
        TextEndEvent(
            type="text_end", content_index=0, content="调用", partial=pending()
        ),
    ]
    for index, (identifier, raw) in enumerate(
        [("call_1", '{"text":"你'), ("call_2", '{"text":"好')]
    ):
        content_index = index + 1
        events.append(
            ToolCallStartEvent(
                type="toolcall_start", content_index=content_index, partial=pending()
            )
        )
        events.append(
            ToolCallDeltaEvent(
                type="toolcall_delta",
                content_index=content_index,
                delta=raw,
                partial=pending(),
            )
        )
        events.append(
            ToolCallEndEvent(
                type="toolcall_end",
                content_index=content_index,
                tool_call=ToolCall(
                    type="toolCall", id=identifier, name="echo", arguments={"text": "x"}
                ),
                partial=pending(),
            )
        )
    events.append(
        DoneEvent(
            type="done",
            reason="toolUse",
            message=AssistantMessage(
                role="assistant",
                content=[
                    TextContent(type="text", text="调用"),
                    ToolCall(
                        type="toolCall",
                        id="call_1",
                        name="echo",
                        arguments={"text": "你"},
                    ),
                    ToolCall(
                        type="toolCall",
                        id="call_2",
                        name="echo",
                        arguments={"text": "好"},
                    ),
                ],
                api="test",
                provider="test",
                model="test",
                usage=Usage(
                    input=1, output=1, cache_read=0, cache_write=0, total_tokens=2
                ),
                stop_reason="toolUse",
                timestamp=0,
            ),
        )
    )
    return events


async def check_tool_contract() -> None:
    kinds, message = await contract(tool_contract_events())
    assert kinds == [
        "start",
        "text_start",
        "text_delta",
        "text_end",
        "toolcall_start",
        "toolcall_delta",
        "toolcall_end",
        "toolcall_start",
        "toolcall_delta",
        "toolcall_end",
        "done",
    ]
    calls = [block for block in message.content if isinstance(block, ToolCall)]
    assert [call.id for call in calls] == ["call_1", "call_2"]
    assert calls[0].arguments == {"text": "你"} and calls[1].arguments == {"text": "好"}
    assert message.stop_reason == "toolUse"
    print("多工具调用事件顺序、内容块索引与 Unicode 参数解析通过")


def load_openai_spec() -> tuple[ModelSpec, str]:
    env = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    provider = env["OPENAI_PROVIDER"]
    identifier = env["OPENAI_MODEL"]
    base_url = env["OPENAI_BASE_URL"]
    api_key = env["OPENAI_API_KEY"]
    assert provider and identifier and base_url and api_key
    spec = ModelSpec(
        api="openai-completions",
        provider=provider,
        id=identifier,
        base_url=base_url,
    )
    return spec, api_key


def check_openai_request(spec: ModelSpec) -> None:
    tool = Tool(
        name="echo",
        description="回显文本",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    )
    system = SystemMessage(
        role="system", content="系统", tools_added=[tool], timestamp=0
    )
    long_id = "call|" + "长 ID / " * 25
    message = AssistantMessage(
        role="assistant",
        content=[
            ThinkingContent(
                type="thinking", thinking="推理", thinking_signature="reasoning_content"
            ),
            TextContent(type="text", text="回答"),
            ToolCall(type="toolCall", id=long_id, name="echo", arguments={}),
        ],
        api="openai-completions",
        provider=spec.provider,
        model=spec.id,
        usage=Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2),
        stop_reason="toolUse",
        timestamp=0,
    )
    result = ToolResultMessage(
        role="toolResult",
        tool_call_id=long_id,
        tool_name="echo",
        content=[TextContent(type="text", text="结果")],
        is_error=False,
        timestamp=1,
    )
    request = openai_completions.to_openai_request(
        [system, UserMessage(role="user", content="问", timestamp=0), message, result],
        spec,
        {"api_key": "k"},
    )
    assert request["messages"][0] == {"role": "system", "content": "系统"}
    assistant = next(item for item in request["messages"] if item["role"] == "assistant")
    tool_message = next(item for item in request["messages"] if item["role"] == "tool")
    assert assistant["reasoning_content"] == "推理"
    assert assistant["tool_calls"][0]["id"] == tool_message["tool_call_id"] != long_id
    assert len(tool_message["tool_call_id"]) <= 40
    assert request["tools"][0]["function"]["name"] == "echo"

    with pytest.raises(ValueError, match="api/provider/model"):
        cross = deepcopy(message)
        cross.provider = "other"
        openai_completions.to_openai_request([cross], spec, {"api_key": "k"})

    with pytest.raises(ValueError, match="签名"):
        unsigned = deepcopy(message)
        unsigned.content = [ThinkingContent(type="thinking", thinking="推理")]
        openai_completions.to_openai_request([unsigned], spec, {"api_key": "k"})

    strict_system = deepcopy(system)
    strict_system.tools_added = [
        Tool(
            name="echo",
            description="回显文本",
            parameters={"type": "object", "properties": {}},
            constrained_sampling=JsonSchemaSampling(
                type="json_schema", strict="require"
            ),
        )
    ]
    strict = openai_completions.to_openai_request([strict_system], spec, {"api_key": "k"})
    assert strict["tools"][0]["function"]["strict"] is True

    print("OpenAI 请求转换、ID 映射、思考回放与签名拒绝检查通过")


def check_anthropic_request() -> None:
    spec = ModelSpec(
        api="anthropic-messages",
        provider="anthropic",
        id="claude-test",
        base_url="https://api.anthropic.com",
    )
    tool = Tool(
        name="echo",
        description="回显文本",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )
    system = SystemMessage(
        role="system", content="系统", tools_added=[tool], timestamp=0
    )
    long_id = "call|" + "长 ID / " * 25
    message = AssistantMessage(
        role="assistant",
        content=[
            ThinkingContent(type="thinking", thinking="推理", thinking_signature="sig"),
            TextContent(type="text", text="回答"),
            ToolCall(
                type="toolCall", id=long_id, name="echo", arguments={"text": "你好"}
            ),
        ],
        api="anthropic-messages",
        provider="anthropic",
        model="claude-test",
        usage=Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2),
        stop_reason="toolUse",
        timestamp=0,
    )
    result = ToolResultMessage(
        role="toolResult",
        tool_call_id=long_id,
        tool_name="echo",
        content=[TextContent(type="text", text="结果")],
        is_error=False,
        timestamp=1,
    )
    options: StreamOptions = {"api_key": "k", "max_tokens": 128}
    request = anthropic_messages.to_anthropic_request(
        [system, UserMessage(role="user", content="问", timestamp=0), message, result],
        spec,
        options,
    )
    assert request["system"] == "系统"
    assert request["max_tokens"] == 128
    assistant = next(
        item for item in request["messages"] if item["role"] == "assistant"
    )
    assert assistant["content"][0] == {
        "type": "thinking",
        "thinking": "推理",
        "signature": "sig",
    }
    tool_use = next(
        block for block in assistant["content"] if block["type"] == "tool_use"
    )
    tool_result = next(
        block
        for item in request["messages"]
        if isinstance(item["content"], list)
        for block in item["content"]
        if block["type"] == "tool_result"
    )
    assert tool_use["id"] == tool_result["tool_use_id"]
    assert tool_use["id"] != long_id and len(tool_use["id"]) <= 64
    assert request["tools"][0]["input_schema"] == tool.parameters

    with pytest.raises(ValueError, match="max_tokens"):
        anthropic_messages.to_anthropic_request(
            [UserMessage(role="user", content="问", timestamp=0)],
            spec,
            {"api_key": "k"},
        )

    with pytest.raises(ValueError, match="api/provider/model"):
        invalid = deepcopy(message)
        invalid.model = "other"
        anthropic_messages.to_anthropic_request([invalid], spec, options)

    with pytest.raises(ValueError, match="签名"):
        unsigned = deepcopy(message)
        unsigned.content = [ThinkingContent(type="thinking", thinking="推理")]
        anthropic_messages.to_anthropic_request([unsigned], spec, options)

    redacted = deepcopy(message)
    redacted.content = [
        ThinkingContent(
            type="thinking", thinking="", thinking_signature="opaque", redacted=True
        )
    ]
    converted = anthropic_messages.to_anthropic_request([redacted], spec, options)
    assert converted["messages"][0]["content"][0] == {
        "type": "redacted_thinking",
        "data": "opaque",
    }

    print("Anthropic 请求转换、max_tokens、ID 映射、思考回放与签名拒绝检查通过")


def set_event() -> Event:
    event = Event()
    event.set()
    return event


async def check_openai_real(spec: ModelSpec, api_key: str) -> None:
    options: StreamOptions = {"api_key": api_key}
    text_context: LlmContext = {
        "messages": [
            SystemMessage(role="system", content="按用户要求回答。", timestamp=0),
            UserMessage(role="user", content="只回答完成。", timestamp=0),
        ]
    }
    response = stream(spec, text_context, options)
    kinds: list[str] = []
    text: list[str] = []
    async for event in response:
        kinds.append(event["type"])
        if event["type"] == "text_delta":
            text.append(event["delta"])
    message = await response.result()
    assert kinds[0] == "start" and kinds[-1] == "done"
    assert message.stop_reason == "stop" and message.usage is not None
    assert message.usage.total_tokens > 0 and message.api == "openai-completions"
    assert message.provider == spec.provider and message.model == spec.id
    assert "".join(text).strip()
    completed = await complete(spec, text_context, options)
    assert completed.stop_reason == "stop" and completed.usage is not None

    tool = Tool(
        name="echo",
        description="回显给定文本。",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    )
    tool_context: LlmContext = {
        "messages": [
            SystemMessage(
                role="system",
                content="必须调用提供的工具。",
                tools_added=[tool],
                timestamp=0,
            ),
            UserMessage(
                role="user",
                content="必须调用 echo 工具，参数 text 为 完成，随后报告工具返回的文本。",
                timestamp=0,
            ),
        ]
    }
    response = stream(spec, tool_context, options)
    tool_calls: list[ToolCall] = []
    async for event in response:
        if event["type"] == "toolcall_end":
            tool_calls.append(event["tool_call"])
    assistant = await response.result()
    assert assistant.stop_reason == "toolUse" and tool_calls
    assert all(isinstance(call.arguments, dict) for call in tool_calls)

    follow: LlmContext = {
        "messages": [
            *tool_context["messages"],
            assistant,
            *[
                ToolResultMessage(
                    role="toolResult",
                    tool_call_id=call.id,
                    tool_name=call.name,
                    content=[TextContent(type="text", text="完成")],
                    is_error=False,
                    timestamp=0,
                )
                for call in tool_calls
            ],
            UserMessage(role="user", content="报告工具返回的文本。", timestamp=0),
        ]
    }
    finished = await complete(spec, follow, options)
    assert finished.usage is not None
    assert "".join(
        block.text for block in finished.content if isinstance(block, TextContent)
    ).strip()

    cancel_options: StreamOptions = {"api_key": api_key, "signal": set_event()}
    response = stream(spec, text_context, cancel_options)
    with pytest.raises(CancelledError, match="执行已取消"):
        async for _ in response:
            pass
    await response.aclose()

    broken = ModelSpec(
        api="openai-completions",
        provider="local",
        id=spec.id,
        base_url="http://127.0.0.1:1/v1",
    )
    response = stream(broken, text_context, options)
    with pytest.raises(APIConnectionError):
        async for _ in response:
            pass
    await response.aclose()

    response = stream(spec, text_context, options)
    async for _ in response:
        break
    await response.aclose()

    print("真实 OpenAI 文本、工具循环、用量、取消、请求失败与提前关闭检查通过")


def run_checked(coro) -> None:
    loop = asyncio.new_event_loop()
    errors: list = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(coro)
        loop.run_until_complete(loop.shutdown_asyncgens())
    finally:
        asyncio.set_event_loop(None)
        loop.close()
    assert not errors, errors


def check() -> None:
    asyncio.run(check_container())
    asyncio.run(check_tool_contract())
    spec, api_key = load_openai_spec()
    check_openai_request(spec)
    check_anthropic_request()
    run_checked(check_openai_real(spec, api_key))
    print("真实请求多次调用、提前关闭与取消后的流与客户端收尾检查通过")
    print("Anthropic 真实请求验证：缺少端点及凭证，未执行（仅本地契约与输入校验）")


if __name__ == "__main__":
    check()
