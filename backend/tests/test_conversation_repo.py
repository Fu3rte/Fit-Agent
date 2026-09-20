# 对话历史存储层测试：真实 SQLite + 真实 migrations（禁止 Mock）。
# 依据：CHAT_HISTORY_IMPLEMENTATION_PLAN.md §3.1／§3.2 与阶段 1 验证清单；
#       Pi append-only 语义（session-manager.ts:1041-1067）。

import json
import shutil
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from itertools import product
from pathlib import Path

import aiosqlite
import pytest

from domain.conversations.repo import (
    ConversationNotFound,
    ConversationRepo,
    RunNotFound,
)
from domain.conversations.schema import (
    RUN_STATUSES,
    RUN_TRANSITIONS,
    InvalidConversationPayload,
    InvalidConversationRow,
    validate_payload,
)
from storage.db import Database
from storage.migrations import load_migrations

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "storage" / "migrations"

_CONVERSATION_TABLES = (
    "conversations",
    "conversation_entries",
    "conversation_runs",
    "conversation_run_events",
)

_CONVERSATION_INDEXES = ("idx_conversation_entries_conversation_created",)


@asynccontextmanager
async def _open_db(
    path: Path, migrations_dir: Path | None = None
) -> AsyncIterator[Database]:
    db = Database(path, migrations_dir)
    await db.open()
    try:
        yield db
    finally:
        await db.close()


@asynccontextmanager
async def _harness(tmp_path: Path) -> AsyncIterator[tuple[Database, ConversationRepo]]:
    """真实迁移到最新版本的库 + repo。"""
    async with _open_db(tmp_path / "fit_agent.db") as db:
        await db.migrate()
        yield db, ConversationRepo(db)


async def _count(db: Database, table: str) -> int:
    async def op(conn: aiosqlite.Connection) -> int:
        async with conn.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return int(row[0])

    return await db.under_lock(op)


async def _create_run(
    repo: ConversationRepo,
    db: Database,
    conversation_id: str,
    suffix: str,
    *,
    created_at: str = "2026-06-01T09:00:00+00:00",
):
    async with db.transaction() as conn:
        return await repo.begin_run_in_transaction(
            conn,
            conversation_id=conversation_id,
            run_id=f"run-{suffix}",
            thread_id=f"thread-{suffix}",
            client_request_id=f"request-{suffix}",
            entry_id=f"entry-{suffix}",
            content=f"问题 {suffix}",
            created_at=created_at,
        )


async def _drive_to(
    repo: ConversationRepo,
    db: Database,
    run_id: str,
    status: str,
    *,
    stamp: str = "2026-06-01T09:05:00+00:00",
) -> None:
    """按合法路径把 Run 推到目标状态：夹具不依赖矩阵禁止的直达迁移。"""
    async with db.transaction() as conn:
        if status == "completed":
            await repo.update_run_status_in_transaction(
                conn, run_id, status="running", updated_at=stamp
            )
            await repo.complete_run_in_transaction(
                conn,
                run_id,
                entry_id=f"{run_id}-assistant",
                content="回答",
                created_at=stamp,
            )
            return
        if status == "waiting":
            await repo.update_run_status_in_transaction(
                conn, run_id, status="running", updated_at=stamp
            )
        await repo.update_run_status_in_transaction(
            conn,
            run_id,
            status=status,
            updated_at=stamp,
            error_code="fixture_failure" if status == "failed" else None,
        )


async def test_fresh_database_migrates_to_version_four(tmp_path: Path) -> None:
    """全新库迁移到 v4；四张对话表与 entries 时间序索引就位。"""
    async with _harness(tmp_path) as (db, _repo):
        assert await db.pragma_value("user_version") == 4

        async def op(conn: aiosqlite.Connection) -> tuple[list[str], list[str]]:
            async with conn.execute(
                "SELECT type, name FROM sqlite_master WHERE name IN"
                " (SELECT name FROM sqlite_master) ORDER BY type, name"
            ) as cursor:
                rows = await cursor.fetchall()
            tables = [str(row["name"]) for row in rows if row["type"] == "table"]
            indexes = [str(row["name"]) for row in rows if row["type"] == "index"]
            return tables, indexes

        tables, indexes = await db.under_lock(op)
        assert set(_CONVERSATION_TABLES).issubset(tables)
        assert set(_CONVERSATION_INDEXES).issubset(indexes)


def test_migration_files_are_contiguous() -> None:
    """004 归入连续编号清单（编号即 user_version，不跳号）。"""
    migrations = load_migrations(MIGRATIONS_DIR)
    assert [version for version, _name, _sql in migrations] == [1, 2, 3, 4]
    assert migrations[-1][1] == "004_conversation_history.sql"


async def test_version_three_database_upgrades_without_business_data_loss(
    tmp_path: Path,
) -> None:
    """真实 v3 库（含业务数据）升级到 v4：业务行原样保留。"""
    v3_dir = tmp_path / "v3"
    v3_dir.mkdir()
    for name in ("001_initial.sql", "002_timed_sets_and_new_actions.sql", "003_rejected_plan_status.sql"):
        shutil.copy(MIGRATIONS_DIR / name, v3_dir / name)

    db_path = tmp_path / "legacy.db"
    async with _open_db(db_path, v3_dir) as legacy_db:
        assert await legacy_db.migrate() == 3
        async with legacy_db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO body_metrics (measured_on, weight_kg, body_fat_pct)"
                " VALUES ('2026-05-01', 78.5, 18.2)"
            )
            await cursor.close()
            cursor = await conn.execute(
                "INSERT INTO workout_sessions (performed_on, plan_session_id)"
                " VALUES ('2026-05-01', NULL)"
            )
            session_id = cursor.lastrowid
            await cursor.close()
            cursor = await conn.execute(
                "INSERT INTO workout_sets (workout_session_id, exercise_id, set_no,"
                " set_type, load_convention, weight_kg, reps, duration_seconds)"
                " VALUES (?, 'barbell-back-squat', 1, 'work',"
                " 'barbell_includes_bar_total', 100.0, 5, NULL)",
                (session_id,),
            )
            await cursor.close()

    async def business_rows(conn: aiosqlite.Connection) -> list[tuple[object, ...]]:
        async with conn.execute(
            "SELECT measured_on, weight_kg, body_fat_pct FROM body_metrics"
        ) as cursor:
            metrics = [tuple(row) for row in await cursor.fetchall()]
        async with conn.execute(
            "SELECT s.id, s.performed_on, s.plan_session_id, t.exercise_id, t.set_no,"
            " t.set_type, t.load_convention, t.weight_kg, t.reps, t.duration_seconds"
            " FROM workout_sessions s JOIN workout_sets t ON t.workout_session_id = s.id"
        ) as cursor:
            workouts = [tuple(row) for row in await cursor.fetchall()]
        return metrics + workouts

    async with _open_db(db_path, v3_dir) as legacy_db:
        before = await legacy_db.under_lock(business_rows)

    async with _open_db(db_path) as upgraded_db:
        assert await upgraded_db.migrate() == 4
        assert await upgraded_db.pragma_value("user_version") == 4
        after = await upgraded_db.under_lock(business_rows)
        repo = ConversationRepo(upgraded_db)
        conversation = await repo.create_conversation(
            conversation_id="conv-1",
            title="升级后可用",
            created_at="2026-06-01T09:00:00+00:00",
        )
        assert conversation.updated_at == conversation.created_at

    assert after == before
    assert before, "v3 库必须真的带有业务数据"


