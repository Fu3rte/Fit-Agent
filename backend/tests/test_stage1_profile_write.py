"""Stage 1 S1-04：档案内部写入的事务边界与写入旁路检查（验收 5）。

验收对照（stage1.md §5 S1-04 验收 5）：档案内部写入复用外层事务，不自行提交或无条件递增
版本，为 Stage 2 组合事务保留原子边界；异常回滚后样本档案与版本均不变。并检查生产调用点
不存在档案写入旁路（本阶段不接 HTTP／Agent／CLI，不提供建档旁路）。
"""

import ast
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

# 唯一获批的 ``context_version`` 推进语句（Stage 2 S2-05）与其方法名、唯一调用方。
_APPROVED_VERSION_ADVANCE = re.compile(
    r"context_version\s*=\s*context_version\s*\+\s*1"
)
_APPROVED_VERSION_ADVANCE_METHOD = "bump_context_version_in_transaction"
# 其余任何形式的档案行写入／版本改写仍一律禁止。
_VERSION_WRITE_SQL = (
    r"(?is)update\s+user_profile[^;]*context_version",
    r"(?is)insert\s+into\s+user_profile",
    r"(?is)set\s+context_version",
)


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


def _string_literals(source: Path) -> list[str]:
    """源文件里的字符串字面量：SQL 都是字面量，逐条扫描不让相邻语句互相串味。"""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_profile_module_never_writes_context_version() -> None:
    """Stage 2 显式扩展（不删测试、不放宽）：domain/profile 内不得有第二条版本写入路径。

    原断言为「domain/profile 一律不得写 ``context_version``」。stage2.md §5 S2-05「复用
    Stage 1 内部档案写入，集中推进版本」要求确认事务推进版本，而本文件上方的写入旁路守卫
    （``_PROFILE_WRITE_ALLOWED``）与 README 硬规则 3（SQL 只在 repo）已决定 ``user_profile``
    的 SQL 只在 ``domain/profile/repo.py``。因此本守卫按仍然成立、且覆盖面更强的口径收窄
    为——版本自增语句全包恰好一处（见 :func:`test_profile_version_advance_stays_single_seam`），
    其余任何档案行写入／版本改写语句一律不许出现；档案写入本身仍不得顺带推进版本。
    """
    advances: list[str] = []
    for source in sorted((BACKEND_ROOT / "domain" / "profile").glob("*.py")):
        for literal in _string_literals(source):
            if _APPROVED_VERSION_ADVANCE.search(literal):
                advances.append(source.name)
                continue
            for pattern in _VERSION_WRITE_SQL:
                assert not re.search(pattern, literal), (
                    f"{source.name} 命中 {pattern}：{literal!r}"
                )
    assert advances == ["repo.py"], f"context_version 推进只允许一处：{advances}"


def test_profile_version_advance_stays_single_seam() -> None:
    """唯一版本推进点：批准方法体内一处语句，且 ``app/`` 内只有确认事务调用它。

    「一次成功确认仅 +1」与「只有确认事务推进版本」（01 1.4、stage2.md §5 S2-05）由本守卫
    逐条锁死：语句位置、无第二个方法／第二个计数器、唯一调用方。
    """
    repo_source = (BACKEND_ROOT / "domain" / "profile" / "repo.py").read_text(
        encoding="utf-8"
    )
    bodies = [
        ast.get_source_segment(repo_source, node) or ""
        for node in ast.walk(ast.parse(repo_source))
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == _APPROVED_VERSION_ADVANCE_METHOD
    ]
    assert len(bodies) == 1, (
        f"批准版本推进方法缺失或重名：{_APPROVED_VERSION_ADVANCE_METHOD}"
    )
    assert _APPROVED_VERSION_ADVANCE.search(bodies[0]), "自增语句必须在批准的方法体内"

    callers = sorted(
        source.name
        for source in (BACKEND_ROOT / "app").glob("*.py")
        if _APPROVED_VERSION_ADVANCE_METHOD in source.read_text(encoding="utf-8")
    )
    assert callers == ["confirm.py"], f"版本推进只允许确认事务调用：{callers}"


