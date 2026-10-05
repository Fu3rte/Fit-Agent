import asyncio
import json
from concurrent.futures import CancelledError, ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

from pydantic import TypeAdapter, ValidationError

from app.agent.message_context import convert_to_llm, prepare_message_context
from app.agent.tools.files import WORKSPACE, create_file_tools
from app.ai.context import (
    get_current_system_message,
    get_current_system_prompt,
    get_current_tools,
    normalize_context,
    validate_tool_pairs,
)
from app.ai.messages import Message, serialize_message

_adapter = TypeAdapter(Message)


def snapshot(messages: list[Message]) -> list[str]:
    return [serialize_message(message) for message in messages]


def assert_rejected(source: list[dict], error: str) -> None:
    with ThreadPoolExecutor(max_workers=1) as executor:
        failure = executor.submit(
            asyncio.run, prepare_message_context(source)
        ).exception()
    assert failure is not None and error in f"{type(failure).__name__}: {failure}"


async def check_projection(messages: list[Message]) -> None:
    before = snapshot(messages)
    identities = [id(message) for message in messages]
    signal = Event()
    order = []
    captured = []

    async def transform(items, cancel):
        assert cancel is signal and items is not messages
        assert all(
            left is not right for left, right in zip(items, messages, strict=True)
        )
        order.append("transform")
        captured.append(items)
        items[0].sections["a"] = "request-only"
        items[0].tools_added[0].parameters["x-request"] = {"nested": [None, True]}
        items[4].details["nested"][1]["value"] = "request-only"
        items[1].content = "request-only question"
        return items

    async def convert(items):
        assert order == ["transform"]
        assert items[4].details["nested"][1]["value"] == "request-only"
        order.append("convert")
        await asyncio.sleep(0)
        converted = convert_to_llm(items)
        captured.append(converted)
        return converted

    projected = await prepare_message_context(
        messages, transform_context=transform, convert_to_llm=convert, signal=signal
    )
    assert order == ["transform", "convert"]
    assert snapshot(messages) == before
    assert [id(message) for message in messages] == identities
    assert projected[1].content == "request-only question"
    assert projected[4].details["nested"][1]["value"] == "request-only"
    for index in (2, 5):
        assert serialize_message(projected[index]) == before[index]
        assert projected[index].usage is not messages[index].usage
        assert projected[index].usage.cost is not messages[index].usage.cost
        assert projected[index].content[0] is not messages[index].content[0]
    assert projected[2].content[2].arguments is not messages[2].content[2].arguments
    assert (
        projected[0].tools_added[0].parameters
        is not messages[0].tools_added[0].parameters
    )
    saved = snapshot(projected)
    for items in captured:
        items[4].details["nested"].clear()
        items[0].tools_added.clear()
    assert snapshot(projected) == saved and snapshot(messages) == before
    projected[0].sections.clear()
    projected[2].usage.cost.input = 999
    projected[2].content[2].arguments["path"] = "projection-only"
    assert snapshot(messages) == before

    calls = []

    def sync_convert(items):
        calls.append("convert")
        return convert_to_llm(items)

    untransformed = await prepare_message_context(messages, convert_to_llm=sync_convert)
    assert calls == ["convert"] and snapshot(untransformed) == before
    assert snapshot(await prepare_message_context(messages)) == before
    assert snapshot(normalize_context(messages)) == before
    assert snapshot(convert_to_llm(messages)) == before
    assert snapshot(messages) == before

    sources = [message.model_dump(exclude_unset=True) for message in messages]
    for source in sources:
        if "usage" in source:
            del source["usage"]["cost"]
    no_cost = [_adapter.validate_python(source) for source in sources]
    assert snapshot(await prepare_message_context(no_cost)) == snapshot(no_cost)
    assert snapshot(messages) == before

    async def crop(items, cancel):
        return [message for message in items if message.role != "toolResult"]

    with ThreadPoolExecutor(max_workers=1) as executor:
        failure = executor.submit(
            asyncio.run, prepare_message_context(messages, transform_context=crop)
        ).exception()
    assert isinstance(failure, ValueError) and "配对" in str(failure)
    assert snapshot(messages) == before


