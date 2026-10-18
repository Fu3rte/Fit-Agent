from dataclasses import dataclass
from threading import Event

from app.ai.messages import AssistantMessage, ToolCall, text_projection
from app.ai.types import check_cancelled
from app.application.business.plans import PlanService
from app.application.business.profile import (
    ProfileService,
    entries_by_id,
    require_branch,
    require_session,
)
from app.application.business.workouts import WorkoutService
from app.application.session.queries import SessionQueries
from app.domain.business.errors import (
    BusinessError,
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
    ProposalAlreadySaved,
    ProposalCancellationInvalid,
    WorkoutAccessDenied,
    WorkoutConfirmationInvalid,
    WorkoutProposalInvalidated,
    WorkoutProposalNotFound,
    WorkoutSaveProcessing,
    WorkoutVersionConflict,
)
from app.domain.business.models import (
    BusinessContext,
    PendingProposalArguments,
    PendingProposalCancellation,
    PlanPendingProposal,
    ProfilePendingProposal,
    WorkoutPendingProposal,
)
from app.domain.business.repository import BusinessRepository
from app.domain.session.models import SessionMessageEntry


@dataclass(frozen=True)
class PendingProposalReference:
    # 本轮待确认提案引用：只含身份与关联节点，完整内容仍按 proposal_id 受控读取。
    business_kind: str
    proposal_id: str
    status: str
    request_entry_id: str
    source_entry_id: str
    display_entry_id: str | None
    confirmation_entry_id: str | None


