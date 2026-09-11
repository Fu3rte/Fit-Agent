"""Stage 3 S3-02：计划侧业务表迁移（005）、草稿 kind 扩展（006）与可推荐系统迁移（007）。

验收对照（stage3.md §5 S3-02）：

- 连续编号迁移建 ``plan_versions``／``scheduled_sessions``／``arrangement_revisions``；
  索引与外键最小集；草稿表增加 kind／计划载荷／档案补丁／记录载荷列。
- 空库、Stage 2 库升级、重复启动、失败迁移回滚、高版本拒绝；旧档案草稿仍可读可确认。
- 系统迁移只把 Stage 1 已核对的 24 项置 ``recommendable=1``（不多置、不静默扩目录）；
  ``list_recommendable`` 恰为 24 项且 id 与 Stage 1 核对表一致。
- 不新增第二套版本计数器；事务内读取不嵌套取锁（锁不可重入）。

边界（stage3.md §5 S3-02）：不实现计划领域生成（S3-03）；``SEED_ROWS`` 直接引用 Stage 1
核对表，迁移 id 清单与它一致才算通过。记录侧四表（S3-09，009 迁移）在表集合断言中显式
扩展（不删测试、不放宽既有断言）；所有用例只操作 ``tmp_path`` 下的临时文件库与临时迁移
目录，不触碰真实用户数据目录。
"""

import json
import re
import shutil
import sqlite3
from pathlib import Path

import anyio
import pytest

from api.dto import draft_dto
from app.confirm import ConfirmService
from app.draft_repo import DRAFT_KINDS, DraftRepo
from app.drafts import DraftService
from domain.actions.repo import ExerciseRepo
from domain.profile.repo import ProfileRepo
from domain.profile.schema import Fact, Profile, ProfileSnapshot, profile_to_json
from storage.db import Database
from storage.errors import FutureSchemaVersion, MigrationError
from storage.migrations import DEFAULT_MIGRATIONS_DIR, load_migrations
from storage.run_repo import RunRepo
from tests.support import open_database
from tests.test_stage1_actions_seed import SEED_ROWS

STAGE0_TABLES = {
    "conversations",
    "runs",
    "messages",
    "run_events",
    "app_config",
    "provider_config",
}
STAGE1_TABLES = {"exercises", "user_profile"}
STAGE2_TABLES = {"business_drafts"}
# S3-02 由 005 迁移新增的计划侧三表；记录侧四表归 S3-09（009 迁移，见下）。
STAGE3_PLAN_TABLES = {"plan_versions", "scheduled_sessions", "arrangement_revisions"}
# S3-09 由 009 迁移新增的记录侧四表（本任务已建；表集合断言显式扩展、不删测试）。
STAGE3_RECORD_TABLES = {
    "training_sessions",
    "session_revisions",
    "exercise_logs",
    "training_sets",
}
# 仍未建的后续阶段表（统计／复盘侧归 S3-12／S3-13）。
LATER_STAGE_TABLES = {"reviews", "pr_candidates"}
DRAFT_PAYLOAD_COLUMNS = (
    "proposed_plan_json",
    "proposed_profile_patch_json",
    "proposed_record_json",
    "proposed_arrangement_json",
)
STAGE2_VERSION = 4  # 004_stage2_business_drafts.sql 执行后的 user_version
# 008_stage3_arrangement_draft_payload.sql 执行后的 user_version（009 记录侧表之前的 Stage 3 库）
STAGE3_PLAN_VERSION = 8
LATEST_VERSION = len(load_migrations())
DECIDED_DRAFT_KINDS = ("profile_update", "plan", "training_record", "arrangement")
CHECKED_RECOMMENDABLE_IDS = frozenset(str(row["id"]) for row in SEED_ROWS)
RECOMMENDABLE_MIGRATION_FILE = "007_stage3_recommendable_seed.sql"
PLAN_TABLES_MIGRATION_FILE = "005_stage3_plan_tables.sql"

