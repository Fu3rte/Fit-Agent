import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

from pydantic import TypeAdapter

from app.ai.api.openai_completions import REASONING_FIELDS, to_openai_request
from app.ai.messages import (
    AssistantMessage,
    Message,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    serialize_message,
)
from app.ai.types import ModelSpec, StreamOptions
from app.model_config import load_model_config

_adapter = TypeAdapter(Message)


def check() -> None:
    config = load_model_config()
    spec = ModelSpec(
        api="openai-completions",
        provider=config.OPENAI_PROVIDER,
        id=config.OPENAI_MODEL,
        base_url=config.OPENAI_BASE_URL,
    )
    options: StreamOptions = {"api_key": "k"}
    message = AssistantMessage(
        role="assistant",
        content=[TextContent(type="text", text="answer", text_signature="原始文本签名")],
        api="openai-completions",
        provider=config.OPENAI_PROVIDER,
        model=config.OPENAI_MODEL,
        usage=Usage(input=0, output=0, cache_read=0, cache_write=0, total_tokens=0),
        stop_reason="stop",
        timestamp=0,
    )
    details = [
        {
            "type": "reasoning.encrypted",
            "data": "opaque",
            "id": "r1",
            "nested": [None, 1],
        }
    ]
    signature = json.dumps(details, ensure_ascii=False, indent=2)
    with ThreadPoolExecutor(max_workers=1) as executor:
        for marker in (*REASONING_FIELDS, signature):
            signed = deepcopy(message)
            signed.content.insert(
                0,
                ThinkingContent(
                    type="thinking",
                    thinking="思考文本" if marker in REASONING_FIELDS else "",
                    thinking_signature=marker,
                    redacted=marker == signature,
                ),
            )
            before = serialize_message(signed)
            assert serialize_message(_adapter.validate_json(before)) == before
            request = to_openai_request([signed], spec, options)
            projected = request["messages"][0]
            assert projected["content"] == "answer"
            if marker in REASONING_FIELDS:
                assert projected[marker] == "思考文本"
            else:
                assert projected["reasoning_details"] == details
                assert signed.content[0].thinking_signature == signature
            assert serialize_message(signed) == before
            for field in ("api", "provider", "model"):
                other = deepcopy(signed)
                setattr(other, field, "different-source")
                snapshot = serialize_message(other)
                failure = executor.submit(
                    to_openai_request, [other], spec, options
                ).exception()
                assert isinstance(failure, ValueError) and "api/provider/model" in str(
                    failure
                )
                assert serialize_message(other) == snapshot
        unsigned = deepcopy(message)
        unsigned.content.insert(
            0, ThinkingContent(type="thinking", thinking="思考文本")
        )
        failure = executor.submit(
            to_openai_request, [unsigned], spec, options
        ).exception()
        assert isinstance(failure, ValueError) and "缺少" in str(failure)
        invalid = deepcopy(message)
        invalid.content.insert(
            0,
            ThinkingContent(
                type="thinking",
                thinking="",
                thinking_signature='{"opaque":true}',
                redacted=True,
            ),
        )
        failure = executor.submit(
            to_openai_request, [invalid], spec, options
        ).exception()
        assert isinstance(failure, ValueError) and "无法回放" in str(failure)
        called = deepcopy(message)
        called.stop_reason = "toolUse"
        called.content = [
            ToolCall(
                type="toolCall",
                id="call-1",
                name="read",
                arguments={},
                thought_signature="原始工具签名",
            )
        ]
        result = ToolResultMessage(
            role="toolResult",
            tool_call_id="call-1",
            tool_name="read",
            content=[],
            is_error=False,
            timestamp=1,
        )
        snapshot = serialize_message(called)
        failure = executor.submit(
            to_openai_request, [called, result], spec, options
        ).exception()
        assert isinstance(failure, ValueError) and "工具签名" in str(failure)
        assert serialize_message(called) == snapshot
    request = to_openai_request([message], spec, options)
    assert request["messages"] == [{"role": "assistant", "content": "answer"}]
    assert message.content[0].text_signature == "原始文本签名"
    print(
        "请求转换字段检查：回放标记、JSON 数据、来源兼容、签名原值保护与协议拒绝检查通过"
    )


if __name__ == "__main__":
    check()
