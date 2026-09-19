"""计划与计划日程只读路由：无任何写入入口。"""

from typing import Any

from fastapi import APIRouter, Request

from api.dto import UnknownResource, plan_dto, plan_session_dto
from domain.plans.repo import PlanRepo

router = APIRouter()


@router.get("/api/plans")
async def list_plans(request: Request) -> dict[str, Any]:
    """全部计划版本（含已归档历史，按版本号升序）。"""
    plans = await PlanRepo(request.app.state.db).list_versions()
    return {"plans": [plan_dto(plan) for plan in plans]}


@router.get("/api/plans/active")
async def get_active_plan(request: Request) -> dict[str, Any]:
    """当前 active 计划；没有正式启用的计划时发 ``plan: null``。"""
    plan = await PlanRepo(request.app.state.db).read_active()
    return {"plan": None if plan is None else plan_dto(plan)}


@router.get("/api/plans/{plan_id}")
async def get_plan(plan_id: int, request: Request) -> dict[str, Any]:
    """按身份读取一个计划版本（含历史版本）；不存在即 404。"""
    plan = await PlanRepo(request.app.state.db).read_by_id(plan_id)
    if plan is None:
        raise UnknownResource(f"计划不存在：{plan_id}")
    return {"plan": plan_dto(plan)}


@router.get("/api/plans/{plan_id}/sessions")
async def list_plan_sessions(plan_id: int, request: Request) -> dict[str, Any]:
    """某个计划的全部日程（含已取消行，按应训练日排序）；计划不存在即 404。"""
    repo = PlanRepo(request.app.state.db)
    if await repo.read_by_id(plan_id) is None:
        raise UnknownResource(f"计划不存在：{plan_id}")
    sessions = await repo.list_sessions(plan_id)
    return {"sessions": [plan_session_dto(session) for session in sessions]}