def check_failures(messages: list[Message]) -> None:
    before = snapshot(messages)
    entered = []
    signal = Event()
    signal.set()

    async def transform(items, cancel):
        entered.append("transform")
        cancel.set()
        return items

    def convert(items):
        entered.append("convert")
        return convert_to_llm(items)

    with ThreadPoolExecutor(max_workers=1) as executor:
        failure = executor.submit(
            asyncio.run,
            prepare_message_context(
                messages,
                transform_context=transform,
                convert_to_llm=convert,
                signal=signal,
            ),
        ).exception()
        assert isinstance(failure, CancelledError) and entered == []
        signal.clear()
        failure = executor.submit(
            asyncio.run,
            prepare_message_context(
                messages,
                transform_context=transform,
                convert_to_llm=convert,
                signal=signal,
            ),
        ).exception()
        assert isinstance(failure, CancelledError) and entered == ["transform"]
        signal.clear()

        async def failing_transform(items, cancel):
            items[4].details["nested"].clear()
            raise RuntimeError("hook failure")

        failure = executor.submit(
            asyncio.run,
            prepare_message_context(messages, transform_context=failing_transform),
        ).exception()
        assert isinstance(failure, RuntimeError) and str(failure) == "hook failure"

        async def invalid_transform(items, cancel):
            items[1].role = "custom"
            return items

        failure = executor.submit(
            asyncio.run,
            prepare_message_context(messages, transform_context=invalid_transform),
        ).exception()
        assert isinstance(failure, ValidationError)

        def invalid_convert(items):
            items[2].usage.input = True
            return items

        failure = executor.submit(
            asyncio.run,
            prepare_message_context(messages, convert_to_llm=invalid_convert),
        ).exception()
        assert isinstance(failure, ValidationError)

        def unpaired_convert(items):
            items[4].tool_name = "write"
            return convert_to_llm(items)

        failure = executor.submit(
            asyncio.run,
            prepare_message_context(messages, convert_to_llm=unpaired_convert),
        ).exception()
        assert isinstance(failure, ValueError) and "名称不匹配" in str(failure)

        async def cancel_convert(items):
            signal.set()
            return convert_to_llm(items)

        failure = executor.submit(
            asyncio.run,
            prepare_message_context(
                messages, convert_to_llm=cancel_convert, signal=signal
            ),
        ).exception()
        assert isinstance(failure, CancelledError)
    assert snapshot(messages) == before


