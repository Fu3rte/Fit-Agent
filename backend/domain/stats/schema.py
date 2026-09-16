"""stats 类型定义：有效工作组、三类 PB、趋势／摘要与月历事实（讨论总结 §3.2／§6.4／§7.1、
REFACTOR_PLAN §6.2／§6.4、stage2.md §6.1／§6.2／§7／§8）。

本模块只描述结构：「哪些组算有效」由 ``domain.stats.repo`` 的唯一共享 SQL 决定，
「PB 与趋势怎么算」由 ``domain.stats.service`` 的确定性函数决定。

三条硬边界：

- **只读现算**：PB 与趋势都不落表（不建 ``personal_bests`` 或其他统计结果表），来源是当前有效
  训练组与身体指标；修改或删除记录后重新查询即自然反映最新结果。
- **三类动作不混算**：``weight_kg``／``reps``／``duration_seconds`` 按记录口径只有有效的那一项
  （外加重量：重量与次数；纯自重：次数；计时：时长），其余为 ``None``，不虚构 0 值或口径。
- **日期来自事实**：``performed_on`` 是来源训练自己的发生日期，由 repo 从 ``workout_sessions``
  读出，统计层不取「今天」（REFACTOR_PLAN §5.5：禁止 ``date.today()``）；需要当天的接口
  （趋势窗口、停训天数）由调用方注入业务日期。

数据不足时状态显式区分：``no_data``（无记录）／``insufficient_data``（只有一条），不补 0、
不伪造变化值；趋势输出不评价进步、退步或停滞，也不含训练容量与计划完成率。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, cast

from domain.actions.schema import LoadConvention, RecordType

#: 三类 PB（讨论总结 §7.1；不建容量 PB，也没有估算 1RM）。
PersonalBestType = Literal["weight_pb", "reps_pb", "duration_pb"]
PERSONAL_BEST_TYPES: tuple[PersonalBestType, ...] = (
    "weight_pb",
    "reps_pb",
    "duration_pb",
)


class InvalidWorkSet(ValueError):
    """有效工作组缺少其记录口径要求的度量值：与 repo 的过滤口径不一致，大声失败不静默兜底。"""


@dataclass(frozen=True, slots=True)
class ValidWorkSet:
    """一个有效工作组：训练记录仍存在、``set_type='work'``、且字段与其记录口径匹配。

    是 ``domain.stats.repo`` 唯一共享 SQL 的读出结果，PB 与后续趋势复用同一份事实。
    动作名称来自目录（``exercises.standard_name_zh``），PB 结果直接带出，不二次查目录。
    """

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
        """把「有效工作组」查询的一行解码为组事实。

        取值域（记录口径、负重口径与重量同现同隐）已由库内 CHECK 与写入领域规则保证，
        有效性（哪一项度量该有值）由共享 SQL 的过滤条件决定；本函数只解码，不补默认值。
        """
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


#: 趋势与摘要的默认窗口：最近 30 天（讨论总结 §3.2、stage2.md §7）。
TREND_WINDOW_DAYS = 30

#: 数据是否足够给出变化值：``ok`` 有最近两条记录，``insufficient_data`` 只有一条，``no_data`` 无记录。
TrendStatus = Literal["ok", "no_data", "insufficient_data"]

#: 单次日程状态（stage2.md §8）：取消、未完成、已完成；完成由是否存在关联训练现算。
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
    """一个力量趋势系列：截至各日期的累计 PB，因此曲线按日期不下降。

    系列口径与 PB 分组一致（stage2.md §6.2）：``weight_pb`` 为动作＋负重口径的累计最大重量；
    ``reps_pb`` 只有纯自重动作的累计单组最大次数；``duration_pb`` 为计时动作的累计单组最长秒数。
    外加重量动作不生成次数 PB 曲线（讨论总结 §7.1）。累计包含窗口之前的全部历史，故 ``points``
    只覆盖窗口内有有效组的日期；口径修正后三类系列都无分组重量，``weight_kg`` 恒为 ``None``。
    """

    exercise_id: str
    exercise_name: str
    pb_type: PersonalBestType
    load_convention: LoadConvention | None
    weight_kg: float | None
    points: tuple[StrengthPoint, ...]


@dataclass(frozen=True, slots=True)
class MetricChange:
    """最近两条有效记录的变化；数据足够时给出当前值、前值、差值与两次日期。

    ``status`` 不是 ``ok`` 时全部取值字段为 ``None``：不补 0，也不把唯一一条记录当成变化。
    """

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

    不含训练容量、计划完成率，也不评价进步、退步或停滞；供看板与 Stage 3 MemoryAssembler 复用。
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
    """月历上的一条计划日程事实：落在 ``scheduled_on`` 当天，完成状态由关联训练现算。

    ``cancelled_at`` 非空即 ``cancelled``（已取消的日程不再占用名额）；否则有关联训练即
    ``complete``（``workout_session_id`` 与 ``actual_performed_on`` 标识实际训练，含跨月），
    其余为 ``incomplete``。
    """

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
    """一条现算 PB：数值 + 来源组事实（来源训练、组序号、``performed_on``）与适用的重量／口径。

    ``value`` 的单位随 ``pb_type``：``weight_pb`` 为 kg、``reps_pb`` 为次数、``duration_pb`` 为秒数。
    ``weight_kg`` 是该 PB 来源组的适用重量：``weight_pb`` 与 ``value`` 同值，只有纯自重动作才有
    ``reps_pb``（无重量），故 ``reps_pb`` 与 ``duration_pb`` 恒为 ``None``；``load_convention``
    同理只在外加重量动作上出现。
    """

    exercise_id: str
    exercise_name: str
    pb_type: PersonalBestType
    value: int | float
    load_convention: LoadConvention | None
    weight_kg: float | None
    workout_session_id: int
    set_no: int
    performed_on: date
