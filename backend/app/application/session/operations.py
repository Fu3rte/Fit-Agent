from collections.abc import Sequence

from app.application.session.credentials import (
    reject_credentials,
    reject_request_credentials,
)
from app.application.session.positions import now_ms
from app.application.session.queries import SessionQueries
from app.domain.session.attachments import AttachmentMetadata
from app.domain.session.errors import (
    OperationConflict,
    OperationExpired,
    SessionMismatch,
)
from app.domain.session.models import (
    EditRequest,
    OperationOutcome,
    RegenerateRequest,
    SendRequest,
    SessionOperation,
    SessionRun,
    SteeringInput,
    SteeringRequest,
)
from app.domain.session.repository import SessionRepository

_REQUEST_MODELS = {
    "send": SendRequest,
    "edit": EditRequest,
    "regenerate": RegenerateRequest,
    "steering": SteeringRequest,
}


def same_request(kind: str, stored_request: object, incoming: object) -> bool:
    model = _REQUEST_MODELS[kind]
    stored = model.model_validate({
        key: value for key, value in stored_request.items() if key != "accepted_attachments"
    })
    if kind == "edit" and (
        ("attachments" in stored.model_fields_set) != ("attachments" in incoming.model_fields_set)
    ):
        return False
    return stored == incoming


class SessionOperations:
    def __init__(
        self, repository: SessionRepository, queries: SessionQueries
    ) -> None:
        self._repository = repository
        self._queries = queries

    async def get_operation(self, operation_id: str) -> SessionOperation | None:
        return await self._repository.get_operation(operation_id)

    async def get_operation_outcome(
        self, session_id: str, operation_id: str,
    ) -> OperationOutcome | None:
        async with self._repository.transaction():
            await self._queries.require_session(session_id)
            if await self._repository.get_operation_invalidation(operation_id) == session_id:
                raise OperationExpired("该操作已失效")
            operation = await self._repository.get_operation(operation_id)
            if operation is None or operation.session_id != session_id:
                return None
            run = await self._queries.require_run(session_id, operation.run_id)
            steering = None
            if operation.kind == "steering":
                steering = await self._queries.require_steering(session_id, operation.steering_id)
            return OperationOutcome(created=False, operation=operation, run=run, steering=steering)

    async def get_operation_invalidation(self, operation_id: str) -> str | None:
        # 返回失效记录所属会话 ID；None 表示未失效。
        return await self._repository.get_operation_invalidation(operation_id)

    async def resolve_operation(
        self, operation_id: str, session_id: str, kind: str, request: object,
        *, credentials: Sequence[str] = (),
    ) -> OperationOutcome | None:
        reject_credentials({"session_id": session_id, "operation_id": operation_id}, credentials)
        reject_request_credentials(request, credentials)
        async with self._repository.transaction():
            return await self._resolve_operation(operation_id, session_id, kind, request)

    async def record_operation(
        self,
        operation_id: str,
        session_id: str,
        kind: str,
        request: object,
        run_id: str,
        steering_id: str | None = None,
        accepted_attachments: list[AttachmentMetadata] | None = None,
    ) -> SessionOperation:
        recorded_request = request.model_dump(exclude_unset=True)
        if accepted_attachments is not None:
            recorded_request["accepted_attachments"] = [item.model_dump() for item in accepted_attachments]
        operation = SessionOperation(
            operation_id=operation_id,
            session_id=session_id,
            kind=kind,
            request=recorded_request,
            run_id=run_id,
            steering_id=steering_id,
            created_at=now_ms(),
        )
        await self._repository.insert_operation(operation)
        return operation

    async def _resolve_operation(
        self,
        operation_id: str,
        session_id: str,
        kind: str,
        request: object,
    ) -> OperationOutcome | None:
        await self._ensure_active(operation_id, session_id)
        stored = await self._repository.get_operation(operation_id)
        if stored is None:
            # 操作 ID 在聊天与确认入口间统一防冲突：确认账本已占用即视为冲突。
            if await self._repository.confirmation_operation_exists(operation_id):
                raise OperationConflict(
                    f"operation_id 已被确认操作占用: {operation_id}"
                )
            return None
        if stored.session_id != session_id:
            raise SessionMismatch(f"operation_id 属于其他会话: {operation_id}")
        if stored.kind != kind or not same_request(stored.kind, stored.request, request):
            raise OperationConflict(f"operation_id 已被其他请求占用: {operation_id}")
        run: SessionRun | None = None
        steering: SteeringInput | None = None
        if stored.kind == "steering":
            steering = await self._queries.require_steering(
                stored.session_id, stored.steering_id
            )
        else:
            run = await self._queries.require_run(stored.session_id, stored.run_id)
        return OperationOutcome(
            created=False, operation=stored, run=run, steering=steering
        )

    async def _ensure_active(self, operation_id: str, session_id: str) -> None:
        # 已失效编号禁止重新执行：查重之前先检查失效记录。
        invalidated_session = await self._repository.get_operation_invalidation(
            operation_id
        )
        if invalidated_session is None:
            return
        if invalidated_session != session_id:
            raise SessionMismatch(f"operation_id 属于其他会话: {operation_id}")
        raise OperationExpired(f"operation_id 已失效: {operation_id}")
