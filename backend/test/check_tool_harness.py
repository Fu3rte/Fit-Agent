import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import sleep
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict, Field

from app.agent.agent_loop import run_agent_loop
from app.agent.config import AgentLoopConfig
from app.agent.tool import (
    AfterToolCallResult,
    AgentTool,
    AgentToolResult,
    BeforeToolCallResult,
    run_tool_call,
)
from app.ai.messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from app.ai.stream import AssistantResponse
from test.regression_support import text


class ProbeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str
    value: int = Field(default=1, ge=1)


def call(name: str, arguments: dict, **options):
    tool_call = ToolCall(type="toolCall", id="call", name=name, arguments=arguments)
    return asyncio.run(run_tool_call(tool_call, **options))


def check() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)

        def execute(tool_call_id, params: ProbeArguments, signal, on_update):
            target = root / f"{params.name}.txt"
            target.write_text(str(params.value), encoding="utf-8")
            return AgentToolResult([TextContent(type="text", text=f"写入 {target.name}")])

        tool = AgentTool(
            "probe", "probe", ProbeArguments, execute, execution_mode="sequential"
        )
        declared = {"probe": tool.definition()}

        message = call("probe", {"name": "a"}, tools={}, declared={})
        assert message.is_error is True and "工具不存在" in text(message)

        message = call("probe", {"name": "a"}, tools={"probe": tool}, declared={})
        assert message.is_error is True and "未授权" in text(message)
        assert not (root / "a.txt").exists()

        message = call(
            "probe", {"name": "b", "value": 5}, tools={"probe": tool}, declared=declared
        )
        assert message.is_error is False
        assert (root / "b.txt").read_text(encoding="utf-8") == "5"

        prepared = replace(
            tool,
            prepare_arguments=lambda args: {**args, "value": args.get("value", 1) * 3},
        )
        message = call(
            "probe",
            {"name": "c", "value": 2},
            tools={"probe": prepared},
            declared={"probe": prepared.definition()},
        )
        assert message.is_error is False
        assert (root / "c.txt").read_text(encoding="utf-8") == "6"

        def boom(args):
            raise ValueError("参数预处理崩溃")

        failing = replace(tool, prepare_arguments=boom)
        message = call(
            "probe",
            {"name": "d"},
            tools={"probe": failing},
            declared={"probe": failing.definition()},
        )
        assert message.is_error is True and "参数预处理失败" in text(message)
        assert "参数预处理崩溃" in text(message)
        assert not (root / "d.txt").exists()

        message = call(
            "probe",
            {"name": "e", "value": 0},
            tools={"probe": tool},
            declared=declared,
        )
        assert message.is_error is True and "value" in text(message)
        assert "收到的参数" in text(message)
        assert not (root / "e.txt").exists()

        def explode(tool_call_id, params, signal, on_update):
            raise RuntimeError("执行崩溃")

        broken = replace(tool, execute=explode)
        message = call(
            "probe",
            {"name": "f"},
            tools={"probe": broken},
            declared={"probe": broken.definition()},
        )
        assert message.is_error is True and "工具执行失败" in text(message)
        assert "执行崩溃" in text(message)

        async def block(context, signal):
            return BeforeToolCallResult(block=True, reason="权限不足")

        message = call(
            "probe",
            {"name": "g"},
            tools={"probe": tool},
            declared=declared,
            before_tool_call=block,
        )
        assert message.is_error is True and "权限不足" in text(message)
        assert not (root / "g.txt").exists()

        async def override(context, signal):
            assert context.duration_ms >= 0
            return AfterToolCallResult(
                content=[TextContent(type="text", text="后处理覆盖")], is_error=True
            )

        message = call(
            "probe",
            {"name": "h"},
            tools={"probe": tool},
            declared=declared,
            after_tool_call=override,
        )
        assert message.is_error is True and text(message) == "后处理覆盖"
        assert (root / "h.txt").exists()

        async def after_boom(context, signal):
            raise RuntimeError("后处理崩溃")

        message = call(
            "probe",
            {"name": "i"},
            tools={"probe": tool},
            declared=declared,
            after_tool_call=after_boom,
        )
        assert message.is_error is True and "工具后处理失败" in text(message)
        assert "写入 i.txt" in text(message)
        assert (root / "i.txt").exists()

        async def mutate(context, signal):
            context.tool_call.id = "hacked"
            context.arguments.name = "hacked"
            return None

        message = call(
            "probe",
            {"name": "k"},
            tools={"probe": tool},
            declared=declared,
            before_tool_call=mutate,
        )
        assert message.tool_call_id == "call"
        assert (root / "k.txt").exists() and not (root / "hacked.txt").exists()

        def pollute(args):
            args["value"] = []
            return args

        polluted = replace(tool, prepare_arguments=pollute)
        message = call(
            "probe",
            {"name": "l", "value": 5},
            tools={"probe": polluted},
            declared={"probe": polluted.definition()},
        )
        assert message.is_error is True and message.tool_call_id == "call"
        assert '"value": 5' in text(message) and "[]" not in text(message)
        assert not (root / "l.txt").exists()

        inconsistent = replace(tool, description="不一致的声明")
        try:
            call(
                "probe",
                {"name": "m"},
                tools={"probe": tool},
                declared={"probe": inconsistent.definition()},
            )
        except ValueError as error:
            assert "不一致" in str(error)
        else:
            raise AssertionError("声明与注册表不一致未抛出框架异常")
        assert not (root / "m.txt").exists()

        started = Event()
        cleanup = root / "cleanup.txt"

        def slow(tool_call_id, params: ProbeArguments, signal, on_update):
            started.set()
            while not signal.is_set():
                sleep(0.01)
            cleanup.write_text("done", encoding="utf-8")
            return AgentToolResult(
                [TextContent(type="text", text="已中止")], is_error=True
            )

        slow_tool = replace(tool, execute=slow)

        async def cancel_running():
            signal = Event()
            tool_call = ToolCall(
                type="toolCall",
                id="cancel-call",
                name="probe",
                arguments={"name": "n"},
            )
            task = asyncio.create_task(
                run_tool_call(
                    tool_call,
                    tools={"probe": slow_tool},
                    declared={"probe": slow_tool.definition()},
                    signal=signal,
                )
            )
            while not started.is_set():
                await asyncio.sleep(0.01)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert signal.is_set()
            assert cleanup.read_text(encoding="utf-8") == "done"

        asyncio.run(cancel_running())

        limited = replace(tool, max_output_chars=4)
        message = call(
            "probe",
            {"name": "j"},
            tools={"probe": limited},
            declared={"probe": limited.definition()},
        )
        assert message.is_error is False and "[输出截断" in text(message)
    print("工具 harness 查找、预处理、校验、权限、执行、后处理与截断检查通过")


