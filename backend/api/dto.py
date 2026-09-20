"""表单 API 传输层：请求体模型 ↔ 领域对象映射、领域对象 → 响应 DTO、统一错误形状。"""

import sqlite3
from collections.abc import Callable, Sequence
from datetime import date
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from domain.actions.rules import RecordLoadMismatch, UnknownExercise
from domain.actions.schema import Exercise, LoadConvention
from domain.body_metrics.rules import InvalidBodyMetric
from domain.body_metrics.schema import BodyMetric
from domain.body_metrics.service import BodyMetricNotFound
from domain.plans.schema import Plan, PlanSession
from domain.plans.service import PlanActivationError, PlanNotFound
from domain.profile.rules import InvalidProfile, UnknownExerciseReference
from domain.profile.schema import (
    FIELD_VALUE_KINDS,
    PROFILE_FIELDS,
    Fact,
    FactState,
    Profile,
)
from domain.records.rules import InvalidRecordFact
from domain.records.schema import SetType, WorkoutSession, WorkoutSetInput
from domain.records.service import (
    PlanSessionLinkAmbiguous,
    PlanSessionLinkUnavailable,
    WorkoutRecordNotFound,
)
from domain.stats.schema import (
    CalendarMonth,
    MetricChange,
    PersonalBest,
    TrendReport,
    WorkoutFact,
    WorkoutGap,
)
from provider_settings import ModelConfigurationError

ERROR_CODE_INVALID_REQUEST = "invalid_request"


class InvalidRequestShape(ValueError):
    """请求体不是接口约定的 JSON 形状。"""


class UnknownResource(ValueError):
    """按身份读取的只读资源不存在（计划版本）。"""


# ---------- 查询参数 ----------

#: 月历月份参数：严格的 ``YYYY-MM``（年份首位不得为 0）。
_MONTH_PATTERN = r"^[1-9][0-9]{3}-(0[1-9]|1[0-2])$"

MonthQuery = Annotated[str, Query(pattern=_MONTH_PATTERN)]


# ---------- 请求体模型（Pydantic 只管形状、必填与基础类型） ----------


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


class ProfileFactIn(BaseModel):
    """画像单字段的三态事实：``known`` 携带值，``unknown``／``denied`` 的值必须为 null。"""

    model_config = ConfigDict(extra="forbid")

    state: FactState
    value: Any = None


class ProfileBody(BaseModel):
    """画像整份覆盖请求体（PUT 语义）：七字段一次写入，无 context_version。"""

    model_config = ConfigDict(extra="forbid")

    training_goal: ProfileFactIn
    weekly_frequency: ProfileFactIn
    available_equipment: ProfileFactIn
    explicit_preferences: ProfileFactIn
    current_level: ProfileFactIn
    known_injuries: ProfileFactIn
    forbidden_exercise_ids: ProfileFactIn


class AgentRunBody(BaseModel):
    """``POST /api/agent/run`` 的请求体。"""

    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    request: str
    regenerate: bool = False


class AgentPlanBody(BaseModel):
    """``POST /api/agent/confirm``／``reject`` 的请求体：会话身份 ＋ 目标计划身份。"""

    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    plan_id: int


class WorkoutSetBody(BaseModel):
    """自然语言打卡确认提交的一组训练事实。"""

    model_config = ConfigDict(extra="forbid")

    exercise_id: str
    set_no: int
    set_type: str
    reps: int | None = None
    load_convention: str | None = None
    weight_kg: float | None = None
    duration_seconds: int | None = None


class ConfirmWorkoutBody(BaseModel):
    """``POST /api/agent/confirm-workout`` 的请求体：完整确认载荷。"""

    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    performed_on: date
    sets: list[WorkoutSetBody]
    plan_session_id: int | None = None
    auto_link: bool = False


class ProviderPutBody(BaseModel):
    """模型配置整份覆盖请求体（PUT ``/api/provider``）。"""

    model_config = ConfigDict(extra="forbid")

    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    api: Literal["openai_compatible", "anthropic_messages"] | None = None
    structured_output: Literal["json_schema", "function_calling_strict"] | None = None


# ---------- 请求体 → 领域对象 ----------


