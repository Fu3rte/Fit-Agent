# read_training_history：日期闭区间、动作集合与数量条件取 AND 的训练回溯，命中会话返回全部组。
# 条件之间 AND；动作过滤用 EXISTS；limit 在过滤与 performed_on DESC, id DESC 排序后截断。
# 事实用 tmp_path 下的真实迁移库与真实 Repo／Service；模型入口只用不可调用的替身。

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, fields
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from app.application.agent.budget import ModelRequestBudget
from app.application.agent.contracts import ToolExecutionContext
from app.application.agent.harness.tools.common import TrainingHarnessContext
from app.application.agent.harness.tools.read_training_history import (
    ReadTrainingHistoryArgs,
    TrainingHistoryPayload,
    read_training_history,
)
from app.application.ports import ModelGateway
from app.bootstrap import (
    ReadRepositories,
    SqliteHealthProbe,
    build_repositories,
    build_services,
)
from app.domain.records.schema import SetType, WorkoutSetInput
from app.infrastructure.database.connection import Database

BUSINESS_DAY = date(2026, 6, 1)

BARBELL_BACK_SQUAT = "barbell-back-squat"
PULL_UP = "pull-up"
PLANK = "plank"


async def _unavailable_model(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("read_training_history 只读，不触达模型入口")


_MODEL = ModelGateway(
    text=_unavailable_model,
    structured=_unavailable_model,
    tools=_unavailable_model,
)


@dataclass(frozen=True, slots=True)
class HistoryHarness:
    """一次测试的迁移库、真实服务与注入上下文。"""

    repositories: ReadRepositories
    services: Any

    def context(self) -> TrainingHarnessContext:
        return TrainingHarnessContext(
            model=_MODEL,
            budget=ModelRequestBudget(),
            business_day=BUSINESS_DAY,
            profiles=self.repositories.profiles,
            plans=self.repositories.plans,
            catalog=self.repositories.exercises,
            records=self.services.records,
            stats=self.services.stats,
        )

    async def seed_workout(
        self, performed_on: date, sets: Sequence[WorkoutSetInput]
    ) -> int:
        session = await self.services.records.create(performed_on, sets)
        return session.id


@asynccontextmanager
async def _harness(tmp_path: Path) -> AsyncIterator[HistoryHarness]:
    db = Database(tmp_path / "fit_agent.db")
    await db.open()
    await db.migrate()
    try:
        repositories = build_repositories(db)
        services = build_services(
            repositories, db, SqliteHealthProbe(db, db.path.parent)
        )
        yield HistoryHarness(repositories=repositories, services=services)
    finally:
        await db.close()


async def _call(harness: HistoryHarness, **args: Any) -> Any:
    content = await read_training_history.ainvoke(
        {"runtime": SimpleNamespace(context=harness.context()), **args}
    )
    return json.loads(content)


def _squat_set() -> WorkoutSetInput:
    return WorkoutSetInput(
        exercise_id=BARBELL_BACK_SQUAT,
        set_no=1,
        reps=5,
        set_type="work",
        load_convention="barbell_includes_bar_total",
        weight_kg=60.0,
    )


def _pull_up_set(*, set_type: SetType = "work") -> WorkoutSetInput:
    return WorkoutSetInput(
        exercise_id=PULL_UP, set_no=1, reps=8, set_type=set_type
    )


def _plank_set() -> WorkoutSetInput:
    return WorkoutSetInput(
        exercise_id=PLANK,
        set_no=1,
        reps=None,
        set_type="work",
        duration_seconds=60,
    )


async def test_defaults_and_declared_bounds(tmp_path: Path) -> None:
    """缺省 limit 为 20、exercise_ids 为空 tuple；limit 与 exercise_ids 的上下界由 Schema 表达。"""
    async with _harness(tmp_path) as harness:
        for offset in range(25):
            await harness.seed_workout(
                date(2026, 5, 1) + timedelta(days=offset), [_squat_set()]
            )

        payload = await _call(harness)

        assert len(payload["sessions"]) == 20
        TrainingHistoryPayload.model_validate(payload)

        assert (await _call(harness, limit=1))["sessions"] == payload["sessions"][:1]
        assert len((await _call(harness, limit=100))["sessions"]) == 25

        assert ReadTrainingHistoryArgs(runtime=None).model_dump() == {
            "runtime": None,
            "from_on": None,
            "to_on": None,
            "exercise_ids": (),
            "limit": 20,
        }


async def test_rejects_reversed_dates_and_overlong_exercise_ids() -> None:
    """日期逆序与超过 20 个 exercise_ids 由 Schema 拒绝，边界值本身合法。"""
    ReadTrainingHistoryArgs.model_validate(
        {"runtime": None, "from_on": "2026-05-01", "to_on": "2026-05-01"}
    )
    ReadTrainingHistoryArgs.model_validate(
        {"runtime": None, "exercise_ids": tuple(f"e{index}" for index in range(20))}
    )

    with pytest.raises(ValidationError):
        ReadTrainingHistoryArgs.model_validate(
            {"runtime": None, "from_on": "2026-05-10", "to_on": "2026-05-09"}
        )
    with pytest.raises(ValidationError):
        ReadTrainingHistoryArgs.model_validate(
            {
                "runtime": None,
                "exercise_ids": tuple(f"e{index}" for index in range(21)),
            }
        )
    for limit in (0, 101):
        with pytest.raises(ValidationError):
            ReadTrainingHistoryArgs.model_validate(
                {"runtime": None, "limit": limit}
            )


async def test_date_and_exercise_conditions_combine_with_and(tmp_path: Path) -> None:
    """日期闭区间与动作集合取 AND：任一单独成立，合取后进一步收窄。"""
    async with _harness(tmp_path) as harness:
        await harness.seed_workout(date(2026, 5, 1), [_squat_set()])
        await harness.seed_workout(date(2026, 5, 10), [_pull_up_set()])
        await harness.seed_workout(date(2026, 5, 20), [_squat_set()])

        window_only = await _call(harness, from_on="2026-05-05")
        assert [session["performed_on"] for session in window_only["sessions"]] == [
            "2026-05-20",
            "2026-05-10",
        ]

        exercise_only = await _call(harness, exercise_ids=(BARBELL_BACK_SQUAT,))
        assert [session["performed_on"] for session in exercise_only["sessions"]] == [
            "2026-05-20",
            "2026-05-01",
        ]

        both = await _call(
            harness, from_on="2026-05-05", exercise_ids=(BARBELL_BACK_SQUAT,)
        )
        assert [session["performed_on"] for session in both["sessions"]] == [
            "2026-05-20"
        ]

        upper = await _call(
            harness, to_on="2026-05-10", exercise_ids=(BARBELL_BACK_SQUAT,)
        )
        assert [session["performed_on"] for session in upper["sessions"]] == [
            "2026-05-01"
        ]


async def test_matching_one_exercise_returns_every_set_of_the_session(
    tmp_path: Path,
) -> None:
    """命中会话任一动作即返回该会话全部组；组按 exercise_id, set_no 稳定排序。"""
    async with _harness(tmp_path) as harness:
        squat_then_plank = await harness.seed_workout(
            date(2026, 5, 20), [_squat_set(), _plank_set()]
        )
        await harness.seed_workout(date(2026, 5, 1), [_squat_set()])

        payload = await _call(harness, exercise_ids=(PLANK,))

        assert [session["id"] for session in payload["sessions"]] == [squat_then_plank]
        sets = payload["sessions"][0]["sets"]
        assert [item["exercise_id"] for item in sets] == [
            BARBELL_BACK_SQUAT,
            PLANK,
        ]
        assert set(payload["sessions"][0]) == {
            "id",
            "performed_on",
            "plan_session_id",
            "sets",
        }
        assert set(sets[0]) == {
            "exercise_id",
            "set_no",
            "set_type",
            "load_convention",
            "weight_kg",
            "reps",
            "duration_seconds",
        }


async def test_orders_by_performed_on_then_id_descending(tmp_path: Path) -> None:
    """结果按 performed_on 降序、身份降序：同日后建立的训练排在前面。"""
    async with _harness(tmp_path) as harness:
        oldest = await harness.seed_workout(date(2026, 5, 20), [_squat_set()])
        first_same_day = await harness.seed_workout(date(2026, 5, 25), [_squat_set()])
        second_same_day = await harness.seed_workout(date(2026, 5, 25), [_plank_set()])

        payload = await _call(harness)

        assert [session["id"] for session in payload["sessions"]] == [
            second_same_day,
            first_same_day,
            oldest,
        ]


async def test_projects_assisted_set_type(tmp_path: Path) -> None:
    """set_type 契约覆盖领域三态：assisted 组完整投影，不被窄化为两类。"""
    async with _harness(tmp_path) as harness:
        await harness.seed_workout(date(2026, 5, 20), [_pull_up_set(set_type="assisted")])

        sets = (await _call(harness))["sessions"][0]["sets"]

        assert sets[0]["set_type"] == "assisted"
        assert sets[0]["load_convention"] is None
        assert sets[0]["weight_kg"] is None


async def test_no_match_returns_empty_sessions(tmp_path: Path) -> None:
    """无任何记录与条件无命中都返回空 sessions。"""
    async with _harness(tmp_path) as harness:
        assert (await _call(harness)) == {"sessions": []}

        await harness.seed_workout(date(2026, 5, 20), [_squat_set()])

        assert (await _call(harness, exercise_ids=(PULL_UP,))) == {"sessions": []}
        assert (await _call(harness, from_on="2026-06-01")) == {"sessions": []}


def test_runtime_is_hidden_from_the_model_visible_schema() -> None:
    """模型可见 Schema 只有四个查询参数；runtime、身份与 revision 都不出现。"""
    visible = cast(Any, read_training_history.tool_call_schema).model_json_schema()

    assert set(visible["properties"]) == {
        "from_on",
        "to_on",
        "exercise_ids",
        "limit",
    }
    assert read_training_history.args_schema is ReadTrainingHistoryArgs
    assert set(ReadTrainingHistoryArgs.model_json_schema()["properties"]) == {
        "runtime",
        "from_on",
        "to_on",
        "exercise_ids",
        "limit",
    }
    assert ReadTrainingHistoryArgs.model_json_schema()["additionalProperties"] is False
    injected = {field.name for field in fields(ToolExecutionContext)} | {"revision"}
    assert injected.isdisjoint(visible["properties"])

    with pytest.raises(ValidationError):
        ReadTrainingHistoryArgs.model_validate(
            {"runtime": None, "business_day": "2026-06-02"}
        )
