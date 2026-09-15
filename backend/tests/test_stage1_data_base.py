"""Stage 1 子任务 01：新版 001_initial.sql —— 7 张业务表、约束、外键与动作种子。

覆盖迁移首次执行、重复启动不覆盖数据、user_version=1、业务表／索引／外键／CHECK 约束、
「最多一条 active 计划」与「同一计划日程最多一条有效训练」，以及动作种子的负重口径与
最小加重单位。LangGraph Checkpoint 表不在业务迁移中创建（总结 §5.6）。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from storage.db import Database

BUSINESS_TABLES = {
    "athlete_profile",
    "exercises",
    "body_metrics",
    "workout_sessions",
    "workout_sets",
    "plans",
    "plan_sessions",
}
LOAD_CONVENTIONS = {
    "barbell_includes_bar_total",
    "dumbbell_per_hand",
    "machine_pin_displayed_value",
    "plate_loaded_total_excluding_empty",
    "unilateral_setting_per_side",
}
# 24 项已核验动作种子的逐项映射：id -> (load_convention, min_load_increment_kg,
# source_ref, recommendable)。recommendable 全为 True（reviewer P1-1 用户拍板 A）。
EXPECTED_EXERCISE_SEED = {
    "barbell-back-squat": ("barbell_includes_bar_total", 2.5, "exercises-dataset:0043", True),
    "barbell-deadlift": ("barbell_includes_bar_total", 2.5, "exercises-dataset:0032", True),
    "barbell-romanian-deadlift": ("barbell_includes_bar_total", 2.5, "exercises-dataset:0085", True),
    "leg-press-45": ("plate_loaded_total_excluding_empty", 2.5, "exercises-dataset:0739", True),
    "bulgarian-split-squat": ("dumbbell_per_hand", 2.5, "exercises-dataset:0410", True),
    "barbell-bench-press": ("barbell_includes_bar_total", 2.5, "exercises-dataset:0025", True),
    "dumbbell-bench-press": ("dumbbell_per_hand", 2.5, "exercises-dataset:0289", True),
    "dumbbell-incline-bench-press": ("dumbbell_per_hand", 2.5, "exercises-dataset:0314", True),
    "seated-dumbbell-shoulder-press": ("dumbbell_per_hand", 2.5, "exercises-dataset:0405", True),
    "dumbbell-lateral-raise": ("dumbbell_per_hand", 2.5, "exercises-dataset:0334", True),
    "dumbbell-reverse-fly": ("dumbbell_per_hand", 2.5, "exercises-dataset:0383", True),
    "barbell-bent-over-row": ("barbell_includes_bar_total", 2.5, "exercises-dataset:0027", True),
    "seated-cable-row": ("machine_pin_displayed_value", 5.0, "exercises-dataset:0861", True),
    "one-arm-dumbbell-row": ("dumbbell_per_hand", 2.5, "exercises-dataset:0292", True),
    "lat-pulldown": ("machine_pin_displayed_value", 5.0, "exercises-dataset:0198", True),
    "pull-up": (None, None, "exercises-dataset:0652", True),
    "seated-leg-curl": ("machine_pin_displayed_value", 5.0, "exercises-dataset:0599", True),
    "leg-extension": ("machine_pin_displayed_value", 5.0, "exercises-dataset:0585", True),
    "machine-standing-calf-raise": ("machine_pin_displayed_value", 5.0, "exercises-dataset:0605", True),
    "dumbbell-biceps-curl": ("dumbbell_per_hand", 2.5, "exercises-dataset:0294", True),
    "cable-pushdown": ("machine_pin_displayed_value", 5.0, "exercises-dataset:0201", True),
    "cable-overhead-triceps-extension": ("machine_pin_displayed_value", 5.0, "exercises-dataset:0194", True),
    "parallel-bar-dip": (None, None, "exercises-dataset:0251", True),
    "hanging-leg-raise": (None, None, "exercises-dataset:0472", True),
}
# LangGraph SQLite Checkpointer 自管这些表，业务迁移不得复制一套。
CHECKPOINT_TABLES = {
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
}


async def _migrated(path: Path) -> Database:
    db = Database(path)
    await db.open()
    await db.migrate()
    return db


async def _table_names(db: Database) -> set[str]:
    async def op(conn):
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ) as cursor:
            return {str(row["name"]) for row in await cursor.fetchall()}

    return await db.under_lock(op)


async def _index_names(db: Database) -> set[str]:
    async def op(conn):
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ) as cursor:
            return {str(row["name"]) for row in await cursor.fetchall()}

    return await db.under_lock(op)


async def _count(db: Database, table: str) -> int:
    async def op(conn):
        async with conn.execute(f"SELECT COUNT(*) AS n FROM {table}") as cursor:
            return int((await cursor.fetchone())["n"])

    return await db.under_lock(op)


async def test_first_run_creates_business_tables_and_user_version_one(
    tmp_path: Path,
) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        assert await db.pragma_value("user_version") == 1
        # AUTOINCREMENT 会附建 sqlite_sequence；除它之外恰是 7 张业务表。
        assert await _table_names(db) == BUSINESS_TABLES | {"sqlite_sequence"}
        assert "idx_plans_single_active" in await _index_names(db)
    finally:
        await db.close()


async def test_no_langgraph_checkpoint_tables(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        assert await _table_names(db) & CHECKPOINT_TABLES == set()
    finally:
        await db.close()


async def test_repeated_start_keeps_schema_and_does_not_overwrite_data(
    tmp_path: Path,
) -> None:
    path = tmp_path / "x.db"
    db = await _migrated(path)
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO body_metrics (measured_on, weight_kg, body_fat_pct)"
            " VALUES ('2026-06-01', 70.5, 18.0)"
        )
    await db.close()

    reopened = await _migrated(path)  # 再次启动：无新迁移可执行，不重建表、不清数据
    try:
        assert await reopened.pragma_value("user_version") == 1
        assert await _count(reopened, "body_metrics") == 1
        assert await _count(reopened, "exercises") == 24
    finally:
        await reopened.close()


async def test_external_weight_allows_integer_or_one_decimal(
    tmp_path: Path,
) -> None:
    """外加重量已拍精度：整数或最多一位小数（62.5／62／0／1000 均合法）。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO workout_sessions (id, performed_on) VALUES (1, '2026-06-01')"
            )
            for set_no, weight in enumerate((0, 62, 62.5, 1000), start=1):
                await conn.execute(
                    "INSERT INTO workout_sets"
                    " (workout_session_id, exercise_id, set_no, set_type,"
                    "  load_convention, weight_kg, reps)"
                    " VALUES (1, 'barbell-back-squat', ?, 'work',"
                    "  'barbell_includes_bar_total', ?, 5)",
                    (set_no, weight),
                )
        assert await _count(db, "workout_sets") == 4
    finally:
        await db.close()


