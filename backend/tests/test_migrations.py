"""S0-04：编号迁移、user_version 与最小存储结构约束。

生产迁移经 storage/migrations/001_*.sql（Stage 0）、002_*.sql（Stage 1 两项业务
存储）、004_*.sql（Stage 2 草稿表）、005–007_*.sql（Stage 3 计划侧表、草稿 kind
扩展、可推荐系统迁移）、008–010_*.sql（Stage 3 安排草稿载荷、记录侧四表、统计侧
``pr_candidates`` 视图）与 011_*.sql（Stage 3 复盘两表）；
013_*.sql（Stage 4 动作肌群）与 014_*.sql（Stage 4 摘要两表）；
升级/失败/重跑语义用临时注入的迁移目录验证（不向生产迁移目录塞测试用假迁移）。
"""

import sqlite3
from pathlib import Path

import pytest

from storage.db import Database
from storage.errors import FutureSchemaVersion, MigrationError
from storage.migrations import load_migrations
from storage.run_repo import RunRepo
from tests.support import open_database

EXPECTED_RUNTIME_TABLES = {
    "conversations",
    "runs",
    "messages",
    "run_events",
    "app_config",
    "provider_config",
}
# Stage 1 只新增这两项业务存储（stage1.md §3）。
STAGE1_TABLES = {"exercises", "user_profile"}
# Stage 2 S2-02 只新增草稿表这一项（stage2.md §5 S2-02 边界）。
STAGE2_TABLES = {"business_drafts"}
# Stage 3 S3-02 只新增计划侧三表（stage3.md §5 S3-02）；记录侧四表由 S3-09 的 009 迁移新增。
STAGE3_PLAN_TABLES = {"plan_versions", "scheduled_sessions", "arrangement_revisions"}
STAGE3_RECORD_TABLES = {
    "training_sessions",
    "session_revisions",
    "exercise_logs",
    "training_sets",
}
# S3-13 由 011 迁移新增的复盘两表：正文／快照本体与精确来源修订引用。
STAGE3_REVIEW_TABLES = {"reviews", "review_source_revisions"}
# S4-06a 由 014 迁移新增的摘要两表：覆盖范围与来源关联（07 7.4 摘要持久化）。
STAGE4_SUMMARY_TABLES = {"summaries", "summary_sources"}
# Stage 3 表已全部落地：统计侧 ``pr_candidates`` 是视图（010），不在 type='table' 扫描内。
LATER_STAGE_TABLES: set[str] = set()
LATEST_VERSION = len(load_migrations())


async def _table_names(db: Database) -> set[str]:
    async def op(conn):
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ) as cursor:
            return {str(row["name"]) for row in await cursor.fetchall()}

    return await db.under_lock(op)


