import asyncio
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from app.ai.messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
)
from app.application.session.service import CREDENTIAL_SAFE_MESSAGE, SessionService
from app.domain.session.errors import IncompleteToolChain
from app.domain.session.models import (
    SendCommand,
    SendRequest,
    SessionMessageEntry,
    SteeringCommand,
    SteeringRequest,
)
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from app.interfaces.http import app
from app.model_config import load_model_config
from test.regression_support import (
    Server,
    client,
    patch_default_database,
    temporary_root,
)

EVIDENCE = temporary_root("session-history")

USAGE = Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2)
SYSTEM_MESSAGE = SystemMessage(
    role="system",
    content="系统提示词，仅后端可见。",
    tools_added=[],
    timestamp=100,
)


def assistant(content, stop_reason: str, timestamp: int) -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=content,
        api="openai-completions",
        provider="example",
        model="model-1",
        usage=USAGE,
        stop_reason=stop_reason,
        timestamp=timestamp,
    )


def text(value: str) -> TextContent:
    return TextContent(type="text", text=value, text_signature="text-sig")


def entry(session_id, entry_id, parent_id, run_id, message, created_at):
    return SessionMessageEntry(
        session_id=session_id,
        id=entry_id,
        parent_id=parent_id,
        run_id=run_id,
        type="message",
        messages=[message],
        created_at=created_at,
    )


async def open_service(path: Path):
    database = await open_database(path)
    return database, SessionService(SqliteSessionRepository(database))


async def append(service, session_id, run_id, node_id, parent_id, message, created_at):
    outcome = await service.append_entry(
        entry(session_id, node_id, parent_id, run_id, message, created_at)
    )
    assert outcome.created and not outcome.credential_detected
    return outcome.entry


def assert_consistent(history) -> None:
    entries = history.entries
    ids = [item.id for item in entries]
    assert len(ids) == len(set(ids))
    if entries:
        assert entries[0].parent_id is None
        for previous, current in zip(entries, entries[1:]):
            assert current.parent_id == previous.id
        assert entries[-1].id == history.session.active_leaf_id
    else:
        assert history.session.active_leaf_id is None
    run_ids = [run.id for run in history.runs]
    assert len(run_ids) == len(set(run_ids))
    assert [run.started_at for run in history.runs] == sorted(
        run.started_at for run in history.runs
    )
    for run in history.runs:
        assert run.request_entry_id in ids
        if run.last_entry_id is not None:
            assert run.last_entry_id in ids
    steering_ids = [item.id for item in history.steering]
    assert len(steering_ids) == len(set(steering_ids))
    assert [item.created_at for item in history.steering] == sorted(
        item.created_at for item in history.steering
    )
    for item in history.steering:
        assert item.run_id in run_ids
        if item.status == "consumed":
            assert item.entry_id in ids
        else:
            assert item.entry_id is None


async def scenario_empty(service: SessionService) -> dict:
    session_id = "hist-empty"
    await service.create_session(session_id, "空会话")
    history = await service.get_session_history(session_id)
    assert history.session.id == session_id
    assert history.session.active_leaf_id is None
    assert history.entries == [] and history.runs == [] and history.steering == []
    assert_consistent(history)
    return {"entries": 0, "runs": 0, "steering": 0}