async def test_conversation_row_conflicts_and_checks_are_enforced(
    tmp_path: Path,
) -> None:
    """CHECK／外键／UNIQUE／级联约束在数据库层生效：非法 entry_type、非法 JSON、悬空会话引用、

    同一会话重复序号全部拒绝。
    """
    async with _harness(tmp_path) as (db, repo):
        await repo.create_conversation(
            conversation_id="conv-1",
            title="约束",
            created_at="2026-06-01T09:00:00+00:00",
        )
        with pytest.raises(sqlite3.IntegrityError):
            async with db.transaction() as conn:
                cursor = await conn.execute(
                    "INSERT INTO conversation_entries (id, conversation_id, sequence,"
                    " entry_type, payload_json, created_at) VALUES"
                    " ('bad-type', 'conv-1', 1, 'tool_call', '{}', '2026-06-01T09:00:00+00:00')"
                )
                await cursor.close()
        with pytest.raises(sqlite3.IntegrityError):
            async with db.transaction() as conn:
                cursor = await conn.execute(
                    "INSERT INTO conversation_entries (id, conversation_id, sequence,"
                    " entry_type, payload_json, created_at) VALUES"
                    " ('bad-json', 'conv-1', 1, 'message', 'not json',"
                    " '2026-06-01T09:00:00+00:00')"
                )
                await cursor.close()
        with pytest.raises(sqlite3.IntegrityError):
            async with db.transaction() as conn:
                cursor = await conn.execute(
                    "INSERT INTO conversation_entries (id, conversation_id, sequence,"
                    " entry_type, payload_json, created_at) VALUES"
                    " ('dangling', 'conv-missing', 1, 'message', '{}',"
                    " '2026-06-01T09:00:00+00:00')"
                )
                await cursor.close()
        with pytest.raises(sqlite3.IntegrityError):
            async with db.transaction() as conn:
                cursor = await conn.execute(
                    "INSERT INTO conversation_entries (id, conversation_id, sequence,"
                    " entry_type, payload_json, created_at) VALUES"
                    " ('dup-1', 'conv-1', 1, 'message', '{}', '2026-06-01T09:00:00+00:00'),"
                    " ('dup-2', 'conv-1', 1, 'message', '{}', '2026-06-01T09:00:01+00:00')"
                )
                await cursor.close()
        with pytest.raises(sqlite3.IntegrityError):
            async with db.transaction() as conn:
                cursor = await conn.execute(
                    "INSERT INTO conversation_runs (id, conversation_id, thread_id,"
                    " client_request_id, user_entry_id, assistant_entry_id, status,"
                    " error_code, created_at, updated_at) VALUES"
                    " ('run-bad', 'conv-1', 'thread-bad', 'request-bad', 'entry-bad', NULL,"
                    " 'queued', NULL, '2026-06-01T09:00:00+00:00', '2026-06-01T09:00:00+00:00')"
                )
                await cursor.close()
        assert await _count(db, "conversation_entries") == 0
        assert await _count(db, "conversation_runs") == 0


async def test_read_back_rejects_corrupted_payload(tmp_path: Path) -> None:
    """读回同样经 schema 校验：形状越界的 payload 立即报错，不静默兜底。"""
    async with _harness(tmp_path) as (db, repo):
        await repo.create_conversation(
            conversation_id="conv-1",
            title="损坏",
            created_at="2026-06-01T09:00:00+00:00",
        )
        async with db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO conversation_entries (id, conversation_id, sequence,"
                " entry_type, payload_json, created_at) VALUES"
                " ('entry-x', 'conv-1', 1, 'message',"
                ' \'{"role": "robot", "content": "x", "status": "complete", "run_id": "r"}\','
                " '2026-06-01T09:00:00+00:00')"
            )
            await cursor.close()
        with pytest.raises(InvalidConversationRow):
            await repo.list_entries("conv-1")


async def test_create_list_read_delete_cascades(tmp_path: Path) -> None:
    """创建、列表（updated_at 降序）、读取、删除级联清理 entries／runs／events。"""
    async with _harness(tmp_path) as (db, repo):
        await repo.create_conversation(
            conversation_id="conv-old",
            title="旧",
            created_at="2026-06-01T09:00:00+00:00",
        )
        await repo.create_conversation(
            conversation_id="conv-new",
            title="新",
            created_at="2026-06-02T09:00:00+00:00",
        )
        await _create_run(repo, db, "conv-old", "a", created_at="2026-06-03T09:00:00+00:00")
        async with db.transaction() as conn:
            await repo.append_event_in_transaction(
                conn,
                run_id="run-a",
                sequence=0,
                event_type="node",
                payload={"name": "chat"},
                created_at="2026-06-03T09:00:01+00:00",
            )
            await repo.append_entry_in_transaction(
                conn,
                conversation_id="conv-old",
                entry_id="entry-compaction",
                entry_type="compaction",
                payload={
                    "summary": "摘要",
                    "first_kept_entry_id": "entry-a",
                    "tokens_before": 1200,
                },
                created_at="2026-06-03T09:00:02+00:00",
            )

        listed = await repo.list_conversations()
        assert [item.id for item in listed] == ["conv-old", "conv-new"]
        assert (await repo.read_conversation("conv-new")).title == "新"  # type: ignore[union-attr]
        assert await repo.read_conversation("conv-missing") is None
        old = await repo.read_conversation("conv-old")
        assert old is not None and old.updated_at == "2026-06-03T09:00:02+00:00"

        assert await repo.delete_conversation("conv-old") is True
        assert await repo.delete_conversation("conv-old") is False
        assert await repo.read_conversation("conv-old") is None
        assert await _count(db, "conversation_entries") == 0
        assert await _count(db, "conversation_runs") == 0
        assert await _count(db, "conversation_run_events") == 0
        assert [item.id for item in await repo.list_conversations()] == ["conv-new"]