# 逐表计数语句一律字面量：表名是测试常量，但不拼接 SQL（与 repo 层同口径）
_COUNT_SQL = {
    "business_drafts": "SELECT COUNT(*) FROM business_drafts",
    "plan_versions": "SELECT COUNT(*) FROM plan_versions",
    "scheduled_sessions": "SELECT COUNT(*) FROM scheduled_sessions",
    "arrangement_revisions": "SELECT COUNT(*) FROM arrangement_revisions",
    "conversations": "SELECT COUNT(*) FROM conversations",
    # S3-09（009 迁移）记录侧四表：升级后同样为零写入
    "training_sessions": "SELECT COUNT(*) FROM training_sessions",
    "session_revisions": "SELECT COUNT(*) FROM session_revisions",
    "exercise_logs": "SELECT COUNT(*) FROM exercise_logs",
    "training_sets": "SELECT COUNT(*) FROM training_sets",
}


def _first_time_profile() -> Profile:
    """首次确认可用样本：八项事实明确回答，无身体情况报告、无动作限制。"""
    return Profile(
        training_goal=Fact.known("增肌"),
        training_experience=Fact.known("新手"),
        weekly_frequency=Fact.known(3),
        session_duration_minutes=Fact.known(60),
        available_equipment=Fact.known(("杠铃",)),
        body_weight_kg=Fact.known(72.5),
        action_restrictions=Fact.known(()),
        body_conditions=Fact.known(()),
    )


async def _table_names(db: Database) -> set[str]:
    async def op(conn):
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ) as cursor:
            return {str(row["name"]) for row in await cursor.fetchall()}

    return await db.under_lock(op)


async def _column_names(db: Database, table: str) -> set[str]:
    """逐表列名（表名参数化，不拼接 SQL）。"""

    async def op(conn):
        async with conn.execute(
            "SELECT name FROM pragma_table_info(?)", (table,)
        ) as cursor:
            return {str(row["name"]) for row in await cursor.fetchall()}

    return await db.under_lock(op)


async def _row_count(db: Database, table: str) -> int:
    sql = _COUNT_SQL[table]

    async def op(conn):
        async with conn.execute(sql) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return int(row[0])

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


async def _recommendable_ids(db: Database) -> set[str]:
    return {exercise.id for exercise in await ExerciseRepo(db).list_recommendable()}


def _stage2_only_migrations(tmp_path: Path) -> Path:
    """只含 001–004 的临时迁移目录：构造「已有 Stage 2 库」的升级前状态。"""
    directory = tmp_path / "stage2-migrations"
    directory.mkdir()
    for name in (
        "001_stage0_runtime_and_settings.sql",
        "002_stage1_actions_profile.sql",
        "003_stage1_action_seed.sql",
        "004_stage2_business_drafts.sql",
    ):
        shutil.copy(DEFAULT_MIGRATIONS_DIR / name, directory)
    return directory


def _stage3_plan_only_migrations(tmp_path: Path) -> Path:
    """只含 001–008 的临时迁移目录：构造「已有 Stage 3 计划侧库」的升级前状态。"""
    directory = tmp_path / "stage3-plan-migrations"
    directory.mkdir()
    for name in (
        "001_stage0_runtime_and_settings.sql",
        "002_stage1_actions_profile.sql",
        "003_stage1_action_seed.sql",
        "004_stage2_business_drafts.sql",
        "005_stage3_plan_tables.sql",
        "006_stage3_draft_kinds.sql",
        "007_stage3_recommendable_seed.sql",
        "008_stage3_arrangement_draft_payload.sql",
    ):
        shutil.copy(DEFAULT_MIGRATIONS_DIR / name, directory)
    return directory


async def _insert_draft_row(conn, **overrides: object) -> None:
    """按 business_drafts 列插入一行；默认是合法 Pending 档案草稿。"""
    params: dict[str, object] = {
        "id": "d-raw",
        "kind": "profile_update",
        "conversation_id": "c-raw",
        "run_id": None,
        "base_profile_json": None,
        "proposed_profile_json": profile_to_json(_first_time_profile()),
        "base_business_version": 0,
        "revision": 1,
        "status": "pending",
        "committed_revision": None,
        "committed_business_version": None,
        "created_at": "2026-09-11T00:00:00+00:00",
        "updated_at": "2026-09-11T00:00:00+00:00",
        # S3-02 新增的三类载荷列：默认 NULL（非对应 kind 的合法表达）
        "proposed_plan_json": None,
        "proposed_profile_patch_json": None,
        "proposed_record_json": None,
        # S3-08（008 迁移）新增的安排草稿载荷列
        "proposed_arrangement_json": None,
    }
    params.update(overrides)
    payloads = DRAFT_PAYLOAD_COLUMNS
    await conn.execute(
        "INSERT INTO business_drafts (id, kind, conversation_id, run_id,"
        " base_profile_json, proposed_profile_json, base_business_version, revision,"
        " status, committed_revision, committed_business_version, created_at,"
        " updated_at, proposed_plan_json, proposed_profile_patch_json,"
        " proposed_record_json, proposed_arrangement_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            params["id"],
            params["kind"],
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
            *(params[column] for column in payloads),
        ),
    )


