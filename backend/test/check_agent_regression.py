import asyncio
import json
import sqlite3
import time
from concurrent.futures import CancelledError
from threading import Event, Thread
from time import time_ns
from uuid import uuid4

import aiosqlite
import pytest

from app.agent.agent_loop import run_agent_loop
from app.agent.config import AgentLoopConfig
from app.agent.tools.bash import create_bash_tool
from app.ai.messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from app.application.session.service import (
    CREDENTIAL_SAFE_MESSAGE,
    SessionService,
)
from app.domain.session.models import (
    SendCommand,
    SendRequest,
    SessionMessageEntry,
    SessionRun,
)
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from app.interfaces.http import (
    CredentialDetectedError,
    CredentialFilter,
    RunState,
    public_content,
    publish_terminal,
    terminal_decision,
)
from app.model_config import load_model_config
from test.regression_support import temporary_root

EVIDENCE = temporary_root("agent-regression")
ABORT_TRIGGER = "regress_abort_tool_result"


class ConsumeFailure(Exception):
    pass


def system_message(tools) -> SystemMessage:
    return SystemMessage(
        role="system",
        content="执行用户指定的工具操作，工具参数遵循声明。",
        tools_added=[tool.definition() for tool in tools.values()],
        timestamp=0,
    )


async def append(service, session_id, run_id, node_id, parent_id, message):
    outcome = await service.append_entry(
        SessionMessageEntry(
            session_id=session_id,
            id=node_id,
            parent_id=parent_id,
            run_id=run_id,
            type="message",
            messages=[message],
            created_at=time_ns() // 1_000_000,
        )
    )
    assert not outcome.credential_detected
    return outcome


def check_tool_cancel(config, database_path):
    async def scenario():
        database = await open_database(database_path)
        try:
            service = SessionService(SqliteSessionRepository(database))
            session_id = str(uuid4())
            await service.create_session(session_id, "取消注入")
            tools = {"bash": create_bash_tool(session_id)}
            prompt = "必须调用 bash，command 精确为 sleep 3，timeout 为 10；完成后报告。"
            send = await service.accept_send(
                SendCommand(
                    operation_id=str(uuid4()),
                    session_id=session_id,
                    request=SendRequest(text=prompt),
                ),
                system_message=system_message(tools),
            )
            cancel = Event()
            started = Event()
            trace = []
            position = {"id": send.run.request_entry_id}

            async def save_message(node_id, message):
                await append(
                    service,
                    session_id,
                    send.run.id,
                    node_id,
                    position["id"],
                    message,
                )
                trace.append(("save", node_id))
                position["id"] = node_id

            async def emit(event):
                key = event.get("message_id") or event.get("tool_call_id")
                trace.append(("emit", event["type"], key))
                if event["type"] == "tool_start":
                    started.set()

            async def steer():
                return []

            config_loop = AgentLoopConfig(
                model=config,
                max_turns=4,
                get_steering_messages=steer,
                save_message=save_message,
            )

            def watcher():
                started.wait(60)
                time.sleep(0.5)
                for _ in range(3):
                    cancel.set()

            thread = Thread(target=watcher)
            thread.start()
            failure = None
            try:
                await run_agent_loop(
                    [UserMessage(role="user", content=prompt, timestamp=0)],
                    {"messages": [system_message(tools)], "tools": tools},
                    config_loop,
                    emit,
                    cancel,
                )
            except BaseException as error:
                failure = error
            thread.join()

            kinds = [entry[1] for entry in trace if entry[0] == "emit"]
            assert isinstance(failure, CancelledError), failure
            assert cancel.is_set()
            assert kinds.count("tool_result") == 1
            assert kinds[-1] == "trace_end"
            saved_at = {
                entry[1]: index
                for index, entry in enumerate(trace)
                if entry[0] == "save"
            }
            delivered = [
                (index, entry)
                for index, entry in enumerate(trace)
                if entry[0] == "emit" and entry[1] == "tool_result"
            ]
            assert delivered and saved_at[delivered[0][1][2]] < delivered[0][0]
            entries = await service.list_entries(session_id)
            assert any(
                isinstance(entry.messages[0], ToolResultMessage) for entry in entries
            )
            await service.finish_run(session_id, send.run.id, "cancelled")
            run = await service.get_run(session_id, send.run.id)
            assert run.status == "cancelled" and run.finished_at is not None
            return {
                "kinds": kinds,
                "cancels": 3,
                "run_status": run.status,
                "entries": len(entries),
            }
        finally:
            await database.close()

    return asyncio.run(scenario())


