import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from uuid import uuid4

from src.agent.agent_loop import run_agent_loop, run_loop
from src.agent.config import AgentLoopConfig
from src.agent.tools.files import WORKSPACE, create_file_tools
from src.ai.api.openai_completions import to_openai_request
from src.ai.messages import (
    AssistantMessage,
    SystemMessage,
    ToolResultMessage,
    UserMessage,
)
from src.ai.stream import stream
from src.model_config import load_model_config


def check():
    config = load_model_config()
    assert config.MODEL_API == "openai-completions"
    tools = create_file_tools()
    loop_config = AgentLoopConfig(model=config, max_turns=8)
    limits = []

    def checked_stream(model, context, options):
        assert options["max_tokens"] == 16384
        request = to_openai_request(context["messages"], model, options)
        assert request["max_tokens"] == options["max_tokens"]
        assert "reasoning_effort" not in request and "thinking" not in request
        limits.append(options["max_tokens"])
        return stream(model, context, options)

    with TemporaryDirectory(dir=WORKSPACE) as directory:
        root = Path(directory)
        path = f"{root.name}/react.txt"
        token = uuid4().hex
        events = []

        async def emit(event):
            events.append(event)

        system = SystemMessage(
            role="system",
            content="执行用户指定的工具操作，工具参数遵循声明。",
            tools_added=[tool.definition() for tool in tools.values()],
            timestamp=time_ns() // 1_000_000,
        )
        prompts = [
            UserMessage(
                role="user",
                content=f"只操作 {root.name}/ 内的文件。必须调用 write 在 {path} 写入精确文本 "
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
        assert (root / "react.txt").read_text(encoding="utf-8") == token
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
