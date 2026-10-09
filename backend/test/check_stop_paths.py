import asyncio
import json
from concurrent.futures import CancelledError
from contextlib import aclosing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import get_args

import pytest
from openai import APIConnectionError
from pydantic import ValidationError

from app.agent.agent_loop import run_agent_loop
from app.agent.config import AgentLoopConfig
from app.agent.tools.files import create_file_tools
from app.agent.usage import summarize_usage
from app.ai.api.openai_completions import STOP_REASONS
from app.ai.context import normalize_context
from app.ai.messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    ToolCall,
    ToolReference,
    UserMessage,
)
from app.ai.stream import complete, stream
from app.ai.types import DoneReason, ModelSpec
from app.model_config import load_model_config
from test.regression_support import TEST_SESSION, session_workspace


def checked_run(coro):
    errors = []
    with asyncio.Runner() as runner:
        runner.get_loop().set_exception_handler(lambda _loop, context: errors.append(context))
        result = runner.run(coro)
    assert not errors, errors
    return result


def user(text):
    return UserMessage(role="user", content=text, timestamp=0)


async def check_stream(model, options):
    context = {"messages": [user("只回复 OK。") ]}
    response = stream(model, context, options)
    events = []
    async with aclosing(response):
        async for event in response:
            events.append(event)
    result = await response.result()
    assert result == events[-1]["message"]
    assert events[-1]["reason"] == result.stop_reason == "stop"
    assert sum(event["type"] == "start" for event in events) == 1
    assert sum(event["type"] == "done" for event in events) == 1
    assert result.usage is not None and result.usage.total_tokens > 0
    completed = await complete(model, context, options)
    assert completed.stop_reason == result.stop_reason
    assert "".join(block.text for block in completed.content if isinstance(block, TextContent)).strip() == "OK"
    assert "".join(block.text for block in result.content if isinstance(block, TextContent)).strip() == "OK"
    assert completed.usage is not None
    assert set(get_args(DoneReason)) == {"stop", "toolUse", "length", "error", "aborted"}
    assert STOP_REASONS == {
        "stop": "stop", "tool_calls": "toolUse", "length": "length", "content_filter": "error",
    }
    for reason in ("unknown", "deferred"):
        with pytest.raises(ValidationError):
            AssistantMessage.model_validate({**result.model_dump(), "stop_reason": reason})
    with pytest.raises(ValidationError, match="error_message"):
        AssistantMessage.model_validate({**result.model_dump(exclude_unset=True), "stop_reason": "error"})
    limited = await complete(model, {"messages": [user("逐行列出从 1 到 10000 的整数。") ]}, {**options, "max_tokens": 1})
    assert limited.stop_reason == limited.raw_stop_reason == "length"
    assert limited.usage is not None
    assert not any(isinstance(block, ToolCall) for block in limited.content)
    for _ in range(2):
        response = stream(model, context, options)
        async with aclosing(response):
            async for _event in response:
                break
        with pytest.raises(RuntimeError, match="尚未完整结束"):
            await response.result()
    cancel = Event()
    cancel.set()
    response = stream(model, context, {**options, "signal": cancel})
    with pytest.raises(CancelledError):
        async with aclosing(response):
            async for _event in response:
                pytest.fail("请求前取消产生了事件")
    broken = ModelSpec(api=model.api, provider=model.provider, id=model.id, base_url="http://127.0.0.1:1/v1")
    with pytest.raises(APIConnectionError):
        await complete(broken, context, options)


async def check_steering(config, directory):
    polls = 0
    events = []

    async def emit(event):
        events.append(event)

    async def steer():
        nonlocal polls
        polls += 1
        return [user("只回复 SECOND。") ] if polls == 2 else []

    loop_config = AgentLoopConfig(model=config, max_turns=3, get_steering_messages=steer)
    messages = await run_agent_loop([user("只回复 FIRST。")], {"messages": [], "tools": {}}, loop_config, emit)
    assert polls == 3
    assert sum(isinstance(message, AssistantMessage) for message in messages) == 2
    assert sum(isinstance(message, UserMessage) for message in messages) == 2
    assert events[-1]["status"] == "completed"
    normalize_context(messages)

    polls = 0
    events.clear()
    workspace, prefix = session_workspace(directory)
    tools = {"write": create_file_tools(TEST_SESSION, tmp_root=directory)["write"]}
    path = f"{prefix}/truncated.txt"
    system = SystemMessage(role="system", content="必须调用 write 写入用户提供的完整文本。", tools_added=[tools["write"].definition()], timestamp=0)
    def limited_first_stream(model, context, options):
        assert options["max_tokens"] == 16384
        return stream(model, context, options)

    async def steer_length():
        nonlocal polls
        polls += 1
        if polls == 2:
            return [
                SystemMessage(role="system", content="", tools_removed=[ToolReference(name="write")], timestamp=0),
                user("文件写入请求已经结束。只回复 OK。"),
            ]
        return []

    loop_config.get_steering_messages = steer_length
    messages = await run_agent_loop(
        [user(f"直接调用 write，在 {path} 写入从 1 到 100000 的全部整数，每行一个。必须逐行填满 content 参数，禁止省略。")],
        {"messages": [system], "tools": tools}, loop_config, emit,
        stream_fn=limited_first_stream,
    )
    assistants = [message for message in messages if isinstance(message, AssistantMessage)]
    assert len(assistants) == 2 and polls == 3
    assert assistants[0].stop_reason == "length" and assistants[-1].stop_reason == "stop"
    assert assistants[0].raw_stop_reason == "length"
    assert assistants[0].usage is not None and assistants[0].usage.output > 0
    assert not any(isinstance(block, ToolCall) for block in assistants[0].content)
    first_updates = []
    for event in events:
        if event["type"] == "message_end":
            assert event["message"] == assistants[0]
            break
        if event["type"] == "message_update":
            first_updates.append(event["assistant_event"])
    fragments = [event for event in first_updates if event["type"] == "toolcall_delta"]
    assert fragments, {
        "events": {event["type"] for event in first_updates},
        "text": "".join(block.text for block in assistants[0].content if isinstance(block, TextContent))[:400],
    }
    assert len({event["content_index"] for event in fragments}) == 1
    raw_arguments = "".join(event["delta"] for event in fragments)
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw_arguments)
    assert not any(event["type"] == "toolcall_end" for event in first_updates)
    assert not any(event["type"] in {"tool_start", "tool_result"} for event in events)
    assert not (workspace / "truncated.txt").exists()
    normalize_context([system, *messages])

    polls = 0
    events.clear()
    path = f"{prefix}/real.txt"
    loop_config.get_steering_messages = steer
    messages = await run_agent_loop(
        [user(f"调用 write，在 {path} 写入精确文本 real，然后报告结果。")],
        {"messages": [system], "tools": tools}, loop_config, emit,
    )
    assert (workspace / "real.txt").read_text(encoding="utf-8") == "real"
    assert polls == 3
    assert any(event["type"] == "tool_result" for event in events)
    normalize_context([system, *messages])


