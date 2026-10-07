import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Lock, Thread
from time import time_ns
from types import SimpleNamespace
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.agent.agent_loop import run_agent_loop
from app.agent.config import AgentLoopConfig
from app.agent.tool import (
    AgentTool,
    AgentToolResult,
    BeforeToolCallResult,
    run_tool_batch,
)
from app.agent.tools.files import WORKSPACE, create_file_tools
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
from app.application.session.service import SessionService
from app.domain.session.models import SendCommand, SendRequest, SessionMessageEntry
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from app.interfaces.http import CredentialDetectedError, CredentialFilter
from test.regression_support import temporary_root, text

EVIDENCE = temporary_root("tool-scheduling")


class ProbeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str
    value: int = Field(default=1, ge=1)


def make_call(name: str, arguments: dict, call_id: str) -> ToolCall:
    return ToolCall(type="toolCall", id=call_id, name=name, arguments=arguments)


def batch(calls, **options):
    return asyncio.run(run_tool_batch(calls, **options))


def check_prepare_before_execute() -> None:
    order: list[tuple[str, str]] = []
    lock = Lock()

    def execute(tool_call_id, params: ProbeArguments, signal, on_update):
        with lock:
            order.append(("exec", tool_call_id))
        return AgentToolResult([TextContent(type="text", text=params.name)])

    tool = AgentTool("probe", "probe", ProbeArguments, execute, execution_mode="parallel")

    async def before(context, signal):
        with lock:
            order.append(("prepare", context.tool_call.id))
        return None

    calls = [
        make_call("probe", {"name": "a"}, "c-a"),
        make_call("probe", {"name": "b"}, "c-b"),
    ]
    result = batch(
        calls,
        tools={"probe": tool},
        declared={"probe": tool.definition()},
        before_tool_call=before,
        execution_mode="parallel",
    )
    kinds = [item[0] for item in order]
    assert kinds == ["prepare", "prepare", "exec", "exec"], order
    assert [message.tool_call_id for message in result.messages] == ["c-a", "c-b"]
    assert result.prepare_started_at <= (result.first_result_at or 0) <= result.finalized_at


def check_parallel_overlap() -> None:
    barrier = Barrier(2, timeout=5)

    def execute(tool_call_id, params: ProbeArguments, signal, on_update):
        barrier.wait()
        return AgentToolResult([TextContent(type="text", text=params.name)])

    tool = AgentTool("probe", "probe", ProbeArguments, execute, execution_mode="parallel")
    calls = [
        make_call("probe", {"name": "a"}, "c-a"),
        make_call("probe", {"name": "b"}, "c-b"),
    ]
    result = batch(
        calls,
        tools={"probe": tool},
        declared={"probe": tool.definition()},
        execution_mode="parallel",
    )
    assert all(not message.is_error for message in result.messages), text(result.messages[0])
    completions = []
    result = batch(
        calls,
        tools={"probe": tool},
        declared={"probe": tool.definition()},
        execution_mode="parallel",
        on_tool_finalized=lambda finalized: _collect(completions, finalized.message.tool_call_id),
    )
    assert len(completions) == 2 and set(completions) == {"c-a", "c-b"}


async def _collect(target: list, value: str) -> None:
    target.append(value)


def _barrier_probe(barrier: Barrier, mode: str) -> AgentTool:
    def execute(tool_call_id, params: ProbeArguments, signal, on_update):
        barrier.wait()
        return AgentToolResult([TextContent(type="text", text=params.name)])

    return AgentTool("probe", "probe", ProbeArguments, execute, execution_mode=mode)


def check_global_sequential() -> None:
    barrier = Barrier(2, timeout=0.4)
    tool = _barrier_probe(barrier, "parallel")
    calls = [
        make_call("probe", {"name": "a"}, "c-a"),
        make_call("probe", {"name": "b"}, "c-b"),
    ]
    result = batch(
        calls,
        tools={"probe": tool},
        declared={"probe": tool.definition()},
        execution_mode="sequential",
    )
    assert all(message.is_error for message in result.messages), "全局 sequential 出现了执行重叠"
    barrier.reset()
    parallel = batch(
        calls,
        tools={"probe": tool},
        declared={"probe": tool.definition()},
        execution_mode="parallel",
    )
    assert all(not message.is_error for message in parallel.messages)


