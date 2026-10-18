import json
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
    BusinessError,
    InvalidBusinessPayload,
    PlanAccessDenied,
    PlanConfirmationInvalid,
    PlanNotFound,
    PlanProposalInvalidated,
    PlanProposalNotFound,
    PlanSaveProcessing,
    PlanVersionConflict,
    ProfileRequired,
    ProfileVersionConflict,
)
from app.domain.business.models import (
    BusinessContext,
    BusinessFieldError,
    CurrentPlan,
    PlanAdjustmentArguments,
    PlanAdjustmentProposal,
    PlanContent,
    PlanImportArguments,
    PlanImportProposal,
    PlanPreparationKind,
    PlanProposal,
    PlanProposalArguments,
    PlanRecord,
    PlanSaveArguments,
    PlanSaveRecord,
    PlanSaveResult,
    PlanSnapshot,
    PlanStatusResult,
    ProfileContent,
    ProfileRecord,
)
from app.domain.business.repository import BusinessRepository
from app.domain.session.models import SessionMessageEntry

_PLAN_TOOL_NAMES = {
    "generation": "prepare_plan",
    "import": "prepare_plan_import",
    "adjustment": "prepare_plan_adjustment",
}


class PlanService:
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

    async def get_current_plan(self) -> CurrentPlan:
        async with self._repository.transaction():
            record = await self._repository.get_current_plan()
            return CurrentPlan(id=None, content=None) if record is None else CurrentPlan(
                id=record.id, content=record.content,
            )

    async def get_plan(self, plan_id: str) -> PlanRecord:
        async with self._repository.transaction():
            record = await self._repository.get_plan(plan_id)
            if record is None:
                raise PlanNotFound()
            return record

    async def list_plans(self) -> list[PlanRecord]:
        async with self._repository.transaction():
            return await self._repository.list_plans()

    def _validate_plan(self, payload: object, profile: ProfileContent | None,
                       preparation_kind: PlanPreparationKind = "generation") -> PlanContent:
        content = validate_content(PlanContent, payload)
        failures: list[BusinessFieldError] = []
        for day_index, day in enumerate(content.days):
            path = f"/payload/days/{day_index}/exercises"
            if preparation_kind == "generation" and day.kind == "training" and not day.exercises:
                failures.append(BusinessFieldError(path=path, message="生成训练日必须包含具体动作。"))
            for index, exercise in enumerate(day.exercises):
                prefix = f"{path}/{index}"
                if preparation_kind == "generation" and exercise.sets is None:
                    failures.append(BusinessFieldError(path=prefix + "/sets", message="生成动作必须明确组数。"))
                if preparation_kind == "generation" and exercise.reps is None and exercise.duration_seconds is None:
                    failures.append(BusinessFieldError(path=prefix + "/reps", message="生成动作必须明确次数或时长。"))
                if exercise.exercise_id is None:
                    continue
                referenced = self._catalog.get(exercise.exercise_id)
                if referenced is None:
                    failures.append(BusinessFieldError(path=prefix + "/exercise_id", message="动作 ID 必须存在于动作目录中。"))
                elif profile is not None and (exercise.exercise_id in (profile.forbidden_exercise_ids or [])
                      or referenced.equipment in (profile.unavailable_equipment or [])):
                    failures.append(BusinessFieldError(path=prefix + "/exercise_id", message="动作不符合已保存画像的动作或器械限制。"))
        if failures:
            raise InvalidBusinessPayload(failures)
        return content

    def _plan_role(self, entries: dict[str, SessionMessageEntry], entry_id: str, role: str) -> None:
        require_role(
            entries, entry_id, role, PlanConfirmationInvalid,
            "消息节点角色或当前路径不合法。", "消息节点角色或当前路径不合法。",
        )

    async def prepare_plan(
        self, context: BusinessContext, arguments: PlanProposalArguments,
        signal: Event | None = None,
    ) -> PlanProposal:
        return await self._prepare_plan(context, arguments, "generation", signal)

    async def prepare_plan_import(self, context: BusinessContext, arguments: PlanImportArguments,
                                  signal: Event | None = None) -> PlanImportProposal:
        return await self._prepare_plan(context, arguments, "import", signal)

    async def prepare_plan_adjustment(self, context: BusinessContext, arguments: PlanAdjustmentArguments,
                                      signal: Event | None = None) -> PlanAdjustmentProposal:
        return await self._prepare_plan(context, arguments, "adjustment", signal)

    def _plan_profile_conflict(
        self, profile: ProfileRecord | None, version: int | None,
        preparation_kind: PlanPreparationKind,
    ) -> BusinessError | None:
        if profile is None and preparation_kind == "generation":
            return ProfileRequired()
        if (None if profile is None else profile.version) != version:
            return ProfileVersionConflict()
        return None

    async def _prepare_plan(self, context: BusinessContext,
                            arguments: PlanProposalArguments | PlanImportArguments,
                            preparation_kind: PlanPreparationKind, signal: Event | None
                            ) -> PlanProposal | PlanImportProposal | PlanAdjustmentProposal:
        check_cancelled(signal)
        async with self._repository.transaction():
            entries = entries_by_id(await require_branch(self._sessions, context.session_id))
            self._plan_role(entries, context.request_entry_id, "user")
            self._plan_role(entries, context.source_entry_id, "assistant")
            order = {entry_id: index for index, entry_id in enumerate(entries)}
            if order[context.request_entry_id] >= order[context.source_entry_id]:
                raise PlanConfirmationInvalid("请求与来源节点时序不合法。")
            profile = await self._repository.get_profile()
            conflict = self._plan_profile_conflict(profile, arguments.base_profile_version, preparation_kind)
            if conflict is not None:
                raise conflict
            current = await self._repository.get_current_plan()
            if arguments.base_plan_id != (None if current is None else current.id):
                raise PlanVersionConflict()
            content = self._validate_plan(arguments.payload, None if profile is None else profile.content, preparation_kind)
            snapshot = PlanSnapshot(
                **arguments.model_dump(exclude={"payload"}), payload=content,
                preparation_kind=preparation_kind,
                proposal_id=str(uuid4()), session_id=context.session_id,
                request_entry_id=context.request_entry_id, source_entry_id=context.source_entry_id,
                status="pending", created_at=now_ms(),
            )
            await self._repository.insert_plan_snapshot(snapshot)
            await self._repository.invalidate_pending_plans(context.session_id, snapshot.proposal_id)
            return self._plan_proposal(snapshot)

    def _plan_proposal(self, snapshot: PlanSnapshot) -> PlanProposal | PlanImportProposal | PlanAdjustmentProposal:
        model = {"generation": PlanProposal, "import": PlanImportProposal,
                 "adjustment": PlanAdjustmentProposal}[snapshot.preparation_kind]
        fields = {"proposal_id", "base_profile_version", "base_plan_id", "payload"}
        if snapshot.preparation_kind != "generation":
            fields.add("preparation_kind")
        return model.model_validate(snapshot.model_dump(include=fields))

    def _require_plan_result(
        self, display: SessionMessageEntry, snapshot: PlanSnapshot,
        source: SessionMessageEntry, entries: dict[str, SessionMessageEntry],
    ) -> None:
        require_batch_result(display, source, entries, PlanConfirmationInvalid)
        result = display.messages[0]
        if result.is_error or result.tool_name != _PLAN_TOOL_NAMES[snapshot.preparation_kind]:
            raise PlanConfirmationInvalid("展示节点不是该准备工具的结果节点。")
        if len(result.content) != 1 or not isinstance(result.content[0], TextContent):
            raise PlanConfirmationInvalid("展示结果须为完整 JSON text。")
        expected = self._plan_proposal(snapshot)
        displayed = type(expected).model_validate(json.loads(result.content[0].text))
        if displayed != expected:
            raise PlanConfirmationInvalid("展示内容与快照不一致。")

    async def bind_plan_display_entry(self, proposal_id: str, display_entry_id: str) -> None:
        async with self._repository.transaction():
            snapshot = await self._repository.get_plan_snapshot(proposal_id)
            if snapshot is None:
                raise PlanProposalNotFound()
            entries = entries_by_id(await require_branch(self._sessions, snapshot.session_id))
            self._plan_role(entries, snapshot.source_entry_id, "assistant")
            display = entries.get(display_entry_id)
            if display is None:
                raise PlanConfirmationInvalid("展示节点须在当前消息路径中。")
            self._require_plan_result(display, snapshot, entries[snapshot.source_entry_id], entries)
            await self._repository.bind_plan_display_entry(proposal_id, display_entry_id)

    def _plan_saved_result(
        self, context: BusinessContext, snapshot: PlanSnapshot | None, record: PlanSaveRecord | None,
    ) -> PlanSaveResult | None:
        return saved_result(
            context, snapshot, record, PlanAccessDenied, PlanProposalNotFound
        )

    async def save_plan(
        self, context: BusinessContext, arguments: PlanSaveArguments,
        signal: Event | None = None, *, on_committed=None,
    ) -> PlanSaveResult:
        return await save_with_replacement(
            self._repository, self._replacements, context, arguments, signal,
            self._begin_plan_save, self._commit_plan_save,
            self._repository.set_plan_snapshot_status, PlanVersionConflict,
            on_committed=on_committed,
        )

    async def check_plan_save_authorization(
        self, context: BusinessContext, arguments: PlanSaveArguments,
    ) -> None:
        async with self._repository.transaction():
            branch = await require_branch(self._sessions, context.session_id)
            snapshot = await self._repository.get_plan_snapshot(arguments.proposal_id)
            record = await self._repository.get_plan_save_record(arguments.proposal_id)
            if self._plan_saved_result(context, snapshot, record) is not None:
                return
            if snapshot is None:
                raise PlanProposalNotFound()
            await self._check_plan_binding(context, branch, snapshot, arguments)

    async def _begin_plan_save(
        self, context: BusinessContext, arguments: PlanSaveArguments,
    ) -> PlanSaveResult | tuple[PlanSnapshot, PlanSnapshot]:
        branch = await require_branch(self._sessions, context.session_id)
        snapshot = await self._repository.get_plan_snapshot(arguments.proposal_id)
        record = await self._repository.get_plan_save_record(arguments.proposal_id)
        saved = self._plan_saved_result(context, snapshot, record)
        if saved is not None:
            return saved
        if snapshot is None:
            raise PlanProposalNotFound()
        if snapshot.status == "processing":
            raise PlanSaveProcessing()
        if snapshot.status == "invalidated":
            raise PlanProposalInvalidated()
        if snapshot.status == "conflicted":
            profile = await self._repository.get_profile()
            conflict = self._plan_profile_conflict(profile, snapshot.base_profile_version, snapshot.preparation_kind)
            if conflict is not None:
                raise conflict
            raise PlanVersionConflict()
        bound = await self._check_plan_binding(context, branch, snapshot, arguments)
        await self._repository.begin_plan_save(snapshot.proposal_id, arguments.confirmation_entry_id)
        return snapshot, bound

    async def _check_plan_binding(
        self, context: BusinessContext, branch: list[SessionMessageEntry],
        snapshot: PlanSnapshot, arguments: PlanSaveArguments,
    ) -> PlanSnapshot:
        if snapshot.display_entry_id != arguments.display_entry_id:
            raise PlanConfirmationInvalid("展示节点与后端绑定不一致。")
        if snapshot.confirmation_entry_id not in {None, arguments.confirmation_entry_id}:
            raise PlanConfirmationInvalid("快照已绑定其他确认消息。")
        await ensure_confirmation_exclusive(
            self._repository, context.session_id, arguments.confirmation_entry_id,
            snapshot.proposal_id, PlanConfirmationInvalid, "确认消息已绑定其他快照。",
        )
        entries = entries_by_id(branch)
        for entry_id, role in [
            (snapshot.request_entry_id, "user"), (snapshot.source_entry_id, "assistant"),
            (arguments.confirmation_entry_id, "user"),
        ]:
            self._plan_role(entries, entry_id, role)
        await check_explicit_confirmation(
            self._repository, context.session_id, entries, arguments, "plan",
            PlanConfirmationInvalid,
        )
        display = entries.get(arguments.display_entry_id)
        if display is None:
            raise PlanConfirmationInvalid("展示节点须在当前消息路径中。")
        self._require_plan_result(display, snapshot, entries[snapshot.source_entry_id], entries)
        order = {entry.id: index for index, entry in enumerate(branch)}
        if not (order[snapshot.request_entry_id] < order[snapshot.source_entry_id]
                < order[display.id] < order[arguments.confirmation_entry_id]):
            raise PlanConfirmationInvalid("展示与确认节点时序不合法。")
        return snapshot.model_copy(update={
            "status": "processing", "confirmation_entry_id": arguments.confirmation_entry_id,
        })

    async def _commit_plan_save(
        self, snapshot: PlanSnapshot, bound: PlanSnapshot, context: BusinessContext,
    ) -> PlanSaveResult | BusinessError:
        if await self._repository.get_plan_snapshot(snapshot.proposal_id) != bound:
            raise PlanSaveProcessing()
        await self._check_plan_binding(
            context, await require_branch(self._sessions, context.session_id), bound,
            PlanSaveArguments(proposal_id=bound.proposal_id, display_entry_id=bound.display_entry_id,
                              confirmation_entry_id=bound.confirmation_entry_id))
        profile = await self._repository.get_profile()
        current = await self._repository.get_current_plan()
        conflict = self._plan_profile_conflict(profile, snapshot.base_profile_version, snapshot.preparation_kind)
        if conflict is None and snapshot.base_plan_id != (None if current is None else current.id):
            conflict = PlanVersionConflict()
        if conflict is not None:
            await self._repository.set_plan_snapshot_status(snapshot.proposal_id, "conflicted")
            return conflict
        content = self._validate_plan(snapshot.payload, None if profile is None else profile.content, snapshot.preparation_kind)
        saved_at = now_ms()
        record = PlanRecord(id=str(uuid4()), is_current=True, content=content, created_at=saved_at)
        await self._repository.insert_plan(record)
        result = PlanSaveResult(proposal_id=snapshot.proposal_id, id=record.id, content=content,
                                created_at=saved_at, saved_at=saved_at)
        await self._repository.complete_plan_save(PlanSaveRecord(
            proposal_id=snapshot.proposal_id, session_id=snapshot.session_id,
            display_entry_id=bound.display_entry_id, confirmation_entry_id=bound.confirmation_entry_id,
            result=result, saved_at=saved_at,
        ))
        return result

    async def get_plan_save_status(
        self, context: BusinessContext, proposal_id: str, signal: Event | None = None,
    ) -> PlanStatusResult:
        check_cancelled(signal)
        async with self._repository.transaction():
            await require_session(self._sessions, context.session_id)
            snapshot = await self._repository.get_plan_snapshot(proposal_id)
            record = await self._repository.get_plan_save_record(proposal_id)
            saved = self._plan_saved_result(context, snapshot, record)
            if saved is not None:
                return PlanStatusResult(proposal_id=proposal_id, status="saved", result=saved)
            if snapshot is None:
                raise PlanProposalNotFound()
            return PlanStatusResult(proposal_id=proposal_id, status=snapshot.status, result=None)

    async def list_plan_display_bindings(self, session_id: str) -> dict[str, str]:
        async with self._repository.transaction():
            await require_session(self._sessions, session_id)
            return {
                item.display_entry_id: item.proposal_id
                for item in await self._repository.list_plan_snapshots(session_id)
                if item.display_entry_id is not None and item.status in {"pending", "processing", "saved"}
            }

    async def list_plan_confirmation_bindings(self, session_id: str) -> dict[str, str]:
        async with self._repository.transaction():
            await require_session(self._sessions, session_id)
            bindings: dict[str, str] = {}
            for item in [*await self._repository.list_plan_snapshots(session_id),
                         *await self._repository.list_plan_save_records(session_id)]:
                entry_id = item.confirmation_entry_id
                if entry_id is not None:
                    if entry_id in bindings and bindings[entry_id] != item.proposal_id:
                        raise PlanConfirmationInvalid()
                    bindings[entry_id] = item.proposal_id
            return bindings