def check_commit_failure(config, database_path):
    async def scenario():
        database = await open_database(database_path)
        try:
            service = SessionService(SqliteSessionRepository(database))
            session_id = str(uuid4())
            await service.create_session(session_id, "提交异常")
            tools = {"bash": create_bash_tool(session_id)}
            prompt = "必须调用 bash，command 精确为 echo ok，timeout 为 5；完成后报告。"
            send = await service.accept_send(
                SendCommand(
                    operation_id=str(uuid4()),
                    session_id=session_id,
                    request=SendRequest(text=prompt),
                ),
                system_message=system_message(tools),
            )
            async with aiosqlite.connect(database_path) as raw:
                await raw.execute(
                    f"CREATE TRIGGER {ABORT_TRIGGER} BEFORE INSERT ON session_entries "
                    "WHEN NEW.messages LIKE '%toolResult%' "
                    "BEGIN SELECT RAISE(ABORT, '注入的提交失败'); END"
                )
                await raw.commit()
            trace = []
            position = {"id": send.run.request_entry_id}

            async def save_message(node_id, message):
                await append(
                    service,
                    session_id,
                    send.run.id,
                    node_id,
                    position["id"],
                    message,
                )
                trace.append(("save", node_id))
                position["id"] = node_id

            async def emit(event):
                trace.append(("emit", event["type"]))

            async def steer():
                return []

            config_loop = AgentLoopConfig(
                model=config,
                max_turns=4,
                get_steering_messages=steer,
                save_message=save_message,
            )
            failure = None
            try:
                await run_agent_loop(
                    [UserMessage(role="user", content=prompt, timestamp=0)],
                    {"messages": [system_message(tools)], "tools": tools},
                    config_loop,
                    emit,
                    None,
                )
            except BaseException as error:
                failure = error

            kinds = [entry[1] for entry in trace if entry[0] == "emit"]
            assert isinstance(failure, sqlite3.IntegrityError), failure
            assert "message_end" in kinds
            assert "tool_result" not in kinds
            assert kinds[-1] == "trace_end"
            assert not database.connection.in_transaction
            entries = await service.list_entries(session_id)
            assert any(
                isinstance(entry.messages[0], AssistantMessage) for entry in entries
            )
            assert not any(
                isinstance(entry.messages[0], ToolResultMessage) for entry in entries
            )
            run = await service.get_run(session_id, send.run.id)
            assert run.status == "running" and run.last_entry_id is not None

            async with aiosqlite.connect(database_path) as raw:
                await raw.execute(f"DROP TRIGGER {ABORT_TRIGGER}")
                await raw.commit()
            recovered = await append(
                service,
                session_id,
                send.run.id,
                str(uuid4()),
                position["id"],
                ToolResultMessage(
                    role="toolResult",
                    tool_call_id="regress",
                    tool_name="bash",
                    content=[TextContent(type="text", text="ok")],
                    is_error=False,
                    timestamp=0,
                ),
            )
            assert recovered.created is True
            return {
                "kinds": kinds,
                "failure": type(failure).__name__,
                "entries_after_failure": len(entries),
                "recovered": recovered.created,
            }
        finally:
            await database.close()

    return asyncio.run(scenario())


def check_repeated_cancel(database_path):
    async def scenario():
        database = await open_database(database_path)
        try:
            async def repeated():
                async with database.transaction_scope():
                    task = asyncio.current_task()
                    task.cancel()
                    task.cancel()
                    await asyncio.sleep(0)

            with pytest.raises(asyncio.CancelledError):
                await asyncio.create_task(repeated())
            assert not database.connection.in_transaction

            async def ping(connection):
                cursor = await connection.execute("SELECT 1")
                row = await cursor.fetchone()
                await cursor.close()
                return row[0]

            assert await database.read(ping) == 1
            return {"rollback": True}
        finally:
            await database.close()

    return asyncio.run(scenario())


def check_credential_shards(config):
    key = config.OPENAI_API_KEY
    guard = CredentialFilter((key,))
    assert guard.contains(key)
    assert guard.contains({"outer": [{"inner": key}], key: "value"})
    assert guard.contains(f"前缀 {key} 后缀")
    assert not guard.contains(key[:-1])
    assert not guard.contains("普通内容")

    shards = [key[index : index + 3] for index in range(0, len(key), 3)]
    accumulated = ""
    hits = []
    for shard in shards:
        accumulated += shard
        assert key not in guard.tail_safe(accumulated)
        if guard.contains(accumulated):
            hits.append(len(accumulated))
    assert hits == [len(key)]

    snapshots = []
    for index in range(len(key) + 1):
        prefix = key[:index]
        snapshot = guard.tail_safe(prefix)
        assert key not in snapshot
        snapshots.append(snapshot)
        if guard.contains(prefix):
            assert index == len(key)
    assert any(snapshots)

    message = AssistantMessage(
        role="assistant",
        content=[TextContent(type="text", text=f"结尾{key}")],
        api=config.MODEL_API,
        provider=config.OPENAI_PROVIDER,
        model=config.OPENAI_MODEL,
        usage=Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2),
        stop_reason="stop",
        timestamp=0,
    )
    streamed = public_content(message, guard, True)
    assert key not in streamed[0]["text"]
    assert text_of(streamed) != f"结尾{key}"
    committed = public_content(message, guard, False)
    assert text_of(committed) == f"结尾{key}"
    assert not guard.contains({"session_id": str(uuid4()), "title": "普通标题"})
    return {
        "shards": len(shards),
        "blocked_at": hits[0],
        "key_length": len(key),
        "streamed_chars": len(text_of(streamed)),
        "committed_chars": len(text_of(committed)),
    }