def check_mixed_batch_sequential() -> None:
    barrier = Barrier(2, timeout=0.4)
    parallel_tool = _barrier_probe(barrier, "parallel")
    sequential_tool = AgentTool(
        "seq", "seq", ProbeArguments,
        lambda tool_call_id, params, signal, on_update: (
            barrier.wait(),
            AgentToolResult([TextContent(type="text", text=params.name)]),
        )[1],
        execution_mode="sequential",
    )
    calls = [
        make_call("probe", {"name": "a"}, "c-a"),
        make_call("seq", {"name": "b"}, "c-b"),
    ]
    result = batch(
        calls,
        tools={"probe": parallel_tool, "seq": sequential_tool},
        declared={"probe": parallel_tool.definition(), "seq": sequential_tool.definition()},
        execution_mode="parallel",
    )
    assert any(message.is_error for message in result.messages), "含 sequential 工具的混合批次出现了执行重叠"


def check_completion_order() -> None:
    fast_finalized = Event()
    completions: list[str] = []
    lock = Lock()

    def execute(tool_call_id, params: ProbeArguments, signal, on_update):
        if params.name == "slow":
            assert fast_finalized.wait(timeout=5), "fast 未先定稿"
        return AgentToolResult([TextContent(type="text", text=params.name)])

    tool = AgentTool("probe", "probe", ProbeArguments, execute, execution_mode="parallel")
    calls = [
        make_call("probe", {"name": "slow"}, "slow"),
        make_call("probe", {"name": "fast"}, "fast"),
    ]

    async def on_finalized(finalized):
        with lock:
            completions.append(finalized.message.tool_call_id)
        if finalized.message.tool_call_id == "fast":
            fast_finalized.set()

    result = batch(
        calls,
        tools={"probe": tool},
        declared={"probe": tool.definition()},
        execution_mode="parallel",
        on_tool_finalized=on_finalized,
    )
    assert completions == ["fast", "slow"], completions
    assert completions.count("fast") == 1 and completions.count("slow") == 1
    assert [message.tool_call_id for message in result.messages] == ["slow", "fast"]
    assert [message.content[0].text for message in result.messages] == ["slow", "fast"]
    for finalized_order, message in zip(completions, [result.messages[1], result.messages[0]]):
        assert finalized_order == message.tool_call_id


def check_failure_indices() -> None:
    def prepare_failure(args):
        raise ValueError("预处理崩溃")

    def execute(tool_call_id, params: ProbeArguments, signal, on_update):
        if tool_call_id == "c-exec":
            raise RuntimeError("执行崩溃")
        return AgentToolResult([TextContent(type="text", text=params.name)])

    prep = AgentTool(
        "prep", "prep", ProbeArguments, execute,
        execution_mode="parallel", prepare_arguments=prepare_failure,
    )
    deny = AgentTool("deny", "deny", ProbeArguments, execute, execution_mode="parallel")
    fail = AgentTool("fail", "fail", ProbeArguments, execute, execution_mode="parallel")
    post = AgentTool("post", "post", ProbeArguments, execute, execution_mode="parallel")
    tools = {"prep": prep, "deny": deny, "fail": fail, "post": post}
    declared = {name: tool.definition() for name, tool in tools.items()}

    async def before(context, signal):
        if context.tool_call.id == "c-deny":
            return BeforeToolCallResult(block=True, reason="权限不足")
        return None

    async def after(context, signal):
        if context.tool_call.id == "c-post":
            raise RuntimeError("后处理崩溃")
        return None

    calls = [
        make_call("prep", {"name": "a"}, "c-prep"),
        make_call("deny", {"name": "b"}, "c-deny"),
        make_call("fail", {"name": "c"}, "c-exec"),
        make_call("post", {"name": "d"}, "c-post"),
    ]
    completions: list[str] = []
    result = batch(
        calls,
        tools=tools,
        declared=declared,
        before_tool_call=before,
        after_tool_call=after,
        execution_mode="parallel",
        on_tool_finalized=lambda finalized: _collect(completions, finalized.message.tool_call_id),
    )
    assert [message.tool_call_id for message in result.messages] == [
        "c-prep", "c-deny", "c-exec", "c-post",
    ]
    expected = {
        "c-prep": "参数预处理失败",
        "c-deny": "权限不足",
        "c-exec": "工具执行失败",
        "c-post": "工具后处理失败",
    }
    for message in result.messages:
        assert message.is_error is True
        assert expected[message.tool_call_id] in text(message), text(message)
    assert sorted(completions) == sorted(expected)


