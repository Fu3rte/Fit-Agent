# read_progress：全时三类 PB、窗口内体重变化与业务日口径停训天数的严格投影。
# window_days 只约束体重变化；PB 与停训天数保持各自既有口径。
# 事实用 tmp_path 下的真实迁移库与真实 Repo／Service；模型入口只用不可调用的替身。

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, fields
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from app.application.agent.budget import ModelRequestBudget
from app.application.agent.contracts import ToolExecutionContext
from app.application.agent.harness.tools.common import TrainingHarnessContext
from app.application.agent.harness.tools.read_progress import (
    InactivityView,
    ProgressPayload,
    ReadProgressArgs,
    read_progress,
)
from app.application.ports import ModelGateway
from app.bootstrap import (
    ReadRepositories,
    SqliteHealthProbe,
    build_repositories,
    build_services,
)
from app.domain.records.schema import WorkoutSetInput
from app.infrastructure.database.connection import Database

BUSINESS_DAY = date(2026, 6, 1)

BARBELL_BACK_SQUAT = "barbell-back-squat"
PULL_UP = "pull-up"
PLANK = "plank"


async def _unavailable_model(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("read_progress 只读，不触达模型入口")


_MODEL = ModelGateway(
    text=_unavailable_model,
    structured=_unavailable_model,
    tools=_unavailable_model,
)


@dataclass(frozen=True, slots=True)
class ProgressHarness:
    """一次测试的迁移库、真实服务与可指定业务日的注入上下文。"""

    db: Database
    repositories: ReadRepositories
    services: Any

    def context(self, business_day: date = BUSINESS_DAY) -> TrainingHarnessContext:
        return TrainingHarnessContext(
            model=_MODEL,
            budget=ModelRequestBudget(),
            business_day=business_day,
            profiles=self.repositories.profiles,
            plans=self.repositories.plans,
            catalog=self.repositories.exercises,
            records=self.services.records,
            stats=self.services.stats,
        )

    async def seed_workout(
        self, performed_on: date, sets: tuple[WorkoutSetInput, ...]
    ) -> int:
        session = await self.services.records.create(performed_on, sets)
        return session.id

    async def seed_weight(self, measured_on: date, weight_kg: float) -> None:
        await self.services.body_metrics.create(measured_on, weight_kg)


@asynccontextmanager
async def _harness(tmp_path: Path) -> AsyncIterator[ProgressHarness]:
    db = Database(tmp_path / "fit_agent.db")
    await db.open()
    await db.migrate()
    try:
        repositories = build_repositories(db)
        services = build_services(
            repositories, db, SqliteHealthProbe(db, db.path.parent)
        )
        yield ProgressHarness(db=db, repositories=repositories, services=services)
    finally:
        await db.close()


async def _call(
    harness: ProgressHarness,
    *,
    business_day: date = BUSINESS_DAY,
    **args: Any,
) -> Any:
    content = await read_progress.ainvoke(
        {
            "runtime": SimpleNamespace(context=harness.context(business_day)),
            **args,
        }
    )
    return json.loads(content)


def _squat_set(*, weight_kg: float = 60.0, reps: int = 5) -> WorkoutSetInput:
    return WorkoutSetInput(
        exercise_id=BARBELL_BACK_SQUAT,
        set_no=1,
        reps=reps,
        set_type="work",
        load_convention="barbell_includes_bar_total",
        weight_kg=weight_kg,
    )


def _pull_up_set(*, reps: int = 12) -> WorkoutSetInput:
    return WorkoutSetInput(exercise_id=PULL_UP, set_no=1, reps=reps, set_type="work")


def _plank_set(*, duration_seconds: int = 60) -> WorkoutSetInput:
    return WorkoutSetInput(
        exercise_id=PLANK,
        set_no=1,
        reps=None,
        set_type="work",
        duration_seconds=duration_seconds,
    )


async def test_window_days_defaults_to_thirty(tmp_path: Path) -> None:
    """缺省 window_days 为 30，顶层字段集严格锁定。"""
    async with _harness(tmp_path) as harness:
        payload = await _call(harness)

        assert set(payload) == {
            "business_day",
            "window_days",
            "personal_bests",
            "weight_change",
            "days_since_last_workout",
        }
        assert payload["window_days"] == 30
        assert payload["business_day"] == "2026-06-01"
        assert payload["personal_bests"] == []
        ProgressPayload.model_validate(payload)


async def test_window_days_accepts_seven_and_three_sixty_five(tmp_path: Path) -> None:
    """7 与 365 两个端点合法，越界值由 Schema 拒绝。"""
    async with _harness(tmp_path) as harness:
        assert (await _call(harness, window_days=7))["window_days"] == 7
        assert (await _call(harness, window_days=365))["window_days"] == 365

        for value in (6, 366):
            with pytest.raises(ValidationError):
                ReadProgressArgs.model_validate(
                    {"runtime": None, "window_days": value}
                )


async def test_window_days_bounds_the_weight_change_window(tmp_path: Path) -> None:
    """窗口为 [business_day - (window_days - 1), business_day]：窗口外的体重点不参与。"""
    async with _harness(tmp_path) as harness:
        await harness.seed_weight(date(2026, 5, 10), 75.0)
        await harness.seed_weight(date(2026, 5, 30), 74.0)

        narrow = (await _call(harness, window_days=7))["weight_change"]
        assert narrow["status"] == "insufficient_data"

        wide = (await _call(harness, window_days=30))["weight_change"]
        assert wide["status"] == "ok"
        assert wide["current"] == pytest.approx(74.0)
        assert wide["previous"] == pytest.approx(75.0)
        assert wide["change"] == pytest.approx(-1.0)


async def test_weight_change_reports_no_data_single_and_double_points(
    tmp_path: Path,
) -> None:
    """体重无记录为 no_data，单点为 insufficient_data，双点才给变化值。"""
    async with _harness(tmp_path) as harness:
        assert (await _call(harness))["weight_change"] == {
            "status": "no_data",
            "current": None,
            "current_on": None,
            "previous": None,
            "previous_on": None,
            "change": None,
        }

        await harness.seed_weight(date(2026, 5, 30), 74.2)

        single = (await _call(harness))["weight_change"]
        assert single["status"] == "insufficient_data"
        assert single["current"] is None
        assert single["change"] is None

        await harness.seed_weight(date(2026, 5, 25), 74.8)

        double = (await _call(harness))["weight_change"]
        assert double == {
            "status": "ok",
            "current": pytest.approx(74.2),
            "current_on": "2026-05-30",
            "previous": pytest.approx(74.8),
            "previous_on": "2026-05-25",
            "change": pytest.approx(-0.6),
        }


async def test_personal_bests_project_all_three_types(tmp_path: Path) -> None:
    """三类 PB 各自投影来源组身份、数值与适用重量；PB 不受 window_days 限制。"""
    async with _harness(tmp_path) as harness:
        squat_id = await harness.seed_workout(date(2020, 1, 1), (_squat_set(),))
        await harness.seed_workout(date(2026, 5, 30), (_pull_up_set(reps=12),))
        await harness.seed_workout(date(2026, 5, 30), (_plank_set(duration_seconds=60),))

        payload = await _call(harness, window_days=7)

        bests = {best["pb_type"]: best for best in payload["personal_bests"]}
        assert set(bests) == {"weight_pb", "reps_pb", "duration_pb"}
        assert bests["weight_pb"] == {
            "exercise_id": BARBELL_BACK_SQUAT,
            "exercise_name": "杠铃背蹲",
            "pb_type": "weight_pb",
            "value": pytest.approx(60.0),
            "workout_session_id": squat_id,
            "set_no": 1,
            "performed_on": "2020-01-01",
            "load_convention": "barbell_includes_bar_total",
            "weight_kg": pytest.approx(60.0),
        }
        assert bests["reps_pb"]["value"] == pytest.approx(12.0)
        assert bests["reps_pb"]["load_convention"] is None
        assert bests["reps_pb"]["weight_kg"] is None
        assert bests["duration_pb"]["value"] == pytest.approx(60.0)


async def test_inactivity_reports_no_workout_and_workout(tmp_path: Path) -> None:
    """没有训练记录为 no_data；有记录即按业务日给出天数与最近训练日。"""
    async with _harness(tmp_path) as harness:
        assert (await _call(harness))["days_since_last_workout"] == {
            "status": "no_data",
            "days": None,
            "last_performed_on": None,
        }

        await harness.seed_workout(date(2026, 5, 30), (_squat_set(),))

        assert (await _call(harness))["days_since_last_workout"] == {
            "status": "ok",
            "days": 2,
            "last_performed_on": "2026-05-30",
        }


async def test_business_day_determines_inactivity_days(tmp_path: Path) -> None:
    """同一训练记录下，停训天数只由注入的业务日决定，不由统计窗口决定。"""
    async with _harness(tmp_path) as harness:
        await harness.seed_workout(date(2026, 5, 30), (_squat_set(),))

        early = await _call(
            harness, business_day=date(2026, 6, 1), window_days=7
        )
        late = await _call(
            harness, business_day=date(2026, 6, 5), window_days=365
        )

        assert early["days_since_last_workout"]["days"] == 2
        assert late["days_since_last_workout"]["days"] == 6
        InactivityView.model_validate(late["days_since_last_workout"])


def test_runtime_is_hidden_from_the_model_visible_schema() -> None:
    """模型可见 Schema 只暴露 window_days；runtime、身份与 revision 都不出现。"""
    visible = cast(Any, read_progress.tool_call_schema).model_json_schema()

    assert set(visible["properties"]) == {"window_days"}
    assert read_progress.args_schema is ReadProgressArgs
    assert set(ReadProgressArgs.model_json_schema()["properties"]) == {
        "runtime",
        "window_days",
    }
    assert ReadProgressArgs.model_json_schema()["additionalProperties"] is False
    injected = {field.name for field in fields(ToolExecutionContext)} | {"revision"}
    assert injected.isdisjoint(visible["properties"])


def test_args_reject_undeclared_fields() -> None:
    """进展参数拒绝未声明字段：业务日只能来自注入上下文。"""
    ReadProgressArgs(runtime=None)
    with pytest.raises(ValidationError):
        ReadProgressArgs.model_validate(
            {"runtime": None, "business_day": "2026-06-02"}
        )
