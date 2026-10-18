from traceback import walk_tb

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.application.session.attachment_files import AttachmentFiles
from app.application.session.credentials import (
    CREDENTIAL_SAFE_MESSAGE,
    CredentialFilter,
)
from app.application.session.operations import SessionOperations
from app.application.session.queries import SessionQueries
from app.domain.business.errors import BusinessError
from app.domain.session.attachments import AttachmentError
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from app.model_config import ModelNotConfigured

NO_STORE = {"Cache-Control": "no-store"}


async def invalid_request(request: Request, error: RequestValidationError):
    code = "invalid_request"
    if any(item["type"] == "attachment_format_invalid" or (
        item["type"] == "value_error" and "attachments" in item["loc"]
        and item["loc"][-1] == "data_base64"
    ) for item in error.errors()):
        code = "attachment_format_invalid"
    return JSONResponse(status_code=422, content={
        "detail": {"code": code, "message": "请求字段不合法。"},
    }, headers=NO_STORE)


async def http_error(request: Request, error: HTTPException):
    headers = {**NO_STORE, **(error.headers or {})}
    return JSONResponse({"detail": error.detail}, error.status_code, headers=headers)


async def business_error(request: Request, error: BusinessError):
    detail = error.detail()
    if CredentialFilter(request.app.state.active_secrets()).contains(detail):
        return JSONResponse({"detail": {
            "code": "credential_detected", "message": CREDENTIAL_SAFE_MESSAGE,
        }}, 422, headers=NO_STORE)
    return JSONResponse({"detail": detail}, error.http_status, headers=NO_STORE)


def stored_attachment_read_error(error: BaseException) -> bool:
    codes = {
        SqliteSessionRepository.get_attachments.__code__,
        SqliteSessionRepository.list_entry_attachments.__code__,
        SqliteSessionRepository.list_steering_attachments.__code__,
    }
    return any(frame.f_code in codes for frame, _ in walk_tb(error.__traceback__))


async def attachment_metadata_error(request: Request, error: ValueError):
    if not stored_attachment_read_error(error):
        raise error
    return JSONResponse({"detail": {
        "code": "internal_error", "message": "服务内部错误。",
    }}, 500, headers=NO_STORE)


async def attachment_error(request: Request, error: AttachmentError):
    code = error.code
    if code == "attachment_format_invalid" and (
        stored_attachment_read_error(error)
        or any(frame.f_code is AttachmentFiles.read.__code__ for frame, _ in walk_tb(error.__traceback__))
    ):
        code = "internal_error"
    status = {
        "invalid_request": 422, "attachment_format_invalid": 422,
        "attachment_size_exceeded": 413, "attachment_not_found": 404,
        "attachment_access_denied": 403, "attachment_conflict": 409,
        "internal_error": 500,
    }[code]
    return JSONResponse({"detail": {"code": code, "message": "附件操作失败。"}}, status, headers=NO_STORE)


async def attachment_native_error(request: Request, error: OSError | UnicodeDecodeError):
    frames = {frame.f_code for frame, _ in walk_tb(error.__traceback__)}
    boundary = AttachmentFiles.read.__code__ in frames
    if isinstance(error, OSError):
        boundary = boundary or bool(frames & {
            AttachmentFiles.write.__code__, AttachmentFiles.prepare.__code__,
            AttachmentFiles.remove_created.__code__,
        })
    if not boundary:
        raise error
    return JSONResponse({"detail": {
        "code": "internal_error", "message": "服务内部错误。",
    }}, 500, headers=NO_STORE)


async def internal_error(request: Request, error: Exception):
    return JSONResponse(status_code=500, content={
        "detail": {"code": "internal_error", "message": "服务内部错误。"},
    }, headers=NO_STORE)


async def model_not_configured(request: Request, error: ModelNotConfigured):
    return JSONResponse(status_code=409, content={
        "detail": {
            "code": "model_not_configured",
            "message": "尚未配置可用的模型，无法发起新的运行。",
        },
    }, headers=NO_STORE)


def reject(
    status: int,
    code: str,
    message: str,
    *,
    reason: str | None = None,
    headers: dict[str, str] | None = None,
) -> None:
    detail = {"code": code, "message": message}
    if reason is not None:
        detail["reason"] = reason
    raise HTTPException(status, detail, headers=headers)


def reject_operation_expired() -> None:
    reject(409, "operation_conflict", "该操作已失效。", reason="operation_expired")


async def reject_if_expired(
    operations: SessionOperations, operation_id: str, session_id: str
) -> None:
    if await operations.get_operation_invalidation(operation_id) == session_id:
        reject_operation_expired()


async def require_run(queries: SessionQueries, run_id: str, session_id: str) -> None:
    # 数据库为运行存在与会话归属的权威，重启后原键重试仍可定位。
    run = await queries.find_run_any(str(run_id))
    if run is None:
        reject(404, "run_not_found", "运行不存在。")
    if run.session_id != str(session_id):
        reject(409, "session_mismatch", "运行与会话不匹配。")


def check_workout_query(request: Request, allowed: set[str]) -> None:
    keys = list(request.query_params.keys())
    if any(key not in allowed or len(request.query_params.getlist(key)) != 1 for key in keys):
        reject(422, "invalid_request", "请求字段不合法。")
