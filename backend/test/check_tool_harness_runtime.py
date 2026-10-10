import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock, Timer
from time import sleep
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict

from app.agent.agent_loop import run_agent_loop
from app.agent.config import AgentLoopConfig
from app.agent.tool import (
    AfterToolCallResult,
    AgentTool,
    AgentToolResult,
    CredentialDetectedError,
    ExecutionMode,
    run_tool_batch,
)
from app.agent.tools.files import create_file_tools
from app.ai.messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    ToolCall,
    UserMessage,
    text_projection,
)
from app.interfaces.http import CredentialFilter
from test.check_tool_scheduling import single, usage
from test.regression_support import TEST_SESSION, session_workspace

MARKER = "REVIEW-CREDENTIAL-MARKER"
MODEL = SimpleNamespace(
    api="openai-completions",
    provider="openai",
    model="runtime-model",
    base_url="http://unused",
    api_key="unused",
)


class ProbeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str


def make_call(name: str, arguments: dict, call_id: str) -> ToolCall:
    return ToolCall(type="toolCall", id=call_id, name=name, arguments=arguments)


def probe(
    execute, *, max_output_chars: int = 50_000, execution_mode: ExecutionMode = "parallel"
):
    return AgentTool(
        "probe",
        "probe",
        ProbeArguments,
        execute,
        execution_mode=execution_mode,
        max_output_chars=max_output_chars,
    )


def text_result(text: str) -> AgentToolResult:
    return AgentToolResult([TextContent(type="text", text=text)])


def batch(calls, **options):
    return asyncio.run(run_tool_batch(calls, **options))


def check_async_execute() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        workspace, prefix = session_workspace(root)
        (workspace / "note.txt").write_text("hello async\n", encoding="utf-8")
        read = create_file_tools(TEST_SESSION, tmp_root=root)["read"]
        call = make_call("read", {"path": f"{prefix}/note.txt"}, "c-async")

        async def read_async(tool_call_id, params, signal, on_update):
            return await asyncio.to_thread(
                read.execute, tool_call_id, params, signal, on_update
            )

        async_tool = AgentTool(
            read.name, read.description, read.arguments, read_async, read.execution_mode
        )
        result = batch(
            [call],
            tools={"read": async_tool},
            declared={"read": async_tool.definition()},
        )
        assert result.failure is None, result.failure
        assert [message.tool_call_id for message in result.messages] == ["c-async"]
        assert not result.messages[0].is_error
        assert "hello async" in text_projection(result.messages[0].content)

        sync_result = batch(
            [call], tools={"read": read}, declared={"read": read.definition()}
        )
        assert sync_result.failure is None
        assert "hello async" in text_projection(sync_result.messages[0].content)


def check_async_cancel_cleanup() -> None:
    async def scenario():
        signal = Event()
        finished: list[str] = []
        lock = Lock()
        done_finalized = Event()

        async def execute(tool_call_id, params, signal, on_update):
            if tool_call_id == "c-wait":
                while not signal.is_set():
                    await asyncio.sleep(0.01)
            with lock:
                finished.append(tool_call_id)
            return text_result(tool_call_id)

        tool = probe(execute)
        calls = [
            make_call("probe", {"name": "a"}, "c-done"),
            make_call("probe", {"name": "b"}, "c-wait"),
        ]

        async def on_finalized(finalized):
            if finalized.message.tool_call_id == "c-done":
                done_finalized.set()

        task = asyncio.create_task(
            run_tool_batch(
                calls,
                tools={"probe": tool},
                declared={"probe": tool.definition()},
                execution_mode="parallel",
                signal=signal,
                on_tool_finalized=on_finalized,
            )
        )
        while not done_finalized.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        return await task, finished, signal

    result, finished, signal = asyncio.run(scenario())
    assert signal.is_set()
    assert isinstance(result.failure, asyncio.CancelledError)
    assert [message.tool_call_id for message in result.messages] == ["c-done"]
    assert sorted(finished) == ["c-done", "c-wait"], finished