def profile_from_dto(body: ProfileBody) -> Profile:
    """画像请求体 → :class:`Profile`（整份覆盖）。"""
    return Profile(
        **{
            name: _fact_from_in(name, getattr(body, name))
            for name in PROFILE_FIELDS
        }
    )


def _fact_from_in(name: str, raw: ProfileFactIn) -> Fact[Any]:
    if raw.state != "known":
        if raw.value is not None:
            raise InvalidRequestShape(f"画像字段 {name} 在 {raw.state} 状态不得带值")
        return Fact(state=raw.state, value=None)
    if raw.value is None:
        raise InvalidRequestShape(f"画像字段 {name} 的 known 值不能为空")
    value = raw.value
    if FIELD_VALUE_KINDS[name] == "text_list":
        if not isinstance(value, list):
            raise InvalidRequestShape(f"画像字段 {name} 需要文本数组：{value!r}")
        return Fact.known(tuple(value))
    return Fact.known(value)


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


# ---------- 领域对象 → 响应 DTO ----------


def _fact_dto(fact: Fact[Any]) -> dict[str, Any]:
    """单个三态事实 → 传输对象：``unknown``／``denied`` 不带值，``known`` 带值。"""
    value = fact.value
    if isinstance(value, tuple):
        value = list(value)
    return {"state": fact.state, "value": value if fact.is_known else None}


def profile_facts_dto(profile: Profile) -> dict[str, Any]:
    """画像 → 七字段三态对象；未填写（unknown）与明确为空（denied）保持可分。"""
    return {name: _fact_dto(getattr(profile, name)) for name in PROFILE_FIELDS}


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


def plan_session_candidate_dto(session: PlanSession) -> dict[str, Any]:
    """当天可关联的计划日程候选（未取消且未被其他训练关联）。"""
    return {
        "id": session.id,
        "plan_id": session.plan_id,
        "scheduled_on": session.scheduled_on.isoformat(),
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
        "equipment_variant": exercise.equipment_variant,
        "record_type": exercise.record_type,
        "load_convention": exercise.load_convention,
        "min_load_increment_kg": exercise.min_load_increment_kg,
        "recommendable": exercise.recommendable,
        "modes": list(exercise.modes),
        "source_ref": exercise.source_ref,
        "attribution": exercise.attribution,
    }


def plan_dto(plan: Plan) -> dict[str, Any]:
    """一个计划版本 → 传输对象：行字段 + 结构化内容（已解码的合法 JSON）。"""
    return {
        "id": plan.id,
        "version": plan.version,
        "status": plan.status,
        "source_plan_id": plan.source_plan_id,
        "structured_content": plan.structured_content,
        "evaluator_result": plan.evaluator_result,
        "created_at": plan.created_at,
        "confirmed_at": plan.confirmed_at,
        "archived_at": plan.archived_at,
    }


def plan_session_dto(session: PlanSession) -> dict[str, Any]:
    """一条计划日程 → 传输对象；``cancelled_at`` 非空表示该日程已取消。"""
    return {
        "id": session.id,
        "plan_id": session.plan_id,
        "scheduled_on": session.scheduled_on.isoformat(),
        "cancelled_at": session.cancelled_at,
    }


def personal_best_dto(pb: PersonalBest) -> dict[str, Any]:
    """一条现算 PB → 传输对象：数值、适用负重口径与重量，加来源训练／组序号／日期。"""
    return {
        "exercise_id": pb.exercise_id,
        "exercise_name": pb.exercise_name,
        "pb_type": pb.pb_type,
        "value": pb.value,
        "load_convention": pb.load_convention,
        "weight_kg": pb.weight_kg,
        "workout_session_id": pb.workout_session_id,
        "set_no": pb.set_no,
        "performed_on": pb.performed_on.isoformat(),
    }


def metric_change_dto(change: MetricChange) -> dict[str, Any]:
    """体重／体脂最近两条记录的变化；状态不是 ``ok`` 时取值字段全为 null（不补 0）。"""
    return {
        "status": change.status,
        "current": change.current,
        "current_on": _optional_iso(change.current_on),
        "previous": change.previous,
        "previous_on": _optional_iso(change.previous_on),
        "change": change.change,
    }


