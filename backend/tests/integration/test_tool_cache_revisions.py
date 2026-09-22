# 阶段 6 revision 契约：业务写与对应 namespace bump 同一事务提交，回滚时一起回滚；
# 每条写路径提交后 revision 恰好加一；exercises 目录变化由 schema_version 承担，不新增 namespace。
# 依据：06-cache-observability.md「数据修订号／写入改造范围／测试」。

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from shutil import copy

import aiosqlite
import pytest

from app.application.ports import CACHE_NAMESPACES
from app.application.services.body_metrics_service import BodyMetricNotFound
from app.application.services.plans_service import EvaluationNotPassed
from app.application.services.records_service import WorkoutRecordNotFound
from app.bootstrap import (
    AppServices,
    SqliteHealthProbe,
    build_repositories,
    build_services,
)
from app.domain.plans.schema import (
    DeterministicResult,
    EvaluationResult,
    PlanDraft,
    RubricResult,
    RubricVerdict,
    RuleFailure,
)
from app.domain.records.schema import WorkoutSetInput
from app.infrastructure.database.connection import Database
from app.infrastructure.database.migrations import DEFAULT_MIGRATIONS_DIR
from app.infrastructure.database.repositories.tool_cache_repository import (
    ToolCacheRevisionsRepo,
)
from tests.agent.test_agent_run_branches import BUSINESS_DAY, _profile, _row_counts

MIGRATIONS_DIR = DEFAULT_MIGRATIONS_DIR

BARBELL_BACK_SQUAT = "barbell-back-squat"
CREATED_AT = "2026-06-01T08:00:00+00:00"
PERFORMED_ON = date(2026, 5, 20)


def _services(db: Database) -> AppServices:
    """生产构造点在 ``app.bootstrap``，测试复用同一入口，不另写一套接线。"""
    return build_services(
        build_repositories(db),
        db,
        SqliteHealthProbe(db, db.path.parent),
    )


@dataclass(frozen=True, slots=True)
class RevisionHarness:
    db: Database
    revisions: ToolCacheRevisionsRepo

    async def read(self, namespace: str) -> int:
        return (await self.revisions.read_all())[namespace]


@asynccontextmanager
async def _harness(tmp_path: Path) -> AsyncIterator[RevisionHarness]:
    db = Database(tmp_path / "fit_agent.db")
    await db.open()
    await db.migrate()
    try:
        await _services(db).profile.update(_profile())
        yield RevisionHarness(db=db, revisions=ToolCacheRevisionsRepo(db))
    finally:
        await db.close()


def _squat_set(*, reps: int = 5) -> WorkoutSetInput:
    """杠铃背蹲的一个工作组：外加负重口径与重量同现。"""
    return WorkoutSetInput(
        exercise_id=BARBELL_BACK_SQUAT,
        set_no=1,
        reps=reps,
        set_type="work",
        load_convention="barbell_includes_bar_total",
        weight_kg=60.0,
    )


def _verdict() -> RubricVerdict:
    return RubricVerdict(passed=True, reason="符合目标")


def _evaluation(*, passed: bool) -> EvaluationResult:
    rubric = RubricResult(
        goal_alignment=_verdict(),
        schedule_reasonableness=_verdict(),
        explanation_quality=_verdict(),
    )
    deterministic = DeterministicResult(
        passed=passed,
        failures=()
        if passed
        else (RuleFailure(code="weekly_frequency", message="与画像不符"),),
    )
    return EvaluationResult(
        passed=passed,
        deterministic=deterministic,
        rubric=rubric,
        blocking_failures=(),
        warnings=(),
        revision_count=0,
    )


