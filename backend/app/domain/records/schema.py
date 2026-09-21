"""records 类型定义：一次训练（workout_sessions）与它的组（workout_sets）。"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, cast

from app.domain.actions.rules import LOAD_CONVENTIONS
from app.domain.actions.schema import LoadConvention

SetType = Literal["work", "warmup", "assisted"]
SET_TYPES: tuple[SetType, ...] = ("work", "warmup", "assisted")


class InvalidRecordRow(ValueError):
    """workout_sessions／workout_sets 行无法解析：数据损坏，大声失败不静默兜底。"""


@dataclass(frozen=True, slots=True)
class WorkoutSetInput:
    """一组待写入的事实：动作身份、动作内组序号、组类型、次数、持续秒数与（外加负重的）重量。"""

    exercise_id: str
    set_no: int
    reps: int | None
    set_type: SetType
    load_convention: LoadConvention | None = None
    weight_kg: float | None = None
    duration_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class WorkoutSet:
    """``workout_sets`` 的一行：``load_convention`` 与 ``weight_kg`` 同现同隐。"""

    id: int
    workout_session_id: int
    exercise_id: str
    set_no: int
    set_type: SetType
    load_convention: LoadConvention | None
    weight_kg: float | None
    reps: int | None
    duration_seconds: int | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "WorkoutSet":
        """把一行 workout_sets 映射为组事实；与列约束不符即 :class:`InvalidRecordRow`。"""
        set_type = str(row["set_type"])
        if set_type not in SET_TYPES:
            raise InvalidRecordRow(f"组类型不在三态内：{set_type!r}")
        raw_convention = row["load_convention"]
        convention: LoadConvention | None = None
        if raw_convention is not None:
            text = str(raw_convention)
            if text not in LOAD_CONVENTIONS:
                raise InvalidRecordRow(f"负重口径不在六种内：{text!r}")
            convention = cast(LoadConvention, text)
        raw_weight = row["weight_kg"]
        weight_kg = None if raw_weight is None else float(raw_weight)
        if (convention is None) != (weight_kg is None):
            raise InvalidRecordRow(
                f"负重口径与重量必须同现同隐：convention={convention!r} weight={weight_kg!r}"
            )
        raw_reps = row["reps"]
        raw_duration = row["duration_seconds"]
        return cls(
            id=int(row["id"]),
            workout_session_id=int(row["workout_session_id"]),
            exercise_id=str(row["exercise_id"]),
            set_no=int(row["set_no"]),
            set_type=cast(SetType, set_type),
            load_convention=convention,
            weight_kg=weight_kg,
            reps=None if raw_reps is None else int(raw_reps),
            duration_seconds=None if raw_duration is None else int(raw_duration),
        )


@dataclass(frozen=True, slots=True)
class WorkoutSession:
    """一次训练：发生日期 + 关联的计划日程（``None`` 即额外训练）+ 全部组事实。"""

    id: int
    performed_on: date
    plan_session_id: int | None
    sets: tuple[WorkoutSet, ...]

    @classmethod
    def from_row(
        cls, row: Mapping[str, Any], sets: Sequence[WorkoutSet]
    ) -> "WorkoutSession":
        """把 workout_sessions 行与其组行（另一张表，已解析）合成一次训练。"""
        raw_date = row["performed_on"]
        try:
            performed_on = date.fromisoformat(str(raw_date))
        except ValueError as exc:
            raise InvalidRecordRow(f"训练发生日期无法解析为自然日：{raw_date!r}") from exc
        plan_session_id = row["plan_session_id"]
        return cls(
            id=int(row["id"]),
            performed_on=performed_on,
            plan_session_id=None if plan_session_id is None else int(plan_session_id),
            sets=tuple(sets),
        )
