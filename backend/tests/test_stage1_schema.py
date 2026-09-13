"""Stage 1 S1-02：增量迁移 002 与动作目录／单用户档案最小持久化结构。

生产迁移经 storage/migrations/002_*.sql（结构）与 003_*.sql（S1-03 精选种子）；
升级、失败与高版本语义沿用 S0-04 的
临时迁移目录注入法（不向生产迁移目录塞测试用假迁移）。所有用例只操作 pytest
``tmp_path`` 下的临时文件库，不触碰真实用户数据目录、不读取真实凭据。

验收对照（stage1.md §5 S1-02）：空库可迁移；Stage 0 库升级保留会话、消息、Run、
业务时区与 Provider 配置；重复启动不重置档案或版本、不重复种子；失败迁移不留
半套结构；高版本库仍拒绝启动。边界：只建两项业务存储、不预填用户事实、
``context_version`` 不被目录查询或普通写入推进。
"""

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from storage.db import Database
from storage.errors import FutureSchemaVersion, MigrationError
from storage.migrations import DEFAULT_MIGRATIONS_DIR, load_migrations
from storage.run_repo import RunRepo
from storage.setting_repo import DEFAULT_PROVIDER, SettingRepo
from tests.support import open_database, seed_legacy_run

STAGE0_TABLES = {
    "conversations",
    "runs",
    "messages",
    "run_events",
    "app_config",
    "provider_config",
}
STAGE1_TABLES = {"exercises", "user_profile"}
# Stage 2 S2-02 由 004 迁移建立草稿表（stage2.md §5 S2-02），不再属「后续阶段不建」。
STAGE2_TABLES = {"business_drafts"}
# Stage 3 S3-02 由 005 迁移建立计划侧三表（stage3.md §5 S3-02），不再属「后续阶段不建」。
STAGE3_PLAN_TABLES = {"plan_versions", "scheduled_sessions", "arrangement_revisions"}
# Stage 3 S3-09 由 009 迁移建立记录侧四表（stage3.md §5 S3-09）。
STAGE3_RECORD_TABLES = {
    "training_sessions",
    "session_revisions",
    "exercise_logs",
    "training_sets",
}
# Stage 3 S3-13 由 011 迁移建立复盘两表（stage3.md §5 S3-13）。
STAGE3_REVIEW_TABLES = {"reviews", "review_source_revisions"}
# Stage 4 S4-06a 由 014 迁移建立摘要两表（stage4.md S4-06；07 7.4 摘要持久化）。
STAGE4_SUMMARY_TABLES = {"summaries", "summary_sources"}
# Stage 3 表已全部落地：统计侧 ``pr_candidates`` 是视图（010），不在 type='table' 扫描内。
LATER_STAGE_TABLES: set[str] = set()

LATEST_VERSION = len(load_migrations())
# 003 精选种子行数（已拍 24 项清单；见 evidence/S1-evidence-2026-09-09.md）
SEEDED_EXERCISE_COUNT = 24
FAKE_KEY = "sk-fitagent-fake-s102-not-a-real-key"

_DEFAULT_ALIASES_JSON = json.dumps(["barbell back squat"], ensure_ascii=False)
_DEFAULT_MODES_JSON = json.dumps(["深蹲"], ensure_ascii=False)


def _exercise_params(
    exercise_id: str,
    standard_name_zh: str,
    *,
    equipment_variant: str = "barbell",
    record_type: str = "reps_weight",
    load_convention: str | None = "barbell_includes_bar_total",
    unilateral: int = 0,
    recommendable: int = 0,
    active: int = 1,
    aliases_json: str = _DEFAULT_ALIASES_JSON,
    modes_json: str = _DEFAULT_MODES_JSON,
    source_ref: str = "src-0001",
    attribution: str = "© Gym visual",
    instructions_zh: str = "示例动作说明",
) -> dict[str, object]:
    """构造 exercises 行参数；默认值代表「未检查、未停用」的合法种子形态。"""
    return {
        "id": exercise_id,
        "standard_name_zh": standard_name_zh,
        "equipment_variant": equipment_variant,
        "record_type": record_type,
        "load_convention": load_convention,
        "unilateral": unilateral,
        "recommendable": recommendable,
        "active": active,
        "aliases_json": aliases_json,
        "modes_json": modes_json,
        "source_ref": source_ref,
        "attribution": attribution,
        "instructions_zh": instructions_zh,
    }


