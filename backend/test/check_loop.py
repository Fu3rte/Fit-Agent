import json
import subprocess
import sys
from concurrent.futures import CancelledError, ThreadPoolExecutor
from copy import deepcopy
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import time_ns
from uuid import uuid4

from src.agent.loop import run_turn
from src.agent.message_context import convert_to_llm, validate_tool_pairs
from src.agent.messages import (
    AssistantMessage,
    SystemMessage,
    ToolResultMessage,
    UserMessage,
    serialize_message,
)
from src.agent.prompts import SYSTEM_PROMPT
from src.agent.providers.openai import complete
from src.agent.tools.files import WORKSPACE, create_file_tools
from src.agent.usage import summarize_usage
from src.model_config import load_model_config


def check() -> None:
    tools = create_file_tools()
    config = load_model_config()
    with (
        TemporaryDirectory(dir=WORKSPACE) as directory,
        config.create_client() as client,
    ):
        root = Path(directory)
        path = f"{root.name}/note.txt"
        token = uuid4().hex
        messages = [
            SystemMessage(
                role="system",
                content=SYSTEM_PROMPT,
                toolsAdded=[tool.definition() for tool in tools.values()],
                timestamp=time_ns() // 1_000_000,
            )
        ]
        request = partial(complete, client, config)

        def turn(prompt: str):
            pending = deepcopy(messages)
            pending.append(
                UserMessage(
                    role="user", content=prompt, timestamp=time_ns() // 1_000_000
                )
            )
            before = [serialize_message(message) for message in pending]
            order = []

            async def transform(items, signal):
                assert items is not pending and signal is None
                order.append("transform")
                items[0].sections = {"check": "执行本次用户请求，工具参数遵循声明。"}
                return items

            async def convert(items):
                order.append("convert")
                return convert_to_llm(items)

            events = list(
                run_turn(
                    pending,
                    request,
                    tools,
                    64,
                    transform_context=transform,
                    convert_to_llm=convert,
                )
            )
            assert [
                serialize_message(message) for message in pending[: len(before)]
            ] == before
            assert order and order == ["transform", "convert"] * (len(order) // 2)
            assert len(order) // 2 == sum(
                isinstance(message, AssistantMessage)
                for message in pending[len(before) :]
            )
            validate_tool_pairs(pending)
            assert events[-1].event == "done"
            assert events[-1].data == {"status": "completed"}
            assert events[-2].event == "message" and events[-2].data["text"]
            starts = {}
            results = {}
            for event in events[:-2]:
                identifier = event.data["tool_call_id"]
                if event.event == "tool_start":
                    assert identifier not in starts
                    assert isinstance(event.data["arguments"], dict)
                    starts[identifier] = event.data
                else:
                    assert event.event == "tool_result"
                    assert identifier in starts and identifier not in results
                    results[identifier] = event.data["content"]
            assert starts.keys() == results.keys()
            for message in pending:
                if (
                    isinstance(message, ToolResultMessage)
                    and message.toolCallId in results
                ):
                    assert message.content[0].text == results[message.toolCallId]
                    assert message.isError is False
                    assert message.toolName == starts[message.toolCallId]["name"]
                if isinstance(message, AssistantMessage):
                    assert message.api == "openai-completions"
                    assert message.provider == config.OPENAI_PROVIDER
                    assert message.model == config.OPENAI_MODEL
                    assert (
                        message.responseId
                        and message.responseModel
                        and message.timestamp
                    )
                    assert "cost" not in message.usage.model_fields_set
            totals = summarize_usage(pending)
            assert totals.totalTokens == sum(
                message.usage.totalTokens
                for message in pending
                if isinstance(message, AssistantMessage)
            )
            assert "cost" not in totals.model_dump(exclude_unset=True)
            messages[:] = pending
            return events, starts, results

        _, starts, _ = turn(
            f"只操作 {root.name}/ 内的文件。使用 write 在 {path} 写入精确文本 "
            f"{token}，然后使用 read 读取该文件。完成后说明结果。"
        )
        assert {item["name"] for item in starts.values()} >= {"write", "read"}
        assert (root / "note.txt").read_text(encoding="utf-8") == token
        count = len(messages)
        events, starts, results = turn(
            "沿用上一轮上下文中的文件路径和原文本：使用 edit 将原文本加上 -updated；"
            "edit 的 new_text 必须精确等于上一轮原文本拼接 -updated，禁止附加空白或换行；"
            "使用 read 读取修改后的文件；使用 ls 列出该文件的父目录；"
            "使用 find 在该父目录查找 **/*.txt；使用 grep 在该父目录搜索 updated。"
            "所有工具均必须调用，最后回答原文本和新文本。"
        )
        evidence = Path(__file__).resolve().parents[2] / ".pi/delivery/react-chat/t1"
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "real-multiturn.json").write_text(
            json.dumps(
                {
                    "events": [
                        {"event": event.event, "data": event.data} for event in events
                    ],
                    "file_content": (root / "note.txt").read_text(encoding="utf-8"),
                    "messages": [
                        json.loads(serialize_message(message)) for message in messages
                    ],
                    "usage": summarize_usage(messages).model_dump(exclude_unset=True),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        assert len(messages) > count
        assert {item["name"] for item in starts.values()} >= {
            "edit",
            "read",
            "ls",
            "find",
            "grep",
        }
        assert (root / "note.txt").read_text(encoding="utf-8") == token + "-updated"
        assert token in events[-2].data["text"]
        assert any(token + "-updated" in result for result in results.values())

        cancel = Event()
        pending = deepcopy(messages)
        pending.append(
            UserMessage(
                role="user",
                content=f"使用 write 将 {path} 覆盖为 cancelled。",
                timestamp=time_ns() // 1_000_000,
            )
        )
        iterator = run_turn(pending, request, tools, 64, cancel)
        first = next(iterator)
        assert first.event == "tool_start" and first.data["name"] == "write"
        cancel.set()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(list, iterator)
            assert isinstance(future.exception(), CancelledError)
        assert (root / "note.txt").read_text(encoding="utf-8") == token + "-updated"
        assert messages[-1].role == "assistant"

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                list, run_turn(deepcopy(messages), request, tools, 64, cancel)
            )
            assert isinstance(future.exception(), CancelledError)

        cancel.clear()

        def complete_then_cancel(history):
            response = request(history)
            cancel.set()
            return response

        pending = deepcopy(messages)
        pending.append(
            UserMessage(
                role="user", content="请回答完成。", timestamp=time_ns() // 1_000_000
            )
        )
        before = deepcopy(pending)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                list, run_turn(pending, complete_then_cancel, tools, 64, cancel)
            )
            assert isinstance(future.exception(), CancelledError)
        assert pending == before

        backend = Path(__file__).resolve().parents[1]
        cli = subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                "main.py",
                "--prompt",
                f"使用 read 读取 {path} 并说明实际内容。",
            ],
            cwd=backend,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
        )
        assert cli.returncode == 0
        assert all(
            label in cli.stdout for label in ("Action>", "Observation>", "Agent>")
        )
        assert token + "-updated" in cli.stdout
        interactive = subprocess.run(
            [sys.executable, "-X", "utf8", "main.py"],
            input=f"使用 read 读取 {path} 并报告内容。\n使用 read 再次读取上一轮的同一文件并报告内容。\n/exit\n",
            cwd=backend,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=240,
        )
        assert interactive.returncode == 0
        assert all(
            interactive.stdout.count(label) >= 2
            for label in ("Action>", "Observation>", "Agent>")
        )
        assert interactive.stdout.count(token + "-updated") >= 2
        assert (
            config.OPENAI_API_KEY
            not in cli.stdout + cli.stderr + interactive.stdout + interactive.stderr
        )
        (evidence / "real-cli.json").write_text(
            json.dumps(
                {"single_turn": cli.stdout, "multi_turn": interactive.stdout},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    print("真实模型多轮上下文、六工具事件配对、取消边界与 CLI 检查通过")


if __name__ == "__main__":
    check()