async def _insert_plan_version(conn, **overrides: object) -> None:
    """按 005 列插入一个合法计划版本；overrides 用于构造待拒绝的非法行。"""
    params: dict[str, object] = {
        "id": "pv-raw",
        "version": 9,
        "source_plan_version_id": None,
        "starts_on": "2026-09-14",
        "review_on": "2026-10-12",
        "mode": "regular",
        "payload_json": json.dumps({"schema_version": 1, "plan_workouts": []}),
        "source_draft_id": "d-1",
        "confirmed_at": "2026-09-11T00:00:00+00:00",
    }
    params.update(overrides)
    await conn.execute(
        "INSERT INTO plan_versions (id, version, source_plan_version_id, starts_on,"
        " review_on, mode, payload_json, source_draft_id, confirmed_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            params["id"],
            params["version"],
            params["source_plan_version_id"],
            params["starts_on"],
            params["review_on"],
            params["mode"],
            params["payload_json"],
            params["source_draft_id"],
            params["confirmed_at"],
        ),
    )


async def _insert_scheduled_session(conn, **overrides: object) -> None:
    """按 005 列插入一个合法应训练名额；overrides 用于构造待拒绝的非法行。"""
    params: dict[str, object] = {
        "id": "ss-raw",
        "plan_version_id": "pv-1",
        "plan_workout_key": "push-a",
        "scheduled_on": "2026-09-14",
        "cancelled_at": None,
        "locked_at": None,
    }
    params.update(overrides)
    await conn.execute(
        "INSERT INTO scheduled_sessions (id, plan_version_id, plan_workout_key,"
        " scheduled_on, cancelled_at, locked_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            params["id"],
            params["plan_version_id"],
            params["plan_workout_key"],
            params["scheduled_on"],
            params["cancelled_at"],
            params["locked_at"],
        ),
    )


async def _insert_arrangement_revision(conn, **overrides: object) -> None:
    """按 005 列插入一笔完整目标快照；overrides 用于构造待拒绝的非法行。"""
    params: dict[str, object] = {
        "id": "ar-raw",
        "scheduled_session_id": "ss-1",
        "revision_no": 9,
        "target_snapshot_json": json.dumps({"items": []}),
        "source_draft_id": "d-1",
        "accepted_at": "2026-09-11T00:00:00+00:00",
    }
    params.update(overrides)
    await conn.execute(
        "INSERT INTO arrangement_revisions (id, scheduled_session_id, revision_no,"
        " target_snapshot_json, source_draft_id, accepted_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            params["id"],
            params["scheduled_session_id"],
            params["revision_no"],
            params["target_snapshot_json"],
            params["source_draft_id"],
            params["accepted_at"],
        ),
    )


# ---------- 空库：迁移 005–007 的结构、边界与可推荐集合 ----------


async def test_empty_database_creates_stage3_plan_tables_without_statistics_side_tables(
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
            | {"sqlite_sequence"}
        )
        assert tables & LATER_STAGE_TABLES == set()  # 统计／复盘侧表归 S3-12／S3-13

        # 草稿表增量列就位（kind + 三类载荷；既有列一个不少）
        draft_columns = await _column_names(db, "business_drafts")
        assert {"kind", *DRAFT_PAYLOAD_COLUMNS} <= draft_columns
        assert {"base_profile_json", "proposed_profile_json", "status"} <= draft_columns

        # 迁移只建结构：计划侧三表为空、草稿为空、档案仍是「未建档」载体
        for table in sorted(STAGE3_PLAN_TABLES):
            assert await _row_count(db, table) == 0, table
        assert await _row_count(db, "business_drafts") == 0
        assert await _profile_row(db) == (None, 0)


