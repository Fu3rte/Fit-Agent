"""表单 API 传输层：请求体模型 ↔ 领域对象映射、领域对象 → 响应 DTO、统一错误形状。

正本：``LANGGRAPH_REFACTOR_PLAN.md`` §4（目标代码结构）、§6.1（校验责任划分）与 Stage 1
子任务 04（表单 API）。三条硬边界：

- **只做传输**：不写 SQL、不创建 Agent Run 或通用草稿；表单写入直接调用领域 service。
- **校验责任分层**：Pydantic 模型只管 JSON 形状、必填字段与基础类型（一律 ``extra="forbid"``，
  未知字段即拒绝）；日期、数值范围、动作与负重口径匹配、组类型、日程关联规则由 ``domain``
  拒绝（422），本层不重复实现，也不把领域错误静默改写成别的形状（例如不把 ``known([])``
  改写成 ``denied``）。
- **错误形状统一且不泄漏**：响应体固定为
  ``{"http_status": int, "error_code": "invalid_request", "message": str}``；``message`` 只取
  异常文本，不含 SQL、文件路径或堆栈；未登记的异常不映射（保持 500 服务端故障，不伪装成
  客户端错误）。
"""

import sqlite3
from collections.abc import Callable
from datetime import date
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from domain.actions.rules import RecordLoadMismatch, UnknownExercise
from domain.actions.schema import Exercise, LoadConvention
from domain.body_metrics.rules import InvalidBodyMetric
from domain.body_metrics.schema import BodyMetric
from domain.body_metrics.service import BodyMetricNotFound
from domain.plans.schema import Plan, PlanSession
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

#: 统一错误码：本层只表达「请求或输入不合法」，不新增业务语义错误码。
ERROR_CODE_INVALID_REQUEST = "invalid_request"


class InvalidRequestShape(ValueError):
    """请求体不是接口约定的 JSON 形状（在触碰领域层之前即拒绝，400 ``invalid_request``）。"""


class UnknownResource(ValueError):
    """按身份读取的只读资源不存在（计划版本）：404 ``invalid_request``。

    明确未找到，不创建资源、不伪造空结果；不新增前端契约之外的 ``error_code``。
    训练记录与身体指标的「不存在」由领域服务抛出（:class:`WorkoutRecordNotFound`／
    :class:`BodyMetricNotFound`），计划侧只有只读服务，因此没有对应的领域异常。
    """


# ---------- 请求体模型（Pydantic 只管形状、必填与基础类型） ----------


class SetIn(BaseModel):
    """提交的一组训练事实；组序号由 API 按提交顺序分配（05 不要求输入组序号）。"""

    model_config = ConfigDict(extra="forbid")

    exercise_id: str
    set_type: str
    reps: int
    load_convention: str | None = None
    weight_kg: float | None = None


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


# ---------- 请求体 → 领域对象 ----------


def profile_from_dto(body: ProfileBody) -> Profile:
    """画像请求体 → :class:`Profile`（整份覆盖）。

    只做三态形状映射：``known`` 必须带值、``unknown``／``denied`` 不得带值。列表字段的
    ``known`` 空列表**原样传给领域层**，由 ``domain.profile.rules`` 拒绝（明确为空只能用
    ``denied`` 表达，本层不代它改写）。值是否符合字段类型与值域同样归领域校验。
    """
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


def workout_facts_from_dto(body: RecordBody) -> tuple[WorkoutSetInput, ...]:
    """训练记录请求体的组列表 → 组事实；``set_no`` 按提交顺序对同一动作依次分配 1,2,3…

    组的取值域（组类型、负重口径、重量、次数、总组数）一律由 ``domain.records.rules``
    校验，本层不猜、不补默认值。
    """
    counters: dict[str, int] = {}
    facts: list[WorkoutSetInput] = []
    for item in body.sets:
        set_no = counters.get(item.exercise_id, 0) + 1
        counters[item.exercise_id] = set_no
        facts.append(
            WorkoutSetInput(
                exercise_id=item.exercise_id,
                set_no=set_no,
                reps=item.reps,
                set_type=cast(SetType, item.set_type),
                load_convention=cast(LoadConvention | None, item.load_convention),
                weight_kg=item.weight_kg,
            )
        )
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


# ---------- 异常 → 统一错误形状 ----------

#: 异常类型 → HTTP 状态码（``error_code`` 一律 ``invalid_request``）。请求体形状与 Pydantic
#: 校验错（未知字段、缺字段、非法类型、非 JSON）是 400；领域规则与值域是 422；身份不存在是
#: 404；关联日程不可用或候选不唯一、以及库内约束兜底是 409。未登记异常不映射（500）。
_ERROR_STATUS: tuple[tuple[type[Exception], int], ...] = (
    (RequestValidationError, 400),
    (InvalidRequestShape, 400),
    (InvalidRecordFact, 422),
    (InvalidBodyMetric, 422),
    (InvalidProfile, 422),
    (UnknownExerciseReference, 422),
    (UnknownExercise, 422),
    (RecordLoadMismatch, 422),
    (WorkoutRecordNotFound, 404),
    (BodyMetricNotFound, 404),
    (UnknownResource, 404),
    (PlanSessionLinkUnavailable, 409),
    (PlanSessionLinkAmbiguous, 409),
    (sqlite3.IntegrityError, 409),
)


def _handler_for(
    status: int,
) -> Callable[[Request, Exception], JSONResponse]:
    def handler(request: Request, exc: Exception) -> JSONResponse:
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
