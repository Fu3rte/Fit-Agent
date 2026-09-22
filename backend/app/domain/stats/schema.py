"""stats 类型定义：有效工作组、三类 PB、趋势／摘要与月历事实。"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, cast

from app.domain.actions.schema import LoadConvention, RecordType

PersonalBestType = Literal["weight_pb", "reps_pb", "duration_pb"]
PERSONAL_BEST_TYPES: tuple[PersonalBestType, ...] = (
    "weight_pb",
    "reps_pb",
    "duration_pb",
)


class InvalidWorkSet(ValueError):
    """有效工作组缺少其记录口径要求的度量值。"""


@dataclass(frozen=True, slots=True)
class ValidWorkSet:
    """一个有效工作组：训练记录仍存在、``set_type='work'``、且字段与其记录口径匹配。"""

    exercise_id: str
    exercise_name: str
    record_type: RecordType
    load_convention: LoadConvention | None
    weight_kg: float | None
    reps: int | None
    duration_seconds: int | None
    workout_session_id: int
    set_no: int
    performed_on: date

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ValidWorkSet":
        """把「有效工作组」查询的一行解码为组事实。"""
        raw_convention = row["load_convention"]
        raw_weight = row["weight_kg"]
        raw_reps = row["reps"]
        raw_duration = row["duration_seconds"]
        return cls(
            exercise_id=str(row["exercise_id"]),
            exercise_name=str(row["standard_name_zh"]),
            record_type=cast(RecordType, str(row["record_type"])),
            load_convention=(
                None
                if raw_convention is None
                else cast(LoadConvention, str(raw_convention))
            ),
            weight_kg=None if raw_weight is None else float(raw_weight),
            reps=None if raw_reps is None else int(raw_reps),
            duration_seconds=None if raw_duration is None else int(raw_duration),
            workout_session_id=int(row["workout_session_id"]),
            set_no=int(row["set_no"]),
            performed_on=date.fromisoformat(str(row["performed_on"])),
        )


TREND_WINDOW_DAYS = 30

TrendStatus = Literal["ok", "no_data", "insufficient_data"]

CalendarSessionStatus = Literal["cancelled", "incomplete", "complete"]


@dataclass(frozen=True, slots=True)
class MetricPoint:
    """趋势折线上的一个原始点（体重 kg／体脂 %）：按发生日期取该日记录值，不插值、不补 0。"""

    measured_on: date
    value: float


@dataclass(frozen=True, slots=True)
class StrengthPoint:
    """力量趋势上的一点：``value`` 截至该日期该系列的累计 PB（历史最好成绩）。"""

    performed_on: date
    value: int | float


@dataclass(frozen=True, slots=True)
class StrengthTrend:
    """一个力量趋势系列：截至各日期的累计 PB，因此曲线按日期不下降。"""

    exercise_id: str
    exercise_name: str
    pb_type: PersonalBestType
    load_convention: LoadConvention | None
    weight_kg: float | None
    points: tuple[StrengthPoint, ...]


@dataclass(frozen=True, slots=True)
class MetricChange:
    """最近两条有效记录的变化。"""

    status: TrendStatus
    current: float | None
    current_on: date | None
    previous: float | None
    previous_on: date | None
    change: float | None


@dataclass(frozen=True, slots=True)
class WorkoutGap:
    """距上次训练天数：由注入的业务日期与最近 ``performed_on`` 复算，无训练历史即 ``no_data``。"""

    status: TrendStatus
    days: int | None
    last_performed_on: date | None


@dataclass(frozen=True, slots=True)
class TrendSummary:
    """确定性趋势摘要（讨论总结 §3.3／§6.4）：只含体重变化、体脂变化与停训天数。

    不含训练容量、计划完成率，也不评价进步、退步或停滞；供看板与只读进展工具复用。
    """

    weight_change: MetricChange
    body_fat_change: MetricChange
    days_since_last_workout: WorkoutGap


@dataclass(frozen=True, slots=True)
class TrendReport:
    """一次趋势查询的完整确定性结果：窗口、两类原始点、力量累计 PB 系列与趋势摘要。"""

    window_days: int
    from_on: date
    to_on: date
    weight: tuple[MetricPoint, ...]
    body_fat: tuple[MetricPoint, ...]
    strength: tuple[StrengthTrend, ...]
    trend_summary: TrendSummary


@dataclass(frozen=True, slots=True)
class WorkoutFact:
    """一次实际训练的最小事实：日期与关联日程（``None`` 即额外训练）。"""

    workout_session_id: int
    performed_on: date
    plan_session_id: int | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "WorkoutFact":
        raw_link = row["plan_session_id"]
        return cls(
            workout_session_id=int(row["id"]),
            performed_on=date.fromisoformat(str(row["performed_on"])),
            plan_session_id=None if raw_link is None else int(raw_link),
        )


@dataclass(frozen=True, slots=True)
class CalendarPlanSession:
    """月历上的一条计划日程事实：落在 ``scheduled_on`` 当天，完成状态由关联训练现算。"""

    plan_session_id: int
    scheduled_on: date
    status: CalendarSessionStatus
    workout_session_id: int | None
    actual_performed_on: date | None


@dataclass(frozen=True, slots=True)
class CalendarDay:
    """月历上的一天：只含当天真实发生的计划日程与训练事实（空白日期不生成休息日文案）。"""

    date: date
    plan_sessions: tuple[CalendarPlanSession, ...]
    workouts: tuple[WorkoutFact, ...]


@dataclass(frozen=True, slots=True)
class CalendarMonth:
    """一个自然月的月历事实：只读取当前 active 计划的日程，训练事实按 ``performed_on`` 落天。"""

    month: str
    from_on: date
    to_on: date
    days: tuple[CalendarDay, ...]


@dataclass(frozen=True, slots=True)
class PersonalBest:
    """一条现算 PB：数值 + 来源组事实与适用的重量／口径。"""

    exercise_id: str
    exercise_name: str
    pb_type: PersonalBestType
    value: int | float
    load_convention: LoadConvention | None
    weight_kg: float | None
    workout_session_id: int
    set_no: int
    performed_on: date