async def test_append_entry_advances_sequence_and_reads_in_order(
    tmp_path: Path,
) -> None:
    """append-only：会话内序号从 1 递增，追加后 ``updated_at`` 推进，Entry 按序号读取。"""
    async with _harness(tmp_path) as (db, repo):
        await repo.create_conversation(
            conversation_id="conv-1",
            title="线性",
            created_at="2026-06-01T09:00:00+00:00",
        )
        async with db.transaction() as conn:
            first = await repo.append_entry_in_transaction(
                conn,
                conversation_id="conv-1",
                entry_id="entry-1",
                entry_type="message",
                payload={
                    "role": "user",
                    "content": "第一句",
                    "status": "complete",
                    "run_id": "run-1",
                },
                created_at="2026-06-01T09:00:01+00:00",
            )
            second = await repo.append_entry_in_transaction(
                conn,
                conversation_id="conv-1",
                entry_id="entry-2",
                entry_type="message",
                payload={
                    "role": "assistant",
                    "content": "第二句",
                    "status": "complete",
                    "run_id": "run-1",
                    "usage": {"input": 10, "output": 20},
                    "provider": "openai_compatible",
                    "model": "m",
                },
                created_at="2026-06-01T09:00:02+00:00",
            )

        assert first.sequence == 1
        assert second.sequence == 2
        conversation = await repo.read_conversation("conv-1")
        assert conversation is not None
        assert conversation.updated_at == "2026-06-01T09:00:02+00:00"
        entries = await repo.list_entries("conv-1")
        assert [entry.id for entry in entries] == ["entry-1", "entry-2"]
        assert [entry.sequence for entry in entries] == [1, 2]
        assert entries[1].payload["role"] == "assistant"


async def test_append_entry_rejects_unknown_conversation_and_invalid_payload(
    tmp_path: Path,
) -> None:
    """未知会话与越界载荷即拒绝，且同事务不留半条记录。"""
    async with _harness(tmp_path) as (db, repo):
        await repo.create_conversation(
            conversation_id="conv-1",
            title="并发",
            created_at="2026-06-01T09:00:00+00:00",
        )
        async with db.transaction() as conn:
            await repo.append_entry_in_transaction(
                conn,
                conversation_id="conv-1",
                entry_id="entry-1",
                entry_type="message",
                payload={
                    "role": "user",
                    "content": "第一句",
                    "status": "complete",
                    "run_id": "run-1",
                },
                created_at="2026-06-01T09:00:01+00:00",
            )

        with pytest.raises(ConversationNotFound):
            async with db.transaction() as conn:
                await repo.append_entry_in_transaction(
                    conn,
                    conversation_id="conv-missing",
                    entry_id="entry-4",
                    entry_type="message",
                    payload={
                        "role": "user",
                        "content": "无会话",
                        "status": "complete",
                        "run_id": "run-4",
                    },
                    created_at="2026-06-01T09:00:05+00:00",
                )

        with pytest.raises(InvalidConversationPayload):
            async with db.transaction() as conn:
                await repo.append_entry_in_transaction(
                    conn,
                    conversation_id="conv-1",
                    entry_id="entry-5",
                    entry_type="message",
                    payload={
                        "role": "user",
                        "content": "状态越界",
                        "status": "queued",
                        "run_id": "run-5",
                    },
                    created_at="2026-06-01T09:00:06+00:00",
                )

        assert await _count(db, "conversation_entries") == 1
        assert (await repo.list_entries("conv-1"))[0].sequence == 1
        conversation = await repo.read_conversation("conv-1")
        assert conversation is not None
        assert conversation.updated_at == "2026-06-01T09:00:01+00:00"


async def test_same_client_request_id_creates_one_run_and_one_user_entry(
    tmp_path: Path,
) -> None:
    """``client_request_id`` 全局幂等：重复请求返回同一个 Run 与同一条用户 Entry。"""
    async with _harness(tmp_path) as (db, repo):
        await repo.create_conversation(
            conversation_id="conv-1",
            title="幂等",
            created_at="2026-06-01T09:00:00+00:00",
        )
        first_run, first_entry = await _create_run(repo, db, "conv-1", "dup")
        second_run, second_entry = await _create_run(repo, db, "conv-1", "dup")

        assert first_run.id == second_run.id == "run-dup"
        assert first_entry.id == second_entry.id == "entry-dup"
        assert first_run.status == "pending"
        assert await _count(db, "conversation_runs") == 1
        assert await _count(db, "conversation_entries") == 1
        assert (await repo.read_run_by_client_request_id("request-dup")) == first_run

        await repo.create_conversation(
            conversation_id="conv-2",
            title="另一会话",
            created_at="2026-06-01T09:00:00+00:00",
        )
        with pytest.raises(ValueError):
            async with db.transaction() as conn:
                await repo.begin_run_in_transaction(
                    conn,
                    conversation_id="conv-2",
                    run_id="run-dup",
                    thread_id="thread-dup",
                    client_request_id="request-dup",
                    entry_id="entry-dup-2",
                    content="别的会话复用同一幂等键",
                    created_at="2026-06-01T09:01:00+00:00",
                )
        assert await _count(db, "conversation_runs") == 1
        assert await _count(db, "conversation_entries") == 1

        with pytest.raises(sqlite3.IntegrityError):
            async with db.transaction() as conn:
                await repo.begin_run_in_transaction(
                    conn,
                    conversation_id="conv-1",
                    run_id="run-other",
                    thread_id="thread-dup",
                    client_request_id="request-other",
                    entry_id="entry-other",
                    content="thread 复用",
                    created_at="2026-06-01T09:02:00+00:00",
                )
        assert await _count(db, "conversation_runs") == 1


