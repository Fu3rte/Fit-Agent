"""表单端点的统一错误形状：按异常类型注册状态码与固定错误体。"""

import sqlite3
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.errors import InvalidRequestShape
from app.api.schemas.plan_dto import UnknownResource
from app.application.ports import ConversationNotFound, ModelConfigurationError
from app.application.services.body_metrics_service import BodyMetricNotFound
from app.application.services.plans_service import (
    PlanActivationError,
    PlanNotFound,
)
from app.application.services.records_service import (
    PlanSessionLinkAmbiguous,
    PlanSessionLinkUnavailable,
    WorkoutRecordNotFound,
)
from app.domain.actions.rules import RecordLoadMismatch, UnknownExercise
from app.domain.body_metrics.rules import InvalidBodyMetric
from app.domain.profile.rules import InvalidProfile, UnknownExerciseReference
from app.domain.records.rules import InvalidRecordFact

ERROR_CODE_INVALID_REQUEST = "invalid_request"

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
    (ConversationNotFound, 404),
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
