# prepare_workout_record：模型可见 Schema 只含 request，提取用共享预算，动作只收 canonical id，
# 组次沿用领域校验，候选日程取自业务日当天，确认前不写任何业务行。
# 事实用 tmp_path 下的真实迁移库与真实 RecordsService；模型是可脚本化的固定替身。

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.application.agent.budget import ModelRequestBudget
from app.application.agent.contracts import ToolExecutionContext
from app.application.agent.harness.tools.common import (
    TrainingHarnessContext,
)
from app.application.agent.harness.tools.exercise_dataset.store import (
    ExerciseCatalogUnavailable,
)
from app.application.agent.harness.tools.prepare_workout_record import (
    PrepareWorkoutRecordArgs,
    WorkoutConfirmationPayload,
    prepare_workout_record,
)
from app.application.ports import ModelGateway
from app.bootstrap import SqliteHealthProbe, build_repositories, build_services
from app.domain.actions.rules import UnknownExercise
from app.domain.records.rules import InvalidRecordFact
from app.infrastructure.database.connection import Database
from app.infrastructure.database.repositories.plans_repository import PlanRepo

BUSINESS_DAY = date(2026, 6, 1)
CREATED_AT = "2026-06-01T08:00:00+00:00"
PULL_UP = "pull-up"
RUN_ID = "prepare-workout-record-run"
USER_ID = "local-user"

#: 只读断言口径：任何一张业务表变化即失败。
BUSINESS_TABLES = ("plans", "plan_sessions", "workout_sessions", "workout_sets")


class _ScriptedStructured:
    """结构化提取替身：按脚本出队并交给目标 Schema 校验。"""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = list(rows)

    async def __call__(self, _system_prompt, _user_payload, schema):
        if not self._rows:
            raise AssertionError("结构化提取调用次数超出脚本")
        return schema.model_validate(self._rows.pop(0))


async def _forbidden_model_call(*_args, **_kwargs):
    raise AssertionError("本测试的模型入口只允许结构化提取")


def _extraction(
    *, exercise_id: str = PULL_UP, set_no: int = 1, reps: int = 8
) -> dict[str, object]:
    return {
        "performed_on": BUSINESS_DAY.isoformat(),
        "sets": [
            {
                "exercise_id": exercise_id,
                "set_no": set_no,
                "set_type": "work",
                "reps": reps,
            }
        ],
    }


@asynccontextmanager
async def _harness(
    tmp_path: Path,
    rows: list[dict[str, object]],
    *,
    snapshot: bool = True,
) -> AsyncIterator[tuple[Database, TrainingHarnessContext]]:
    db = Database(tmp_path / "fit_agent.db")
    await db.open()
    await db.migrate()
    try:
        repositories = build_repositories(db)
        services = build_services(
            repositories, db, SqliteHealthProbe(db, db.path.parent)
        )
        user_version = await db.pragma_value("user_version")
        assert isinstance(user_version, int)
        context = TrainingHarnessContext(
            model=ModelGateway(
                text=_forbidden_model_call,
                structured=_ScriptedStructured(rows),
                tools=_forbidden_model_call,
            ),
            budget=ModelRequestBudget(),
            business_day=BUSINESS_DAY,
            profiles=repositories.profiles,
            plans=repositories.plans,
            catalog=repositories.exercises,
            records=services.records,
            stats=services.stats,
            snapshot=(
                ToolExecutionContext(
                    user_id=USER_ID,
                    run_id=RUN_ID,
                    business_day=BUSINESS_DAY,
                    profile_revision=1,
                    workouts_revision=1,
                    plans_revision=1,
                    catalog_revision=user_version,
                )
                if snapshot
                else None
            ),
        )
        yield db, context
    finally:
        await db.close()


async def _call(context: TrainingHarnessContext, request: str) -> str:
    return await prepare_workout_record.ainvoke(
        {"request": request, "runtime": SimpleNamespace(context=context)}
    )


def _payload(content: str) -> WorkoutConfirmationPayload:
    return WorkoutConfirmationPayload.model_validate(json.loads(content))


async def _counts(db: Database) -> dict[str, int]:
    async def op(conn) -> dict[str, int]:
        counts: dict[str, int] = {}
        for table in BUSINESS_TABLES:
            async with conn.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
                counts[table] = int((await cursor.fetchone())[0])
        return counts

    return await db.under_lock(op)


async def _seed_active_plan(db: Database) -> int:
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "INSERT INTO plans (version, status, source_plan_id, structured_content,"
            " created_at, confirmed_at) VALUES (1, 'active', NULL, '{}', ?, ?)",
            (CREATED_AT, CREATED_AT),
        )
        try:
            return int(cursor.lastrowid or 0)
        finally:
            await cursor.close()