def check_future_already_finished():
    from concurrent.futures import Future

    async def scenario():
        loop = asyncio.get_running_loop()
        ticks: list[str] = []
        future = Future()
        future.set_result("完成")
        loop.call_soon(ticks.append, "tick")
        # 已完成的 future 在注册等待前已经结束，主事件循环仍可响应。
        result = await asyncio.wrap_future(future)
        assert result == "完成"
        await asyncio.sleep(0)
        assert ticks == ["tick"]
        return {"result": result, "responsive": True}

    return asyncio.run(scenario())


def check_terminal_consistency():
    async def scenario():
        cancelled = RunState(uuid4())
        run = SessionRun(
            session_id=str(uuid4()),
            id=str(uuid4()),
            request_entry_id=str(uuid4()),
            last_entry_id=None,
            status="cancelled",
            started_at=0,
            finished_at=1,
            error_code="cancelled",
            error_message="执行已取消。",
        )
        publish_terminal(cancelled, run, None)
        event = cancelled.events.get_nowait()
        assert event.event == "error" and event.data["status"] == "cancelled"

        completed = RunState(uuid4())
        run = run.model_copy(
            update={
                "status": "completed",
                "error_code": None,
                "error_message": None,
            }
        )
        publish_terminal(completed, run, "stop")
        event = completed.events.get_nowait()
        assert event.event == "done" and event.data["stop_reason"] == "stop"

        failed = RunState(uuid4())
        run = run.model_copy(
            update={
                "status": "failed",
                "error_code": "credential_detected",
                "error_message": CREDENTIAL_SAFE_MESSAGE,
            }
        )
        publish_terminal(failed, run, None)
        event = failed.events.get_nowait()
        assert event.event == "error"
        assert event.data["status"] == "failed"
        assert event.data["code"] == "credential_detected"
        assert event.data["message"] == CREDENTIAL_SAFE_MESSAGE
        return {"cancelled": "error", "completed": "done", "failed": "error"}

    return asyncio.run(scenario())


def text_of(content):
    return "".join(block.get("text", "") for block in content)


def check_terminal_priority():
    # 凭据命中与取消交错时安全失败优先；无失败时按取消/完成裁决。
    assert terminal_decision(CredentialDetectedError(), True) == (
        "failed",
        "credential_detected",
        CREDENTIAL_SAFE_MESSAGE,
    )
    assert terminal_decision(CredentialDetectedError(), False) == (
        "failed",
        "credential_detected",
        CREDENTIAL_SAFE_MESSAGE,
    )
    assert terminal_decision(None, True) == ("cancelled", "cancelled", "执行已取消。")
    status, code, message = terminal_decision(RuntimeError("boom"), False)
    assert status == "failed" and code == "execution_failed" and message
    assert terminal_decision(None, False) == ("completed", None, None)
    return {"credential_over_cancel": True}


def check_steering_consume_failure(config):
    async def scenario():
        emitted: list[str] = []

        async def emit(event):
            emitted.append(event["type"])

        async def steer():
            return [UserMessage(role="user", content="消费失败输入", timestamp=0)]

        async def consumed(messages):
            raise ConsumeFailure()

        loop_config = AgentLoopConfig(
            model=config,
            max_turns=3,
            get_steering_messages=steer,
            on_steering_consumed=consumed,
        )
        failure = None
        try:
            await run_agent_loop(
                [UserMessage(role="user", content="开始", timestamp=0)],
                {"messages": [], "tools": {}},
                loop_config,
                emit,
            )
        except BaseException as error:
            failure = error
        # 消费提交失败：不得发送 consumed，也不得发起包含该输入的模型请求。
        assert isinstance(failure, ConsumeFailure), failure
        assert not any(kind.startswith("message_") for kind in emitted)
        assert emitted[-1] == "trace_end"
        return {"emitted": emitted}

    return asyncio.run(scenario())


def check():
    config = load_model_config()
    assert config.MODEL_API == "openai-completions"
    evidence = {}
    evidence["tool_cancel"] = check_tool_cancel(
        config, EVIDENCE / f"cancel-{uuid4().hex}.db"
    )
    evidence["commit_failure"] = check_commit_failure(
        config, EVIDENCE / f"commit-{uuid4().hex}.db"
    )
    evidence["repeated_cancel"] = check_repeated_cancel(
        EVIDENCE / f"repeat-{uuid4().hex}.db"
    )
    evidence["credential_shards"] = check_credential_shards(config)
    evidence["terminal_priority"] = check_terminal_priority()
    evidence["terminal_consistency"] = check_terminal_consistency()
    evidence["future_already_finished"] = check_future_already_finished()
    evidence["steering_consume_failure"] = check_steering_consume_failure(config)
    (EVIDENCE / "agent-regression.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "PASS: 工具期间取消注入、重复取消、数据库提交异常中断、跨分片凭据拦截、"
        "凭据优先裁决、终态一致、已完成 future 收尾与消费失败不发起模型请求"
    )


if __name__ == "__main__":
    check()