async def test_system_migration_marks_exactly_the_checked_stage1_list_recommendable(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        # list_recommendable 恰为 Stage 1 已核对清单：不多置、不静默扩目录
        recommendable = await _recommendable_ids(db)
        assert recommendable == CHECKED_RECOMMENDABLE_IDS
        assert len(recommendable) == 24
        all_exercises = await ExerciseRepo(db).list_all()
        assert {exercise.id for exercise in all_exercises} == CHECKED_RECOMMENDABLE_IDS
        assert all(exercise.recommendable for exercise in all_exercises)

        # 停用行不进入推荐候选，但已检查标记不被系统迁移或停用覆盖（03 3.2）
        async with db.transaction() as conn:
            await conn.execute(
                "UPDATE exercises SET active = 0 WHERE id = 'hanging-leg-raise'"
            )
        stopped = await ExerciseRepo(db).get_by_id("hanging-leg-raise")
        assert stopped is not None and stopped.active is False
        assert stopped.recommendable is True
        assert len(await _recommendable_ids(db)) == 23


def test_recommendable_migration_updates_only_the_checked_ids() -> None:
    """007 是系统数据迁移：只 UPDATE exercises.recommendable，id 清单与 Stage 1 核对表一致。"""
    sql = (DEFAULT_MIGRATIONS_DIR / RECOMMENDABLE_MIGRATION_FILE).read_text(
        encoding="utf-8"
    )
    body = "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    ).strip()
    assert body.startswith("UPDATE exercises SET recommendable = 1")
    for keyword in ("INSERT", "CREATE", "DROP", "ALTER", "DELETE", "user_profile"):
        assert keyword not in body
    # 迁移内逐项列出的 id 恰为核对表（不靠 WHERE 之外的隐含范围）
    ids = set(re.findall(r"'([a-z0-9-]+)'", body))
    assert ids == CHECKED_RECOMMENDABLE_IDS
    assert len(ids) == 24


async def test_stage3_tables_do_not_add_a_second_business_version_counter(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        for table in sorted(STAGE3_PLAN_TABLES):
            columns = await _column_names(db, table)
            assert "context_version" not in columns, table
            assert "business_version" not in columns, table
        # 统一业务版本仍只在档案行上（01 1.4）；plan_versions.version 是计划版本序号
        assert "context_version" in await _column_names(db, "user_profile")
        assert "version" in await _column_names(db, "plan_versions")


async def test_plan_side_constraints_are_minimal_but_enforced(tmp_path: Path) -> None:
    """新表的最小约束：唯一键、取值、JSON 与必要外键都在库层拒绝非法行。"""
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c-raw")
        async with db.transaction() as conn:
            await _insert_draft_row(conn, id="d-1")
            await _insert_draft_row(conn, id="d-2")
            await _insert_plan_version(
                conn, id="pv-1", version=1, source_draft_id="d-1"
            )
            await _insert_scheduled_session(conn, id="ss-1", plan_version_id="pv-1")
            await _insert_arrangement_revision(
                conn, id="ar-1", scheduled_session_id="ss-1"
            )

        rejections = (
            # plan_versions：版本号唯一且从 1 起；mode 与 payload 受约束
            (_insert_plan_version, {"id": "pv-dup", "version": 1}),
            (_insert_plan_version, {"id": "pv-mode", "version": 2, "mode": "peak"}),
            (
                _insert_plan_version,
                {"id": "pv-json", "version": 2, "payload_json": "not json"},
            ),
            (
                _insert_plan_version,
                {"id": "pv-source", "version": 2, "source_draft_id": "d-missing"},
            ),
            # scheduled_sessions：同版本同一天至多一个名额；必须挂存在版本
            (
                _insert_scheduled_session,
                {"id": "ss-dup", "scheduled_on": "2026-09-14"},
            ),
            (
                _insert_scheduled_session,
                {"id": "ss-plan", "plan_version_id": "pv-missing"},
            ),
            # arrangement_revisions：每次接受一个 revision_no；指向存在的日程与快照 JSON
            (_insert_arrangement_revision, {"id": "ar-dup", "revision_no": 9}),
            (_insert_arrangement_revision, {"id": "ar-zero", "revision_no": 0}),
            (
                _insert_arrangement_revision,
                {"id": "ar-json", "target_snapshot_json": "not json"},
            ),
            (
                _insert_arrangement_revision,
                {"id": "ar-ss", "scheduled_session_id": "ss-missing"},
            ),
        )
        for insert, overrides in rejections:
            async with db.transaction() as conn:
                with pytest.raises(sqlite3.IntegrityError):
                    await insert(conn, **overrides)
        # 合法行仍只有最初三行，非法行未留下半条
        assert await _row_count(db, "plan_versions") == 1
        assert await _row_count(db, "scheduled_sessions") == 1
        assert await _row_count(db, "arrangement_revisions") == 1


# ---------- Stage 2 库升级：旧档案草稿可读可确认、目录与档案不变 ----------


async def test_stage2_upgrade_keeps_legacy_profile_draft_readable_and_confirmable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    stage2_dir = _stage2_only_migrations(tmp_path)

    async with open_database(path, migrations_dir=stage2_dir) as db:
        assert await db.pragma_value("user_version") == STAGE2_VERSION
        await RunRepo(db).create_conversation("c-legacy")
        # 004 时代的插入语句（列清单里没有 kind）：模拟 Stage 2 已落库的旧档案草稿
        stamp = "2026-09-11T00:00:00+00:00"
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO business_drafts (id, conversation_id, run_id,"
                " base_profile_json, proposed_profile_json, base_business_version,"
                " revision, status, committed_revision, committed_business_version,"
                " created_at, updated_at)"
                " VALUES ('d-legacy', 'c-legacy', NULL, NULL, ?, 0, 1, 'pending',"
                " NULL, NULL, ?, ?)",
                (profile_to_json(_first_time_profile()), stamp, stamp),
            )
            await conn.execute(
                "UPDATE exercises SET active = 0 WHERE id = 'hanging-leg-raise'"
            )

    # 用生产迁移目录重开同一库：005–007 增量执行，旧数据与草稿原样保留
    async with open_database(path) as db:
        assert await db.migrate() == LATEST_VERSION

        # 旧档案草稿：kind 由 006 回填为 profile_update，内容／基线／revision 不变
        legacy = await DraftRepo(db).get("d-legacy")
        assert legacy is not None
        assert legacy.kind == "profile_update"
        assert legacy.status == "pending" and legacy.revision == 1
        assert legacy.base_profile_json is None and legacy.base_business_version == 0
        view = await DraftService(db).get_draft("d-legacy")
        assert view is not None
        assert draft_dto(view)["kind"] == "profile_update"

        # 事务内读取不嵌套取锁：确认事务在升级后的库上照常完成（版本恰好 +1）
        result = await ConfirmService(db).confirm_profile_draft(
            draft_id="d-legacy", seen_revision=1
        )
        assert result.committed_revision == 1
        assert result.committed_business_version == 1
        assert await ProfileRepo(db).read() == ProfileSnapshot(
            profile=_first_time_profile(), context_version=1
        )
        committed = await DraftRepo(db).get("d-legacy")
        assert committed is not None and committed.status == "committed"

        # 目录：停用状态保留，系统迁移仍把已核对 24 项标记为已检查
        stopped = await ExerciseRepo(db).get_by_id("hanging-leg-raise")
        assert stopped is not None and stopped.active is False
        assert stopped.recommendable is True
        assert len(await _recommendable_ids(db)) == 23

        # 计划侧三表随迁移就位且没有任何正式事实
        for table in sorted(STAGE3_PLAN_TABLES):
            assert await _row_count(db, table) == 0, table


async def test_stage3_upgrade_adds_record_tables_and_keeps_plan_facts(
    tmp_path: Path,
) -> None:
    """Stage 3 库（005–008）升级到 009：记录侧四表就位，已落库的计划事实原样保留。"""
    path = tmp_path / "app.db"
    stage3_dir = _stage3_plan_only_migrations(tmp_path)

    async with open_database(path, migrations_dir=stage3_dir) as db:
        assert await db.pragma_value("user_version") == STAGE3_PLAN_VERSION
        await RunRepo(db).create_conversation("c-plan")
        async with db.transaction() as conn:
            await _insert_draft_row(conn, id="d-1", conversation_id="c-plan")
            await _insert_plan_version(
                conn, id="pv-1", version=1, source_draft_id="d-1"
            )
            await _insert_scheduled_session(conn, id="ss-1", plan_version_id="pv-1")
            await _insert_arrangement_revision(
                conn, id="ar-1", scheduled_session_id="ss-1"
            )

    # 用生产迁移目录重开同一库：009 增量执行，计划事实与安排快照不变
    async with open_database(path) as db:
        assert await db.migrate() == LATEST_VERSION
        assert await _table_names(db) >= STAGE3_RECORD_TABLES
        assert await _row_count(db, "plan_versions") == 1
        assert await _row_count(db, "scheduled_sessions") == 1
        assert await _row_count(db, "arrangement_revisions") == 1
        for table in sorted(STAGE3_RECORD_TABLES):
            assert await _row_count(db, table) == 0, table


async def test_reopening_does_not_rerun_stage3_migrations(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    async with open_database(path) as db:
        await RunRepo(db).create_conversation("c1")
        created = await DraftRepo(db).create_pending(
            draft_id="d1",
            conversation_id="c1",
            run_id=None,
            base_profile_json=None,
            proposed_profile_json=profile_to_json(_first_time_profile()),
            base_business_version=0,
        )
        assert created.kind == "profile_update"
        assert await db.pragma_value("user_version") == LATEST_VERSION

    async with open_database(path) as db:
        assert await db.migrate() == LATEST_VERSION  # 无新迁移可执行
        assert await db.pragma_value("user_version") == LATEST_VERSION
        drafts = await DraftRepo(db).list_for_conversation("c1")
        assert drafts == (created,)


async def test_failed_stage3_migration_rolls_back_and_can_be_retried(
    tmp_path: Path,
) -> None:
    """005 失败：版本不虚报、不留半套结构与半套草稿扩展，修复后可从 004 继续。"""
    # 先造一个 Stage 2 库（001–004）并写入旧数据
    directory = _stage2_only_migrations(tmp_path)
    path = tmp_path / "app.db"
    async with open_database(path, migrations_dir=directory) as db:
        assert await db.pragma_value("user_version") == STAGE2_VERSION
        await RunRepo(db).create_conversation("c1")

    # 临时目录补上 005–009（005 故意损坏）：与生产目录同编号，不动生产目录
    for name in (
        "005_stage3_plan_tables.sql",
        "006_stage3_draft_kinds.sql",
        "007_stage3_recommendable_seed.sql",
        "008_stage3_arrangement_draft_payload.sql",
        "009_stage3_record_tables.sql",
    ):
        shutil.copy(DEFAULT_MIGRATIONS_DIR / name, directory / name)
    broken = directory / PLAN_TABLES_MIGRATION_FILE
    original = (DEFAULT_MIGRATIONS_DIR / PLAN_TABLES_MIGRATION_FILE).read_text(
        encoding="utf-8"
    )
    broken.write_text(
        "CREATE TABLE plan_versions (id TEXT PRIMARY KEY);\nTHIS IS NOT VALID SQL;",
        encoding="utf-8",
    )

    # 重跑时 005 失败：版本停在 004，005 的半套结构回滚，006–009 不执行
    async with open_database(path, migrate=False, migrations_dir=directory) as db:
        with pytest.raises(MigrationError, match="005"):
            await db.migrate()
        assert await db.pragma_value("user_version") == STAGE2_VERSION
        tables = await _table_names(db)
        assert "plan_versions" not in tables
        assert "kind" not in await _column_names(db, "business_drafts")
        assert await _row_count(db, "conversations") == 1  # 旧数据保留

        # 修复迁移后重跑：不需要删库，005–009 依序补齐
        broken.write_text(original, encoding="utf-8")
        assert await db.migrate() == LATEST_VERSION
        assert await db.pragma_value("user_version") == LATEST_VERSION
        assert await _table_names(db) >= (STAGE3_PLAN_TABLES | STAGE3_RECORD_TABLES)
        assert "kind" in await _column_names(db, "business_drafts")
        assert await _recommendable_ids(db) == CHECKED_RECOMMENDABLE_IDS


async def test_higher_schema_version_is_refused_without_touching_stage3_schema(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    async with open_database(path) as db:
        async with db.transaction() as conn:
            await conn.execute("PRAGMA user_version = 99")
        with pytest.raises(FutureSchemaVersion, match="99"):
            await db.migrate()
        assert await db.pragma_value("user_version") == 99
        # 结构未被降级或重建
        assert await _table_names(db) >= (STAGE3_PLAN_TABLES | STAGE3_RECORD_TABLES)
    assert path.exists()  # 未删除用户数据库


# ---------- 草稿 kind 与载荷列：取值与 JSON 约束 ----------


async def test_draft_kind_accepts_decided_values_and_rejects_unknown(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c-raw")
        assert set(DRAFT_KINDS) == set(DECIDED_DRAFT_KINDS)
        async with db.transaction() as conn:
            for kind in DRAFT_KINDS:
                await _insert_draft_row(conn, id=f"d-{kind}", kind=kind)
        for forbidden in ("stale", "review", "profile"):
            async with db.transaction() as conn:
                with pytest.raises(sqlite3.IntegrityError):
                    await _insert_draft_row(conn, id=f"d-{forbidden}", kind=forbidden)
        ddl = (DEFAULT_MIGRATIONS_DIR / "006_stage3_draft_kinds.sql").read_text(
            encoding="utf-8"
        )
        for kind in DRAFT_KINDS:
            assert f"'{kind}'" in ddl


async def test_draft_default_kind_backfills_legacy_profile_drafts(
    tmp_path: Path,
) -> None:
    """老写法（不写 kind 列）落库为 profile_update：006 的 DEFAULT 是读旧写新兼容点。"""
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c-raw")
        stamp = "2026-09-11T00:00:00+00:00"
        async with db.transaction() as conn:
            # 004 时代的插入语句：列清单里根本没有 kind
            await conn.execute(
                "INSERT INTO business_drafts (id, conversation_id, run_id,"
                " base_profile_json, proposed_profile_json, base_business_version,"
                " revision, status, committed_revision, committed_business_version,"
                " created_at, updated_at)"
                " VALUES ('d-no-kind', 'c-raw', NULL, NULL, ?, 0, 1, 'pending',"
                " NULL, NULL, ?, ?)",
                (profile_to_json(_first_time_profile()), stamp, stamp),
            )
        draft = await DraftRepo(db).get("d-no-kind")
        assert draft is not None and draft.kind == "profile_update"
        # 迁移 006 的 ADD COLUMN 也把 004 时代旧行回填成同一值
        assert "DEFAULT 'profile_update'" in (
            DEFAULT_MIGRATIONS_DIR / "006_stage3_draft_kinds.sql"
        ).read_text(encoding="utf-8")


async def test_draft_payload_columns_require_valid_json_when_present(
    tmp_path: Path,
) -> None:
    # 不用 pytest.mark.parametrize：anyio 插件为 async 测试重建 callspec 时会丢弃
    # 既有参数化（request.param 缺失，与 tests/test_provider_settings.py 同一说明）
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c-raw")
        # NULL = 尚未写入或非该 kind：合法
        async with db.transaction() as conn:
            await _insert_draft_row(conn, id="d-null-payload")
        for column in DRAFT_PAYLOAD_COLUMNS:
            async with db.transaction() as conn:
                await _insert_draft_row(
                    conn,
                    id=f"d-{column}",
                    kind="plan",
                    **{column: json.dumps({"ok": 1})},
                )
            for index, value in enumerate(("not json", "{", "[]}")):
                async with db.transaction() as conn:
                    with pytest.raises(sqlite3.IntegrityError):
                        await _insert_draft_row(
                            conn,
                            id=f"d-bad-{column}-{index}",
                            kind="plan",
                            **{column: value},
                        )
        assert await _row_count(db, "business_drafts") == 1 + len(DRAFT_PAYLOAD_COLUMNS)


# ---------- 事务内读取不嵌套取锁（升级后照常） ----------


async def test_transaction_internal_reads_of_stage3_schema_do_not_nest_lock(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation("c1")
        await DraftRepo(db).create_pending(
            draft_id="d1",
            conversation_id="c1",
            run_id=None,
            base_profile_json=None,
            proposed_profile_json=profile_to_json(_first_time_profile()),
            base_business_version=0,
        )

        async def read_inside_transaction(conn) -> tuple[int, int, int]:
            draft = await DraftRepo(db).get_in_transaction(conn, "d1")
            assert draft is not None
            counts = []
            for table in sorted(STAGE3_PLAN_TABLES):
                async with conn.execute(_COUNT_SQL[table]) as cursor:
                    row = await cursor.fetchone()
                assert row is not None
                counts.append(int(row[0]))
            return (draft.revision, *counts)  # type: ignore[return-value]

        with anyio.fail_after(5):  # 死锁（嵌套取锁）会在超时后显式失败
            async with db.transaction() as conn:
                assert await read_inside_transaction(conn) == (1, 0, 0, 0)
            # 事务外的自取锁读取在事务结束后仍正常
            assert await DraftRepo(db).get("d1") is not None
