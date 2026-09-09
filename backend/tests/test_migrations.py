"""S0-04：编号迁移、user_version 与最小存储结构约束。

生产迁移经 storage/migrations/001_*.sql（Stage 0）与 002_*.sql（Stage 1 两项业务
存储）；升级/失败/重跑语义用临时注入的迁移目录验证（不向生产迁移目录塞测试用假迁移）。
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
# Stage 1 只新增这两项业务存储（stage1.md §3）；drafts/plans/records/stats 仍不得建。
STAGE1_TABLES = {"exercises", "user_profile"}
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
        assert tables >= EXPECTED_RUNTIME_TABLES | STAGE1_TABLES
        # 不创建 11 张业务表（07 章责任边界）：库中表恰为运行时四表 +
        # app_config/provider_config + Stage 1 的动作目录与档案两项业务存储
        # （sqlite_sequence 来自 AUTOINCREMENT）
        assert tables == EXPECTED_RUNTIME_TABLES | STAGE1_TABLES | {"sqlite_sequence"}


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
        result = await repo.create_run_with_user_message(
            "c1", "r-new", "cri-new", "b", retry_of_run_id="r-old"
        )
        assert result["run"]["retry_of_run_id"] == "r-old"
        # 已拍语义可保存：重启中断标记（Stage 4 负责流转时机，本层只验证存储能力）
        async with db.transaction() as conn:
            await conn.execute(
                "UPDATE runs SET status = 'failed', error_code = 'interrupted_by_restart'"
                " WHERE id = 'r-old'"
            )
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
