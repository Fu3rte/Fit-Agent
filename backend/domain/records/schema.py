"""records 类型定义：一次训练（workout_sessions）与它的组（workout_sets）。

正本：``Fit-Agent-LangGraph-重构讨论总结.md`` §9（关键关系：``workout_sets.set_type`` 区分
work／warmup／assisted；``workout_sessions.plan_session_id`` 可空外键，NULL 表示额外训练；
同一日程最多完成一次）、``LANGGRAPH_REFACTOR_PLAN.md`` §5.4（组类型固定三态、RIR 不出现在
任何列或 DTO）；列与约束以 ``storage/migrations/001_initial.sql`` 的既有定义为准。

三条硬边界：

- **日期是日期对象**：``performed_on`` 用 ``datetime.date``，由调用方按业务时区算好后注入；
  repo 与领域服务不得自行取「今天」（REFACTOR_PLAN §5.5：禁止 ``date.today()``）。
- **组类型恰三态**：``work / warmup / assisted``；没有旧模型的「未申报保留为空」语义，
  也没有 RIR、辅助次数、时长或修订状态字段——它们不在这两张表的任何列里。
- **负重口径与重量同现同隐**：外加负重型两者齐备（重量可为 0kg），自重／计时型两者均为
  ``None``（库内 CHECK 同集合），不虚构 0kg。

``WorkoutSetInput`` 是写入侧的组事实：组身份（``id``）与所属训练（``workout_session_id``）由
落库分配，调用方只给事实本身；``WorkoutSet`` 是库内行，两者字段互不兼容。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, cast

from domain.actions.rules import LOAD_CONVENTIONS
from domain.actions.schema import LoadConvention

#: 组类型固定三态（讨论总结 §9、REFACTOR_PLAN §5.4；库内 CHECK 同集合）。
SetType = Literal["work", "warmup", "assisted"]
SET_TYPES: tuple[SetType, ...] = ("work", "warmup", "assisted")


class InvalidRecordRow(ValueError):
    """workout_sessions／workout_sets 行无法解析：数据损坏，大声失败不静默兜底。

    与 ``InvalidPlanRow``／``InvalidCatalogRow`` 同口径：读到的行与列约束不符（日期不是
    ISO 日期、组类型越界、重量与负重口径只有一半）即显式失败。
    """


@dataclass(frozen=True, slots=True)
class WorkoutSetInput:
    """一组待写入的事实：动作身份、动作内组序号、组类型、次数与（外加负重的）重量。

    不携带组身份与所属训练：``id``／``workout_session_id`` 由落库分配（见 :class:`WorkoutSet`）。
    ``set_type`` 必须由调用方明确给出，不给默认值——组类型没有「未申报」态。
    """

    exercise_id: str
    set_no: int
    reps: int
    set_type: SetType
    load_convention: LoadConvention | None = None
    weight_kg: float | None = None


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
    reps: int

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
                raise InvalidRecordRow(f"负重口径不在五种内：{text!r}")
            convention = cast(LoadConvention, text)
        raw_weight = row["weight_kg"]
        weight_kg = None if raw_weight is None else float(raw_weight)
        if (convention is None) != (weight_kg is None):
            raise InvalidRecordRow(
                f"负重口径与重量必须同现同隐：convention={convention!r} weight={weight_kg!r}"
            )
        return cls(
            id=int(row["id"]),
            workout_session_id=int(row["workout_session_id"]),
            exercise_id=str(row["exercise_id"]),
            set_no=int(row["set_no"]),
            set_type=cast(SetType, set_type),
            load_convention=convention,
            weight_kg=weight_kg,
            reps=int(row["reps"]),
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
        """把 workout_sessions 行与其组行（另一张表，已解析）合成一次训练。

        ``sets`` 由调用方按 ``workout_session_id`` 取好；日期无法按 ISO 自然日解析即
        :class:`InvalidRecordRow`。
        """
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
