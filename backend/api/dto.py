"""表单 API 传输层：请求体模型 ↔ 领域对象映射、领域对象 → 响应 DTO、统一错误形状。

响应侧同时是只读统计（PB、趋势、月历）的唯一边界映射：``personal_best_dto``／``trends_dto``／
``calendar_dto`` 也是 Subtask 05 前端契约的后端侧正本。

Stage 5 的 Agent 三端点（``api/routes_agent.py``）也在这里定义请求体与 confirm／reject 异常到
既有 JSON 错误形状的映射（stage5.md §4.4／§3.7）：``/api/agent/run`` 的流前校验错误与
``/confirm``／``/reject`` 的领域错误都复用同一形状，不在 Agent 层另造一套。

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
from collections.abc import Callable, Sequence
from datetime import date
from typing import Annotated, Any, cast
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


# ---------- 查询参数 ----------

#: 月历月份参数：严格的 ``YYYY-MM``（不接受 ``2026-6`` 这类非补零写法，也不接受多个月份集合）。
_MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"

MonthQuery = Annotated[str, Query(pattern=_MONTH_PATTERN)]


def decode_year_month(month: str) -> tuple[int, int]:
    """``YYYY-MM`` → ``(年, 月)``；形状已由 :data:`MonthQuery` 正则保证，这里只拒绝不存在的年份。

    年份 0 不是合法自然年，不靠数据库或领域层兜底（那会变成 500），在传输层直接归为 400。
    """
    year_text, _, month_text = month.partition("-")
    year = int(year_text)
    if year < 1:
        raise InvalidRequestShape(f"月份年份非法：{month}")
    return year, int(month_text)


# ---------- 请求体模型（Pydantic 只管形状、必填与基础类型） ----------


class SetIn(BaseModel):
    """提交的一组训练事实；组序号由 API 按提交顺序分配（05 不要求输入组序号）。

    ``reps``／``duration_seconds`` 都可省略：哪一项必填由目录动作的记录口径决定，
    本层不重复实现该规则（与值域、负重口径同口径：一律由 ``domain`` 拒绝）。
    """

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
    """``POST /api/agent/run`` 的请求体（stage5.md §3.7）：``conversation_id`` 按 UUID 校验。

    ``conversation_id`` 就是 Checkpointer 的 ``thread_id``（不另建映射）；``regenerate`` 缺省 false。
    形状、字段类型或 UUID 非法都在流建立**前**按既有 JSON 错误形状（400）拒绝，不进入 SSE。
    """

    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    request: str
    regenerate: bool = False


class AgentPlanBody(BaseModel):
    """``POST /api/agent/confirm``／``reject`` 的请求体（stage5.md §3.7）：会话身份 ＋ 目标计划身份。"""

    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    plan_id: int


class WorkoutSetBody(BaseModel):
    """自然语言打卡确认提交的一组训练事实；组序号由模型提取结果原样携带（stage6.md §2.2）。

    与表单 ``SetIn`` 的差别只有 ``set_no``：自然语言路径的提取结果已含组序号，用户修改后提交
    的是同一形状的完整载荷。取值范围、负重口径与必填／互斥字段一律由 ``domain.records`` 拒绝。
    """

    model_config = ConfigDict(extra="forbid")

    exercise_id: str
    set_no: int
    set_type: str
    reps: int | None = None
    load_convention: str | None = None
    weight_kg: float | None = None
    duration_seconds: int | None = None


class ConfirmWorkoutBody(BaseModel):
    """``POST /api/agent/confirm-workout`` 的请求体（stage6.md §2.4.2）：完整确认载荷。

    ``conversation_id`` 按 UUID 校验（与 Agent Run 同一约定）；本端点不要求服务端证明该 session
    此前完成过自然语言解析，所以它只做形状校验，不参与任何关联查询。``sets`` 是用户可修改后的完整值。
    """

    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    performed_on: date
    sets: list[WorkoutSetBody]
    plan_session_id: int | None = None
    auto_link: bool = False


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


def workout_set_inputs_from_dto(sets: Sequence[WorkoutSetBody]) -> tuple[WorkoutSetInput, ...]:
    """自然语言确认请求体的组列表 → 组事实；``set_no`` 原样采用（提取结果已给出）。

    组序号范围与同一动作内不重复、组类型、负重口径、重量、次数、时长与按记录口径的必填字段
    一律由 ``domain.records`` 校验，本层不猜、不补默认值；这也使确认路径与表单路径复用同一套
    领域规则（stage6.md §2.2）。
    """
    return tuple(
        WorkoutSetInput(
            exercise_id=item.exercise_id,
            set_no=item.set_no,
            reps=item.reps,
            set_type=cast(SetType, item.set_type),
            load_convention=cast(LoadConvention | None, item.load_convention),
            weight_kg=item.weight_kg,
            duration_seconds=item.duration_seconds,
        )
        for item in sets
    )


def workout_facts_from_dto(body: RecordBody) -> tuple[WorkoutSetInput, ...]:
    """训练记录请求体的组列表 → 组事实；``set_no`` 按提交顺序对同一动作依次分配 1,2,3…

    组的取值域（组类型、负重口径、重量、次数、时长、总组数与按记录口径的必填字段）一律由
    ``domain.records`` 校验，本层不猜、不补默认值。计时时长的不小于 1 秒也是同一条领域规则
    （``domain.records.rules.validate_duration_seconds``），本层不另写一遍。
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
                duration_seconds=item.duration_seconds,
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
    """趋势报告 → 传输对象：窗口、体重／体脂原始点、力量累计 PB 系列与趋势摘要。

    ``strength`` 由后端按截至各日期的累计 PB 算出（Stage 2 前端不展示该曲线）；
    所有点数、状态与差值都是后端事实，前端不得重算。
    """
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
    """月历 → 传输对象：只含有事实的日期；计划状态落在 ``scheduled_on``，训练落在 ``performed_on``。

    空白日期不出条目（不生成「休息日」文案）；计划条目的 ``workout_session_id`` 非空即已完成该日程，
    ``actual_performed_on`` 在跨日、跨月时仍给出真实训练日期；``workout.plan_session_id`` 为 null 即额外训练。
    """
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


# ---------- 异常 → 统一错误形状 ----------

#: 异常类型 → HTTP 状态码（``error_code`` 一律 ``invalid_request``）。请求体形状与 Pydantic
#: 校验错（未知字段、缺字段、非法类型、非 JSON）是 400；领域规则与值域是 422；身份不存在是
#: 404；关联日程不可用或候选不唯一、以及库内约束兜底是 409。未登记异常不映射（500）。
#:
#: 确认／拒绝（``api/routes_agent.py``）按 stage5.md §3.7 复用同一形状与状态码：计划不存在是 404；
#: ID 不匹配、状态冲突、日期过期与再校验失败都是 409（``PlanActivationError`` 一族，含
#: ``graph.nodes.ConfirmationConflict``；子类按 MRO 先命中更具体的登记项）。
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
    (PlanNotFound, 404),
    (PlanSessionLinkUnavailable, 409),
    (PlanSessionLinkAmbiguous, 409),
    (PlanActivationError, 409),
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
