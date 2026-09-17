"""Stage 4 子任务 02：003 迁移（rejected 状态 + 单 draft 部分唯一索引）安全升级。

依据：``refactor-log/stage4.md`` §3.8／§5.2／§6 Subtask 02／§9.3／§11（数据库项）、
``Fit-Agent-LangGraph-重构讨论总结.md`` §3.3／§3.4／§9、``LANGGRAPH_REFACTOR_PLAN.md`` §5.5。

覆盖：002 → 003 首次升级完整保留计划行、计划日程行与 ``workout_sessions.plan_session_id`` 关联、
迁移后 ``user_version=3``、重复启动不重复迁移或覆盖数据、重建后保留 ``source_plan_id`` 自引用／
外键（含 ``plan_sessions`` 的 ``ON DELETE CASCADE``）／``version`` 与 ``plan_sessions`` 唯一约束／
``idx_plans_single_active``、新增 ``idx_plans_single_draft`` 且两个部分唯一索引都生效、
``status`` CHECK 接受 ``rejected`` 并拒绝其它非法状态、plans 没有任何 ``rejected_at`` 时间列、
身份序列不重发（新插入继续递增）。

测试只使用 pytest ``tmp_path`` 下的独立临时库：先用只含 001／002 的临时迁移目录造出真正的旧库
（``user_version=2``），再用仓库默认迁移目录（含 003）升级，不伪造「旧版程序」。
"""

import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from storage.db import Database
from storage.migrations import DEFAULT_MIGRATIONS_DIR

#: 升级前写入的计划版本行（id、来源自引用、三个状态与三个时间字段都逐列比对）。
PLANS_SEED = (
    (
        1,
        1,
        "archived",
        None,
        '{"v": 1}',
        None,
        "2025-12-01T08:00:00+08:00",
        "2025-12-01T08:00:00+08:00",
        "2026-01-01T08:00:00+08:00",
    ),
    (
        2,
        2,
        "active",
        1,
        '{"v": 2}',
        '{"passed": true}',
        "2026-01-01T08:00:00+08:00",
        "2026-01-01T08:00:00+08:00",
        None,
    ),
    (
        3,
        3,
        "draft",
        2,
        '{"v": 3}',
        None,
        "2026-06-01T08:00:00+08:00",
        None,
        None,
    ),
)

#: 升级前写入的计划日程行（含已取消行与旧计划的历史日程）。
PLAN_SESSIONS_SEED = (
    (1, 2, "2026-06-01", None),
    (2, 2, "2026-06-03", "2026-06-02T09:00:00+08:00"),
    (3, 1, "2025-12-01", None),
)

#: 升级前写入的训练事实（含额外训练与关联计划日程的训练）。
WORKOUT_SESSIONS_SEED = (
    (1, "2026-06-01", 1),
    (2, "2026-06-02", None),
    (3, "2026-06-03", 2),
)

#: 七张业务表（003 不新增表、不建 View）。
EXPECTED_TABLES = {
    "athlete_profile",
    "body_metrics",
    "exercises",
    "plan_sessions",
    "plans",
    "workout_sessions",
    "workout_sets",
}

#: plans 列集合：除 001 的九列外不得新增任何 rejected 时间字段。
EXPECTED_PLAN_COLUMNS = (
    "id",
    "version",
    "status",
    "source_plan_id",
    "structured_content",
    "evaluator_result",
    "created_at",
    "confirmed_at",
    "archived_at",
)


def _migrations_dir_through_002(tmp_path: Path) -> Path:
    """只含 001／002 的临时迁移目录：用来造出真正的 Stage 2 旧库（``user_version=2``）。"""
    directory = tmp_path / "migrations_002"
    directory.mkdir()
    for name in ("001_initial.sql", "002_timed_sets_and_new_actions.sql"):
        shutil.copy(DEFAULT_MIGRATIONS_DIR / name, directory / name)
    return directory


