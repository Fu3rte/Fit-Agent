import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

from pydantic import BaseModel, TypeAdapter, ValidationError

from src.ai import messages as message_models
from src.ai.messages import (
    AssistantMessage,
    Message,
    SystemMessage,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    serialize_message,
)

_adapter = TypeAdapter(Message)


def assert_rejected(source: dict, model: str = "Message") -> None:
    with ThreadPoolExecutor(max_workers=1) as executor:
        error = executor.submit(
            TypeAdapter(getattr(message_models, model)).validate_python, source
        ).exception()
    assert isinstance(error, ValidationError), error


def roundtrip(source: dict) -> Message:
    parsed = _adapter.validate_python(source)
    encoded = serialize_message(parsed)
    assert json.loads(encoded) == source
    assert _adapter.validate_json(encoded) == parsed
    return parsed


def check() -> None:
    messages: list[dict] = [
        {
            "role": "system",
            "content": [
                {"type": "text", "text": "instructions", "text_signature": "sig"}
            ],
            "sections": {"removed": None, "active": "指令片段"},
            "tools_removed": [{"name": "old-tool"}],
            "tools_added": [
                {
                    "name": "lookup",
                    "description": "lookup data",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "examples": ["a", {"b": 1}]}
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                        "x-provider": {"nested": [None, True, 2.5]},
                    },
                    "constrained_sampling": {
                        "type": "grammar",
                        "variants": {"openai_regex": "[a-z]+"},
                    },
                }
            ],
            "timestamp": 1.5,
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "question"},
                {"type": "image", "data": "base64", "mime_type": "image/png"},
            ],
            "timestamp": 2,
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "toolCall",
                    "id": "call-1",
                    "name": "lookup",
                    "arguments": {"query": "a", "nested": {"values": [1, None]}},
                    "thought_signature": "thought-signature",
                    "namespace": "tools",
                },
                {
                    "type": "thinking",
                    "thinking": "plan",
                    "thinking_signature": "encrypted-replay-payload",
                    "redacted": True,
                },
                {
                    "type": "text",
                    "text": "done",
                    "text_signature": '{"v":1,"id":"response-id","phase":"final_answer"}',
                },
            ],
            "api": "openai-completions",
            "provider": "example",
            "model": "model-1",
            "response_model": "actual-model",
            "response_id": "response-1",
            "provider_thinking_level": "custom-effort",
            "thinking_level": "off",
            "error_message": "safe-error",
            "raw_stop_reason": "provider-stop",
            "end_turn": False,
            "usage": {
                "input": 1,
                "output": 2,
                "cache_read": 3,
                "cache_write": 4,
                "cache_write_1h": 5,
                "reasoning": 1,
                "total_tokens": 3,
                "cost": {
                    "input": 0.1,
                    "output": 0.2,
                    "cache_read": 0.3,
                    "cache_write": 0.4,
                    "total": 1,
                },
            },
            "stop_reason": "toolUse",
            "diagnostics": [
                {
                    "type": "provider",
                    "timestamp": 3,
                    "error": {
                        "name": "Error",
                        "message": "safe",
                        "stack": "redacted-stack",
                        "code": 7.5,
                    },
                    "details": {"response": {"status": 429}},
                }
            ],
            "deferred": {
                "provider": "example",
                "model_id": "model-1",
                "api": "custom",
                "id": "task-1",
                "expires_at": 123.5,
                "poll_after_ms": 2.5,
                "data": {"cursor": [1, {"next": None}]},
            },
            "timestamp": 3,
        },
        {
            "role": "toolResult",
            "tool_call_id": "call-1",
            "tool_name": "lookup",
            "content": [
                {"type": "text", "text": "result"},
                {"type": "image", "data": "base64", "mime_type": "image/jpeg"},
            ],
            "is_error": False,
            "timestamp": 4,
            "details": None,
            "nested_calls": {
                "calls": [
                    {
                        "id": "nested-1",
                        "name": "fetch",
                        "arguments": {"id": 9},
                        "arguments_bytes": 8.5,
                        "status": "ok",
                        "duration_ms": 2.5,
                        "error": "safe-error",
                    }
                ],
                "complete": True,
            },
        },
    ]

    messages[3]["usage"] = deepcopy(messages[2]["usage"])
    classes = [SystemMessage, UserMessage, AssistantMessage, ToolResultMessage]
    for source, model in zip(messages, classes, strict=True):
        parsed = _adapter.validate_json(json.dumps(source).encode())
        restored = _adapter.validate_json(serialize_message(parsed))
        assert type(restored) is model and restored == parsed
        assert json.loads(serialize_message(restored)) == source
        assert TypeAdapter(Message).validate_python(source) == parsed

    required = {
        "SystemMessage": {"role", "content", "timestamp"},
        "UserMessage": {"role", "content", "timestamp"},
        "AssistantMessage": {
            "role",
            "content",
            "api",
            "provider",
            "model",
            "usage",
            "stop_reason",
            "timestamp",
        },
        "ToolResultMessage": {
            "role",
            "tool_call_id",
            "tool_name",
            "content",
            "is_error",
            "timestamp",
        },
        "TextContent": {"type", "text"},
        "ImageContent": {"type", "data", "mime_type"},
        "ThinkingContent": {"type", "thinking"},
        "ToolCall": {"type", "id", "name", "arguments"},
        "Usage": {"input", "output", "cache_read", "cache_write", "total_tokens"},
        "UsageCost": {"input", "output", "cache_read", "cache_write", "total"},
        "DeferredHandle": {"provider", "model_id", "api", "id"},
        "AssistantMessageDiagnostic": {"type", "timestamp"},
        "DiagnosticErrorInfo": {"message"},
        "NestedToolCalls": {"calls", "complete"},
        "NestedToolCallRecord": {"id", "name", "status"},
        "Tool": {"name", "description", "parameters"},
        "ToolReference": {"name"},
        "GrammarSampling": {"type", "variants"},
        "GrammarVariants": set(),
        "JsonSchemaSampling": {"type", "strict"},
    }
    checked = set()

    def check_fields(model: BaseModel) -> None:
        name = type(model).__name__
        if name in checked:
            return
        checked.add(name)
        fields = type(model).model_fields
        assert {
            key for key, field in fields.items() if field.is_required()
        } == required[name]
        source = model.model_dump(exclude_unset=True)
        minimal = {key: source[key] for key in required[name]}
        restored = type(model).model_validate(minimal)
        assert restored.model_dump(exclude_unset=True) == minimal
        for key in required[name]:
            assert_rejected({k: v for k, v in minimal.items() if k != key}, name)
        for key in fields:
            nullable = (name, key) in {
                ("ToolResultMessage", "details"),
                ("DeferredHandle", "data"),
            }
            if nullable:
                assert type(model).model_validate({**minimal, key: None}).model_dump(
                    exclude_unset=True
                ) == {**minimal, key: None}
            else:
                assert_rejected({**minimal, key: None}, name)
            value = getattr(model, key)
            for item in value if isinstance(value, list) else [value]:
                if isinstance(item, BaseModel):
                    check_fields(item)

    for source in messages:
        check_fields(_adapter.validate_python(source))
    for sampling in (
        False,
        {"type": "json_schema", "strict": "prefer"},
        {"type": "json_schema", "strict": "require"},
        {"type": "grammar", "variants": {}},
        {
            "type": "grammar",
            "variants": {"openai_lark": "start: /[a-z]+/", "openai_regex": "[a-z]+"},
        },
    ):
        source = deepcopy(messages[0])
        source["tools_added"][0]["constrained_sampling"] = sampling
        parsed = roundtrip(source)
        config = parsed.tools_added[0].constrained_sampling
        if isinstance(config, BaseModel):
            check_fields(config)
    assert checked == required.keys()

    for original in messages[2:]:
        source = deepcopy(original)
        del source["usage"]["cost"]
        restored = roundtrip(source)
        assert "cost" not in json.loads(serialize_message(restored))["usage"]
        assert_rejected({**source["usage"], "cost": None}, "Usage")
        assert_rejected({**source["usage"], "cost": {}}, "Usage")

    minimal_assistant = {key: messages[2][key] for key in required["AssistantMessage"]}
    for reason in (
        "pending",
        "stop",
        "length",
        "toolUse",
        "error",
        "aborted",
    ):
        source = {**minimal_assistant, "content": [], "stop_reason": reason}
        if reason == "error":
            source["error_message"] = "协议失败说明"
        assistant = roundtrip(source)
        serialized = json.loads(serialize_message(assistant))
        assert serialized["content"] == [] and serialized["stop_reason"] == reason
        assert "response_id" not in serialized and "response_model" not in serialized
        assert isinstance(assistant, AssistantMessage)
    assert_rejected({**minimal_assistant, "stop_reason": "deferred"})
    assert_rejected({**minimal_assistant, "stop_reason": "error"})
    for reason in ("pending", "aborted"):
        roundtrip({**minimal_assistant, "usage": None, "stop_reason": reason})
    for reason in ("stop", "length", "toolUse", "error"):
        assert_rejected({
            **minimal_assistant, "usage": None, "stop_reason": reason,
            "error_message": "协议失败说明",
        })
    for level in ("off", "minimal", "low", "medium", "high", "xhigh", "max"):
        assistant = roundtrip({**minimal_assistant, "thinking_level": level})
        assert assistant.thinking_level == level
    for source in messages[:2]:
        roundtrip({**source, "content": "纯文本"})

    blocks = [
        messages[1]["content"][0],
        messages[1]["content"][1],
        messages[2]["content"][1],
        messages[2]["content"][0],
    ]
    for source, allowed in zip(
        messages,
        [
            {"text"},
            {"text", "image"},
            {"text", "thinking", "toolCall"},
            {"text", "image"},
        ],
        strict=True,
    ):
        for block in blocks:
            candidate = {**source, "content": [block]}
            if block["type"] in allowed:
                roundtrip(candidate)
            else:
                assert_rejected(candidate)
        for content in (None, 1, [{"type": "unknown"}], [{"text": "missing type"}]):
            assert_rejected({**source, "content": content})
        assert_rejected({**source, "timestamp": True})
        assert_rejected({**source, "timestamp": "1"})
        assert_rejected({**source, "role": "custom"})
        assert_rejected({**source, "unknown": True})
    for source in messages[2:]:
        assert_rejected({**source, "content": "text"})
    assert_rejected({**messages[3], "is_error": 0})
    assert_rejected({**messages[2], "stop_reason": "unknown"})
    assert_rejected({**messages[2], "thinking_level": "unknown"})
    assert_rejected({**messages[0], "role": "user"})
    for value in (None, [], "json", 1, True):
        assert_rejected({**messages[2]["content"][0], "arguments": value}, "ToolCall")
    for value in (
        None,
        True,
        0,
        1,
        "false",
        {"type": "unknown"},
        {"type": "json_schema", "strict": "unknown"},
        {"type": "grammar", "variants": {"unknown": "x"}},
    ):
        assert_rejected(
            {**messages[0]["tools_added"][0], "constrained_sampling": value}, "Tool"
        )

    for value in (
        None,
        True,
        1,
        2.5,
        "值",
        [],
        {},
        {"nested": [None, False, {"a": [1]}]},
    ):
        roundtrip({**messages[3], "details": value})
        source = deepcopy(messages[2])
        source["deferred"]["data"] = value
        roundtrip(source)
    for value in (float("nan"), float("inf"), float("-inf")):
        assert_rejected({**messages[3], "details": {"nested": [value]}})
        source = deepcopy(messages[2])
        source["content"][0]["arguments"] = {"nested": [value]}
        assert_rejected(source)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json, sys; from pydantic import TypeAdapter; "
                "from src.ai.messages import Message; "
                "TypeAdapter(Message).validate_python(json.loads(sys.argv[1]))",
                json.dumps(source),
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
        )
        assert result.returncode != 0 and b"ValidationError" in result.stderr

    for arguments in (
        "{'bad': (1, 2)}",
        "{1: 'non-string key'}",
        "{'bad': {1, 2}}",
        "{'bad': object()}",
    ):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from src.ai.messages import ToolCall; "
                "ToolCall.model_validate({'type':'toolCall','id':'x','name':'x',"
                f"'arguments':{arguments}}})",
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
        )
        assert result.returncode != 0 and b"ValidationError" in result.stderr

    assert ToolCall.model_validate(messages[2]["content"][0]).arguments["nested"] == {
        "values": [1, None]
    }
    print("四种消息、全部依赖字段、判别限制、省略/null、严格 JSON 往返自检通过")


if __name__ == "__main__":
    check()