def test_request_boundaries_and_extra_fields_are_rejected() -> None:
    """request 长度 1–4000 合法，空串、超长与未声明字段由 Schema 拒绝。"""
    PrepareWorkoutRecordArgs(request="x", runtime=None)
    PrepareWorkoutRecordArgs(request="x" * 4000, runtime=None)
    for bad in (
        {"request": ""},
        {"request": "x" * 4001},
        {"request": "x", "extra": 1},
    ):
        with pytest.raises(ValidationError):
            PrepareWorkoutRecordArgs(runtime=None, **bad)
    assert PrepareWorkoutRecordArgs.model_json_schema()["additionalProperties"] is False


def test_runtime_is_hidden_from_the_model_visible_schema() -> None:
    """模型可见 Schema 只有 request；runtime 只存在于 args_schema 用于注入校验。"""
    visible = prepare_workout_record.tool_call_schema.model_json_schema()

    assert set(visible["properties"]) == {"request"}
    assert prepare_workout_record.args_schema is PrepareWorkoutRecordArgs
    assert set(PrepareWorkoutRecordArgs.model_json_schema()["properties"]) == {
        "request",
        "runtime",
    }


async def test_legal_extraction_returns_a_confirmation_payload(tmp_path: Path) -> None:
    """合法提取：workout 复用 WorkoutSetInput 投影，requires_confirmation 固定为 True。"""
    async with _harness(tmp_path, [_extraction()]) as (db, context):
        before = await _counts(db)
        payload = _payload(await _call(context, "记录今天做8个引体"))

        assert payload.requires_confirmation is True
        assert payload.workout["performed_on"] == BUSINESS_DAY.isoformat()
        assert payload.workout["sets"] == [
            {
                "exercise_id": PULL_UP,
                "set_no": 1,
                "set_type": "work",
                "load_convention": None,
                "weight_kg": None,
                "reps": 8,
                "duration_seconds": None,
            }
        ]
        assert payload.candidate_plan_sessions == ()
        assert await _counts(db) == before


async def test_candidate_plan_sessions_are_filtered_by_the_business_day(
    tmp_path: Path,
) -> None:
    """无日程回空 tuple；当天未完成日程只给 id、plan_id、scheduled_on。"""
    async with _harness(tmp_path, [_extraction(), _extraction()]) as (db, context):
        empty = _payload(await _call(context, "记录今天做8个引体"))
        assert empty.candidate_plan_sessions == ()

        plan_id = await _seed_active_plan(db)
        async with db.transaction() as conn:
            await PlanRepo(db).create_sessions_in_transaction(
                conn, plan_id, scheduled_on=[BUSINESS_DAY]
            )
        sessions = await PlanRepo(db).list_sessions(plan_id)

        payload = _payload(await _call(context, "记录今天做8个引体"))
        assert payload.candidate_plan_sessions == (
            {
                "id": sessions[0].id,
                "plan_id": plan_id,
                "scheduled_on": BUSINESS_DAY.isoformat(),
            },
        )


async def test_unknown_exercise_and_invalid_set_number_are_reraised(
    tmp_path: Path,
) -> None:
    """目录外动作抛 UnknownExercise，非法组序号抛 InvalidRecordFact，两者都不写行。"""
    async with _harness(
        tmp_path, [_extraction(exercise_id="not-in-catalog")]
    ) as (db, context):
        before = await _counts(db)
        with pytest.raises(UnknownExercise):
            await _call(context, "记录一个目录外动作")
        assert await _counts(db) == before

    (tmp_path / "second").mkdir()
    async with _harness(tmp_path / "second", [_extraction(set_no=0)]) as (db, context):
        before = await _counts(db)
        with pytest.raises(InvalidRecordFact):
            await _call(context, "记录第 0 组引体")
        assert await _counts(db) == before


async def test_missing_fact_snapshot_is_catalog_unavailable(tmp_path: Path) -> None:
    """没有 Run 事实快照：动作目录与 revision 不可用，明确失败且不写行。"""
    async with _harness(tmp_path, [_extraction()], snapshot=False) as (db, context):
        before = await _counts(db)
        with pytest.raises(ExerciseCatalogUnavailable):
            await _call(context, "记录今天做8个引体")
        assert await _counts(db) == before


async def test_tool_call_writes_no_business_rows(tmp_path: Path) -> None:
    """确认前的 Tool 调用不写训练记录表：workout_sessions 与 workout_sets 保持为零。"""
    async with _harness(tmp_path, [_extraction()]) as (db, context):
        await _call(context, "记录今天做8个引体")

        counts = await _counts(db)
        assert counts["workout_sessions"] == 0
        assert counts["workout_sets"] == 0
