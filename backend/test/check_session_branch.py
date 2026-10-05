from copy import deepcopy

from pydantic import TypeAdapter, ValidationError

from app.ai.context import validate_tool_pairs
from app.ai.messages import (
    AssistantMessage,
    Message,
    SystemMessage,
    ToolResultMessage,
    UserMessage,
    serialize_message,
)
from app.domain.session.branch import build_session_context, build_session_path
from app.domain.session.models import SessionMessageEntry

_adapter = TypeAdapter(Message)

USAGE = {
    "input": 1,
    "output": 2,
    "cache_read": 3,
    "cache_write": 4,
    "cache_write_1h": 5,
    "reasoning": 1,
    "total_tokens": 10,
    "cost": {
        "input": 0.1,
        "output": 0.2,
        "cache_read": 0.3,
        "cache_write": 0.4,
        "total": 1,
    },
}

SYSTEM = {
    "role": "system",
    "content": [
        {"type": "text", "text": "instructions", "text_signature": "sig"}
    ],
    "sections": {"active": "指令片段", "removed": None},
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
    "tools_removed": [{"name": "old-tool"}],
    "timestamp": 1.5,
}

USER = {
    "role": "user",
    "content": [
        {"type": "text", "text": "问题"},
        {"type": "image", "data": "base64", "mime_type": "image/png"},
    ],
    "timestamp": 2,
}

ASSISTANT = {
    "role": "assistant",
    "content": [
        {
            "type": "thinking",
            "thinking": "plan",
            "thinking_signature": "enc",
            "redacted": True,
        },
        {
            "type": "toolCall",
            "id": "call-1",
            "name": "lookup",
            "arguments": {"query": "a", "nested": {"values": [1, None]}},
            "thought_signature": "thought-signature",
            "namespace": "tools",
        },
        {
            "type": "text",
            "text": "done",
            "text_signature": '{"v":1,"id":"response-1"}',
        },
    ],
    "api": "openai-completions",
    "provider": "example",
    "model": "model-1",
    "response_model": "actual-model",
    "response_id": "response-1",
    "usage": USAGE,
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
        "data": {"cursor": [1, {"next": None}]},
    },
    "timestamp": 3,
}

ASSISTANT_FINAL = {
    "role": "assistant",
    "content": [{"type": "text", "text": "done", "text_signature": '{"v":1}'}],
    "api": "openai-completions",
    "provider": "example",
    "model": "model-1",
    "usage": USAGE,
    "stop_reason": "stop",
    "timestamp": 5,
}

ASSISTANT_ABORTED = {
    "role": "assistant",
    "content": [],
    "api": "openai-completions",
    "provider": "example",
    "model": "model-1",
    "usage": None,
    "stop_reason": "aborted",
    "timestamp": 6,
}

TOOL_RESULT = {
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
        "calls": [{"id": "nested-1", "name": "fetch", "status": "ok"}],
        "complete": True,
    },
}


def parse(source: dict) -> Message:
    return _adapter.validate_python(source)


def node_source(message: Message, **overrides: object) -> dict:
    source = {
        "session_id": "s1",
        "id": "n1",
        "parent_id": None,
        "run_id": None,
        "type": "message",
        "messages": [message.model_dump(exclude_unset=True)],
        "created_at": 0,
    }
    source.update(overrides)
    return source


def make_entry(message: Message, **overrides: object) -> SessionMessageEntry:
    return SessionMessageEntry.model_validate(node_source(message, **overrides))


def ids(path: list[SessionMessageEntry]) -> list[str]:
    return [entry.id for entry in path]


def assert_rejected(source: dict, label: str) -> None:
    try:
        SessionMessageEntry.model_validate(source)
    except ValidationError:
        return
    raise AssertionError(f"{label}: 预期节点模型拒绝")


def assert_value_error(call, label: str) -> None:
    try:
        call()
    except ValueError as error:
        assert label in str(error), (label, str(error))
        assert "问题" not in str(error), (label, str(error))
        return
    raise AssertionError(f"{label}: 预期 ValueError")