def check_hook_isolation() -> None:
    seen: dict[str, str] = {}
    lock = Lock()

    def execute(tool_call_id, params: ProbeArguments, signal, on_update):
        return AgentToolResult([TextContent(type="text", text=params.name)])

    tool = AgentTool("probe", "probe", ProbeArguments, execute, execution_mode="parallel")

    async def before(context, signal):
        with lock:
            seen[context.tool_call.id] = context.arguments.name
        return None

    async def after(context, signal):
        assert context.duration_ms >= 0
        with lock:
            seen[context.tool_call.id] = context.arguments.name
        return None

    names = {"c-1": "one", "c-2": "two", "c-3": "three"}
    calls = [make_call("probe", {"name": name}, call_id) for call_id, name in names.items()]
    result = batch(
        calls,
        tools={"probe": tool},
        declared={"probe": tool.definition()},
        before_tool_call=before,
        after_tool_call=after,
        execution_mode="parallel",
    )
    assert seen == names, seen
    assert [message.tool_call_id for message in result.messages] == ["c-1", "c-2", "c-3"]


def check_parallel_cancel_cleanup() -> None:
    signal = Event()
    both_started = Event()
    lock = Lock()
    started = {"count": 0}
    finished: list[str] = []

    def execute(tool_call_id, params: ProbeArguments, signal, on_update):
        with lock:
            started["count"] += 1
            if started["count"] == 2:
                both_started.set()
        assert signal.wait(timeout=10)
        with lock:
            finished.append(tool_call_id)
        return AgentToolResult([TextContent(type="text", text="已取消")], is_error=True)

    tool = AgentTool("probe", "probe", ProbeArguments, execute, execution_mode="parallel")
    calls = [
        make_call("probe", {"name": "a"}, "c-a"),
        make_call("probe", {"name": "b"}, "c-b"),
    ]
    watcher = Thread(target=lambda: (both_started.wait(timeout=10), signal.set()))
    watcher.start()
    try:
        result = batch(
            calls,
            tools={"probe": tool},
            declared={"probe": tool.definition()},
            execution_mode="parallel",
            signal=signal,
        )
    finally:
        watcher.join()
    assert signal.is_set()
    assert sorted(finished) == ["c-a", "c-b"], finished
    assert [message.tool_call_id for message in result.messages] == ["c-a", "c-b"]


def check_event_failure_cleanup() -> None:
    lock = Lock()
    finished: list[str] = []

    def execute(tool_call_id, params: ProbeArguments, signal, on_update):
        with lock:
            finished.append(tool_call_id)
        return AgentToolResult([TextContent(type="text", text="ok")])

    tool = AgentTool("probe", "probe", ProbeArguments, execute, execution_mode="parallel")
    calls = [
        make_call("probe", {"name": "a"}, "c-a"),
        make_call("probe", {"name": "b"}, "c-b"),
    ]

    async def on_finalized(finalized):
        raise RuntimeError("事件回调失败")

    result = batch(
        calls,
        tools={"probe": tool},
        declared={"probe": tool.definition()},
        execution_mode="parallel",
        on_tool_finalized=on_finalized,
    )
    assert isinstance(result.failure, RuntimeError)
    assert "事件回调失败" in str(result.failure)
    assert result.messages == []
    assert sorted(finished) == ["c-a", "c-b"], finished