async def scenario_full_branch(service: SessionService) -> dict:
    session_id = "hist-full"
    await service.create_session(session_id, "完整历史")
    send = await service.accept_send(
        SendCommand(
            operation_id=str(uuid4()),
            session_id=session_id,
            request=SendRequest(text="问题一"),
        ),
        system_message=SYSTEM_MESSAGE,
    )
    run = send.run
    user_id = run.request_entry_id
    system_id = (await service.get_entry(session_id, user_id)).parent_id

    tool_call = assistant(
        [
            ThinkingContent(
                type="thinking",
                thinking="内部计划",
                thinking_signature="thinking-sig",
                redacted=True,
            ),
            text("调用工具"),
            ToolCall(
                type="toolCall",
                id="call-1",
                name="lookup",
                arguments={"q": 1},
                thought_signature="thought-sig",
                namespace="tools",
            ),
        ],
        "toolUse",
        200,
    )
    await append(service, session_id, run.id, "a-tool", user_id, tool_call, 210)

    tool_result = ToolResultMessage(
        role="toolResult",
        tool_call_id="call-1",
        tool_name="lookup",
        content=[text("结果正文")],
        is_error=False,
        timestamp=220,
        details={"hidden": "内部诊断"},
        nested_calls={
            "calls": [{"id": "nested-1", "name": "fetch", "status": "ok"}],
            "complete": True,
        },
    )
    await append(service, session_id, run.id, "t-1", "a-tool", tool_result, 230)

    accepted = await service.accept_steering(
        SteeringCommand(
            operation_id=str(uuid4()),
            session_id=session_id,
            request=SteeringRequest(target_run_id=run.id, text="补充输入"),
        )
    )
    steering = accepted.steering
    consumed = await service.consume_steering(session_id, run.id, steering.id)
    assert consumed.created and consumed.entry is not None
    steering_id = consumed.entry.id

    final = assistant([text("最终回答")], "stop", 300)
    await append(service, session_id, run.id, "a-final", steering_id, final, 310)
    await service.finish_run(session_id, run.id, "completed")

    history = await service.get_session_history(session_id)
    assert [item.id for item in history.entries] == [
        system_id,
        user_id,
        "a-tool",
        "t-1",
        steering_id,
        "a-final",
    ]
    assert history.session.active_leaf_id == "a-final"
    assert_consistent(history)
    assert [item.id for item in history.runs] == [run.id]
    stored_run = history.runs[0]
    assert stored_run.request_entry_id == user_id
    assert stored_run.last_entry_id == "a-final"
    assert stored_run.status == "completed"
    assert stored_run.finished_at is not None
    assert stored_run.error_code is None and stored_run.error_message is None
    assert [item.id for item in history.steering] == [steering.id]
    stored_steering = history.steering[0]
    assert stored_steering.status == "consumed"
    assert stored_steering.entry_id == steering_id
    assert stored_steering.reason is None
    assert stored_steering.message.content == "补充输入"
    assert (
        stored_steering.message.timestamp
        == (await service.get_entry(session_id, steering_id)).messages[0].timestamp
    )
    return {
        "entry_ids": [item.id for item in history.entries],
        "run_id": run.id,
        "steering_id": steering.id,
        "consumed_entry_id": steering_id,
        "root": system_id,
        "user": user_id,
    }


async def scenario_failed_without_assistant(service: SessionService) -> dict:
    session_id = "hist-full"
    send = await service.accept_send(
        SendCommand(
            operation_id=str(uuid4()),
            session_id=session_id,
            request=SendRequest(text="问题二"),
        ),
        system_message=SYSTEM_MESSAGE,
    )
    run = send.run
    await service.finish_run(
        session_id,
        run.id,
        "failed",
        error_code="execution_failed",
        error_message="执行失败，请重新发起请求。",
    )
    history = await service.get_session_history(session_id)
    assert_consistent(history)
    stored = next(item for item in history.runs if item.id == run.id)
    assert stored.status == "failed"
    assert stored.last_entry_id is None
    assert stored.finished_at is not None
    assert stored.error_code == "execution_failed"
    assert stored.error_message == "执行失败，请重新发起请求。"
    assert history.session.active_leaf_id == run.request_entry_id
    return {
        "run_id": run.id,
        "status": stored.status,
        "last_entry_id": stored.last_entry_id,
    }


