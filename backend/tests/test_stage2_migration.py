"""Stage 2 Subtask 02 §A：002 迁移（计时组字段 + 三个新动作种子）安全升级。

依据：``refactor-log/stage2.md`` §3、``refactor-log/stage2-subTasks/02-migration-and-records.md`` §A、
``LANGGRAPH_REFACTOR_PLAN.md`` §11 阶段 2、``Fit-Agent-LangGraph-重构讨论总结.md`` §3.1／§7.1／§9。

覆盖：001 → 002 首次升级完整保留 Stage 1 训练组与既有 24 项种子、迁移后 ``user_version=2``、
重复启动不重复迁移或覆盖数据、``reps`` 可空与 ``duration_seconds`` 新增、重建后外键／唯一约束／
组类型约束／最多 50 组触发器／索引一律保留、外加重量口径 ``external_added_weight`` 可用、
三个新动作种子逐字段正确、计时时长范围**不在库内**（只由 Domain 唯一规则实施）。

测试只使用 pytest ``tmp_path`` 下的独立临时库：先用只含 ``001_initial.sql`` 的临时迁移目录造出
Stage 1 旧库，再用仓库默认迁移目录（含 002）升级，避免伪造「旧版程序」。
"""

import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from storage.db import Database
from storage.migrations import DEFAULT_MIGRATIONS_DIR

#: 002 新增的三个动作种子逐字段期望（stage2.md §3；来源见迁移注释）。
NEW_SEED = {
    "weighted-pull-up": (
        "负重引体",
        "weighted",
        "reps_weight",
        "external_added_weight",
        5.0,
        1,
        '["垂直拉"]',
        "exercises-dataset:0841",
        "© Gym visual — https://gymvisual.com/",
    ),
    "plank": (
        "平板支撑",
        "bodyweight",
        "time",
        None,
        None,
        1,
        '["核心"]',
        "refactor-log/stage2.md:§3",
        "无第三方媒体再分发 / Fit-Agent Stage 2 口径",
    ),
    "front-lever": (
        "前水平",
        "bodyweight",
        "time",
        None,
        None,
        1,
        '["核心"]',
        "exercises-dataset:3296",
        "© Gym visual — https://gymvisual.com/",
    ),
}

#: Stage 2 之前已发布的 24 项种子身份（002 必须原样保留，只追加不修改）。
STAGE1_SEED_IDS = (
    "barbell-back-squat",
    "barbell-deadlift",
    "barbell-romanian-deadlift",
    "leg-press-45",
    "bulgarian-split-squat",
    "barbell-bench-press",
    "dumbbell-bench-press",
    "dumbbell-incline-bench-press",
    "seated-dumbbell-shoulder-press",
    "dumbbell-lateral-raise",
    "dumbbell-reverse-fly",
    "barbell-bent-over-row",
    "seated-cable-row",
    "one-arm-dumbbell-row",
    "lat-pulldown",
    "pull-up",
    "seated-leg-curl",
    "leg-extension",
    "machine-standing-calf-raise",
    "dumbbell-biceps-curl",
    "cable-pushdown",
    "cable-overhead-triceps-extension",
    "parallel-bar-dip",
    "hanging-leg-raise",
)

#: 升级前写入的 Stage 1 训练组（id 与列值都必须原样保留到 002 之后）。
STAGE1_SETS = (
    # (id, set_no, set_type, load_convention, weight_kg, reps)
    (1, 1, "warmup", "barbell_includes_bar_total", 40.0, 8),
    (2, 2, "work", "barbell_includes_bar_total", 62.5, 5),
    (3, 1, "work", None, None, 8),
)


def _stage1_only_migrations_dir(tmp_path: Path) -> Path:
    """只含 ``001_initial.sql`` 的临时迁移目录：用来造出真正的 Stage 1（user_version=1）旧库。"""
    directory = tmp_path / "migrations_001"
    directory.mkdir()
    shutil.copy(
        DEFAULT_MIGRATIONS_DIR / "001_initial.sql", directory / "001_initial.sql"
    )
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


