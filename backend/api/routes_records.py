"""训练记录、身体指标与动作目录表单路由（Stage 1 子任务 04 §9；plan §4 未列单独路由文件）。

传输边界：只调用领域 service（``WorkoutRecordsService``／``BodyMetricsService``／
``ActionCatalogService``），不做领域规则、不写 SQL、不创建 Agent Run 或草稿；请求体形状与错误
映射见 :mod:`api.dto`。业务日期由 :func:`api.deps.current_business_date` 按固定业务时区注入，
不接受客户端传入。

路由顺序：``/api/records/plan-session-candidates`` 必须声明在 ``/api/records/{record_id}`` 之前，
否则会被路径参数吞掉。
"""

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Request

from api.deps import current_business_date
from api.dto import (
    BodyMetricBody,
    RecordBody,
    body_metric_dto,
    exercise_dto,
    plan_session_candidate_dto,
    record_dto,
    workout_facts_from_dto,
)
from domain.actions.service import ActionCatalogService
from domain.body_metrics.service import BodyMetricsService
from domain.records.service import WorkoutRecordNotFound, WorkoutRecordsService

router = APIRouter()


# ---------- 训练记录 ----------


@router.get("/api/records")
async def list_records(request: Request) -> dict[str, Any]:
    """全部训练及其组事实（按发生日期排序）。"""
    sessions = await WorkoutRecordsService(request.app.state.db).list_all()
    return {"records": [record_dto(session) for session in sessions]}


@router.post("/api/records")
async def create_record(body: RecordBody, request: Request) -> dict[str, Any]:
    """新增一次训练（连同全部组，原子写入）；``plan_session_id`` 为 None 即额外训练。

    ``auto_link=true`` 时仅当天恰有一个未完成日程才关联，零个或多个候选一律 409（不猜）。
    """
    session = await WorkoutRecordsService(request.app.state.db).create(
        body.performed_on,
        workout_facts_from_dto(body),
        plan_session_id=body.plan_session_id,
        auto_link=body.auto_link,
    )
    return {"record": record_dto(session)}


@router.get("/api/records/plan-session-candidates")
async def list_plan_session_candidates(
    request: Request,
    date: date | None = None,
    business_day: date = Depends(current_business_date),
) -> dict[str, Any]:
    """当天可关联的计划日程候选（未取消且未被其他训练关联）；省略 date 用业务自然日。"""
    target = business_day if date is None else date
    sessions = await WorkoutRecordsService(
        request.app.state.db
    ).list_unfinished_plan_sessions(target)
    return {"sessions": [plan_session_candidate_dto(session) for session in sessions]}


@router.get("/api/records/{record_id}")
async def get_record(record_id: int, request: Request) -> dict[str, Any]:
    """按身份读取一次训练及其全部组；不存在即 404。"""
    session = await WorkoutRecordsService(request.app.state.db).get(record_id)
    if session is None:
        raise WorkoutRecordNotFound(f"训练记录不存在：{record_id}")
    return {"record": record_dto(session)}


@router.put("/api/records/{record_id}")
async def update_record(
    record_id: int, body: RecordBody, request: Request
) -> dict[str, Any]:
    """整条覆盖一次训练（日期、关联日程与全部组行一起替换）；不存在即 404。"""
    session = await WorkoutRecordsService(request.app.state.db).update(
        record_id,
        body.performed_on,
        workout_facts_from_dto(body),
        plan_session_id=body.plan_session_id,
        auto_link=body.auto_link,
    )
    return {"record": record_dto(session)}


@router.delete("/api/records/{record_id}", status_code=204)
async def delete_record(record_id: int, request: Request) -> None:
    """物理删除一次训练（组行级联删除）；不存在即 404。"""
    await WorkoutRecordsService(request.app.state.db).delete(record_id)


# ---------- 身体指标 ----------


@router.get("/api/body-metrics")
async def list_body_metrics(request: Request) -> dict[str, Any]:
    """全部身体指标（按发生日期排序）；体脂未记录保持 null。"""
    metrics = await BodyMetricsService(request.app.state.db).list_all()
    return {"metrics": [body_metric_dto(metric) for metric in metrics]}


@router.post("/api/body-metrics")
async def create_body_metric(body: BodyMetricBody, request: Request) -> dict[str, Any]:
    """新增一条身体指标；体脂省略或 null 都落库为 NULL，不补 0。"""
    metric = await BodyMetricsService(request.app.state.db).create(
        body.measured_on, body.weight_kg, body.body_fat_pct
    )
    return {"metric": body_metric_dto(metric)}


@router.put("/api/body-metrics/{metric_id}")
async def update_body_metric(
    metric_id: int, body: BodyMetricBody, request: Request
) -> dict[str, Any]:
    """整条覆盖一条身体指标；不存在即 404。"""
    metric = await BodyMetricsService(request.app.state.db).update(
        metric_id, body.measured_on, body.weight_kg, body.body_fat_pct
    )
    return {"metric": body_metric_dto(metric)}


@router.delete("/api/body-metrics/{metric_id}", status_code=204)
async def delete_body_metric(metric_id: int, request: Request) -> None:
    """删除一条身体指标；不存在即 404。"""
    await BodyMetricsService(request.app.state.db).delete(metric_id)


# ---------- 动作目录 ----------


@router.get("/api/exercises")
async def list_exercises(request: Request) -> dict[str, Any]:
    """动作目录全量（按稳定身份排序）：表单的动作选择与负重口径来源。"""
    exercises = await ActionCatalogService(request.app.state.db).list_all()
    return {"exercises": [exercise_dto(exercise) for exercise in exercises]}
