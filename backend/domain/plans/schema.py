"""plans 类型定义：计划版本行与计划日程行（讨论总结 §9、REFACTOR_PLAN §5.5；Stage 1 子任务 02 §8），
以及 Stage 4 的统一计划 Schema 与 Evaluator 结果 Schema（stage4.md §3.1／§3.5；Subtask 02）。

与 ``plans``／``plan_sessions`` 表列一一对应。五条硬边界：

- **只读行结构**：本模块只有读取结构；草稿写入、确认与激活结构是 Subtask 03／Stage 5 的事。
- **计划内容的形状在这里冻结**：``plans.structured_content`` 是 :class:`PlanDraft`、
  ``plans.evaluator_result`` 是 :class:`EvaluationResult`；编解码只在本模块
  （``plan_draft_to_json``／``plan_draft_from_json``／``evaluation_result_to_json``／
  ``evaluation_result_from_json``）。``Plan.from_row`` 仍按「合法 JSON」解码（Stage 1–3 的既有行
  形状不受影响），需要计划语义的读取路径必须显式过 ``PlanDraft.model_validate``／
  ``plan_draft_from_json``。解码失败即数据损坏，大声失败不静默兜底。
- **未声明字段一律拒绝**：计划与 Evaluator 模型一律 ``extra="forbid"`` 且 strict（类型不做隐式
  转换）。不保留模型自由扩展字段，也没有 RIR、估算 1RM、训练容量、完成率，或任何计划状态时间字段。
- **状态四态**：``draft → active → archived`` 与 ``draft 候选 → rejected``（讨论总结 §3.3／§3.4）；
  ``rejected`` 是终态，不带时间字段（库内 CHECK 同集合，见 ``003_rejected_plan_status.sql``）。
- **可追溯与单调**：``source_plan_id`` 指向生成该版本所基于的旧计划（首个计划为 None），
  ``version`` 只追加、恰好 +1（由 Stage 5 的确认事务保证，库内 UNIQUE 兜底）。

值域一律复用已合入规则的唯一出处，不另建口径：单组次数 1–100
（``domain.records.rules.REPS_MIN``／``REPS_MAX``）、计时下限 1 秒且**无业务上限**
（``DURATION_SECONDS_MIN``）、重量 0–1000kg 且最多一位小数（``WEIGHT_KG_MIN``／``WEIGHT_KG_MAX``／
``WEIGHT_KG_DECIMALS``／``PRECISION_EPSILON``）、每周训练次数 1–7
（``domain.profile.rules.WEEKLY_FREQUENCY_MIN``／``WEEKLY_FREQUENCY_MAX``）。

纯模块：不读库、不碰 IO、不取「今天」、不依赖 FastAPI／LangGraph／模型 SDK（``domain/__init__`` 约束）。
"""

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

#: 计划状态（讨论总结 §3.3／§3.4：``draft → active → archived`` 与 ``draft 候选 → rejected``；库内 CHECK 同集合）。
PlanStatus = Literal["draft", "active", "archived", "rejected"]
PLAN_STATUSES: tuple[PlanStatus, ...] = ("draft", "active", "archived", "rejected")

#: 计划周期：从 ``starts_on`` 起连续 7 天（讨论总结 §3.3、stage4.md §3.1 决策 9A）。
PLAN_WINDOW_DAYS = 7


class InvalidPlanRow(ValueError):
    """plans／plan_sessions 行或计划内容 JSON 无法解析（JSON 损坏、状态越界或形状不符）：数据损坏，不静默吞掉。"""


# ---------- 统一计划 Schema（stage4.md §3.1） ----------


def _reject_blank_text(value: str) -> str:
    """非空文本：去空白后不得为空；不改写原值（与 ``domain.records.rules.validate_exercise_id`` 同口径）。"""
    if not value.strip():
        raise ValueError("必须是非空文本（去空白后不得为空）")
    return value


#: 非空文本字段（``goal``／``explanation``／``exercise_id``）。
NonEmptyText = Annotated[str, AfterValidator(_reject_blank_text)]


