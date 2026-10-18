import asyncio
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.application.business.confirmation import ConfirmationService
from app.application.business.coordination import ReplacementCoordinator
from app.application.session.credentials import (
    CREDENTIAL_SAFE_MESSAGE,
    CredentialFilter,
)
from app.application.session.runtime import runs, runs_lock, session_gate, track
from app.application.session.service import SessionService
from app.domain.session.errors import (
    CredentialDetected,
    OperationExpired,
    RunNotFound,
    SessionConflict,
    SessionNotFound,
)
from app.interfaces.http_api.contracts import CreateSessionRequest, PathUUID
from app.interfaces.http_api.errors import (
    NO_STORE,
    check_workout_query,
    reject,
    reject_operation_expired,
)
from app.interfaces.http_api.projections import (
    attachment_object,
    confirmation_operation_object,
    history_entry,
    history_steering,
    pending_reference,
    run_object,
    session_object,
    steering_object,
)

router = APIRouter()


@router.post("/api/sessions")
async def create_session(payload: CreateSessionRequest, request: Request):
    service: SessionService = request.app.state.session_service
    credentials = request.app.state.active_secrets()
    try:
        created, session = await service.create_session_result(
            str(payload.session_id), payload.title, credentials=credentials
        )
    except CredentialDetected:
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    except SessionConflict:
        reject(409, "session_conflict", "会话标题冲突。")
    return JSONResponse(session_object(session), status_code=201 if created else 200)


@router.get("/api/sessions")
async def list_sessions(request: Request):
    guard = CredentialFilter(request.app.state.active_secrets())
    sessions = await request.app.state.session_queries.list_sessions()
    ordered = sorted(
        sessions, key=lambda session: (session.updated_at, session.id), reverse=True
    )
    payload = {"sessions": [session_object(session) for session in ordered]}
    if guard.contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


async def drain_session_runs(session_id: str) -> None:
    # 对目标会话的运行发出协作取消，等待执行任务与持久化收尾真正退出后返回。
    with runs_lock:
        states = [
            state for state in runs.values() if state.session_id == UUID(session_id)
        ]
    for state in states:
        state.disconnect()
    await asyncio.gather(*(state.task for state in states))


@router.delete("/api/sessions/{session_id}")
async def delete_session(session_id: PathUUID, request: Request):
    service: SessionService = request.app.state.session_service
    replacements: ReplacementCoordinator = request.app.state.replacements
    if CredentialFilter(request.app.state.active_secrets(session_id)).contains(session_id):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)

    async def remove() -> None:
        # 串行新运行受理并登记删除意图，等待目标执行与收尾退出后执行删除事务。
        async with session_gate(session_id), replacements.register(session_id):
            await drain_session_runs(session_id)
            await service.delete_session(session_id)

    task = asyncio.get_running_loop().create_task(remove())
    track(task)
    # 删除任务的生命周期独立于 HTTP 连接：断连取消不影响服务端继续完成删除。
    await asyncio.shield(task)
    return JSONResponse({"session_id": session_id, "deleted": True}, headers=NO_STORE)


@router.get("/api/sessions/{session_id}/attachments/{attachment_id}")
async def get_attachment(session_id: PathUUID, attachment_id: PathUUID, request: Request):
    check_workout_query(request, set())
    credentials = request.app.state.active_secrets(session_id)
    if CredentialFilter(credentials).contains({"session_id": session_id, "attachment_id": attachment_id}):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    try:
        metadata, text = await request.app.state.session_attachments.get_attachment_content(
            session_id, attachment_id, credentials=credentials,
        )
    except SessionNotFound:
        reject(404, "session_not_found", "会话不存在。")
    except CredentialDetected:
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    payload = {**attachment_object(metadata), "text": text}
    if CredentialFilter(credentials).contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers=NO_STORE)