async def _open(path: Path, migrations_dir: Path | None = None) -> Database:
    """开库并迁移到最新版本；迁移失败也要关连接，不把半套连接留给测试进程。"""
    db = Database(path) if migrations_dir is None else Database(path, migrations_dir)
    await db.open()
    try:
        await db.migrate()
    except BaseException:
        await db.close()
        raise
    return db


async def _rows(db: Database, sql: str, params: tuple[Any, ...] = ()) -> list[dict]:
    async def op(conn):
        async with conn.execute(sql, params) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    return await db.under_lock(op)


async def _exec(db: Database, sql: str, params: tuple[Any, ...] = ()) -> None:
    async with db.transaction() as conn:
        await conn.execute(sql, params)


async def _schema_names(db: Database, kind: str) -> set[str]:
    rows = await _rows(db, "SELECT name FROM sqlite_master WHERE type = ?", (kind,))
    return {str(row["name"]) for row in rows}


async def _count(db: Database, table: str) -> int:
    rows = await _rows(db, f"SELECT COUNT(*) AS n FROM {table}")
    return int(rows[0]["n"])


async def _seed_plan_data(db: Database) -> None:
    """写入计划、日程与训练事实（id 显式给出，便于升级后逐行比对）。"""
    async with db.transaction() as conn:
        for plan in PLANS_SEED:
            await conn.execute(
                "INSERT INTO plans (id, version, status, source_plan_id, structured_content,"
                " evaluator_result, created_at, confirmed_at, archived_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                plan,
            )
        for session in PLAN_SESSIONS_SEED:
            await conn.execute(
                "INSERT INTO plan_sessions (id, plan_id, scheduled_on, cancelled_at)"
                " VALUES (?, ?, ?, ?)",
                session,
            )
        for workout in WORKOUT_SESSIONS_SEED:
            await conn.execute(
                "INSERT INTO workout_sessions (id, performed_on, plan_session_id)"
                " VALUES (?, ?, ?)",
                workout,
            )


async def _upgraded_stage2_db(tmp_path: Path) -> tuple[Database, dict[str, list[dict]]]:
    """002 旧库写入计划数据后升级到 003；同时返回升级前快照供逐行比对。"""
    path = tmp_path / "stage2.db"
    db = await _open(path, _migrations_dir_through_002(tmp_path))
    try:
        assert await db.pragma_value("user_version") == 2
        await _seed_plan_data(db)
        before = {
            "plans": await _rows(db, "SELECT * FROM plans ORDER BY id"),
            "plan_sessions": await _rows(db, "SELECT * FROM plan_sessions ORDER BY id"),
            "workouts": await _rows(
                db, "SELECT id, performed_on, plan_session_id FROM workout_sessions ORDER BY id"
            ),
        }
    finally:
        await db.close()
    upgraded = await _open(path)
    return upgraded, before


async def test_upgrade_from_002_keeps_plan_data_and_sets_user_version_three(
    tmp_path: Path,
) -> None:
    """002 → 003：user_version=3，计划行、日程行与训练关联逐行不变。"""
    db, before = await _upgraded_stage2_db(tmp_path)
    try:
        assert await db.pragma_value("user_version") == 3
        after = {
            "plans": await _rows(db, "SELECT * FROM plans ORDER BY id"),
            "plan_sessions": await _rows(db, "SELECT * FROM plan_sessions ORDER BY id"),
            "workouts": await _rows(
                db, "SELECT id, performed_on, plan_session_id FROM workout_sessions ORDER BY id"
            ),
        }
        assert after == before
        # 显式列出关键事实，避免「快照对比」在两侧同时为空时给出假通过。
        assert [row["id"] for row in after["plans"]] == [1, 2, 3]
        assert [row["status"] for row in after["plans"]] == ["archived", "active", "draft"]
        assert [row["source_plan_id"] for row in after["plans"]] == [None, 1, 2]
        assert after["plan_sessions"] == [
            {"id": 1, "plan_id": 2, "scheduled_on": "2026-06-01", "cancelled_at": None},
            {
                "id": 2,
                "plan_id": 2,
                "scheduled_on": "2026-06-03",
                "cancelled_at": "2026-06-02T09:00:00+08:00",
            },
            {"id": 3, "plan_id": 1, "scheduled_on": "2025-12-01", "cancelled_at": None},
        ]
        assert [
            (row["id"], row["plan_session_id"]) for row in after["workouts"]
        ] == [(1, 1), (2, None), (3, 2)]
    finally:
        await db.close()


