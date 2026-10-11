import asyncio
import sqlite3
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import aiosqlite
from pydantic import TypeAdapter, ValidationError

from app.agent.compaction_summary import (
    COMPACTION_SUMMARY_PREFIX,
    COMPACTION_SUMMARY_SUFFIX,
    CompactionSummaryMessage,
)
from app.agent.message_context import (
    AgentMessage,
    convert_to_llm,
    prepare_message_context,
)
from app.ai.context import validate_tool_pairs
from app.ai.messages import Message, serialize_message
from app.application.session.service import SessionService
from app.domain.session.attachments import attachment_storage_ref
from app.domain.session.branch import (
    build_session_context,
    build_session_path,
    message_entries,
)
from app.domain.session.models import (
    CompactionDetails,
    CompactionEntry,
    SendCommand,
    SendRequest,
    SessionMessageEntry,
)
from app.infrastructure.persistence.sqlite.database import (
    _MIGRATIONS,
    SCHEMA_VERSION,
    apply_schema,
    open_database,
)
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository

_adapter = TypeAdapter(Message)
_agent_adapter = TypeAdapter(list[AgentMessage])

BACKEND = Path(__file__).resolve().parents[1]
TEMP_ROOT = BACKEND / "temp" / "session-compaction"

NOW = 1_000
MIG_SESSION = "11111111-1111-4111-8111-111111111111"
MIG_ATTACH = "22222222-2222-4222-8222-222222222222"
COMP_SESSION = "33333333-3333-4333-8333-333333333333"

USAGE = {
    "input": 5,
    "output": 6,
    "cache_read": 1,
    "cache_write": 2,
    "total_tokens": 14,
}
SYSTEM_SOURCE = {"role": "system", "content": "instructions", "timestamp": 1}
USER_SOURCE = {"role": "user", "content": "开始", "timestamp": 2}
ASSISTANT_SOURCE = {
    "role": "assistant",
    "content": [{"type": "text", "text": "回答"}],
    "api": "openai-completions",
    "provider": "example",
    "model": "model-1",
    "usage": USAGE,
    "stop_reason": "stop",
    "timestamp": 3,
}
TOOL_RESULT_SOURCE = {
    "role": "toolResult",
    "tool_call_id": "call-1",
    "tool_name": "lookup",
    "content": [{"type": "text", "text": "result"}],
    "is_error": False,
    "timestamp": 4,
}

SOURCES = {
    "system": SYSTEM_SOURCE,
    "user": USER_SOURCE,
    "assistant": ASSISTANT_SOURCE,
    "toolResult": TOOL_RESULT_SOURCE,
}
PARSED = {key: _adapter.validate_python(source) for key, source in SOURCES.items()}
TEXTS = {key: "[" + serialize_message(message) + "]" for key, message in PARSED.items()}
STEERING_TEXT = serialize_message(PARSED["user"])


def system_message():
    return _adapter.validate_python(deepcopy(SYSTEM_SOURCE))


def assistant_message(**overrides):
    return _adapter.validate_python({**deepcopy(ASSISTANT_SOURCE), **overrides})


def attachment_reference(session_id: str):
    path = attachment_storage_ref(session_id, MIG_ATTACH, "note.md").removeprefix("tmp/")
    return {"attachment_id": MIG_ATTACH, "file_name": "note.md", "path": path}


def compaction_entry(
    session_id: str,
    entry_id: str,
    parent_id: str,
    run_id: str,
    first_kept_entry_id: str,
    *,
    created_at: int,
    summary: str = "历史摘要",
    details=None,
) -> CompactionEntry:
    return CompactionEntry(
        session_id=session_id,
        id=entry_id,
        parent_id=parent_id,
        run_id=run_id,
        type="compaction",
        summary=summary,
        first_kept_entry_id=first_kept_entry_id,
        tokens_before=1234,
        usage=_adapter.validate_python(ASSISTANT_SOURCE).usage,
        system_message=system_message(),
        details=details,
        created_at=created_at,
    )


async def open_store(path: Path):
    database = await open_database(path)
    repository = SqliteSessionRepository(database)
    return database, repository, SessionService(repository)


