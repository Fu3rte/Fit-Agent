"""Stage 2 S2-02：迁移 004（business_drafts）、草稿 repo 最小持久化与事务内读取能力。

验收对照（stage2.md §5 S2-02）：

- 空库迁移、Stage 0／1 库升级、重复启动、迁移失败回滚、高版本库拒绝启动均正确；
  旧会话、档案、动作目录、设置及版本不变。
- 只新增 ``business_drafts`` 一张表；既有表集合与凭据泄漏扫描断言显式扩展
  （见 ``tests/test_migrations.py``、``tests/test_stage1_schema.py``、
  ``tests/test_provider_settings.py``，均未删除或用 skip 绕过）。
- 草稿 repo 保存快照、revision、来源与凭据；事务内读取不嵌套取锁、不读另一连接的快照；
  临时文件库升级、约束失败与关闭重开的自动化在本文件。来源配对（Run 必须同会话）与
  创建原子性（读回失败／取消不留行）也由本文件覆盖。

边界：不实现草稿创建／Diff（S2-03）、纠错与丢弃（S2-04）、确认事务（S2-05）、过期拦截
（S2-06）与业务 API（S2-07）。本文件只用内部 repo 与 pytest ``tmp_path`` 下的临时文件库，
不触碰真实用户数据目录、不读取真实凭据、不联网；非 Windows 平台结果不作为 Windows 门槛。
"""

import asyncio
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from app import draft_repo
from app.draft_repo import DRAFT_STATUSES, INITIAL_REVISION, DraftRepo
from domain.actions.repo import ExerciseRepo
from domain.actions.schema import Exercise
from domain.profile.repo import ProfileRepo
from domain.profile.schema import (
    ActionRestriction,
    Fact,
    Profile,
    ProfileSnapshot,
    profile_to_json,
)
from domain.profile.service import ProfileService
from storage.db import Database
from storage.errors import FutureSchemaVersion, MigrationError
from storage.migrations import DEFAULT_MIGRATIONS_DIR, load_migrations
from storage.run_repo import RunRepo
from storage.setting_repo import DEFAULT_PROVIDER, SettingRepo
from tests.support import open_database

STAGE0_TABLES = {
    "conversations",
    "runs",
    "messages",
    "run_events",
    "app_config",
    "provider_config",
}
STAGE1_TABLES = {"exercises", "user_profile"}
# S2-02 只新增这一张表（stage2.md §5 S2-02 边界）。
STAGE2_TABLES = {"business_drafts"}
# S3-02 由 005 迁移新增计划侧三表（stage3.md §5 S3-02 边界）。
STAGE3_PLAN_TABLES = {"plan_versions", "scheduled_sessions", "arrangement_revisions"}
# S3-09 由 009 迁移新增记录侧四表（stage3.md §5 S3-09 边界）。
STAGE3_RECORD_TABLES = {
    "training_sessions",
    "session_revisions",
    "exercise_logs",
    "training_sets",
}
# S3-13 由 011 迁移新增复盘两表（stage3.md §5 S3-13 边界）。
STAGE3_REVIEW_TABLES = {"reviews", "review_source_revisions"}
# S4-06a 由 014 迁移新增摘要两表（stage4.md S4-06；07 7.4 摘要持久化）。
STAGE4_SUMMARY_TABLES = {"summaries", "summary_sources"}
# Stage 3 表已全部落地：统计侧 ``pr_candidates`` 是视图（010），不在 type='table' 扫描内。
LATER_STAGE_TABLES: set[str] = set()

LATEST_VERSION = len(load_migrations())
STAGE0_VERSION = 1
STAGE1_VERSION = 3
STAGE2_VERSION = 4  # 004_stage2_business_drafts.sql 执行后的 user_version
SEEDED_EXERCISE_COUNT = 24  # 003 精选种子行数（stage1.md §5 S1-03：已拍 24 项清单）
FAKE_KEY = "sk-fitagent-fake-s202-not-a-real-key"


def _seeded_profile() -> Profile:
    """已建档样本：结构合法的最小事实集合（S2-01 的部分档案可保存形态）。"""
    return Profile(training_goal=Fact.known("力量"), body_weight_kg=Fact.known(72.5))


def _proposed_profile() -> Profile:
    """草稿拟议结果样本：改目标、补经验与频率、加一条模式限制、明确无器械。"""
    return Profile(
        training_goal=Fact.known("增肌"),
        training_experience=Fact.known("零基础"),
        weekly_frequency=Fact.known(3),
        session_duration_minutes=Fact.known(60),
        available_equipment=Fact.denied(),
        action_restrictions=Fact.known(
            (ActionRestriction(scope="movement_pattern", target="深蹲"),)
        ),
        body_weight_kg=Fact.known(73.0),
    )