async def _insert_exercise(conn, params: dict[str, object]) -> None:
    # 参数顺序与列名一致；显式元组（不用推导式），与既有测试的写法保持一致。
    await conn.execute(
        "INSERT INTO exercises (id, standard_name_zh, equipment_variant, record_type,"
        " load_convention, unilateral, recommendable, active, aliases_json, modes_json,"
        " source_ref, attribution, instructions_zh)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            params["id"],
            params["standard_name_zh"],
            params["equipment_variant"],
            params["record_type"],
            params["load_convention"],
            params["unilateral"],
            params["recommendable"],
            params["active"],
            params["aliases_json"],
            params["modes_json"],
            params["source_ref"],
            params["attribution"],
            params["instructions_zh"],
        ),
    )


async def _table_names(db: Database) -> set[str]:
    async def op(conn):
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ) as cursor:
            return {str(row["name"]) for row in await cursor.fetchall()}

    return await db.under_lock(op)


async def _column_names(db: Database, table: str) -> set[str]:
    """逐表列名（表名来自库内 sqlite_master，不是外部输入）。"""

    async def op(conn):
        async with conn.execute(f"PRAGMA table_info({table})") as cursor:
            return {str(row["name"]) for row in await cursor.fetchall()}

    return await db.under_lock(op)


async def _exercise_count(db: Database) -> int:
    async def op(conn):
        async with conn.execute("SELECT COUNT(*) FROM exercises") as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return int(row[0])

    return await db.under_lock(op)


async def _profile_row(db: Database) -> tuple[object, object, object]:
    async def op(conn):
        async with conn.execute(
            "SELECT id, profile_json, context_version FROM user_profile"
        ) as cursor:
            rows = await cursor.fetchall()
        assert len(rows) == 1
        return (rows[0]["id"], rows[0]["profile_json"], rows[0]["context_version"])

    return await db.under_lock(op)


