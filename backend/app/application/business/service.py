import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
from threading import Event
from uuid import uuid4

from pydantic import ValidationError

from app.ai.messages import AssistantMessage, TextContent, ToolCall, ToolResultMessage
from app.ai.types import check_cancelled
from app.application.business.catalog import Catalog
from app.application.business.coordination import ReplacementCoordinator
from app.application.session.service import SessionService
from app.domain.business.errors import (
    BusinessError,
    CommitVetoed,
    InvalidBusinessPayload,
    PlanAccessDenied,
    PlanConfirmationInvalid,
    PlanNotFound,
    PlanProposalInvalidated,
    PlanProposalNotFound,
    PlanSaveProcessing,
    PlanVersionConflict,
    ProfileAccessDenied,
    ProfileConfirmationInvalid,
    ProfileProposalInvalidated,
    ProfileProposalNotFound,
    ProfileRequired,
    ProfileSessionNotFound,
    ProfileUpdateProcessing,
    ProfileVersionConflict,
    ProposalAlreadySaved,
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
    CatalogExercise,
    CurrentPlan,
    PendingProposalArguments,
    PlanAdjustmentArguments,
    PlanAdjustmentProposal,
    PlanContent,
    PlanImportArguments,
    PlanImportProposal,
    PlanPendingProposal,
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
    ProfilePendingProposal,
    ProfileProposal,
    ProfileProposalArguments,
    ProfileRecord,
    ProfileResponse,
    ProfileSaveArguments,
    ProfileSaveRecord,
    ProfileSaveResult,
    ProfileSnapshot,
    ProfileStatusResult,
    WorkoutContent,
    WorkoutListArguments,
    WorkoutListResult,
    WorkoutPendingProposal,
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
from app.domain.session.errors import SessionNotFound
from app.domain.session.models import SessionMessageEntry

BUSINESS_TIMEZONE = "Asia/Shanghai"
# ponytail: 固定 UTC+8 换算自然日；Asia/Shanghai 1991 年后无夏令时，覆盖全部业务日期，
# 若需更早历史精度再引入 tzdata。
_BUSINESS_OFFSET = timezone(timedelta(hours=8))

PREPARE_TOOL_NAME = "prepare_profile_update"


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


def business_date(created_at_ms: int) -> str:
    return datetime.fromtimestamp(created_at_ms / 1000, _BUSINESS_OFFSET).date().isoformat()


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _field_errors(error: ValidationError) -> list[BusinessFieldError]:
    results: list[BusinessFieldError] = []
    for item in error.errors():
        path = "/payload" + "".join(f"/{part}" for part in item.get("loc", ()))
        results.append(BusinessFieldError(path=path, message="字段取值不合法。"))
    return results


class BusinessService:
    def __init__(
        self,
        repository: BusinessRepository,
        catalog: Catalog,
        sessions: SessionService,
        replacements: ReplacementCoordinator,
    ) -> None:
        self._repository = repository
        self._catalog = catalog
        self._sessions = sessions
        self._replacements = replacements

    # 页面接口与工具共用的读取。

    async def get_profile(self) -> ProfileResponse:
        async with self._repository.transaction():
            profile = await self._repository.get_profile()
            if profile is None:
                return ProfileResponse(version=None, content=None)
            return ProfileResponse(version=profile.version, content=profile.content)

    def search_exercises(
        self,
        name: str,
        *,
        equipment: str | None = None,
        body_part: str | None = None,
        target: str | None = None,
        muscle_group: str | None = None,
    ) -> list[CatalogExercise]:
        return self._catalog.search(
            name,
            equipment=equipment,
            body_part=body_part,
            target=target,
            muscle_group=muscle_group,
        )

    # 准备工具：持久化完整不可变画像快照。

    async def prepare_profile_update(
        self,
        context: BusinessContext,
        arguments: ProfileProposalArguments,
        signal: Event | None = None,
    ) -> ProfileProposal:
        content = self._validate_profile(arguments.payload)
        check_cancelled(signal)
        async with self._repository.transaction():
            entries = self._entries(await self._require_branch(context.session_id))
            self._require_role(entries, context.request_entry_id, "user", "请求节点")
            self._require_role(entries, context.source_entry_id, "assistant", "来源节点")
            profile = await self._repository.get_profile()
            current_version = None if profile is None else profile.version
            if arguments.base_profile_version != current_version:
                raise ProfileVersionConflict()
            snapshot = ProfileSnapshot(
                proposal_id=str(uuid4()),
                session_id=context.session_id,
                request_entry_id=context.request_entry_id,
                source_entry_id=context.source_entry_id,
                profile_id=arguments.profile_id,
                base_profile_version=arguments.base_profile_version,
                payload=content,
                status="pending",
                created_at=_now_ms(),
            )
            await self._repository.insert_snapshot(snapshot)
            # 新快照创建成功即令同会话、同目标画像的旧待确认快照失效；本次事务失败时
            # 旧快照保持原状态。
            await self._repository.invalidate_pending(
                context.session_id, snapshot.profile_id, snapshot.proposal_id
            )
            return ProfileProposal(
                proposal_id=snapshot.proposal_id,
                profile_id=snapshot.profile_id,
                base_profile_version=snapshot.base_profile_version,
                payload=snapshot.payload,
            )

    # 展示绑定：准备结果节点持久化成功后由消息保存入口调用。

    async def bind_display_entry(self, proposal_id: str, display_entry_id: str) -> None:
        async with self._repository.transaction():
            await self._repository.bind_display_entry(proposal_id, display_entry_id)

    # 保存工具：确认关联先于保存事务落地，画像写入、saved 状态及固定结果原子提交。

    async def save_profile_update(
        self,
        context: BusinessContext,
        arguments: ProfileSaveArguments,
        signal: Event | None = None,
    ) -> ProfileSaveResult:
        return await self._save_with_replacement(
            context, arguments, signal, self._begin_save, self._commit_save,
            self._repository.set_snapshot_status, ProfileVersionConflict,
        )

    async def _save_with_replacement(
        self, context, arguments, signal, begin, commit, reset, conflict_type
    ):
        while True:
            check_cancelled(signal)
            async with self._repository.transaction():
                started = await begin(context, arguments)
            if isinstance(started, (ProfileSaveResult, WorkoutSaveResult, PlanSaveResult)):
                return started
            snapshot, bound = started
            try:
                async with self._repository.transaction(
                    lambda: self._replacements.pending_count(context.session_id) > 0
                ):
                    result = await commit(snapshot, bound, context)
            except CommitVetoed:
                check_cancelled(signal)
                # 让位给编辑与重新生成：撤回本次关联，按替换后的消息路径重新校验。
                async with self._repository.transaction():
                    await reset(snapshot.proposal_id, "pending")
                await self._replacements.wait_clear(context.session_id)
                continue
            if isinstance(result, BusinessError):
                raise result
            if result is not None:
                return result
            raise conflict_type()

    async def get_profile_update_status(
        self,
        context: BusinessContext,
        proposal_id: str,
        signal: Event | None = None,
    ) -> ProfileStatusResult:
        check_cancelled(signal)
        async with self._repository.transaction():
            await self._require_session(context.session_id)
            snapshot = await self._repository.get_snapshot(proposal_id)
            record = await self._repository.get_save_record(proposal_id)
            saved = self._saved_result(context, snapshot, record)
            if saved is not None:
                return ProfileStatusResult(
                    proposal_id=proposal_id, status="saved", result=saved
                )
            if snapshot is None:
                raise ProfileProposalNotFound()
            return ProfileStatusResult(
                proposal_id=proposal_id, status=snapshot.status, result=None
            )

    # 确认节点 → proposal_id：保存回复被重新生成后，原操作按此定位供状态查询。

    async def list_confirmation_bindings(self, session_id: str) -> dict[str, str]:
        async with self._repository.transaction():
            await self._require_session(session_id)
            bindings: dict[str, str] = {}
            for item in [
                *await self._repository.list_snapshots(session_id),
                *await self._repository.list_save_records(session_id),
            ]:
                entry_id = item.confirmation_entry_id
                if entry_id is None:
                    continue
                if entry_id in bindings and bindings[entry_id] != item.proposal_id:
                    raise ProfileConfirmationInvalid()
                bindings[entry_id] = item.proposal_id
            return bindings

    async def list_display_bindings(self, session_id: str) -> dict[str, str]:
        async with self._repository.transaction():
            await self._require_session(session_id)
            return {
                item.display_entry_id: item.proposal_id
                for item in await self._repository.list_snapshots(session_id)
                if item.display_entry_id is not None
                and item.status in {"pending", "processing", "saved"}
            }

    # 会话内容替换及整会话删除：快照随绑定的消息节点清理，幂等记录保留。

    async def remove_for_entries(
        self, session_id: str, entry_ids: set[str]
    ) -> None:
        await self._repository.delete_snapshots_for_entries(session_id, entry_ids)
        await self._repository.delete_workout_snapshots_for_entries(session_id, entry_ids)
        await self._repository.delete_plan_snapshots_for_entries(session_id, entry_ids)

    async def remove_for_session(self, session_id: str) -> None:
        await self._repository.delete_snapshots_for_session(session_id)
        await self._repository.delete_workout_snapshots_for_session(session_id)
        await self._repository.delete_plan_snapshots_for_session(session_id)

    # 内部协作。

    async def check_profile_save_authorization(
        self, context: BusinessContext, arguments: ProfileSaveArguments,
    ) -> None:
        async with self._repository.transaction():
            branch = await self._require_branch(context.session_id)
            snapshot = await self._repository.get_snapshot(arguments.proposal_id)
            record = await self._repository.get_save_record(arguments.proposal_id)
            if self._saved_result(context, snapshot, record) is not None:
                return
            if snapshot is None:
                raise ProfileProposalNotFound()
            await self._check_binding(context, branch, snapshot, arguments)

    async def _begin_save(
        self, context: BusinessContext, arguments: ProfileSaveArguments
    ) -> ProfileSaveResult | tuple[ProfileSnapshot, ProfileSnapshot]:
        branch = await self._require_branch(context.session_id)
        snapshot = await self._repository.get_snapshot(arguments.proposal_id)
        record = await self._repository.get_save_record(arguments.proposal_id)
        saved = self._saved_result(context, snapshot, record)
        if saved is not None:
            return saved
        if snapshot is None:
            raise ProfileProposalNotFound()
        if snapshot.status == "processing":
            raise ProfileUpdateProcessing()
        if snapshot.status == "invalidated":
            raise ProfileProposalInvalidated()
        if snapshot.status == "conflicted":
            raise ProfileVersionConflict()
        bound = await self._check_binding(context, branch, snapshot, arguments)
        await self._repository.begin_save(
            snapshot.proposal_id, arguments.confirmation_entry_id
        )
        return snapshot, bound

    async def _commit_save(
        self, snapshot: ProfileSnapshot, bound: ProfileSnapshot,
        context: BusinessContext,
    ) -> ProfileSaveResult | None:
        # 确认关联落地后重新读取：状态与绑定必须仍是本次操作。
        if await self._repository.get_snapshot(snapshot.proposal_id) != bound:
            raise ProfileUpdateProcessing()
        profile = await self._repository.get_profile()
        current_version = None if profile is None else profile.version
        if snapshot.base_profile_version != current_version:
            await self._repository.set_snapshot_status(snapshot.proposal_id, "conflicted")
            return None
        content = self._validate_profile(snapshot.payload)
        version = 1 if profile is None else profile.version + 1
        saved_at = _now_ms()
        await self._repository.save_profile(content, version, saved_at)
        result = ProfileSaveResult(
            proposal_id=snapshot.proposal_id,
            profile_id=snapshot.profile_id,
            version=version,
            content=content,
            saved_at=saved_at,
        )
        await self._repository.complete_save(
            ProfileSaveRecord(
                proposal_id=snapshot.proposal_id,
                session_id=snapshot.session_id,
                profile_id=snapshot.profile_id,
                display_entry_id=bound.display_entry_id,
                confirmation_entry_id=bound.confirmation_entry_id,
                result=result,
                saved_at=saved_at,
            )
        )
        return result

    def _saved_result(
        self,
        context: BusinessContext,
        snapshot: ProfileSnapshot | None,
        record: ProfileSaveRecord | None,
    ) -> ProfileSaveResult | None:
        # 幂等记录在会话及消息删除后仍是已完成保存的归属与固定结果来源。
        owner = snapshot if record is None else record
        if owner is None:
            return None
        if owner.session_id != context.session_id:
            raise ProfileAccessDenied()
        if record is not None:
            return record.result
        if snapshot.status == "saved":
            raise ProfileProposalNotFound("已保存快照缺少幂等记录。")
        return None

    async def _check_binding(
        self,
        context: BusinessContext,
        branch: list[SessionMessageEntry],
        snapshot: ProfileSnapshot,
        arguments: ProfileSaveArguments,
    ) -> ProfileSnapshot:
        if snapshot.display_entry_id != arguments.display_entry_id:
            # 未成功持久化的准备结果不能形成有效展示绑定。
            raise ProfileConfirmationInvalid("展示节点与后端绑定不一致。")
        if (
            snapshot.confirmation_entry_id is not None
            and snapshot.confirmation_entry_id != arguments.confirmation_entry_id
        ):
            raise ProfileConfirmationInvalid("该快照已绑定其他确认消息。")
        located = await self._repository.find_snapshot_by_confirmation(
            context.session_id, arguments.confirmation_entry_id
        )
        if located is not None and located.proposal_id != snapshot.proposal_id:
            # 一条确认消息只授权保存一个快照，原绑定保持不变。
            raise ProfileConfirmationInvalid("该确认消息已绑定其他快照。")
        for located in [
            await self._repository.find_save_record_by_confirmation(
                context.session_id, arguments.confirmation_entry_id
            ),
            await self._repository.find_workout_snapshot_by_confirmation(
                context.session_id, arguments.confirmation_entry_id
            ),
            await self._repository.find_workout_save_record_by_confirmation(
                context.session_id, arguments.confirmation_entry_id
            ),
            await self._repository.find_plan_snapshot_by_confirmation(
                context.session_id, arguments.confirmation_entry_id
            ),
            await self._repository.find_plan_save_record_by_confirmation(
                context.session_id, arguments.confirmation_entry_id
            ),
        ]:
            if located is not None and located.proposal_id != snapshot.proposal_id:
                raise ProfileConfirmationInvalid("该确认消息已绑定其他快照。")
        entries = self._entries(branch)
        self._require_role(entries, snapshot.source_entry_id, "assistant", "来源节点")
        display = entries.get(arguments.display_entry_id)
        confirmation = entries.get(arguments.confirmation_entry_id)
        if display is None or confirmation is None:
            raise ProfileConfirmationInvalid("展示及确认节点须在当前消息路径中。")
        if confirmation.messages[0].role != "user":
            raise ProfileConfirmationInvalid("确认节点必须是用户消息。")
        self._require_prepare_result(
            display, entries[snapshot.source_entry_id], entries
        )
        order = {entry.id: index for index, entry in enumerate(branch)}
        if order[confirmation.id] <= order[display.id]:
            raise ProfileConfirmationInvalid("确认消息必须晚于完整画像展示消息。")
        return snapshot.model_copy(
            update={
                "status": "processing",
                "confirmation_entry_id": arguments.confirmation_entry_id,
            }
        )

    def _require_prepare_result(
        self,
        display: SessionMessageEntry,
        source: SessionMessageEntry,
        entries: dict[str, SessionMessageEntry],
    ) -> None:
        self._require_batch_result(display, source, entries, ProfileConfirmationInvalid)
        result = display.messages[0]
        if result.tool_name != PREPARE_TOOL_NAME or result.is_error:
            raise ProfileConfirmationInvalid("展示节点不是该准备工具的结果节点。")

    def _require_batch_result(
        self, display: SessionMessageEntry, source: SessionMessageEntry,
        entries: dict[str, SessionMessageEntry], error_type: type[BusinessError],
    ) -> None:
        path = list(entries.values())
        order = {entry.id: index for index, entry in enumerate(path)}
        if source.id not in order or display.id not in order or order[source.id] >= order[display.id]:
            raise error_type("来源与展示节点须在当前消息路径中且时序有效。")
        assistant = source.messages[0]
        if not isinstance(assistant, AssistantMessage):
            raise error_type("来源节点不是助手消息。")
        calls = [block for block in assistant.content if isinstance(block, ToolCall)]
        if len({call.id for call in calls}) != len(calls):
            raise error_type("来源批次调用标识重复。")
        results = path[order[source.id] + 1:order[display.id] + 1]
        if len(results) > len(calls):
            raise error_type("展示节点超出来源工具批次。")
        parent_id = source.id
        for entry, call in zip(results, calls):
            message = entry.messages[0]
            if (entry.parent_id != parent_id or not isinstance(message, ToolResultMessage)
                    or message.tool_call_id != call.id or message.tool_name != call.name):
                raise error_type("展示路径与来源批次调用顺序或结果配对不一致。")
            parent_id = entry.id

    def _entries(
        self, branch: list[SessionMessageEntry]
    ) -> dict[str, SessionMessageEntry]:
        return {entry.id: entry for entry in branch}

    def _require_role(
        self,
        entries: dict[str, SessionMessageEntry],
        entry_id: str,
        role: str,
        label: str,
    ) -> None:
        entry = entries.get(entry_id)
        if entry is None:
            raise ProfileConfirmationInvalid(f"{label}不在当前消息路径中。")
        if entry.messages[0].role != role:
            raise ProfileConfirmationInvalid(f"{label}必须是{role}消息。")

    async def _require_session(self, session_id: str) -> None:
        try:
            await self._sessions.get_session(session_id)
        except SessionNotFound as error:
            raise ProfileSessionNotFound() from error

    async def _require_branch(
        self, session_id: str
    ) -> list[SessionMessageEntry]:
        await self._require_session(session_id)
        return await self._sessions.get_current_branch(session_id)

    def _validate_content(self, model, payload):
        try:
            return model.model_validate(payload)
        except ValidationError as error:
            raise InvalidBusinessPayload(_field_errors(error)) from error

    def _validate_profile(self, payload: object) -> ProfileContent:
        content = self._validate_content(ProfileContent, payload)
        failures: list[BusinessFieldError] = []
        if content.unavailable_equipment is not None:
            for index, item in enumerate(content.unavailable_equipment):
                if item not in self._catalog.equipment:
                    failures.append(
                        BusinessFieldError(
                            path=f"/payload/unavailable_equipment/{index}",
                            message="器械标识必须存在于动作目录中。",
                        )
                    )
        if content.forbidden_exercise_ids is not None:
            for index, item in enumerate(content.forbidden_exercise_ids):
                if item not in self._catalog.exercise_ids:
                    failures.append(
                        BusinessFieldError(
                            path=f"/payload/forbidden_exercise_ids/{index}",
                            message="动作 ID 必须存在于动作目录中。",
                        )
                    )
        if failures:
            raise InvalidBusinessPayload(failures)
        return content

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
        content = self._validate_content(PlanContent, payload)
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
        entry = entries.get(entry_id)
        if entry is None or entry.messages[0].role != role:
            raise PlanConfirmationInvalid("消息节点角色或当前路径不合法。")

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
            entries = self._entries(await self._require_branch(context.session_id))
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
                status="pending", created_at=_now_ms(),
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
        self._require_batch_result(display, source, entries, PlanConfirmationInvalid)
        result = display.messages[0]
        tool_name = {"generation": "prepare_plan", "import": "prepare_plan_import",
                     "adjustment": "prepare_plan_adjustment"}[snapshot.preparation_kind]
        if result.is_error or result.tool_name != tool_name:
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
            entries = self._entries(await self._require_branch(snapshot.session_id))
            self._plan_role(entries, snapshot.source_entry_id, "assistant")
            display = entries.get(display_entry_id)
            if display is None:
                raise PlanConfirmationInvalid("展示节点须在当前消息路径中。")
            self._require_plan_result(display, snapshot, entries[snapshot.source_entry_id], entries)
            await self._repository.bind_plan_display_entry(proposal_id, display_entry_id)

    def _plan_saved_result(
        self, context: BusinessContext, snapshot: PlanSnapshot | None, record: PlanSaveRecord | None,
    ) -> PlanSaveResult | None:
        owner = snapshot if record is None else record
        if owner is None:
            return None
        if owner.session_id != context.session_id:
            raise PlanAccessDenied()
        if record is not None:
            return record.result
        if snapshot.status == "saved":
            raise PlanProposalNotFound("已保存快照缺少幂等记录。")
        return None

    async def save_plan(
        self, context: BusinessContext, arguments: PlanSaveArguments,
        signal: Event | None = None,
    ) -> PlanSaveResult:
        return await self._save_with_replacement(
            context, arguments, signal, self._begin_plan_save, self._commit_plan_save,
            self._repository.set_plan_snapshot_status, PlanVersionConflict,
        )

    async def check_plan_save_authorization(
        self, context: BusinessContext, arguments: PlanSaveArguments,
    ) -> None:
        async with self._repository.transaction():
            branch = await self._require_branch(context.session_id)
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
        branch = await self._require_branch(context.session_id)
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
        for located in [
            await self._repository.find_snapshot_by_confirmation(context.session_id, arguments.confirmation_entry_id),
            await self._repository.find_save_record_by_confirmation(context.session_id, arguments.confirmation_entry_id),
            await self._repository.find_workout_snapshot_by_confirmation(context.session_id, arguments.confirmation_entry_id),
            await self._repository.find_workout_save_record_by_confirmation(context.session_id, arguments.confirmation_entry_id),
            await self._repository.find_plan_snapshot_by_confirmation(context.session_id, arguments.confirmation_entry_id),
            await self._repository.find_plan_save_record_by_confirmation(context.session_id, arguments.confirmation_entry_id),
        ]:
            if located is not None and located.proposal_id != snapshot.proposal_id:
                raise PlanConfirmationInvalid("确认消息已绑定其他快照。")
        entries = self._entries(branch)
        for entry_id, role in [
            (snapshot.request_entry_id, "user"), (snapshot.source_entry_id, "assistant"),
            (context.request_entry_id, "user"), (context.source_entry_id, "assistant"),
            (arguments.confirmation_entry_id, "user"),
        ]:
            self._plan_role(entries, entry_id, role)
        display = entries.get(arguments.display_entry_id)
        if display is None:
            raise PlanConfirmationInvalid("展示节点须在当前消息路径中。")
        self._require_plan_result(display, snapshot, entries[snapshot.source_entry_id], entries)
        order = {entry.id: index for index, entry in enumerate(branch)}
        if not (order[snapshot.request_entry_id] < order[snapshot.source_entry_id]
                < order[display.id] < order[arguments.confirmation_entry_id]
                <= order[context.request_entry_id] < order[context.source_entry_id]):
            raise PlanConfirmationInvalid("展示、确认及当前请求时序不合法。")
        return snapshot.model_copy(update={
            "status": "processing", "confirmation_entry_id": arguments.confirmation_entry_id,
        })

    async def _commit_plan_save(
        self, snapshot: PlanSnapshot, bound: PlanSnapshot, context: BusinessContext,
    ) -> PlanSaveResult | BusinessError:
        if await self._repository.get_plan_snapshot(snapshot.proposal_id) != bound:
            raise PlanSaveProcessing()
        await self._check_plan_binding(context, await self._require_branch(context.session_id), bound,
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
        saved_at = _now_ms()
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
            await self._require_session(context.session_id)
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
            await self._require_session(session_id)
            return {
                item.display_entry_id: item.proposal_id
                for item in await self._repository.list_plan_snapshots(session_id)
                if item.display_entry_id is not None and item.status in {"pending", "processing", "saved"}
            }

    async def list_plan_confirmation_bindings(self, session_id: str) -> dict[str, str]:
        async with self._repository.transaction():
            await self._require_session(session_id)
            bindings: dict[str, str] = {}
            for item in [*await self._repository.list_plan_snapshots(session_id),
                         *await self._repository.list_plan_save_records(session_id)]:
                entry_id = item.confirmation_entry_id
                if entry_id is not None:
                    if entry_id in bindings and bindings[entry_id] != item.proposal_id:
                        raise PlanConfirmationInvalid()
                    bindings[entry_id] = item.proposal_id
            return bindings

    async def get_workout(self, workout_id: str) -> WorkoutRecord:
        async with self._repository.transaction():
            record = await self._repository.get_workout(workout_id)
            if record is None:
                raise WorkoutNotFound()
            return record

    async def list_workouts(self, arguments: WorkoutListArguments) -> WorkoutListResult:
        return await self._repository.list_workouts(arguments)

    def _validate_workout(self, payload: object) -> WorkoutContent:
        content = self._validate_content(WorkoutContent, payload)
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
        entry = entries.get(entry_id)
        if entry is None or entry.messages[0].role != role:
            raise WorkoutConfirmationInvalid("消息节点角色或当前路径不合法。")

    async def prepare_workout(
        self, context: BusinessContext, arguments: WorkoutProposalArguments,
        signal: Event | None = None,
    ) -> WorkoutProposal:
        content = self._validate_workout(arguments.payload)
        self._check_workout_date(context, arguments.performed_on)
        check_cancelled(signal)
        async with self._repository.transaction():
            entries = self._entries(await self._require_branch(context.session_id))
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
                status="pending", created_at=_now_ms(),
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
            entries = self._entries(await self._require_branch(snapshot.session_id))
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
        self._require_batch_result(display, source, entries, WorkoutConfirmationInvalid)
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
        signal: Event | None = None,
    ) -> WorkoutSaveResult:
        return await self._save_with_replacement(
            context, arguments, signal,
            partial(self._begin_workout_save, updating=False), self._commit_workout_save,
            self._repository.set_workout_snapshot_status, WorkoutVersionConflict,
        )

    async def update_workout(
        self, context: BusinessContext, arguments: WorkoutSaveArguments,
        signal: Event | None = None,
    ) -> WorkoutSaveResult:
        return await self._save_with_replacement(
            context, arguments, signal,
            partial(self._begin_workout_save, updating=True), self._commit_workout_save,
            self._repository.set_workout_snapshot_status, WorkoutVersionConflict,
        )

    def _workout_saved_result(
        self, context: BusinessContext, snapshot: WorkoutSnapshot | None,
        record: WorkoutSaveRecord | None,
    ) -> WorkoutSaveResult | None:
        owner = snapshot if record is None else record
        if owner is None:
            return None
        if owner.session_id != context.session_id:
            raise WorkoutAccessDenied()
        if record is not None:
            return record.result
        if snapshot.status == "saved":
            raise WorkoutProposalNotFound("已保存快照缺少幂等记录。")
        return None

    async def check_workout_save_authorization(
        self, context: BusinessContext, arguments: WorkoutSaveArguments, *, updating: bool,
    ) -> None:
        async with self._repository.transaction():
            branch = await self._require_branch(context.session_id)
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
        branch = await self._require_branch(context.session_id)
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
        for located in [
            await self._repository.find_snapshot_by_confirmation(context.session_id, arguments.confirmation_entry_id),
            await self._repository.find_save_record_by_confirmation(context.session_id, arguments.confirmation_entry_id),
            await self._repository.find_workout_snapshot_by_confirmation(context.session_id, arguments.confirmation_entry_id),
            await self._repository.find_workout_save_record_by_confirmation(context.session_id, arguments.confirmation_entry_id),
            await self._repository.find_plan_snapshot_by_confirmation(context.session_id, arguments.confirmation_entry_id),
            await self._repository.find_plan_save_record_by_confirmation(context.session_id, arguments.confirmation_entry_id),
        ]:
            if located is not None and located.proposal_id != snapshot.proposal_id:
                raise WorkoutConfirmationInvalid("确认消息已绑定其他快照。")
        entries = self._entries(branch)
        for entry_id, role in [
            (snapshot.request_entry_id, "user"), (snapshot.source_entry_id, "assistant"),
            (context.request_entry_id, "user"), (context.source_entry_id, "assistant"),
            (arguments.confirmation_entry_id, "user"),
        ]:
            self._workout_role(entries, entry_id, role)
        display = entries.get(arguments.display_entry_id)
        if display is None:
            raise WorkoutConfirmationInvalid("展示节点须在当前消息路径中。")
        self._require_workout_result(display, snapshot, entries[snapshot.source_entry_id], entries)
        order = {entry.id: index for index, entry in enumerate(branch)}
        if not (order[display.id] < order[arguments.confirmation_entry_id]
                <= order[context.request_entry_id] < order[context.source_entry_id]):
            raise WorkoutConfirmationInvalid("展示、确认及当前请求时序不合法。")
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
            context, await self._require_branch(context.session_id), bound, arguments
        )
        current = await self._repository.get_workout_by_date(snapshot.performed_on)
        if not self._workout_base_matches(snapshot, current):
            await self._repository.set_workout_snapshot_status(snapshot.proposal_id, "conflicted")
            return None
        content = self._validate_workout(snapshot.payload)
        saved_at = _now_ms()
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
            await self._require_session(context.session_id)
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
            await self._require_session(session_id)
            return {
                item.display_entry_id: item.proposal_id
                for item in await self._repository.list_workout_snapshots(session_id)
                if item.display_entry_id is not None and item.status in {"pending", "processing", "saved"}
            }

    async def list_workout_confirmation_bindings(self, session_id: str) -> dict[str, str]:
        async with self._repository.transaction():
            await self._require_session(session_id)
            bindings: dict[str, str] = {}
            for item in [*await self._repository.list_workout_snapshots(session_id),
                         *await self._repository.list_workout_save_records(session_id)]:
                entry_id = item.confirmation_entry_id
                if entry_id is not None:
                    if entry_id in bindings and bindings[entry_id] != item.proposal_id:
                        raise WorkoutConfirmationInvalid()
                    bindings[entry_id] = item.proposal_id
            return bindings

    # 本轮待确认提案引用：只读聚合三类仍待确认的提案身份与关联节点，供模型投影使用。

    async def list_pending_references(self, session_id: str) -> list[PendingProposalReference]:
        async with self._repository.transaction():
            await self._require_session(session_id)
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
            entries = self._entries(await self._require_branch(context.session_id))
            if arguments.business_kind == "profile":
                return await self._pending_profile(context, entries, arguments.proposal_id)
            if arguments.business_kind == "plan":
                return await self._pending_plan(context, entries, arguments.proposal_id)
            return await self._pending_workout(context, entries, arguments.proposal_id)

    async def _pending_profile(
        self, context: BusinessContext, entries: dict[str, SessionMessageEntry], proposal_id: str,
    ) -> ProfilePendingProposal:
        snapshot = await self._repository.get_snapshot(proposal_id)
        if snapshot is None:
            raise ProfileProposalNotFound()
        if snapshot.session_id != context.session_id:
            raise ProfileAccessDenied()
        self._require_role(entries, snapshot.request_entry_id, "user", "请求节点")
        self._require_role(entries, snapshot.source_entry_id, "assistant", "来源节点")
        if snapshot.display_entry_id is not None:
            self._require_role(entries, snapshot.display_entry_id, "toolResult", "展示节点")
        self._pending_status(
            snapshot.status, ProfileUpdateProcessing,
            ProfileProposalInvalidated, ProfileVersionConflict,
        )
        display_id = snapshot.display_entry_id
        if display_id is None:
            raise ProfileConfirmationInvalid("展示节点尚未绑定。")
        self._require_prepare_result(
            entries[display_id], entries[snapshot.source_entry_id], entries
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
        self._plan_role(entries, snapshot.request_entry_id, "user")
        self._plan_role(entries, snapshot.source_entry_id, "assistant")
        if snapshot.display_entry_id is not None:
            self._plan_role(entries, snapshot.display_entry_id, "toolResult")
        self._pending_status(
            snapshot.status, PlanSaveProcessing,
            PlanProposalInvalidated, PlanVersionConflict,
        )
        display_id = snapshot.display_entry_id
        if display_id is None:
            raise PlanConfirmationInvalid("展示节点尚未绑定。")
        self._require_plan_result(
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
        self._workout_role(entries, snapshot.request_entry_id, "user")
        self._workout_role(entries, snapshot.source_entry_id, "assistant")
        if snapshot.display_entry_id is not None:
            self._workout_role(entries, snapshot.display_entry_id, "toolResult")
        self._pending_status(
            snapshot.status, WorkoutSaveProcessing,
            WorkoutProposalInvalidated, WorkoutVersionConflict,
        )
        display_id = snapshot.display_entry_id
        if display_id is None:
            raise WorkoutConfirmationInvalid("展示节点尚未绑定。")
        self._require_workout_result(
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