def check_model() -> None:
    messages = [parse(SYSTEM), parse(USER), parse(ASSISTANT), parse(TOOL_RESULT)]
    classes = [SystemMessage, UserMessage, AssistantMessage, ToolResultMessage]
    for index, (message, expected) in enumerate(zip(messages, classes, strict=True)):
        entry = make_entry(message, id=f"n{index}", created_at=index)
        assert type(entry.messages[0]) is expected
        assert entry.messages[0].model_dump(exclude_unset=True) == message.model_dump(
            exclude_unset=True
        )
        assert serialize_message(entry.messages[0]) == serialize_message(message)
        assert entry.session_id == "s1" and entry.id == f"n{index}"

    valid = node_source(parse(USER))
    rejections = [
        ("未知字段", {"unknown": 1}),
        ("id 类型", {"id": 1}),
        ("session_id 类型", {"session_id": 1}),
        ("parent_id 类型", {"parent_id": 1}),
        ("run_id 类型", {"run_id": 1}),
        ("未知节点类型", {"type": "custom"}),
        ("created_at 字符串", {"created_at": "0"}),
        ("created_at 布尔", {"created_at": True}),
        ("空消息数组", {"messages": []}),
        (
            "多元素消息数组",
            {"messages": [parse(USER).model_dump(exclude_unset=True)] * 2},
        ),
        (
            "未定义消息角色",
            {"messages": [{"role": "custom", "content": "x", "timestamp": 1}]},
        ),
        (
            "非法内容块",
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "unknown"}],
                        "timestamp": 1,
                    }
                ]
            },
        ),
        (
            "消息额外字段",
            {
                "messages": [
                    {"role": "user", "content": "x", "timestamp": 1, "unknown": 2}
                ]
            },
        ),
        ("messages 非数组", {"messages": {}}),
    ]
    for label, overrides in rejections:
        assert_rejected({**valid, **overrides}, label)

    pending_source = {**deepcopy(ASSISTANT), "stop_reason": "pending", "usage": None}
    pending = parse(pending_source)
    assert isinstance(pending, AssistantMessage)
    assert pending.stop_reason == "pending" and pending.usage is None
    assert_rejected(node_source(pending), "pending 助手消息")

    aborted = make_entry(parse(ASSISTANT_ABORTED), id="aborted")
    assert aborted.messages[0].stop_reason == "aborted"
    assert aborted.messages[0].usage is None
    for reason in ("stop", "length", "toolUse", "error"):
        source = {**ASSISTANT_FINAL, "stop_reason": reason}
        if reason == "error":
            source["error_message"] = "协议失败说明"
        entry = make_entry(parse(source), id=reason)
        assert entry.messages[0].stop_reason == reason


def build_tree() -> list[SessionMessageEntry]:
    alt = {
        **deepcopy(ASSISTANT_FINAL),
        "content": [{"type": "text", "text": "另一个回答"}],
        "timestamp": 7,
    }
    edited = {**deepcopy(USER), "content": "修改后的问题"}
    system2 = {**deepcopy(SYSTEM), "content": "第二个系统", "timestamp": 8}
    user2 = {**deepcopy(USER), "content": "第二个问题", "timestamp": 9}
    return [
        make_entry(parse(SYSTEM), id="sys", created_at=10),
        make_entry(parse(USER), id="u1", parent_id="sys", created_at=11),
        make_entry(parse(ASSISTANT), id="a1", parent_id="u1", run_id="r1", created_at=12),
        make_entry(
            parse(TOOL_RESULT), id="t1", parent_id="a1", run_id="r1", created_at=13
        ),
        make_entry(
            parse(ASSISTANT_FINAL), id="a2", parent_id="t1", run_id="r1", created_at=14
        ),
        make_entry(parse(alt), id="a1b", parent_id="u1", run_id="r2", created_at=15),
        make_entry(parse(edited), id="u1e", parent_id="sys", created_at=16),
        make_entry(parse(system2), id="sys2", created_at=17),
        make_entry(parse(user2), id="u2", parent_id="sys2", created_at=18),
    ]


def check_path() -> None:
    entries = build_tree()
    by_id = {entry.id: entry for entry in entries}

    assert build_session_path("s1", [], None) == []
    assert build_session_context("s1", [], None) == []
    assert build_session_path("s1", entries, None) == []
    assert build_session_context("s1", entries, None) == []

    cases = {
        "a2": ["sys", "u1", "a1", "t1", "a2"],
        "a1b": ["sys", "u1", "a1b"],
        "u1e": ["sys", "u1e"],
        "u2": ["sys2", "u2"],
        "sys2": ["sys2"],
    }
    reversed_entries = list(reversed(entries))
    for leaf, expected in cases.items():
        assert ids(build_session_path("s1", entries, leaf)) == expected
        assert ids(build_session_path("s1", reversed_entries, leaf)) == expected

    detached = make_entry(parse(USER), id="detached", parent_id="missing")
    assert ids(build_session_path("s1", [*entries, detached], "a2")) == cases["a2"]

    path = build_session_path("s1", entries, "a2")
    context = build_session_context("s1", entries, "a2")
    assert [serialize_message(message) for message in context] == [
        serialize_message(by_id[key].messages[0])
        for key in ("sys", "u1", "a1", "t1", "a2")
    ]
    for entry, message in zip(path, context, strict=True):
        assert message is entry.messages[0]

    assert_value_error(
        lambda: build_session_path("s1", [*entries, by_id["sys"]], "a2"),
        "重复节点 ID",
    )
    assert_value_error(lambda: build_session_path("s1", entries, "nope"), "未知 leaf")
    assert_value_error(lambda: build_session_path("s2", entries, "a2"), "不属于会话")

    orphan = make_entry(parse(USER), id="orphan", parent_id="ghost")
    assert_value_error(
        lambda: build_session_path("s1", [orphan], "orphan"), "缺少父节点"
    )

    self_entry = make_entry(parse(USER), id="self", parent_id="self")
    assert_value_error(
        lambda: build_session_path("s1", [self_entry], "self"), "自引用"
    )

    x = make_entry(parse(USER), id="x", parent_id="y")
    y = make_entry(parse(ASSISTANT_FINAL), id="y", parent_id="x")
    assert_value_error(lambda: build_session_path("s1", [x, y], "x"), "环")