async def scenario_unpaired(service: SessionService) -> dict:
    session_id = "hist-unpaired"
    await service.create_session(session_id, "未配对工具链")
    send = await service.accept_send(
        SendCommand(
            operation_id=str(uuid4()),
            session_id=session_id,
            request=SendRequest(text="调用工具"),
        ),
        system_message=SYSTEM_MESSAGE,
    )
    run = send.run
    await append(
        service,
        session_id,
        run.id,
        "up-a1",
        run.request_entry_id,
        assistant(
            [
                text("需要工具"),
                ToolCall(type="toolCall", id="call-x", name="lookup", arguments={}),
            ],
            "toolUse",
            400,
        ),
        410,
    )
    await service.finish_run(session_id, run.id, "cancelled")

    history = await service.get_session_history(session_id)
    assert [item.id for item in history.entries][-1] == "up-a1"
    assert history.runs[0].status == "cancelled"
    assert history.runs[0].last_entry_id == "up-a1"

    before = (
        len(history.entries),
        len(history.runs),
        history.session.active_leaf_id,
    )
    blocked_op = str(uuid4())
    try:
        await service.accept_send(
            SendCommand(
                operation_id=blocked_op,
                session_id=session_id,
                request=SendRequest(text="继续"),
            ),
            system_message=SYSTEM_MESSAGE,
        )
    except IncompleteToolChain:
        pass
    else:
        raise AssertionError("未配对工具链未拒绝续聊")

    after = await service.get_session_history(session_id)
    assert (
        len(after.entries),
        len(after.runs),
        after.session.active_leaf_id,
    ) == before
    assert await service.get_operation(blocked_op) is None
    assert_consistent(after)
    return {"blocked": True, "entries": len(after.entries)}


async def scenario_interrupted_recovery(root: Path) -> dict:
    path = root / "interrupted.db"
    database, service = await open_service(path)
    session_id = "hist-interrupted"
    await service.create_session(session_id, "中断恢复")
    send = await service.accept_send(
        SendCommand(
            operation_id=str(uuid4()),
            session_id=session_id,
            request=SendRequest(text="未完成"),
        ),
        system_message=SYSTEM_MESSAGE,
    )
    run = send.run
    accepted = await service.accept_steering(
        SteeringCommand(
            operation_id=str(uuid4()),
            session_id=session_id,
            request=SteeringRequest(target_run_id=run.id, text="遗留输入"),
        )
    )
    pending_id = accepted.steering.id
    await database.close()

    reopened, recovered = await open_service(path)
    try:
        await recovered.recover_interrupted()
        history = await recovered.get_session_history(session_id)
        assert_consistent(history)
        stored = next(item for item in history.runs if item.id == run.id)
        assert stored.status == "interrupted"
        assert stored.finished_at is None
        input_record = next(
            item for item in history.steering if item.id == pending_id
        )
        assert input_record.status == "discarded"
        assert input_record.reason == "interrupted"
        assert input_record.entry_id is None
        return {
            "run_status": stored.status,
            "finished_at": stored.finished_at,
            "steering_status": input_record.status,
            "reason": input_record.reason,
        }
    finally:
        await reopened.close()


async def scenario_concurrency(service: SessionService) -> dict:
    session_id = "hist-conc"
    await service.create_session(session_id, "并发快照")
    first = await service.accept_send(
        SendCommand(
            operation_id=str(uuid4()),
            session_id=session_id,
            request=SendRequest(text="起点"),
        ),
        system_message=SYSTEM_MESSAGE,
    )
    await append(
        service,
        session_id,
        first.run.id,
        "c-0",
        first.run.request_entry_id,
        assistant([text("回答 0")], "stop", 500),
        510,
    )
    await service.finish_run(session_id, first.run.id, "completed")

    async def writer() -> None:
        for index in range(3):
            send = await service.accept_send(
                SendCommand(
                    operation_id=str(uuid4()),
                    session_id=session_id,
                    request=SendRequest(text=f"问题 {index}"),
                ),
                system_message=SYSTEM_MESSAGE,
            )
            await append(
                service,
                session_id,
                send.run.id,
                f"c-{index + 1}",
                send.run.request_entry_id,
                assistant([text(f"回答 {index + 1}")], "stop", 600 + index),
                610 + index,
            )
            await service.finish_run(session_id, send.run.id, "completed")

    async def reader() -> int:
        for _ in range(20):
            history = await service.get_session_history(session_id)
            assert_consistent(history)
        return 1

    results = await asyncio.gather(writer(), reader(), reader())
    assert results[1:] == [1, 1]
    final = await service.get_session_history(session_id)
    assert_consistent(final)
    assert len(final.entries) == 9 and len(final.runs) == 4
    return {"entries": len(final.entries), "runs": len(final.runs)}


