"""Stage 1 子任务 02 §8：计划只读 domain/plans —— active／draft／历史版本与计划日程读取。

对照 02 清单：以新 plans / plan_sessions 重写 schema 和 repo、支持读取当前 active 计划、draft
计划、历史版本与日程、保存并暴露来源计划 ID／单调版本号／最小评估字段、暂不开放创建确认拒绝
激活入口。计划数据在本测试中只用直接 SQL 写入（不经过领域写入口，领域层也没有写入口）。

测试只使用 pytest tmp_path 下的独立临时库，不 import tests.support。
"""

import json
from datetime import date
from pathlib import Path

import pytest

from domain.plans.repo import PlanRepo
from domain.plans.schema import InvalidPlanRow, Plan, PlanSession
from domain.plans.service import PlanReadService
from storage.db import Database

#: 计划载荷在 Stage 1 只按不透明合法 JSON 读取：形状不做任何校验。
OPAQUE_CONTENT = {"plan_workouts": [{"workout_key": "A"}], "任意键": [1, 2, 3]}


async def _migrated(path: Path) -> Database:
    db = Database(path)
    await db.open()
    await db.migrate()
    return db


async def _insert_plan(
    db: Database,
    *,
    plan_id: int,
    version: int,
    status: str,
    content: object = None,
    source_plan_id: int | None = None,
    evaluator_result: object | None = None,
    created_at: str = "2026-06-01T00:00:00+08:00",
    confirmed_at: str | None = None,
    archived_at: str | None = None,
) -> None:
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO plans (id, version, status, source_plan_id, structured_content,"
            " evaluator_result, created_at, confirmed_at, archived_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id,
                version,
                status,
                source_plan_id,
                json.dumps(OPAQUE_CONTENT if content is None else content),
                None if evaluator_result is None else json.dumps(evaluator_result),
                created_at,
                confirmed_at,
                archived_at,
            ),
        )


async def _insert_session(
    db: Database,
    *,
    session_id: int,
    plan_id: int,
    scheduled_on: str,
    cancelled_at: str | None = None,
) -> None:
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO plan_sessions (id, plan_id, scheduled_on, cancelled_at)"
            " VALUES (?, ?, ?, ?)",
            (session_id, plan_id, scheduled_on, cancelled_at),
        )


async def test_read_active_returns_only_the_enabled_plan(tmp_path: Path) -> None:
    """当前 active 计划：无 active 时返回 None，不拿 draft 或历史版本当替代。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        repo = PlanRepo(db)
        assert await repo.read_active() is None
        await _insert_plan(db, plan_id=1, version=1, status="draft")
        await _insert_plan(db, plan_id=2, version=2, status="archived")
        assert await repo.read_active() is None
        await _insert_plan(db, plan_id=3, version=3, status="active")
        active = await repo.read_active()
        assert active is not None
        assert active.id == 3
        assert active.status == "active"
    finally:
        await db.close()


async def test_drafts_and_versions_are_readable(tmp_path: Path) -> None:
    """draft 计划与历史（归档）版本可分别读取，全量按单调版本号升序。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        repo = PlanRepo(db)
        await _insert_plan(db, plan_id=1, version=1, status="archived")
        await _insert_plan(db, plan_id=2, version=2, status="active")
        await _insert_plan(db, plan_id=3, version=3, status="draft")

        drafts = await repo.list_drafts()
        assert [(plan.id, plan.status) for plan in drafts] == [(3, "draft")]

        versions = await repo.list_versions()
        assert [plan.version for plan in versions] == [1, 2, 3]
        assert [plan.status for plan in versions] == ["archived", "active", "draft"]

        archived = await repo.read_by_id(1)
        assert archived is not None and archived.status == "archived"
        assert await repo.read_by_id(999) is None
    finally:
        await db.close()


