"""plans 类型定义：计划版本行与计划日程行，以及统一计划 Schema 与 Evaluator 结果 Schema。"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

from domain.profile.rules import WEEKLY_FREQUENCY_MAX, WEEKLY_FREQUENCY_MIN
from domain.records.rules import (
    DURATION_SECONDS_MIN,
    PRECISION_EPSILON,
    REPS_MAX,
    REPS_MIN,
    WEIGHT_KG_DECIMALS,
    WEIGHT_KG_MAX,
    WEIGHT_KG_MIN,
)

PlanStatus = Literal["draft", "active", "archived", "rejected"]
PLAN_STATUSES: tuple[PlanStatus, ...] = ("draft", "active", "archived", "rejected")

PLAN_WINDOW_DAYS = 7


class InvalidPlanRow(ValueError):
    """plans／plan_sessions 行或计划内容 JSON 无法解析。"""


# ---------- 统一计划 Schema（stage4.md §3.1） ----------


def _reject_blank_text(value: str) -> str:
    """非空文本：去空白后不得为空；不改写原值。"""
    if not value.strip():
        raise ValueError("必须是非空文本（去空白后不得为空）")
    return value


NonEmptyText = Annotated[str, AfterValidator(_reject_blank_text)]


def _as_business_date(value: object) -> object:
    """自然日：接受日期对象或 ISO 日期文本。"""
    if isinstance(value, datetime):
        raise ValueError(f"计划日期必须是自然日，不是时刻：{value!r}")
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"计划日期必须是 ISO 日期文本：{value!r}") from exc
    return value


BusinessDate = Annotated[date, BeforeValidator(_as_business_date)]


class PlanSchemaModel(BaseModel):
    """计划与 Evaluator 模型的共同底线：未声明字段一律拒绝，标量字段不做隐式转换。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class NeedsCalibration(PlanSchemaModel):
    """没有有效历史时的负荷：只标记待校准，不带具体重量或来源（禁止模型猜重量）。"""

    status: Literal["needs_calibration"]


class KnownLoad(PlanSchemaModel):
    """有历史时的具体负荷：重量与来源同现，来源必须是该动作最近一次有效工作组。"""

    status: Literal["known"]
    weight_kg: float = Field(ge=WEIGHT_KG_MIN, le=WEIGHT_KG_MAX)
    source_workout_session_id: int
    source_set_no: int

    @model_validator(mode="after")
    def _require_recorded_precision(self) -> "KnownLoad":
        if (
            abs(self.weight_kg - round(self.weight_kg, WEIGHT_KG_DECIMALS))
            >= PRECISION_EPSILON
        ):
            raise ValueError(f"负荷重量最多一位小数：{self.weight_kg!r}")
        return self


Load = Annotated[KnownLoad | NeedsCalibration, Field(discriminator="status")]


class RepsPrescription(PlanSchemaModel):
    """次数处方共同结构：次数区间与可选渐进说明。"""

    reps_min: int = Field(ge=REPS_MIN, le=REPS_MAX)
    reps_max: int = Field(ge=REPS_MIN, le=REPS_MAX)
    progression_note: str | None = None

    @model_validator(mode="after")
    def _require_ordered_reps_range(self) -> "RepsPrescription":
        if self.reps_min > self.reps_max:
            raise ValueError(f"次数区间必须 min <= max：{self.reps_min}–{self.reps_max}")
        return self


class WeightedRepsPrescription(RepsPrescription):
    """外加负重的次数处方：与目录 ``record_type='reps_weight'`` 一一对应。"""

    type: Literal["weighted_reps"]
    load: Load


class BodyweightRepsPrescription(RepsPrescription):
    """纯自重的次数处方：与目录 ``record_type='reps_bodyweight'`` 一一对应；不得携带重量或负荷来源。"""

    type: Literal["bodyweight_reps"]


class TimedPrescription(PlanSchemaModel):
    """计时处方：时长区间（秒），下限至少 1 秒、**无业务上限**；不得携带次数或负荷。"""

    type: Literal["timed"]
    duration_seconds_min: int = Field(ge=DURATION_SECONDS_MIN)
    duration_seconds_max: int = Field(ge=DURATION_SECONDS_MIN)
    progression_note: str | None = None

    @model_validator(mode="after")
    def _require_ordered_duration_range(self) -> "TimedPrescription":
        if self.duration_seconds_min > self.duration_seconds_max:
            raise ValueError(
                "计时区间必须 min <= max："
                f"{self.duration_seconds_min}–{self.duration_seconds_max}"
            )
        return self


Prescription = Annotated[
    WeightedRepsPrescription | BodyweightRepsPrescription | TimedPrescription,
    Field(discriminator="type"),
]


class PlannedExercise(PlanSchemaModel):
    """计划里的一个动作：稳定身份、组数与一个处方（处方的互斥字段由判别联合保证）。"""

    exercise_id: NonEmptyText
    sets: int = Field(gt=0)
    prescription: Prescription


class TrainingDay(PlanSchemaModel):
    """一个训练日：一个计划窗口内的日期，至少一个动作，且同一动作不得重复。"""

    scheduled_on: BusinessDate
    exercises: tuple[PlannedExercise, ...] = Field(min_length=1, strict=False)

    @model_validator(mode="after")
    def _require_unique_exercises(self) -> "TrainingDay":
        ids = [exercise.exercise_id for exercise in self.exercises]
        duplicated = sorted({exercise_id for exercise_id in ids if ids.count(exercise_id) > 1})
        if duplicated:
            raise ValueError(f"同一训练日不得重复同一动作：{duplicated}")
        return self


