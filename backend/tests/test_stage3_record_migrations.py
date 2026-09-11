"""Stage 3 S3-09：记录侧业务表迁移（009）—— training_sessions／session_revisions／exercise_logs／training_sets。

验收对照（stage3.md §5 S3-09）：

- 迁移建四表与必要索引；``record_type`` 对齐目录三类（``reps_weight``／``reps_bodyweight``／
  ``time``，2026-09-11 拍板 A）；辅助／热身／RIR 可空语义。
- 升级兼容同 S3-02：空库、Stage 2 库升级、Stage 3 计划侧库升级、重复启动、失败迁移回滚、
  高版本拒绝。
- 表集合旁路扫描显式扩展（本文件 + test_migrations／test_stage1_schema／test_stage2_draft_storage／
  test_stage3_plan_migrations／test_provider_settings 逐处显式加入四表，不放宽既有断言）。
- 不同负重口径不混比的存储字段齐备：原始值+单位+换算键（领域策略见 test_stage3_record_domain.py）。

边界：只建记录侧四表与领域 schema／换算键，不建统计视图（``pr_candidates`` 归 S3-12）、复盘表
（``reviews`` 归 S3-13），不写记录草稿载荷结构（S3-10）。所有用例只操作 ``tmp_path`` 下的
临时文件库与临时迁移目录，不触碰真实用户数据目录。
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
STAGE2_TABLES = {"business_drafts"}
STAGE3_PLAN_TABLES = {"plan_versions", "scheduled_sessions", "arrangement_revisions"}
# S3-09 由 009 迁移新增的记录侧四表。
STAGE3_RECORD_TABLES = {
    "training_sessions",
    "session_revisions",
    "exercise_logs",
    "training_sets",
}
# S3-13 由 011 迁移新增的复盘两表。
STAGE3_REVIEW_TABLES = {"reviews", "review_source_revisions"}

STAGE2_VERSION = 4  # 004_stage2_business_drafts.sql 执行后的 user_version
STAGE3_PLAN_VERSION = (
    8  # 008_stage3_arrangement_draft_payload.sql 执行后的 user_version
)
RECORD_MIGRATION_FILE = "009_stage3_record_tables.sql"
# 紧接 009 的迁移（S3-12 统计侧视图、S3-13 复盘两表、S3-14 视图修正）：临时目录与生产目录同编号
PR_CANDIDATES_MIGRATION_FILE = "010_stage3_pr_candidates_view.sql"
REVIEWS_MIGRATION_FILE = "011_stage3_reviews.sql"
PR_CANDIDATES_FIX_MIGRATION_FILE = "012_stage3_pr_candidates_assisted_reps.sql"
LATEST_VERSION = len(load_migrations())
SEEDED_EXERCISE_ID = "barbell-back-squat"  # 目录种子内动作（003）

# 逐表计数一律字面量语句（表名不参与 SQL 拼接，与 repo 层同口径）
_COUNT_SQL = {
    "training_sessions": "SELECT COUNT(*) FROM training_sessions",
    "session_revisions": "SELECT COUNT(*) FROM session_revisions",
    "exercise_logs": "SELECT COUNT(*) FROM exercise_logs",
    "training_sets": "SELECT COUNT(*) FROM training_sets",
}

_STAMP = "2026-09-11T00:00:00+00:00"


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


async def _index_names(db: Database, table: str) -> set[str]:
    async def op(conn):
        async with conn.execute(
            "SELECT name FROM pragma_index_list(?)", (table,)
        ) as cursor:
            return {str(row["name"]) for row in await cursor.fetchall()}

    return await db.under_lock(op)


async def _record_row_counts(db: Database) -> dict[str, int]:
    """记录侧四表的行数（四表各自一条字面量语句，不拼接 SQL）。"""

    async def op(conn):
        counts: dict[str, int] = {}
        for table, statement in _COUNT_SQL.items():
            async with conn.execute(statement) as cursor:
                row = await cursor.fetchone()
            assert row is not None
            counts[table] = int(row[0])
        return counts

    return await db.under_lock(op)


async def _insert_session(conn, **overrides: object) -> None:
    """按 009 列插入一个合法训练身份；overrides 用于构造待拒绝的非法行。"""
    params: dict[str, object] = {
        "id": "ts-raw",
        "current_revision_id": None,
        "created_at": _STAMP,
    }
    params.update(overrides)
    await conn.execute(
        "INSERT INTO training_sessions (id, current_revision_id, created_at)"
        " VALUES (?, ?, ?)",
        (params["id"], params["current_revision_id"], params["created_at"]),
    )


async def _insert_revision(conn, **overrides: object) -> None:
    """按 009 列插入一笔完整修订；overrides 用于构造待拒绝的非法行。"""
    params: dict[str, object] = {
        "id": "sr-raw",
        "session_id": "ts-1",
        "revision_no": 1,
        "previous_revision_id": None,
        "status": "valid",
        "occurred_on": "2026-09-07",
        "started_at": None,
        "time_precision": None,
        "arrangement_revision_id": None,
        "completion_declared": 0,
        "is_return_phase": 0,
        "feedback_json": None,
        "source_draft_id": "d-1",
        "confirmed_at": _STAMP,
    }
    params.update(overrides)
    await conn.execute(
        "INSERT INTO session_revisions (id, session_id, revision_no,"
        " previous_revision_id, status, occurred_on, started_at, time_precision,"
        " arrangement_revision_id, completion_declared, is_return_phase,"
        " feedback_json, source_draft_id, confirmed_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            params["id"],
            params["session_id"],
            params["revision_no"],
            params["previous_revision_id"],
            params["status"],
            params["occurred_on"],
            params["started_at"],
            params["time_precision"],
            params["arrangement_revision_id"],
            params["completion_declared"],
            params["is_return_phase"],
            params["feedback_json"],
            params["source_draft_id"],
            params["confirmed_at"],
        ),
    )


async def _insert_log(conn, **overrides: object) -> None:
    """按 009 列插入一个动作事实；overrides 用于构造待拒绝的非法行。"""
    params: dict[str, object] = {
        "id": "el-raw",
        "session_revision_id": "sr-1",
        "exercise_id": SEEDED_EXERCISE_ID,
        "position": 1,
        "target_item_key": None,
        "record_type": "reps_weight",
        "load_notation": "barbell_includes_bar_total",
        "exercise_snapshot_json": None,
        "warmup_summary_text": None,
    }
    params.update(overrides)
    await conn.execute(
        "INSERT INTO exercise_logs (id, session_revision_id, exercise_id, position,"
        " target_item_key, record_type, load_notation, exercise_snapshot_json,"
        " warmup_summary_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            params["id"],
            params["session_revision_id"],
            params["exercise_id"],
            params["position"],
            params["target_item_key"],
            params["record_type"],
            params["load_notation"],
            params["exercise_snapshot_json"],
            params["warmup_summary_text"],
        ),
    )


async def _insert_set(conn, **overrides: object) -> None:
    """按 009 列插入一组事实；overrides 用于构造待拒绝的非法行。"""
    params: dict[str, object] = {
        "id": "tset-raw",
        "exercise_log_id": "el-1",
        "set_no": 1,
        "set_type": "work",
        "target_set_key": None,
        "load_value_text": "60",
        "load_unit": "kg",
        "load_kg_key": 60000,
        "reps": 8,
        "duration_seconds": None,
        "rir": 2,
        "assistance": "none",
        "assisted_reps": None,
        "quality_text": None,
    }
    params.update(overrides)
    await conn.execute(
        "INSERT INTO training_sets (id, exercise_log_id, set_no, set_type,"
        " target_set_key, load_value_text, load_unit, load_kg_key, reps,"
        " duration_seconds, rir, assistance, assisted_reps, quality_text)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            params["id"],
            params["exercise_log_id"],
            params["set_no"],
            params["set_type"],
            params["target_set_key"],
            params["load_value_text"],
            params["load_unit"],
            params["load_kg_key"],
            params["reps"],
            params["duration_seconds"],
            params["rir"],
            params["assistance"],
            params["assisted_reps"],
            params["quality_text"],
        ),
    )


async def _insert_minimal_chain(conn) -> None:
    """造一条合法事实链：会话 → 记录草稿 → 训练身份 → 修订 → 动作事实 → 一组。"""
    await conn.execute(
        "INSERT INTO conversations (id, created_at) VALUES ('c-raw', ?)",
        (_STAMP,),
    )
    await conn.execute(
        "INSERT INTO business_drafts (id, kind, conversation_id, run_id,"
        " base_profile_json, proposed_profile_json, base_business_version, revision,"
        " status, committed_revision, committed_business_version, created_at,"
        " updated_at, proposed_plan_json, proposed_profile_patch_json,"
        " proposed_record_json, proposed_arrangement_json)"
        " VALUES ('d-1', 'training_record', 'c-raw', NULL, NULL, '{}', 0, 1,"
        " 'pending', NULL, NULL, ?, ?, NULL, NULL, NULL, NULL)",
        (_STAMP, _STAMP),
    )
    await _insert_session(conn, id="ts-1")
    await _insert_revision(conn, id="sr-1", session_id="ts-1")
    await _insert_log(conn, id="el-1", session_revision_id="sr-1")
    await _insert_set(conn, id="tset-1", exercise_log_id="el-1")


def _migrations_through(tmp_path: Path, last: int) -> Path:
    """只含 001–last 的临时迁移目录：构造升级前状态。"""
    directory = tmp_path / f"through-{last}-migrations"
    directory.mkdir()
    for name in sorted(path.name for path in DEFAULT_MIGRATIONS_DIR.glob("*.sql")):
        if int(name.split("_", 1)[0]) <= last:
            shutil.copy(DEFAULT_MIGRATIONS_DIR / name, directory)
    return directory


# ---------- 空库：009 建记录侧四表与最小索引，且不越界建统计／复盘侧 ----------


async def test_empty_database_creates_record_tables_with_minimum_indexes(
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
            | {"sqlite_sequence"}
        )

        # 迁移只建结构：四表为空
        assert set(await _record_row_counts(db)) == STAGE3_RECORD_TABLES
        assert set((await _record_row_counts(db)).values()) == {0}

        # 最小索引：按动作取历史＋按安排快照取执行记录；其余读取由唯一索引最左前缀覆盖
        assert await _index_names(db, "exercise_logs") >= {"idx_exercise_logs_exercise"}
        assert await _index_names(db, "session_revisions") >= {
            "idx_session_revisions_arrangement"
        }
        assert await _index_names(db, "training_sets") == {
            "sqlite_autoindex_training_sets_1",
            "sqlite_autoindex_training_sets_2",
        }


async def test_record_tables_add_no_second_version_counter_and_keep_catalog_vocabulary(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        for table in sorted(STAGE3_RECORD_TABLES):
            columns = await _column_names(db, table)
            assert "context_version" not in columns, table
            assert "business_version" not in columns, table
        # 统一业务版本仍只在档案行上（01 1.4）
        assert "context_version" in await _column_names(db, "user_profile")

        # record_type 对齐目录三类；口径列不冒充第四类（拍板 A）
        ddl = (DEFAULT_MIGRATIONS_DIR / RECORD_MIGRATION_FILE).read_text(
            encoding="utf-8"
        )
        for record_type in ("reps_weight", "reps_bodyweight", "time"):
            assert f"'{record_type}'" in ddl
        assert "external_load_reps" not in ddl


# ---------- 约束：库层拒绝非法行，合法行仍可写 ----------


async def test_record_tables_constraints_reject_invalid_rows(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        async with db.transaction() as conn:
            await _insert_minimal_chain(conn)
            # 当前修订指针指向本身份的修订；同一训练身份多笔修订
            await conn.execute(
                "UPDATE training_sessions SET current_revision_id = 'sr-1'"
                " WHERE id = 'ts-1'"
            )
            await _insert_revision(
                conn,
                id="sr-2",
                session_id="ts-1",
                revision_no=2,
                previous_revision_id="sr-1",
                status="incomplete",
                # 可空语义：未明确的事实保持为空（不是工作组、不是 RIR 0、不是无辅助）
            )
            await _insert_log(
                conn,
                id="el-2",
                session_revision_id="sr-2",
                position=2,
                record_type="reps_bodyweight",
                load_notation=None,
                warmup_summary_text="递增至 60kg",
            )
            await _insert_set(
                conn,
                id="tset-2",
                exercise_log_id="el-2",
                set_no=1,
                set_type=None,
                load_value_text=None,
                load_unit=None,
                load_kg_key=None,
                reps=5,
                rir=None,
                assistance=None,
            )

        rejections = (
            # training_sessions：指针必须指向属于本身份的修订（复合外键）
            (
                _insert_session,
                {"id": "ts-cross", "current_revision_id": "sr-missing"},
            ),
            # session_revisions：状态恰三态、revision_no 从 1 起、同身份修订号唯一
            (_insert_revision, {"id": "sr-bad-status", "status": "formal"}),
            (_insert_revision, {"id": "sr-zero", "revision_no": 0}),
            (_insert_revision, {"id": "sr-dup", "revision_no": 1}),
            (_insert_revision, {"id": "sr-session", "session_id": "ts-missing"}),
            (_insert_revision, {"id": "sr-draft", "source_draft_id": "d-missing"}),
            (
                _insert_revision,
                {"id": "sr-json", "feedback_json": "not json"},
            ),
            # exercise_logs：口径恰三类、负重口径只属外加负重次数型、同修订位置唯一
            (
                _insert_log,
                {"id": "el-fourth", "record_type": "external_load_reps"},
            ),
            (
                _insert_log,
                {"id": "el-notation", "record_type": "reps_bodyweight"},
            ),
            (
                _insert_log,
                {"id": "el-no-notation", "load_notation": None},
            ),
            (
                _insert_log,
                {"id": "el-unknown-notation", "load_notation": "invented"},
            ),
            (_insert_log, {"id": "el-pos", "position": 1}),
            (
                _insert_log,
                {"id": "el-rev", "session_revision_id": "sr-missing"},
            ),
            (
                _insert_log,
                {"id": "el-exercise", "exercise_id": "not-in-catalog"},
            ),
            (_insert_log, {"id": "el-snapshot", "exercise_snapshot_json": "{"}),
            # training_sets：环次从 1 起、同动作事实组号唯一
            (_insert_set, {"id": "tset-dup", "set_no": 1}),
            (_insert_set, {"id": "tset-zero", "set_no": 0}),
            (_insert_set, {"id": "tset-log", "exercise_log_id": "el-missing"}),
            # 负重三态必须齐备（原始值＋单位＋换算键），不得只写一半
            (_insert_set, {"id": "tset-half", "load_unit": None}),
            (_insert_set, {"id": "tset-key", "load_kg_key": None}),
            (_insert_set, {"id": "tset-unit", "load_unit": "stone"}),
            # RIR 非负；组类型与辅助取值在已拍词表内
            (_insert_set, {"id": "tset-rir", "rir": -1}),
            (_insert_set, {"id": "tset-type", "set_type": "working"}),
            (_insert_set, {"id": "tset-help", "assistance": "spotter"}),
            (_insert_set, {"id": "tset-reps", "reps": 0}),
        )
        for insert, overrides in rejections:
            async with db.transaction() as conn:
                with pytest.raises(sqlite3.IntegrityError):
                    await insert(conn, **overrides)

        # 非法行未留下半条：仍只有最初造出的合法链
        assert await _record_row_counts(db) == {
            "training_sessions": 1,
            "session_revisions": 2,
            "exercise_logs": 2,
            "training_sets": 2,
        }

        # 可空语义读回仍为空（未被库层默认值补造）
        async def op(conn):
            async with conn.execute(
                "SELECT set_type, rir, assistance FROM training_sets WHERE id = 'tset-2'"
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None
            return (row["set_type"], row["rir"], row["assistance"])

        assert await db.under_lock(op) == (None, None, None)


async def test_current_revision_pointer_must_belong_to_the_same_session(
    tmp_path: Path,
) -> None:
    """复合外键把「当前修订属于自身」落到库层：跨训练身份的指针写不进（报告 §3.2）。"""
    async with open_database(tmp_path / "app.db") as db:
        async with db.transaction() as conn:
            await _insert_minimal_chain(conn)
            await conn.execute(
                "UPDATE training_sessions SET current_revision_id = 'sr-1'"
                " WHERE id = 'ts-1'"
            )
            await _insert_session(conn, id="ts-2")
            await _insert_revision(conn, id="sr-2", session_id="ts-2", revision_no=1)
        # ts-2 指向 ts-1 的修订：拒绝；指向自己的修订：接受
        async with db.transaction() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                await conn.execute(
                    "UPDATE training_sessions SET current_revision_id = 'sr-1'"
                    " WHERE id = 'ts-2'"
                )
        async with db.transaction() as conn:
            await conn.execute(
                "UPDATE training_sessions SET current_revision_id = 'sr-2'"
                " WHERE id = 'ts-2'"
            )

        async def op(conn):
            async with conn.execute(
                "SELECT id, current_revision_id FROM training_sessions ORDER BY id"
            ) as cursor:
                return [
                    (str(row["id"]), str(row["current_revision_id"]))
                    for row in await cursor.fetchall()
                ]

        assert await db.under_lock(op) == [("ts-1", "sr-1"), ("ts-2", "sr-2")]


async def test_arrangement_link_is_optional_and_points_at_existing_snapshot(
    tmp_path: Path,
) -> None:
    """记录关联可信安排快照：可空；非空时必须在正式表内存在（05 5.1）。"""
    async with open_database(tmp_path / "app.db") as db:
        async with db.transaction() as conn:
            await _insert_minimal_chain(conn)
        async with db.transaction() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                await _insert_revision(
                    conn,
                    id="sr-arrangement",
                    arrangement_revision_id="ar-missing",
                )


# ---------- 升级：Stage 2 库与 Stage 3 计划侧库均可增量升到 009 ----------


async def test_stage2_upgrade_adds_record_tables_and_keeps_legacy_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    stage2_dir = _migrations_through(tmp_path, STAGE2_VERSION)

    async with open_database(path, migrations_dir=stage2_dir) as db:
        assert await db.pragma_value("user_version") == STAGE2_VERSION
        await RunRepo(db).create_conversation("c-legacy")
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO business_drafts (id, conversation_id, run_id,"
                " base_profile_json, proposed_profile_json, base_business_version,"
                " revision, status, committed_revision, committed_business_version,"
                " created_at, updated_at)"
                " VALUES ('d-legacy', 'c-legacy', NULL, NULL, '{}', 0, 1, 'pending',"
                " NULL, NULL, ?, ?)",
                (_STAMP, _STAMP),
            )

    async with open_database(path) as db:
        assert await db.migrate() == LATEST_VERSION
        assert await db.pragma_value("user_version") == LATEST_VERSION
        assert await _table_names(db) >= STAGE3_RECORD_TABLES

        # 旧草稿与旧会话原样保留；记录侧四表随迁移就位且为空
        async def op(conn):
            async with conn.execute(
                "SELECT COUNT(*) AS drafts, (SELECT COUNT(*) FROM conversations)"
                " AS conversations FROM business_drafts"
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None
            return (int(row["drafts"]), int(row["conversations"]))

        assert await db.under_lock(op) == (1, 1)
        assert set((await _record_row_counts(db)).values()) == {0}


async def test_stage3_plan_upgrade_keeps_plan_facts_and_adds_record_tables(
    tmp_path: Path,
) -> None:
    """Stage 3 计划侧库（user_version=8）升到 009：计划事实不变、记录表就位。"""
    path = tmp_path / "app.db"
    stage3_dir = _migrations_through(tmp_path, STAGE3_PLAN_VERSION)

    async with open_database(path, migrations_dir=stage3_dir) as db:
        assert await db.pragma_value("user_version") == STAGE3_PLAN_VERSION
        await RunRepo(db).create_conversation("c-plan")
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO business_drafts (id, kind, conversation_id, run_id,"
                " base_profile_json, proposed_profile_json, base_business_version,"
                " revision, status, committed_revision, committed_business_version,"
                " created_at, updated_at, proposed_plan_json,"
                " proposed_profile_patch_json, proposed_record_json,"
                " proposed_arrangement_json)"
                " VALUES ('d-plan', 'plan', 'c-plan', NULL, NULL, '{}', 0, 1,"
                " 'pending', NULL, NULL, ?, ?, NULL, NULL, NULL, NULL)",
                (_STAMP, _STAMP),
            )
            await conn.execute(
                "INSERT INTO plan_versions (id, version, source_plan_version_id,"
                " starts_on, review_on, mode, payload_json, source_draft_id,"
                " confirmed_at) VALUES ('pv-1', 1, NULL, '2026-09-14', '2026-10-12',"
                " 'regular', ?, 'd-plan', ?)",
                (json.dumps({"schema_version": 1, "plan_workouts": []}), _STAMP),
            )

    async with open_database(path) as db:
        assert await db.migrate() == LATEST_VERSION
        assert await _table_names(db) >= STAGE3_RECORD_TABLES
        # 记录侧四表由 009 建立：009 本身不建复盘表（归 011，另有 S3-13 用例）
        assert "CREATE TABLE reviews" not in (
            DEFAULT_MIGRATIONS_DIR / RECORD_MIGRATION_FILE
        ).read_text(encoding="utf-8")

        async def op(conn):
            async with conn.execute(
                "SELECT id, version, mode FROM plan_versions"
            ) as cursor:
                return [
                    (str(row["id"]), int(row["version"]), str(row["mode"]))
                    for row in await cursor.fetchall()
                ]

        assert await db.under_lock(op) == [("pv-1", 1, "regular")]
        assert set((await _record_row_counts(db)).values()) == {0}


async def test_reopening_does_not_rerun_record_migration(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    async with open_database(path) as db:
        assert await db.migrate() == LATEST_VERSION
        async with db.transaction() as conn:
            await _insert_minimal_chain(conn)

    async with open_database(path) as db:
        assert await db.migrate() == LATEST_VERSION  # 无新迁移可执行
        assert await db.pragma_value("user_version") == LATEST_VERSION
        # 若迁移被重复执行，CREATE TABLE 会冲突——不抛错且事实仍在即证明跳过
        assert (await _record_row_counts(db))["training_sets"] == 1


async def test_failed_record_migration_rolls_back_and_can_be_retried(
    tmp_path: Path,
) -> None:
    """009 失败：版本不虚报、不留半套结构，修复后可从 008 继续（不删用户库）。"""
    directory = _migrations_through(tmp_path, STAGE3_PLAN_VERSION)
    path = tmp_path / "app.db"
    async with open_database(path, migrations_dir=directory) as db:
        assert await db.pragma_value("user_version") == STAGE3_PLAN_VERSION
        await RunRepo(db).create_conversation("c1")

    shutil.copy(
        DEFAULT_MIGRATIONS_DIR / RECORD_MIGRATION_FILE,
        directory / RECORD_MIGRATION_FILE,
    )
    shutil.copy(
        DEFAULT_MIGRATIONS_DIR / PR_CANDIDATES_MIGRATION_FILE,
        directory / PR_CANDIDATES_MIGRATION_FILE,
    )
    shutil.copy(
        DEFAULT_MIGRATIONS_DIR / REVIEWS_MIGRATION_FILE,
        directory / REVIEWS_MIGRATION_FILE,
    )
    shutil.copy(
        DEFAULT_MIGRATIONS_DIR / PR_CANDIDATES_FIX_MIGRATION_FILE,
        directory / PR_CANDIDATES_FIX_MIGRATION_FILE,
    )
    broken = directory / RECORD_MIGRATION_FILE
    original = (DEFAULT_MIGRATIONS_DIR / RECORD_MIGRATION_FILE).read_text(
        encoding="utf-8"
    )
    broken.write_text(
        "CREATE TABLE training_sessions (id TEXT PRIMARY KEY);\nTHIS IS NOT VALID SQL;",
        encoding="utf-8",
    )

    async with open_database(path, migrate=False, migrations_dir=directory) as db:
        with pytest.raises(MigrationError, match="009"):
            await db.migrate()
        assert await db.pragma_value("user_version") == STAGE3_PLAN_VERSION
        tables = await _table_names(db)
        assert tables & STAGE3_RECORD_TABLES == set()  # 半套结构已回滚
        assert await RunRepo(db).get_conversation("c1") is not None  # 旧数据保留

        # 修复迁移后重跑：不需要删库，009 补齐（并继续到最新迁移）
        broken.write_text(original, encoding="utf-8")
        assert await db.migrate() == LATEST_VERSION
        assert await db.pragma_value("user_version") == LATEST_VERSION
        assert await _table_names(db) >= STAGE3_RECORD_TABLES
        assert await RunRepo(db).get_conversation("c1") is not None


async def test_higher_schema_version_refuses_without_touching_record_schema(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    async with open_database(path) as db:
        async with db.transaction() as conn:
            await conn.execute("PRAGMA user_version = 99")
        with pytest.raises(FutureSchemaVersion, match="99"):
            await db.migrate()
        assert await db.pragma_value("user_version") == 99
        assert await _table_names(db) >= STAGE3_RECORD_TABLES  # 结构未被降级或重建
    assert path.exists()  # 未删除用户数据库


async def test_record_migration_is_contiguous_after_arrangement_payload(
    tmp_path: Path,
) -> None:
    """编号连续（S3-02 同口径）：009 紧接 008，且不新增业务版本计数器。"""
    names = sorted(path.name for path in DEFAULT_MIGRATIONS_DIR.glob("*.sql"))
    position = names.index(RECORD_MIGRATION_FILE)
    assert position >= 1
    assert (
        int(names[position].split("_", 1)[0])
        == int(names[position - 1].split("_", 1)[0]) + 1
    )
    sql = (DEFAULT_MIGRATIONS_DIR / RECORD_MIGRATION_FILE).read_text(encoding="utf-8")
    body = "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    )
    assert "user_profile" not in body
    assert "context_version" not in body