async def test_fresh_initialize_creates_runtime_tables(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        assert await db.migrate() == LATEST_VERSION
        assert await db.pragma_value("user_version") == LATEST_VERSION
        tables = await _table_names(db)
        assert tables == (
            EXPECTED_RUNTIME_TABLES
            | STAGE1_TABLES
            | STAGE2_TABLES
            | STAGE3_PLAN_TABLES
            | STAGE3_RECORD_TABLES
            | STAGE3_REVIEW_TABLES
            | STAGE4_SUMMARY_TABLES
            | {"sqlite_sequence"}
        )
        assert tables & LATER_STAGE_TABLES == set()  # 不建统计／复盘侧表


async def test_repeated_start_does_not_rerun_migrations(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    async with open_database(path) as db:
        await RunRepo(db).create_conversation("sentinel")
    async with open_database(path) as db:
        assert await db.migrate() == LATEST_VERSION  # 无新迁移可执行
        # 若迁移被重复执行，CREATE TABLE 会与已存在表冲突——此处不抛错即证明跳过
        sentinel = await RunRepo(db).get_conversation("sentinel")
        assert sentinel is not None


def _write_migration(directory: Path, version: int, name: str, sql: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{version:03d}_{name}.sql").write_text(sql, encoding="utf-8")


async def test_upgrade_preserves_existing_data(tmp_path: Path) -> None:
    """旧版本库带哨兵数据升级：数据保留、新结构就位、版本推进。"""
    migrations_dir = tmp_path / "migrations"
    _write_migration(
        migrations_dir, 1, "base", "CREATE TABLE base_marker (id TEXT PRIMARY KEY);"
    )
    path = tmp_path / "app.db"
    async with open_database(path, migrate=False, migrations_dir=migrations_dir) as db:
        await db.migrate()
        async with db.transaction() as conn:
            await conn.execute("INSERT INTO base_marker (id) VALUES ('sentinel')")

    _write_migration(
        migrations_dir, 2, "extend", "CREATE TABLE extra_marker (id TEXT PRIMARY KEY);"
    )
    async with open_database(path, migrate=False, migrations_dir=migrations_dir) as db:
        assert await db.migrate() == 2
        async with (
            db.transaction() as conn,
            conn.execute("SELECT id FROM base_marker") as cursor,
        ):
            rows = await cursor.fetchall()
        assert [str(row["id"]) for row in rows] == ["sentinel"]
        async with db.transaction() as conn:
            await conn.execute("INSERT INTO extra_marker (id) VALUES ('x')")


async def test_failed_migration_leaves_no_partial_structure(tmp_path: Path) -> None:
    """失败的单次迁移：版本号不虚报、不留半套结构；修复后可继续，不删用户库。"""
    migrations_dir = tmp_path / "migrations"
    _write_migration(
        migrations_dir, 1, "base", "CREATE TABLE keep_me (id TEXT PRIMARY KEY);"
    )
    _write_migration(
        migrations_dir,
        2,
        "broken",
        "CREATE TABLE half_done (id TEXT PRIMARY KEY);\nTHIS IS NOT VALID SQL;",
    )
    path = tmp_path / "app.db"
    async with open_database(path, migrate=False, migrations_dir=migrations_dir) as db:
        with pytest.raises(MigrationError, match="broken"):
            await db.migrate()
        assert await db.pragma_value("user_version") == 1
        tables = await _table_names(db)
        assert "keep_me" in tables
        assert "half_done" not in tables  # 半套结构已回滚

        # 修复迁移文件后重跑：不需要删除数据库
        _write_migration(
            migrations_dir, 2, "broken", "CREATE TABLE half_done (id TEXT PRIMARY KEY);"
        )
        assert await db.migrate() == 2
        assert "half_done" in await _table_names(db)


async def test_future_schema_version_refuses_to_start(tmp_path: Path) -> None:
    """高于程序支持的库版本：停止启动，不降级、不重建库。"""
    async with open_database(tmp_path / "app.db") as db:
        async with db.transaction() as conn:
            await conn.execute("PRAGMA user_version = 99")
        with pytest.raises(FutureSchemaVersion, match="99"):
            await db.migrate()
        assert await db.pragma_value("user_version") == 99  # 库未被改动
    assert (tmp_path / "app.db").exists()  # 未删除用户数据库


async def test_runs_status_allows_only_five_states(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await repo.create_conversation("c1")
        async with db.transaction() as conn:
            for status in ("pending", "running", "completed", "failed", "cancelled"):
                await conn.execute(
                    "INSERT INTO runs (id, conversation_id, client_request_id, status,"
                    " error_code, retry_of_run_id, created_at, updated_at)"
                    " VALUES (?, 'c1', ?, ?, NULL, NULL, 't', 't')",
                    (f"r-{status}", f"cri-{status}", status),
                )
        async with db.transaction() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                await conn.execute(
                    "INSERT INTO runs (id, conversation_id, client_request_id, status,"
                    " error_code, retry_of_run_id, created_at, updated_at)"
                    " VALUES ('r-bad', 'c1', 'cri-bad', 'queued', NULL, NULL, 't', 't')"
                )


async def test_client_request_id_is_globally_unique(tmp_path: Path) -> None:
    """client_request_id 是全局唯一约束（07 7.4）：跨会话同键也被拒绝；
    仓库层同键重复调用返回已有 Run（幂等语义归 S0-06 验证）。"""
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await repo.create_conversation("c1")
        await repo.create_conversation("c2")
        first = await repo.create_run_with_user_message("c1", "r1", "cri-1", "a")
        assert first["created"] is True
        # 结构层验证：不同会话的相同幂等键同样违反全局 UNIQUE
        async with db.transaction() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                await conn.execute(
                    "INSERT INTO runs (id, conversation_id, client_request_id, status,"
                    " created_at, updated_at) VALUES ('r2', 'c2', 'cri-1', 'pending', 't', 't')"
                )
        # 仓库层同键重复调用：不产生新记录，返回已有 Run
        repeat = await repo.create_run_with_user_message("c2", "r2", "cri-1", "b")
        assert repeat["created"] is False
        assert repeat["run"]["id"] == "r1"
        assert repeat["run"]["conversation_id"] == "c1"
        assert (await repo.get_run("r2")) is None


async def test_error_code_and_retry_pointer_semantics(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await repo.create_conversation("c1")
        await repo.create_run_with_user_message("c1", "r-old", "cri-old", "a")
        # 已拍语义可保存：重启中断标记（Stage 4 负责流转时机，本层只验证存储能力）
        async with db.transaction() as conn:
            await conn.execute(
                "UPDATE runs SET status = 'failed', error_code = 'interrupted_by_restart'"
                " WHERE id = 'r-old'"
            )
        # 08 8.2：同一时刻只允许一个活跃 Run，因此重试请求必然发生在旧 Run 结束之后
        result = await repo.create_run_with_user_message(
            "c1", "r-new", "cri-new", "b", retry_of_run_id="r-old"
        )
        assert result["run"]["retry_of_run_id"] == "r-old"
        saved = await repo.get_run("r-old")
        assert saved is not None
        assert saved["status"] == "failed"
        assert saved["error_code"] == "interrupted_by_restart"


async def test_run_events_id_is_autoincrement_row_identity(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await repo.create_conversation("c1")
        await repo.create_run_with_user_message("c1", "r1", "cri-1", "a")
        ids = await repo.append_run_events(
            "r1", [("progress", {"step": 1}), ("progress", {"step": 2})]
        )
        assert ids == sorted(ids) and len(set(ids)) == 2
        events = await repo.list_run_events("r1")
        assert [event["event_type"] for event in events] == ["progress", "progress"]


async def test_run_events_structure_matches_decided_schema(tmp_path: Path) -> None:
    """结构验收：run_events 以 event_type + payload_json 存轨迹，
    id 为 INTEGER PRIMARY KEY AUTOINCREMENT（仅行身份，非恢复游标）。"""
    async with open_database(tmp_path / "app.db") as db:

        async def op(conn):
            async with conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'run_events'"
            ) as cursor:
                return str((await cursor.fetchone())["sql"])

        ddl = await db.under_lock(op)
        assert "id INTEGER PRIMARY KEY AUTOINCREMENT" in ddl
        assert "event_type" in ddl and "payload_json" in ddl


async def test_migration_failure_blocks_service_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """迁移失败：版本号不虚报成功且不开放后续服务（S0-04 验收）。

    生命周期在连接打开后执行迁移，注入迁移失败应异常上抛、关闭已建立连接，
    服务不进入可用状态；不删除用户数据库。"""
    import storage.db as storage_db
    from api.app import create_app

    async def failing_migrations(conn, migrations_dir=None) -> int:
        raise MigrationError("注入的迁移失败")

    monkeypatch.setattr(storage_db, "run_migrations", failing_migrations)
    data_dir = tmp_path / "data"
    app = create_app(data_dir)
    with pytest.raises(MigrationError, match="注入的迁移失败"):
        async with app.router.lifespan_context(app):
            pass  # 生命周期启动即失败：不得进入可用状态
    assert not app.state.db.is_open  # 已建立连接被关闭
    assert (data_dir / "app.db").exists()  # 未以删除用户数据库替代恢复


async def test_failed_013_rolls_back_muscle_column_and_version(tmp_path: Path) -> None:
    """013 失败注入：ALTER 已执行但后续语句失败 → 整片回滚（列不存在、版本停在 012）。

    用真实 001–012 加一个注入失败的 013 验证；修复后可重跑，不需删库（07 7.2）。
    """
    real_dir = Path(__file__).resolve().parents[1] / "storage" / "migrations"
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    for source in sorted(real_dir.glob("0*.sql")):
        if source.name.startswith(("013", "014")):
            continue
        (migrations_dir / source.name).write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8"
        )
    _write_migration(
        migrations_dir,
        13,
        "stage4_action_muscle",
        "ALTER TABLE exercises ADD COLUMN muscle TEXT;\nTHIS IS NOT VALID SQL;",
    )
    path = tmp_path / "app.db"
    async with open_database(path, migrate=False, migrations_dir=migrations_dir) as db:
        with pytest.raises(MigrationError, match="stage4_action_muscle"):
            await db.migrate()
        assert await db.pragma_value("user_version") == 12
        async with (
            db.transaction() as conn,
            conn.execute("PRAGMA table_info(exercises)") as cursor,
        ):
            columns = {str(row["name"]) for row in await cursor.fetchall()}
        assert "muscle" not in columns  # 半套结构已回滚

        _write_migration(
            migrations_dir,
            13,
            "stage4_action_muscle",
            "ALTER TABLE exercises ADD COLUMN muscle TEXT;",
        )
        assert await db.migrate() == 13
        async with (
            db.transaction() as conn,
            conn.execute("PRAGMA table_info(exercises)") as cursor,
        ):
            columns = {str(row["name"]) for row in await cursor.fetchall()}
        assert "muscle" in columns


async def _index_names(db: Database) -> set[str]:
    async def op(conn):
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ) as cursor:
            return {str(row["name"]) for row in await cursor.fetchall()}

    return await db.under_lock(op)


async def test_failed_014_rolls_back_summary_tables_and_version(tmp_path: Path) -> None:
    """014 失败注入：真实脚本的表与索引先执行、末尾语句失败 → 整片回滚（无半套结构、版本停在 013）。

    注入脚本 = 真实 014 全文 + 非法 SQL（表、索引、第二张表都已执行后失败）；
    修复后把真实 014 原文放入临时目录重跑，验证升级路径不需删库（07 7.2）。
    """
    real_dir = Path(__file__).resolve().parents[1] / "storage" / "migrations"
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    for source in sorted(real_dir.glob("0*.sql")):
        if source.name.startswith("014"):
            continue
        (migrations_dir / source.name).write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8"
        )
    real_sql = (real_dir / "014_stage4_summaries.sql").read_text(encoding="utf-8")
    _write_migration(
        migrations_dir, 14, "stage4_summaries", f"{real_sql}\nTHIS IS NOT VALID SQL;"
    )
    path = tmp_path / "app.db"
    async with open_database(path, migrate=False, migrations_dir=migrations_dir) as db:
        with pytest.raises(MigrationError, match="stage4_summaries"):
            await db.migrate()
        assert await db.pragma_value("user_version") == 13
        tables = await _table_names(db)
        assert "summaries" not in tables  # 半套结构已回滚
        assert "summary_sources" not in tables
        assert "idx_summaries_conversation_coverage" not in await _index_names(db)

        # 修复：把真实 014 原文放入临时目录重跑（不删用户库）
        (migrations_dir / "014_stage4_summaries.sql").write_text(
            real_sql, encoding="utf-8"
        )
        assert await db.migrate() == 14
        tables = await _table_names(db)
        assert {"summaries", "summary_sources"} <= tables
        assert "idx_summaries_conversation_coverage" in await _index_names(db)