def check_full_output_credentials() -> None:
    guard = CredentialFilter((MARKER,))
    call = make_call("probe", {"name": "a"}, "c-1")

    def run(tool, **options):
        finalized: list[str] = []

        async def on_finalized(outcome):
            finalized.append(text_projection(outcome.message.content))

        result = batch(
            [call],
            tools={"probe": tool},
            declared={"probe": tool.definition()},
            on_tool_finalized=on_finalized,
            contains_credentials=guard.contains,
            **options,
        )
        return result, finalized

    # 1. 凭据位于截断边界之后：完整结果命中，禁止定稿。
    beyond = probe(lambda *_: text_result("x" * 20 + MARKER), max_output_chars=10)
    result, finalized = run(beyond)
    assert isinstance(result.failure, CredentialDetectedError), result.failure
    assert result.messages == [] and finalized == []

    # 2. 跨截断边界：截断后仅保留前半段，完整结果仍命中。
    across = probe(lambda *_: text_result("x" * 8 + MARKER), max_output_chars=12)
    result, finalized = run(across)
    assert isinstance(result.failure, CredentialDetectedError), result.failure
    assert result.messages == []

    # 3. 无凭据的完整结果照常截断并定稿。
    plain = probe(lambda *_: text_result("y" * 30), max_output_chars=10)
    result, finalized = run(plain)
    assert result.failure is None and len(result.messages) == 1
    assert finalized[0] == "y" * 10 + "\n[输出截断：超过 10 字符]"

    # 4. 执行异常文本中的凭据。
    def leak(*_):
        raise RuntimeError(f"失败 {MARKER}")

    result, _ = run(probe(leak))
    assert isinstance(result.failure, CredentialDetectedError), result.failure

    # 5. 后处理覆盖结果中的凭据。
    async def override(_context, _signal):
        return AfterToolCallResult(content=[TextContent(type="text", text=MARKER)])

    result, _ = run(probe(lambda *_: text_result("ok")), after_tool_call=override)
    assert isinstance(result.failure, CredentialDetectedError), result.failure

    # 6. 后处理异常文本中的凭据。
    async def boom(_context, _signal):
        raise RuntimeError(MARKER)

    result, _ = run(probe(lambda *_: text_result("ok")), after_tool_call=boom)
    assert isinstance(result.failure, CredentialDetectedError), result.failure