@router.get("/api/sessions/{session_id}/history")
async def get_history(session_id: PathUUID, request: Request):
    check_workout_query(request, set())
    service = request.app.state.session_queries
    confirmations: ConfirmationService = request.app.state.confirmations
    database = request.app.state.database
    guard = CredentialFilter(request.app.state.active_secrets(session_id))
    # 消息、会话、运行、Steering 与确认扩展在同一数据库读取快照内取得：
    # 会话与业务仓库共用同一 Database，同事务对当前 Task 可重入。
    try:
        async with database.transaction_scope():
            try:
                history = await service.get_session_history(str(session_id))
            except SessionNotFound:
                reject(404, "session_not_found", "会话不存在。")
            extension = await confirmations.history(str(session_id))
    except SessionNotFound:
        reject(404, "session_not_found", "会话不存在。")
    # 投影前先检查完整原始记录：系统内容、隐藏块、签名、嵌套元数据、身份字段及确认结果与错误。
    if guard.contains(
        {
            "session": history.session.model_dump(),
            "entries": [entry.model_dump() for entry in history.entries],
            "runs": [run.model_dump() for run in history.runs],
            "steering": [item.model_dump() for item in history.steering],
            "confirmations": [item.model_dump() for item in extension.confirmations],
            "pending_proposals": [pending_reference(item) for item in extension.pending_proposals],
        }
    ):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    payload = {
        "session": session_object(history.session),
        "entries": [history_entry(entry) for entry in history.entries],
        "runs": [run_object(run) for run in history.runs],
        "steering": [history_steering(item) for item in history.steering],
        "confirmations": [confirmation_operation_object(item) for item in extension.confirmations],
        "pending_proposals": [pending_reference(item) for item in extension.pending_proposals],
    }
    if guard.contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@router.get("/api/sessions/{session_id}/operations/{operation_id}")
async def get_operation(session_id: PathUUID, operation_id: PathUUID, request: Request):
    check_workout_query(request, set())
    service = request.app.state.session_operations
    try:
        outcome = await service.get_operation_outcome(session_id, operation_id)
    except OperationExpired:
        reject_operation_expired()
    except SessionNotFound:
        reject(404, "session_not_found", "会话不存在。")
    guard = CredentialFilter(request.app.state.active_secrets(session_id))
    if guard.contains({"session_id": session_id, "operation_id": operation_id, "outcome": outcome.model_dump() if outcome is not None else None}):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    payload = {
        "operation_id": operation_id,
        "session_id": session_id,
        "accepted": outcome is not None,
        "kind": outcome.operation.kind if outcome is not None else None,
        "run": run_object(outcome.run) if outcome is not None else None,
        "steering": steering_object(outcome.steering) if outcome is not None and outcome.steering is not None else None,
    }
    if guard.contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers=NO_STORE)


@router.get("/api/sessions/{session_id}/runs/{run_id}")
async def get_run(session_id: PathUUID, run_id: PathUUID, request: Request):
    check_workout_query(request, set())
    service = request.app.state.session_queries
    try:
        await service.get_session(str(session_id))
    except SessionNotFound:
        reject(404, "session_not_found", "会话不存在。")
    try:
        run = await service.get_run(str(session_id), str(run_id))
    except SessionNotFound:
        reject(404, "run_not_found", "运行不存在。")
    payload = run_object(run)
    if CredentialFilter(request.app.state.active_secrets(session_id)).contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers=NO_STORE)


@router.get("/api/sessions/{session_id}/runs")
async def list_session_runs(session_id: PathUUID, request: Request):
    check_workout_query(request, set())
    service = request.app.state.session_queries
    guard = CredentialFilter(request.app.state.active_secrets(session_id))
    try:
        stored = await service.list_runs(session_id)
    except SessionNotFound:
        reject(404, "session_not_found", "会话不存在。")
    payload = {
        "session_id": session_id,
        "runs": [run_object(run) for run in stored],
    }
    if guard.contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@router.get("/api/sessions/{session_id}/runs/{run_id}/steering")
async def list_run_steering(session_id: PathUUID, run_id: PathUUID, request: Request):
    check_workout_query(request, set())
    service = request.app.state.session_queries
    guard = CredentialFilter(request.app.state.active_secrets(session_id))
    try:
        stored = await service.list_steering(session_id, run_id)
    except RunNotFound:
        reject(404, "run_not_found", "运行不存在。")
    except SessionNotFound:
        reject(404, "session_not_found", "会话不存在。")
    # 投影前先检查完整原始记录：输入正文、内容块签名及嵌套元数据。
    if guard.contains([item.model_dump() for item in stored]):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    payload = {
        "session_id": session_id,
        "run_id": run_id,
        "steering": [history_steering(item) for item in stored],
    }
    if guard.contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})
