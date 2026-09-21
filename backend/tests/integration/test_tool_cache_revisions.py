# 阶段 6 revision 契约：业务写与对应 namespace bump 同一事务提交，回滚时一起回滚；
# 每条写路径提交后 revision 恰好加一；exercises 目录变化由 schema_version 承担，不新增 namespace。
# 依据：06-cache-observability.md「数据修订号／写入改造范围／测试」。

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

from app.application.ports import CACHE_NAMESPACES
from app.application.services.body_metrics_service import BodyMetricNotFound
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
from app.infrastructure.database.repositories.tool_cache_repository import (
    ToolCacheRevisionsRepo,
)
from tests.agent.test_agent_run_branches import BUSINESS_DAY, _profile

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


async def test_fresh_migration_seeds_three_namespaces_at_zero(tmp_path: Path) -> None:
    async with _harness(tmp_path) as h:
        assert await h.revisions.read_all() == {
            "plans": 0,
            "workouts": 0,
            "metrics": 0,
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


async def test_plan_persistence_paths_bump_plans_once_each(tmp_path: Path) -> None:
    async with _harness(tmp_path) as h:
        persistence = _services(h.db).plan_persistence
        draft = PlanDraft.model_validate(_draft_content())

        written = await persistence.persist_plan_result(
            draft,
            _evaluation(passed=True),
            existing_draft_id=None,
            created_at=CREATED_AT,
        )
        assert await h.read("plans") == 1

        await persistence.persist_plan_result(
            draft,
            _evaluation(passed=True),
            existing_draft_id=written.id,
            created_at=CREATED_AT,
        )
        assert await h.read("plans") == 2

        rejected = await persistence.persist_plan_result(
            draft,
            _evaluation(passed=False),
            existing_draft_id=None,
            created_at=CREATED_AT,
        )
        assert rejected.status == "rejected"
        assert await h.read("plans") == 3


async def test_plan_reject_and_activate_bump_plans_once_each(tmp_path: Path) -> None:
    async with _harness(tmp_path) as h:
        persistence = _services(h.db).plan_persistence
        activation = _services(h.db).plan_activation
        draft = PlanDraft.model_validate(_draft_content())

        rejected_draft = await persistence.persist_plan_result(
            draft,
            _evaluation(passed=True),
            existing_draft_id=None,
            created_at=CREATED_AT,
        )
        await activation.reject(rejected_draft.id, archived_at=CREATED_AT)
        assert await h.read("plans") == 2

        activatable = await persistence.persist_plan_result(
            draft,
            _evaluation(passed=True),
            existing_draft_id=None,
            created_at=CREATED_AT,
        )
        await activation.activate(
            activatable.id,
            business_day=BUSINESS_DAY,
            confirmed_at=CREATED_AT,
            archived_at=CREATED_AT,
        )
        assert await h.read("plans") == 4
