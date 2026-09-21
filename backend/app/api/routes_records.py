"""训练记录、身体指标与动作目录表单路由。"""

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Request

from app.api.dependencies import app_services, current_business_date
from app.api.schemas.log_dto import (
    BodyMetricBody,
    RecordBody,
    body_metric_dto,
    exercise_dto,
    record_dto,
    workout_facts_from_dto,
)
from app.api.schemas.plan_dto import plan_session_candidate_dto
from app.application.services.records_service import WorkoutRecordNotFound

router = APIRouter()


@router.get("/api/records")
async def list_records(request: Request) -> dict[str, Any]:
    """全部训练及其组事实（按发生日期排序）。"""
    sessions = await app_services(request).records.list_all()
    return {"records": [record_dto(session) for session in sessions]}


@router.post("/api/records")
async def create_record(body: RecordBody, request: Request) -> dict[str, Any]:
    """新增一次训练（连同全部组，原子写入）；``plan_session_id`` 为 None 即额外训练。"""
    session = await app_services(request).records.create(
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
    sessions = await app_services(request).records.list_unfinished_plan_sessions(target)
    return {"sessions": [plan_session_candidate_dto(session) for session in sessions]}


@router.get("/api/records/{record_id}")
async def get_record(record_id: int, request: Request) -> dict[str, Any]:
    """按身份读取一次训练及其全部组；不存在即 404。"""
    session = await app_services(request).records.get(record_id)
    if session is None:
        raise WorkoutRecordNotFound(f"训练记录不存在：{record_id}")
    return {"record": record_dto(session)}


@router.put("/api/records/{record_id}")
async def update_record(
    record_id: int, body: RecordBody, request: Request
) -> dict[str, Any]:
    """整条覆盖一次训练（日期、关联日程与全部组行一起替换）；不存在即 404。"""
    session = await app_services(request).records.update(
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
    await app_services(request).records.delete(record_id)


@router.get("/api/body-metrics")
async def list_body_metrics(request: Request) -> dict[str, Any]:
    """全部身体指标（按发生日期排序）；体脂未记录保持 null。"""
    metrics = await app_services(request).body_metrics.list_all()
    return {"metrics": [body_metric_dto(metric) for metric in metrics]}


@router.post("/api/body-metrics")
async def create_body_metric(body: BodyMetricBody, request: Request) -> dict[str, Any]:
    """新增一条身体指标；体脂省略或 null 都落库为 NULL，不补 0。"""
    metric = await app_services(request).body_metrics.create(
        body.measured_on, body.weight_kg, body.body_fat_pct
    )
    return {"metric": body_metric_dto(metric)}


@router.put("/api/body-metrics/{metric_id}")
async def update_body_metric(
    metric_id: int, body: BodyMetricBody, request: Request
) -> dict[str, Any]:
    """整条覆盖一条身体指标；不存在即 404。"""
    metric = await app_services(request).body_metrics.update(
        metric_id, body.measured_on, body.weight_kg, body.body_fat_pct
    )
    return {"metric": body_metric_dto(metric)}


@router.delete("/api/body-metrics/{metric_id}", status_code=204)
async def delete_body_metric(metric_id: int, request: Request) -> None:
    """删除一条身体指标；不存在即 404。"""
    await app_services(request).body_metrics.delete(metric_id)


@router.get("/api/exercises")
async def list_exercises(request: Request) -> dict[str, Any]:
    """动作目录全量（按稳定身份排序）：表单的动作选择与负重口径来源。"""
    exercises = await app_services(request).exercises.list_all()
    return {"exercises": [exercise_dto(exercise) for exercise in exercises]}