def check_failure_preserves_results() -> None:
    def execute(tool_call_id, params: ProbeArguments, signal, on_update):
        return AgentToolResult([TextContent(type="text", text=params.name)])

    tool = AgentTool("probe", "probe", ProbeArguments, execute, execution_mode="parallel")
    calls = [
        make_call("probe", {"name": "a"}, "c-a"),
        make_call("probe", {"name": "b"}, "c-b"),
    ]

    async def on_finalized(finalized):
        if finalized.message.tool_call_id == "c-b":
            raise RuntimeError("后处理回调失败")

    result = batch(
        calls,
        tools={"probe": tool},
        declared={"probe": tool.definition()},
        execution_mode="parallel",
        on_tool_finalized=on_finalized,
    )
    assert [message.tool_call_id for message in result.messages] == ["c-a"]
    assert isinstance(result.failure, RuntimeError)


def check_failure_signals_in_flight() -> None:
    signal = Event()
    both_started = Event()
    lock = Lock()
    started = {"count": 0}
    finished: list[str] = []

    def execute(tool_call_id, params: ProbeArguments, signal, on_update):
        with lock:
            started["count"] += 1
            if started["count"] == 2:
                both_started.set()
        if tool_call_id == "c-a":
            assert signal.wait(timeout=10), "框架失败未通知在途工具停止"
        with lock:
            finished.append(tool_call_id)
        return AgentToolResult([TextContent(type="text", text=params.name)])

    tool = AgentTool("probe", "probe", ProbeArguments, execute, execution_mode="parallel")
    calls = [
        make_call("probe", {"name": "a"}, "c-a"),
        make_call("probe", {"name": "b"}, "c-b"),
    ]

    async def on_finalized(finalized):
        if finalized.message.tool_call_id == "c-b":
            raise RuntimeError("回调失败")

    result = batch(
        calls,
        tools={"probe": tool},
        declared={"probe": tool.definition()},
        execution_mode="parallel",
        signal=signal,
        on_tool_finalized=on_finalized,
    )
    assert signal.is_set()
    assert isinstance(result.failure, RuntimeError)
    assert [message.tool_call_id for message in result.messages] == ["c-a"]
    assert sorted(finished) == ["c-a", "c-b"], finished


def check_credential_stops_sequential_scheduling() -> None:
    executed: list[str] = []

    def execute(tool_call_id, params: ProbeArguments, signal, on_update):
        executed.append(tool_call_id)
        return AgentToolResult([TextContent(type="text", text="受保护内容")])

    tool = AgentTool("probe", "probe", ProbeArguments, execute, execution_mode="sequential")
    calls = [
        make_call("probe", {"name": "a"}, "c-a"),
        make_call("probe", {"name": "b"}, "c-b"),
    ]

    async def on_finalized(finalized):
        if finalized.message.tool_call_id == "c-a":
            raise CredentialDetectedError()

    result = batch(
        calls,
        tools={"probe": tool},
        declared={"probe": tool.definition()},
        execution_mode="sequential",
        on_tool_finalized=on_finalized,
    )
    assert executed == ["c-a"], executed
    assert result.messages == []
    assert result.first_result_at is None
    assert isinstance(result.failure, CredentialDetectedError)


def check_task_cancel_cleanup() -> None:
    async def scenario():
        signal = Event()
        both_started = Event()
        lock = Lock()
        started = {"count": 0}
        finished: list[str] = []
        done_finalized = Event()

        def execute(tool_call_id, params: ProbeArguments, signal, on_update):
            with lock:
                started["count"] += 1
                if started["count"] == 2:
                    both_started.set()
            if tool_call_id == "c-wait":
                assert signal.wait(timeout=10)
            with lock:
                finished.append(tool_call_id)
            return AgentToolResult([TextContent(type="text", text=tool_call_id)])

        tool = AgentTool("probe", "probe", ProbeArguments, execute, execution_mode="parallel")
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


