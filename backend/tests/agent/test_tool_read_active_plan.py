# read_active_plan：无参读取当前 active 计划与七天窗口内的训练日。
# 输出严格投影业务日与 active 计划；canonical 动作名称按目录关联，缺失即目录引用损坏明确失败。
# 事实用 tmp_path 下的真实迁移库与真实 Repo／Service；模型入口只用不可调用的替身。

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import fields
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from app.application.agent.budget import ModelRequestBudget
from app.application.agent.contracts import ToolExecutionContext
from app.application.agent.harness.tools.common import (
    CatalogReferentialIntegrityError,
    TrainingHarnessContext,
)
from app.application.agent.harness.tools.read_active_plan import (
    ActivePlanView,
    ReadActivePlanArgs,
    ReadActivePlanPayload,
    read_active_plan,
)
from app.application.ports import ModelGateway
from app.bootstrap import SqliteHealthProbe, build_repositories, build_services
from app.infrastructure.database.connection import Database

BUSINESS_DAY = date(2026, 6, 1)
CREATED_AT = "2026-06-01T08:00:00+00:00"

BARBELL_BACK_SQUAT = "barbell-back-squat"
BARBELL_BENCH_PRESS = "barbell-bench-press"
PULL_UP = "pull-up"
PLANK = "plank"


async def _unavailable_model(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("read_active_plan 只读，不触达模型入口")


_MODEL = ModelGateway(
    text=_unavailable_model,
    structured=_unavailable_model,
    tools=_unavailable_model,
)


@asynccontextmanager
async def _harness(
    tmp_path: Path,
) -> AsyncIterator[tuple[Database, TrainingHarnessContext]]:
    db = Database(tmp_path / "fit_agent.db")
    await db.open()
    await db.migrate()
    try:
        repositories = build_repositories(db)
        services = build_services(
            repositories, db, SqliteHealthProbe(db, db.path.parent)
        )
        context = TrainingHarnessContext(
            model=_MODEL,
            budget=ModelRequestBudget(),
            business_day=BUSINESS_DAY,
            profiles=repositories.profiles,
            plans=repositories.plans,
            catalog=repositories.exercises,
            records=services.records,
            stats=services.stats,
        )
        yield db, context
    finally:
        await db.close()


async def _seed_plan(db: Database, content: Mapping[str, Any]) -> int:
    """预置一个 active 计划行，返回计划身份。"""
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "INSERT INTO plans (version, status, source_plan_id, structured_content,"
            " created_at, confirmed_at) VALUES (1, 'active', NULL, ?, ?, ?)",
            (json.dumps(content, ensure_ascii=False), CREATED_AT, CREATED_AT),
        )
        try:
            return int(cursor.lastrowid or 0)
        finally:
            await cursor.close()


async def _call(context: TrainingHarnessContext) -> Any:
    content = await read_active_plan.ainvoke(
        {"runtime": SimpleNamespace(context=context)}
    )
    return json.loads(content)


def _plan_content(*, squat_id: str = BARBELL_BACK_SQUAT) -> dict[str, Any]:
    """三练的 active 计划：训练日在 JSON 内乱序，窗口 2026-06-01–07。"""
    return {
        "goal": "增肌",
        "starts_on": "2026-06-01",
        "explanation": "每周三练",
        "weekly_frequency": 3,
        "training_days": [
            {
                "scheduled_on": "2026-06-02",
                "exercises": [
                    {
                        "exercise_id": PULL_UP,
                        "sets": 3,
                        "prescription": {
                            "type": "bodyweight_reps",
                            "reps_min": 8,
                            "reps_max": 12,
                            "progression_note": None,
                        },
                    }
                ],
            },
            {
                "scheduled_on": "2026-06-01",
                "exercises": [
                    {
                        "exercise_id": squat_id,
                        "sets": 3,
                        "prescription": {
                            "type": "weighted_reps",
                            "reps_min": 5,
                            "reps_max": 8,
                            "progression_note": "每周加重",
                            "load": {
                                "status": "known",
                                "weight_kg": 60.0,
                                "source_workout_session_id": 1,
                                "source_set_no": 1,
                            },
                        },
                    },
                    {
                        "exercise_id": PLANK,
                        "sets": 2,
                        "prescription": {
                            "type": "timed",
                            "duration_seconds_min": 45,
                            "duration_seconds_max": 60,
                            "progression_note": None,
                        },
                    },
                ],
            },
            {
                "scheduled_on": "2026-06-03",
                "exercises": [
                    {
                        "exercise_id": BARBELL_BENCH_PRESS,
                        "sets": 3,
                        "prescription": {
                            "type": "weighted_reps",
                            "reps_min": 8,
                            "reps_max": 10,
                            "progression_note": None,
                            "load": {"status": "needs_calibration"},
                        },
                    }
                ],
            },
        ],
    }


async def test_without_active_plan_returns_null(tmp_path: Path) -> None:
    """没有 active 计划：顶层只有业务日与 null，不编造计划。"""
    async with _harness(tmp_path) as (_db, context):
        assert await _call(context) == {
            "business_day": "2026-06-01",
            "active_plan": None,
        }