def _as_business_date(value: object) -> object:
    """自然日：接受日期对象或 ISO 日期文本（JSON 载荷与直接构造都走同一条路）。

    ``datetime`` 是 ``date`` 的子类但表示绝对时刻，按业务时区解释成自然日不在本层猜测，一律拒绝
    （与 ``domain.records.rules.validate_performed_on`` 同一口径）。
    """
    if isinstance(value, datetime):
        raise ValueError(f"计划日期必须是自然日，不是时刻：{value!r}")
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"计划日期必须是 ISO 日期文本：{value!r}") from exc
    return value


#: 计划日期字段（``starts_on``／``scheduled_on``）：strict 模式下的自然日。
BusinessDate = Annotated[date, BeforeValidator(_as_business_date)]


class PlanSchemaModel(BaseModel):
    """计划与 Evaluator 模型的共同底线：未声明字段一律拒绝，标量字段不做隐式转换。

    集合字段（``training_days``／``exercises``／``failures``⋯⋯）额外接受 JSON 数组并在模型内统一为
    元组：读库后 ``Plan.structured_content`` 已是解码后的对象（数组即列表），读取路径必须能直接用
    ``model_validate`` 校验它。
    """

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


#: 负荷判别联合（stage4.md §3.1）：``status`` 是判别键。
Load = Annotated[KnownLoad | NeedsCalibration, Field(discriminator="status")]


class RepsPrescription(PlanSchemaModel):
    """次数处方共同结构：次数区间（1–100，``min <= max``）与可选渐进说明。

    三类处方的字段严格互斥：本基类不带负荷字段，只有外加负重次数处方才携带 ``load``。
    """

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


#: 三类处方判别联合（stage4.md §3.1）：``type`` 是判别键，且必须与目录动作的记录口径一致
#: （一致性由 ``domain.plans.rules.validate_plan_draft`` 按目录复验）。
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
    """一个训练日：一个计划窗口内的日期，至少一个动作，且同一动作不得重复。

    ``exercises`` 接受 JSON 数组与 Python 列表（模型内统一为元组）：读库后 ``Plan.structured_content``
    已是解码后的对象，读取路径必须能直接用 ``model_validate`` 校验它。
    """

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
    """统一计划草案：顶层目标、开始日期、解释、每周训练次数与具体日期的训练日。

    ``starts_on`` 是七天窗口的第一天；``training_days`` 数量必须恰好等于 ``weekly_frequency``，
    每个 ``scheduled_on`` 唯一且落在窗口内。``source_plan_id`` 不属于计划内容（只存
    ``plans.source_plan_id``，首次生成为 NULL）。
    """

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


# ---------- Evaluator 结果 Schema（stage4.md §3.5） ----------


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
    """Evaluator 的分层结构化结果（stage4.md §3.5）。

    ``passed`` 不是自由字段：它必须恰好等于确定性层通过 **且** 两个硬门槛（目标匹配、安排合理性）
    通过；解释质量失败只出现在 ``warnings``，不改变 ``passed``。
    """

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
# 与 ``domain.profile.schema`` 的画像编解码同一套口径（``ensure_ascii=False``、``sort_keys=True``）。


def plan_draft_to_json(draft: PlanDraft) -> str:
    """统一计划草案 → ``plans.structured_content`` 文本（日期按 ISO 文本写出）。"""
    return json.dumps(draft.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)


def plan_draft_from_json(raw: str) -> PlanDraft:
    """``plans.structured_content`` 文本 → 统一计划草案；形状不符即数据损坏。"""
    try:
        return PlanDraft.model_validate_json(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidPlanRow(f"structured_content 不是合法计划内容：{exc}") from exc


def evaluation_result_to_json(result: EvaluationResult) -> str:
    """Evaluator 结果 → ``plans.evaluator_result`` 文本。"""
    return json.dumps(result.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)


def evaluation_result_from_json(raw: str) -> EvaluationResult:
    """``plans.evaluator_result`` 文本 → Evaluator 结果；形状不符即数据损坏。"""
    try:
        return EvaluationResult.model_validate_json(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidPlanRow(f"evaluator_result 不是合法评估结果：{exc}") from exc


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