async def test_at_most_one_active_plan(tmp_path: Path) -> None:
    """部分唯一索引：任意时刻最多一条 active 计划（draft／archived 不受限）。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO plans (version, status, structured_content, created_at)"
                " VALUES (1, 'active', '{}', 't')"
            )
            await conn.execute(
                "INSERT INTO plans (version, status, structured_content, created_at)"
                " VALUES (2, 'draft', '{}', 't')"
            )
            await conn.execute(
                "INSERT INTO plans (version, status, structured_content, created_at)"
                " VALUES (3, 'archived', '{}', 't')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            async with db.transaction() as conn:
                await conn.execute(
                    "INSERT INTO plans (version, status, structured_content, created_at)"
                    " VALUES (4, 'active', '{}', 't')"
                )
    finally:
        await db.close()


async def test_plan_session_at_most_one_linked_workout(tmp_path: Path) -> None:
    """同一计划日程最多关联一条有效训练；NULL（额外训练）可重复。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO plans (id, version, status, structured_content, created_at)"
                " VALUES (1, 1, 'active', '{}', 't')"
            )
            await conn.execute(
                "INSERT INTO plan_sessions (id, plan_id, scheduled_on)"
                " VALUES (1, 1, '2026-06-01')"
            )
            await conn.execute(
                "INSERT INTO workout_sessions (id, performed_on, plan_session_id)"
                " VALUES (1, '2026-06-01', 1)"
            )
            await conn.execute(
                "INSERT INTO workout_sessions (id, performed_on, plan_session_id)"
                " VALUES (2, '2026-06-02', NULL)"
            )
            await conn.execute(
                "INSERT INTO workout_sessions (id, performed_on, plan_session_id)"
                " VALUES (3, '2026-06-03', NULL)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            async with db.transaction() as conn:
                await conn.execute(
                    "INSERT INTO workout_sessions (id, performed_on, plan_session_id)"
                    " VALUES (4, '2026-06-04', 1)"
                )
    finally:
        await db.close()


async def test_workout_session_total_sets_capped_at_fifty(tmp_path: Path) -> None:
    """一次训练总组数 1–50：跨动作计数的边用触发器强制（第 51 组拒绝）。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO workout_sessions (id, performed_on) VALUES (1, '2026-06-01')"
            )
            for exercise_id in ("pull-up", "parallel-bar-dip"):
                for set_no in range(1, 26):
                    await conn.execute(
                        "INSERT INTO workout_sets"
                        " (workout_session_id, exercise_id, set_no, set_type, reps)"
                        " VALUES (1, ?, ?, 'work', 8)",
                        (exercise_id, set_no),
                    )
        assert await _count(db, "workout_sets") == 50
        # 第 51 组：组序号仍合法（26），但超出一次训练总组数上限。
        with pytest.raises(sqlite3.IntegrityError):
            async with db.transaction() as conn:
                await conn.execute(
                    "INSERT INTO workout_sets"
                    " (workout_session_id, exercise_id, set_no, set_type, reps)"
                    " VALUES (1, 'pull-up', 26, 'work', 8)"
                )
        assert await _count(db, "workout_sets") == 50
    finally:
        await db.close()


async def test_foreign_keys_enforced_and_cascade(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO workout_sessions (id, performed_on) VALUES (1, '2026-06-01')"
            )
            await conn.execute(
                "INSERT INTO workout_sets"
                " (workout_session_id, exercise_id, set_no, set_type, reps)"
                " VALUES (1, 'pull-up', 1, 'work', 8)"
            )
        # 未知动作：exercises 外键拒绝
        with pytest.raises(sqlite3.IntegrityError):
            async with db.transaction() as conn:
                await conn.execute(
                    "INSERT INTO workout_sets"
                    " (workout_session_id, exercise_id, set_no, set_type, reps)"
                    " VALUES (1, 'no-such-exercise', 2, 'work', 8)"
                )
        # 未知训练：workout_sessions 外键拒绝
        with pytest.raises(sqlite3.IntegrityError):
            async with db.transaction() as conn:
                await conn.execute(
                    "INSERT INTO workout_sets"
                    " (workout_session_id, exercise_id, set_no, set_type, reps)"
                    " VALUES (999, 'pull-up', 1, 'work', 8)"
                )
        # ON DELETE CASCADE：删训练连带删组
        async with db.transaction() as conn:
            await conn.execute("DELETE FROM workout_sessions WHERE id = 1")
        assert await _count(db, "workout_sets") == 0
    finally:
        await db.close()


async def test_range_and_enum_constraints_reject_invalid_values(
    tmp_path: Path,
) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO workout_sessions (id, performed_on) VALUES (1, '2026-06-01')"
            )
        invalid = [
            # body_metrics 体重范围 20–400
            "INSERT INTO body_metrics (measured_on, weight_kg) VALUES ('d', 19.9)",
            "INSERT INTO body_metrics (measured_on, weight_kg) VALUES ('d', 400.1)",
            # 体脂范围 0–100
            "INSERT INTO body_metrics (measured_on, weight_kg, body_fat_pct)"
            " VALUES ('d', 70, 101)",
            # 计划状态枚举
            "INSERT INTO plans (version, status, structured_content, created_at)"
            " VALUES (1, 'pending', '{}', 't')",
            # 组类型枚举（work / warmup / assisted）
            "INSERT INTO workout_sets"
            " (workout_session_id, exercise_id, set_no, set_type, reps)"
            " VALUES (1, 'pull-up', 1, 'drop', 5)",
            # 单组次数 1–100
            "INSERT INTO workout_sets"
            " (workout_session_id, exercise_id, set_no, set_type, reps)"
            " VALUES (1, 'pull-up', 1, 'work', 0)",
            "INSERT INTO workout_sets"
            " (workout_session_id, exercise_id, set_no, set_type, reps)"
            " VALUES (1, 'pull-up', 1, 'work', 101)",
            # 组序号 1–50
            "INSERT INTO workout_sets"
            " (workout_session_id, exercise_id, set_no, set_type, reps)"
            " VALUES (1, 'pull-up', 51, 'work', 8)",
            # 外加重量范围 0–1000
            "INSERT INTO workout_sets"
            " (workout_session_id, exercise_id, set_no, set_type, load_convention,"
            "  weight_kg, reps)"
            " VALUES (1, 'barbell-back-squat', 1, 'work', 'barbell_includes_bar_total',"
            "  1000.1, 5)",
            "INSERT INTO workout_sets"
            " (workout_session_id, exercise_id, set_no, set_type, load_convention,"
            "  weight_kg, reps)"
            " VALUES (1, 'barbell-back-squat', 1, 'work', 'barbell_includes_bar_total',"
            "  -1, 5)",
            # 外加重量精度：最多一位小数（62.55 拒绝）
            "INSERT INTO workout_sets"
            " (workout_session_id, exercise_id, set_no, set_type, load_convention,"
            "  weight_kg, reps)"
            " VALUES (1, 'barbell-back-squat', 1, 'work', 'barbell_includes_bar_total',"
            "  62.55, 5)",
            # 负重口径与重量同现同隐：只给口径不给重量被拒绝
            "INSERT INTO workout_sets"
            " (workout_session_id, exercise_id, set_no, set_type, load_convention, reps)"
            " VALUES (1, 'barbell-back-squat', 1, 'work', 'barbell_includes_bar_total', 5)",
        ]
        for statement in invalid:
            with pytest.raises(sqlite3.IntegrityError):
                async with db.transaction() as conn:
                    await conn.execute(statement)
    finally:
        await db.close()


async def test_exercise_seed_validates_load_and_increment(tmp_path: Path) -> None:
    """动作种子：24 项稳定 ID；外加负重带口径与最小加重单位，自重动作均为 NULL。"""
    db = await _migrated(tmp_path / "x.db")
    try:

        async def op(conn):
            async with conn.execute(
                "SELECT id, record_type, load_convention, min_load_increment_kg,"
                " modes_json FROM exercises"
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

        rows = await db.under_lock(op)
        assert len(rows) == 24
        assert len({row["id"] for row in rows}) == 24
        for row in rows:
            modes = json.loads(row["modes_json"])
            assert isinstance(modes, list) and modes, row
            assert all(isinstance(mode, str) and mode for mode in modes), row
            if row["record_type"] == "reps_weight":
                assert row["load_convention"] in LOAD_CONVENTIONS, row
                assert row["min_load_increment_kg"] is not None, row
                assert row["min_load_increment_kg"] > 0, row
            else:
                assert row["load_convention"] is None, row
                assert row["min_load_increment_kg"] is None, row
    finally:
        await db.close()


async def test_exercise_seed_matches_verified_mapping(tmp_path: Path) -> None:
    """逐项锁定 24 项已核验动作的负重口径、最小加重单位、来源与可推荐标记（reviewer P2-1）。"""
    db = await _migrated(tmp_path / "x.db")
    try:

        async def op(conn):
            async with conn.execute(
                "SELECT id, load_convention, min_load_increment_kg, source_ref,"
                " recommendable FROM exercises"
            ) as cursor:
                return {str(row["id"]): row for row in await cursor.fetchall()}

        rows = await db.under_lock(op)
        actual = {
            exercise_id: (
                row["load_convention"],
                row["min_load_increment_kg"],
                row["source_ref"],
                bool(row["recommendable"]),
            )
            for exercise_id, row in rows.items()
        }
        assert actual == EXPECTED_EXERCISE_SEED
        # 已核验动作全部可用于计划（用户拍板 A）。
        assert all(entry[3] for entry in actual.values())
    finally:
        await db.close()


async def test_exercises_reject_invalid_load_increment_pairing(tmp_path: Path) -> None:
    """库内约束：负重次数型必须带口径与加重单位，自重型不得携带（不虚构 0kg）。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        invalid = [
            "INSERT INTO exercises (id, standard_name_zh, equipment_variant,"
            " record_type, load_convention, min_load_increment_kg, modes_json,"
            " source_ref, attribution)"
            " VALUES ('x1', '架空动作一', 'barbell', 'reps_weight', NULL, 2.5, '[]', 's', 'a')",
            "INSERT INTO exercises (id, standard_name_zh, equipment_variant,"
            " record_type, load_convention, min_load_increment_kg, modes_json,"
            " source_ref, attribution)"
            " VALUES ('x2', '架空动作二', 'bodyweight', 'reps_bodyweight',"
            " 'dumbbell_per_hand', NULL, '[]', 's', 'a')",
        ]
        for statement in invalid:
            with pytest.raises(sqlite3.IntegrityError):
                async with db.transaction() as conn:
                    await conn.execute(statement)
    finally:
        await db.close()


async def test_no_rir_column_anywhere(tmp_path: Path) -> None:
    """新版 Schema 不含任何 RIR 列（总结 §7：彻底删除 RIR）。"""
    db = await _migrated(tmp_path / "x.db")
    try:

        async def op(conn):
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ) as cursor:
                tables = [str(row["name"]) for row in await cursor.fetchall()]
            columns: dict[str, set[str]] = {}
            for table in tables:
                async with conn.execute(f"PRAGMA table_info({table})") as cursor:
                    columns[table] = {
                        str(row["name"]).lower() for row in await cursor.fetchall()
                    }
            return columns

        columns = await db.under_lock(op)
        for table, names in columns.items():
            assert not any("rir" in name for name in names), table
    finally:
        await db.close()