def check_real_file_tools() -> None:
    with TemporaryDirectory(dir=WORKSPACE) as directory:
        root = Path(directory)
        tools = create_file_tools()
        (root / "note.txt").write_text("hello\nworld\n", encoding="utf-8")
        prefix = root.name
        calls = [
            make_call("read", {"path": f"{prefix}/note.txt"}, "c-read"),
            make_call("grep", {"path": f"{prefix}", "pattern": "world", "glob": "**/*.txt"}, "c-grep"),
            make_call("find", {"path": f"{prefix}", "pattern": "**/*.txt"}, "c-find"),
            make_call("ls", {"path": f"{prefix}"}, "c-ls"),
        ]
        declared = {name: tools[name].definition() for name in {"read", "grep", "find", "ls"}}
        result = batch(calls, tools=tools, declared=declared, execution_mode="parallel")
        assert [message.tool_call_id for message in result.messages] == [
            "c-read", "c-grep", "c-find", "c-ls",
        ]
        assert all(not message.is_error for message in result.messages)
        assert "hello" in text(result.messages[0])
        assert "note.txt" in text(result.messages[1])
        assert f"{prefix}/note.txt" in text(result.messages[2])


def single(message: AssistantMessage):
    async def events():
        yield {
            "type": "start",
            "partial": message.model_copy(update={"stop_reason": "pending", "usage": None}),
        }
        yield {"type": "done", "reason": message.stop_reason, "message": message}

    return AssistantResponse(events())


def usage() -> Usage:
    return Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2)


def check_credential_stops_scheduling() -> None:
    async def scenario() -> dict:
        database = await open_database(EVIDENCE / f"credential-{uuid4().hex}.db")
        try:
            service = SessionService(SqliteSessionRepository(database))
            session_id = str(uuid4())
            await service.create_session(session_id, "凭据停止调度")
            with TemporaryDirectory(dir=WORKSPACE) as directory:
                root = Path(directory)
                executed: list[str] = []

                def execute(tool_call_id, params: ProbeArguments, signal, on_update):
                    executed.append(params.name)
                    (root / f"{params.name}.txt").write_text("SECRET", encoding="utf-8")
                    return AgentToolResult([TextContent(type="text", text="SECRET")])

                tools = {
                    "leak": AgentTool("leak", "leak", ProbeArguments, execute, execution_mode="sequential"),
                    "after": AgentTool("after", "after", ProbeArguments, execute, execution_mode="sequential"),
                }
                system = SystemMessage(
                    role="system",
                    content="",
                    tools_added=[tool.definition() for tool in tools.values()],
                    timestamp=0,
                )
                send = await service.accept_send(
                    SendCommand(
                        operation_id=str(uuid4()),
                        session_id=session_id,
                        request=SendRequest(text="执行"),
                    ),
                    system_message=system,
                )
                position = {"id": send.run.request_entry_id}

                async def save_message(node_id, message):
                    outcome = await service.append_entry(
                        SessionMessageEntry(
                            session_id=session_id,
                            id=node_id,
                            parent_id=position["id"],
                            run_id=send.run.id,
                            type="message",
                            messages=[message],
                            created_at=time_ns() // 1_000_000,
                        )
                    )
                    assert not outcome.credential_detected
                    position["id"] = node_id

                guard = CredentialFilter(("SECRET",))

                async def emit(event):
                    if event["type"] == "tool_execution_end" and guard.contains(event["content"]):
                        raise CredentialDetectedError()

                model = SimpleNamespace(
                    MODEL_API="openai-completions",
                    OPENAI_PROVIDER="openai",
                    OPENAI_MODEL="test-model",
                    OPENAI_BASE_URL="http://unused",
                    OPENAI_API_KEY="unused",
                )
                tool_use = AssistantMessage(
                    role="assistant",
                    content=[
                        ToolCall(type="toolCall", id="leak", name="leak", arguments={"name": "leak"}),
                        ToolCall(type="toolCall", id="after", name="after", arguments={"name": "after"}),
                    ],
                    api=model.MODEL_API,
                    provider=model.OPENAI_PROVIDER,
                    model=model.OPENAI_MODEL,
                    usage=usage(),
                    stop_reason="toolUse",
                    timestamp=0,
                )

                def stream_fn(model_spec, context, options):
                    return single(tool_use)

                config = AgentLoopConfig(model=model, max_turns=4, save_message=save_message)
                failure = None
                try:
                    await run_agent_loop(
                        [UserMessage(role="user", content="执行", timestamp=0)],
                        {"messages": [system], "tools": tools},
                        config,
                        emit,
                        stream_fn=stream_fn,
                    )
                except BaseException as error:
                    failure = error
                branch = await service.get_current_branch(session_id)
                return {
                    "failure": type(failure).__name__,
                    "executed": executed,
                    "leak_exists": (root / "leak.txt").exists(),
                    "after_exists": (root / "after.txt").exists(),
                    "tool_results": [
                        entry.messages[0].tool_call_id
                        for entry in branch
                        if isinstance(entry.messages[0], ToolResultMessage)
                    ],
                }
        finally:
            await database.close()

    evidence = asyncio.run(scenario())
    assert evidence["failure"] == "CredentialDetectedError", evidence
    assert evidence["executed"] == ["leak"], evidence
    assert evidence["leak_exists"] is True, evidence
    assert evidence["after_exists"] is False, evidence
    assert evidence["tool_results"] == [], evidence