def _draft_content() -> dict:
    """单练的自重计划：窗口 2026-06-01–07，与画像每周一次一致。"""
    return {
        "goal": "增肌",
        "starts_on": "2026-06-01",
        "explanation": "每周一练",
        "weekly_frequency": 1,
        "training_days": [
            {
                "scheduled_on": "2026-06-02",
                "exercises": [
                    {
                        "exercise_id": "pull-up",
                        "sets": 3,
                        "prescription": {
                            "type": "bodyweight_reps",
                            "reps_min": 8,
                            "reps_max": 12,
                            "progression_note": None,
                        },
                    }
                ],
            }
        ],
    }


async def test_fresh_migration_seeds_four_namespaces_at_zero(tmp_path: Path) -> None:
    """迁移后四个域都在位且从 0 起（建档前的原始基线，不经 harness 写入）。"""
    db = Database(tmp_path / "fresh.db")
    await db.open()
    try:
        await db.migrate()
        assert await ToolCacheRevisionsRepo(db).read_all() == {
            "plans": 0,
            "workouts": 0,
            "metrics": 0,
            "profile": 0,
        }
    finally:
        await db.close()


@asynccontextmanager
async def _open_db(
    path: Path, migrations_dir: Path | None = None
) -> AsyncIterator[Database]:
    db = Database(path, migrations_dir)
    await db.open()
    try:
        yield db
    finally:
        await db.close()


async def test_upgrade_keeps_existing_revisions_and_adds_profile_at_zero(
    tmp_path: Path,
) -> None:
    """v6 库升级到 v7：三域已推进的 revision 原样保留，profile 从 0 起。"""
    v6_dir = tmp_path / "v6"
    v6_dir.mkdir()
    for name in sorted(MIGRATIONS_DIR.glob("00[1-6]_*.sql")):
        copy(name, v6_dir / name.name)

    db_path = tmp_path / "legacy.db"
    async with _open_db(db_path, v6_dir) as legacy:
        assert await legacy.migrate() == 6

        async def advance(conn: aiosqlite.Connection) -> None:
            cursor = await conn.execute(
                "UPDATE tool_cache_revisions SET revision = 7 WHERE namespace = 'plans'"
            )
            await cursor.close()

        await legacy.under_lock(advance)

    async with _open_db(db_path) as upgraded:
        assert await upgraded.migrate() == 7
        assert await ToolCacheRevisionsRepo(upgraded).read_all() == {
            "plans": 7,
            "workouts": 0,
            "metrics": 0,
            "profile": 0,
        }


async def test_migration_does_not_add_catalog_namespace(tmp_path: Path) -> None:
    """exercises 内容变化由迁移推进 schema_version，不新增 revision namespace。"""
    async with _harness(tmp_path) as h:
        assert tuple(sorted(await h.revisions.read_all())) == tuple(
            sorted(CACHE_NAMESPACES)
        )
        assert "catalog" not in await h.revisions.read_all()


async def test_records_write_paths_bump_workouts_once_each(tmp_path: Path) -> None:
    async with _harness(tmp_path) as h:
        service = _services(h.db).records
        created = await service.create(PERFORMED_ON, (_squat_set(),))
        assert await h.read("workouts") == 1

        await service.update(created.id, PERFORMED_ON, (_squat_set(reps=6),))
        assert await h.read("workouts") == 2

        await service.delete(created.id)
        assert await h.read("workouts") == 3


async def test_records_rollback_leaves_revision_unchanged(tmp_path: Path) -> None:
    async with _harness(tmp_path) as h:
        service = _services(h.db).records
        created = await service.create(PERFORMED_ON, (_squat_set(),))
        before = await h.read("workouts")

        with pytest.raises(WorkoutRecordNotFound):
            await service.delete(created.id + 1000)
        with pytest.raises(WorkoutRecordNotFound):
            await service.update(
                created.id + 1000, PERFORMED_ON, (_squat_set(reps=7),)
            )

        assert await h.read("workouts") == before
        assert (await service.get(created.id)) is not None