async def _exercise_row(db: Database, exercise_id: str) -> dict[str, object] | None:
    async def op(conn):
        async with conn.execute(
            "SELECT * FROM exercises WHERE id = ?", (exercise_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else dict(row)

    return await db.under_lock(op)


def _stage0_only_migrations(tmp_path: Path) -> Path:
    """只含 001 的临时迁移目录：用于构造「已有 Stage 0 库」的升级前状态。"""
    directory = tmp_path / "stage0-migrations"
    directory.mkdir()
    shutil.copy(
        DEFAULT_MIGRATIONS_DIR / "001_stage0_runtime_and_settings.sql", directory
    )
    return directory


# ---------- 空库迁移与范围边界 ----------


async def test_fresh_database_migrates_to_latest_with_seed_and_no_profile_facts(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        assert await db.migrate() == LATEST_VERSION
        assert await db.pragma_value("user_version") == LATEST_VERSION

        tables = await _table_names(db)
        assert tables == (
            STAGE0_TABLES
            | STAGE1_TABLES
            | STAGE2_TABLES
            | STAGE3_PLAN_TABLES
            | STAGE3_RECORD_TABLES
            | STAGE3_REVIEW_TABLES
            | STAGE4_SUMMARY_TABLES
            | {"sqlite_sequence"}
        )
        assert tables & LATER_STAGE_TABLES == set()  # 不建统计／复盘侧业务表

        # 003 只写入精选产品种子：结构就位且目录非空，但仍无用户事实
        assert await _exercise_count(db) == SEEDED_EXERCISE_COUNT

        # 档案只有「未建档」技术载体：无用户事实、版本 0
        assert await _profile_row(db) == (1, None, 0)


async def test_stage0_upgrade_preserves_runtime_rows_timezone_and_credentials(
    tmp_path: Path,
) -> None:
    """已有 Stage 0 库升级到最新（002 结构 + 003 种子）：数据保留、新结构就位、凭据仍可内部读取。"""
    path = tmp_path / "app.db"
    stage0_dir = _stage0_only_migrations(tmp_path)

    async with open_database(path, migrations_dir=stage0_dir) as db:
        assert await db.pragma_value("user_version") == 1
        runs = RunRepo(db)
        settings = SettingRepo(db)
        await seed_legacy_run(
            db,
            conversation_id="c-legacy",
            run_id="r-legacy",
            client_request_id="cri-legacy",
            text="旧库用户请求",
        )
        assert (await settings.initialize_business_timezone(lambda: "Asia/Shanghai"))[
            "initialized"
        ]
        await settings.set_provider_api_key(DEFAULT_PROVIDER, FAKE_KEY)

    # 用生产迁移目录重开同一库：只执行缺失的 002/003
    async with open_database(path) as db:
        assert await db.migrate() == LATEST_VERSION
        runs = RunRepo(db)
        settings = SettingRepo(db)

        conversation = await runs.get_conversation("c-legacy")
        assert conversation is not None
        run = await runs.get_run("r-legacy")
        assert run is not None and run["status"] == "pending"
        assert [m["kind"] for m in await runs.list_messages("c-legacy")] == [
            "user_request"
        ]
        assert [e["event_type"] for e in await runs.list_run_events("r-legacy")] == [
            "note"
        ]
        assert await settings.get_business_timezone() == "Asia/Shanghai"
        assert (
            await settings.get_provider_api_key_internal(DEFAULT_PROVIDER) == FAKE_KEY
        )
        status = await settings.get_provider_status(DEFAULT_PROVIDER)
        assert status is not None and status["has_api_key"] is True

        # 新结构就位且仍不预填用户事实；003 种子随迁移一并写入
        tables = await _table_names(db)
        assert tables >= STAGE1_TABLES
        assert await _exercise_count(db) == SEEDED_EXERCISE_COUNT
        assert await _profile_row(db) == (1, None, 0)


async def test_repeated_start_does_not_reset_profile_version_or_catalog_rows(
    tmp_path: Path,
) -> None:
    """重复启动只跳过已执行迁移：不重置档案与版本、不覆盖停用状态、不重复行。"""
    path = tmp_path / "app.db"
    sample_profile = {"body_weight_kg": 70.0, "goal": "力量"}
    async with open_database(path) as db, db.transaction() as conn:
        await conn.execute(
            "UPDATE user_profile SET profile_json = ?, context_version = 7"
            " WHERE id = 1",
            (json.dumps(sample_profile, ensure_ascii=False),),
        )
        await _insert_exercise(
            conn, _exercise_params("ex-1", "示例动作-杠铃", recommendable=1, active=0)
        )

    async with open_database(path) as db:
        assert await db.migrate() == LATEST_VERSION  # 无新迁移可执行
        assert await _profile_row(db) == (
            1,
            json.dumps(sample_profile, ensure_ascii=False),
            7,
        )
        assert (
            await _exercise_count(db) == SEEDED_EXERCISE_COUNT + 1
        )  # 种子 + 本用例样本行
        row = await _exercise_row(db, "ex-1")
        assert row is not None
        assert row["active"] == 0  # 停用状态不被重置
        assert row["recommendable"] == 1  # 已检查标记不被覆盖


async def test_failed_stage1_migration_leaves_no_partial_structure(
    tmp_path: Path,
) -> None:
    """002 失败：版本不虚报、不留半套结构、Stage 0 数据完好，修复后可继续。"""
    directory = tmp_path / "migrations"
    directory.mkdir()
    shutil.copy(
        DEFAULT_MIGRATIONS_DIR / "001_stage0_runtime_and_settings.sql", directory
    )
    broken = directory / "002_stage1_actions_profile.sql"
    broken.write_text(
        "CREATE TABLE exercises (id TEXT PRIMARY KEY);\nTHIS IS NOT VALID SQL;",
        encoding="utf-8",
    )
    path = tmp_path / "app.db"
    async with open_database(path, migrate=False, migrations_dir=directory) as db:
        with pytest.raises(MigrationError, match="002"):
            await db.migrate()
        assert await db.pragma_value("user_version") == 1
        tables = await _table_names(db)
        assert tables == STAGE0_TABLES | {"sqlite_sequence"}
        assert "exercises" not in tables  # 半套结构已回滚

        broken.write_text(
            "CREATE TABLE exercises (id TEXT PRIMARY KEY);", encoding="utf-8"
        )
        assert await db.migrate() == 2
        assert "exercises" in await _table_names(db)


async def test_higher_schema_version_is_refused_with_stage1_migrations(
    tmp_path: Path,
) -> None:
    """库版本高于程序支持：仍拒绝启动，不降级、不重建。"""
    async with open_database(tmp_path / "app.db") as db:
        async with db.transaction() as conn:
            await conn.execute("PRAGMA user_version = 99")
        with pytest.raises(FutureSchemaVersion, match="99"):
            await db.migrate()
        assert await db.pragma_value("user_version") == 99


# ---------- 结构约束（口径可区分、停用不删除、档案载体） ----------


async def test_record_type_accepts_only_the_three_decided_kinds(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        async with db.transaction() as conn:
            await _insert_exercise(conn, _exercise_params("ex-1", "示例动作-杠铃"))
            await _insert_exercise(
                conn,
                _exercise_params(
                    "ex-bodyweight",
                    "示例动作-自重",
                    equipment_variant="bodyweight",
                    record_type="reps_bodyweight",
                    load_convention=None,
                    aliases_json=json.dumps(["pull up"], ensure_ascii=False),
                ),
            )
            await _insert_exercise(
                conn,
                _exercise_params(
                    "ex-timed",
                    "示例计时动作",
                    equipment_variant="bodyweight",
                    record_type="time",
                    load_convention=None,
                ),
            )
        async with db.transaction() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                # 不新增辅助负重型（03「本章已拍结论」）
                await _insert_exercise(
                    conn,
                    _exercise_params(
                        "ex-assisted", "示例辅助动作", record_type="reps_assisted"
                    ),
                )

        async def ddl(conn):
            async with conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'exercises'"
            ) as cursor:
                return str((await cursor.fetchone())["sql"])

        exercises_ddl = await db.under_lock(ddl)
        for kind in ("reps_weight", "reps_bodyweight", "time"):
            assert kind in exercises_ddl
        assert "reps_assisted" not in exercises_ddl


async def test_load_convention_required_for_weight_reps_only(tmp_path: Path) -> None:
    conventions = (
        "barbell_includes_bar_total",
        "dumbbell_per_hand",
        "machine_pin_displayed_value",
        "plate_loaded_total_excluding_empty",
        "unilateral_setting_per_side",
    )
    async with open_database(tmp_path / "app.db") as db:
        # 五种已拍负重口径都被接受
        async with db.transaction() as conn:
            for index, convention in enumerate(conventions):
                await _insert_exercise(
                    conn,
                    _exercise_params(
                        f"ex-conv-{index}",
                        f"示例负重动作{index}",
                        load_convention=convention,
                    ),
                )
        async with db.transaction() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                await _insert_exercise(
                    conn,
                    _exercise_params("ex-no-conv", "缺口径", load_convention=None),
                )
        async with db.transaction() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                await _insert_exercise(
                    conn,
                    _exercise_params(
                        "ex-bw-conv",
                        "自重带口径",
                        record_type="reps_bodyweight",
                        load_convention="dumbbell_per_hand",
                    ),
                )
        async with db.transaction() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                await _insert_exercise(
                    conn,
                    _exercise_params(
                        "ex-unknown-conv",
                        "未知口径",
                        load_convention="cable_stack_half_value",
                    ),
                )


async def test_stop_keeps_row_and_defaults_stay_not_recommendable(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        async with db.transaction() as conn:
            await _insert_exercise(conn, _exercise_params("ex-1", "示例动作-杠铃"))
        row = await _exercise_row(db, "ex-1")
        assert row is not None
        assert row["recommendable"] == 0  # 未检查不得进推荐候选
        assert row["active"] == 1
        assert row["unilateral"] == 0

        async with db.transaction() as conn:
            await conn.execute("UPDATE exercises SET active = 0 WHERE id = 'ex-1'")
        stopped = await _exercise_row(db, "ex-1")
        assert stopped is not None  # 停用不删除
        assert stopped["active"] == 0
        assert stopped["aliases_json"] == _DEFAULT_ALIASES_JSON
        assert stopped["load_convention"] == "barbell_includes_bar_total"
        assert stopped["recommendable"] == 0


async def test_profile_row_is_singleton_and_json_validated(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        async with db.transaction() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                await conn.execute(
                    "INSERT INTO user_profile (id, profile_json, context_version)"
                    " VALUES (2, NULL, 0)"
                )
        async with db.transaction() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                await conn.execute(
                    "UPDATE user_profile SET profile_json = 'not json' WHERE id = 1"
                )
        async with db.transaction() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                await conn.execute(
                    "UPDATE user_profile SET context_version = -1 WHERE id = 1"
                )
        async with db.transaction() as conn:
            await conn.execute(
                'UPDATE user_profile SET profile_json = \'{"goal": "力量"}\''
                " WHERE id = 1"
            )
        assert await _profile_row(db) == (1, '{"goal": "力量"}', 0)


async def test_exercises_json_columns_require_valid_json(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        async with db.transaction() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                await _insert_exercise(
                    conn,
                    _exercise_params("ex-1", "示例动作-杠铃", aliases_json="not json"),
                )
        async with db.transaction() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                await _insert_exercise(
                    conn, _exercise_params("ex-2", "示例动作", modes_json="not json")
                )


async def test_catalog_activity_never_advances_context_version(tmp_path: Path) -> None:
    """目录写入与读取都不得推进统一业务版本（stage1.md §5 S1-02 边界）。"""
    async with open_database(tmp_path / "app.db") as db:
        async with db.transaction() as conn:
            await _insert_exercise(
                conn, _exercise_params("ex-1", "示例动作-杠铃", recommendable=1)
            )

        # 推荐候选筛选与按身份读取（本阶段只有结构，用等价 SQL 表达读取）
        async def read_candidates(conn):
            async with conn.execute(
                "SELECT id FROM exercises WHERE active = 1 AND recommendable = 1"
            ) as cursor:
                return [str(row["id"]) for row in await cursor.fetchall()]

        # 系统迁移已将已核对 24 项种子置为可推荐（S3-02/D2），新增目录行进入同一筛选
        candidates = await db.under_lock(read_candidates)
        assert "ex-1" in candidates
        assert len(candidates) == SEEDED_EXERCISE_COUNT + 1
        assert await _profile_row(db) == (1, None, 0)
        # 不存在第二套业务版本计数器：统一业务版本（context_version）只在档案行上；
        # Stage 3 的 plan_versions 是计划版本序列，不是业务版本计数器（01 1.4/D9）
        tables = await _table_names(db)
        for table in sorted(tables):
            if table == "user_profile":
                continue
            assert "context_version" not in await _column_names(db, table), table


async def test_flag_columns_reject_non_boolean_and_standard_name_stays_unique(
    tmp_path: Path,
) -> None:
    """约束检查补齐：三个标记列只接受 0/1；一个身份只有一个标准名。"""
    async with open_database(tmp_path / "app.db") as db:
        for column in ("unilateral", "recommendable", "active"):
            params = _exercise_params(f"ex-{column}", f"示例动作-{column}")
            params[column] = 2
            async with db.transaction() as conn:
                with pytest.raises(sqlite3.IntegrityError):
                    await _insert_exercise(conn, params)

        async with db.transaction() as conn:
            await _insert_exercise(conn, _exercise_params("ex-1", "示例动作-杠铃"))
        async with db.transaction() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                # 同一标准名不得挂到第二个身份（03 3.1 动作身份归一）
                await _insert_exercise(conn, _exercise_params("ex-2", "示例动作-杠铃"))