def check_database_ordering() -> None:
    async def scenario() -> dict:
        database = await open_database(EVIDENCE / f"order-{uuid4().hex}.db")
        try:
            service = SessionService(SqliteSessionRepository(database))
            session_id = str(uuid4())
            await service.create_session(session_id, "调度排序")
            with TemporaryDirectory(dir=WORKSPACE) as directory:
                root = Path(directory)
                fast_finalized = Event()

                def probe_execute(tool_call_id, params: ProbeArguments, signal, on_update):
                    if params.name == "slow":
                        assert fast_finalized.wait(timeout=5), "fast 未先定稿"
                    (root / f"{params.name}.txt").write_text(params.name, encoding="utf-8")
                    return AgentToolResult([TextContent(type="text", text=params.name)])

                probe = AgentTool(
                    "probe", "probe", ProbeArguments, probe_execute, execution_mode="parallel"
                )
                tools = {**create_file_tools(), "probe": probe}
                system = SystemMessage(
                    role="system",
                    content="",
                    tools_added=[tool.definition() for tool in tools.values()],
                    timestamp=0,
                )
                send = await service.accept_send(
                    SendCommand(
                        operation_id=str(uuid4()),
                        session_id=session_id,
                        request=SendRequest(text="按顺序执行 probe"),
                    ),
                    system_message=system,
                )
                position = {"id": send.run.request_entry_id}
                trace: list[tuple] = []
                completions: list[str] = []
                result_events: list[str] = []

                async def save_message(node_id, message):
                    parent_id = position["id"]
                    outcome = await service.append_entry(
                        SessionMessageEntry(
                            session_id=session_id,
                            id=node_id,
                            parent_id=parent_id,
                            run_id=send.run.id,
                            type="message",
                            messages=[message],
                            created_at=time_ns() // 1_000_000,
                        )
                    )
                    assert not outcome.credential_detected
                    position["id"] = node_id
                    trace.append(("save", node_id))

                async def emit(event):
                    trace.append(
                        ("emit", event["type"], event.get("message_id") or event.get("tool_call_id"))
                    )
                    if event["type"] == "tool_execution_end":
                        completions.append(event["tool_call_id"])
                        if event["tool_call_id"] == "fast":
                            fast_finalized.set()
                    elif event["type"] == "tool_result":
                        result_events.append(event["tool_call_id"])

                model = SimpleNamespace(
                    MODEL_API="openai-completions",
                    OPENAI_PROVIDER="openai",
                    OPENAI_MODEL="test-model",
                    OPENAI_BASE_URL="http://unused",
                    OPENAI_API_KEY="unused",
                )
                tool_use = AssistantMessage(
                    role="assistant",
                    content=[
                        ToolCall(type="toolCall", id="slow", name="probe", arguments={"name": "slow"}),
                        ToolCall(type="toolCall", id="fast", name="probe", arguments={"name": "fast"}),
                    ],
                    api=model.MODEL_API,
                    provider=model.OPENAI_PROVIDER,
                    model=model.OPENAI_MODEL,
                    usage=usage(),
                    stop_reason="toolUse",
                    timestamp=0,
                )
                stop = AssistantMessage(
                    role="assistant",
                    content=[TextContent(type="text", text="完成")],
                    api=model.MODEL_API,
                    provider=model.OPENAI_PROVIDER,
                    model=model.OPENAI_MODEL,
                    usage=usage(),
                    stop_reason="stop",
                    timestamp=0,
                )
                count = {"value": 0}

                def stream_fn(model_spec, context, options):
                    count["value"] += 1
                    return single(tool_use if count["value"] == 1 else stop)

                config = AgentLoopConfig(model=model, max_turns=4, save_message=save_message)
                messages = await run_agent_loop(
                    [UserMessage(role="user", content="执行 probe", timestamp=0)],
                    {"messages": [system], "tools": tools},
                    config,
                    emit,
                    stream_fn=stream_fn,
                )
                branch = await service.get_current_branch(session_id)
                stored = [entry.messages[0] for entry in branch]
                results = [message for message in stored if isinstance(message, ToolResultMessage)]
                assert [message.tool_call_id for message in results] == ["slow", "fast"]
                returned = [message for message in messages if isinstance(message, ToolResultMessage)]
                assert [message.tool_call_id for message in returned] == ["slow", "fast"]
                assert completions == ["fast", "slow"], completions
                assert completions.count("fast") == 1 and completions.count("slow") == 1
                assistant_entry = next(
                    entry for entry in branch
                    if isinstance(entry.messages[0], AssistantMessage)
                    and entry.messages[0].stop_reason == "toolUse"
                )
                result_entries = [
                    entry for entry in branch if isinstance(entry.messages[0], ToolResultMessage)
                ]
                assert result_entries[0].parent_id == assistant_entry.id
                assert result_entries[1].parent_id == result_entries[0].id
                assert (root / "slow.txt").read_text(encoding="utf-8") == "slow"
                assert (root / "fast.txt").read_text(encoding="utf-8") == "fast"
                save_positions = {
                    entry[1]: index for index, entry in enumerate(trace) if entry[0] == "save"
                }
                for index, entry in enumerate(trace):
                    if entry[0] == "emit" and entry[1] == "tool_result":
                        assert entry[2] in save_positions and save_positions[entry[2]] < index
                assert result_events == ["slow", "fast"], result_events
                return {"completions": completions, "stored": [m.tool_call_id for m in results]}
        finally:
            await database.close()

    evidence = asyncio.run(scenario())
    (EVIDENCE / "tool-scheduling.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def check() -> None:
    check_prepare_before_execute()
    check_parallel_overlap()
    check_global_sequential()
    check_mixed_batch_sequential()
    check_completion_order()
    check_failure_indices()
    check_hook_isolation()
    check_parallel_cancel_cleanup()
    check_event_failure_cleanup()
    check_failure_preserves_results()
    check_failure_signals_in_flight()
    check_credential_stops_sequential_scheduling()
    check_task_cancel_cleanup()
    check_real_file_tools()
    check_credential_stops_scheduling()
    check_database_ordering()
    print(
        "PASS: 三阶段调度（顺序准备、并行重叠、顺序模式、完成顺序与调用顺序分离、"
        "失败索引、hook 隔离、取消/凭据/回调失败的结果保留与收尾、真实文件工具与数据库排序）"
    )


if __name__ == "__main__":
    check()