async def assert_foreign_keys(database) -> None:
    async def run(connection):
        cursor = await connection.execute("PRAGMA foreign_key_check")
        rows = await cursor.fetchall()
        await cursor.close()
        return rows

    assert await database.read(run) == []


async def fails(coroutine, label: str) -> None:
    try:
        await coroutine
    except sqlite3.DatabaseError:
        return
    raise AssertionError(f"{label}: 预期数据库拒绝，但语句成功")


async def raw_insert(connection, **values) -> None:
    await connection.execute(
        "INSERT INTO session_entries (session_id, id, parent_id, run_id, type, "
        "messages, summary, first_kept_entry_id, tokens_before, usage, "
        "system_message, details, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            values["session_id"],
            values["id"],
            values.get("parent_id"),
            values.get("run_id"),
            values["type"],
            values.get("messages"),
            values.get("summary"),
            values.get("first_kept_entry_id"),
            values.get("tokens_before"),
            values.get("usage"),
            values.get("system_message"),
            values.get("details"),
            values.get("created_at", NOW),
        ),
    )


async def seed_version_9(connection) -> None:
    await connection.execute(
        "INSERT INTO sessions (id, title, active_leaf_id, created_at, updated_at) "
        "VALUES (?, ?, NULL, ?, ?)",
        (MIG_SESSION, "迁移", NOW, NOW),
    )
    for key, entry_id, parent_id, run_id, created_at in (
        ("system", "m-sys", None, None, 11),
        ("user", "m-u", "m-sys", None, 12),
    ):
        await connection.execute(
            "INSERT INTO session_entries (session_id, id, parent_id, run_id, type, "
            "messages, created_at) VALUES (?, ?, ?, ?, 'message', ?, ?)",
            (MIG_SESSION, entry_id, parent_id, run_id, TEXTS[key], created_at),
        )
    await connection.execute(
        "INSERT INTO session_runs (session_id, id, request_entry_id, last_entry_id, "
        "status, started_at, finished_at, error_code, error_message) "
        "VALUES (?, 'm-run', 'm-u', NULL, 'running', ?, NULL, NULL, NULL)",
        (MIG_SESSION, NOW),
    )
    for key, entry_id, parent_id, created_at in (
        ("assistant", "m-a", "m-u", 13),
        ("toolResult", "m-t", "m-a", 14),
    ):
        await connection.execute(
            "INSERT INTO session_entries (session_id, id, parent_id, run_id, type, "
            "messages, created_at) VALUES (?, ?, ?, 'm-run', 'message', ?, ?)",
            (MIG_SESSION, entry_id, parent_id, TEXTS[key], created_at),
        )
    await connection.execute(
        "UPDATE session_runs SET last_entry_id = 'm-t' "
        "WHERE session_id = ? AND id = 'm-run'",
        (MIG_SESSION,),
    )
    await connection.execute(
        "UPDATE sessions SET active_leaf_id = 'm-t' WHERE id = ?", (MIG_SESSION,)
    )
    await connection.execute(
        "INSERT INTO session_attachments (attachment_id, session_id, file_name, "
        "size_bytes, storage_ref, created_at) VALUES (?, ?, 'note.md', 3, ?, ?)",
        (
            MIG_ATTACH,
            MIG_SESSION,
            attachment_storage_ref(MIG_SESSION, MIG_ATTACH, "note.md"),
            NOW,
        ),
    )
    await connection.execute(
        "INSERT INTO session_entry_attachments (session_id, entry_id, attachment_id, "
        "position) VALUES (?, 'm-u', ?, 0)",
        (MIG_SESSION, MIG_ATTACH),
    )
    await connection.execute(
        "INSERT INTO steering_inputs (session_id, id, run_id, message, status, "
        "entry_id, reason, created_at, updated_at) "
        "VALUES (?, 'm-st', 'm-run', ?, 'pending', NULL, NULL, ?, ?)",
        (MIG_SESSION, STEERING_TEXT, NOW, NOW),
    )
    await connection.execute(
        "INSERT INTO session_operations (operation_id, session_id, kind, request, "
        "run_id, steering_id, created_at) VALUES (?, ?, 'send', '{\"text\": \"hi\"}', "
        "'m-run', NULL, ?)",
        (str(uuid4()), MIG_SESSION, NOW),
    )
    await connection.execute(
        "INSERT INTO profile_snapshots (proposal_id, session_id, request_entry_id, "
        "source_entry_id, profile_id, base_profile_version, payload, display_entry_id, "
        "confirmation_entry_id, status, created_at) "
        "VALUES (?, ?, 'm-u', 'm-a', 1, 1, '{\"a\": 1}', 'm-t', NULL, 'pending', ?)",
        (str(uuid4()), MIG_SESSION, NOW),
    )


