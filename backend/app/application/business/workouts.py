import json
from functools import partial
from threading import Event
from uuid import uuid4

from app.ai.messages import TextContent
from app.ai.types import check_cancelled
from app.application.business.catalog import Catalog
from app.application.business.coordination import ReplacementCoordinator
from app.application.business.profile import (
    check_explicit_confirmation,
    ensure_confirmation_exclusive,
    entries_by_id,
    now_ms,
    require_batch_result,
    require_branch,
    require_role,
    require_session,
    save_with_replacement,
    saved_result,
    validate_content,
)
from app.application.session.queries import SessionQueries
from app.domain.business.errors import (
    InvalidBusinessPayload,
    WorkoutAccessDenied,
    WorkoutConfirmationInvalid,
    WorkoutNotFound,
    WorkoutProposalInvalidated,
    WorkoutProposalNotFound,
    WorkoutSaveProcessing,
    WorkoutVersionConflict,
)
from app.domain.business.models import (
    BusinessContext,
    BusinessFieldError,
    WorkoutContent,
    WorkoutListArguments,
    WorkoutListResult,
    WorkoutProposal,
    WorkoutProposalArguments,
    WorkoutRecord,
    WorkoutSaveArguments,
    WorkoutSaveRecord,
    WorkoutSaveResult,
    WorkoutSnapshot,
    WorkoutStatusResult,
)
from app.domain.business.repository import BusinessRepository
from app.domain.session.models import SessionMessageEntry