async def test_run_events_keep_order_and_reject_duplicate_sequence(
    tmp_path: Path,
) -> None:
    """Event 由 ``(run_id, sequence)`` 保序；重复序号被拒绝且不留半条记录。"""
    async with _harness(tmp_path) as (db, repo):
        await repo.create_conversation(
            conversation_id="conv-1",
            title="事件",
            created_at="2026-06-01T09:00:00+00:00",
        )
        await _create_run(repo, db, "conv-1", "e")
        async with db.transaction() as conn:
            for sequence, name in ((0, "router"), (1, "chat")):
                await repo.append_event_in_transaction(
                    conn,
                    run_id="run-e",
                    sequence=sequence,
                    event_type="node",
                    payload={"name": name},
                    created_at=f"2026-06-01T09:00:0{sequence + 1}+00:00",
                )

        with pytest.raises(sqlite3.IntegrityError):
            async with db.transaction() as conn:
                await repo.append_event_in_transaction(
                    conn,
                    run_id="run-e",
                    sequence=1,
                    event_type="message",
                    payload={"text": "重复"},
                    created_at="2026-06-01T09:00:05+00:00",
                )

        with pytest.raises(sqlite3.IntegrityError):
            async with db.transaction() as conn:
                await repo.append_event_in_transaction(
                    conn,
                    run_id="run-missing",
                    sequence=0,
                    event_type="message",
                    payload={"text": "悬空 run"},
                    created_at="2026-06-01T09:00:06+00:00",
                )

        events = await repo.list_run_events("run-e")
        assert [(event.sequence, event.payload["name"]) for event in events] == [
            (0, "router"),
            (1, "chat"),
        ]
        assert await _count(db, "conversation_run_events") == 2


async def test_run_status_update_is_scoped_to_existing_run(tmp_path: Path) -> None:
    """状态更新只影响目标 Run，未知 Run 即 :class:`RunNotFound`。"""
    async with _harness(tmp_path) as (db, repo):
        await repo.create_conversation(
            conversation_id="conv-1",
            title="状态",
            created_at="2026-06-01T09:00:00+00:00",
        )
        await _create_run(repo, db, "conv-1", "s")
        async with db.transaction() as conn:
            updated = await repo.update_run_status_in_transaction(
                conn, "run-s", status="running", updated_at="2026-06-01T09:00:10+00:00"
            )
        assert updated.status == "running" and updated.updated_at == "2026-06-01T09:00:10+00:00"
        assert updated.error_code is None

        with pytest.raises(InvalidConversationPayload):
            async with db.transaction() as conn:
                await repo.update_run_status_in_transaction(
                    conn, "run-s", status="queued", updated_at="2026-06-01T09:00:11+00:00"
                )
        with pytest.raises(RunNotFound):
            async with db.transaction() as conn:
                await repo.update_run_status_in_transaction(
                    conn, "run-missing", status="failed", updated_at="2026-06-01T09:00:12+00:00"
                )
        with pytest.raises(InvalidConversationPayload):
            async with db.transaction() as conn:
                await repo.update_run_status_in_transaction(
                    conn, "run-s", status="failed", updated_at="2026-06-01T09:00:13+00:00"
                )
        still_running = await repo.read_run("run-s")
        assert still_running is not None
        assert still_running.status == "running"
        assert still_running.updated_at == "2026-06-01T09:00:10+00:00"
        assert still_running.error_code is None

        async with db.transaction() as conn:
            failed = await repo.update_run_status_in_transaction(
                conn,
                "run-s",
                status="failed",
                updated_at="2026-06-01T09:00:14+00:00",
                error_code="model_unavailable",
            )
        assert failed.status == "failed"
        assert failed.error_code == "model_unavailable"
        assert failed.updated_at == "2026-06-01T09:00:14+00:00"
        run = await repo.read_run("run-s")
        assert run is not None and run.status == "failed"


async def test_assistant_complete_entry_and_run_status_commit_together(
    tmp_path: Path,
) -> None:
    """Assistant 完整 Entry 与 Run ``completed`` 同事务提交，序号接着用户 Entry。"""
    async with _harness(tmp_path) as (db, repo):
        await repo.create_conversation(
            conversation_id="conv-1",
            title="完成",
            created_at="2026-06-01T09:00:00+00:00",
        )
        await _create_run(repo, db, "conv-1", "c")
        await _drive_to(repo, db, "run-c", "running")
        async with db.transaction() as conn:
            run, entry = await repo.complete_run_in_transaction(
                conn,
                "run-c",
                entry_id="entry-c-assistant",
                content="回答",
                created_at="2026-06-01T09:00:20+00:00",
                usage={"input": 5, "output": 7},
                provider="openai_compatible",
                model="m",
            )
        assert run.status == "completed"
        assert run.assistant_entry_id == "entry-c-assistant"
        assert entry.sequence == 2
        assert entry.payload["status"] == "complete"
        assert entry.payload["usage"] == {"input": 5, "output": 7}
        entries = await repo.list_entries("conv-1")
        assert [item.id for item in entries] == ["entry-c", "entry-c-assistant"]
        assert entry.entry_type == "message" and entry.payload["role"] == "assistant"
        assert entry.payload["run_id"] == "run-c"
        assert run.assistant_entry_id == entries[-1].id

        with pytest.raises(InvalidConversationPayload):
            async with db.transaction() as conn:
                await repo.complete_run_in_transaction(
                    conn,
                    "run-c",
                    entry_id="entry-c-second",
                    content="第二条回答",
                    created_at="2026-06-01T09:00:21+00:00",
                )
        assert await _count(db, "conversation_entries") == 2
        unchanged_conversation = await repo.read_conversation("conv-1")
        unchanged_run = await repo.read_run("run-c")
        assert unchanged_conversation is not None
        assert unchanged_conversation.updated_at == "2026-06-01T09:00:20+00:00"
        assert unchanged_run == run