async def check_cancel(config, model, options):
    for boundary in ("generation", "message_end", "tool_start", "tool_result"):
        cancel = Event()
        events = []
        trace = []
        polls = 0
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace, prefix = session_workspace(root)
            tools = (
                {"write": create_file_tools(TEST_SESSION, tmp_root=root)["write"]}
                if boundary in {"tool_start", "tool_result"}
                else {}
            )
            system = SystemMessage(role="system", content="按用户要求执行。", tools_added=[tool.definition() for tool in tools.values()], timestamp=0)

            async def steer():
                nonlocal polls
                polls += 1
                return []

            async def save_message(node_id, message):
                trace.append(("save", node_id))

            async def emit(event):
                events.append(event)
                key = event.get("message_id") or event.get("tool_call_id")
                trace.append(("emit", event["type"], key))
                if boundary == "generation" and event["type"] == "message_update":
                    cancel.set()
                elif boundary != "generation" and event["type"] == boundary:
                    cancel.set()

            config_loop = AgentLoopConfig(model=config, max_turns=3, get_steering_messages=steer, save_message=save_message)
            prompt = (
                f"调用 write 在 {prefix}/cancelled.txt 写入 cancelled。" if tools
                else "只回复 OK。" if boundary == "message_end"
                else "逐行输出从 1 到 10000 的整数。"
            )
            with pytest.raises(CancelledError):
                await run_agent_loop([user(prompt)], {"messages": [system], "tools": tools}, config_loop, emit, cancel)
            ends = [event["message"] for event in events if event["type"] == "message_end"]
            assert len(ends) == 1
            assert ends[0].stop_reason == {"generation": "aborted", "message_end": "stop", "tool_start": "toolUse", "tool_result": "toolUse"}[boundary]
            assert events[-1]["type"] == "trace_end" and events[-1]["status"] == "cancelled"
            assert [event for event in events if event["type"] == "turn_end"][-1]["status"] == "cancelled"
            assert polls == 1
            saved_at = {
                entry[1]: index for index, entry in enumerate(trace) if entry[0] == "save"
            }
            for index, entry in enumerate(trace):
                if entry[0] == "emit" and entry[1] in {"message_end", "tool_result"}:
                    assert entry[2] in saved_at and saved_at[entry[2]] < index, (boundary, entry)
            results = [event for event in events if event["type"] == "tool_result"]
            if boundary == "tool_result":
                assert results and (workspace / "cancelled.txt").read_text(encoding="utf-8") == "cancelled"
            else:
                assert not results and not (workspace / "cancelled.txt").exists()
            if boundary == "generation":
                assert not any(isinstance(block, ToolCall) for block in ends[0].content)
                if ends[0].usage is None:
                    with pytest.raises(ValueError, match="完整助手消息"):
                        summarize_usage(ends)

    async def external_cancel():
        response = stream(model, {"messages": [user("逐行输出从 1 到 10000 的整数。") ]}, options)
        async with aclosing(response):
            async for event in response:
                if event["type"].endswith("_delta"):
                    asyncio.current_task().cancel()
                    await asyncio.sleep(0)
        pytest.fail("外部取消未传播")

    with pytest.raises(asyncio.CancelledError):
        await asyncio.create_task(external_cancel())


def check():
    config = load_model_config()
    assert config.MODEL_API == "openai-completions"
    model = ModelSpec(api=config.MODEL_API, provider=config.OPENAI_PROVIDER, id=config.OPENAI_MODEL, base_url=config.OPENAI_BASE_URL)
    options = {"api_key": config.OPENAI_API_KEY, "max_tokens": 16384}
    checked_run(check_stream(model, options))
    with TemporaryDirectory() as directory:
        checked_run(check_steering(config, Path(directory)))
    checked_run(check_cancel(config, model, options))
    print("真实 OpenAI stop、toolUse、length、aborted、steering、取消和资源收尾检查通过")


if __name__ == "__main__":
    check()