async def service_checks(root: Path) -> dict:
    database, service = await open_service(root / "main.db")
    try:
        evidence = {
            "empty": await scenario_empty(service),
            "full_branch": await scenario_full_branch(service),
            "failed_without_assistant": await scenario_failed_without_assistant(service),
            "unpaired": await scenario_unpaired(service),
            "concurrency": await scenario_concurrency(service),
        }
    finally:
        await database.close()
    evidence["interrupted"] = await scenario_interrupted_recovery(root)
    return evidence


def raw_insert_session(path: Path, session_id: str, title: str, updated_at: int) -> None:
    raw = sqlite3.connect(path, timeout=30)
    try:
        raw.execute(
            "INSERT INTO sessions (id, title, active_leaf_id, created_at, updated_at) "
            "VALUES (?, ?, NULL, ?, ?)",
            (session_id, title, updated_at, updated_at),
        )
        raw.commit()
    finally:
        raw.close()


def raw_insert_entry(
    raw, session_id: str, entry_id: str, parent_id: str | None, message, created_at: int
) -> None:
    raw.execute(
        "INSERT INTO session_entries "
        "(session_id, id, parent_id, run_id, type, messages, created_at) "
        "VALUES (?, ?, ?, NULL, 'message', ?, ?)",
        (
            session_id,
            entry_id,
            parent_id,
            json.dumps([message], ensure_ascii=False),
            created_at,
        ),
    )


async def seed_http_history(path: Path) -> dict:
    database, service = await open_service(path)
    try:
        session_id = str(uuid4())
        await service.create_session(session_id, "HTTP 历史")
        send = await service.accept_send(
            SendCommand(
                operation_id=str(uuid4()),
                session_id=session_id,
                request=SendRequest(text="问题一"),
            ),
            system_message=SYSTEM_MESSAGE,
        )
        run = send.run
        user_id = run.request_entry_id
        system_id = (await service.get_entry(session_id, user_id)).parent_id
        await append(
            service,
            session_id,
            run.id,
            "h-a-tool",
            user_id,
            assistant(
                [
                    ThinkingContent(
                        type="thinking",
                        thinking="内部计划",
                        thinking_signature="thinking-sig",
                        redacted=True,
                    ),
                    text("调用工具"),
                    ToolCall(
                        type="toolCall",
                        id="call-1",
                        name="lookup",
                        arguments={"q": 1},
                    ),
                ],
                "toolUse",
                200,
            ),
            210,
        )
        await append(
            service,
            session_id,
            run.id,
            "h-t-1",
            "h-a-tool",
            ToolResultMessage(
                role="toolResult",
                tool_call_id="call-1",
                tool_name="lookup",
                content=[text("结果正文")],
                is_error=False,
                timestamp=220,
            ),
            230,
        )
        await append(
            service,
            session_id,
            run.id,
            "h-a-final",
            "h-t-1",
            assistant([text("最终回答")], "stop", 300),
            310,
        )
        await service.finish_run(session_id, run.id, "completed")
        return {
            "session_id": session_id,
            "run_id": run.id,
            "user_id": user_id,
            "system_id": system_id,
            "a_tool": "h-a-tool",
            "t_1": "h-t-1",
            "a_final": "h-a-final",
        }
    finally:
        await database.close()