async def test_waiting_event_and_run_status_commit_together(tmp_path: Path) -> None:
    """``waiting`` Event 与 Run ``waiting`` 同事务提交。"""
    async with _harness(tmp_path) as (db, repo):
        await repo.create_conversation(
            conversation_id="conv-1",
            title="等待",
            created_at="2026-06-01T09:00:00+00:00",
        )
        await _create_run(repo, db, "conv-1", "w")
        await _drive_to(repo, db, "run-w", "running")
        async with db.transaction() as conn:
            run, event = await repo.update_run_with_event_in_transaction(
                conn,
                "run-w",
                event_type="waiting",
                sequence=0,
                payload={"draft_plan_id": 7},
                status="waiting",
                created_at="2026-06-01T09:00:30+00:00",
            )
        assert run.status == "waiting"
        assert event.event_type == "waiting"
        assert event.payload == {"draft_plan_id": 7}
        stored = await repo.read_run("run-w")
        assert stored is not None and stored.status == "waiting"
        events = await repo.list_run_events("run-w")
        assert [(item.sequence, item.event_type) for item in events] == [(0, "waiting")]

        with pytest.raises(sqlite3.IntegrityError):
            async with db.transaction() as conn:
                await repo.update_run_with_event_in_transaction(
                    conn,
                    "run-w",
                    event_type="waiting",
                    sequence=0,
                    payload={"draft_plan_id": 8},
                    status="waiting",
                    created_at="2026-06-01T09:00:31+00:00",
                )
        assert await _count(db, "conversation_run_events") == 1
        still_waiting = await repo.read_run("run-w")
        assert still_waiting is not None
        assert still_waiting.updated_at == "2026-06-01T09:00:30+00:00"

        with pytest.raises(InvalidConversationPayload):
            async with db.transaction() as conn:
                await repo.complete_run_in_transaction(
                    conn,
                    "run-w",
                    entry_id="entry-w-bad",
                    content="",
                    created_at="2026-06-01T09:00:32+00:00",
                )
        assert await _count(db, "conversation_entries") == 1
        assert await repo.read_run("run-w") == still_waiting


async def test_latest_compaction_is_read_by_sequence(tmp_path: Path) -> None:
    """最新 compaction Entry 读取：返回序号最大的一条，并保留全部原始 Entry。"""
    async with _harness(tmp_path) as (db, repo):
        await repo.create_conversation(
            conversation_id="conv-1",
            title="压缩",
            created_at="2026-06-01T09:00:00+00:00",
        )
        assert await repo.read_latest_compaction("conv-1") is None
        async with db.transaction() as conn:
            await repo.append_entry_in_transaction(
                conn,
                conversation_id="conv-1",
                entry_id="entry-1",
                entry_type="message",
                payload={
                    "role": "user",
                    "content": "第一句",
                    "status": "complete",
                    "run_id": "run-1",
                },
                created_at="2026-06-01T09:00:01+00:00",
            )
            await repo.append_entry_in_transaction(
                conn,
                conversation_id="conv-1",
                entry_id="entry-compaction-1",
                entry_type="compaction",
                payload={
                    "summary": "第一次摘要",
                    "first_kept_entry_id": "entry-1",
                    "tokens_before": 900,
                    "usage": {"input": 1, "output": 2},
                },
                created_at="2026-06-01T09:00:02+00:00",
            )
            await repo.append_entry_in_transaction(
                conn,
                conversation_id="conv-1",
                entry_id="entry-compaction-2",
                entry_type="compaction",
                payload={
                    "summary": "第二次摘要",
                    "first_kept_entry_id": "entry-1",
                    "tokens_before": 1400,
                },
                created_at="2026-06-01T09:00:03+00:00",
            )

        latest = await repo.read_latest_compaction("conv-1")
        assert latest is not None
        assert latest.id == "entry-compaction-2"
        assert latest.sequence == 3
        assert latest.payload["summary"] == "第二次摘要"
        entries = await repo.list_entries("conv-1")
        assert [item.id for item in entries] == [
            "entry-1",
            "entry-compaction-1",
            "entry-compaction-2",
        ]


async def test_converge_unfinished_runs_only_touches_pending_and_running(
    tmp_path: Path,
) -> None:
    """启动收敛：遗留 ``pending``／``running`` 标记 failed，其余状态与已提交 Event 不变。"""
    async with _harness(tmp_path) as (db, repo):
        await repo.create_conversation(
            conversation_id="conv-1",
            title="收敛",
            created_at="2026-06-01T09:00:00+00:00",
        )
        targets = {
            "pending": "pending",
            "running": "running",
            "waiting": "waiting",
            "completed": "completed",
            "failed": "failed",
            "cancelled": "cancelled",
        }
        for suffix, status in targets.items():
            await _create_run(repo, db, "conv-1", suffix)
            if status != "pending":
                await _drive_to(repo, db, f"run-{suffix}", status)
        async with db.transaction() as conn:
            await repo.append_event_in_transaction(
                conn,
                run_id="run-pending",
                sequence=0,
                event_type="message",
                payload={"text": "已推送片段"},
                created_at="2026-06-01T09:06:00+00:00",
            )

        converged = await repo.converge_unfinished_runs(
            error_code="server_restart", updated_at="2026-06-02T00:00:00+00:00"
        )
        assert converged == 2

        statuses = {}
        for suffix, status in targets.items():
            run = await repo.read_run(f"run-{suffix}")
            assert run is not None
            statuses[status] = run.status
            if status in ("pending", "running"):
                assert run.error_code == "server_restart"
                assert run.updated_at == "2026-06-02T00:00:00+00:00"
            elif status == "failed":
                assert run.error_code == "fixture_failure"
                assert run.updated_at == "2026-06-01T09:05:00+00:00"
            else:
                assert run.updated_at == "2026-06-01T09:05:00+00:00"
        assert statuses == {
            "pending": "failed",
            "running": "failed",
            "waiting": "waiting",
            "completed": "completed",
            "failed": "failed",
            "cancelled": "cancelled",
        }
        assert len(await repo.list_run_events("run-pending")) == 1

        assert (
            await repo.converge_unfinished_runs(
                error_code="server_restart", updated_at="2026-06-02T00:00:01+00:00"
            )
            == 0
        )


_LEGAL_CONFIRMATIONS: tuple[tuple[str, dict[str, object]], ...] = (
    (
        "plan_confirmed",
        {
            "action": "plan_confirmed",
            "run_id": "run-1",
            "draft_plan_id": 7,
            "text": "已确认计划 #7",
        },
    ),
    (
        "plan_rejected",
        {
            "action": "plan_rejected",
            "run_id": "run-1",
            "draft_plan_id": 7,
            "text": "已拒绝计划 #7",
        },
    ),
    (
        "workout_confirmed",
        {
            "action": "workout_confirmed",
            "run_id": "run-1",
            "workout_session_id": 3,
            "text": "已确认训练记录 #3",
        },
    ),
)

