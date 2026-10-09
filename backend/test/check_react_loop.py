import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from uuid import uuid4

from app.agent.agent_loop import run_agent_loop, run_loop
from app.agent.config import AgentLoopConfig
from app.agent.tools.files import create_file_tools
from app.ai.api.openai_completions import to_openai_request
from app.ai.messages import (
    AssistantMessage,
    SystemMessage,
    ToolResultMessage,
    UserMessage,
)
from app.ai.stream import stream
from app.model_config import load_model_config
from test.regression_support import TEST_SESSION, session_workspace


def check():
    config = load_model_config()
    assert config.MODEL_API == "openai-completions"
    loop_config = AgentLoopConfig(model=config, max_turns=8)
    limits = []

    def checked_stream(model, context, options):
        assert options["max_tokens"] == 16384
        request = to_openai_request(context["messages"], model, options)
        assert request["max_tokens"] == options["max_tokens"]
        assert "reasoning_effort" not in request and "thinking" not in request
        limits.append(options["max_tokens"])
        return stream(model, context, options)

    with TemporaryDirectory() as directory:
        root = Path(directory)
        tools = create_file_tools(TEST_SESSION, tmp_root=root)
        workspace, prefix = session_workspace(root)
        path = f"{prefix}/react.txt"
        token = uuid4().hex
        events = []
        trace = []

        async def save_message(node_id, message):
            trace.append(("save", node_id))

        async def emit(event):
            events.append(event)
            key = event.get("message_id") or event.get("tool_call_id")
            trace.append(("emit", event["type"], key))

        loop_config.save_message = save_message

        system = SystemMessage(
            role="system",
            content="执行用户指定的工具操作，工具参数遵循声明。",
            tools_added=[tool.definition() for tool in tools.values()],
            timestamp=time_ns() // 1_000_000,
        )
        prompts = [
            UserMessage(
                role="user",
                content=f"只使用文件工具操作工作文件。必须调用 write 在 {path} 写入精确文本 "
                f"{token}，随后必须调用 read 读取该文件，最后回答文件内容。禁止添加空白或换行。",
                timestamp=time_ns() // 1_000_000,
            )
        ]
        context = {"messages": [system], "tools": tools}
        messages = asyncio.run(
            run_agent_loop(prompts, context, loop_config, emit, stream_fn=checked_stream)
        )
        assert len(limits) >= 2 and all(limit == 16384 for limit in limits)
        assert len(limits) == sum(
            isinstance(message, AssistantMessage) for message in messages
        )
        assert all(
            message.usage is not None and message.usage.output <= 16384
            for message in messages
            if isinstance(message, AssistantMessage)
        )
        assert context["messages"] == [system]
        assert messages[:1] == prompts and len(prompts) == 1
        assert (workspace / "react.txt").read_text(encoding="utf-8") == token
        results = [
            message for message in messages if isinstance(message, ToolResultMessage)
        ]
        assert results
        assert all(
            not result.is_error and result.content[0].text
            for result in results
            if result.tool_name in {"write", "read"}
        )
        assert (
            isinstance(messages[-1], AssistantMessage)
            and messages[-1].stop_reason == "stop"
        )
        starts = {
            event["tool_call_id"]
            for event in events
            if event["type"] == "tool_start"
        }
        recorded = {
            event["tool_call_id"]
            for event in events
            if event["type"] == "tool_result"
        }
        assert starts == recorded
        assert {
            event["name"] for event in events if event["type"] == "tool_start"
        } >= {"write", "read"}
        assert events[0]["type"] == "trace_start"
        assert events[-1]["type"] == "trace_end" and events[-1]["status"] == "completed"
        started, ended = {}, set()
        for event in events:
            if event["type"] == "message_start":
                assert event["message_id"] not in started
                started[event["message_id"]] = True
            elif event["type"] == "message_update":
                assert event["message_id"] in started
            elif event["type"] == "message_end":
                assert event["message_id"] in started and event["message_id"] not in ended
                ended.add(event["message_id"])
        assert set(started) == ended
        saved_at = {
            entry[1]: index for index, entry in enumerate(trace) if entry[0] == "save"
        }
        for index, entry in enumerate(trace):
            if entry[0] == "emit" and entry[1] in {"message_end", "tool_result"}:
                assert entry[2] in saved_at and saved_at[entry[2]] < index, entry
        recorded = [
            message for message in messages if isinstance(message, ToolResultMessage)
        ]
        delivered = {
            event["tool_call_id"]: event
            for event in events
            if event["type"] == "tool_result"
        }
        assert delivered and set(delivered) == {
            message.tool_call_id for message in recorded
        }
        for message in recorded:
            event = delivered[message.tool_call_id]
            assert event["content"] == message.content[0].text
            assert event["is_error"] == message.is_error

        events.clear()
        context = {
            "messages": [
                UserMessage(
                    role="user",
                    content="请回复完成。",
                    timestamp=time_ns() // 1_000_000,
                )
            ],
            "tools": {},
        }
        messages = []
        limits.clear()
        asyncio.run(run_loop(context, messages, loop_config, None, emit, checked_stream))
        assert limits == [16384]
        assert len(messages) == 1 and isinstance(messages[0], AssistantMessage)
        assert messages[0].stop_reason == "stop"
        assert messages[0].usage is not None and messages[0].usage.output <= 16384
    print("真实 OpenAI 固定输出预算、每轮参数传递、上下文隔离与工具循环检查通过")


if __name__ == "__main__":
    check()