def check() -> None:
    registry = create_file_tools()
    declarations = {
        name: tool.definition().model_dump(exclude_unset=True)
        for name, tool in registry.items()
    }
    usage = {
        "input": 2,
        "output": 3,
        "cache_read": 4,
        "cache_write": 5,
        "cache_write_1h": 1,
        "reasoning": 2,
        "total_tokens": 14,
        "cost": {
            "input": 0.1,
            "output": 0.2,
            "cache_read": 0.3,
            "cache_write": 0.4,
            "total": 1,
        },
    }
    with TemporaryDirectory(dir=WORKSPACE) as directory:
        path = Path(directory) / "context.txt"
        path.write_text("context-check", encoding="utf-8")
        arguments = {"path": path.relative_to(WORKSPACE).as_posix()}
        result = registry["read"].invoke(json.dumps(arguments))
        assert result.isError is False
        sources = [
            {
                "role": "system",
                "content": "base",
                "sections": {"a": "A", "gone": "G", "empty": ""},
                "tools_added": [declarations["read"], declarations["write"]],
                "timestamp": 0,
            },
            {"role": "user", "content": "question", "timestamp": 1},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "reading",
                        "text_signature": "text-signature",
                    },
                    {
                        "type": "thinking",
                        "thinking": "",
                        "thinking_signature": "encrypted",
                        "redacted": True,
                    },
                    {
                        "type": "toolCall",
                        "id": "call-1",
                        "name": "read",
                        "arguments": arguments,
                        "thought_signature": "thought-signature",
                    },
                ],
                "api": "openai-completions",
                "provider": "custom-provider",
                "model": "model",
                "usage": usage,
                "stop_reason": "toolUse",
                "response_id": "response-id",
                "diagnostics": [
                    {"type": "safe", "timestamp": 2, "details": {"a": [None]}}
                ],
                "deferred": {
                    "provider": "custom-provider",
                    "model_id": "model",
                    "api": "custom-api",
                    "id": "task-id",
                    "data": None,
                },
                "timestamp": 2,
            },
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": "later-1",
                        "text_signature": "system-signature",
                    },
                    {"type": "text", "text": "later-2"},
                ],
                "sections": {"a": "updated-A", "gone": None, "b": "B"},
                "tools_removed": [
                    {"name": "write"},
                    {"name": "read"},
                    {"name": "absent"},
                ],
                "tools_added": [
                    {
                        **declarations["read"],
                        "description": "updated read",
                        "parameters": {
                            **declarations["read"]["parameters"],
                            "x-provider": {"nested": [None, True]},
                        },
                    },
                    declarations["ls"],
                ],
                "timestamp": 3,
            },
            {
                "role": "toolResult",
                "tool_call_id": "call-1",
                "tool_name": "read",
                "content": [{"type": "text", "text": result.content}],
                "details": {"nested": [None, {"value": "original"}]},
                "usage": usage,
                "is_error": False,
                "timestamp": 4,
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "done",
                        "text_signature": '{"v":1,"id":"response","phase":"final_answer"}',
                    }
                ],
                "api": "openai-completions",
                "provider": "custom-provider",
                "model": "model",
                "usage": usage,
                "stop_reason": "stop",
                "timestamp": 5,
            },
            {
                "role": "system",
                "content": [],
                "sections": {"empty": None},
                "tools_removed": [{"name": "ls"}],
                "tools_added": [declarations["write"]],
                "timestamp": 6,
            },
        ]
        messages = [_adapter.validate_python(source) for source in sources]
        before = snapshot(messages)
        asyncio.run(check_projection(messages))
        check_failures(messages)
        state = get_current_system_message(messages)
        assert state.timestamp == 0 and state.content == "base\n\nlater-1\nlater-2"
        assert state.sections == {"a": "updated-A", "b": "B"}
        assert [tool.name for tool in state.tools_added] == ["read", "write"]
        assert state.tools_added[0].description == "updated read"
        assert state.tools_added[0].parameters["x-provider"] == {"nested": [None, True]}
        assert (
            get_current_system_prompt(messages)
            == "base\n\nlater-1\nlater-2\n\nupdated-A\n\nB"
        )
        assert get_current_tools(messages) == state.tools_added
        state.tools_added[0].parameters.clear()
        state.sections.clear()
        assert snapshot(messages) == before
        assert get_current_system_message([]) is None
        assert get_current_system_prompt([messages[1]]) == ""
        assert get_current_tools([messages[1]]) == []
        empty = _adapter.validate_json('{"role":"system","content":"","timestamp":7}')
        assert get_current_system_message([empty]) == empty
        validate_tool_pairs(messages)

        for candidate, error in [
            ([sources[4]], "缺少待配对调用"),
            ([sources[4], sources[2]], "缺少待配对调用"),
            ([sources[2]], "缺少结果"),
            ([sources[2], sources[4], sources[4]], "缺少待配对调用"),
            ([sources[2], sources[4], sources[2], sources[4]], "重复工具调用 ID"),
            ([sources[2], {**sources[4], "tool_name": "write"}], "名称不匹配"),
            ([sources[2], {**sources[4], "tool_call_id": "unknown"}], "缺少待配对调用"),
            ([sources[2], sources[1], sources[4]], "未完整配对"),
            ([sources[2], sources[5], sources[4]], "未完整配对"),
            ([{**sources[1], "role": "custom"}], "ValidationError"),
            ([{**sources[1], "timestamp": None}], "ValidationError"),
        ]:
            assert_rejected(candidate, error)
        duplicate = deepcopy(sources[2])
        duplicate["content"].append(deepcopy(duplicate["content"][2]))
        assert_rejected([duplicate, sources[4]], "重复工具调用 ID")
        parallel = deepcopy(sources[2])
        parallel["content"].append({**parallel["content"][2], "id": "call-2"})
        parallel_sources = [
            parallel,
            {**sources[4], "tool_call_id": "call-2"},
            sources[4],
        ]
        parallel_messages = [
            _adapter.validate_python(source) for source in parallel_sources
        ]
        projected = asyncio.run(prepare_message_context(parallel_messages))
        assert snapshot(projected) == snapshot(parallel_messages)
        assert_rejected([parallel, sources[4]], "缺少结果")
    print("上下文钩子顺序、取消、系统重放、工具变化、原值保护与配对检查通过")


if __name__ == "__main__":
    check()