async def _table_names(db: Database) -> set[str]:
    async def op(conn):
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ) as cursor:
            return {str(row["name"]) for row in await cursor.fetchall()}

    return await db.under_lock(op)


async def _profile_row(db: Database) -> tuple[object, object]:
    async def op(conn):
        async with conn.execute(
            "SELECT profile_json, context_version FROM user_profile WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return (row["profile_json"], row["context_version"])

    return await db.under_lock(op)


async def _exercise_count(db: Database) -> int:
    async def op(conn):
        async with conn.execute("SELECT COUNT(*) FROM exercises") as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return int(row[0])

    return await db.under_lock(op)


async def _draft_count(db: Database) -> int:
    async def op(conn):
        async with conn.execute("SELECT COUNT(*) FROM business_drafts") as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return int(row[0])

    return await db.under_lock(op)


async def _table_ddl(db: Database, table: str) -> str:
    async def op(conn):
        async with conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return str(row["sql"])

    return await db.under_lock(op)


def _stage0_only_migrations(tmp_path: Path) -> Path:
    """只含 001 的临时迁移目录：构造「已有 Stage 0 库」的升级前状态。"""
    directory = tmp_path / "stage0-migrations"
    directory.mkdir()
    shutil.copy(
        DEFAULT_MIGRATIONS_DIR / "001_stage0_runtime_and_settings.sql", directory
    )
    return directory


def _stage1_only_migrations(tmp_path: Path) -> Path:
    """只含 001–003 的临时迁移目录：构造「已有 Stage 1 库」的升级前状态。"""
    directory = tmp_path / "stage1-migrations"
    directory.mkdir()
    for name in (
        "001_stage0_runtime_and_settings.sql",
        "002_stage1_actions_profile.sql",
        "003_stage1_action_seed.sql",
    ):
        shutil.copy(DEFAULT_MIGRATIONS_DIR / name, directory)
    return directory


async def _insert_exercise(conn, exercise_id: str) -> None:
    """插入一条合法动作身份（其余列取未检查、未停用的合法形态）。"""
    await conn.execute(
        "INSERT INTO exercises (id, standard_name_zh, equipment_variant, record_type,"
        " load_convention, unilateral, recommendable, active, aliases_json, modes_json,"
        " source_ref, attribution, instructions_zh)"
        " VALUES (?, ?, 'barbell', 'reps_weight', 'barbell_includes_bar_total', 0, 0, 1,"
        " ?, ?, 'src-test', '© test', '示例说明')",
        (
            exercise_id,
            f"示例动作-{exercise_id}",
            json.dumps(["test alias"], ensure_ascii=False),
            json.dumps(["深蹲"], ensure_ascii=False),
        ),
    )


async def _insert_draft_row(conn, **overrides: object) -> None:
    """按 business_drafts 列序插入一行；默认是合法 Pending 行，用例只改需违反约束的列。"""
    params: dict[str, object] = {
        "id": "d-raw",
        "conversation_id": "c-raw",
        "run_id": None,
        "base_profile_json": None,
        "proposed_profile_json": profile_to_json(_proposed_profile()),
        "base_business_version": 0,
        "revision": 1,
        "status": "pending",
        "committed_revision": None,
        "committed_business_version": None,
        "created_at": "2026-09-10T00:00:00+00:00",
        "updated_at": "2026-09-10T00:00:00+00:00",
    }
    params.update(overrides)
    await conn.execute(
        "INSERT INTO business_drafts (id, conversation_id, run_id, base_profile_json,"
        " proposed_profile_json, base_business_version, revision, status,"
        " committed_revision, committed_business_version, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            params["id"],
            params["conversation_id"],
            params["run_id"],
            params["base_profile_json"],
            params["proposed_profile_json"],
            params["base_business_version"],
            params["revision"],
            params["status"],
            params["committed_revision"],
            params["committed_business_version"],
            params["created_at"],
            params["updated_at"],
        ),
    )


# ---------- 迁移：空库、Stage 1 升级、重复启动、失败回滚、高版本 ----------


