import asyncio
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

import aiosqlite
from pydantic import TypeAdapter

from app.ai.messages import Message, serialize_message
from app.infrastructure.persistence.sqlite.database import (
    _FOREIGN_KEYS,
    _INDEXES,
    _MIGRATIONS,
    _TABLE_COLUMNS,
    _TRIGGERS,
    SCHEMA_VERSION,
    apply_schema,
    default_database_path,
    open_database,
)

_adapter = TypeAdapter(Message)
_list_adapter = TypeAdapter(list[Message])

BACKEND = Path(__file__).resolve().parents[1]
TEMP_ROOT = BACKEND / "temp" / "session-database"
NOW = 1_000

SYSTEM_SOURCE = {
    "role": "system",
    "content": [
        {"type": "text", "text": "instructions", "text_signature": "sig"}
    ],
    "sections": {"active": "指令片段", "removed": None},
    "tools_added": [
        {
            "name": "lookup",
            "description": "lookup data",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "examples": ["a", {"b": 1}]}},
                "required": ["query"],
                "additionalProperties": False,
                "x-provider": {"nested": [None, True, 2.5]},
            },
            "constrained_sampling": {
                "type": "grammar",
                "variants": {"openai_regex": "[a-z]+"},
            },
        }
    ],
    "timestamp": 1.5,
}
USER_SOURCE = {
    "role": "user",
    "content": [
        {"type": "text", "text": "问题"},
        {"type": "image", "data": "base64", "mime_type": "image/png"},
    ],
    "timestamp": 2,
}
ASSISTANT_SOURCE = {
    "role": "assistant",
    "content": [
        {"type": "thinking", "thinking": "plan", "thinking_signature": "enc", "redacted": True},
        {
            "type": "toolCall",
            "id": "call-1",
            "name": "lookup",
            "arguments": {"query": "a", "nested": {"values": [1, None]}},
            "thought_signature": "thought-signature",
            "namespace": "tools",
        },
        {"type": "text", "text": "done", "text_signature": '{"v":1,"id":"response-1"}'},
    ],
    "api": "openai-completions",
    "provider": "example",
    "model": "model-1",
    "response_model": "actual-model",
    "response_id": "response-1",
    "usage": {
        "input": 1,
        "output": 2,
        "cache_read": 3,
        "cache_write": 4,
        "cache_write_1h": 5,
        "reasoning": 1,
        "total_tokens": 10,
        "cost": {"input": 0.1, "output": 0.2, "cache_read": 0.3, "cache_write": 0.4, "total": 1},
    },
    "stop_reason": "toolUse",
    "diagnostics": [
        {
            "type": "provider",
            "timestamp": 3,
            "error": {"name": "Error", "message": "safe", "stack": "redacted-stack", "code": 7.5},
            "details": {"response": {"status": 429}},
        }
    ],
    "deferred": {
        "provider": "example",
        "model_id": "model-1",
        "api": "custom",
        "id": "task-1",
        "expires_at": 123.5,
        "poll_after_ms": 2.5,
        "data": {"cursor": [1, {"next": None}]},
    },
    "timestamp": 3,
}
TOOL_RESULT_SOURCE = {
    "role": "toolResult",
    "tool_call_id": "call-1",
    "tool_name": "lookup",
    "content": [
        {"type": "text", "text": "result"},
        {"type": "image", "data": "base64", "mime_type": "image/jpeg"},
    ],
    "is_error": False,
    "timestamp": 4,
    "details": None,
    "nested_calls": {
        "calls": [
            {
                "id": "nested-1",
                "name": "fetch",
                "arguments": {"id": 9},
                "arguments_bytes": 8.5,
                "status": "ok",
                "duration_ms": 2.5,
                "error": "safe-error",
            }
        ],
        "complete": True,
    },
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

TABLES = set(_TABLE_COLUMNS)
INDEXES = set(_INDEXES)
TRIGGERS = set(_TRIGGERS)
FOREIGN_KEYS = {
    table: {
        (from_column, target, to_column)
        for target, pairs in groups
        for from_column, to_column in pairs
    }
    for table, groups in _FOREIGN_KEYS.items()
}


async def one(connection, sql, parameters=()):
    cursor = await connection.execute(sql, parameters)
    row = await cursor.fetchone()
    await cursor.close()
    return row


async def many(connection, sql, parameters=()):
    cursor = await connection.execute(sql, parameters)
    rows = await cursor.fetchall()
    await cursor.close()
    return rows


async def fails(coroutine, label: str) -> None:
    try:
        await coroutine
    except sqlite3.DatabaseError:
        return
    raise AssertionError(f"{label}: 预期数据库拒绝，但语句成功")


async def insert_session(connection, session_id: str, created_at: int = NOW) -> None:
    await connection.execute(
        "INSERT INTO sessions (id, title, active_leaf_id, created_at, updated_at) "
        "VALUES (?, ?, NULL, ?, ?)",
        (session_id, "会话", created_at, created_at),
    )


async def insert_entry(
    connection,
    session_id,
    entry_id,
    parent_id,
    run_id,
    type_,
    messages,
    created_at=NOW,
) -> None:
    await connection.execute(
        "INSERT INTO session_entries "
        "(session_id, id, parent_id, run_id, type, messages, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (session_id, entry_id, parent_id, run_id, type_, messages, created_at),
    )


async def insert_run(
    connection,
    session_id,
    run_id,
    request_entry_id,
    last_entry_id,
    status,
    started_at=NOW,
) -> None:
    await connection.execute(
        "INSERT INTO session_runs "
        "(session_id, id, request_entry_id, last_entry_id, status, started_at, "
        "finished_at, error_code, error_message) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
        (session_id, run_id, request_entry_id, last_entry_id, status, started_at),
    )


async def insert_steering(
    connection,
    session_id,
    steering_id,
    run_id,
    status,
    entry_id,
    message=STEERING_TEXT,
    reason=None,
) -> None:
    await connection.execute(
        "INSERT INTO steering_inputs "
        "(session_id, id, run_id, message, status, entry_id, reason, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, steering_id, run_id, message, status, entry_id, reason, NOW, NOW),
    )


async def insert_operation(
    connection,
    operation_id,
    session_id,
    kind,
    run_id,
    steering_id=None,
    request='{"text": "hi"}',
) -> None:
    await connection.execute(
        "INSERT INTO session_operations "
        "(operation_id, session_id, kind, request, run_id, steering_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (operation_id, session_id, kind, request, run_id, steering_id, NOW),
    )


async def seed(connection, session_id: str, prefix: str, run_id: str) -> None:
    sys_id, user_id, assistant_id, steering_user_id = (
        f"{prefix}sys",
        f"{prefix}u1",
        f"{prefix}a1",
        f"{prefix}u2",
    )
    await connection.execute("BEGIN")
    await insert_session(connection, session_id)
    await insert_entry(connection, session_id, sys_id, None, None, "message", TEXTS["system"])
    await insert_entry(connection, session_id, user_id, sys_id, None, "message", TEXTS["user"])
    await insert_run(connection, session_id, run_id, user_id, None, "running")
    await insert_entry(
        connection, session_id, assistant_id, user_id, run_id, "message", TEXTS["assistant"]
    )
    await insert_entry(
        connection, session_id, steering_user_id, assistant_id, run_id, "message", TEXTS["user"]
    )
    await connection.execute(
        "UPDATE session_runs SET last_entry_id = ? WHERE session_id = ? AND id = ?",
        (assistant_id, session_id, run_id),
    )
    await connection.execute(
        "UPDATE sessions SET active_leaf_id = ?, updated_at = ? WHERE id = ?",
        (assistant_id, NOW, session_id),
    )
    await connection.execute("COMMIT")


async def scenario_initialize(path: Path) -> None:
    database = await open_database(path)
    connection = database.connection
    await seed(connection, "s1", "", "r1")
    stored = await one(
        connection,
        "SELECT messages FROM session_entries WHERE session_id = ? AND id = ?",
        ("s1", "a1"),
    )
    assert stored[0] == TEXTS["assistant"]
    assert (await one(connection, "PRAGMA user_version"))[0] == SCHEMA_VERSION
    await database.close()

    reopened = await open_database(path)
    connection = reopened.connection
    assert (await one(connection, "PRAGMA user_version"))[0] == SCHEMA_VERSION
    assert (await one(connection, "PRAGMA foreign_keys"))[0] == 1
    assert (await one(connection, "SELECT count(*) FROM session_entries"))[0] == 4
    assert (await one(connection, "SELECT count(*) FROM sessions"))[0] == 1
    assert (
        await one(
            connection,
            "SELECT messages FROM session_entries WHERE session_id = ? AND id = ?",
            ("s1", "a1"),
        )
    )[0] == TEXTS["assistant"]
    assert (await one(connection, "SELECT active_leaf_id FROM sessions WHERE id = 's1'"))[0] == "a1"
    await reopened.close()


async def scenario_structure(connection) -> None:
    names = {row[0] for row in await many(connection, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert names == TABLES
    for table in TABLES:
        row = await one(connection, "SELECT strict FROM pragma_table_list WHERE name = ?", (table,))
        assert row is not None and row[0] == 1
    index_names = {row[0] for row in await many(connection, "SELECT name FROM sqlite_master WHERE type='index'")}
    assert INDEXES <= index_names
    trigger_names = {row[0] for row in await many(connection, "SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert TRIGGERS <= trigger_names
    for table, expected in FOREIGN_KEYS.items():
        rows = await many(connection, f"PRAGMA foreign_key_list({table})")
        actual = {(row[3], row[2], row[4]) for row in rows}
        assert expected <= actual, (table, expected - actual)
    assert await many(connection, "PRAGMA foreign_key_check") == []


async def scenario_cross_session(connection) -> None:
    await fails(
        insert_entry(connection, "s1", "x1", "bsys", None, "message", TEXTS["user"]),
        "跨会话父节点",
    )
    await fails(
        insert_entry(connection, "s1", "x2", "a1", "r2", "message", TEXTS["user"]),
        "跨会话 run_id",
    )
    await fails(
        insert_run(connection, "s1", "rx", "bu1", None, "running"),
        "跨会话 request_entry_id",
    )
    await fails(
        connection.execute("UPDATE sessions SET active_leaf_id = 'ba1' WHERE id = 's1'"),
        "跨会话 active_leaf_id",
    )
    await fails(
        insert_steering(connection, "s1", "stx", "r2", "pending", None),
        "跨会话 Steering 运行",
    )
    await insert_steering(connection, "s2", "bst1", "r2", "pending", None)
    await fails(
        insert_steering(connection, "s1", "stx2", "r1", "consumed", "bu2"),
        "跨会话 consumed 节点",
    )
    await fails(
        insert_operation(connection, "opx", "s1", "steering", "r1", steering_id="bst1"),
        "跨会话操作 Steering",
    )


async def scenario_illegal_values(connection) -> None:
    await fails(
        insert_entry(connection, "s1", "x3", "sys", None, "custom", TEXTS["user"]),
        "非法节点类型",
    )
    await fails(
        insert_run(connection, "s1", "rx2", "u1", None, "weird"),
        "非法运行状态",
    )
    await fails(
        insert_steering(connection, "s1", "stx3", "r1", "weird", None),
        "非法 Steering 状态",
    )
    await fails(
        insert_operation(connection, "opx2", "s1", "weird", "r1"),
        "非法操作类型",
    )
    invalid_messages = [
        "not json",
        "{}",
        "[]",
        "[1]",
        '[{"timestamp": 1}]',
        '[{"role": "custom", "timestamp": 1}]',
    ]
    for index, value in enumerate(invalid_messages):
        await fails(
            insert_entry(connection, "s1", f"bad{index}", "sys", None, "message", value),
            f"非法 messages {value!r}",
        )
    array_with_two = (
        "["
        + serialize_message(PARSED["user"])
        + ","
        + serialize_message(PARSED["user"])
        + "]"
    )
    await fails(
        insert_entry(connection, "s1", "bad2", "sys", None, "message", array_with_two),
        "长度不为 1 的消息数组",
    )
    await fails(
        insert_steering(
            connection,
            "s1",
            "stx4",
            "r1",
            "pending",
            None,
            message=serialize_message(PARSED["assistant"]),
        ),
        "Steering 非用户消息",
    )
    await fails(
        insert_operation(connection, "opx3", "s1", "send", "r1", request="[]"),
        "操作请求非对象",
    )


async def scenario_pending_assistant(connection) -> None:
    pending = {
        **ASSISTANT_SOURCE,
        "stop_reason": "pending",
        "usage": None,
    }
    pending_text = "[" + serialize_message(_adapter.validate_python(pending)) + "]"
    await fails(
        insert_entry(connection, "s1", "pending", "a1", "r1", "message", pending_text),
        "pending 助手消息",
    )


async def scenario_run_links(connection) -> None:
    await fails(
        insert_run(connection, "s1", "rsub", "a1", None, "running"),
        "非用户请求节点",
    )
    await fails(
        connection.execute(
            "UPDATE session_runs SET last_entry_id = 'u1' WHERE session_id = 's1' AND id = 'r1'"
        ),
        "错误 last_entry_id",
    )
    await fails(
        insert_run(connection, "s1", "rlast", "u1", "u2", "running"),
        "last_entry_id 不属于该运行",
    )
    await insert_run(connection, "s1", "r1b", "u1", None, "running")
    await fails(
        connection.execute(
            "UPDATE session_runs SET request_entry_id = 'a1' "
            "WHERE session_id = 's1' AND id = 'r1'"
        ),
        "更新请求节点为非用户消息",
    )
    await fails(
        insert_steering(connection, "s1", "stx5", "r1b", "consumed", "u2"),
        "消费节点不属于目标运行",
    )
    await fails(
        insert_steering(connection, "s1", "stx6", "r1", "consumed", "a1"),
        "消费节点非用户消息",
    )


async def scenario_steering_and_operations(connection) -> None:
    await fails(
        insert_steering(connection, "s1", "stx7", "r1", "pending", "u2"),
        "pending 携带 entry_id",
    )
    await fails(
        insert_steering(connection, "s1", "stx8", "r1", "consumed", None),
        "consumed 缺少 entry_id",
    )
    await insert_steering(connection, "s1", "st1", "r1", "pending", None)
    await fails(
        insert_operation(connection, "opx4", "s1", "send", "r1", steering_id="st1"),
        "非 steering 操作携带 steering_id",
    )
    await fails(
        insert_operation(connection, "opx5", "s1", "steering", "r1", steering_id=None),
        "steering 操作缺少 steering_id",
    )
    await fails(
        insert_operation(connection, "opx6", "s1", "steering", "r1b", steering_id="st1"),
        "操作 Steering 不属于指定运行",
    )
    await insert_operation(connection, "op1", "s1", "steering", "r1", steering_id="st1")
    await fails(
        insert_operation(connection, "op1", "s1", "send", "r1"),
        "重复 operation_id",
    )
    await insert_steering(connection, "s1", "st2", "r1", "consumed", "u2")
    await fails(
        insert_steering(connection, "s1", "st3", "r1", "consumed", "u2"),
        "同一节点重复关联 Steering",
    )
    await fails(
        connection.execute(
            "UPDATE steering_inputs SET status = 'consumed', entry_id = 'a1' "
            "WHERE session_id = 's1' AND id = 'st1'"
        ),
        "更新 Steering 关联非用户节点",
    )
    await insert_operation(connection, "op2", "s1", "send", "r1")
    await insert_steering(connection, "s1", "st2b", "r1b", "pending", None)
    await fails(
        connection.execute(
            "UPDATE session_operations SET kind = 'steering', steering_id = 'st2b' "
            "WHERE operation_id = 'op2'"
        ),
        "更新操作关联错误运行的 Steering",
    )


async def scenario_entry_integrity(connection) -> None:
    await fails(
        insert_entry(connection, "s1", "selfref", "selfref", None, "message", TEXTS["user"]),
        "自引用父节点",
    )
    await fails(
        insert_entry(connection, "s1", "ghostchild", "ghost", None, "message", TEXTS["user"]),
        "不存在的父节点",
    )
    await fails(
        connection.execute(
            "UPDATE session_entries SET messages = ? WHERE session_id = 's1' AND id = 'a1'",
            (TEXTS["user"],),
        ),
        "节点更新",
    )
    await fails(
        connection.execute("DELETE FROM session_entries WHERE session_id = 's1' AND id = 'a1'"),
        "节点直接删除",
    )
    assert (
        await one(
            connection,
            "SELECT count(*) FROM session_entries WHERE session_id = 's1'",
        )
    )[0] == 4


async def scenario_message_roundtrip(connection) -> None:
    await connection.execute("BEGIN")
    await insert_session(connection, "rt")
    await insert_entry(connection, "rt", "rtsys", None, None, "message", TEXTS["system"])
    await insert_entry(connection, "rt", "rtu", "rtsys", None, "message", TEXTS["user"])
    await insert_run(connection, "rt", "rt-run", "rtu", None, "running")
    await insert_entry(connection, "rt", "rta", "rtu", "rt-run", "message", TEXTS["assistant"])
    await insert_entry(connection, "rt", "rtt", "rta", "rt-run", "message", TEXTS["toolResult"])
    await connection.execute("COMMIT")

    pairs = [
        ("rtsys", "system"),
        ("rtu", "user"),
        ("rta", "assistant"),
        ("rtt", "toolResult"),
    ]
    for entry_id, key in pairs:
        stored = await one(
            connection,
            "SELECT messages FROM session_entries WHERE session_id = 'rt' AND id = ?",
            (entry_id,),
        )
        text = stored[0]
        assert text == TEXTS[key], key
        restored = _list_adapter.validate_json(text)
        assert restored == [PARSED[key]], key
        assert json.loads(text) == [PARSED[key].model_dump(exclude_unset=True)], key


async def scenario_schema_rollback(path: Path) -> None:
    connection = await aiosqlite.connect(path, isolation_level=None)
    await connection.execute("PRAGMA foreign_keys = ON")
    broken = (
        "CREATE TABLE broken (a INTEGER NOT NULL) STRICT;\n"
        "CREATE TABLE broken (a INTEGER);\n"
    )
    try:
        await apply_schema(connection, broken, 1)
    except sqlite3.DatabaseError:
        pass
    else:
        raise AssertionError("损坏 schema 未抛出异常")
    assert (await one(connection, "PRAGMA user_version"))[0] == 0
    assert await many(connection, "SELECT name FROM sqlite_master WHERE type='table'") == []
    await connection.close()

    database = await open_database(path)
    assert (await one(database.connection, "PRAGMA user_version"))[0] == SCHEMA_VERSION
    assert (await one(database.connection, "SELECT count(*) FROM sessions"))[0] == 0
    await database.close()


async def scenario_transaction_rollback(connection) -> None:
    await connection.execute("BEGIN")
    await insert_session(connection, "s9")
    await insert_entry(connection, "s9", "s9sys", None, None, "message", TEXTS["system"])
    await insert_entry(connection, "s9", "s9u", "s9sys", None, "message", TEXTS["user"])
    await insert_run(connection, "s9", "s9r", "s9u", None, "running")
    await connection.execute("UPDATE sessions SET active_leaf_id = 's9u' WHERE id = 's9'")
    await insert_operation(connection, "s9op", "s9", "send", "s9r")
    assert connection.in_transaction
    await fails(
        insert_operation(connection, "s9op", "s9", "send", "s9r"),
        "事务内重复 operation_id",
    )
    await connection.execute("ROLLBACK")
    assert not connection.in_transaction
    assert (await one(connection, "SELECT count(*) FROM sessions WHERE id = 's9'"))[0] == 0
    assert (await one(connection, "SELECT count(*) FROM session_entries WHERE session_id = 's9'"))[0] == 0
    assert (await one(connection, "SELECT count(*) FROM session_runs WHERE session_id = 's9'"))[0] == 0
    assert (await one(connection, "SELECT count(*) FROM session_operations WHERE session_id = 's9'"))[0] == 0


async def scenario_version_guards(root: Path) -> None:
    unsupported = root / "unsupported.db"
    database = await open_database(unsupported)
    await seed(database.connection, "g1", "", "g1r")
    await database.close()
    raw = await aiosqlite.connect(unsupported, isolation_level=None)
    await raw.execute("PRAGMA user_version = 99")
    await raw.close()
    try:
        await open_database(unsupported)
    except RuntimeError:
        pass
    else:
        raise AssertionError("不支持的版本未报错")
    raw = await aiosqlite.connect(unsupported, isolation_level=None)
    assert (await one(raw, "SELECT count(*) FROM sessions"))[0] == 1
    assert (await one(raw, "SELECT count(*) FROM session_entries"))[0] == 4
    await raw.close()

    versionless = root / "versionless.db"
    raw = await aiosqlite.connect(versionless, isolation_level=None)
    await raw.execute("CREATE TABLE legacy (x INTEGER)")
    await raw.close()
    try:
        await open_database(versionless)
    except RuntimeError:
        pass
    else:
        raise AssertionError("user_version=0 且已有应用表未报错")
    raw = await aiosqlite.connect(versionless, isolation_level=None)
    assert (await one(raw, "PRAGMA user_version"))[0] == 0
    assert (await one(raw, "SELECT count(*) FROM legacy"))[0] == 0
    await raw.close()

    damaged = root / "damaged.db"
    database = await open_database(damaged)
    await seed(database.connection, "g2", "", "g2r")
    await database.close()
    raw = await aiosqlite.connect(damaged, isolation_level=None)
    await raw.execute("DROP TRIGGER session_entries_immutable_update")
    await raw.close()
    try:
        await open_database(damaged)
    except RuntimeError:
        pass
    else:
        raise AssertionError("版本匹配但结构缺失未报错")
    raw = await aiosqlite.connect(damaged, isolation_level=None)
    assert (await one(raw, "SELECT count(*) FROM session_entries"))[0] == 4
    await raw.close()


async def scenario_commit_failure(root: Path) -> None:
    path = root / "commit-failure.db"
    database = await open_database(path)
    connection = database.connection
    try:
        await insert_session(connection, "cf")

        # 延迟外键：语句本身成功，COMMIT 时因外键未决失败。
        await connection.execute("PRAGMA defer_foreign_keys = ON")
        await connection.execute("BEGIN")
        await insert_entry(
            connection, "cf", "cf-u", None, None, "message", TEXTS["user"]
        )
        await insert_entry(
            connection, "cf-missing", "cfx", None, None, "message", TEXTS["user"]
        )
        commit_failed = False
        try:
            await connection.execute("COMMIT")
        except sqlite3.IntegrityError:
            commit_failed = True
        assert commit_failed
        if connection.in_transaction:
            await connection.execute("ROLLBACK")
        assert not connection.in_transaction
        assert (
            await one(
                connection,
                "SELECT count(*) FROM session_entries WHERE session_id = 'cf-missing'",
            )
        )[0] == 0
        assert (
            await one(
                connection,
                "SELECT count(*) FROM session_entries WHERE session_id = 'cf'",
            )
        )[0] == 0

        # RAISE(ABORT) 仅回滚语句；事务保持活动，COMMIT 成功。
        await connection.execute(
            "CREATE TRIGGER review_abort BEFORE INSERT ON session_entries "
            "BEGIN SELECT RAISE(ABORT, '注入语句失败'); END"
        )
        await connection.execute("BEGIN")
        statement_failed = False
        try:
            await insert_entry(
                connection, "cf", "cf-y", None, None, "message", TEXTS["user"]
            )
        except sqlite3.IntegrityError:
            statement_failed = True
        assert statement_failed
        assert connection.in_transaction
        await connection.execute("COMMIT")
        assert not connection.in_transaction
        await connection.execute("DROP TRIGGER review_abort")
    finally:
        await database.close()


async def scenario_migration_v1(path: Path) -> None:
    connection = await aiosqlite.connect(path, isolation_level=None)
    await connection.execute("PRAGMA foreign_keys = ON")
    await apply_schema(
        connection, _MIGRATIONS[1].read_text(encoding="utf-8"), 1
    )
    await seed(connection, "m1", "", "m1r")
    triggers = {
        row[0]
        for row in await many(
            connection, "SELECT name FROM sqlite_master WHERE type='trigger'"
        )
    }
    assert "session_entries_no_delete" in triggers
    assert (await one(connection, "PRAGMA user_version"))[0] == 1
    await connection.close()

    migrated = await open_database(path)
    connection = migrated.connection
    try:
        assert (await one(connection, "PRAGMA user_version"))[0] == SCHEMA_VERSION
        assert (await one(connection, "SELECT count(*) FROM session_entries"))[0] == 4
        triggers = {
            row[0]
            for row in await many(
                connection, "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert "session_entries_no_delete" not in triggers
        assert (
            await one(
                connection,
                "SELECT count(*) FROM session_operation_invalidations",
            )
        )[0] == 0
        # 迁移后放开删除守卫：可在受理事务内真实删除节点。
        await connection.execute(
            "DELETE FROM session_entries WHERE session_id = 'm1' AND id = 'u2'"
        )
        assert (
            await one(
                connection,
                "SELECT count(*) FROM session_entries "
                "WHERE session_id = 'm1' AND id = 'u2'",
            )
        )[0] == 0
    finally:
        await migrated.close()


async def run() -> None:
    assert default_database_path() == BACKEND / "data" / "fit-agent.db"
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=TEMP_ROOT, ignore_cleanup_errors=True) as directory:
        root = Path(directory)

        main_path = root / "main.db"
        main = await open_database(main_path)
        try:
            connection = main.connection
            await seed(connection, "s1", "", "r1")
            await seed(connection, "s2", "b", "r2")

            await scenario_structure(connection)
            assert (await one(connection, "PRAGMA foreign_keys"))[0] == 1
            await scenario_cross_session(connection)
            await scenario_illegal_values(connection)
            await scenario_pending_assistant(connection)
            await scenario_run_links(connection)
            await scenario_steering_and_operations(connection)
            await scenario_entry_integrity(connection)
            await scenario_message_roundtrip(connection)
            await scenario_transaction_rollback(connection)
            assert await many(connection, "PRAGMA foreign_key_check") == []
            session_ids = {row[0] for row in await many(connection, "SELECT id FROM sessions")}
            assert session_ids == {"s1", "s2", "rt"}
        finally:
            await main.close()

        await scenario_initialize(root / "reopen.db")
        await scenario_schema_rollback(root / "rollback.db")
        await scenario_version_guards(root)
        await scenario_migration_v1(root / "migration.db")
        await scenario_commit_failure(root)

    print(
        "会话 schema、初始化、版本检查、全部关联约束、事务回滚、"
        "延迟外键 COMMIT 失败与语句级中止自检通过"
    )


def check() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    check()
