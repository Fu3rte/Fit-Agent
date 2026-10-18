import asyncio
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.application.business.confirmation import ConfirmationService
from app.application.business.service import BusinessService
from app.application.session.credentials import (
    CREDENTIAL_SAFE_MESSAGE,
    CredentialFilter,
)
from app.application.session.runtime import (
    accepting_runs,
    active,
    operation_lock,
    session_gate,
    track,
)
from app.domain.business.errors import ConfirmationNotFound, business_http_status
from app.domain.business.models import (
    SaveConfirmationOperation,
    SaveConfirmationRequest,
    WorkoutListArguments,
)
from app.interfaces.http_api.contracts import (
    PathUUID,
    SaveConfirmationWire,
    WorkoutQuery,
)
from app.interfaces.http_api.errors import NO_STORE, check_workout_query, reject
from app.interfaces.http_api.projections import confirmation_operation_object

router = APIRouter()


@router.get("/api/profile")
async def get_profile(request: Request):
    business: BusinessService = request.app.state.business
    guard = CredentialFilter(request.app.state.active_secrets())
    response = await business.profile.get_profile()
    payload = response.model_dump()
    if guard.contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


def workout_response(response, request: Request) -> JSONResponse:
    payload = response.model_dump()
    if CredentialFilter(request.app.state.active_secrets()).contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers=NO_STORE)


@router.get("/api/exercises/equipment")
async def get_exercise_equipment(request: Request):
    check_workout_query(request, set())
    payload = request.app.state.business.exercise_equipment()
    if CredentialFilter(request.app.state.active_secrets()).contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers=NO_STORE)


@router.get("/api/plans/current")
async def get_current_plan(request: Request):
    check_workout_query(request, set())
    return workout_response(await request.app.state.business.plans.get_current_plan(), request)


@router.get("/api/plans")
async def list_plans(request: Request):
    check_workout_query(request, set())
    payload = [record.model_dump() for record in await request.app.state.business.plans.list_plans()]
    if CredentialFilter(request.app.state.active_secrets()).contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers=NO_STORE)


@router.get("/api/plans/{plan_id}")
async def get_plan(plan_id: PathUUID, request: Request):
    check_workout_query(request, set())
    return workout_response(await request.app.state.business.plans.get_plan(plan_id), request)


@router.get("/api/workouts")
async def list_workouts(request: Request, query: Annotated[WorkoutQuery, Query()]):
    check_workout_query(request, {"date_from", "date_to", "page", "page_size"})
    arguments = WorkoutListArguments.model_validate(query.model_dump())
    return workout_response(await request.app.state.business.workouts.list_workouts(arguments), request)


@router.get("/api/workouts/{workout_id}")
async def get_workout(workout_id: PathUUID, request: Request):
    check_workout_query(request, set())
    return workout_response(await request.app.state.business.workouts.get_workout(workout_id), request)


def _confirmation_object(operation: SaveConfirmationOperation, status_code: int, request: Request) -> JSONResponse:
    payload = confirmation_operation_object(operation)
    if CredentialFilter(request.app.state.active_secrets(operation.session_id)).contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, status_code=status_code, headers=NO_STORE)


def _confirmation_error_response(operation: SaveConfirmationOperation, request: Request) -> JSONResponse:
    # failed 操作按真实 error 返回既有 detail 结构；仅内容校验错误携带 errors，状态沿用业务定义。
    error = operation.error
    detail: dict = {"code": error.code, "message": error.message}
    if error.code == "invalid_business_payload":
        detail["errors"] = [item.model_dump() for item in (error.errors or [])]
    if CredentialFilter(request.app.state.active_secrets(operation.session_id)).contains(detail):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse({"detail": detail}, status_code=business_http_status(error.code), headers=NO_STORE)


def _confirmation_post_response(operation: SaveConfirmationOperation, request: Request) -> JSONResponse:
    if operation.status == "failed":
        return _confirmation_error_response(operation, request)
    return _confirmation_object(operation, 200 if operation.status == "saved" else 202, request)


@router.post("/api/sessions/{session_id}/confirmations")
async def create_confirmation(session_id: PathUUID, payload: SaveConfirmationWire, request: Request):
    check_workout_query(request, set())
    confirmations: ConfirmationService = request.app.state.confirmations
    session = str(session_id)
    operation_id = str(payload.operation_id)
    req = SaveConfirmationRequest(
        operation_id=operation_id,
        business_kind=payload.business_kind,
        proposal_id=str(payload.proposal_id),
        display_entry_id=str(payload.display_entry_id),
    )
    # 观测依赖显式传入：使用主事件循环已有 TraceStore 与现有凭据检查，不新增逐请求观测连接。
    trace_store = request.app.state.trace_store
    contains = CredentialFilter(request.app.state.active_secrets(session)).contains

    async def execute_new():
        # 后台执行：受理即登记处理中操作并直接调用业务保存；客户端断连不打断收尾，active 恰好释放一次。
        try:
            return await confirmations.confirm(
                session, req, trace_store=trace_store, contains=contains
            )
        finally:
            active.release()

    async with operation_lock(operation_id), session_gate(session):
        # 同键幂等查询先于新操作占用检查：已登记操作复用原对象，处理中同键不误判为新操作。
        try:
            registered = await confirmations.get(session, operation_id)
        except ConfirmationNotFound:
            registered = None
        if registered is not None:
            # 复用路径不新增记录器或根 Span：同键操作沿用原结果，观测边界保持不变。
            return _confirmation_post_response(
                await confirmations.confirm(
                    session, req, trace_store=trace_store, contains=contains
                ), request
            )
        if not accepting_runs.is_set():
            reject(409, "run_busy", "服务正在关闭，暂不接受新确认。")
        if not active.acquire(blocking=False):
            reject(409, "run_busy", "已有任务正在执行。")
        task = asyncio.get_running_loop().create_task(execute_new())
        track(task)
        # shield 令断连取消只关闭响应，后台任务继续完成原操作并持久化终态。
        return _confirmation_post_response(await asyncio.shield(task), request)


@router.get("/api/sessions/{session_id}/confirmations/{operation_id}")
async def get_confirmation(session_id: PathUUID, operation_id: PathUUID, request: Request):
    check_workout_query(request, set())
    confirmations: ConfirmationService = request.app.state.confirmations
    # 查询只读：不触发保存、不创建确认节点；未登记或会话不存在沿用服务的 404 错误。
    operation = await confirmations.get(str(session_id), str(operation_id))
    return _confirmation_object(operation, 200, request)