_ILLEGAL_CONFIRMATIONS: tuple[tuple[str, dict[str, object]], ...] = (
    (
        "run_id 缺失",
        {"action": "plan_confirmed", "draft_plan_id": 7, "text": "确认"},
    ),
    (
        "run_id 空串",
        {
            "action": "plan_confirmed",
            "run_id": "",
            "draft_plan_id": 7,
            "text": "确认",
        },
    ),
    (
        "plan_confirmed 缺 draft_plan_id",
        {"action": "plan_confirmed", "run_id": "run-1", "text": "确认"},
    ),
    (
        "plan_confirmed draft_plan_id 非正整数",
        {
            "action": "plan_confirmed",
            "run_id": "run-1",
            "draft_plan_id": 0,
            "text": "确认",
        },
    ),
    (
        "plan_confirmed 混入 workout_session_id",
        {
            "action": "plan_confirmed",
            "run_id": "run-1",
            "draft_plan_id": 7,
            "workout_session_id": 3,
            "text": "确认",
        },
    ),
    (
        "plan_rejected 混入 workout_session_id",
        {
            "action": "plan_rejected",
            "run_id": "run-1",
            "draft_plan_id": 7,
            "workout_session_id": 3,
            "text": "拒绝",
        },
    ),
    (
        "workout_confirmed 缺 workout_session_id",
        {"action": "workout_confirmed", "run_id": "run-1", "text": "记录"},
    ),
    (
        "workout_confirmed workout_session_id 非正整数",
        {
            "action": "workout_confirmed",
            "run_id": "run-1",
            "workout_session_id": -1,
            "text": "记录",
        },
    ),
    (
        "workout_confirmed 混入 draft_plan_id",
        {
            "action": "workout_confirmed",
            "run_id": "run-1",
            "workout_session_id": 3,
            "draft_plan_id": 7,
            "text": "记录",
        },
    ),
    (
        "text 缺失",
        {"action": "workout_confirmed", "run_id": "run-1", "workout_session_id": 3},
    ),
    (
        "未知字段",
        {
            "action": "workout_confirmed",
            "run_id": "run-1",
            "workout_session_id": 3,
            "text": "记录",
            "extra": 1,
        },
    ),
    (
        "action 越界",
        {
            "action": "plan_archived",
            "run_id": "run-1",
            "draft_plan_id": 7,
            "text": "归档",
        },
    ),
)


def test_confirmation_payload_binds_identity_per_action() -> None:
    """三类确认动作各自绑定唯一业务身份：合法三例原样通过，缺失／错配／混入一律拒绝。"""
    for label, payload in _LEGAL_CONFIRMATIONS:
        assert validate_payload("confirmation", payload) == payload, label
    for _label, payload in _ILLEGAL_CONFIRMATIONS:
        with pytest.raises(InvalidConversationPayload):
            validate_payload("confirmation", payload)


async def test_confirmation_entry_identity_is_validated_on_write(tmp_path: Path) -> None:
    """确认 Entry 按动作绑定身份写入；非法载荷不落库。"""
    async with _harness(tmp_path) as (db, repo):
        await repo.create_conversation(
            conversation_id="conv-1",
            title="确认",
            created_at="2026-06-01T09:00:00+00:00",
        )
        action, payload = _LEGAL_CONFIRMATIONS[0]
        async with db.transaction() as conn:
            entry = await repo.append_entry_in_transaction(
                conn,
                conversation_id="conv-1",
                entry_id="entry-confirmation",
                entry_type="confirmation",
                payload=payload,
                created_at="2026-06-01T09:00:01+00:00",
            )
        assert entry.payload["action"] == action
        assert entry.sequence == 1

        for label, illegal in _ILLEGAL_CONFIRMATIONS:
            with pytest.raises(InvalidConversationPayload):
                async with db.transaction() as conn:
                    await repo.append_entry_in_transaction(
                        conn,
                        conversation_id="conv-1",
                        entry_id=f"entry-{label}",
                        entry_type="confirmation",
                        payload=illegal,
                        created_at="2026-06-01T09:00:02+00:00",
                    )
        assert await _count(db, "conversation_entries") == 1


async def test_confirmation_append_is_idempotent_per_run_action_and_target(
    tmp_path: Path,
) -> None:
    """确认 Entry 的幂等键 = 来源 Run ＋ 动作 ＋ 业务身份：重复调用不追加，换业务身份才追加。"""
    async with _harness(tmp_path) as (db, repo):
        await repo.create_conversation(
            conversation_id="conv-1",
            title="确认幂等",
            created_at="2026-06-01T09:00:00+00:00",
        )
        await _create_run(repo, db, "conv-1", "confirmation")

        async def _append(entry_id: str, plan_id: int, stamp: str):
            async with db.transaction() as conn:
                return await repo.append_confirmation_once_in_transaction(
                    conn,
                    conversation_id="conv-1",
                    run_id="run-confirmation",
                    entry_id=entry_id,
                    action="plan_confirmed",
                    text="用户已确认训练计划",
                    draft_plan_id=plan_id,
                    created_at=stamp,
                )

        first = await _append("entry-confirm-1", 7, "2026-06-01T09:01:00+00:00")
        repeated = await _append("entry-confirm-2", 7, "2026-06-01T09:02:00+00:00")
        other_target = await _append("entry-confirm-3", 8, "2026-06-01T09:03:00+00:00")

        assert first is not None and first.payload["draft_plan_id"] == 7
        assert repeated is None
        assert other_target is not None
        confirmations = await repo.read_confirmations(
            "run-confirmation", "plan_confirmed"
        )
        assert [entry.id for entry in confirmations] == [
            "entry-confirm-1",
            "entry-confirm-3",
        ]
        assert await repo.read_confirmations("run-confirmation", "workout_confirmed") == ()
        assert await _count(db, "conversation_entries") == 3