async def create_version_9(path: Path) -> None:
    connection = await aiosqlite.connect(path, isolation_level=None)
    try:
        await connection.execute("PRAGMA foreign_keys = ON")
        for version in range(1, 10):
            await apply_schema(
                connection, _MIGRATIONS[version].read_text(encoding="utf-8"), version
            )
        await connection.execute("BEGIN")
        await seed_version_9(connection)
        await connection.execute("COMMIT")
    finally:
        await connection.close()


async def one(connection, sql, parameters=()):
    cursor = await connection.execute(sql, parameters)
    row = await cursor.fetchone()
    await cursor.close()
    return row


async def scenario_migration(root: Path) -> None:
    path = root / "migration.db"
    await create_version_9(path)
    raw = await aiosqlite.connect(path, isolation_level=None)
    assert (await one(raw, "PRAGMA user_version"))[0] == 9
    await raw.close()

    database = await open_database(path)
    connection = database.connection
    try:
        assert (await one(connection, "PRAGMA user_version"))[0] == SCHEMA_VERSION == 11
        assert (await one(connection, "PRAGMA foreign_keys"))[0] == 1
        assert (await one(connection, "SELECT count(*) FROM session_entries"))[0] == 4
        assert (
            await one(
                connection,
                "SELECT messages FROM session_entries WHERE session_id = ? AND id = 'm-a'",
                (MIG_SESSION,),
            )
        )[0] == TEXTS["assistant"]
        assert (
            await one(connection, "SELECT active_leaf_id FROM sessions WHERE id = ?", (MIG_SESSION,))
        )[0] == "m-t"
        assert (
            await one(connection, "SELECT last_entry_id FROM session_runs WHERE id = 'm-run'")
        )[0] == "m-t"
        assert (
            await one(connection, "SELECT status FROM steering_inputs WHERE id = 'm-st'")
        )[0] == "pending"
        assert (await one(connection, "SELECT count(*) FROM session_operations"))[0] == 1
        assert (
            await one(connection, "SELECT payload FROM profile_snapshots")
        )[0] == '{"a": 1}'
        assert (
            await one(connection, "SELECT count(*) FROM session_attachments")
        )[0] == 1
        assert (
            await one(connection, "SELECT count(*) FROM session_entry_attachments")
        )[0] == 1
        columns = {row[1] for row in await fetch_all(connection, "PRAGMA table_info(session_entries)")}
        assert {"summary", "first_kept_entry_id", "tokens_before", "usage",
                "system_message", "details", "model_omitted"} <= columns
        assert (
            await one(
                connection,
                "SELECT count(*) FROM session_entries WHERE model_omitted <> 0",
            )
        )[0] == 0
        triggers = {
            row[0]
            for row in await fetch_all(
                connection, "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        assert "session_entries_parent_exists_insert" in triggers
        assert "session_entries_immutable_update" in triggers
        assert "session_entries_no_delete" not in triggers
        assert await fetch_all(connection, "PRAGMA foreign_key_check") == []
    finally:
        await database.close()

    # 升级后的库可继续以新结构读写压缩节点。
    reopened, repository, service = await open_store(path)
    try:
        node = compaction_entry(
            MIG_SESSION, "m-c", "m-t", "m-run", "m-u", created_at=17
        )
        async with repository.transaction():
            await repository.insert_entry(node)
        stored = await service.get_entry(MIG_SESSION, "m-c")
        assert isinstance(stored, CompactionEntry)
        assert stored.model_dump() == node.model_dump()

        # 省略标记随新结构真实往返：失败助手消息节点持久化 model_omitted。
        omitted = SessionMessageEntry(
            session_id=MIG_SESSION,
            id="m-omit",
            parent_id="m-c",
            run_id="m-run",
            type="message",
            messages=[
                assistant_message(
                    stop_reason="error",
                    error_message="context overflow",
                    context_overflow=True,
                )
            ],
            created_at=18,
            model_omitted=True,
        )
        async with repository.transaction():
            await repository.insert_entry(omitted)
        stored_omitted = await service.get_entry(MIG_SESSION, "m-omit")
        assert stored_omitted.model_omitted is True
        assert stored_omitted.messages[0].context_overflow is True
        assert (
            await one(
                reopened.connection,
                "SELECT model_omitted FROM session_entries "
                "WHERE session_id = ? AND id = 'm-omit'",
                (MIG_SESSION,),
            )
        )[0] == 1
    finally:
        await reopened.close()


async def fetch_all(connection, sql, parameters=()):
    cursor = await connection.execute(sql, parameters)
    rows = await cursor.fetchall()
    await cursor.close()
    return rows


async def scenario_compaction(database, repository, service) -> None:
    await service.create_session(COMP_SESSION, "压缩")
    send = await service.accept_send(
        SendCommand(
            operation_id=str(uuid4()),
            session_id=COMP_SESSION,
            request=SendRequest(text="开始"),
        ),
        system_message=system_message(),
    )
    run = send.run
    user = await service.get_entry(COMP_SESSION, run.request_entry_id)
    system_id = user.parent_id
    assistant = SessionMessageEntry(
        session_id=COMP_SESSION,
        id="cp-a1",
        parent_id=user.id,
        run_id=run.id,
        type="message",
        messages=[assistant_message()],
        created_at=200,
    )
    await service.append_entry(assistant)

    details = CompactionDetails.model_validate(
        {"attachments": [attachment_reference(COMP_SESSION)]}
    )
    compaction = compaction_entry(
        COMP_SESSION, "cp-c1", "cp-a1", run.id, user.id, created_at=201, details=details
    )
    async with repository.transaction():
        await repository.insert_entry(compaction)

    stored = await service.get_entry(COMP_SESSION, "cp-c1")
    assert isinstance(stored, CompactionEntry)
    assert stored.model_dump() == compaction.model_dump()
    assert stored.details.attachments[0].path == attachment_reference(COMP_SESSION)["path"]

    # 完整原始路径包含压缩节点，且顺序保持父子链；业务读取过滤为真实消息节点。
    entries = await service.list_entries(COMP_SESSION)
    path = build_session_path(COMP_SESSION, entries, "cp-c1")
    assert [entry.id for entry in path] == [system_id, user.id, "cp-a1", "cp-c1"]
    assert [entry.id for entry in message_entries(path)] == [system_id, user.id, "cp-a1"]
    assert [entry.id for entry in await service.get_branch(COMP_SESSION, "cp-c1")] == [
        system_id,
        user.id,
        "cp-a1",
    ]
    context = build_session_context(COMP_SESSION, entries, "cp-c1")
    assert [message.role for message in context] == ["system", "user", "assistant"]
    assert await service.get_context(COMP_SESSION, "cp-c1") is not None


async def scenario_roundtrip(root: Path) -> None:
    path = root / "roundtrip.db"
    database, repository, service = await open_store(path)
    try:
        await scenario_compaction(database, repository, service)
        await assert_foreign_keys(database)
    finally:
        await database.close()

    reopened, repository, service = await open_store(path)
    try:
        session = await service.get_session(COMP_SESSION)
        assert session.active_leaf_id == "cp-a1"
        run = await service.get_run(COMP_SESSION, (await service.list_runs(COMP_SESSION))[0].id)
        user = await service.get_entry(COMP_SESSION, run.request_entry_id)
        stored = await service.get_entry(COMP_SESSION, "cp-c1")
        assert isinstance(stored, CompactionEntry) and stored.summary == "历史摘要"
        assert stored.details.attachments[0].attachment_id == MIG_ATTACH
        assert [entry.id for entry in await service.get_branch(COMP_SESSION, "cp-c1")] == [
            user.parent_id,
            user.id,
            "cp-a1",
        ]
        assert (await service.get_entry(COMP_SESSION, "cp-c1")).type == "compaction"
        await assert_foreign_keys(reopened)
    finally:
        await reopened.close()


def check_model_rejections() -> None:
    base = {
        "session_id": MIG_SESSION,
        "id": "c1",
        "parent_id": "p1",
        "run_id": "r1",
        "type": "compaction",
        "summary": "摘要",
        "first_kept_entry_id": "k1",
        "tokens_before": 10,
        "usage": USAGE,
        "system_message": SYSTEM_SOURCE,
        "details": None,
        "created_at": 1,
    }
    CompactionEntry.model_validate(base)
    rejections = [
        ("空摘要", {"summary": "   "}),
        ("负 tokens_before", {"tokens_before": -1}),
        ("空保留起点", {"first_kept_entry_id": ""}),
        ("缺少 usage", {"usage": None}),
        ("usage 非法", {"usage": {"input": 1}}),
        ("未知字段", {"unknown": 1}),
        ("非指定类型", {"type": "message"}),
        ("detail 非对象路径", {"details": {"attachments": [
            {"attachment_id": MIG_ATTACH, "file_name": "note.md", "path": "other/x.md"}
        ]}}),
    ]
    for label, overrides in rejections:
        try:
            CompactionEntry.model_validate({**base, **overrides})
        except ValidationError:
            continue
        raise AssertionError(f"{label}: 预期压缩节点被拒绝")

    # 消息节点保持独立载荷校验：不接受压缩字段与多元素 messages。
    message_base = {
        "session_id": "s1",
        "id": "m1",
        "parent_id": None,
        "run_id": None,
        "type": "message",
        "messages": [USER_SOURCE],
        "created_at": 1,
    }
    SessionMessageEntry.model_validate(message_base)
    for label, overrides in [
        ("消息节点携带压缩字段", {"summary": "摘要"}),
        ("空消息数组", {"messages": []}),
        ("多元素消息数组", {"messages": [USER_SOURCE, USER_SOURCE]}),
    ]:
        try:
            SessionMessageEntry.model_validate({**message_base, **overrides})
        except ValidationError:
            continue
        raise AssertionError(f"{label}: 预期消息节点被拒绝")


def check_path_integrity() -> None:
    system = SessionMessageEntry.model_validate({
        "session_id": "s1", "id": "sys", "parent_id": None, "run_id": None,
        "type": "message", "messages": [SYSTEM_SOURCE], "created_at": 0,
    })
    user = SessionMessageEntry.model_validate({
        "session_id": "s1", "id": "u1", "parent_id": "sys", "run_id": None,
        "type": "message", "messages": [USER_SOURCE], "created_at": 1,
    })
    node = CompactionEntry.model_validate({
        "session_id": "s1", "id": "c1", "parent_id": "u1", "run_id": "r1",
        "type": "compaction", "summary": "摘要", "first_kept_entry_id": "sys",
        "tokens_before": 1, "usage": USAGE, "system_message": SYSTEM_SOURCE,
        "details": None, "created_at": 2,
    })
    path = build_session_path("s1", [system, user, node], "c1")
    assert [entry.id for entry in path] == ["sys", "u1", "c1"]

    def assert_value_error(call, label: str) -> None:
        try:
            call()
        except ValueError as error:
            assert label in str(error), (label, str(error))
            return
        raise AssertionError(f"{label}: 预期 ValueError")

    assert_value_error(
        lambda: build_session_path("s1", [system, user, node, node], "c1"), "重复节点 ID"
    )
    assert_value_error(lambda: build_session_path("s1", [system, user, node], "nope"), "未知 leaf")
    assert_value_error(lambda: build_session_path("s2", [system, user, node], "c1"), "不属于会话")
    broken = CompactionEntry.model_validate({
        "session_id": "s1", "id": "c2", "parent_id": "ghost", "run_id": "r1",
        "type": "compaction", "summary": "摘要", "first_kept_entry_id": "c2",
        "tokens_before": 1, "usage": USAGE, "system_message": SYSTEM_SOURCE,
        "details": None, "created_at": 3,
    })
    assert_value_error(lambda: build_session_path("s1", [broken], "c2"), "缺少父节点")
    self_node = CompactionEntry.model_validate({
        "session_id": "s1", "id": "s", "parent_id": "s", "run_id": "r1",
        "type": "compaction", "summary": "摘要", "first_kept_entry_id": "s",
        "tokens_before": 1, "usage": USAGE, "system_message": SYSTEM_SOURCE,
        "details": None, "created_at": 4,
    })
    assert_value_error(lambda: build_session_path("s1", [self_node], "s"), "自引用")
    x = SessionMessageEntry.model_validate({
        "session_id": "s1", "id": "x", "parent_id": "y", "run_id": None,
        "type": "message", "messages": [USER_SOURCE], "created_at": 5,
    })
    y = SessionMessageEntry.model_validate({
        "session_id": "s1", "id": "y", "parent_id": "x", "run_id": None,
        "type": "message", "messages": [USER_SOURCE], "created_at": 6,
    })
    assert_value_error(lambda: build_session_path("s1", [x, y], "x"), "环")


async def scenario_db_guards(database, repository, service) -> None:
    await service.create_session("s-guards", "校验")
    send = await service.accept_send(
        SendCommand(
            operation_id=str(uuid4()),
            session_id="s-guards",
            request=SendRequest(text="开始"),
        ),
        system_message=system_message(),
    )
    run = send.run
    user = await service.get_entry("s-guards", run.request_entry_id)
    await service.append_entry(
        SessionMessageEntry(
            session_id="s-guards", id="g-a1", parent_id=user.id, run_id=run.id,
            type="message", messages=[assistant_message()], created_at=300,
        )
    )
    await service.create_session("s-other", "其他")
    connection = database.connection

    valid = {
        "session_id": "s-guards", "id": "g-c1", "parent_id": "g-a1", "run_id": run.id,
        "type": "compaction", "messages": None, "summary": "摘要",
        "first_kept_entry_id": user.id, "tokens_before": 1,
        "usage": '{"input":1,"output":1,"cache_read":0,"cache_write":0,"total_tokens":2}',
        "system_message": serialize_message(system_message()), "details": None,
    }
    illegal = [
        ("压缩行携带 messages", {"messages": TEXTS["user"]}),
        ("压缩行空摘要", {"summary": ""}),
        ("压缩行负 tokens", {"tokens_before": -5}),
        ("压缩行缺 usage", {"usage": None}),
        ("压缩行 usage 非对象", {"usage": "[]"}),
        ("压缩行缺 system_message", {"system_message": None}),
        ("压缩行 details 非对象", {"details": "[]"}),
        ("压缩行缺保留起点", {"first_kept_entry_id": None}),
        ("跨会话保留起点", {"first_kept_entry_id": "no-such"}),
        ("未知节点类型", {"type": "custom"}),
    ]
    for label, overrides in illegal:
        await fails(
            raw_insert(connection, **{**valid, **overrides}), label
        )

    # 消息行不得携带压缩载荷。
    await fails(
        raw_insert(
            connection,
            session_id="s-guards", id="g-bad", parent_id="g-a1", run_id=run.id,
            type="message", messages=TEXTS["user"], summary="摘要",
        ),
        "消息行携带压缩字段",
    )
    # 跨会话父节点。
    await fails(
        raw_insert(
            connection, session_id="s-other", id="g-c2", parent_id="g-a1",
            run_id=run.id, type="compaction", summary="摘要",
            first_kept_entry_id="g-a1", tokens_before=1,
            usage='{}', system_message=serialize_message(system_message()), details=None,
        ),
        "跨会话父节点",
    )
    assert (await one(connection, "SELECT count(*) FROM session_entries WHERE id LIKE 'g-c%'"))[0] == 0
    await service.finish_run("s-guards", run.id, "completed")


async def scenario_transaction_rollback(database, repository, service) -> None:
    await service.create_session("s-tx", "回滚")
    send = await service.accept_send(
        SendCommand(
            operation_id=str(uuid4()),
            session_id="s-tx",
            request=SendRequest(text="开始"),
        ),
        system_message=system_message(),
    )
    run = send.run
    user = await service.get_entry("s-tx", run.request_entry_id)
    leaf = (await service.get_session("s-tx")).active_leaf_id
    before = len(await service.list_entries("s-tx"))

    async def failing() -> None:
        async with repository.transaction():
            await repository.insert_entry(
                SessionMessageEntry(
                    session_id="s-tx", id="tx-a", parent_id=user.id, run_id=run.id,
                    type="message", messages=[assistant_message()], created_at=400,
                )
            )
            await repository.insert_entry(
                compaction_entry("s-tx", "tx-c", "tx-a", run.id, user.id, created_at=401)
            )
            raise RuntimeError("boom")

    try:
        await failing()
    except RuntimeError:
        pass
    else:
        raise AssertionError("事务内异常未传播")

    assert len(await service.list_entries("s-tx")) == before
    assert await repository.get_entry("s-tx", "tx-a") is None
    assert await repository.get_entry("s-tx", "tx-c") is None
    assert (await service.get_session("s-tx")).active_leaf_id == leaf
    await service.finish_run("s-tx", run.id, "completed")


async def check_conversion() -> None:
    summary = CompactionSummaryMessage(
        role="compactionSummary", summary="历史摘要", tokens_before=88, timestamp=9
    )
    messages = [
        _adapter.validate_python(SYSTEM_SOURCE),
        _adapter.validate_python(USER_SOURCE),
        summary,
        _adapter.validate_python(ASSISTANT_SOURCE),
    ]
    converted = convert_to_llm(messages)
    assert [message.role for message in converted] == [
        "system",
        "user",
        "user",
        "assistant",
    ]
    assert converted[2].content[0].text == (
        COMPACTION_SUMMARY_PREFIX + "历史摘要" + COMPACTION_SUMMARY_SUFFIX
    )
    assert converted[2].timestamp == 9
    validate_tool_pairs(converted)

    projected = await prepare_message_context(messages)
    assert [message.role for message in projected] == ["system", "user", "user", "assistant"]

    # AI 层 Message 保持四种标准角色：摘要角色被拒绝。
    try:
        _adapter.validate_python(
            {"role": "compactionSummary", "summary": "s", "tokens_before": 1, "timestamp": 1}
        )
    except ValidationError:
        pass
    else:
        raise AssertionError("Message 未拒绝摘要角色")

    # AgentMessage 联合接受摘要并拒绝未知角色。
    _agent_adapter.validate_python([USER_SOURCE, summary.model_dump(exclude_unset=True)])
    try:
        _agent_adapter.validate_python([{"role": "unknown", "timestamp": 1}])
    except ValidationError:
        pass
    else:
        raise AssertionError("AgentMessage 未拒绝未知角色")

    for label, overrides in [
        ("未知字段", {"unknown": 1}),
        ("tokens 类型", {"tokens_before": "1"}),
        ("时间戳非法", {"timestamp": None}),
    ]:
        try:
            CompactionSummaryMessage.model_validate(
                {
                    "role": "compactionSummary",
                    "summary": "s",
                    "tokens_before": 1,
                    "timestamp": 1,
                    **overrides,
                }
            )
        except ValidationError:
            continue
        raise AssertionError(f"摘要消息 {label}: 预期被拒绝")


async def run() -> None:
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=TEMP_ROOT, ignore_cleanup_errors=True) as directory:
        root = Path(directory)
        await scenario_migration(root)
        await scenario_roundtrip(root)

        database, repository, service = await open_store(root / "main.db")
        try:
            await scenario_db_guards(database, repository, service)
            await scenario_transaction_rollback(database, repository, service)
            await assert_foreign_keys(database)
        finally:
            await database.close()

        check_model_rejections()
        check_path_integrity()
        await check_conversion()

    print(
        "会话压缩节点：版本 9 升级保真、节点读写、路径重建、载荷校验、"
        "事务回滚、关闭重开、摘要转换与模型消息校验自检通过"
    )


def check() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    check()
