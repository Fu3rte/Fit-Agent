import asyncio
from concurrent.futures import CancelledError, ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import time_ns
from uuid import uuid4

from src.agent.agent_loop import run_agent_loop
from src.agent.config import AgentLoopConfig
from src.agent.message_context import convert_to_llm
from src.agent.prompts import SYSTEM_PROMPT
from src.agent.tools.files import WORKSPACE, create_file_tools
from src.agent.usage import summarize_usage
from src.ai.context import validate_tool_pairs
from src.ai.messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    ToolResultMessage,
    UserMessage,
    serialize_message,
)
from src.model_config import load_model_config


def check() -> None:
    tools = create_file_tools()
    config = load_model_config()
    base_config = AgentLoopConfig(model=config, max_turns=64)
    with TemporaryDirectory(dir=WORKSPACE) as directory:
        root = Path(directory)
        path = f"{root.name}/note.txt"
        token = uuid4().hex
        session: list = [
            SystemMessage(
                role="system",
                content=SYSTEM_PROMPT,
                tools_added=[tool.definition() for tool in tools.values()],
                timestamp=time_ns() // 1_000_000,
            )
        ]

        def turn(prompt: str):
            events = []
            order = []

            async def emit(event):
                events.append(event)

            async def transform(items, signal):
                assert signal is None
                order.append("transform")
                items[0].sections = {"check": "执行本次用户请求，工具参数遵循声明。"}
                return items

            async def convert(items):
                order.append("convert")
                return convert_to_llm(items)

            pending = deepcopy(session)
            user = UserMessage(
                role="user", content=prompt, timestamp=time_ns() // 1_000_000
            )
            before = [serialize_message(message) for message in pending]
            context = {"messages": pending, "tools": tools}
            local = AgentLoopConfig(
                model=config,
                max_turns=64,
                transform_context=transform,
                convert_to_llm=convert,
            )
            new_messages = asyncio.run(run_agent_loop([user], context, local, emit))
            assert context["messages"] is pending
            assert [serialize_message(message) for message in pending] == before
            assert order and order == ["transform", "convert"] * (len(order) // 2)
            assert len(order) // 2 == sum(
                isinstance(message, AssistantMessage) for message in new_messages
            )
            committed = [*session, *new_messages]
            validate_tool_pairs(committed)
            assert (
                events[-1]["type"] == "trace_end"
                and events[-1]["status"] == "completed"
            )
            starts = {}
            results = {}
            for event in events:
                if event["type"] == "tool_start":
                    assert event["tool_call_id"] not in starts
                    assert isinstance(event["arguments"], dict)
                    starts[event["tool_call_id"]] = event
                elif event["type"] == "tool_result":
                    assert event["tool_call_id"] in starts
                    results[event["tool_call_id"]] = event["content"]
            assert starts.keys() == results.keys()
            for message in committed:
                if (
                    isinstance(message, ToolResultMessage)
                    and message.tool_call_id in results
                ):
                    assert message.content[0].text == results[message.tool_call_id]
                    assert message.is_error is False
                    assert message.tool_name == starts[message.tool_call_id]["name"]
                if isinstance(message, AssistantMessage):
                    assert message.api == "openai-completions"
                    assert message.provider == config.OPENAI_PROVIDER
                    assert message.model == config.OPENAI_MODEL
                    assert (
                        message.response_id
                        and message.response_model
                        and message.timestamp
                    )
                    assert "cost" not in message.usage.model_fields_set
            totals = summarize_usage(committed)
            assert totals.total_tokens == sum(
                message.usage.total_tokens
                for message in committed
                if isinstance(message, AssistantMessage)
            )
            assert "cost" not in totals.model_dump(exclude_unset=True)
            session[:] = committed
            return events, starts, results

        _, starts, _ = turn(
            f"只操作 {root.name}/ 内的文件。使用 write 在 {path} 写入精确文本 "
            f"{token}，然后使用 read 读取该文件。完成后说明结果。"
        )
        assert {item["name"] for item in starts.values()} >= {"write", "read"}
        assert (root / "note.txt").read_text(encoding="utf-8") == token
        count = len(session)
        events, starts, results = turn(
            "沿用上一轮上下文中的文件路径和原文本：使用 edit 将原文本加上 -updated；"
            "edit 的 new_text 必须精确等于上一轮原文本拼接 -updated，禁止附加空白或换行；"
            "使用 read 读取修改后的文件；使用 ls 列出该文件的父目录；"
            "使用 find 在该父目录查找 **/*.txt；使用 grep 在该父目录搜索 updated。"
            "所有工具均必须调用，最后回答原文本和新文本。"
        )
        assert len(session) > count
        assert {item["name"] for item in starts.values()} >= {
            "edit",
            "read",
            "ls",
            "find",
            "grep",
        }
        assert (root / "note.txt").read_text(encoding="utf-8") == token + "-updated"
        final_text = "".join(
            block.text
            for block in session[-1].content
            if isinstance(block, TextContent)
        )
        assert token in final_text and token + "-updated" in final_text
        assert any(token + "-updated" in result for result in results.values())

        cancel = Event()

        async def cancel_on_tool(event):
            if event["type"] == "tool_start":
                cancel.set()

        pending = deepcopy(session)
        user = UserMessage(
            role="user",
            content=f"使用 write 将 {path} 覆盖为 cancelled。",
            timestamp=time_ns() // 1_000_000,
        )
        context = {"messages": pending, "tools": tools}
        with ThreadPoolExecutor(max_workers=1) as executor:
            failure = executor.submit(
                asyncio.run,
                run_agent_loop([user], context, base_config, cancel_on_tool, cancel),
            ).exception()
        assert isinstance(failure, CancelledError)
        assert (root / "note.txt").read_text(encoding="utf-8") == token + "-updated"
        assert isinstance(session[-1], AssistantMessage)

        async def noop(event):
            pass

        with ThreadPoolExecutor(max_workers=1) as executor:
            failure = executor.submit(
                asyncio.run,
                run_agent_loop(
                    [user],
                    {"messages": deepcopy(session), "tools": tools},
                    base_config,
                    noop,
                    cancel,
                ),
            ).exception()
        assert isinstance(failure, CancelledError)

        cancel.clear()

        async def cancel_on_message_end(event):
            if event["type"] == "message_end":
                cancel.set()

        pending = deepcopy(session)
        user = UserMessage(
            role="user", content="请回答完成。", timestamp=time_ns() // 1_000_000
        )
        before = deepcopy(pending)
        with ThreadPoolExecutor(max_workers=1) as executor:
            failure = executor.submit(
                asyncio.run,
                run_agent_loop(
                    [user],
                    {"messages": pending, "tools": tools},
                    base_config,
                    cancel_on_message_end,
                    cancel,
                ),
            ).exception()
        assert isinstance(failure, CancelledError)
        assert pending == before

        strict = AgentLoopConfig(model=config, max_turns=1)
        with ThreadPoolExecutor(max_workers=1) as executor:
            failure = executor.submit(
                asyncio.run,
                run_agent_loop(
                    [
                        UserMessage(
                            role="user",
                            content=f"必须调用 write 在 {path} 写入 max-turns 文本，"
                            "然后必须调用 read 读取该文件，最后回答问题。",
                            timestamp=time_ns() // 1_000_000,
                        )
                    ],
                    {"messages": deepcopy(session), "tools": tools},
                    strict,
                    noop,
                ),
            ).exception()
        assert isinstance(failure, RuntimeError) and "上限" in str(failure)

    print("真实模型多轮上下文、工具事件配对、取消边界与最大轮数检查通过")


if __name__ == "__main__":
    check()