class WorkoutService:
    def __init__(
        self,
        repository: BusinessRepository,
        catalog: Catalog,
        sessions: SessionQueries,
        replacements: ReplacementCoordinator,
    ) -> None:
        self._repository = repository
        self._catalog = catalog
        self._sessions = sessions
        self._replacements = replacements

    async def get_workout(self, workout_id: str) -> WorkoutRecord:
        async with self._repository.transaction():
            record = await self._repository.get_workout(workout_id)
            if record is None:
                raise WorkoutNotFound()
            return record

    async def list_workouts(self, arguments: WorkoutListArguments) -> WorkoutListResult:
        return await self._repository.list_workouts(arguments)

    def _validate_workout(self, payload: object) -> WorkoutContent:
        content = validate_content(WorkoutContent, payload)
        failures = [
            BusinessFieldError(
                path=f"/payload/exercises/{index}/exercise_id",
                message="动作 ID 必须存在于动作目录中。",
            )
            for index, exercise in enumerate(content.exercises)
            if exercise.exercise_id is not None
            and exercise.exercise_id not in self._catalog.exercise_ids
        ]
        if failures:
            raise InvalidBusinessPayload(failures)
        return content

    def _check_workout_date(self, context: BusinessContext, performed_on: str) -> None:
        if performed_on > context.business_date:
            raise InvalidBusinessPayload([
                BusinessFieldError(path="/performed_on", message="训练日期不能晚于业务日期。")
            ])

    def _workout_role(
        self, entries: dict[str, SessionMessageEntry], entry_id: str, role: str,
    ) -> None:
        require_role(
            entries, entry_id, role, WorkoutConfirmationInvalid,
            "消息节点角色或当前路径不合法。", "消息节点角色或当前路径不合法。",
        )

    async def prepare_workout(
        self, context: BusinessContext, arguments: WorkoutProposalArguments,
        signal: Event | None = None,
    ) -> WorkoutProposal:
        content = self._validate_workout(arguments.payload)
        self._check_workout_date(context, arguments.performed_on)
        check_cancelled(signal)
        async with self._repository.transaction():
            entries = entries_by_id(await require_branch(self._sessions, context.session_id))
            self._workout_role(entries, context.request_entry_id, "user")
            self._workout_role(entries, context.source_entry_id, "assistant")
            current = await self._repository.get_workout_by_date(arguments.performed_on)
            if not self._workout_base_matches(arguments, current):
                raise WorkoutVersionConflict()
            snapshot = WorkoutSnapshot(
                **arguments.model_dump(exclude={"payload"}), payload=content,
                proposal_id=str(uuid4()), session_id=context.session_id,
                request_entry_id=context.request_entry_id,
                source_entry_id=context.source_entry_id,
                status="pending", created_at=now_ms(),
            )
            await self._repository.insert_workout_snapshot(snapshot)
            await self._repository.invalidate_pending_workouts(
                context.session_id, snapshot.performed_on, snapshot.proposal_id
            )
            return self._workout_proposal(snapshot)

    def _workout_proposal(self, snapshot: WorkoutSnapshot) -> WorkoutProposal:
        return WorkoutProposal.model_validate(snapshot.model_dump(include={
            "proposal_id", "performed_on", "base_workout_id", "base_workout_version", "payload",
        }))

    def _workout_base_matches(
        self, proposal: WorkoutProposalArguments, current: WorkoutRecord | None
    ) -> bool:
        if current is None:
            return proposal.base_workout_id is None and proposal.base_workout_version is None
        return (proposal.base_workout_id, proposal.base_workout_version) == (current.id, current.version)

    async def bind_workout_display_entry(self, proposal_id: str, display_entry_id: str) -> None:
        async with self._repository.transaction():
            snapshot = await self._repository.get_workout_snapshot(proposal_id)
            if snapshot is None:
                raise WorkoutProposalNotFound()
            entries = entries_by_id(await require_branch(self._sessions, snapshot.session_id))
            self._workout_role(entries, snapshot.source_entry_id, "assistant")
            display = entries.get(display_entry_id)
            if display is None:
                raise WorkoutConfirmationInvalid("展示节点须在当前消息路径中。")
            self._require_workout_result(display, snapshot, entries[snapshot.source_entry_id], entries)
            await self._repository.bind_workout_display_entry(proposal_id, display_entry_id)

    def _require_workout_result(
        self, display: SessionMessageEntry, snapshot: WorkoutSnapshot,
        source: SessionMessageEntry, entries: dict[str, SessionMessageEntry],
    ) -> None:
        require_batch_result(display, source, entries, WorkoutConfirmationInvalid)
        result = display.messages[0]
        if result.is_error or result.tool_name != "prepare_workout":
            raise WorkoutConfirmationInvalid("展示节点不是该准备工具的结果节点。")
        if len(result.content) != 1 or not isinstance(result.content[0], TextContent):
            raise WorkoutConfirmationInvalid("展示结果须为完整 JSON text。")
        displayed = WorkoutProposal.model_validate(json.loads(result.content[0].text))
        if displayed != self._workout_proposal(snapshot):
            raise WorkoutConfirmationInvalid("展示内容与快照不一致。")

    async def save_workout(
        self, context: BusinessContext, arguments: WorkoutSaveArguments,
        signal: Event | None = None, *, on_committed=None,
    ) -> WorkoutSaveResult:
        return await save_with_replacement(
            self._repository, self._replacements, context, arguments, signal,
            partial(self._begin_workout_save, updating=False), self._commit_workout_save,
            self._repository.set_workout_snapshot_status, WorkoutVersionConflict,
            on_committed=on_committed,
        )

    async def update_workout(
        self, context: BusinessContext, arguments: WorkoutSaveArguments,
        signal: Event | None = None, *, on_committed=None,
    ) -> WorkoutSaveResult:
        return await save_with_replacement(
            self._repository, self._replacements, context, arguments, signal,
            partial(self._begin_workout_save, updating=True), self._commit_workout_save,
            self._repository.set_workout_snapshot_status, WorkoutVersionConflict,
            on_committed=on_committed,
        )

    def _workout_saved_result(
        self, context: BusinessContext, snapshot: WorkoutSnapshot | None,
        record: WorkoutSaveRecord | None,
    ) -> WorkoutSaveResult | None:
        return saved_result(
            context, snapshot, record, WorkoutAccessDenied, WorkoutProposalNotFound
        )

    async def check_workout_save_authorization(
        self, context: BusinessContext, arguments: WorkoutSaveArguments, *, updating: bool,
    ) -> None:
        async with self._repository.transaction():
            branch = await require_branch(self._sessions, context.session_id)
            snapshot = await self._repository.get_workout_snapshot(arguments.proposal_id)
            record = await self._repository.get_workout_save_record(arguments.proposal_id)
            if self._workout_saved_result(context, snapshot, record) is not None:
                return
            if snapshot is None:
                raise WorkoutProposalNotFound()
            if updating != (snapshot.base_workout_id is not None):
                raise WorkoutConfirmationInvalid("保存工具与快照的新增或更新类型不一致。")
            await self._check_workout_binding(context, branch, snapshot, arguments)

    async def _begin_workout_save(
        self, context: BusinessContext, arguments: WorkoutSaveArguments, *, updating: bool,
    ) -> WorkoutSaveResult | tuple[WorkoutSnapshot, WorkoutSnapshot]:
        branch = await require_branch(self._sessions, context.session_id)
        snapshot = await self._repository.get_workout_snapshot(arguments.proposal_id)
        record = await self._repository.get_workout_save_record(arguments.proposal_id)
        saved = self._workout_saved_result(context, snapshot, record)
        if saved is not None:
            return saved
        if snapshot is None:
            raise WorkoutProposalNotFound()
        if snapshot.status == "processing":
            raise WorkoutSaveProcessing()
        if snapshot.status == "invalidated":
            raise WorkoutProposalInvalidated()
        if snapshot.status == "conflicted":
            raise WorkoutVersionConflict()
        if updating != (snapshot.base_workout_id is not None):
            raise WorkoutConfirmationInvalid("保存工具与快照的新增或更新类型不一致。")
        bound = await self._check_workout_binding(context, branch, snapshot, arguments)
        await self._repository.begin_workout_save(snapshot.proposal_id, arguments.confirmation_entry_id)
        return snapshot, bound

    async def _check_workout_binding(
        self, context: BusinessContext, branch: list[SessionMessageEntry],
        snapshot: WorkoutSnapshot, arguments: WorkoutSaveArguments,
    ) -> WorkoutSnapshot:
        if snapshot.display_entry_id != arguments.display_entry_id:
            raise WorkoutConfirmationInvalid("展示节点与后端绑定不一致。")
        if snapshot.confirmation_entry_id not in {None, arguments.confirmation_entry_id}:
            raise WorkoutConfirmationInvalid("快照已绑定其他确认消息。")
        await ensure_confirmation_exclusive(
            self._repository, context.session_id, arguments.confirmation_entry_id,
            snapshot.proposal_id, WorkoutConfirmationInvalid, "确认消息已绑定其他快照。",
        )
        entries = entries_by_id(branch)
        for entry_id, role in [
            (snapshot.request_entry_id, "user"), (snapshot.source_entry_id, "assistant"),
            (arguments.confirmation_entry_id, "user"),
        ]:
            self._workout_role(entries, entry_id, role)
        await check_explicit_confirmation(
            self._repository, context.session_id, entries, arguments, "workout",
            WorkoutConfirmationInvalid,
        )
        display = entries.get(arguments.display_entry_id)
        if display is None:
            raise WorkoutConfirmationInvalid("展示节点须在当前消息路径中。")
        self._require_workout_result(display, snapshot, entries[snapshot.source_entry_id], entries)
        order = {entry.id: index for index, entry in enumerate(branch)}
        if not order[display.id] < order[arguments.confirmation_entry_id]:
            raise WorkoutConfirmationInvalid("展示与确认节点时序不合法。")
        self._check_workout_date(context, snapshot.performed_on)
        return snapshot.model_copy(update={
            "status": "processing", "confirmation_entry_id": arguments.confirmation_entry_id,
        })

    async def _commit_workout_save(
        self, snapshot: WorkoutSnapshot, bound: WorkoutSnapshot, context: BusinessContext,
    ) -> WorkoutSaveResult | None:
        if await self._repository.get_workout_snapshot(snapshot.proposal_id) != bound:
            raise WorkoutSaveProcessing()
        arguments = WorkoutSaveArguments(
            proposal_id=bound.proposal_id, display_entry_id=bound.display_entry_id,
            confirmation_entry_id=bound.confirmation_entry_id,
        )
        await self._check_workout_binding(
            context, await require_branch(self._sessions, context.session_id), bound, arguments
        )
        current = await self._repository.get_workout_by_date(snapshot.performed_on)
        if not self._workout_base_matches(snapshot, current):
            await self._repository.set_workout_snapshot_status(snapshot.proposal_id, "conflicted")
            return None
        content = self._validate_workout(snapshot.payload)
        saved_at = now_ms()
        if current is None:
            record = WorkoutRecord(
                id=str(uuid4()), performed_on=snapshot.performed_on, version=1,
                content=content, created_at=saved_at, updated_at=saved_at,
            )
            await self._repository.insert_workout(record)
        else:
            updated = await self._repository.update_workout(
                current.id, current.version, content, saved_at
            )
            if not updated:
                await self._repository.set_workout_snapshot_status(snapshot.proposal_id, "conflicted")
                return None
            record = current.model_copy(update={
                "version": current.version + 1, "content": content, "updated_at": saved_at,
            })
        result = WorkoutSaveResult(
            **record.model_dump(), proposal_id=snapshot.proposal_id, saved_at=saved_at,
        )
        await self._repository.complete_workout_save(WorkoutSaveRecord(
            proposal_id=snapshot.proposal_id, session_id=snapshot.session_id,
            display_entry_id=bound.display_entry_id,
            confirmation_entry_id=bound.confirmation_entry_id, result=result, saved_at=saved_at,
        ))
        return result

    async def get_workout_save_status(
        self, context: BusinessContext, proposal_id: str, signal: Event | None = None,
    ) -> WorkoutStatusResult:
        check_cancelled(signal)
        async with self._repository.transaction():
            await require_session(self._sessions, context.session_id)
            snapshot = await self._repository.get_workout_snapshot(proposal_id)
            record = await self._repository.get_workout_save_record(proposal_id)
            saved = self._workout_saved_result(context, snapshot, record)
            if saved is not None:
                return WorkoutStatusResult(proposal_id=proposal_id, status="saved", result=saved)
            if snapshot is None:
                raise WorkoutProposalNotFound()
            return WorkoutStatusResult(proposal_id=proposal_id, status=snapshot.status, result=None)

    async def list_workout_display_bindings(self, session_id: str) -> dict[str, str]:
        async with self._repository.transaction():
            await require_session(self._sessions, session_id)
            return {
                item.display_entry_id: item.proposal_id
                for item in await self._repository.list_workout_snapshots(session_id)
                if item.display_entry_id is not None and item.status in {"pending", "processing", "saved"}
            }

    async def list_workout_confirmation_bindings(self, session_id: str) -> dict[str, str]:
        async with self._repository.transaction():
            await require_session(self._sessions, session_id)
            bindings: dict[str, str] = {}
            for item in [*await self._repository.list_workout_snapshots(session_id),
                         *await self._repository.list_workout_save_records(session_id)]:
                entry_id = item.confirmation_entry_id
                if entry_id is not None:
                    if entry_id in bindings and bindings[entry_id] != item.proposal_id:
                        raise WorkoutConfirmationInvalid()
                    bindings[entry_id] = item.proposal_id
            return bindings