async def _seed_stage1_workout(db: Database) -> None:
    """写入 Stage 1 训练与训练组（id 显式给出，便于升级后逐行比对）。"""
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO workout_sessions (id, performed_on) VALUES (1, '2026-06-01')"
        )
        for set_id, set_no, set_type, convention, weight, reps in STAGE1_SETS:
            await conn.execute(
                "INSERT INTO workout_sets (id, workout_session_id, exercise_id, set_no,"
                " set_type, load_convention, weight_kg, reps)"
                " VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
                (
                    set_id,
                    "barbell-back-squat" if convention is not None else "pull-up",
                    set_no,
                    set_type,
                    convention,
                    weight,
                    reps,
                ),
            )


async def _upgraded_stage1_db(tmp_path: Path) -> tuple[Database, list[dict]]:
    """Stage 1 旧库写入训练数据后升级到 002；同时返回升级前的动作目录行供逐行比对。"""
    path = tmp_path / "stage1.db"
    db = await _open(path, _stage1_only_migrations_dir(tmp_path))
    try:
        assert await db.pragma_value("user_version") == 1
        await _seed_stage1_workout(db)
        exercises_before = await _rows(
            db, "SELECT * FROM exercises ORDER BY id"
        )
    finally:
        await db.close()
    upgraded = await _open(path)
    return upgraded, exercises_before


async def test_upgrade_from_001_keeps_stage1_data_and_sets_user_version_two(
    tmp_path: Path,
) -> None:
    """001 → 002：user_version=2、训练组与既有 24 项种子逐行不变、计时列在旧数据上为 NULL。"""
    db, exercises_before = await _upgraded_stage1_db(tmp_path)
    try:
        assert await db.pragma_value("user_version") == 2

        sets = await _rows(
            db,
            "SELECT id, workout_session_id, exercise_id, set_no, set_type, load_convention,"
            " weight_kg, reps, duration_seconds FROM workout_sets ORDER BY id",
        )
        assert [
            (
                row["id"],
                row["workout_session_id"],
                row["set_no"],
                row["set_type"],
                row["load_convention"],
                row["weight_kg"],
                row["reps"],
                row["duration_seconds"],
            )
            for row in sets
        ] == [
            (set_id, 1, set_no, set_type, convention, weight, reps, None)
            for set_id, set_no, set_type, convention, weight, reps in STAGE1_SETS
        ]
        assert [row["exercise_id"] for row in sets] == [
            "barbell-back-squat",
            "barbell-back-squat",
            "pull-up",
        ]

        sessions = await _rows(
            db, "SELECT id, performed_on, plan_session_id FROM workout_sessions"
        )
        assert sessions == [
            {"id": 1, "performed_on": "2026-06-01", "plan_session_id": None}
        ]

        # 既有 24 项种子逐行不变（含来源与推荐标记），只追加三项。
        after = {
            str(row["id"]): row
            for row in await _rows(db, "SELECT * FROM exercises ORDER BY id")
        }
        assert set(after) == set(STAGE1_SEED_IDS) | set(NEW_SEED)
        for row in exercises_before:
            assert after[str(row["id"])] == row, row["id"]
    finally:
        await db.close()


async def test_upgrade_is_repeatable_and_does_not_overwrite_data(tmp_path: Path) -> None:
    """停机重开：002 不再执行，种子不重复、升级后写入的数据不被覆盖。"""
    db, _ = await _upgraded_stage1_db(tmp_path)
    try:
        await _exec(
            db,
            "INSERT INTO workout_sets (workout_session_id, exercise_id, set_no, set_type,"
            " duration_seconds) VALUES (1, 'plank', 1, 'work', 45)",
        )
        await db.close()

        reopened = await _open(db.path)
        try:
            assert await reopened.pragma_value("user_version") == 2
            assert await _count(reopened, "exercises") == 27
            assert await _count(reopened, "workout_sets") == len(STAGE1_SETS) + 1
            timed = await _rows(
                reopened,
                "SELECT reps, duration_seconds FROM workout_sets"
                " WHERE exercise_id = 'plank'",
            )
            assert timed == [{"reps": None, "duration_seconds": 45}]
        finally:
            await reopened.close()
    finally:
        if db.is_open:
            await db.close()