async def test_fresh_database_creates_only_the_draft_table_beyond_stage1(
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
        assert tables & LATER_STAGE_TABLES == set()  # 统计／复盘侧表仍不得建

        # 迁移只建表，不预填草稿、不预填用户事实、不推进版本
        assert await _draft_count(db) == 0
        assert await _profile_row(db) == (None, 0)
        assert await _exercise_count(db) == SEEDED_EXERCISE_COUNT


async def test_stage1_upgrade_adds_drafts_and_preserves_sessions_profile_catalog_settings(
    tmp_path: Path,
) -> None:
    """已有 Stage 1 库升级到 004：数据保留、新表就位、草稿不预填。"""
    path = tmp_path / "app.db"
    stage1_dir = _stage1_only_migrations(tmp_path)
    seeded = _seeded_profile()

    async with open_database(path, migrations_dir=stage1_dir) as db:
        assert await db.pragma_value("user_version") == STAGE1_VERSION
        runs = RunRepo(db)
        settings = SettingRepo(db)
        await runs.create_conversation("c-legacy")
        await runs.create_run_with_user_message(
            "c-legacy", "r-legacy", "cri-legacy", "旧库用户请求"
        )
        await runs.append_run_events("r-legacy", [("note", {"legacy": True})])
        assert (await settings.initialize_business_timezone(lambda: "Asia/Shanghai"))[
            "initialized"
        ]
        await settings.set_provider_api_key(DEFAULT_PROVIDER, FAKE_KEY)
        async with db.transaction() as conn:
            await ProfileService(db).write_profile_in_transaction(conn, seeded)
            await conn.execute(
                "UPDATE user_profile SET context_version = 7 WHERE id = 1"
            )
            await conn.execute(
                "UPDATE exercises SET active = 0, recommendable = 1"
                " WHERE id = 'barbell-back-squat'"
            )

    # 用生产迁移目录（含 004）重开同一库：只执行缺失的 004
    async with open_database(path) as db:
        assert await db.migrate() == LATEST_VERSION
        runs = RunRepo(db)
        settings = SettingRepo(db)

        assert (await runs.get_conversation("c-legacy")) is not None
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

        # 档案与统一业务版本不变
        assert await ProfileRepo(db).read() == ProfileSnapshot(
            profile=seeded, context_version=7
        )
        # 动作目录不被重跑迁移覆盖：行数与样本停用／已检查标记都保留
        assert await _exercise_count(db) == SEEDED_EXERCISE_COUNT
        sample = await ExerciseRepo(db).get_by_id("barbell-back-squat")
        assert sample is not None and sample.active is False
        assert sample.recommendable is True

        # 新表就位且没有任何草稿行
        assert "business_drafts" in await _table_names(db)
        assert await _draft_count(db) == 0


async def test_stage0_upgrade_reaches_latest_and_preserves_legacy_sessions(
    tmp_path: Path,
) -> None:
    """已有 Stage 0 库（仅 001）升级到最新：002–004 依序补齐，旧会话数据原样保留。"""
    path = tmp_path / "app.db"
    stage0_dir = _stage0_only_migrations(tmp_path)

    async with open_database(path, migrations_dir=stage0_dir) as db:
        assert await db.pragma_value("user_version") == STAGE0_VERSION
        # 升级前只有 Stage 0 运行时结构；档案载体、动作目录与草稿表都不存在
        tables = await _table_names(db)
        assert "user_profile" not in tables
        assert "exercises" not in tables
        assert "business_drafts" not in tables
        runs = RunRepo(db)
        await runs.create_conversation("c-stage0")
        await runs.create_run_with_user_message(
            "c-stage0", "r-stage0", "cri-stage0", "Stage 0 旧用户请求"
        )
        await runs.append_run_events("r-stage0", [("note", {"legacy": True})])

    # 用生产迁移目录（含 002–004）重开同一库：缺失迁移依序执行
    async with open_database(path) as db:
        assert await db.migrate() == LATEST_VERSION
        # 旧会话、Run、消息与事件跨升级保留
        runs = RunRepo(db)
        assert (await runs.get_conversation("c-stage0")) is not None
        run = await runs.get_run("r-stage0")
        assert run is not None and run["status"] == "pending"
        assert [m["kind"] for m in await runs.list_messages("c-stage0")] == [
            "user_request"
        ]
        assert [e["event_type"] for e in await runs.list_run_events("r-stage0")] == [
            "note"
        ]
        # 002/003/004 结构就位：档案载体为空、目录种子完整、草稿表为空
        assert await _profile_row(db) == (None, 0)
        assert await _exercise_count(db) == SEEDED_EXERCISE_COUNT
        assert await _draft_count(db) == 0


async def test_repeated_start_keeps_drafts_and_does_not_rerun_migration(
    tmp_path: Path,
) -> None:
    """重复启动只跳过已执行迁移：草稿内容、基线与凭据都不被重置。"""
    path = tmp_path / "app.db"
    async with open_database(path) as db:
        await RunRepo(db).create_conversation("c1")
        created = await DraftRepo(db).create_pending(
            draft_id="d1",
            conversation_id="c1",
            run_id=None,
            base_profile_json=profile_to_json(_seeded_profile()),
            proposed_profile_json=profile_to_json(_proposed_profile()),
            base_business_version=3,
        )
        assert created.revision == INITIAL_REVISION

    async with open_database(path) as db:
        assert await db.migrate() == LATEST_VERSION  # 无新迁移可执行
        drafts = await DraftRepo(db).list_for_conversation("c1")
        assert [draft.id for draft in drafts] == ["d1"]
        assert drafts[0] == created
        assert await _draft_count(db) == 1


async def test_failed_draft_migration_leaves_no_partial_structure(
    tmp_path: Path,
) -> None:
    """004 失败：版本不虚报、不留半套结构、Stage 1 数据完好，修复后可继续（不删库）。"""
    directory = tmp_path / "migrations"
    directory.mkdir()
    for name in (
        "001_stage0_runtime_and_settings.sql",
        "002_stage1_actions_profile.sql",
        "003_stage1_action_seed.sql",
    ):
        shutil.copy(DEFAULT_MIGRATIONS_DIR / name, directory)
    broken = directory / "004_stage2_business_drafts.sql"
    broken.write_text(
        "CREATE TABLE business_drafts (id TEXT PRIMARY KEY);\nTHIS IS NOT VALID SQL;",
        encoding="utf-8",
    )
    path = tmp_path / "app.db"
    async with open_database(path, migrate=False, migrations_dir=directory) as db:
        with pytest.raises(MigrationError, match="004"):
            await db.migrate()
        assert await db.pragma_value("user_version") == STAGE1_VERSION
        tables = await _table_names(db)
        assert "business_drafts" not in tables  # 半套结构已回滚
        assert (
            await _exercise_count(db) == SEEDED_EXERCISE_COUNT
        )  # 已执行的 001–003 保留

        broken.write_text(
            "CREATE TABLE business_drafts (id TEXT PRIMARY KEY);", encoding="utf-8"
        )
        # 临时迁移目录只含 001–004：修复后只跑到本阶段版本（不把生产新增编号算进来）
        assert await db.migrate() == STAGE2_VERSION
        assert "business_drafts" in await _table_names(db)


async def test_higher_schema_version_is_refused_with_draft_migration(
    tmp_path: Path,
) -> None:
    """库版本高于程序支持：停止启动，不降级、不重建，也不执行 004。"""
    async with open_database(tmp_path / "app.db") as db:
        async with db.transaction() as conn:
            await conn.execute("PRAGMA user_version = 99")
        with pytest.raises(FutureSchemaVersion, match="99"):
            await db.migrate()
        assert await db.pragma_value("user_version") == 99
    assert (tmp_path / "app.db").exists()  # 未删除用户数据库


# ---------- 结构约束：生命周期三态、凭据配对、快照与来源 ----------


async def test_status_accepts_only_the_three_decided_lifecycle_states(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c-raw")
        async with db.transaction() as conn:
            await _insert_draft_row(conn, id="d-pending", status="pending")
            await _insert_draft_row(
                conn,
                id="d-committed",
                status="committed",
                committed_revision=1,
                committed_business_version=1,
            )
            await _insert_draft_row(conn, id="d-discarded", status="discarded")
        for forbidden in ("stale", "expired", "discard"):
            async with db.transaction() as conn:
                with pytest.raises(sqlite3.IntegrityError):
                    await _insert_draft_row(conn, id=f"d-{forbidden}", status=forbidden)

        ddl = await _table_ddl(db, "business_drafts")
        for state in DRAFT_STATUSES:
            assert f"'{state}'" in ddl
        assert (
            "stale" not in ddl and "expired" not in ddl
        )  # 过期不是持久化状态（01 1.3）

        drafts = await DraftRepo(db).list_for_conversation("c-raw")
        assert sorted(draft.status for draft in drafts) == [
            "committed",
            "discarded",
            "pending",
        ]


async def test_committed_credentials_and_lifecycle_are_coupled(
    tmp_path: Path,
) -> None:
    """Committed 必有凭据；Pending／Discarded 必无凭据（库内 CHECK 同进同退）。"""
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c-raw")
        invalid_rows = (
            {"status": "committed"},
            {
                "status": "committed",
                "committed_revision": 1,
            },
            {
                "status": "committed",
                "committed_business_version": 1,
            },
            {
                "status": "pending",
                "committed_revision": 1,
                "committed_business_version": 1,
            },
            {
                "status": "discarded",
                "committed_revision": 1,
                "committed_business_version": 1,
            },
        )
        for index, overrides in enumerate(invalid_rows):
            async with db.transaction() as conn:
                with pytest.raises(sqlite3.IntegrityError):
                    await _insert_draft_row(conn, id=f"d-bad-{index}", **overrides)
        assert await _draft_count(db) == 0


async def test_committed_credential_must_match_the_committed_revision(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c-raw")
        async with db.transaction() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                await _insert_draft_row(
                    conn,
                    id="d-mismatch",
                    revision=2,
                    status="committed",
                    committed_revision=1,
                    committed_business_version=1,
                )
        async with db.transaction() as conn:
            await _insert_draft_row(
                conn,
                id="d-match",
                revision=2,
                status="committed",
                committed_revision=2,
                committed_business_version=1,
            )
        draft = await DraftRepo(db).get("d-match")
        assert draft is not None
        assert (draft.revision, draft.committed_revision) == (2, 2)


async def test_snapshot_columns_require_valid_profile_json(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c-raw")
        invalid_rows = (
            {"proposed_profile_json": "not json"},
            {"proposed_profile_json": "["},
            {"proposed_profile_json": None},
            {"base_profile_json": "{"},
        )
        for index, overrides in enumerate(invalid_rows):
            async with db.transaction() as conn:
                with pytest.raises(sqlite3.IntegrityError):
                    await _insert_draft_row(conn, id=f"d-json-{index}", **overrides)
        # 未建档基线（NULL）是合法表达，与全未知档案基线不是同一语义
        async with db.transaction() as conn:
            await _insert_draft_row(conn, id="d-null-base", base_profile_json=None)
        assert await _draft_count(db) == 1


async def test_source_must_reference_existing_conversation_and_run(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        runs = RunRepo(db)
        await runs.create_conversation("c-raw")
        await runs.create_run_with_user_message("c-raw", "r-raw", "cri-raw", "请求")

        async with db.transaction() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                await _insert_draft_row(
                    conn, id="d-bad-conv", conversation_id="c-missing"
                )
        async with db.transaction() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                await _insert_draft_row(conn, id="d-bad-run", run_id="r-missing")
        # Run 存在但属于另一会话：组合外键（conversation_id, run_id）在库层拒绝，
        # 两个身份各自存在也救不了跨会话来源（stage2.md §4.1：不同来源关联不混淆）
        await runs.create_conversation("c-other")
        # 08 8.2：同时只允许一个活跃 Run；本用例需要两个既有 Run 行，故先让 r-raw 结束
        # （结束只改状态、不删行，外键目标仍在）。
        await runs.cancel_run("r-raw")
        await runs.create_run_with_user_message(
            "c-other", "r-other", "cri-other", "另一会话的请求"
        )
        async with db.transaction() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                await _insert_draft_row(
                    conn, id="d-cross", conversation_id="c-raw", run_id="r-other"
                )
        assert await DraftRepo(db).get("d-cross") is None
        async with db.transaction() as conn:
            await _insert_draft_row(conn, id="d-ok", run_id="r-raw")
        draft = await DraftRepo(db).get("d-ok")
        assert draft is not None
        assert (draft.conversation_id, draft.run_id) == ("c-raw", "r-raw")


async def test_revision_and_base_business_version_have_lower_bounds(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c-raw")
        async with db.transaction() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                await _insert_draft_row(conn, id="d-rev0", revision=0)
        async with db.transaction() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                await _insert_draft_row(conn, id="d-verneg", base_business_version=-1)
        assert await _draft_count(db) == 0


# ---------- 草稿 repo：快照／revision／来源／凭据与重开 ----------


async def test_create_pending_saves_snapshot_revision_source_and_leaves_formal_data_untouched(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c1")
        baseline = profile_to_json(_seeded_profile())
        proposed = profile_to_json(_proposed_profile())
        draft = await DraftRepo(db).create_pending(
            draft_id="d1",
            conversation_id="c1",
            run_id=None,
            base_profile_json=baseline,
            proposed_profile_json=proposed,
            base_business_version=3,
        )
        assert draft.id == "d1"
        assert draft.status == "pending" and draft.is_pending is True
        assert draft.revision == INITIAL_REVISION
        assert draft.base_profile_json == baseline
        assert draft.proposed_profile_json == proposed
        assert draft.base_business_version == 3
        assert draft.run_id is None
        assert (draft.committed_revision, draft.committed_business_version) == (
            None,
            None,
        )
        assert (await DraftRepo(db).get("d1")) == draft
        # 草稿保存不产生正式事实：档案载体与统一业务版本原样（01 1.4）
        assert await _profile_row(db) == (None, 0)
        # 未知身份明确返回 None，不创建新草稿
        assert await DraftRepo(db).get("d-missing") is None
        assert await _draft_count(db) == 1


async def test_create_pending_rejects_run_from_another_conversation(
    tmp_path: Path,
) -> None:
    """repo 创建路径同样拒绝跨会话来源（004 组合外键），失败后不留任何草稿行。"""
    async with open_database(tmp_path / "app.db") as db:
        runs = RunRepo(db)
        await runs.create_conversation("c1")
        await runs.create_conversation("c2")
        await runs.create_run_with_user_message("c2", "r2", "cri2", "c2 的请求")

        drafts = DraftRepo(db)
        with pytest.raises(sqlite3.IntegrityError):
            await drafts.create_pending(
                draft_id="d-cross",
                conversation_id="c1",
                run_id="r2",
                base_profile_json=None,
                proposed_profile_json=profile_to_json(_proposed_profile()),
                base_business_version=0,
            )
        assert await drafts.get("d-cross") is None
        assert await _draft_count(db) == 0

        # 正例对照：Run 与会话同属时创建成功（repo 路径的非空 run_id）
        # 08 8.2：先让 c2 的 Run 结束，才能再创建一个活跃 Run
        await runs.cancel_run("r2")
        await runs.create_run_with_user_message("c1", "r1", "cri1", "c1 的请求")
        paired = await drafts.create_pending(
            draft_id="d-paired",
            conversation_id="c1",
            run_id="r1",
            base_profile_json=None,
            proposed_profile_json=profile_to_json(_proposed_profile()),
            base_business_version=0,
        )
        assert paired.run_id == "r1"
        assert await _draft_count(db) == 1


async def test_create_pending_readback_failure_leaves_no_draft_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """插入成功但读回失败：报失败且库里不留草稿（单一事务回滚，非半状态）。

    旧实现把 INSERT＋读回放在 ``under_lock`` 回调里（autocommit）：读回失败时草稿行
    已经落库——本用例在该实现下必然失败（S2-02 评审修复项）。
    """
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c1")

        async def broken_readback(conn, draft_id: str):
            raise RuntimeError("注入：草稿读回失败")

        monkeypatch.setattr(draft_repo, "_require_row", broken_readback)
        with pytest.raises(RuntimeError, match="读回失败"):
            await DraftRepo(db).create_pending(
                draft_id="d-rollback",
                conversation_id="c1",
                run_id=None,
                base_profile_json=None,
                proposed_profile_json=profile_to_json(_proposed_profile()),
                base_business_version=0,
            )
        assert await _draft_count(db) == 0
        # 无锁泄漏、连接可用：同一连接上后续操作正常
        await RunRepo(db).create_conversation("c2")
        assert await _draft_count(db) == 0


async def test_create_pending_cancelled_after_insert_leaves_no_draft_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """插入之后取消：事务整体回滚、取消继续传播，无半条草稿、无锁泄漏。

    注入点在读回等待处抛 ``CancelledError``，覆盖 ``transaction()`` 的取消回滚分支
    （07 7.1：事务异常不遗留开放事务或被占用的锁）。
    """
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c1")

        async def cancelled_readback(conn, draft_id: str):
            raise asyncio.CancelledError  # 注入：读回等待期间任务被取消

        monkeypatch.setattr(draft_repo, "_require_row", cancelled_readback)
        with pytest.raises(asyncio.CancelledError):
            await DraftRepo(db).create_pending(
                draft_id="d-cancel",
                conversation_id="c1",
                run_id=None,
                base_profile_json=None,
                proposed_profile_json=profile_to_json(_proposed_profile()),
                base_business_version=0,
            )
        assert await _draft_count(db) == 0
        # 取消后锁已释放、连接可用
        assert await db.pragma_value("user_version") == LATEST_VERSION
        await RunRepo(db).create_conversation("c3")


async def test_unbuilt_generation_baseline_stays_distinguishable_from_unknown_profile(
    tmp_path: Path,
) -> None:
    """未建档基线（NULL）与显式全未知档案基线必须可分：不得把未建档当成「无限制」。"""
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c1")
        drafts = DraftRepo(db)
        unbuilt = await drafts.create_pending(
            draft_id="d-unbuilt",
            conversation_id="c1",
            run_id=None,
            base_profile_json=None,
            proposed_profile_json=profile_to_json(_proposed_profile()),
            base_business_version=0,
        )
        empty_baseline = await drafts.create_pending(
            draft_id="d-empty-baseline",
            conversation_id="c1",
            run_id=None,
            base_profile_json=profile_to_json(Profile.empty()),
            proposed_profile_json=profile_to_json(_proposed_profile()),
            base_business_version=0,
        )
        assert unbuilt.base_profile_json is None
        assert empty_baseline.base_profile_json == profile_to_json(Profile.empty())
        assert (await drafts.get("d-unbuilt")) == unbuilt


async def test_draft_survives_close_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    async with open_database(path) as db:
        await RunRepo(db).create_conversation("c1")
        created = await DraftRepo(db).create_pending(
            draft_id="d1",
            conversation_id="c1",
            run_id=None,
            base_profile_json=profile_to_json(_seeded_profile()),
            proposed_profile_json=profile_to_json(_proposed_profile()),
            base_business_version=5,
        )
    async with open_database(path) as db:
        reloaded = await DraftRepo(db).get("d1")
        assert reloaded == created  # 内容、基线、来源、revision、状态、时间戳一致
        assert reloaded is not None
        assert reloaded.base_business_version == 5
        assert reloaded.base_profile_json == profile_to_json(_seeded_profile())


async def test_list_for_conversation_returns_only_that_sessions_drafts(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        runs = RunRepo(db)
        await runs.create_conversation("c1")
        await runs.create_conversation("c2")
        drafts = DraftRepo(db)
        for draft_id, conversation_id in (
            ("d1a", "c1"),
            ("d1b", "c1"),
            ("d2a", "c2"),
        ):
            await drafts.create_pending(
                draft_id=draft_id,
                conversation_id=conversation_id,
                run_id=None,
                base_profile_json=None,
                proposed_profile_json=profile_to_json(_proposed_profile()),
                base_business_version=0,
            )
        first_session = await drafts.list_for_conversation("c1")
        assert [draft.id for draft in first_session] == ["d1a", "d1b"]
        assert {draft.conversation_id for draft in first_session} == {"c1"}
        assert [draft.id for draft in await drafts.list_for_conversation("c2")] == [
            "d2a"
        ]
        assert await drafts.list_for_conversation("c-missing") == ()


async def test_commit_credential_persists_and_cannot_be_rewritten(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    async with open_database(path) as db:
        await RunRepo(db).create_conversation("c1")
        drafts = DraftRepo(db)
        created = await drafts.create_pending(
            draft_id="d1",
            conversation_id="c1",
            run_id=None,
            base_profile_json=profile_to_json(_seeded_profile()),
            proposed_profile_json=profile_to_json(_proposed_profile()),
            base_business_version=4,
        )
        async with db.transaction() as conn:
            committed = await drafts.record_commit_in_transaction(
                conn,
                draft_id="d1",
                committed_revision=created.revision,
                committed_business_version=5,
            )
        assert committed.status == "committed"
        assert committed.revision == created.revision
        assert (committed.committed_revision, committed.committed_business_version) == (
            created.revision,
            5,
        )
        # 提交后业务版本前进：草稿记录的原基线不被改写（后续版本变化不改写凭据）
        async with db.transaction() as conn:
            await conn.execute(
                "UPDATE user_profile SET context_version = 9 WHERE id = 1"
            )

    async with open_database(path) as db:
        drafts = DraftRepo(db)
        reloaded = await drafts.get("d1")
        assert reloaded == committed
        assert reloaded is not None
        assert reloaded.base_business_version == 4
        # 已提交草稿不重复写入凭据（条件更新不命中即拒绝）
        async with db.transaction() as conn:
            with pytest.raises(RuntimeError, match="Pending"):
                await drafts.record_commit_in_transaction(
                    conn,
                    draft_id="d1",
                    committed_revision=created.revision,
                    committed_business_version=6,
                )
        assert (await drafts.get("d1")) == committed
        assert (await _profile_row(db))[1] == 9  # 草稿提交凭据写入不推进业务版本


# ---------- 事务内读取：不嵌套取锁、读本事务快照 ----------


async def test_profile_read_in_transaction_shares_transaction_snapshot_without_nested_lock(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = ProfileRepo(db)
        service = ProfileService(db)
        seeded = _seeded_profile()
        proposed = _proposed_profile()
        async with db.transaction() as conn:
            await service.write_profile_in_transaction(conn, seeded)
            await conn.execute(
                "UPDATE user_profile SET context_version = 4 WHERE id = 1"
            )

        async def read_around_an_uncommitted_write():
            async with db.transaction() as conn:
                before = await repo.read_in_transaction(conn)
                await service.write_profile_in_transaction(conn, proposed)
                after = await repo.read_in_transaction(conn)
            return before, after

        # 事务内读取若自行取锁（锁不可重入）会死锁：用超时把死锁变成明确失败
        before, after = await asyncio.wait_for(
            read_around_an_uncommitted_write(), timeout=5
        )
        assert before == ProfileSnapshot(profile=seeded, context_version=4)
        # 读到同一事务未提交的写入 → 用的是事务连接，不是另一连接的快照
        assert after.profile == proposed
        assert after.context_version == 4
        assert (await repo.read()).profile == proposed  # 提交后普通读取一致


async def test_exercise_read_in_transaction_matches_standalone_read_and_sees_own_transaction(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = ExerciseRepo(db)
        standalone = await repo.get_by_id("barbell-back-squat")
        assert standalone is not None
        seen: list[Exercise | None] = []

        with pytest.raises(RuntimeError, match="注入回滚"):
            async with db.transaction() as conn:
                seen.append(
                    await repo.get_by_id_in_transaction(conn, "barbell-back-squat")
                )
                assert (
                    await repo.get_by_id_in_transaction(conn, "no-such-action") is None
                )
                # 事务内先读：本事务尚未插入，读到 None
                seen.append(await repo.get_by_id_in_transaction(conn, "ex-txn-only"))
                await _insert_exercise(conn, "ex-txn-only")
                # 同一事务未提交的插入立即可见 → 用的是本事务连接，不是另一连接
                seen.append(await repo.get_by_id_in_transaction(conn, "ex-txn-only"))
                raise RuntimeError("注入回滚")

        assert await repo.get_by_id("ex-txn-only") is None  # 上一步确为未提交读，已回滚
        assert seen[0] == standalone
        assert seen[1] is None
        assert seen[2] is not None and seen[2].id == "ex-txn-only"


async def test_draft_in_transaction_reads_share_the_transaction_connection_snapshot(
    tmp_path: Path,
) -> None:
    """草稿事务内读取用外层连接：提交凭据写入立即可见，不嵌套取锁、不读另一连接。

    ``transaction()`` 全程持有唯一锁且锁不可重入：事务内的草稿读取若自取锁会死锁，
    用超时把死锁变成明确失败（与档案／动作事务内读取同口径）。
    """
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c1")
        drafts = DraftRepo(db)
        created = await drafts.create_pending(
            draft_id="d1",
            conversation_id="c1",
            run_id=None,
            base_profile_json=profile_to_json(_seeded_profile()),
            proposed_profile_json=profile_to_json(_proposed_profile()),
            base_business_version=4,
        )

        async def commit_then_read_in_same_transaction():
            async with db.transaction() as conn:
                await drafts.record_commit_in_transaction(
                    conn,
                    draft_id="d1",
                    committed_revision=created.revision,
                    committed_business_version=5,
                )
                # 同一事务内读回：本事务尚未提交的 Committed 状态立即可见
                # → 用的是事务连接自己的快照，不是另一连接的已提交状态
                return await drafts.get_in_transaction(conn, "d1")

        inside = await asyncio.wait_for(
            commit_then_read_in_same_transaction(), timeout=5
        )
        assert inside is not None
        assert inside.status == "committed"
        assert (inside.committed_revision, inside.committed_business_version) == (
            created.revision,
            5,
        )
        # 事务提交后普通读取一致；重建 repo 视图按值比较（dataclass 相等）
        outside = await drafts.get("d1")
        assert outside == inside


async def test_in_transaction_access_requires_an_outer_transaction(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c1")
        drafts = DraftRepo(db)
        created = await drafts.create_pending(
            draft_id="d1",
            conversation_id="c1",
            run_id=None,
            base_profile_json=None,
            proposed_profile_json=profile_to_json(_proposed_profile()),
            base_business_version=0,
        )
        profile_repo = ProfileRepo(db)
        exercise_repo = ExerciseRepo(db)

        async def op(conn):
            with pytest.raises(RuntimeError, match="外层事务"):
                await profile_repo.read_in_transaction(conn)
            with pytest.raises(RuntimeError, match="外层事务"):
                await exercise_repo.get_by_id_in_transaction(conn, "barbell-back-squat")
            with pytest.raises(RuntimeError, match="外层事务"):
                await drafts.get_in_transaction(conn, created.id)
            with pytest.raises(RuntimeError, match="外层事务"):
                await drafts.record_commit_in_transaction(
                    conn,
                    draft_id=created.id,
                    committed_revision=created.revision,
                    committed_business_version=1,
                )

        await db.under_lock(op)
        unchanged = await drafts.get("d1")
        assert unchanged == created  # 非事务内调用不留任何写入