def check_progress_callback() -> None:
    # 1. 进度归属真实调用身份（含工具名），且不覆盖最终结果。
    updates: list[tuple[str, str, str]] = []

    def execute(tool_call_id, params, signal, on_update):
        assert on_update is not None
        on_update(text_result(f"progress-{tool_call_id}"))
        return text_result(f"final-{tool_call_id}")

    tool = probe(execute)
    calls = [
        make_call("probe", {"name": "a"}, "c-a"),
        make_call("probe", {"name": "b"}, "c-b"),
    ]
    result = batch(
        calls,
        tools={"probe": tool},
        declared={"probe": tool.definition()},
        on_tool_update=lambda call_id, tool_name, update: updates.append(
            (call_id, tool_name, text_projection(update.content))
        ),
    )
    assert result.failure is None
    assert sorted(updates) == [
        ("c-a", "probe", "progress-c-a"),
        ("c-b", "probe", "progress-c-b"),
    ]
    assert [text_projection(message.content) for message in result.messages] == [
        "final-c-a",
        "final-c-b",
    ]

    # 2. 无订阅者时工具仍收到 None（进度通道未接通）。
    delivered: list[bool] = []

    def observe(tool_call_id, params, signal, on_update):
        delivered.append(on_update is not None)
        return text_result("ok")

    silent = probe(observe)
    result = batch(
        [make_call("probe", {"name": "a"}, "c-1")],
        tools={"probe": silent},
        declared={"probe": silent.definition()},
    )
    assert result.failure is None and delivered == [False]

    # 3. 进度输出命中凭据：不通知订阅者并终止运行。
    notified: list[str] = []

    def leak(tool_call_id, params, signal, on_update):
        on_update(text_result(MARKER))
        return text_result("ok")

    leaking = probe(leak)
    result = batch(
        [make_call("probe", {"name": "a"}, "c-1")],
        tools={"probe": leaking},
        declared={"probe": leaking.definition()},
        on_tool_update=lambda call_id, tool_name, update: notified.append(call_id),
        contains_credentials=CredentialFilter((MARKER,)).contains,
    )
    assert isinstance(result.failure, CredentialDetectedError), result.failure
    assert notified == [] and result.messages == []

    # 4. Agent loop 接通 config.on_tool_update。
    loop_updates: list[tuple[str, str]] = []
    loop_tool = probe(execute)
    count = {"value": 0}
    system = SystemMessage(
        role="system", content="", tools_added=[loop_tool.definition()], timestamp=0
    )
    tool_use = AssistantMessage(
        role="assistant",
        content=[
            ToolCall(type="toolCall", id="c-loop", name="probe", arguments={"name": "a"})
        ],
        api=MODEL.api,
        provider=MODEL.provider,
        model=MODEL.model,
        usage=usage(),
        stop_reason="toolUse",
        timestamp=0,
    )
    stop = AssistantMessage(
        role="assistant",
        content=[TextContent(type="text", text="完成")],
        api=MODEL.api,
        provider=MODEL.provider,
        model=MODEL.model,
        usage=usage(),
        stop_reason="stop",
        timestamp=0,
    )

    def stream_fn(_model_spec, _context, _options):
        count["value"] += 1
        return single(tool_use if count["value"] == 1 else stop)

    config = AgentLoopConfig(
        model=MODEL,
        max_turns=4,
        on_tool_update=lambda call_id, tool_name, update: loop_updates.append(
            (call_id, text_projection(update.content))
        ),
    )

    async def emit(_event) -> None:
        return None

    asyncio.run(
        run_agent_loop(
            [UserMessage(role="user", content="执行", timestamp=0)],
            {"messages": [system], "tools": {"probe": loop_tool}},
            config,
            emit,
            stream_fn=stream_fn,
        )
    )
    assert loop_updates == [("c-loop", "progress-c-loop")], loop_updates

    # 5. 取消：进度已按身份投递，取消后不产生最终定稿。
    async def cancel_scenario():
        signal = Event()
        updates: list[str] = []
        finalized: list[str] = []
        started = Event()

        def execute(tool_call_id, params, signal, on_update):
            on_update(text_result("progress"))
            started.set()
            assert signal.wait(timeout=10)
            return text_result("final")

        tool = probe(execute)

        async def on_finalized(outcome):
            finalized.append(outcome.message.tool_call_id)

        task = asyncio.create_task(
            run_tool_batch(
                [make_call("probe", {"name": "a"}, "c-cancel")],
                tools={"probe": tool},
                declared={"probe": tool.definition()},
                signal=signal,
                on_tool_update=lambda call_id, tool_name, update: updates.append(
                    call_id
                ),
                on_tool_finalized=on_finalized,
            )
        )
        await asyncio.to_thread(started.wait, 10)
        task.cancel()
        return await task, updates, finalized, signal

    result, updates, finalized, signal = asyncio.run(cancel_scenario())
    assert signal.is_set()
    assert isinstance(result.failure, asyncio.CancelledError)
    assert finalized == [] and result.messages == []
    assert updates == ["c-cancel"]

    # 6. 执行结束后的迟到更新被忽略。
    late: list[str] = []

    def spawn(tool_call_id, params, signal, on_update):
        Timer(0.1, lambda: on_update(text_result("late"))).start()
        return text_result("final")

    late_tool = probe(spawn)
    result = batch(
        [make_call("probe", {"name": "a"}, "c-1")],
        tools={"probe": late_tool},
        declared={"probe": late_tool.definition()},
        on_tool_update=lambda call_id, tool_name, update: late.append(
            text_projection(update.content)
        ),
    )
    assert result.failure is None
    sleep(0.3)
    assert late == [], late


def check() -> None:
    check_async_execute()
    check_async_cancel_cleanup()
    check_full_output_credentials()
    check_progress_callback()
    print(
        "PASS: 异步工具执行与取消收尾、完整输出/异常/后处理/进度凭据保护、"
        "批次与 loop 进度回调身份绑定且不覆盖最终结果"
    )


if __name__ == "__main__":
    check()