async def test_metrics_write_paths_bump_metrics_once_each(tmp_path: Path) -> None:
    async with _harness(tmp_path) as h:
        service = _services(h.db).body_metrics
        created = await service.create(PERFORMED_ON, 78.5, 18.2)
        assert await h.read("metrics") == 1

        await service.update(created.id, PERFORMED_ON, 78.0, 18.0)
        assert await h.read("metrics") == 2

        await service.delete(created.id)
        assert await h.read("metrics") == 3


async def test_metrics_rollback_leaves_revision_unchanged(tmp_path: Path) -> None:
    async with _harness(tmp_path) as h:
        service = _services(h.db).body_metrics
        created = await service.create(PERFORMED_ON, 78.5, None)
        before = await h.read("metrics")

        with pytest.raises(BodyMetricNotFound):
            await service.update(created.id + 1000, PERFORMED_ON, 78.0, None)
        with pytest.raises(BodyMetricNotFound):
            await service.delete(created.id + 1000)

        assert await h.read("metrics") == before


async def _drop_singleton_profile_row(conn: aiosqlite.Connection) -> None:
    """制造画像写入失败：单例行缺失时 ``UPDATE`` 影响 0 行。"""
    cursor = await conn.execute("DELETE FROM athlete_profile WHERE id = 1")
    await cursor.close()


async def test_profile_write_bumps_profile_once_and_rolls_it_back_with_the_row(
    tmp_path: Path,
) -> None:
    async with _harness(tmp_path) as h:
        assert await h.read("profile") == 1  # harness 建档一次

        await _services(h.db).profile.update(_profile())
        assert await h.read("profile") == 2

        # 单例行缺失：画像写入在同一事务内失败，已 bump 的 revision 必须一起回滚。
        await h.db.under_lock(_drop_singleton_profile_row)
        with pytest.raises(RuntimeError):
            await _services(h.db).profile.update(_profile())
        assert await h.read("profile") == 2


async def test_persist_draft_writes_once_and_rejects_unpassed_evaluation(
    tmp_path: Path,
) -> None:
    """persist_draft 只在评估通过时写一行并递增 revision；未通过就地失败，不碰任何行与 revision。"""
    async with _harness(tmp_path) as h:
        plans = _services(h.db).plan_writes
        draft = PlanDraft.model_validate(_draft_content())

        written = await plans.persist_draft(
            draft,
            _evaluation(passed=True),
            existing_draft_id=None,
            created_at=CREATED_AT,
        )
        assert await h.read("plans") == 1

        await plans.persist_draft(
            draft,
            _evaluation(passed=True),
            existing_draft_id=written.id,
            created_at=CREATED_AT,
        )
        assert await h.read("plans") == 2

        with pytest.raises(EvaluationNotPassed):
            await plans.persist_draft(
                draft,
                _evaluation(passed=False),
                existing_draft_id=None,
                created_at=CREATED_AT,
            )
        assert await h.read("plans") == 2
        assert (await _row_counts(h.db))["plans"] == 1
        assert (await _row_counts(h.db))["plan_sessions"] == 0


async def test_plan_archive_and_activate_bump_plans_once_each(tmp_path: Path) -> None:
    async with _harness(tmp_path) as h:
        plans = _services(h.db).plan_writes
        draft = PlanDraft.model_validate(_draft_content())

        archived_draft = await plans.persist_draft(
            draft,
            _evaluation(passed=True),
            existing_draft_id=None,
            created_at=CREATED_AT,
        )
        await plans.archive_draft(archived_draft.id, archived_at=CREATED_AT)
        assert await h.read("plans") == 2

        activatable = await plans.persist_draft(
            draft,
            _evaluation(passed=True),
            existing_draft_id=None,
            created_at=CREATED_AT,
        )
        await plans.activate_plan(
            activatable.id,
            business_day=BUSINESS_DAY,
            confirmed_at=CREATED_AT,
            archived_at=CREATED_AT,
        )
        assert await h.read("plans") == 4
