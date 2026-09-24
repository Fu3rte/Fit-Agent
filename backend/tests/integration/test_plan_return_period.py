# 停训回归在确认端点的接线：activate_plan 的复验按当前业务日读停训天数，
# 跨过阈值时首动作减一组的候选能激活，未减组的候选被 sets_mismatch 挡住。

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.application.services.plans_service import PlanRevalidationFailed
from app.bootstrap import SqliteHealthProbe, build_repositories, build_services
from app.domain.plans.schema import (
    DeterministicResult,
    EvaluationResult,
    PlanDraft,
    RubricResult,
    RubricVerdict,
)
from app.domain.records.schema import WorkoutSetInput
from app.infrastructure.database.connection import Database
from app.infrastructure.database.repositories.plans_repository import PlanRepo
from tests.agent.test_agent_run_branches import _profile

CREATED_AT = "2026-06-01T08:00:00+00:00"
WORKOUT_ON = date(2026, 5, 1)
BUSINESS_DAY = date(2026, 5, 31)
PULL_UP = "pull-up"


def _plan_content(*, sets: int) -> dict[str, Any]:
    """单练的自重计划：没有已知负荷目标，因此复验只受组数与结构口径约束。"""
    return {
        "goal": "增肌",
        "starts_on": "2026-05-25",
        "explanation": "每周一练",
        "weekly_frequency": 1,
        "training_days": [
            {
                "scheduled_on": "2026-05-26",
                "exercises": [
                    {
                        "exercise_id": PULL_UP,
                        "sets": sets,
                        "prescription": {
                            "type": "bodyweight_reps",
                            "reps_min": 8,
                            "reps_max": 12,
                        },
                    }
                ],
            }
        ],
    }


def _draft_content(*, sets: int) -> dict[str, Any]:
    """调整候选：窗口落在业务日之后，训练日与 active 逐位一致。"""
    content = _plan_content(sets=sets)
    content["starts_on"] = "2026-06-01"
    content["training_days"] = [
        {
            "scheduled_on": "2026-06-02",
            "exercises": content["training_days"][0]["exercises"],
        }
    ]
    return content


def _evaluation() -> EvaluationResult:
    verdict = RubricVerdict(passed=True, reason="符合目标")
    return EvaluationResult(
        passed=True,
        deterministic=DeterministicResult(passed=True, failures=()),
        rubric=RubricResult(
            goal_alignment=verdict,
            schedule_reasonableness=verdict,
            explanation_quality=verdict,
        ),
        blocking_failures=(),
        warnings=(),
        revision_count=0,
    )


@asynccontextmanager
async def _harness(tmp_path: Path) -> AsyncIterator[Database]:
    db = Database(tmp_path / "fit_agent.db")
    await db.open()
    await db.migrate()
    try:
        services = build_services(
            build_repositories(db),
            db,
            SqliteHealthProbe(db, db.path.parent),
        )
        await services.profile.update(_profile())
        await services.records.create(
            WORKOUT_ON,
            (
                WorkoutSetInput(
                    exercise_id=PULL_UP,
                    set_no=1,
                    reps=10,
                    set_type="work",
                ),
            ),
        )
        yield db
    finally:
        await db.close()


async def _seed_active(db: Database) -> int:
    """预置 active 计划行：真实写入入口是被测服务，这里直接落一行。"""
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "INSERT INTO plans (version, status, source_plan_id, structured_content,"
            " created_at, confirmed_at) VALUES (1, 'active', NULL, ?, ?, ?)",
            (json.dumps(_plan_content(sets=3), ensure_ascii=False), CREATED_AT, CREATED_AT),
        )
        return int(cursor.lastrowid or 0)


async def _persist(db: Database, *, active_id: int, sets: int) -> int:
    services = build_services(
        build_repositories(db), db, SqliteHealthProbe(db, db.path.parent)
    )
    written = await services.plan_writes.persist_draft(
        PlanDraft.model_validate(_draft_content(sets=sets)),
        _evaluation(),
        existing_draft_id=None,
        created_at=CREATED_AT,
        source_plan_id=active_id,
    )
    return written.id


async def test_revalidation_rejects_the_original_first_sets_in_a_return_period(
    tmp_path: Path,
) -> None:
    async with _harness(tmp_path) as db:
        services = build_services(
            build_repositories(db), db, SqliteHealthProbe(db, db.path.parent)
        )
        active_id = await _seed_active(db)
        draft_id = await _persist(db, active_id=active_id, sets=3)

        with pytest.raises(PlanRevalidationFailed) as failure:
            await services.plan_writes.activate_plan(
                draft_id,
                business_day=BUSINESS_DAY,
                confirmed_at=CREATED_AT,
                archived_at=CREATED_AT,
            )

        assert [item.code for item in failure.value.failures] == ["sets_mismatch"]
        kept = await PlanRepo(db).read_by_id(draft_id)
        assert kept is not None and kept.status == "draft"


async def test_revalidation_accepts_the_reduced_first_sets_in_a_return_period(
    tmp_path: Path,
) -> None:
    async with _harness(tmp_path) as db:
        services = build_services(
            build_repositories(db), db, SqliteHealthProbe(db, db.path.parent)
        )
        active_id = await _seed_active(db)
        draft_id = await _persist(db, active_id=active_id, sets=2)

        activated = await services.plan_writes.activate_plan(
            draft_id,
            business_day=BUSINESS_DAY,
            confirmed_at=CREATED_AT,
            archived_at=CREATED_AT,
        )

        assert activated.status == "active"
        assert activated.source_plan_id == active_id