async def test_corrupted_rows_raise_row_errors(tmp_path: Path) -> None:
    """raw SQL 绕过 CHECK 造出的越界行读回即 InvalidConversationRow，写路径仍用 Payload 错误。"""
    async with _harness(tmp_path) as (db, repo):
        await repo.create_conversation(
            conversation_id="conv-1",
            title="损坏行",
            created_at="2026-06-01T09:00:00+00:00",
        )
        async with db.transaction() as conn:
            cursor = await conn.execute("PRAGMA ignore_check_constraints=ON")
            await cursor.close()
            cursor = await conn.execute(
                "INSERT INTO conversation_entries (id, conversation_id, sequence, entry_type,"
                " payload_json, created_at) VALUES ('entry-bad-type', 'conv-1', 1, 'tool_call',"
                ' \'{"role": "user", "content": "x", "status": "complete", "run_id": "r"}\', '
                "'2026-06-01T09:00:01+00:00')"
            )
            await cursor.close()
            cursor = await conn.execute(
                "INSERT INTO conversation_runs (id, conversation_id, thread_id, client_request_id,"
                " user_entry_id, assistant_entry_id, status, error_code, created_at, updated_at)"
                " VALUES ('run-bad-status', 'conv-1', 'thread-bad', 'request-bad', 'entry-bad-type',"
                " NULL, 'queued', NULL, '2026-06-01T09:00:01+00:00', '2026-06-01T09:00:01+00:00')"
            )
            await cursor.close()
            cursor = await conn.execute("PRAGMA ignore_check_constraints=OFF")
            await cursor.close()

        with pytest.raises(InvalidConversationRow):
            await repo.list_entries("conv-1")
        with pytest.raises(InvalidConversationRow):
            await repo.read_run("run-bad-status")
        with pytest.raises(InvalidConversationRow):
            await repo.read_run_by_client_request_id("request-bad")

        with pytest.raises(sqlite3.IntegrityError):
            async with db.transaction() as conn:
                cursor = await conn.execute(
                    "INSERT INTO conversation_entries (id, conversation_id, sequence, entry_type,"
                    " payload_json, created_at) VALUES ('entry-bad-2', 'conv-1', 1,"
                    " 'tool_call', '{}', '2026-06-01T09:00:02+00:00')"
                )
                await cursor.close()


async def test_malformed_payload_json_raises_json_decode_error_in_place(
    tmp_path: Path,
) -> None:
    """绕过 CHECK 落库的畸形 JSON：Entry／Event 读回原位抛 JSONDecodeError，无 catch／fallback。"""
    async with _harness(tmp_path) as (db, repo):
        await repo.create_conversation(
            conversation_id="conv-1",
            title="畸形 JSON",
            created_at="2026-06-01T09:00:00+00:00",
        )
        await _create_run(repo, db, "conv-1", "j")
        stamp = "2026-06-01T09:00:01+00:00"
        async with db.transaction() as conn:
            cursor = await conn.execute("PRAGMA ignore_check_constraints=ON")
            await cursor.close()
            cursor = await conn.execute(
                "INSERT INTO conversation_entries (id, conversation_id, sequence, entry_type,"
                " payload_json, created_at) VALUES (?, ?, 2, 'message', ?, ?)",
                ("entry-broken-json", "conv-1", '{"role": "user"', stamp),
            )
            await cursor.close()
            cursor = await conn.execute(
                "INSERT INTO conversation_run_events (run_id, sequence, event_type,"
                " payload_json, created_at) VALUES ('run-j', 1, 'message', ?, ?)",
                ("not json", stamp),
            )
            await cursor.close()
            cursor = await conn.execute("PRAGMA ignore_check_constraints=OFF")
            await cursor.close()

        async def read_pragma(conn: aiosqlite.Connection) -> int:
            async with conn.execute("PRAGMA ignore_check_constraints") as pragma_cursor:
                pragma_row = await pragma_cursor.fetchone()
            assert pragma_row is not None
            return int(pragma_row[0])

        assert await db.under_lock(read_pragma) == 0
        with pytest.raises(json.JSONDecodeError):
            await repo.list_entries("conv-1")
        with pytest.raises(json.JSONDecodeError):
            await repo.list_run_events("run-j")


_TRANSITION_PAIRS: tuple[tuple[str, str], ...] = tuple(product(RUN_STATUSES, RUN_STATUSES))


@pytest.mark.parametrize(("source", "target"), _TRANSITION_PAIRS)
async def test_run_transition_matrix_is_enforced(
    tmp_path: Path, source: str, target: str
) -> None:
    """矩阵逐格：合法单向迁移生效，同态／终态复活／越级迁移一律拒绝且 Run 不变。"""
    async with _harness(tmp_path) as (db, repo):
        await repo.create_conversation(
            conversation_id="conv-1",
            title="矩阵",
            created_at="2026-06-01T09:00:00+00:00",
        )
        await _create_run(repo, db, "conv-1", "m")
        if source != "pending":
            await _drive_to(repo, db, "run-m", source)
        before = await repo.read_run("run-m")
        assert before is not None
        assert before.status == source
        entries_before = await _count(db, "conversation_entries")
        legal = target in RUN_TRANSITIONS[source]
        error_code = "matrix_failure" if target == "failed" else None
        stamp = "2026-06-01T09:10:00+00:00"

        if target == "completed" and legal:
            async with db.transaction() as conn:
                run, entry = await repo.complete_run_in_transaction(
                    conn,
                    "run-m",
                    entry_id="entry-m-assistant",
                    content="回答",
                    created_at=stamp,
                )
            assert run.status == "completed"
            assert run.assistant_entry_id == "entry-m-assistant"
            assert entry.sequence == 2
            return
        if legal:
            async with db.transaction() as conn:
                updated = await repo.update_run_status_in_transaction(
                    conn, "run-m", status=target, updated_at=stamp, error_code=error_code
                )
            assert updated.status == target
            assert updated.error_code == error_code
            return

        with pytest.raises(InvalidConversationPayload):
            async with db.transaction() as conn:
                await repo.update_run_status_in_transaction(
                    conn, "run-m", status=target, updated_at=stamp, error_code=error_code
                )
        if "completed" not in RUN_TRANSITIONS[source]:
            with pytest.raises(InvalidConversationPayload):
                async with db.transaction() as conn:
                    await repo.complete_run_in_transaction(
                        conn,
                        "run-m",
                        entry_id="entry-m-assistant",
                        content="回答",
                        created_at=stamp,
                    )
        assert await repo.read_run("run-m") == before
        assert await _count(db, "conversation_entries") == entries_before


