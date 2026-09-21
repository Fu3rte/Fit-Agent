"""训练记录、身体指标与动作目录的请求体模型与响应 DTO。"""

from collections.abc import Sequence
from datetime import date
from typing import Any, cast

from pydantic import BaseModel, ConfigDict

from app.api.schemas.agent_dto import WorkoutSetBody
from app.domain.actions.schema import Exercise, LoadConvention
from app.domain.body_metrics.schema import BodyMetric
from app.domain.records.schema import SetType, WorkoutSession, WorkoutSetInput


class SetIn(BaseModel):
    """提交的一组训练事实；组序号由 API 按提交顺序分配。"""

    model_config = ConfigDict(extra="forbid")

    exercise_id: str
    set_type: str
    reps: int | None = None
    load_convention: str | None = None
    weight_kg: float | None = None
    duration_seconds: int | None = None


class RecordBody(BaseModel):
    """训练记录新增／整条覆盖的请求体；``plan_session_id`` 为 None 即额外训练。"""

    model_config = ConfigDict(extra="forbid")

    performed_on: date
    plan_session_id: int | None = None
    auto_link: bool = False
    sets: list[SetIn]


class BodyMetricBody(BaseModel):
    """身体指标请求体；``body_fat_pct`` 省略或 null 都表示该次未记录体脂。"""

    model_config = ConfigDict(extra="forbid")

    measured_on: date
    weight_kg: float
    body_fat_pct: float | None = None


def _workout_set_input(
    item: SetIn | WorkoutSetBody, *, set_no: int
) -> WorkoutSetInput:
    """请求体的一条组 → 组事实；取值域与必填／互斥字段由 ``domain.records`` 校验。"""
    return WorkoutSetInput(
        exercise_id=item.exercise_id,
        set_no=set_no,
        reps=item.reps,
        set_type=cast(SetType, item.set_type),
        load_convention=cast(LoadConvention | None, item.load_convention),
        weight_kg=item.weight_kg,
        duration_seconds=item.duration_seconds,
    )


def workout_set_inputs_from_dto(sets: Sequence[WorkoutSetBody]) -> tuple[WorkoutSetInput, ...]:
    """自然语言确认请求体的组列表 → 组事实；``set_no`` 原样采用提取结果。"""
    return tuple(_workout_set_input(item, set_no=item.set_no) for item in sets)


def workout_facts_from_dto(body: RecordBody) -> tuple[WorkoutSetInput, ...]:
    """训练记录请求体的组列表 → 组事实；``set_no`` 按提交顺序对同一动作依次分配。"""
    counters: dict[str, int] = {}
    facts: list[WorkoutSetInput] = []
    for item in body.sets:
        set_no = counters.get(item.exercise_id, 0) + 1
        counters[item.exercise_id] = set_no
        facts.append(_workout_set_input(item, set_no=set_no))
    return tuple(facts)


def record_dto(session: WorkoutSession) -> dict[str, Any]:
    """一次训练 → 传输对象：稳定身份 + 发生日期 + 关联日程（None 即额外训练）+ 全部组。"""
    return {
        "id": session.id,
        "performed_on": session.performed_on.isoformat(),
        "plan_session_id": session.plan_session_id,
        "sets": [
            {
                "exercise_id": fact.exercise_id,
                "set_no": fact.set_no,
                "set_type": fact.set_type,
                "load_convention": fact.load_convention,
                "weight_kg": fact.weight_kg,
                "reps": fact.reps,
                "duration_seconds": fact.duration_seconds,
            }
            for fact in session.sets
        ],
    }


def body_metric_dto(metric: BodyMetric) -> dict[str, Any]:
    """一条身体指标 → 传输对象；体脂未记录保持 null，不补 0。"""
    return {
        "id": metric.id,
        "measured_on": metric.measured_on.isoformat(),
        "weight_kg": metric.weight_kg,
        "body_fat_pct": metric.body_fat_pct,
    }


def exercise_dto(exercise: Exercise) -> dict[str, Any]:
    """动作目录一行 → 传输对象（负重口径与最小加重单位按目录原样给出）。"""
    return {
        "id": exercise.id,
        "standard_name_zh": exercise.standard_name_zh,
        "aliases": list(exercise.aliases),
        "equipment_variant": exercise.equipment_variant,
        "record_type": exercise.record_type,
        "load_convention": exercise.load_convention,
        "min_load_increment_kg": exercise.min_load_increment_kg,
        "recommendable": exercise.recommendable,
        "modes": list(exercise.modes),
        "source_ref": exercise.source_ref,
        "attribution": exercise.attribution,
    }
