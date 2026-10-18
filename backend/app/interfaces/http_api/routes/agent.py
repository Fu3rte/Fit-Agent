from collections.abc import Awaitable, Callable
from time import time_ns

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.agent.prompts import build_system_prompt
from app.agent.tools.business import business_tool_declarations
from app.agent.tools.files import create_file_tools
from app.ai.messages import SystemMessage
from app.application.business.coordination import ReplacementCoordinator
from app.application.session.admission import AdmissionUnavailable, admit_run
from app.application.session.credentials import (
    CREDENTIAL_SAFE_MESSAGE,
    CredentialFilter,
)
from app.application.session.operations import SessionOperations
from app.application.session.service import SessionService
from app.application.session.steering import SteeringCoordinator
from app.domain.session.errors import (
    CredentialDetected,
    EntryNotFound,
    IncompleteToolChain,
    InvalidTargetEntry,
    OperationConflict,
    OperationExpired,
    RunBusy,
    RunClosed,
    SessionMismatch,
    SessionNotFound,
    SteeringConsumptionConflict,
)
from app.domain.session.models import (
    EditCommand,
    EditRequest,
    OperationOutcome,
    RegenerateCommand,
    RegenerateRequest,
    SendCommand,
    SendRequest,
    SteeringCommand,
)
from app.domain.session.models import SteeringRequest as SteeringParams
from app.interfaces.http_api.contracts import (
    EditPayload,
    PathUUID,
    RegeneratePayload,
    RunRequest,
    SteeringRequest,
    WithdrawRequest,
)
from app.interfaces.http_api.errors import (
    check_workout_query,
    reject,
    reject_operation_expired,
    require_run,
)
from app.interfaces.http_api.events import HttpRunEvents
from app.interfaces.http_api.projections import send_result
from app.interfaces.http_api.stream import RunState, RunStream, stream
from app.model_config import load_model_config

router = APIRouter()


async def resolve_existing(
    operations: SessionOperations,
    operation_id: str,
    session_id: str,
    kind: str,
    operation_request: object,
    credentials: Callable,
) -> OperationOutcome | None:
    # 先按 operation_id 查重：已失效编号返回冲突，已受理操作返回持久化原结果。
    try:
        outcome = await operations.resolve_operation(
            operation_id, session_id, kind, operation_request,
            credentials=credentials(session_id),
        )
        if outcome is not None and CredentialFilter(credentials(session_id)).contains(outcome.model_dump()):
            reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
        return outcome
    except CredentialDetected:
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    except OperationExpired:
        reject_operation_expired()
    except OperationConflict:
        reject(409, "operation_conflict", "操作请求冲突。")
    except SessionMismatch:
        reject(409, "session_mismatch", "运行与会话不匹配。")


async def launch_run(
    request: Request,
    *,
    session_id: str,
    operation_id: str,
    origin: str,
    accept: Callable[..., Awaitable[OperationOutcome]],
    operation_request: object,
):
    async def prepare():
        model = load_model_config()
        secrets = tuple(secret for secret in (model.api_key, request.app.state.retrieval_secret()) if secret)
        tools = create_file_tools(session_id)
        return model, tools, secrets, await request.is_disconnected()

    try:
        result = await admit_run(
            service=request.app.state.session_service,
            queries=request.app.state.session_queries,
            entries=request.app.state.session_entries,
            operations=request.app.state.session_operations,
            run_lifecycle=request.app.state.session_runs,
            session_id=session_id, operation_id=operation_id,
            origin=origin, operation_request=operation_request,
            credentials=request.app.state.active_secrets, prepare=prepare, accept=accept,
            coordinator=request.app.state.steering,
            business=request.app.state.business, executor=request.app.state.executor,
            trace_store=request.app.state.trace_store,
            create_state=RunState, create_events=HttpRunEvents,
        )
        if result.state is None:
            return JSONResponse(send_result(operation_id, result.outcome.run))
        return RunStream(result.state, stream(request, result.state))
    except AdmissionUnavailable as exc:
        if exc.reason == "service_unavailable":
            reject(503, "service_unavailable", "服务正在恢复，暂不接受新运行。")
        if exc.reason == "closing":
            reject(409, "run_busy", "服务正在关闭，暂不接受新运行。")
        if exc.reason == "rate_limited":
            reject(429, "rate_limited", "请求过于频繁，请稍后重试。",
                   headers={"Retry-After": str(exc.retry_after)})
        reject(503, "capacity_exceeded", "当前已有任务执行中，请稍后重试。")
    except OperationExpired:
        reject_operation_expired()
    except CredentialDetected:
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    except RunBusy:
        reject(409, "run_busy", "已有任务正在执行。")
    except InvalidTargetEntry:
        reject(409, "invalid_target_entry", "目标节点必须是用户消息。")
    except EntryNotFound:
        reject(404, "entry_not_found", "指定会话中找不到目标节点。")
    except IncompleteToolChain:
        reject(409, "incomplete_tool_chain", "执行分支存在未配对的工具调用。")
    except OperationConflict:
        reject(409, "operation_conflict", "操作请求冲突。")
    except SessionMismatch:
        reject(409, "session_mismatch", "运行与会话不匹配。")
    except SessionNotFound:
        reject(404, "session_not_found", "会话不存在。")