async def test_rebuilt_table_keeps_constraints_indexes_and_trigger(tmp_path: Path) -> None:
    """重建后的 workout_sets 保留外键、唯一约束、组类型约束、重量口径约束与 50 组触发器。"""
    db, _ = await _upgraded_stage1_db(tmp_path)
    try:
        assert {
            "idx_workout_sets_exercise",
            "idx_workout_sets_session",
        } <= await _schema_names(db, "index")
        assert "trg_workout_sets_max_per_session" in await _schema_names(db, "trigger")

        # 计时组：reps 为空、只给秒数，可写入并被读回。
        await _exec(
            db,
            "INSERT INTO workout_sets (workout_session_id, exercise_id, set_no, set_type,"
            " duration_seconds) VALUES (1, 'plank', 10, 'work', 90)",
        )
        # 外加重量口径 external_added_weight：负重引体自带 5kg 最小加重的口径可写入。
        await _exec(
            db,
            "INSERT INTO workout_sets (workout_session_id, exercise_id, set_no, set_type,"
            " load_convention, weight_kg, reps)"
            " VALUES (1, 'weighted-pull-up', 1, 'work', 'external_added_weight', 10, 5)",
        )
        assert await _count(db, "workout_sets") == len(STAGE1_SETS) + 2

        invalid = [
            # 动作外键仍生效。
            "INSERT INTO workout_sets (workout_session_id, exercise_id, set_no, set_type, reps)"
            " VALUES (1, 'no-such-exercise', 20, 'work', 5)",
            # 训练外键仍生效。
            "INSERT INTO workout_sets (workout_session_id, exercise_id, set_no, set_type, reps)"
            " VALUES (999, 'pull-up', 20, 'work', 5)",
            # 同一动作内组序号唯一约束仍生效。
            "INSERT INTO workout_sets (workout_session_id, exercise_id, set_no, set_type, reps)"
            " VALUES (1, 'pull-up', 1, 'work', 8)",
            # 组类型仍恰三态。
            "INSERT INTO workout_sets (workout_session_id, exercise_id, set_no, set_type, reps)"
            " VALUES (1, 'pull-up', 21, 'drop', 8)",
            # 负重口径与重量仍同现同隐。
            "INSERT INTO workout_sets (workout_session_id, exercise_id, set_no, set_type,"
            " load_convention, reps)"
            " VALUES (1, 'weighted-pull-up', 21, 'work', 'external_added_weight', 5)",
            # 外加重量精度仍是「整数或最多一位小数」。
            "INSERT INTO workout_sets (workout_session_id, exercise_id, set_no, set_type,"
            " load_convention, weight_kg, reps)"
            " VALUES (1, 'weighted-pull-up', 21, 'work', 'external_added_weight', 10.55, 5)",
            # 有值时的次数范围仍是 1–100。
            "INSERT INTO workout_sets (workout_session_id, exercise_id, set_no, set_type, reps)"
            " VALUES (1, 'pull-up', 21, 'work', 101)",
            # 一旦收到口径与加重单位的目录外取值仍被拒绝（外部口径只收了 external_added_weight）。
            "INSERT INTO exercises (id, standard_name_zh, equipment_variant, record_type,"
            " load_convention, min_load_increment_kg, modes_json, source_ref, attribution)"
            " VALUES ('x-belt', '架空动作', 'weighted', 'reps_weight', 'weighted_belt', 2.5,"
            " '[\"垂直拉\"]', 's', 'a')",
        ]
        for statement in invalid:
            with pytest.raises(sqlite3.IntegrityError):
                await _exec(db, statement)

        # 第 51 组（跨动作计数）仍被触发器拒绝：先补齐到 50 组（当前 5 组，分两个动作补 45 组）。
        async with db.transaction() as conn:
            for set_no in range(2, 27):
                await conn.execute(
                    "INSERT INTO workout_sets (workout_session_id, exercise_id, set_no,"
                    " set_type, reps) VALUES (1, 'pull-up', ?, 'work', 5)",
                    (set_no,),
                )
            for set_no in range(1, 21):
                await conn.execute(
                    "INSERT INTO workout_sets (workout_session_id, exercise_id, set_no,"
                    " set_type, reps) VALUES (1, 'parallel-bar-dip', ?, 'work', 5)",
                    (set_no,),
                )
        assert await _count(db, "workout_sets") == 50
        with pytest.raises(sqlite3.IntegrityError):
            await _exec(
                db,
                "INSERT INTO workout_sets (workout_session_id, exercise_id, set_no, set_type,"
                " reps) VALUES (1, 'parallel-bar-dip', 21, 'work', 5)",
            )
        assert await _count(db, "workout_sets") == 50
    finally:
        await db.close()


