import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

from src.agent.messages import (
    AssistantMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    parse_agent_message,
    serialize_message,
)
from src.agent.providers.openai import REASONING_FIELDS, to_openai_request
from src.model_config import load_model_config


def check() -> None:
    config = load_model_config()
    message = AssistantMessage(
        role="assistant",
        content=[TextContent(type="text", text="answer", textSignature="原始文本签名")],
        api="openai-completions",
        provider=config.OPENAI_PROVIDER,
        model=config.OPENAI_MODEL,
        usage=Usage(input=0, output=0, cacheRead=0, cacheWrite=0, totalTokens=0),
        stopReason="stop",
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
                    thinkingSignature=marker,
                    redacted=marker == signature,
                ),
            )
            before = serialize_message(signed)
            assert serialize_message(parse_agent_message(before)) == before
            request = to_openai_request([signed], config)
            projected = request["messages"][0]
            assert projected["content"] == "answer"
            if marker in REASONING_FIELDS:
                assert projected[marker] == "思考文本"
            else:
                assert projected["reasoning_details"] == details
                assert signed.content[0].thinkingSignature == signature
            assert serialize_message(signed) == before
            for field in ("api", "provider", "model"):
                other = deepcopy(signed)
                setattr(other, field, "different-source")
                snapshot = serialize_message(other)
                failure = executor.submit(
                    to_openai_request, [other], config
                ).exception()
                assert isinstance(failure, ValueError) and "api/provider/model" in str(
                    failure
                )
                assert serialize_message(other) == snapshot
        unsigned = deepcopy(message)
        unsigned.content.insert(
            0, ThinkingContent(type="thinking", thinking="思考文本")
        )
        failure = executor.submit(to_openai_request, [unsigned], config).exception()
        assert isinstance(failure, ValueError) and "缺少" in str(failure)
        invalid = deepcopy(message)
        invalid.content.insert(
            0,
            ThinkingContent(
                type="thinking",
                thinking="",
                thinkingSignature='{"opaque":true}',
                redacted=True,
            ),
        )
        failure = executor.submit(to_openai_request, [invalid], config).exception()
        assert isinstance(failure, ValueError) and "无法回放" in str(failure)
        called = deepcopy(message)
        called.stopReason = "toolUse"
        called.content = [
            ToolCall(
                type="toolCall",
                id="call-1",
                name="read",
                arguments={},
                thoughtSignature="原始工具签名",
            )
        ]
        result = ToolResultMessage(
            role="toolResult",
            toolCallId="call-1",
            toolName="read",
            content=[],
            isError=False,
            timestamp=1,
        )
        snapshot = serialize_message(called)
        failure = executor.submit(
            to_openai_request, [called, result], config
        ).exception()
        assert isinstance(failure, ValueError) and "工具签名" in str(failure)
        assert serialize_message(called) == snapshot
    request = to_openai_request([message], config)
    assert request["messages"] == [{"role": "assistant", "content": "answer"}]
    assert message.content[0].textSignature == "原始文本签名"
    print(
        "请求转换字段检查：回放标记、JSON 数据、来源兼容、签名原值保护与协议拒绝检查通过"
    )


if __name__ == "__main__":
    check()