async def test_source_plan_id_version_and_evaluator_result_are_exposed(
    tmp_path: Path,
) -> None:
    """最小追溯字段：来源计划 ID、单调版本号与最小评估字段原样读回。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        await _insert_plan(db, plan_id=1, version=1, status="archived")
        await _insert_plan(
            db,
            plan_id=2,
            version=2,
            status="active",
            source_plan_id=1,
            evaluator_result={"ok": False, "reason": "负荷来源缺失"},
            confirmed_at="2026-06-02T09:00:00+08:00",
        )
        repo = PlanRepo(db)
        active = await repo.read_active()
        assert active is not None
        assert active.source_plan_id == 1
        assert active.version == 2
        assert active.evaluator_result == {"ok": False, "reason": "负荷来源缺失"}
        assert active.confirmed_at == "2026-06-02T09:00:00+08:00"
        first = await repo.read_by_id(1)
        assert first is not None
        assert first.source_plan_id is None
        assert first.evaluator_result is None
    finally:
        await db.close()


async def test_structured_content_is_read_as_opaque_json(tmp_path: Path) -> None:
    """structured_content 只按不透明合法 JSON 读取：形状不被解释、不被裁剪。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        await _insert_plan(db, plan_id=1, version=1, status="draft")
        draft = await PlanRepo(db).read_by_id(1)
        assert draft is not None
        assert draft.structured_content == OPAQUE_CONTENT
    finally:
        await db.close()


async def test_sessions_are_read_per_plan_including_cancelled(tmp_path: Path) -> None:
    """计划日程：按计划读取，含已取消行（行不物理删除），按应训练日排序。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        await _insert_plan(db, plan_id=1, version=1, status="active")
        await _insert_plan(db, plan_id=2, version=2, status="draft")
        await _insert_session(db, session_id=2, plan_id=1, scheduled_on="2026-06-03")
        await _insert_session(
            db,
            session_id=1,
            plan_id=1,
            scheduled_on="2026-06-01",
            cancelled_at="2026-06-02T09:00:00+08:00",
        )
        await _insert_session(db, session_id=3, plan_id=2, scheduled_on="2026-06-08")

        sessions = await PlanRepo(db).list_sessions(1)
        assert sessions == (
            PlanSession(
                id=1,
                plan_id=1,
                scheduled_on=date(2026, 6, 1),
                cancelled_at="2026-06-02T09:00:00+08:00",
            ),
            PlanSession(
                id=2,
                plan_id=1,
                scheduled_on=date(2026, 6, 3),
                cancelled_at=None,
            ),
        )
        assert await PlanRepo(db).list_sessions(999) == ()
    finally:
        await db.close()


async def test_read_service_exposes_active_drafts_history_and_sessions(
    tmp_path: Path,
) -> None:
    """只读服务是 repo 的用例入口：active／draft／历史／日程都可读。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        await _insert_plan(db, plan_id=1, version=1, status="archived")
        await _insert_plan(db, plan_id=2, version=2, status="active")
        await _insert_plan(db, plan_id=3, version=3, status="draft")
        await _insert_session(db, session_id=1, plan_id=2, scheduled_on="2026-06-05")

        service = PlanReadService(db)
        active = await service.get_active()
        assert active is not None and active.id == 2
        assert [plan.id for plan in await service.list_drafts()] == [3]
        assert [plan.version for plan in await service.list_versions()] == [1, 2, 3]
        assert await service.get_by_id(1) is not None
        assert await service.get_by_id(999) is None
        assert [session.id for session in await service.list_sessions(2)] == [1]
        assert await service.list_sessions(999) == ()
    finally:
        await db.close()


def test_repo_exposes_no_plan_write_entries() -> None:
    """Stage 1 不开放计划创建、确认、拒绝或激活入口（激活事务留到 Stage 5）。"""
    forbidden = ("create", "confirm", "reject", "activate", "archive", "insert", "update")
    assert not [
        name for name in dir(PlanRepo) if name.lower().startswith(forbidden)
    ]
    assert not [
        name for name in dir(PlanReadService) if name.lower().startswith(forbidden)
    ]


def test_corrupt_plan_row_is_rejected() -> None:
    """状态越界与 JSON 列损坏都大声失败，不静默兜底（库内 CHECK 只是兜底）。"""
    with pytest.raises(InvalidPlanRow):
        Plan.from_row(
            {
                "id": 1,
                "version": 1,
                "status": "pending",
                "source_plan_id": None,
                "structured_content": "{}",
                "evaluator_result": None,
                "created_at": "t",
                "confirmed_at": None,
                "archived_at": None,
            }
        )
    with pytest.raises(InvalidPlanRow):
        Plan.from_row(
            {
                "id": 1,
                "version": 1,
                "status": "draft",
                "source_plan_id": None,
                "structured_content": "not json",
                "evaluator_result": None,
                "created_at": "t",
                "confirmed_at": None,
                "archived_at": None,
            }
        )
