import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from time import perf_counter

from app.agent.telemetry import RunTrace, TraceSpan, TraceStore
from app.ai.types import check_cancelled
from app.application.business.profile import BUSINESS_TIMEZONE, business_date
from app.application.business.proposals import PendingProposalReference
from app.application.session.entries import SessionEntries
from app.application.session.operations import SessionOperations
from app.application.session.queries import SessionQueries
from app.domain.business.errors import (
    BusinessError,
    ConfirmationBusy,
    ConfirmationNotFound,
    ConfirmationOperationConflict,
    ConfirmationSessionNotFound,
    PlanAccessDenied,
    PlanConfirmationInvalid,
    PlanProposalInvalidated,
    PlanProposalNotFound,
    PlanSaveProcessing,
    PlanVersionConflict,
    ProfileAccessDenied,
    ProfileConfirmationInvalid,
    ProfileProposalInvalidated,
    ProfileProposalNotFound,
    ProfileUpdateProcessing,
    ProfileVersionConflict,
    WorkoutAccessDenied,
    WorkoutConfirmationInvalid,
    WorkoutProposalInvalidated,
    WorkoutProposalNotFound,
    WorkoutSaveProcessing,
    WorkoutVersionConflict,
)
from app.domain.business.models import (
    BusinessContext,
    ConfirmationError,
    PlanSaveArguments,
    ProfileSaveArguments,
    SaveConfirmationOperation,
    SaveConfirmationRequest,
    WorkoutSaveArguments,
)
from app.domain.business.repository import BusinessRepository
from app.domain.session.errors import RunBusy, SessionNotFound
from app.domain.session.models import SaveConfirmation

# 每个业务类型的错误映射：确认受理与保存执行共用，语义与既有业务错误码保持一致。
_STATUS_ERRORS = {
    "profile": (ProfileUpdateProcessing, ProfileProposalInvalidated, ProfileVersionConflict),
    "plan": (PlanSaveProcessing, PlanProposalInvalidated, PlanVersionConflict),
    "workout": (WorkoutSaveProcessing, WorkoutProposalInvalidated, WorkoutVersionConflict),
}
_NOT_FOUND = {
    "profile": ProfileProposalNotFound,
    "plan": PlanProposalNotFound,
    "workout": WorkoutProposalNotFound,
}
_ACCESS_DENIED = {
    "profile": ProfileAccessDenied,
    "plan": PlanAccessDenied,
    "workout": WorkoutAccessDenied,
}
_CONFIRMATION_INVALID = {
    "profile": ProfileConfirmationInvalid,
    "plan": PlanConfirmationInvalid,
    "workout": WorkoutConfirmationInvalid,
}
_INTERRUPTED_MESSAGE = "确认操作已中断。"


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


@dataclass(frozen=True)
class ConfirmationHistory:
    # 会话历史扩展：确认操作与当前有效待确认引用（方案 §7.6）。
    confirmations: list[SaveConfirmationOperation]
    pending_proposals: list[PendingProposalReference]