def check_integrity() -> None:
    entries = build_tree()
    order = [entry.id for entry in entries]
    before = [entry.model_dump(exclude_unset=True) for entry in entries]
    messages_before = {
        entry.id: serialize_message(entry.messages[0]) for entry in entries
    }

    system = entries[0].messages[0]
    assert isinstance(system, SystemMessage)
    assert system.sections == {"active": "指令片段", "removed": None}
    added = system.tools_added[0]
    assert added.name == "lookup" and added.description == "lookup data"
    assert added.parameters["x-provider"] == {"nested": [None, True, 2.5]}
    assert added.parameters["properties"]["query"]["examples"] == ["a", {"b": 1}]
    assert added.constrained_sampling.variants.openai_regex == "[a-z]+"
    assert system.tools_removed[0].name == "old-tool"

    assistant = entries[2].messages[0]
    assert isinstance(assistant, AssistantMessage)
    thinking, tool_call, text = assistant.content
    assert thinking.type == "thinking" and thinking.thinking_signature == "enc"
    assert thinking.redacted is True
    assert tool_call.type == "toolCall" and tool_call.id == "call-1"
    assert tool_call.name == "lookup"
    assert tool_call.arguments == {"query": "a", "nested": {"values": [1, None]}}
    assert tool_call.thought_signature == "thought-signature"
    assert tool_call.namespace == "tools"
    assert text.text_signature == '{"v":1,"id":"response-1"}'
    assert assistant.response_id == "response-1"
    assert assistant.response_model == "actual-model"
    assert assistant.diagnostics[0].error.code == 7.5
    assert assistant.deferred.data == {"cursor": [1, {"next": None}]}
    assert assistant.usage.cost.total == 1 and assistant.timestamp == 3

    tool_result = entries[3].messages[0]
    assert isinstance(tool_result, ToolResultMessage)
    tool_dump = tool_result.model_dump(exclude_unset=True)
    assert "details" in tool_dump and tool_dump["details"] is None
    assert "usage" not in tool_dump
    assert tool_result.tool_call_id == "call-1" and tool_result.tool_name == "lookup"
    assert tool_result.nested_calls.complete is True

    minimal = {
        key: ASSISTANT_FINAL[key]
        for key in (
            "role",
            "content",
            "api",
            "provider",
            "model",
            "usage",
            "stop_reason",
            "timestamp",
        )
    }
    minimal_dump = make_entry(parse(minimal), id="minimal").messages[0].model_dump(
        exclude_unset=True
    )
    assert minimal_dump == minimal
    for absent in ("response_id", "response_model", "diagnostics", "deferred"):
        assert absent not in minimal_dump

    path = build_session_path("s1", entries, "a2")
    context = build_session_context("s1", entries, "a2")
    assert path is not entries and context is not entries
    assert [entry.id for entry in entries] == order
    assert [entry.model_dump(exclude_unset=True) for entry in entries] == before
    for entry in entries:
        assert serialize_message(entry.messages[0]) == messages_before[entry.id]
    assert [serialize_message(message) for message in context] == [
        messages_before[key] for key in ("sys", "u1", "a1", "t1", "a2")
    ]


def check_tool_pair_boundary() -> None:
    unpaired = [
        make_entry(parse(SYSTEM), id="sys"),
        make_entry(parse(USER), id="u1", parent_id="sys"),
        make_entry(parse(ASSISTANT), id="a1", parent_id="u1"),
    ]
    context = build_session_context("s1", unpaired, "a1")
    assert len(context) == 3
    try:
        validate_tool_pairs(context)
    except ValueError as error:
        assert "缺少结果" in str(error)
    else:
        raise AssertionError("未配对工具调用未被拒绝")

    paired = [*unpaired, make_entry(parse(TOOL_RESULT), id="t1", parent_id="a1")]
    validate_tool_pairs(build_session_context("s1", paired, "t1"))


def check() -> None:
    check_model()
    check_path()
    check_integrity()
    check_tool_pair_boundary()
    print("会话节点模型、分支重建、消息原值保护与工具配对边界自检通过")


if __name__ == "__main__":
    check()
