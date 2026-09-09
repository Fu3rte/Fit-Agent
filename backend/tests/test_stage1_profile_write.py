"""Stage 1 S1-04：档案内部写入的事务边界与写入旁路检查（验收 5）。

验收对照（stage1.md §5 S1-04 验收 5）：档案内部写入复用外层事务，不自行提交或无条件递增
版本，为 Stage 2 组合事务保留原子边界；异常回滚后样本档案与版本均不变。并检查生产调用点
不存在档案写入旁路（本阶段不接 HTTP／Agent／CLI，不提供建档旁路）。
"""

import re
from pathlib import Path

import pytest

from domain.profile import rules
from domain.profile.repo import ProfileRepo
from domain.profile.schema import Fact, Profile
from domain.profile.service import ProfileService
from tests.support import open_database

BACKEND_ROOT = Path(__file__).resolve().parents[1]

# 生产代码范围（不扫 tests/、依赖环境）：与 S0 凭据泄漏扫描同口径；含顶层包 agent_core。
_PRODUCTION_PACKAGES = ("agent_core", "api", "app", "domain", "runtime", "storage")
_PRODUCTION_FILES = ("config.py", "main.py")
# 覆盖 INSERT / INSERT OR REPLACE / UPDATE / DELETE / REPLACE 各类写入口。
_PROFILE_WRITE_SQL = re.compile(
    r"(?is)\b(insert\s+(or\s+\w+\s+)?into|update|delete\s+from|replace\s+into)\s+user_profile\b"
)
_PROFILE_WRITE_ALLOWED = "domain/profile/repo.py"


def _production_sources() -> list[Path]:
    sources: list[Path] = []
    for package in _PRODUCTION_PACKAGES:
        sources += sorted((BACKEND_ROOT / package).rglob("*.py"))
    sources += [BACKEND_ROOT / name for name in _PRODUCTION_FILES]
    return sources


def _seeded_profile() -> Profile:
    return Profile(
        training_goal=Fact.known("增肌"),
        body_weight_kg=Fact.known(70.0),
    )


def _other_profile() -> Profile:
    return Profile(
        training_goal=Fact.known("力量"),
        body_weight_kg=Fact.known(68.5),
    )


async def _profile_row(db) -> tuple[str | None, int, int]:
    async def op(conn):
        async with conn.execute(
            "SELECT COUNT(*) AS rows, profile_json, context_version FROM user_profile"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return (row["profile_json"], int(row["context_version"]), int(row["rows"]))

    return await db.under_lock(op)


# ---------- 复用外层事务 ----------


async def test_write_is_rejected_outside_an_outer_transaction(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = ProfileRepo(db)

        async def op(conn):
            await repo.write_in_transaction(conn, _seeded_profile())

        with pytest.raises(RuntimeError, match="外层事务"):
            await db.under_lock(op)
        assert await _profile_row(db) == (None, 0, 1)


async def test_write_commits_with_outer_transaction_and_keeps_version(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    async with open_database(path) as db:
        service = ProfileService(db)
        async with db.transaction() as conn:
            await service.write_profile_in_transaction(conn, _seeded_profile())
        stored = await service.read_formal_profile()
        assert stored.profile == _seeded_profile()
        assert stored.context_version == 0  # 不无条件递增版本
    async with open_database(path) as db:
        stored = await ProfileService(db).read_formal_profile()
        assert stored.profile == _seeded_profile()
        assert stored.context_version == 0


async def test_write_never_changes_existing_context_version(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service = ProfileService(db)
        async with db.transaction() as conn:
            await service.write_profile_in_transaction(conn, _seeded_profile())
            await conn.execute(
                "UPDATE user_profile SET context_version = 5 WHERE id = 1"
            )
        async with db.transaction() as conn:
            await service.write_profile_in_transaction(conn, _other_profile())
        stored = await service.read_formal_profile()
        assert stored.profile == _other_profile()
        assert stored.context_version == 5  # 写入既不递增也不重置版本
        _, version, rows = await _profile_row(db)
        assert (version, rows) == (5, 1)  # 单例载体不新增行


async def test_rollback_leaves_profile_and_version_unchanged(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service = ProfileService(db)
        async with db.transaction() as conn:
            await service.write_profile_in_transaction(conn, _seeded_profile())
            await conn.execute(
                "UPDATE user_profile SET context_version = 5 WHERE id = 1"
            )
        before = await _profile_row(db)

        with pytest.raises(RuntimeError, match="模拟确认事务中途失败"):
            async with db.transaction() as conn:
                await service.write_profile_in_transaction(conn, _other_profile())
                await conn.execute(
                    "UPDATE user_profile SET context_version = context_version + 1"
                    " WHERE id = 1"
                )
                raise RuntimeError("模拟确认事务中途失败")

        assert await _profile_row(db) == before
        stored = await service.read_formal_profile()
        assert stored.profile == _seeded_profile()
        assert stored.context_version == 5


async def test_structural_validation_happens_before_write(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service = ProfileService(db)
        with pytest.raises(rules.InvalidProfile):
            async with db.transaction() as conn:
                await service.write_profile_in_transaction(
                    conn,
                    Profile(weekly_frequency=Fact.known("3")),  # type: ignore[arg-type]
                )
        assert await _profile_row(db) == (None, 0, 1)


# ---------- 生产调用点无写入旁路 ----------


def test_no_profile_write_bypass_in_production_code() -> None:
    sources = _production_sources()
    assert len(sources) >= 6, f"生产源码扫描范围异常缩小：{len(sources)} 个文件"
    offenders: list[str] = []
    for source in sources:
        text = source.read_text(encoding="utf-8")
        if not _PROFILE_WRITE_SQL.search(text):
            continue
        relative = source.relative_to(BACKEND_ROOT).as_posix()
        if relative != _PROFILE_WRITE_ALLOWED:
            offenders.append(relative)
    assert offenders == [], f"档案写入只允许经 {_PROFILE_WRITE_ALLOWED}：{offenders}"


def test_profile_module_never_writes_context_version() -> None:
    forbidden = (
        r"(?is)update\s+user_profile[^;]*context_version",
        r"(?is)insert\s+into\s+user_profile",
        r"(?is)set\s+context_version",
        r"context_version\s*\+",
    )
    for source in sorted((BACKEND_ROOT / "domain" / "profile").glob("*.py")):
        text = source.read_text(encoding="utf-8")
        for pattern in forbidden:
            assert not re.search(pattern, text), f"{source.name} 命中 {pattern}"


def test_profile_service_is_not_wired_into_api_app_or_runtime() -> None:
    for package in ("api", "app", "runtime"):
        for source in sorted((BACKEND_ROOT / package).rglob("*.py")):
            text = source.read_text(encoding="utf-8")
            assert "domain.profile" not in text, source.name
            assert "ProfileService" not in text, source.name
            assert "ProfileRepo" not in text, source.name