def check_loop_hooks() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)

        def execute(tool_call_id, params: ProbeArguments, signal, on_update):
            (root / f"{params.name}.txt").write_text(
                str(params.value), encoding="utf-8"
            )
            return AgentToolResult(
                [TextContent(type="text", text=f"写入 {params.name}.txt")]
            )

        tool = AgentTool(
            "probe", "probe 工具", ProbeArguments, execute, execution_mode="sequential"
        )
        declaration = tool.definition()
        model = SimpleNamespace(
            MODEL_API="openai-completions",
            OPENAI_PROVIDER="openai",
            OPENAI_MODEL="test-model",
            OPENAI_BASE_URL="http://unused",
            OPENAI_API_KEY="unused",
        )
        system = SystemMessage(
            role="system", content="", tools_added=[declaration], timestamp=0
        )
        user = UserMessage(role="user", content="执行 probe。", timestamp=0)

        calls = {"count": 0}
        tool_use = AssistantMessage(
            role="assistant",
            content=[
                ToolCall(
                    type="toolCall",
                    id="loop-call",
                    name="probe",
                    arguments={"name": "loop"},
                )
            ],
            api=model.MODEL_API,
            provider=model.OPENAI_PROVIDER,
            model=model.OPENAI_MODEL,
            usage=Usage(
                input=1, output=1, cache_read=0, cache_write=0, total_tokens=2
            ),
            stop_reason="toolUse",
            timestamp=0,
        )
        stop = AssistantMessage(
            role="assistant",
            content=[TextContent(type="text", text="完成")],
            api=model.MODEL_API,
            provider=model.OPENAI_PROVIDER,
            model=model.OPENAI_MODEL,
            usage=Usage(
                input=1, output=1, cache_read=0, cache_write=0, total_tokens=2
            ),
            stop_reason="stop",
            timestamp=0,
        )

        async def single(message: AssistantMessage):
            yield {
                "type": "start",
                "partial": message.model_copy(
                    update={"stop_reason": "pending", "usage": None}
                ),
            }
            yield {"type": "done", "reason": message.stop_reason, "message": message}

        def stream_fn(model_spec, context, options):
            calls["count"] += 1
            return AssistantResponse(
                single(tool_use if calls["count"] == 1 else stop)
            )

        async def before(context, signal):
            assert context.arguments.name == "loop"
            return BeforeToolCallResult(block=True, reason="循环权限拒绝")

        async def emit(event):
            pass

        config = AgentLoopConfig(model=model, max_turns=4, before_tool_call=before)
        messages = asyncio.run(
            run_agent_loop(
                [user],
                {"messages": [system], "tools": {"probe": tool}},
                config,
                emit,
                stream_fn=stream_fn,
            )
        )
        results = [item for item in messages if isinstance(item, ToolResultMessage)]
        assert results and results[0].is_error and "循环权限拒绝" in text(results[0])
        assert not (root / "loop.txt").exists()

        calls["count"] = 0
        captured = []

        async def after(context, signal):
            assert context.arguments.name == "loop" and context.duration_ms >= 0
            captured.append(context.tool_call.id)
            return AfterToolCallResult(
                content=[TextContent(type="text", text="循环后处理")]
            )

        config = AgentLoopConfig(model=model, max_turns=4, after_tool_call=after)
        messages = asyncio.run(
            run_agent_loop(
                [user],
                {"messages": [system], "tools": {"probe": tool}},
                config,
                emit,
                stream_fn=stream_fn,
            )
        )
        results = [item for item in messages if isinstance(item, ToolResultMessage)]
        assert results and text(results[0]) == "循环后处理"
        assert captured == ["loop-call"]
        assert (root / "loop.txt").exists()
    print("Agent 循环层 before/after hook 配置、传递与结果覆盖检查通过")


if __name__ == "__main__":
    check()
    check_loop_hooks()