@router.post("/api/agent/run")
async def run(payload: RunRequest, request: Request):
    check_workout_query(request, set())
    session_id = str(payload.session_id)
    operation_id = str(payload.operation_id)
    send_request = SendRequest(text=payload.request, attachments=payload.attachments)

    async def accept(svc: SessionService, tools: dict, secrets: tuple[str, ...]):
        system_message = SystemMessage(
            role="system", content=build_system_prompt(),
            tools_added=[tool.definition() for tool in tools.values()] + business_tool_declarations(),
            timestamp=time_ns() // 1_000_000,
        )
        return await svc.accept_send(
            SendCommand(operation_id=operation_id, session_id=session_id, request=send_request),
            system_message=system_message, credentials=secrets,
        )

    return await launch_run(
        request, session_id=session_id, operation_id=operation_id,
        origin="send", accept=accept, operation_request=send_request,
    )


@router.post("/api/agent/edit")
async def edit(payload: EditPayload, request: Request):
    check_workout_query(request, set())
    session_id = str(payload.session_id)
    operation_id = str(payload.operation_id)
    edit_request = EditRequest(
        target_entry_id=str(payload.target_entry_id), text=payload.request,
        **({"attachments": payload.attachments} if "attachments" in payload.model_fields_set else {}),
    )

    async def accept(svc: SessionService, tools: dict, secrets: tuple[str, ...]):
        replacements: ReplacementCoordinator = request.app.state.replacements
        # 内容替换登记会话替换意图：确认提交在事务提交前检查该意图并让位。
        async with replacements.register(session_id):
            return await svc.accept_edit(
                EditCommand(operation_id=operation_id, session_id=session_id, request=edit_request),
                credentials=secrets,
            )

    return await launch_run(
        request, session_id=session_id, operation_id=operation_id,
        origin="edit", accept=accept, operation_request=edit_request,
    )


@router.post("/api/agent/regenerate")
async def regenerate(payload: RegeneratePayload, request: Request):
    check_workout_query(request, set())
    session_id = str(payload.session_id)
    operation_id = str(payload.operation_id)
    regenerate_request = RegenerateRequest(target_entry_id=str(payload.target_entry_id))

    async def accept(svc: SessionService, tools: dict, secrets: tuple[str, ...]):
        replacements: ReplacementCoordinator = request.app.state.replacements
        async with replacements.register(session_id):
            return await svc.accept_regenerate(
                RegenerateCommand(operation_id=operation_id, session_id=session_id, request=regenerate_request),
                credentials=secrets,
            )

    return await launch_run(
        request, session_id=session_id, operation_id=operation_id,
        origin="regenerate", accept=accept, operation_request=regenerate_request,
    )


@router.post("/api/agent/runs/{run_id}/steering")
async def steering(run_id: PathUUID, payload: SteeringRequest, request: Request):
    check_workout_query(request, set())
    coordinator: SteeringCoordinator = request.app.state.steering
    operations: SessionOperations = request.app.state.session_operations
    queries = request.app.state.session_queries
    steering_request = SteeringParams(target_run_id=run_id, text=payload.message, attachments=payload.attachments)
    existing = await resolve_existing(
        operations, payload.operation_id, payload.session_id, "steering", steering_request,
        request.app.state.active_secrets,
    )
    if existing is None:
        await require_run(queries, run_id, payload.session_id)
    credentials = request.app.state.active_secrets(str(payload.session_id))
    try:
        created, steering_input = await coordinator.accept(
            str(run_id),
            SteeringCommand(
                operation_id=str(payload.operation_id), session_id=str(payload.session_id),
                request=steering_request,
            ),
            credentials=credentials,
        )
    except RunClosed:
        reject(409, "run_closed", "运行已关闭。")
    except OperationExpired:
        reject_operation_expired()
    except OperationConflict:
        reject(409, "operation_conflict", "操作请求冲突。")
    except SessionMismatch:
        reject(409, "session_mismatch", "运行与会话不匹配。")
    except SessionNotFound:
        reject(404, "run_not_found", "运行不存在。")
    except CredentialDetected:
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    if CredentialFilter(credentials).contains(steering_input.model_dump()):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return {
        "operation_id": str(payload.operation_id), "session_id": str(payload.session_id),
        "run_id": str(run_id), "steering_id": steering_input.id, "created": created,
        "status": "accepted" if created else steering_input.status,
        "entry_id": steering_input.entry_id, "reason": steering_input.reason,
    }


@router.post("/api/agent/runs/{run_id}/steering/{steering_id}/withdraw")
async def withdraw(run_id: PathUUID, steering_id: PathUUID, payload: WithdrawRequest, request: Request):
    check_workout_query(request, set())
    coordinator: SteeringCoordinator = request.app.state.steering
    await require_run(request.app.state.session_queries, run_id, payload.session_id)
    try:
        withdrawal = await coordinator.withdraw(str(run_id), str(payload.session_id), str(steering_id))
    except SteeringConsumptionConflict:
        reject(409, "steering_consumption_conflict", "目标输入消费已开始。")
    except SessionMismatch:
        reject(409, "session_mismatch", "运行与会话不匹配。")
    except SessionNotFound:
        reject(404, "steering_not_found", "输入不存在。")
    steering_input = withdrawal.steering
    if CredentialFilter(request.app.state.active_secrets(str(payload.session_id))).contains(steering_input.model_dump()):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return {
        "session_id": str(payload.session_id), "run_id": str(run_id),
        "steering_id": steering_input.id, "status": steering_input.status,
        "entry_id": steering_input.entry_id, "reason": steering_input.reason,
    }