class PlanDraft(PlanSchemaModel):
    """统一计划草案：顶层目标、开始日期、解释、每周训练次数与具体日期的训练日。"""

    goal: NonEmptyText
    starts_on: BusinessDate
    explanation: NonEmptyText
    weekly_frequency: int = Field(ge=WEEKLY_FREQUENCY_MIN, le=WEEKLY_FREQUENCY_MAX)
    training_days: tuple[TrainingDay, ...] = Field(strict=False)

    @model_validator(mode="after")
    def _require_seven_day_window(self) -> "PlanDraft":
        if len(self.training_days) != self.weekly_frequency:
            raise ValueError(
                f"训练日数量必须等于每周训练次数：{len(self.training_days)} != {self.weekly_frequency}"
            )
        window_end = self.starts_on + timedelta(days=PLAN_WINDOW_DAYS - 1)
        seen: set[date] = set()
        for day in self.training_days:
            if day.scheduled_on in seen:
                raise ValueError(f"训练日期重复：{day.scheduled_on.isoformat()}")
            seen.add(day.scheduled_on)
            if not self.starts_on <= day.scheduled_on <= window_end:
                raise ValueError(
                    f"训练日期必须落在 {self.starts_on.isoformat()}–{window_end.isoformat()}："
                    f"{day.scheduled_on.isoformat()}"
                )
        return self


# ---------- Evaluator 结果 Schema ----------


class RuleFailure(PlanSchemaModel):
    """确定性层的一条失败：``code`` 是规则标识，``exercise_id`` 只在按动作检查时出现。"""

    code: str
    message: str
    exercise_id: str | None = None


class DeterministicResult(PlanSchemaModel):
    """确定性领域校验的结果：失败项全量列出（不提前中断，供一次修订回传）。"""

    passed: bool
    failures: tuple[RuleFailure, ...] = Field(strict=False)


class RubricVerdict(PlanSchemaModel):
    """一个模型 Rubric 维度的布尔判定与理由；不使用数值评分或权重。"""

    passed: bool
    reason: str


class RubricResult(PlanSchemaModel):
    """三个模型 Rubric 维度：目标匹配与安排合理性是硬门槛，解释质量只进 warning。"""

    goal_alignment: RubricVerdict
    schedule_reasonableness: RubricVerdict
    explanation_quality: RubricVerdict


class EvaluationResult(PlanSchemaModel):
    """Evaluator 的分层结构化结果。"""

    passed: bool
    deterministic: DeterministicResult
    rubric: RubricResult
    blocking_failures: tuple[str, ...] = Field(strict=False)
    warnings: tuple[str, ...] = Field(strict=False)
    revision_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _require_passed_matches_hard_gates(self) -> "EvaluationResult":
        expected = (
            self.deterministic.passed
            and self.rubric.goal_alignment.passed
            and self.rubric.schedule_reasonableness.passed
        )
        if self.passed != expected:
            raise ValueError(
                "passed 必须等于「确定性层通过且两个硬门槛通过」的合取："
                f"passed={self.passed} 期望={expected}"
            )
        return self


# ---------- 计划内容与 Evaluator 结果的 JSON 编解码 ----------


def plan_draft_to_json(draft: PlanDraft) -> str:
    """统一计划草案 → ``plans.structured_content`` 文本（日期按 ISO 文本写出）。"""
    return json.dumps(draft.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)


def evaluation_result_to_json(result: EvaluationResult) -> str:
    """Evaluator 结果 → ``plans.evaluator_result`` 文本。"""
    return json.dumps(result.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)


# ---------- 计划版本行与计划日程行 ----------


def _json_value(label: str, raw: object) -> Any:
    """JSON 列 → 通用对象；只解码形状，不解释计划语义。"""
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError) as exc:
        raise InvalidPlanRow(f"{label} JSON 列损坏：{raw!r}") from exc


def _optional_text(raw: object) -> str | None:
    return None if raw is None else str(raw)


@dataclass(frozen=True, slots=True)
class Plan:
    """一条计划版本行（含历史版本）；状态决定它是 active／draft／archived／rejected。"""

    id: int
    version: int
    status: PlanStatus
    source_plan_id: int | None
    structured_content: Any
    evaluator_result: Any | None
    created_at: str
    confirmed_at: str | None
    archived_at: str | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Plan":
        status = str(row["status"])
        if status not in PLAN_STATUSES:
            raise InvalidPlanRow(f"计划状态非法：{status!r}")
        raw_evaluator = row["evaluator_result"]
        source_plan_id = row["source_plan_id"]
        return cls(
            id=int(row["id"]),
            version=int(row["version"]),
            status=cast(PlanStatus, status),
            source_plan_id=None if source_plan_id is None else int(source_plan_id),
            structured_content=_json_value("structured_content", row["structured_content"]),
            evaluator_result=(
                None
                if raw_evaluator is None
                else _json_value("evaluator_result", raw_evaluator)
            ),
            created_at=str(row["created_at"]),
            confirmed_at=_optional_text(row["confirmed_at"]),
            archived_at=_optional_text(row["archived_at"]),
        )


@dataclass(frozen=True, slots=True)
class PlanSession:
    """一条计划日程；``cancelled_at`` 非空表示该日程已取消（行不物理删除，历史保留）。"""

    id: int
    plan_id: int
    scheduled_on: date
    cancelled_at: str | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "PlanSession":
        return cls(
            id=int(row["id"]),
            plan_id=int(row["plan_id"]),
            scheduled_on=date.fromisoformat(str(row["scheduled_on"])),
            cancelled_at=_optional_text(row["cancelled_at"]),
        )