async def test_rebuilt_plans_keeps_foreign_keys_indexes_and_identity_sequence(
    tmp_path: Path,
) -> None:
    """重建后的 plans 保留自引用、日程外键（含 CASCADE）、唯一约束与单 active 索引，身份续发。"""
    db, _ = await _upgraded_stage2_db(tmp_path)
    try:
        # source_plan_id 自引用仍指向 plans(id)。
        assert await _rows(db, "PRAGMA foreign_key_list(plans)") == [
            {
                "id": 0,
                "seq": 0,
                "table": "plans",
                "from": "source_plan_id",
                "to": "id",
                "on_update": "NO ACTION",
                "on_delete": "NO ACTION",
                "match": "NONE",
            }
        ]
        # plan_sessions 外键仍指向 plans(id) 且保留 ON DELETE CASCADE。
        plan_session_fks = await _rows(db, "PRAGMA foreign_key_list(plan_sessions)")
        assert [
            (row["table"], row["from"], row["to"], row["on_delete"])
            for row in plan_session_fks
        ] == [("plans", "plan_id", "id", "CASCADE")]

        indexes = {row["name"]: row for row in await _rows(db, "PRAGMA index_list(plans)")}
        for name in ("idx_plans_single_active", "idx_plans_single_draft"):
            assert indexes[name]["unique"] == 1, name
            assert indexes[name]["partial"] == 1, name
        sql = {
            str(row["name"]): str(row["sql"])
            for row in await _rows(
                db, "SELECT name, sql FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert "WHERE status = 'active'" in sql["idx_plans_single_active"]
        assert "WHERE status = 'draft'" in sql["idx_plans_single_draft"]

        # version 唯一约束与 plan_sessions 的唯一日名额仍生效。
        for statement in (
            "INSERT INTO plans (id, version, status, structured_content, created_at)"
            " VALUES (10, 1, 'archived', '{}', 't')",
            "INSERT INTO plan_sessions (plan_id, scheduled_on) VALUES (2, '2026-06-01')",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                await _exec(db, statement)

        # 外键仍生效：不存在的计划、非法的来源计划与非法日程关联都被拒绝。
        for statement in (
            "INSERT INTO plan_sessions (plan_id, scheduled_on) VALUES (999, '2026-07-01')",
            "UPDATE plans SET source_plan_id = 999 WHERE id = 3",
            "INSERT INTO workout_sessions (performed_on, plan_session_id)"
            " VALUES ('2026-07-02', 999)",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                await _exec(db, statement)

        # 身份序列不重发：新插入的计划与日程继续递增（不覆盖既有身份）。
        async with db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO plans (version, status, structured_content, created_at)"
                " VALUES (4, 'rejected', '{}', 't')"
            )
            assert cursor.lastrowid == 4
            await cursor.close()
            cursor = await conn.execute(
                "INSERT INTO plan_sessions (plan_id, scheduled_on) VALUES (2, '2026-06-05')"
            )
            assert cursor.lastrowid == 4
            await cursor.close()
    finally:
        await db.close()


async def test_rejected_status_is_accepted_and_other_statuses_are_rejected(
    tmp_path: Path,
) -> None:
    """状态 CHECK 接受 rejected（两个时间字段留空），并拒绝其它非法状态。"""
    db, _ = await _upgraded_stage2_db(tmp_path)
    try:
        await _exec(
            db,
            "INSERT INTO plans (version, status, structured_content, evaluator_result,"
            " created_at, confirmed_at, archived_at)"
            " VALUES (4, 'rejected', '{\"v\": 4}', '{\"passed\": false}', 't4', NULL, NULL)",
        )
        rejected = await _rows(
            db, "SELECT status, confirmed_at, archived_at FROM plans WHERE version = 4"
        )
        assert rejected == [{"status": "rejected", "confirmed_at": None, "archived_at": None}]

        for status in ("pending", "succeeded", "REJECTED", ""):
            with pytest.raises(sqlite3.IntegrityError):
                await _exec(
                    db,
                    "INSERT INTO plans (version, status, structured_content, created_at)"
                    " VALUES (?, ?, '{}', 't')",
                    (10 + len(status), status),
                )
    finally:
        await db.close()


async def test_single_active_and_single_draft_partial_indexes_are_enforced(
    tmp_path: Path,
) -> None:
    """任意时刻最多一条 active 与最多一条 draft；archived／rejected 不受该部分索引限制。"""
    db, _ = await _upgraded_stage2_db(tmp_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            await _exec(
                db,
                "INSERT INTO plans (version, status, structured_content, created_at)"
                " VALUES (4, 'active', '{}', 't')",
            )
        with pytest.raises(sqlite3.IntegrityError):
            await _exec(
                db,
                "INSERT INTO plans (version, status, structured_content, created_at)"
                " VALUES (4, 'draft', '{}', 't')",
            )
        async with db.transaction() as conn:
            for version, status in ((4, "archived"), (5, "rejected"), (6, "rejected")):
                await conn.execute(
                    "INSERT INTO plans (version, status, structured_content, created_at)"
                    " VALUES (?, ?, '{}', 't')",
                    (version, status),
                )
        assert await _count(db, "plans") == len(PLANS_SEED) + 3
        assert [
            row["status"]
            for row in await _rows(db, "SELECT status FROM plans WHERE status = 'active'")
        ] == ["active"]
    finally:
        await db.close()


async def test_plans_has_no_rejected_timestamp_and_no_new_tables(tmp_path: Path) -> None:
    """003 不新增 rejected 时间列、不新增表或 View。"""
    db, _ = await _upgraded_stage2_db(tmp_path)
    try:
        columns = tuple(
            str(row["name"]) for row in await _rows(db, "PRAGMA table_info(plans)")
        )
        assert columns == EXPECTED_PLAN_COLUMNS
        assert not [name for name in columns if "rejected" in name]
        assert {
            name
            for name in await _schema_names(db, "table")
            if not name.startswith("sqlite_")
        } == EXPECTED_TABLES
        assert await _schema_names(db, "view") == set()
    finally:
        await db.close()


async def test_upgrade_is_repeatable_and_does_not_overwrite_data(tmp_path: Path) -> None:
    """停机重开：003 不再执行，升级后写入的数据不被覆盖。"""
    db, _ = await _upgraded_stage2_db(tmp_path)
    try:
        await _exec(
            db,
            "INSERT INTO plans (version, status, structured_content, created_at)"
            " VALUES (4, 'rejected', '{\"v\": 4}', 't4')",
        )
        await db.close()

        reopened = await _open(db.path)
        try:
            assert await reopened.pragma_value("user_version") == 3
            assert await _count(reopened, "plans") == len(PLANS_SEED) + 1
            assert await _count(reopened, "plan_sessions") == len(PLAN_SESSIONS_SEED)
            assert [row["status"] for row in await _rows(reopened, "SELECT status FROM plans ORDER BY id")] == [
                "archived",
                "active",
                "draft",
                "rejected",
            ]
        finally:
            await reopened.close()
    finally:
        if db.is_open:
            await db.close()