async def test_duration_range_is_not_a_database_check(tmp_path: Path) -> None:
    """已拍口径：库内不加时长范围 CHECK，0／负数／超大整数在库层都能写；范围只由 Domain 实施。"""
    db, _ = await _upgraded_stage1_db(tmp_path)
    try:
        async with db.transaction() as conn:
            for set_no, duration in enumerate((0, -5, 10**15), start=30):
                await conn.execute(
                    "INSERT INTO workout_sets (workout_session_id, exercise_id, set_no,"
                    " set_type, duration_seconds) VALUES (1, 'plank', ?, 'work', ?)",
                    (set_no, duration),
                )
        stored = await _rows(
            db,
            "SELECT duration_seconds FROM workout_sets WHERE exercise_id = 'plank'"
            " ORDER BY set_no",
        )
        assert [row["duration_seconds"] for row in stored] == [0, -5, 10**15]
    finally:
        await db.close()


async def test_new_action_seeds_match_frozen_mapping(tmp_path: Path) -> None:
    """三个新动作种子逐字段正确；pull-up 保持纯自重次数动作，不被负重引体取代。"""
    db, _ = await _upgraded_stage1_db(tmp_path)
    try:
        rows = await _rows(
            db,
            "SELECT id, standard_name_zh, equipment_variant, record_type, load_convention,"
            " min_load_increment_kg, recommendable, modes_json, source_ref, attribution"
            " FROM exercises WHERE id IN ('weighted-pull-up', 'plank', 'front-lever')",
        )
        actual = {
            str(row["id"]): (
                row["standard_name_zh"],
                row["equipment_variant"],
                row["record_type"],
                row["load_convention"],
                row["min_load_increment_kg"],
                row["recommendable"],
                row["modes_json"],
                row["source_ref"],
                row["attribution"],
            )
            for row in rows
        }
        assert actual == NEW_SEED
        # 负重引体记录外加重量、最小加重 5kg，不包含体重。
        weighted = next(row for row in rows if row["id"] == "weighted-pull-up")
        assert weighted["load_convention"] == "external_added_weight"
        assert weighted["min_load_increment_kg"] == 5.0

        pull_up = await _rows(
            db,
            "SELECT record_type, load_convention, min_load_increment_kg, source_ref"
            " FROM exercises WHERE id = 'pull-up'",
        )
        assert pull_up == [
            {
                "record_type": "reps_bodyweight",
                "load_convention": None,
                "min_load_increment_kg": None,
                "source_ref": "exercises-dataset:0652",
            }
        ]
    finally:
        await db.close()


async def test_schema_has_no_volume_or_estimated_1rm_columns(tmp_path: Path) -> None:
    """已冻结范围：全库列名不含训练容量／估算 1RM／完成率，也没有统计结果表或有效工作组 View。

    与结构层断言互补：那里只能证明 PB／趋势的输出结构没有这些字段，这里证明库内连可承载它们的
    列与对象都没有（PB 现算不落表，有效工作组过滤只写在 repo 的共享 SQL 里）。
    """
    db, _ = await _upgraded_stage1_db(tmp_path)
    try:
        tables = {
            name
            for name in await _schema_names(db, "table")
            if not name.startswith("sqlite_")
        }
        assert tables == {
            "athlete_profile",
            "body_metrics",
            "exercises",
            "plan_sessions",
            "plans",
            "workout_sessions",
            "workout_sets",
        }
        assert await _schema_names(db, "view") == set()
        assert not {
            "personal_bests",
            "pb_results",
            "statistics",
        } & tables

        columns: list[str] = []
        for table in sorted(tables):
            columns.extend(
                f"{table}.{row['name']}"
                for row in await _rows(db, f"PRAGMA table_info({table})")
            )
        forbidden = ("volume", "1rm", "one_rm", "epley", "completion")
        assert [
            column
            for column in columns
            if any(word in column.lower() for word in forbidden)
        ] == []
    finally:
        await db.close()