def workout_gap_dto(gap: WorkoutGap) -> dict[str, Any]:
    """距上次训练天数；没有训练历史时状态为 ``no_data`` 且天数为 null。"""
    return {
        "status": gap.status,
        "days": gap.days,
        "last_performed_on": _optional_iso(gap.last_performed_on),
    }


def trends_dto(report: TrendReport) -> dict[str, Any]:
    """趋势报告 → 传输对象。"""
    return {
        "window_days": report.window_days,
        "from": report.from_on.isoformat(),
        "to": report.to_on.isoformat(),
        "weight": [
            {"measured_on": point.measured_on.isoformat(), "value": point.value}
            for point in report.weight
        ],
        "body_fat": [
            {"measured_on": point.measured_on.isoformat(), "value": point.value}
            for point in report.body_fat
        ],
        "strength": [
            {
                "exercise_id": trend.exercise_id,
                "exercise_name": trend.exercise_name,
                "pb_type": trend.pb_type,
                "load_convention": trend.load_convention,
                "weight_kg": trend.weight_kg,
                "points": [
                    {
                        "performed_on": point.performed_on.isoformat(),
                        "value": point.value,
                    }
                    for point in trend.points
                ],
            }
            for trend in report.strength
        ],
        "trend_summary": {
            "weight_change": metric_change_dto(report.trend_summary.weight_change),
            "body_fat_change": metric_change_dto(report.trend_summary.body_fat_change),
            "days_since_last_workout": workout_gap_dto(
                report.trend_summary.days_since_last_workout
            ),
        },
    }


def calendar_dto(month: CalendarMonth) -> dict[str, Any]:
    """月历 → 传输对象：只含有事实的日期。"""
    return {
        "month": month.month,
        "from": month.from_on.isoformat(),
        "to": month.to_on.isoformat(),
        "days": [
            {
                "date": day.date.isoformat(),
                "plan_sessions": [
                    {
                        "id": session.plan_session_id,
                        "scheduled_on": session.scheduled_on.isoformat(),
                        "status": session.status,
                        "workout_session_id": session.workout_session_id,
                        "actual_performed_on": _optional_iso(
                            session.actual_performed_on
                        ),
                    }
                    for session in day.plan_sessions
                ],
                "workouts": [workout_fact_dto(fact) for fact in day.workouts],
            }
            for day in month.days
        ],
    }


def workout_fact_dto(fact: WorkoutFact) -> dict[str, Any]:
    """一次实际训练事实 → 传输对象；``plan_session_id`` 为 null 即额外训练。"""
    return {
        "id": fact.workout_session_id,
        "performed_on": fact.performed_on.isoformat(),
        "plan_session_id": fact.plan_session_id,
    }


def _optional_iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


_ERROR_STATUS: tuple[tuple[type[Exception], int], ...] = (
    (RequestValidationError, 400),
    (InvalidRequestShape, 400),
    (ModelConfigurationError, 400),
    (InvalidRecordFact, 422),
    (InvalidBodyMetric, 422),
    (InvalidProfile, 422),
    (UnknownExerciseReference, 422),
    (UnknownExercise, 422),
    (RecordLoadMismatch, 422),
    (WorkoutRecordNotFound, 404),
    (BodyMetricNotFound, 404),
    (UnknownResource, 404),
    (PlanNotFound, 404),
    (PlanSessionLinkUnavailable, 409),
    (PlanSessionLinkAmbiguous, 409),
    (PlanActivationError, 409),
    (sqlite3.IntegrityError, 409),
)


def _handler_for(
    status: int,
) -> Callable[[Request, Exception], JSONResponse]:
    def handler(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            content={
                "http_status": status,
                "error_code": ERROR_CODE_INVALID_REQUEST,
                "message": str(exc),
            },
        )

    return handler


def install_error_handlers(app: FastAPI) -> None:
    """按 :data:`_ERROR_STATUS` 注册异常处理器：全部表单端点共享同一错误形状。"""
    for exc_type, status in _ERROR_STATUS:
        app.add_exception_handler(exc_type, _handler_for(status))