class ConfirmationService:
    """结构化保存确认的独立应用服务（方案 §7）。

    确认节点由后端生成、run_id 为空、不创建模型运行；保存直接调用业务服务，
    授权拒绝整体回滚零提交，业务写入与确认操作成功终态在同一事务内落地。
    """

    def __init__(
        self,
        repository: BusinessRepository,
        queries: SessionQueries,
        operations: SessionOperations,
        entries: SessionEntries,
        business,
    ) -> None:
        self._repository = repository
        self._queries = queries
        self._operations = operations
        self._entries = entries
        self._business = business

    async def confirm(
        self, session_id: str, request: SaveConfirmationRequest, signal: Event | None = None,
        *, trace_store: TraceStore | None = None,
        contains: Callable[[object], bool] | None = None,
    ) -> SaveConfirmationOperation:
        check_cancelled(signal)
        operation, accepted = await self._accept(session_id, request, _now_ms())
        if not accepted:
            # 同键复用（含处理中、已保存、已失败）与已保存快照重复确认：返回原操作对象，
            # 本次请求不是执行负责人，不得启动保存、改写状态或覆盖原执行结果。
            return operation
        return await self._execute(
            operation, request, signal, trace_store=trace_store, contains=contains
        )

    async def get(self, session_id: str, operation_id: str) -> SaveConfirmationOperation:
        async with self._repository.transaction():
            await self._require_session(session_id)
            operation = await self._repository.get_confirmation_operation(operation_id)
            if operation is None or operation.session_id != session_id:
                raise ConfirmationNotFound()
            return operation

    async def history(self, session_id: str) -> ConfirmationHistory:
        async with self._repository.transaction():
            await self._require_session(session_id)
            branch = await self._queries.get_current_branch(session_id)
            order = {entry.id: index for index, entry in enumerate(branch)}
            operations = await self._repository.list_confirmation_operations(session_id)
            confirmations = [item for item in operations if item.confirmation_entry_id in order]
            confirmations.sort(
                key=lambda item: (order[item.confirmation_entry_id], item.operation_id)
            )
            references = [
                item
                for item in await self._business.proposals.list_pending_references(session_id)
                if item.display_entry_id is not None and item.display_entry_id in order
            ]
            return ConfirmationHistory(
                confirmations=confirmations, pending_proposals=references,
            )

    async def recover_interrupted(self) -> int:
        # 启动先核对处理中操作的业务保存记录：已提交恢复 saved，未提交记录中断失败并解绑。
        recovered = 0
        async with self._repository.transaction():
            operations = await self._repository.list_processing_confirmation_operations()
            for operation in operations:
                record = await self._save_record(operation.business_kind, operation.proposal_id)
                if record is not None:
                    await self._repository.set_confirmation_operation_outcome(
                        operation.operation_id, "saved",
                        _dump(record.result.model_dump()), None, _now_ms(),
                    )
                else:
                    error = ConfirmationError(
                        code="confirmation_interrupted", message=_INTERRUPTED_MESSAGE,
                    )
                    await self._repository.set_confirmation_operation_outcome(
                        operation.operation_id, "failed", None, _dump(error.model_dump()),
                        _now_ms(),
                    )
                    await self._release(
                        operation.business_kind, operation.proposal_id,
                        operation.confirmation_entry_id,
                    )
                recovered += 1
        return recovered

    # 受理事务：登记确认节点与操作，或复用、或拒绝；授权拒绝时整体回滚零提交。
    # 返回值第二个项表示本次调用是否是该操作的首次受理，只有首次受理负责执行保存。

    async def _accept(
        self, session_id: str, request: SaveConfirmationRequest, now: int,
    ) -> tuple[SaveConfirmationOperation, bool]:
        async with self._repository.transaction():
            await self._require_session(session_id)
            if await self._operations.get_operation(request.operation_id) is not None:
                raise ConfirmationOperationConflict("该操作 ID 已用于聊天操作。")
            # 聊天与确认共用操作 ID 命名空间：现存操作之外，编辑清除的原操作只留失效标记，
            # 该标记同样禁止跨入口复用编号。
            invalidated_session = await self._operations.get_operation_invalidation(
                request.operation_id
            )
            if invalidated_session is not None:
                if invalidated_session == session_id:
                    raise ConfirmationOperationConflict(
                        "该操作 ID 对应的原操作已因内容编辑失效。",
                        reason="operation_expired",
                    )
                raise ConfirmationOperationConflict("该操作 ID 已被其他会话的操作占用。")
            stored = await self._repository.get_confirmation_operation(request.operation_id)
            if stored is not None:
                if not self._same_request(stored, session_id, request):
                    raise ConfirmationOperationConflict("该操作 ID 已用于其他确认请求。")
                return stored, False
            try:
                await self._queries.ensure_idle()
            except RunBusy as error:
                raise ConfirmationBusy() from error
            snapshot, record = await self._load(request.business_kind, request.proposal_id)
            owner_session = record.session_id if record is not None else snapshot.session_id
            if owner_session != session_id:
                raise _ACCESS_DENIED[request.business_kind]()
            if record is not None:
                # 已保存快照重复确认：关联原确认节点与固定结果，不新增消息或业务版本。
                original_display = (
                    snapshot.display_entry_id if snapshot is not None else record.display_entry_id
                )
                if original_display != request.display_entry_id:
                    raise _CONFIRMATION_INVALID[request.business_kind](
                        "展示节点与后端绑定不一致。"
                    )
                operation = self._build_operation(
                    session_id, request, record.confirmation_entry_id, "saved",
                    record.result, None, now,
                )
                await self._repository.insert_confirmation_operation(operation)
                return operation, False
            self._require_pending(request.business_kind, snapshot.status)
            if snapshot.display_entry_id != request.display_entry_id:
                raise _CONFIRMATION_INVALID[request.business_kind](
                    "展示节点与后端绑定不一致。"
                )
            entry = await self._entries.append_confirmation_entry(
                session_id,
                SaveConfirmation(
                    business_kind=request.business_kind,
                    proposal_id=request.proposal_id,
                    display_entry_id=request.display_entry_id,
                ),
            )
            operation = self._build_operation(
                session_id, request, entry.id, "processing", None, None, now,
            )
            await self._repository.insert_confirmation_operation(operation)
            # 在同一受理事务内完成完整授权核验：不通过即回滚，不留下确认节点或操作。
            context = BusinessContext(
                timezone=BUSINESS_TIMEZONE,
                business_date=business_date(now),
                session_id=session_id,
                run_id="",
                request_entry_id=entry.id,
                source_entry_id=snapshot.source_entry_id,
            )
            await self._check_authorization(request.business_kind, context, snapshot, request)
            return operation, True

    # 已受理执行负责人边界：建立一次独立确认根 Trace，覆盖受理后的校验、保存与结果确定。
    # 业务终态提交后结算根、尝试整批写入，随后返回原确认响应；纯观测故障沿用既有隔离。

    async def _execute(
        self, operation: SaveConfirmationOperation, request: SaveConfirmationRequest,
        signal: Event | None, *, trace_store: TraceStore | None = None,
        contains: Callable[[object], bool] | None = None,
    ) -> SaveConfirmationOperation:
        if trace_store is None:
            # 未提供观测依赖：沿用无记录语义，独立调用与测试路径行为不变。
            return await self._run_save(operation, request, signal)
        # 根身份取实际受理操作：session_id 与 operation_id 来自已受理操作，run_id 为空。
        # 受理起点在受理事务返回之后，根只覆盖已受理后的执行段。
        trace = RunTrace(
            session_id=operation.session_id,
            operation_id=operation.operation_id,
            run_id=None,
            contains=contains,
            accepted_at=time.time_ns() // 1_000_000,
            accepted_perf=perf_counter(),
        )

        async def settle(root: TraceSpan) -> SaveConfirmationOperation:
            result = await self._run_save(operation, request, signal)
            if result.status == "saved":
                root.set_status("completed")
            else:
                # 已受理保存失败以真实结局显式记 failed；错误码只用既有受控代码。
                root.set_status("failed", None if result.error is None else result.error.code)
            return result

        try:
            return await trace.start_span(
                "confirmation", {"business_kind": operation.business_kind}, settle
            )
        finally:
            # 成功、失败、取消与断连收尾都尝试写入并释放记录器；写入属后续开销边界。
            await trace.finish(trace_store)

    # 保存执行：业务写入与确认操作成功终态在同一提交事务内落地。

    async def _run_save(
        self, operation: SaveConfirmationOperation, request: SaveConfirmationRequest,
        signal: Event | None,
    ) -> SaveConfirmationOperation:
        snapshot, _ = await self._load(request.business_kind, request.proposal_id)
        now = _now_ms()
        context = BusinessContext(
            timezone=BUSINESS_TIMEZONE,
            business_date=business_date(now),
            session_id=operation.session_id,
            run_id="",
            request_entry_id=operation.confirmation_entry_id,
            source_entry_id=snapshot.source_entry_id,
        )

        async def on_committed(result) -> None:
            await self._repository.set_confirmation_operation_outcome(
                operation.operation_id, "saved", _dump(result.model_dump()), None, _now_ms(),
            )

        if request.business_kind == "profile":
            arguments = ProfileSaveArguments(
                proposal_id=request.proposal_id,
                display_entry_id=request.display_entry_id,
                confirmation_entry_id=operation.confirmation_entry_id,
            )
            save = self._business.profile.save_profile_update
        elif request.business_kind == "plan":
            arguments = PlanSaveArguments(
                proposal_id=request.proposal_id,
                display_entry_id=request.display_entry_id,
                confirmation_entry_id=operation.confirmation_entry_id,
            )
            save = self._business.plans.save_plan
        else:
            arguments = WorkoutSaveArguments(
                proposal_id=request.proposal_id,
                display_entry_id=request.display_entry_id,
                confirmation_entry_id=operation.confirmation_entry_id,
            )
            save = (
                self._business.workouts.save_workout
                if snapshot.base_workout_id is None
                else self._business.workouts.update_workout
            )
        try:
            await save(context, arguments, signal, on_committed=on_committed)
        except BusinessError as error:
            await self._record_failure(operation, error)
            return await self._reload(operation.operation_id)
        return await self._reload(operation.operation_id)

    # 内部协作。

    async def _check_authorization(self, business_kind, context, snapshot, request) -> None:
        if business_kind == "profile":
            await self._business.profile.check_profile_save_authorization(
                context, ProfileSaveArguments(
                    proposal_id=request.proposal_id,
                    display_entry_id=request.display_entry_id,
                    confirmation_entry_id=context.request_entry_id,
                ),
            )
        elif business_kind == "plan":
            await self._business.plans.check_plan_save_authorization(
                context, PlanSaveArguments(
                    proposal_id=request.proposal_id,
                    display_entry_id=request.display_entry_id,
                    confirmation_entry_id=context.request_entry_id,
                ),
            )
        else:
            await self._business.workouts.check_workout_save_authorization(
                context, WorkoutSaveArguments(
                    proposal_id=request.proposal_id,
                    display_entry_id=request.display_entry_id,
                    confirmation_entry_id=context.request_entry_id,
                ),
                updating=snapshot.base_workout_id is not None,
            )

    def _require_pending(self, business_kind: str, status: str) -> None:
        if status == "pending":
            return
        processing, invalidated, conflict = _STATUS_ERRORS[business_kind]
        if status == "processing":
            raise processing()
        if status == "invalidated":
            raise invalidated()
        if status == "conflicted":
            raise conflict()
        raise _NOT_FOUND[business_kind]()

    async def _record_failure(
        self, operation: SaveConfirmationOperation, error: BusinessError,
    ) -> None:
        payload = ConfirmationError(
            code=error.code, message=str(error),
            errors=error.errors if error.errors else None,
        )
        async with self._repository.transaction():
            await self._repository.set_confirmation_operation_outcome(
                operation.operation_id, "failed", None, _dump(payload.model_dump()), _now_ms(),
            )

    async def _reload(self, operation_id: str) -> SaveConfirmationOperation:
        async with self._repository.transaction():
            operation = await self._repository.get_confirmation_operation(operation_id)
            assert operation is not None, operation_id
            return operation

    async def _load(self, business_kind: str, proposal_id: str):
        if business_kind == "profile":
            snapshot = await self._repository.get_snapshot(proposal_id)
            record = await self._repository.get_save_record(proposal_id)
        elif business_kind == "plan":
            snapshot = await self._repository.get_plan_snapshot(proposal_id)
            record = await self._repository.get_plan_save_record(proposal_id)
        else:
            snapshot = await self._repository.get_workout_snapshot(proposal_id)
            record = await self._repository.get_workout_save_record(proposal_id)
        if snapshot is None and record is None:
            raise _NOT_FOUND[business_kind]()
        return snapshot, record

    async def _save_record(self, business_kind: str, proposal_id: str):
        if business_kind == "profile":
            return await self._repository.get_save_record(proposal_id)
        if business_kind == "plan":
            return await self._repository.get_plan_save_record(proposal_id)
        return await self._repository.get_workout_save_record(proposal_id)

    async def _release(self, business_kind: str, proposal_id: str, confirmation_entry_id: str) -> None:
        if business_kind == "profile":
            await self._repository.release_profile_processing(proposal_id, confirmation_entry_id)
        elif business_kind == "plan":
            await self._repository.release_plan_processing(proposal_id, confirmation_entry_id)
        else:
            await self._repository.release_workout_processing(proposal_id, confirmation_entry_id)

    async def _require_session(self, session_id: str) -> None:
        try:
            await self._queries.get_session(session_id)
        except SessionNotFound as error:
            raise ConfirmationSessionNotFound() from error

    def _build_operation(
        self, session_id, request, confirmation_entry_id, status, result, error, now,
    ) -> SaveConfirmationOperation:
        return SaveConfirmationOperation(
            session_id=session_id,
            operation_id=request.operation_id,
            business_kind=request.business_kind,
            proposal_id=request.proposal_id,
            display_entry_id=request.display_entry_id,
            confirmation_entry_id=confirmation_entry_id,
            status=status,
            result=result,
            error=error,
            created_at=now,
            updated_at=now,
        )

    def _same_request(
        self, stored: SaveConfirmationOperation, session_id: str, request: SaveConfirmationRequest,
    ) -> bool:
        return (
            stored.session_id == session_id
            and stored.business_kind == request.business_kind
            and stored.proposal_id == request.proposal_id
            and stored.display_entry_id == request.display_entry_id
        )