async def test_generic_update_cannot_produce_completed_without_assistant(
    tmp_path: Path,
) -> None:
    """普通状态更新不得生成无 Assistant 的 completed；complete_run 是唯一合法完成路径。"""
    async with _harness(tmp_path) as (db, repo):
        await repo.create_conversation(
            conversation_id="conv-1",
            title="完成",
            created_at="2026-06-01T09:00:00+00:00",
        )
        await _create_run(repo, db, "conv-1", "g")
        await _drive_to(repo, db, "run-g", "running")
        before = await repo.read_run("run-g")
        assert before is not None and before.assistant_entry_id is None

        with pytest.raises(InvalidConversationPayload):
            async with db.transaction() as conn:
                await repo.update_run_status_in_transaction(
                    conn, "run-g", status="completed", updated_at="2026-06-01T09:10:00+00:00"
                )
        assert await repo.read_run("run-g") == before

        async with db.transaction() as conn:
            run, _entry = await repo.complete_run_in_transaction(
                conn,
                "run-g",
                entry_id="entry-g-assistant",
                content="回答",
                created_at="2026-06-01T09:10:01+00:00",
            )
        assert run.status == "completed"
        assert run.assistant_entry_id == "entry-g-assistant"
        stored = await repo.read_run("run-g")
        assert stored is not None and stored.assistant_entry_id == "entry-g-assistant"


async def test_error_code_is_bound_to_failed_status(tmp_path: Path) -> None:
    """error_code 只属于 failed：failed 必带，其他目标状态不得携带；终态不可复活。"""
    async with _harness(tmp_path) as (db, repo):
        await repo.create_conversation(
            conversation_id="conv-1",
            title="错误码",
            created_at="2026-06-01T09:00:00+00:00",
        )
        pending_run, _entry = await _create_run(repo, db, "conv-1", "f")

        for error_code in (None, ""):
            with pytest.raises(InvalidConversationPayload):
                async with db.transaction() as conn:
                    await repo.update_run_status_in_transaction(
                        conn,
                        "run-f",
                        status="failed",
                        updated_at="2026-06-01T09:10:00+00:00",
                        error_code=error_code,
                    )
        with pytest.raises(InvalidConversationPayload):
            async with db.transaction() as conn:
                await repo.update_run_status_in_transaction(
                    conn,
                    "run-f",
                    status="running",
                    updated_at="2026-06-01T09:10:00+00:00",
                    error_code="not_allowed",
                )
        with pytest.raises(InvalidConversationPayload):
            async with db.transaction() as conn:
                await repo.update_run_status_in_transaction(
                    conn,
                    "run-f",
                    status="cancelled",
                    updated_at="2026-06-01T09:10:00+00:00",
                    error_code="not_allowed",
                )
        assert await repo.read_run("run-f") == pending_run

        async with db.transaction() as conn:
            failed = await repo.update_run_status_in_transaction(
                conn,
                "run-f",
                status="failed",
                updated_at="2026-06-01T09:10:01+00:00",
                error_code="model_unavailable",
            )
        assert failed.status == "failed"
        assert failed.error_code == "model_unavailable"
        with pytest.raises(InvalidConversationPayload):
            async with db.transaction() as conn:
                await repo.update_run_status_in_transaction(
                    conn,
                    "run-f",
                    status="cancelled",
                    updated_at="2026-06-01T09:10:02+00:00",
                )
        assert await repo.read_run("run-f") == failed


async def test_compaction_first_kept_entry_must_belong_to_conversation(
    tmp_path: Path,
) -> None:
    """compaction 边界必须是本会话已落库的 Entry：悬空／跨会话 ID 一律拒绝并完整回滚。"""
    async with _harness(tmp_path) as (db, repo):
        await repo.create_conversation(
            conversation_id="conv-1",
            title="边界",
            created_at="2026-06-01T09:00:00+00:00",
        )
        await repo.create_conversation(
            conversation_id="conv-2",
            title="另一会话",
            created_at="2026-06-01T09:00:00+00:00",
        )
        async with db.transaction() as conn:
            await repo.append_entry_in_transaction(
                conn,
                conversation_id="conv-1",
                entry_id="entry-1",
                entry_type="message",
                payload={
                    "role": "user",
                    "content": "第一句",
                    "status": "complete",
                    "run_id": "run-1",
                },
                created_at="2026-06-01T09:00:01+00:00",
            )
            await repo.append_entry_in_transaction(
                conn,
                conversation_id="conv-1",
                entry_id="entry-2",
                entry_type="message",
                payload={
                    "role": "assistant",
                    "content": "第二句",
                    "status": "complete",
                    "run_id": "run-1",
                },
                created_at="2026-06-01T09:00:02+00:00",
            )
            await repo.append_entry_in_transaction(
                conn,
                conversation_id="conv-2",
                entry_id="entry-other",
                entry_type="message",
                payload={
                    "role": "user",
                    "content": "另一会话的第一句",
                    "status": "complete",
                    "run_id": "run-2",
                },
                created_at="2026-06-01T09:00:03+00:00",
            )

        with pytest.raises(InvalidConversationPayload):
            async with db.transaction() as conn:
                await repo.append_entry_in_transaction(
                    conn,
                    conversation_id="conv-1",
                    entry_id="entry-compaction-dangling",
                    entry_type="compaction",
                    payload={
                        "summary": "悬空边界",
                        "first_kept_entry_id": "entry-missing",
                        "tokens_before": 10,
                    },
                    created_at="2026-06-01T09:00:04+00:00",
                )

        with pytest.raises(InvalidConversationPayload):
            async with db.transaction() as conn:
                await repo.append_entry_in_transaction(
                    conn,
                    conversation_id="conv-1",
                    entry_id="entry-compaction-cross-conversation",
                    entry_type="compaction",
                    payload={
                        "summary": "跨会话边界",
                        "first_kept_entry_id": "entry-other",
                        "tokens_before": 30,
                    },
                    created_at="2026-06-01T09:00:05+00:00",
                )
        assert await _count(db, "conversation_entries") == 3

        async with db.transaction() as conn:
            kept = await repo.append_entry_in_transaction(
                conn,
                conversation_id="conv-1",
                entry_id="entry-compaction-current",
                entry_type="compaction",
                payload={
                    "summary": "当前摘要",
                    "first_kept_entry_id": "entry-1",
                    "tokens_before": 40,
                },
                created_at="2026-06-01T09:00:06+00:00",
            )
        assert kept.payload["first_kept_entry_id"] == "entry-1"
        latest = await repo.read_latest_compaction("conv-1")
        assert latest is not None and latest.id == "entry-compaction-current"
        assert await repo.read_latest_compaction("conv-2") is None
