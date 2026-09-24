# 停训回归在计划节点上的接线：停训天数只从本轮 read_progress 事实取，达到阈值即抑制 increase。

from dataclasses import dataclass
from datetime import date
from typing import Any, cast

import pytest

from app.application.agent.contracts import AdjustmentContext, PlanLlmNodeDeps
from app.application.agent.harness.tools.common import ToolCallRecord
from app.application.agent.plan_nodes import (
    _progression_decisions,
    _return_period_gap_days,
)
from app.domain.actions.schema import Exercise
from app.domain.plans.rules import RETURN_PERIOD_GAP_DAYS
from app.domain.plans.schema import PlanDraft
from app.domain.stats.schema import ValidWorkSet

SQUAT = "barbell-back-squat"


def _exercise() -> Exercise:
    return Exercise(
        id=SQUAT,
        standard_name_zh="杠铃背蹲",
        aliases=(),
        equipment_variant="barbell",
        record_type="reps_weight",
        load_convention="barbell_includes_bar_total",
        min_load_increment_kg=2.5,
        recommendable=True,
        modes=("膝主导",),
        source_ref="catalog",
        attribution="catalog",
    )


def _active() -> PlanDraft:
    """active 目标处方：3 组 5–8 次、目标负荷 60kg。"""
    return PlanDraft.model_validate(
        {
            "goal": "增力",
            "starts_on": date(2026, 5, 25),
            "explanation": "计划",
            "weekly_frequency": 1,
            "training_days": [
                {
                    "scheduled_on": date(2026, 5, 25),
                    "exercises": [
                        {
                            "exercise_id": SQUAT,
                            "sets": 3,
                            "prescription": {
                                "type": "weighted_reps",
                                "reps_min": 5,
                                "reps_max": 8,
                                "load": {
                                    "status": "known",
                                    "weight_kg": 60.0,
                                    "source_workout_session_id": 1,
                                    "source_set_no": 1,
                                },
                            },
                        }
                    ],
                }
            ],
        }
    )


def _workout(session_id: int, performed_on: date) -> tuple[ValidWorkSet, ...]:
    """两次都在 60kg 上达到次数上限：没有回归期时渐进决策是 increase。"""
    return tuple(
        ValidWorkSet(
            exercise_id=SQUAT,
            exercise_name="杠铃背蹲",
            record_type="reps_weight",
            load_convention="barbell_includes_bar_total",
            weight_kg=60.0,
            reps=8,
            duration_seconds=None,
            workout_session_id=session_id,
            set_no=set_no,
            performed_on=performed_on,
        )
        for set_no in (1, 2, 3)
    )


@dataclass
class _Catalog:
    exercises: tuple[Exercise, ...]

    async def list_all(self) -> tuple[Exercise, ...]:
        return self.exercises


@dataclass
class _Stats:
    work_sets: tuple[ValidWorkSet, ...]

    async def list_valid_work_sets(self) -> tuple[ValidWorkSet, ...]:
        return self.work_sets


@dataclass
class _Deps:
    catalog: _Catalog
    stats: _Stats


def _facts(*, status: str, days: int | None) -> tuple[ToolCallRecord, ...]:
    """本轮 read_progress 的返回载荷：接线只读这一个字段。"""
    return (
        ToolCallRecord(
            tool_name="read_progress",
            payload={
                "days_since_last_workout": {
                    "status": status,
                    "days": days,
                    "last_performed_on": None,
                }
            },
        ),
    )


async def _decide(*, status: str, days: int | None) -> dict[str, Any]:
    deps = cast(
        "PlanLlmNodeDeps",
        _Deps(
            catalog=_Catalog((_exercise(),)),
            stats=_Stats(
                _workout(1, date(2026, 5, 4)) + _workout(2, date(2026, 5, 11))
            ),
        ),
    )
    decisions = await _progression_decisions(
        AdjustmentContext(
            plan_id=1, active_draft=_active(), linked_workout_session_ids=(1, 2)
        ),
        deps,
        facts=_facts(status=status, days=days),
    )

    assert len(decisions) == 1
    return cast("dict[str, Any]", decisions[0]["decision"])


def test_gap_days_comes_from_the_progress_fact() -> None:
    assert _return_period_gap_days(_facts(status="ok", days=21)) == 21
    assert _return_period_gap_days(_facts(status="no_data", days=None)) is None


def test_gap_days_rejects_a_status_that_disagrees_with_the_number() -> None:
    with pytest.raises(ValueError, match="停训天数与状态不一致"):
        _return_period_gap_days(_facts(status="ok", days=None))


async def test_return_period_suppresses_the_payload_decision() -> None:
    assert await _decide(status="ok", days=RETURN_PERIOD_GAP_DAYS) == {
        "action": "keep",
        "load_kg": 60.0,
    }
    assert await _decide(status="ok", days=RETURN_PERIOD_GAP_DAYS - 1) == {
        "action": "increase",
        "load_kg": 62.5,
    }
    assert await _decide(status="no_data", days=None) == {
        "action": "increase",
        "load_kg": 62.5,
    }
