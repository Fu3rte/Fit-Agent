"""编号迁移执行器：PRAGMA user_version；迁移先于启动恢复。"""

import re
import sqlite3
from pathlib import Path

import aiosqlite

from storage.errors import FutureSchemaVersion, MigrationError

DEFAULT_MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_NAME_PREFIX = re.compile(r"^(\d+)_")


def load_migrations(
    migrations_dir: str | Path | None = None,
) -> list[tuple[int, str, str]]:
    """加载并校验迁移脚本：返回按编号排序的 (version, 文件名, SQL) 列表。"""
    directory = (
        Path(migrations_dir) if migrations_dir is not None else DEFAULT_MIGRATIONS_DIR
    )
    files = sorted(directory.glob("*.sql"))
    if not files:
        raise MigrationError(f"迁移目录没有 .sql 文件: {directory}")
    migrations: list[tuple[int, str, str]] = []
    for expected_version, path in enumerate(files, start=1):
        match = _NAME_PREFIX.match(path.name)
        if match is None or int(match.group(1)) != expected_version:
            raise MigrationError(
                f"迁移文件编号必须从 1 连续（期望 {expected_version:03d}_*.sql）: {path.name}"
            )
        migrations.append(
            (expected_version, path.name, path.read_text(encoding="utf-8"))
        )
    return migrations


async def _user_version(conn: aiosqlite.Connection) -> int:
    async with conn.execute("PRAGMA user_version") as cursor:
        row = await cursor.fetchone()
    return 0 if row is None else int(row[0])


async def run_migrations(
    conn: aiosqlite.Connection,
    migrations_dir: str | Path | None = None,
) -> int:
    """把库从当前 user_version 迁移到最新版本，返回最新版本号。"""
    migrations = load_migrations(migrations_dir)
    current = await _user_version(conn)
    if current > len(migrations):
        raise FutureSchemaVersion(
            f"数据库 user_version={current} 高于程序支持的最新迁移 {len(migrations)}："
            "停止启动，不降级、不重建用户数据库（07 7.2）"
        )
    for version, name, sql in migrations[current:]:
        script = f"BEGIN;\n{sql}\nPRAGMA user_version={version};\nCOMMIT;"
        try:
            # executescript 在 autocommit 模式下原样执行脚本内嵌的 BEGIN/COMMIT。
            await conn.executescript(script)
        except sqlite3.Error as exc:
            # 脚本以 BEGIN 开头：执行失败时事务仍开启，必须回滚。
            if conn.in_transaction:
                await conn.rollback()
            raise MigrationError(f"迁移 {name} 执行失败，已回滚") from exc
    return len(migrations)