async def test_full_plan_projects_exactly_the_contract_fields(tmp_path: Path) -> None:
    """完整计划严格投影全部字段：顶层、视图、训练日、动作各自锁定字段集。"""
    async with _harness(tmp_path) as (db, context):
        plan_id = await _seed_plan(db, _plan_content())

        payload = await _call(context)

        assert set(payload) == {"business_day", "active_plan"}
        plan = payload["active_plan"]
        assert set(plan) == {
            "id",
            "version",
            "starts_on",
            "ends_on",
            "goal",
            "weekly_frequency",
            "training_days",
        }
        assert plan["id"] == plan_id
        assert plan["version"] == 1
        assert plan["goal"] == "增肌"
        assert all(
            set(day) == {"scheduled_on", "exercises"} for day in plan["training_days"]
        )
        assert all(
            set(exercise) == {"exercise_id", "exercise_name", "sets", "prescription"}
            for day in plan["training_days"]
            for exercise in day["exercises"]
        )
        view = ReadActivePlanPayload.model_validate(payload).active_plan
        assert view is not None
        assert view.id == plan_id


async def test_window_and_date_order_are_correct(tmp_path: Path) -> None:
    """starts_on／ends_on 为开始日起七天闭区间；训练日按日期升序，动作保留计划原始顺序。"""
    async with _harness(tmp_path) as (db, context):
        await _seed_plan(db, _plan_content())

        plan = (await _call(context))["active_plan"]

        assert plan["starts_on"] == "2026-06-01"
        assert plan["ends_on"] == "2026-06-07"
        assert plan["weekly_frequency"] == 3
        assert [day["scheduled_on"] for day in plan["training_days"]] == [
            "2026-06-01",
            "2026-06-02",
            "2026-06-03",
        ]
        assert [
            exercise["exercise_id"]
            for exercise in plan["training_days"][0]["exercises"]
        ] == [BARBELL_BACK_SQUAT, PLANK]


async def test_canonical_exercise_names_are_resolved(tmp_path: Path) -> None:
    """canonical 动作名称按目录身份关联；处方按领域 Schema 序列化。"""
    async with _harness(tmp_path) as (db, context):
        await _seed_plan(db, _plan_content())

        days = (await _call(context))["active_plan"]["training_days"]

        assert [
            (exercise["exercise_id"], exercise["exercise_name"])
            for day in days
            for exercise in day["exercises"]
        ] == [
            (BARBELL_BACK_SQUAT, "杠铃背蹲"),
            (PLANK, "平板支撑"),
            (PULL_UP, "自重引体向上"),
            (BARBELL_BENCH_PRESS, "杠铃平板卧推"),
        ]
        squat = days[0]["exercises"][0]
        assert squat["sets"] == 3
        assert squat["prescription"]["load"]["weight_kg"] == 60.0


async def test_missing_canonical_exercise_raises(tmp_path: Path) -> None:
    """目录缺失的动作不得用 null 名称掩盖：计划引用损坏即明确失败。"""
    async with _harness(tmp_path) as (db, context):
        await _seed_plan(db, _plan_content(squat_id="not-in-catalog"))

        with pytest.raises(CatalogReferentialIntegrityError):
            await _call(context)


def test_runtime_is_hidden_from_the_model_visible_schema() -> None:
    """模型可见 Schema 没有参数；runtime、身份与 revision 字段都不进模型可见 Schema。"""
    visible = cast(Any, read_active_plan.tool_call_schema).model_json_schema()

    assert visible["properties"] == {}
    assert visible.get("required", []) == []
    assert read_active_plan.args_schema is ReadActivePlanArgs
    assert set(ReadActivePlanArgs.model_json_schema()["properties"]) == {"runtime"}
    assert ReadActivePlanArgs.model_json_schema()["additionalProperties"] is False
    injected = {field.name for field in fields(ToolExecutionContext)} | {"revision"}
    assert injected.isdisjoint(visible["properties"])


def test_active_plan_view_locks_the_contract_field_set() -> None:
    """ActivePlanView 字段集严格锁定：explanation 等历史字段被拒绝。"""
    kwargs: dict[str, Any] = {
        "id": 1,
        "version": 1,
        "starts_on": BUSINESS_DAY,
        "ends_on": date(2026, 6, 7),
        "goal": "增肌",
        "weekly_frequency": 3,
        "training_days": (),
    }
    assert ActivePlanView(**kwargs).ends_on == date(2026, 6, 7)
    with pytest.raises(ValidationError):
        ActivePlanView.model_validate({**kwargs, "explanation": "历史字段"})


def test_read_active_plan_args_reject_undeclared_fields() -> None:
    """无参工具拒绝未声明字段：业务日只能来自注入上下文。"""
    ReadActivePlanArgs(runtime=None)
    with pytest.raises(ValidationError):
        ReadActivePlanArgs.model_validate(
            {"runtime": None, "business_day": "2026-06-02"}
        )