def check_credential_read(path: Path, http, evidence: dict) -> None:
    key = load_model_config().OPENAI_API_KEY
    before = sqlite3.connect(path, timeout=30)
    try:
        sessions_before = before.execute("SELECT count(*) FROM sessions").fetchone()[0]
        entries_before = before.execute(
            "SELECT count(*) FROM session_entries"
        ).fetchone()[0]
    finally:
        before.close()

    leaked_list = str(uuid4())
    raw_insert_session(path, leaked_list, f"泄露 {key}", 999)
    blocked_list = http.get("/api/sessions")
    assert blocked_list.status_code == 422
    assert blocked_list.json() == {
        "detail": {"code": "credential_detected", "message": CREDENTIAL_SAFE_MESSAGE}
    }

    leaked_history = str(uuid4())
    raw = sqlite3.connect(path, timeout=30)
    try:
        raw.execute(
            "INSERT INTO sessions (id, title, active_leaf_id, created_at, updated_at) "
            "VALUES (?, ?, NULL, ?, ?)",
            (leaked_history, "历史凭据", 1, 1),
        )
        system_text = json.dumps(
            [{"role": "system", "content": "系统", "timestamp": 1}],
            ensure_ascii=False,
        )
        user_text = json.dumps(
            [{"role": "user", "content": f"记住 {key}", "timestamp": 2}],
            ensure_ascii=False,
        )
        raw.execute(
            "INSERT INTO session_entries "
            "(session_id, id, parent_id, run_id, type, messages, created_at) "
            "VALUES (?, ?, NULL, NULL, 'message', ?, 1)",
            (leaked_history, "leak-sys", system_text),
        )
        raw.execute(
            "INSERT INTO session_entries "
            "(session_id, id, parent_id, run_id, type, messages, created_at) "
            "VALUES (?, ?, ?, NULL, 'message', ?, 2)",
            (leaked_history, "leak-user", "leak-sys", user_text),
        )
        raw.execute(
            "UPDATE sessions SET active_leaf_id = ? WHERE id = ?",
            ("leak-user", leaked_history),
        )
        raw.commit()
    finally:
        raw.close()
    blocked_history = http.get(f"/api/sessions/{leaked_history}/history")
    assert blocked_history.status_code == 422
    assert blocked_history.json() == {
        "detail": {"code": "credential_detected", "message": CREDENTIAL_SAFE_MESSAGE}
    }

    # 仅存在于投影会丢弃的字段：系统内容、文本签名、工具结果嵌套元数据。
    leaked_hidden = str(uuid4())
    hidden = sqlite3.connect(path, timeout=30)
    try:
        hidden.execute(
            "INSERT INTO sessions (id, title, active_leaf_id, created_at, updated_at) "
            "VALUES (?, ?, NULL, ?, ?)",
            (leaked_hidden, "隐藏字段凭据", 1, 1),
        )
        raw_insert_entry(
            hidden,
            leaked_hidden,
            "leak-h-sys",
            None,
            {"role": "system", "content": f"系统 {key}", "timestamp": 1},
            1,
        )
        raw_insert_entry(
            hidden,
            leaked_hidden,
            "leak-h-asst",
            "leak-h-sys",
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "正常回答", "text_signature": key}
                ],
                "api": "openai-completions",
                "provider": "example",
                "model": "model-1",
                "usage": {
                    "input": 1,
                    "output": 1,
                    "cache_read": 0,
                    "cache_write": 0,
                    "total_tokens": 2,
                },
                "stop_reason": "stop",
                "timestamp": 3,
            },
            3,
        )
        raw_insert_entry(
            hidden,
            leaked_hidden,
            "leak-h-tool",
            "leak-h-asst",
            {
                "role": "toolResult",
                "tool_call_id": "c1",
                "tool_name": "lookup",
                "content": [{"type": "text", "text": "真实结果"}],
                "is_error": False,
                "timestamp": 4,
                "details": {"hidden": key},
                "nested_calls": {
                    "calls": [
                        {
                            "id": "n1",
                            "name": "fetch",
                            "status": "ok",
                            "arguments": {"secret": key},
                        }
                    ],
                    "complete": True,
                },
            },
            4,
        )
        hidden.execute(
            "UPDATE sessions SET active_leaf_id = ? WHERE id = ?",
            ("leak-h-tool", leaked_hidden),
        )
        hidden.commit()
    finally:
        hidden.close()
    blocked_hidden = http.get(f"/api/sessions/{leaked_hidden}/history")
    assert blocked_hidden.status_code == 422
    assert blocked_hidden.json() == {
        "detail": {"code": "credential_detected", "message": CREDENTIAL_SAFE_MESSAGE}
    }
    assert key not in blocked_hidden.text

    after = sqlite3.connect(path, timeout=30)
    try:
        assert (
            after.execute("SELECT count(*) FROM sessions").fetchone()[0]
            == sessions_before + 3
        )
        assert (
            after.execute("SELECT count(*) FROM session_entries").fetchone()[0]
            == entries_before + 5
        )
        assert (
            after.execute(
                "SELECT active_leaf_id FROM sessions WHERE id = ?", (leaked_history,)
            ).fetchone()[0]
            == "leak-user"
        )
        assert (
            after.execute(
                "SELECT active_leaf_id FROM sessions WHERE id = ?", (leaked_hidden,)
            ).fetchone()[0]
            == "leak-h-tool"
        )
    finally:
        after.close()
    evidence["credential_read"] = {
        "list_status": blocked_list.status_code,
        "history_status": blocked_history.status_code,
        "hidden_status": blocked_hidden.status_code,
    }