# Stage 2 接线边界（stage2.md §5 S2-03/S2-05 已批准）：草稿生命周期与确认编排落位
# app/，必然引用档案领域（同快照基线读取、事务内复查与写入复用）。此处按「显式扩
# 展断言、不删测试、不放宽」的口径，把 Stage 1 的全禁守卫收窄为仍然成立的边界：
# runtime/ 只允许显式白名单（S4-04 起见下方 `_PROFILE_WIRING_ALLOWED_IN_RUNTIME`）；app/ 与
# api/ 内的接线只允许显式白名单；档案
# SQL 写入仍由上方旁路守卫锁在 domain/profile/repo.py，版本推进由上方版本守卫锁在同一
# repo 与确认事务。
# S3-04（stage3.md §5）：计划草稿创建在单一快照内读正式档案与限制，并以可选受限组合补丁
# 的拟议条件保存草稿（01 1.5），故 `plan_drafts.py` 同口径引用档案领域；它只读正式档案
# 与版本、不写档案、不推进 `context_version`（写入与推进仍由上方守卫锁死）。
# S3-07（stage3.md §5）：计划只读投影与「基于计划的指导」前置安全复核要按最新正式条件
# （限制与身体情况）复核整份计划（04 4.5），故 `plan_reads.py` 同口径引用档案领域；它只读
# 档案快照、不写档案、不推进版本（仍由上方两道守卫锁死）。
# S3-08（stage3.md §5）：安排草稿准备在同一快照内读正式档案与 `context_version`，并把准备
# 时的正式档案作为快照保存（01 1.3），故 `arrangement_drafts.py` 同口径引用档案领域；它只读
# 档案、不写档案、不推进版本，接受落盘仍只经 confirm.py 的确认事务。
# S3-10（stage3.md §5）：记录草稿准备在同一快照内读正式档案与 `context_version` 并作为快照
# 保存（01 1.3），故 `record_drafts.py` 同口径引用档案领域；它只读档案、不写档案、不推进
# 版本，记录侧正式写入（确认追加修订与切换指针）仍归 S3-11 的确认事务。
_PROFILE_WIRING_ALLOWED_IN_APP = frozenset(
    {
        "drafts.py",
        "confirm.py",
        "plan_drafts.py",
        "plan_reads.py",
        "arrangement_drafts.py",
        "record_drafts.py",
    }
)

# S2-07（stage2.md §5）业务 API 接线的同样口径：路由只做传输校验与响应／错误映射
# （形状见 api/dto.py），读档案走应用层入口 ProfileService.read_formal_profile（S2-01 §3
# 调用路径映射表），领域规则与 SQL 仍在应用层／repo。白名单只含这两个传输模块，
# api/ 其余文件仍全禁；改动需显式扩展本白名单。
_PROFILE_WIRING_ALLOWED_IN_API = frozenset({"dto.py", "routes_readonly.py"})

# S4-04（stage4.md §5）：Agent 每 Run 从应用层读正式档案与业务版本作为模型上下文，并以同一
# 应用层服务读草稿生成基线（08 8.6「当前事实每 Run 重读」、01 1.3），故 runtime/ 的上下文与
# 工具接线同口径引用档案领域；它们只读档案与版本、不写档案、不推进 `context_version`。写入与
# 推进仍由上方 `_PROFILE_WRITE_SQL` 旁路守卫与版本唯一推进点守卫全仓锁死。
# S4-08 的重算资格（Q2=A）只读当前 ``context_version``，经应用层 ``DraftService`` 走既有
# 生成基线入口，不在 runtime 另开档案领域引用。
_PROFILE_WIRING_ALLOWED_IN_RUNTIME = frozenset({"context.py", "tools.py"})
_PROFILE_DOMAIN_REFERENCES = ("domain.profile", "ProfileService", "ProfileRepo")


def test_profile_wiring_stays_within_the_approved_stage2_seams() -> None:
    for source in sorted((BACKEND_ROOT / "runtime").rglob("*.py")):
        text = source.read_text(encoding="utf-8")
        if not any(reference in text for reference in _PROFILE_DOMAIN_REFERENCES):
            continue
        assert source.name in _PROFILE_WIRING_ALLOWED_IN_RUNTIME, (
            f"runtime/{source.name} 引用档案领域但不在接线白名单：只允许显式扩展"
        )
    for source in sorted((BACKEND_ROOT / "api").rglob("*.py")):
        text = source.read_text(encoding="utf-8")
        if not any(reference in text for reference in _PROFILE_DOMAIN_REFERENCES):
            continue
        assert source.name in _PROFILE_WIRING_ALLOWED_IN_API, (
            f"api/{source.name} 引用档案领域但不在接线白名单：只允许显式扩展"
        )
    for source in sorted((BACKEND_ROOT / "app").rglob("*.py")):
        text = source.read_text(encoding="utf-8")
        references_profile_domain = (
            "domain.profile" in text
            or "ProfileService" in text
            or "ProfileRepo" in text
        )
        if not references_profile_domain:
            continue
        assert source.name in _PROFILE_WIRING_ALLOWED_IN_APP, (
            f"app/{source.name} 引用档案领域但不在接线白名单：只允许显式扩展"
        )