class PendingProposalService:
    def __init__(
        self,
        repository: BusinessRepository,
        sessions: SessionQueries,
        profile: ProfileService,
        plans: PlanService,
        workouts: WorkoutService,
    ) -> None:
        self._repository = repository
        self._sessions = sessions
        self._profile = profile
        self._plans = plans
        self._workouts = workouts

    # 本轮待确认提案引用：只读聚合三类仍待确认的提案身份与关联节点，供模型投影使用。

    async def list_pending_references(self, session_id: str) -> list[PendingProposalReference]:
        async with self._repository.transaction():
            await require_session(self._sessions, session_id)
            references: list[PendingProposalReference] = []
            for business_kind, snapshots in (
                ("profile", await self._repository.list_snapshots(session_id)),
                ("plan", await self._repository.list_plan_snapshots(session_id)),
                ("workout", await self._repository.list_workout_snapshots(session_id)),
            ):
                for snapshot in snapshots:
                    if snapshot.status not in {"pending", "processing"}:
                        continue
                    references.append(PendingProposalReference(
                        business_kind=business_kind,
                        proposal_id=snapshot.proposal_id,
                        status=snapshot.status,
                        request_entry_id=snapshot.request_entry_id,
                        source_entry_id=snapshot.source_entry_id,
                        display_entry_id=snapshot.display_entry_id,
                        confirmation_entry_id=snapshot.confirmation_entry_id,
                    ))
            return references

    # 提案移出原因只读判定：以真实状态、保存幂等记录或记录缺失为证据，无法确定时抛错停止报告。

    async def removed_proposal_reason(self, business_kind: str, proposal_id: str) -> str:
        async with self._repository.transaction():
            if business_kind == "profile":
                snapshot = await self._repository.get_snapshot(proposal_id)
                record = await self._repository.get_save_record(proposal_id)
            elif business_kind == "plan":
                snapshot = await self._repository.get_plan_snapshot(proposal_id)
                record = await self._repository.get_plan_save_record(proposal_id)
            elif business_kind == "workout":
                snapshot = await self._repository.get_workout_snapshot(proposal_id)
                record = await self._repository.get_workout_save_record(proposal_id)
            else:
                raise ValueError(f"未知业务类型: {business_kind}")
            if record is not None:
                return "saved"
            if snapshot is None:
                return "deleted"
            reasons = {
                "saved": "saved",
                "invalidated": "invalidated",
                "conflicted": "conflicted",
                "pending": "out_of_scope",
                "processing": "out_of_scope",
            }
            if snapshot.status in reasons:
                return reasons[snapshot.status]
            raise RuntimeError(
                f"无法确定提案移出原因: {business_kind}/{proposal_id} 状态 {snapshot.status}"
            )

    # 会话内容替换及整会话删除：快照随绑定的消息节点清理，幂等记录保留。

    async def remove_for_entries(
        self, session_id: str, entry_ids: set[str]
    ) -> None:
        await self._repository.delete_snapshots_for_entries(session_id, entry_ids)
        await self._repository.delete_workout_snapshots_for_entries(session_id, entry_ids)
        await self._repository.delete_plan_snapshots_for_entries(session_id, entry_ids)
        await self._repository.delete_confirmation_operations_for_entries(session_id, entry_ids)

    async def remove_for_session(self, session_id: str) -> None:
        await self._repository.delete_snapshots_for_session(session_id)
        await self._repository.delete_workout_snapshots_for_session(session_id)
        await self._repository.delete_plan_snapshots_for_session(session_id)
        await self._repository.delete_confirmation_operations_for_session(session_id)

    # 待确认提案受控只读读取：复用三类业务的快照、路径及展示校验，不写库。

    def _pending_status(
        self, status: str, processing: type[BusinessError],
        invalidated: type[BusinessError], conflict: type[BusinessError],
    ) -> None:
        if status == "processing":
            raise processing()
        if status == "invalidated":
            raise invalidated()
        if status == "conflicted":
            raise conflict()
        if status == "saved":
            raise ProposalAlreadySaved()

    async def get_pending_proposal(
        self, context: BusinessContext, arguments: PendingProposalArguments,
        signal: Event | None = None,
    ) -> ProfilePendingProposal | PlanPendingProposal | WorkoutPendingProposal:
        check_cancelled(signal)
        async with self._repository.transaction():
            entries = entries_by_id(await require_branch(self._sessions, context.session_id))
            if arguments.business_kind == "profile":
                return await self._pending_profile(context, entries, arguments.proposal_id)
            if arguments.business_kind == "plan":
                return await self._pending_plan(context, entries, arguments.proposal_id)
            return await self._pending_workout(context, entries, arguments.proposal_id)

    async def cancel_pending_proposal(
        self, context: BusinessContext, arguments: PendingProposalArguments,
        signal: Event | None = None,
    ) -> PendingProposalCancellation:
        check_cancelled(signal)
        # 同一事务复用读取校验并使指定快照失效，确认受理与保存共用数据库事务锁。
        async with self._repository.transaction():
            proposal = await self.get_pending_proposal(context, arguments, signal)
            branch = await require_branch(self._sessions, context.session_id)
            entries = entries_by_id(branch)
            request = entries.get(context.request_entry_id)
            source = entries.get(context.source_entry_id)
            if (
                request is None or request.messages[0].role != "user"
                or request.confirmation is not None
                or not text_projection(request.messages[0].content).strip()
                or source is None or not isinstance(source.messages[0], AssistantMessage)
            ):
                raise ProposalCancellationInvalid()
            order = {entry.id: index for index, entry in enumerate(branch)}
            if not (
                order[proposal.display_entry_id] < order[request.id] < order[source.id]
            ) or not any(
                isinstance(block, ToolCall) and block.name == "cancel_pending_proposal"
                and block.arguments == arguments.model_dump()
                for block in source.messages[0].content
            ):
                raise ProposalCancellationInvalid()
            setter, processing = {
                "profile": (self._repository.set_snapshot_status, ProfileUpdateProcessing),
                "plan": (self._repository.set_plan_snapshot_status, PlanSaveProcessing),
                "workout": (self._repository.set_workout_snapshot_status, WorkoutSaveProcessing),
            }[arguments.business_kind]
            if any(
                operation.business_kind == arguments.business_kind
                and operation.proposal_id == arguments.proposal_id
                for operation in await self._repository.list_processing_confirmation_operations()
            ):
                raise processing()
            check_cancelled(signal)
            await setter(arguments.proposal_id, "invalidated")
            check_cancelled(signal)
            return PendingProposalCancellation(
                business_kind=arguments.business_kind, proposal_id=arguments.proposal_id,
            )

    async def _pending_profile(
        self, context: BusinessContext, entries: dict[str, SessionMessageEntry], proposal_id: str,
    ) -> ProfilePendingProposal:
        snapshot = await self._repository.get_snapshot(proposal_id)
        if snapshot is None:
            raise ProfileProposalNotFound()
        if snapshot.session_id != context.session_id:
            raise ProfileAccessDenied()
        self._profile._require_role(entries, snapshot.request_entry_id, "user", "请求节点")
        self._profile._require_role(entries, snapshot.source_entry_id, "assistant", "来源节点")
        if snapshot.display_entry_id is not None:
            self._profile._require_role(entries, snapshot.display_entry_id, "toolResult", "展示节点")
        self._pending_status(
            snapshot.status, ProfileUpdateProcessing,
            ProfileProposalInvalidated, ProfileVersionConflict,
        )
        display_id = snapshot.display_entry_id
        if display_id is None:
            raise ProfileConfirmationInvalid("展示节点尚未绑定。")
        self._profile._require_prepare_result(
            entries[display_id], snapshot, entries[snapshot.source_entry_id], entries
        )
        return ProfilePendingProposal(
            business_kind="profile", proposal_id=snapshot.proposal_id, status=snapshot.status,
            payload=snapshot.payload, request_entry_id=snapshot.request_entry_id,
            source_entry_id=snapshot.source_entry_id, display_entry_id=display_id,
            confirmation_entry_id=snapshot.confirmation_entry_id,
            profile_id=snapshot.profile_id, base_profile_version=snapshot.base_profile_version,
        )

    async def _pending_plan(
        self, context: BusinessContext, entries: dict[str, SessionMessageEntry], proposal_id: str,
    ) -> PlanPendingProposal:
        snapshot = await self._repository.get_plan_snapshot(proposal_id)
        if snapshot is None:
            raise PlanProposalNotFound()
        if snapshot.session_id != context.session_id:
            raise PlanAccessDenied()
        self._plans._plan_role(entries, snapshot.request_entry_id, "user")
        self._plans._plan_role(entries, snapshot.source_entry_id, "assistant")
        if snapshot.display_entry_id is not None:
            self._plans._plan_role(entries, snapshot.display_entry_id, "toolResult")
        self._pending_status(
            snapshot.status, PlanSaveProcessing,
            PlanProposalInvalidated, PlanVersionConflict,
        )
        display_id = snapshot.display_entry_id
        if display_id is None:
            raise PlanConfirmationInvalid("展示节点尚未绑定。")
        self._plans._require_plan_result(
            entries[display_id], snapshot, entries[snapshot.source_entry_id], entries
        )
        return PlanPendingProposal(
            business_kind="plan", proposal_id=snapshot.proposal_id, status=snapshot.status,
            payload=snapshot.payload, request_entry_id=snapshot.request_entry_id,
            source_entry_id=snapshot.source_entry_id, display_entry_id=display_id,
            confirmation_entry_id=snapshot.confirmation_entry_id,
            preparation_kind=snapshot.preparation_kind,
            base_profile_version=snapshot.base_profile_version, base_plan_id=snapshot.base_plan_id,
        )

    async def _pending_workout(
        self, context: BusinessContext, entries: dict[str, SessionMessageEntry], proposal_id: str,
    ) -> WorkoutPendingProposal:
        snapshot = await self._repository.get_workout_snapshot(proposal_id)
        if snapshot is None:
            raise WorkoutProposalNotFound()
        if snapshot.session_id != context.session_id:
            raise WorkoutAccessDenied()
        self._workouts._workout_role(entries, snapshot.request_entry_id, "user")
        self._workouts._workout_role(entries, snapshot.source_entry_id, "assistant")
        if snapshot.display_entry_id is not None:
            self._workouts._workout_role(entries, snapshot.display_entry_id, "toolResult")
        self._pending_status(
            snapshot.status, WorkoutSaveProcessing,
            WorkoutProposalInvalidated, WorkoutVersionConflict,
        )
        display_id = snapshot.display_entry_id
        if display_id is None:
            raise WorkoutConfirmationInvalid("展示节点尚未绑定。")
        self._workouts._require_workout_result(
            entries[display_id], snapshot, entries[snapshot.source_entry_id], entries
        )
        return WorkoutPendingProposal(
            business_kind="workout", proposal_id=snapshot.proposal_id, status=snapshot.status,
            payload=snapshot.payload, request_entry_id=snapshot.request_entry_id,
            source_entry_id=snapshot.source_entry_id, display_entry_id=display_id,
            confirmation_entry_id=snapshot.confirmation_entry_id,
            performed_on=snapshot.performed_on, base_workout_id=snapshot.base_workout_id,
            base_workout_version=snapshot.base_workout_version,
        )