def http_checks(evidence: dict) -> None:
    path = patch_default_database("session-history-http")
    server = Server(app)
    with server, client(server.base_url) as http:
        empty = http.get("/api/sessions")
        assert empty.status_code == 200
        assert empty.json() == {"sessions": []}
        assert empty.headers["cache-control"] == "no-store"

        ordered_ids = [str(uuid4()) for _ in range(3)]
        ordered_empty = str(uuid4())
        raw_insert_session(path, ordered_ids[0], "最早", 100)
        raw_insert_session(path, ordered_ids[1], "较新甲", 300)
        raw_insert_session(path, ordered_ids[2], "较新乙", 300)
        raw_insert_session(path, ordered_empty, "空持久会话", 200)
        listed = http.get("/api/sessions")
        assert listed.status_code == 200
        assert listed.headers["cache-control"] == "no-store"
        sessions = listed.json()["sessions"]
        expected = sorted(
            ordered_ids + [ordered_empty],
            key=lambda value: (
                {ordered_ids[0]: 100, ordered_ids[1]: 300, ordered_ids[2]: 300, ordered_empty: 200}[value],
                value,
            ),
            reverse=True,
        )
        assert [item["session_id"] for item in sessions] == expected
        for item in sessions:
            assert set(item) == {
                "session_id",
                "title",
                "active_leaf_id",
                "created_at",
                "updated_at",
            }
        empty_session = next(
            item for item in sessions if item["session_id"] == ordered_empty
        )
        assert empty_session["active_leaf_id"] is None
        assert empty_session["updated_at"] == 200

        seed = asyncio.run(seed_http_history(path))

        for host in ("evil.example", "localhost:9000"):
            assert (
                http.get("/api/sessions", headers={"Host": host}).status_code == 403
            )
            assert (
                http.get(
                    f"/api/sessions/{seed['session_id']}/history",
                    headers={"Host": host},
                ).status_code
                == 403
            )
        for origin in ("null", "https://evil.example"):
            assert (
                http.get("/api/sessions", headers={"Origin": origin}).status_code == 403
            )
            assert (
                http.get(
                    f"/api/sessions/{seed['session_id']}/history",
                    headers={"Origin": origin},
                ).status_code
                == 403
            )

        missing = http.get(f"/api/sessions/{uuid4()}/history")
        assert missing.status_code == 404
        assert missing.json()["detail"]["code"] == "session_not_found"
        invalid = http.get("/api/sessions/not-a-uuid/history")
        assert invalid.status_code == 422
        assert invalid.json() == {
            "detail": {"code": "invalid_request", "message": "请求字段不合法。"}
        }

        history = http.get(f"/api/sessions/{seed['session_id']}/history")
        assert history.status_code == 200, history.text
        assert history.headers["cache-control"] == "no-store"
        body = history.json()
        assert set(body) == {"session", "entries", "runs", "steering"}
        assert set(body["session"]) == {
            "session_id",
            "title",
            "active_leaf_id",
            "created_at",
            "updated_at",
        }
        assert body["session"]["session_id"] == seed["session_id"]
        assert body["session"]["title"] == "HTTP 历史"
        assert body["session"]["active_leaf_id"] == seed["a_final"]
        assert [item["entry_id"] for item in body["entries"]] == [
            seed["system_id"],
            seed["user_id"],
            seed["a_tool"],
            seed["t_1"],
            seed["a_final"],
        ]
        for item in body["entries"]:
            assert set(item) == {
                "entry_id",
                "parent_id",
                "run_id",
                "created_at",
                "message",
            }
        assert body["entries"][0]["message"] == {"role": "system"}
        assert body["entries"][1]["message"] == {
            "role": "user",
            "text": "问题一",
            "timestamp": 100,
        }
        tool_message = body["entries"][2]["message"]
        assert tool_message["role"] == "assistant"
        assert tool_message["stop_reason"] == "toolUse"
        assert tool_message["timestamp"] == 200
        assert [block["content_index"] for block in tool_message["content"]] == [1, 2]
        assert [block["type"] for block in tool_message["content"]] == [
            "text",
            "tool_call",
        ]
        assert tool_message["content"][0]["text"] == "调用工具"
        assert tool_message["content"][1]["tool_call_id"] == "call-1"
        assert tool_message["content"][1]["name"] == "lookup"
        assert tool_message["content"][1]["arguments"] == {"q": 1}
        allowed = {
            "content_index",
            "type",
            "text",
            "thinking",
            "tool_call_id",
            "name",
            "arguments",
        }
        assert all(set(block) <= allowed for block in tool_message["content"])
        encoded = json.dumps(tool_message, ensure_ascii=False)
        assert "内部计划" not in encoded and "thinking-sig" not in encoded
        assert "text-sig" not in encoded
        assert body["entries"][3]["message"] == {
            "role": "toolResult",
            "tool_call_id": "call-1",
            "tool_name": "lookup",
            "content": "结果正文",
            "is_error": False,
            "timestamp": 220,
        }
        assert body["entries"][4]["message"] == {
            "role": "assistant",
            "content": [{"content_index": 0, "type": "text", "text": "最终回答"}],
            "stop_reason": "stop",
            "timestamp": 300,
        }
        assert len(body["runs"]) == 1
        run = body["runs"][0]
        assert set(run) == {
            "session_id",
            "run_id",
            "request_entry_id",
            "last_entry_id",
            "status",
            "started_at",
            "finished_at",
            "error_code",
            "error_message",
        }
        assert run["run_id"] == seed["run_id"]
        assert run["request_entry_id"] == seed["user_id"]
        assert run["last_entry_id"] == seed["a_final"]
        assert run["status"] == "completed"
        assert isinstance(run["started_at"], int) and isinstance(
            run["finished_at"], int
        )
        assert run["error_code"] is None and run["error_message"] is None
        assert body["steering"] == []

        check_credential_read(path, http, evidence)
        evidence["http"] = {
            "empty": empty.json(),
            "ordered": [item["session_id"] for item in sessions],
            "history_entries": [item["entry_id"] for item in body["entries"]],
        }


def check() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=EVIDENCE, ignore_cleanup_errors=True) as directory:
        evidence = asyncio.run(service_checks(Path(directory)))
        http_checks(evidence)
    (EVIDENCE / "session-history.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "PASS: 会话列表排序与空列表、当前分支身份与父子链、消息公开投影、"
        "运行与工具关联、Steering 状态与消费节点、无助手节点失败运行、"
        "未配对工具链可读且拒绝续聊、重启中断恢复、并发读取快照一致、"
        "凭据保护与错误边界"
    )


if __name__ == "__main__":
    check()
