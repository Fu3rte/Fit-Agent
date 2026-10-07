import time
from datetime import datetime, timedelta, timezone
from threading import Event
from uuid import uuid4

from pydantic import ValidationError

from app.ai.messages import AssistantMessage, ToolCall, ToolResultMessage
from app.ai.types import check_cancelled
from app.application.business.catalog import Catalog
from app.application.business.coordination import ReplacementCoordinator
from app.application.session.service import SessionService
from app.domain.business.errors import (
    CommitVetoed,
    InvalidBusinessPayload,
    ProfileAccessDenied,
    ProfileConfirmationInvalid,
    ProfileProposalInvalidated,
    ProfileProposalNotFound,
    ProfileSessionNotFound,
    ProfileUpdateProcessing,
    ProfileVersionConflict,
)
from app.domain.business.models import (
    BusinessContext,
    BusinessFieldError,
    CatalogExercise,
    ProfileContent,
    ProfileProposal,
    ProfileProposalArguments,
    ProfileResponse,
    ProfileSaveArguments,
    ProfileSaveRecord,
    ProfileSaveResult,
    ProfileSnapshot,
    ProfileStatusResult,
)
from app.domain.business.repository import BusinessRepository
from app.domain.session.errors import SessionNotFound
from app.domain.session.models import SessionMessageEntry

BUSINESS_TIMEZONE = "Asia/Shanghai"
# ponytail: 固定 UTC+8 换算自然日；Asia/Shanghai 1991 年后无夏令时，覆盖全部业务日期，
# 若需更早历史精度再引入 tzdata。
_BUSINESS_OFFSET = timezone(timedelta(hours=8))

PREPARE_TOOL_NAME = "prepare_profile_update"


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
        while True:
            check_cancelled(signal)
            async with self._repository.transaction():
                started = await self._begin_save(context, arguments)
            if isinstance(started, ProfileSaveResult):
                return started
            snapshot, bound = started
            try:
                async with self._repository.transaction(
                    lambda: self._replacements.pending_count(context.session_id) > 0
                ):
                    result = await self._commit_save(snapshot, bound)
            except CommitVetoed:
                check_cancelled(signal)
                # 让位给编辑与重新生成：撤回本次关联，按替换后的消息路径重新校验。
                async with self._repository.transaction():
                    await self._repository.set_snapshot_status(
                        snapshot.proposal_id, "pending"
                    )
                await self._replacements.wait_clear(context.session_id)
                continue
            if result is not None:
                return result
            raise ProfileVersionConflict()

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

    async def remove_for_session(self, session_id: str) -> None:
        await self._repository.delete_snapshots_for_session(session_id)

    # 内部协作。

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
        self, snapshot: ProfileSnapshot, bound: ProfileSnapshot
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
        entries = self._entries(branch)
        self._require_role(entries, snapshot.source_entry_id, "assistant", "来源节点")
        display = entries.get(arguments.display_entry_id)
        confirmation = entries.get(arguments.confirmation_entry_id)
        if display is None or confirmation is None:
            raise ProfileConfirmationInvalid("展示及确认节点须在当前消息路径中。")
        if confirmation.messages[0].role != "user":
            raise ProfileConfirmationInvalid("确认节点必须是用户消息。")
        self._require_prepare_result(
            display, snapshot, entries[snapshot.source_entry_id]
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
        snapshot: ProfileSnapshot,
        source: SessionMessageEntry,
    ) -> None:
        result = display.messages[0]
        if (
            not isinstance(result, ToolResultMessage)
            or result.tool_name != PREPARE_TOOL_NAME
            or result.is_error
            or display.parent_id != snapshot.source_entry_id
        ):
            raise ProfileConfirmationInvalid("展示节点不是该准备工具的结果节点。")
        assistant = source.messages[0]
        if not isinstance(assistant, AssistantMessage):
            raise ProfileConfirmationInvalid("来源节点不是助手消息。")
        call = next(
            (
                block
                for block in assistant.content
                if isinstance(block, ToolCall) and block.id == result.tool_call_id
            ),
            None,
        )
        if call is None or call.name != PREPARE_TOOL_NAME:
            raise ProfileConfirmationInvalid("展示节点与准备工具调用不配对。")

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

    def _validate_profile(self, payload: object) -> ProfileContent:
        try:
            content = ProfileContent.model_validate(payload)
        except ValidationError as error:
            raise InvalidBusinessPayload(_field_errors(error)) from error
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
