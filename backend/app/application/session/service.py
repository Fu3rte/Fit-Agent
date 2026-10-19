from collections.abc import Sequence
from pathlib import Path

from app.ai.messages import SystemMessage, UserMessage
from app.application.session.attachments import SessionAttachments
from app.application.session.content_removal import ContentRemoval, SnapshotStore
from app.application.session.credentials import reject_credentials
from app.application.session.entries import SessionEntries
from app.application.session.operations import SessionOperations
from app.application.session.positions import new_id, new_run, now_ms, set_leaf
from app.application.session.queries import SessionQueries
from app.application.session.runs import SessionRuns
from app.domain.session.attachments import (
    PROJECT_ROOT,
    AttachmentError,
    AttachmentReference,
)
from app.domain.session.errors import (
    InvalidTargetEntry,
    RunClosed,
    SessionConflict,
)
from app.domain.session.models import (
    EditCommand,
    OperationOutcome,
    RegenerateCommand,
    SendCommand,
    Session,
    SessionMessageEntry,
    SteeringCommand,
    SteeringInput,
)
from app.domain.session.repository import SessionRepository


class SessionService:
    def __init__(
        self,
        repository: SessionRepository,
        snapshots: SnapshotStore | None = None,
        *,
        project_root: Path | None = None,
    ) -> None:
        self._repository = repository
        self._snapshots = snapshots
        self._project_root = project_root or PROJECT_ROOT
        self.queries = SessionQueries(repository)
        self.runs = SessionRuns(repository, self.queries)
        self.entries = SessionEntries(repository, self.queries, self.runs)
        self.operations = SessionOperations(repository, self.queries)
        self.attachments = SessionAttachments(
            repository, self.queries, project_root=self._project_root
        )
        self.removal = ContentRemoval(repository)
        if snapshots is not None:
            self.removal.attach_snapshots(snapshots)

    def attach_snapshots(self, snapshots: SnapshotStore) -> None:
        self._snapshots = snapshots
        self.removal.attach_snapshots(snapshots)

    async def create_session(
        self,
        session_id: str,
        title: str,
        *,
        credentials: Sequence[str] = (),
    ) -> Session:
        _, session = await self.create_session_result(
            session_id, title, credentials=credentials
        )
        return session

    async def create_session_result(
        self,
        session_id: str,
        title: str,
        *,
        credentials: Sequence[str] = (),
    ) -> tuple[bool, Session]:
        reject_credentials(
            {"session_id": session_id, "title": title}, credentials
        )
        now = now_ms()
        async with self._repository.transaction():
            existing = await self._repository.get_session(session_id)
            if existing is not None:
                if existing.title != title:
                    raise SessionConflict(f"会话已存在: {session_id}")
                return False, existing
            session = Session(
                id=session_id,
                title=title,
                active_leaf_id=None,
                created_at=now,
                updated_at=now,
            )
            await self._repository.insert_session(session)
        return True, session

    async def delete_session(self, session_id: str) -> None:
        # 同一事务内删除会话的全部画像快照，再清除会话及全部关联记录；
        # 已完成保存的幂等记录保留，已保存业务数据保持不动。
        async with self._repository.transaction():
            if await self._repository.get_session(session_id) is None:
                return
            await self._repository.defer_foreign_keys()
            if self._snapshots is not None:
                await self._snapshots.remove_for_session(session_id)
            await self._repository.delete_session(session_id)

    async def accept_send(
        self,
        command: SendCommand,
        *,
        system_message: SystemMessage,
        credentials: Sequence[str] = (),
        now: int | None = None,
    ) -> OperationOutcome:
        request = command.request
        reject_credentials(command.model_dump(exclude_unset=True), credentials)
        reject_credentials(
            system_message.model_dump(exclude_unset=True), credentials
        )
        async with self.attachments.attachment_transaction(
            command, "send", credentials
        ) as created:
            resolved = await self.operations.resolve_operation(
                command.operation_id, command.session_id, "send", request, credentials=credentials
            )
            if resolved is not None:
                return resolved
            await self.queries.ensure_idle()
            session = await self.queries.require_session(command.session_id)
            entries = await self._repository.list_entries(command.session_id)
            await self.queries.ensure_pairable(
                command.session_id, entries, session.active_leaf_id
            )
            now = now_ms() if now is None else now
            attachments = await self.attachments.accept_attachments(
                command.session_id, request.attachments, created, now, credentials,
            )
            parent_id = session.active_leaf_id
            user_timestamp = now
            if parent_id is None:
                system_entry = SessionMessageEntry(
                    session_id=command.session_id,
                    id=new_id(),
                    parent_id=None,
                    run_id=None,
                    type="message",
                    messages=[system_message],
                    created_at=now,
                )
                await self._repository.insert_entry(system_entry)
                parent_id = system_entry.id
                user_timestamp = system_message.timestamp
            user_entry = SessionMessageEntry(
                session_id=command.session_id,
                id=new_id(),
                parent_id=parent_id,
                run_id=None,
                type="message",
                messages=[
                    UserMessage(
                        role="user", content=request.text, timestamp=user_timestamp
                    )
                ],
                created_at=now,
            )
            run = new_run(command.session_id, user_entry.id, now)
            await self._repository.insert_entry(user_entry)
            await self._repository.bind_entry_attachments(
                command.session_id, user_entry.id, [item.attachment_id for item in attachments],
            )
            await self._repository.insert_run(run)
            operation = await self.operations.record_operation(
                command.operation_id, command.session_id, "send", request, run.id,
                accepted_attachments=attachments,
            )
            await set_leaf(self._repository, session, user_entry.id, now)
            return OperationOutcome(
                created=True, operation=operation, run=run, steering=None
            )

    async def accept_edit(
        self,
        command: EditCommand,
        *,
        credentials: Sequence[str] = (),
    ) -> OperationOutcome:
        # 编辑：删除目标用户消息及其全部后续内容，在原父节点下保存修改后的用户节点。
        request = command.request
        reject_credentials(command.model_dump(exclude_unset=True), credentials)
        async with self.attachments.attachment_transaction(
            command, "edit", credentials
        ) as created:
            resolved = await self.operations.resolve_operation(
                command.operation_id, command.session_id, "edit", request, credentials=credentials
            )
            if resolved is not None:
                return resolved
            await self.queries.ensure_idle()
            session = await self.queries.require_session(command.session_id)
            target = await self.queries.require_message_entry(
                command.session_id, request.target_entry_id
            )
            if target.messages[0].role != "user":
                raise InvalidTargetEntry("编辑目标必须是同会话用户消息节点")
            entries = await self._repository.list_entries(command.session_id)
            await self.queries.ensure_pairable(
                command.session_id, entries, target.parent_id
            )
            now = now_ms()
            inputs = request.attachments
            if "attachments" not in request.model_fields_set:
                inputs = [AttachmentReference(kind="reference", attachment_id=item.attachment_id)
                          for item in await self._repository.list_entry_attachments(command.session_id, target.id)]
            if not inputs and not request.text.strip():
                raise AttachmentError("invalid_request", "请求必须包含文字或附件")
            attachments = await self.attachments.accept_attachments(
                command.session_id, inputs, created, now, credentials,
            )
            await self.removal.remove_content(
                command.session_id, target.id, entries, include_target=True
            )
            user_entry = SessionMessageEntry(
                session_id=command.session_id,
                id=new_id(),
                parent_id=target.parent_id,
                run_id=None,
                type="message",
                messages=[
                    UserMessage(role="user", content=request.text, timestamp=now)
                ],
                created_at=now,
            )
            run = new_run(command.session_id, user_entry.id, now)
            await self._repository.insert_entry(user_entry)
            await self._repository.bind_entry_attachments(
                command.session_id, user_entry.id, [item.attachment_id for item in attachments],
            )
            await self._repository.insert_run(run)
            operation = await self.operations.record_operation(
                command.operation_id, command.session_id, "edit", request, run.id,
                accepted_attachments=attachments,
            )
            await set_leaf(self._repository, session, user_entry.id, now)
            return OperationOutcome(
                created=True, operation=operation, run=run, steering=None
            )

    async def accept_regenerate(
        self,
        command: RegenerateCommand,
        *,
        credentials: Sequence[str] = (),
    ) -> OperationOutcome:
        # 重新生成：保留目标用户节点及其祖先，删除目标之后的全部内容。
        request = command.request
        reject_credentials(command.model_dump(exclude_unset=True), credentials)
        async with self._repository.transaction():
            resolved = await self.operations.resolve_operation(
                command.operation_id, command.session_id, "regenerate", request, credentials=credentials
            )
            if resolved is not None:
                return resolved
            await self.queries.ensure_idle()
            session = await self.queries.require_session(command.session_id)
            target = await self.queries.require_message_entry(
                command.session_id, request.target_entry_id
            )
            if target.messages[0].role != "user":
                raise InvalidTargetEntry("重新生成目标必须是同会话用户消息节点")
            if target.confirmation is not None:
                raise InvalidTargetEntry("结构化确认节点不支持模型重新生成。")
            reject_credentials(target.model_dump(), credentials)
            attachments = await self.attachments.accept_attachments(
                command.session_id,
                [AttachmentReference(kind="reference", attachment_id=item.attachment_id) for item in target.attachments],
                [], now_ms(), credentials,
            )
            entries = await self._repository.list_entries(command.session_id)
            await self.queries.ensure_pairable(command.session_id, entries, target.id)
            await self.removal.remove_content(
                command.session_id, target.id, entries, include_target=False
            )
            now = now_ms()
            run = new_run(command.session_id, target.id, now)
            await self._repository.insert_run(run)
            operation = await self.operations.record_operation(
                command.operation_id,
                command.session_id,
                "regenerate",
                request,
                run.id,
                accepted_attachments=attachments,
            )
            await set_leaf(self._repository, session, target.id, now)
            return OperationOutcome(
                created=True, operation=operation, run=run, steering=None
            )

    async def accept_steering(
        self,
        command: SteeringCommand,
        *,
        credentials: Sequence[str] = (),
    ) -> OperationOutcome:
        request = command.request
        reject_credentials(command.model_dump(exclude_unset=True), credentials)
        async with self.attachments.attachment_transaction(
            command, "steering", credentials
        ) as created:
            resolved = await self.operations.resolve_operation(
                command.operation_id, command.session_id, "steering", request, credentials=credentials
            )
            if resolved is not None:
                return resolved
            await self.queries.require_session(command.session_id)
            run = await self.queries.require_run(
                command.session_id, request.target_run_id
            )
            if run.status != "running":
                raise RunClosed("目标运行未在接受输入")
            now = now_ms()
            attachments = await self.attachments.accept_attachments(
                command.session_id, request.attachments, created, now, credentials,
            )
            steering = SteeringInput(
                session_id=command.session_id,
                id=new_id(),
                run_id=run.id,
                message=UserMessage(
                    role="user", content=request.text, timestamp=now
                ),
                status="pending",
                entry_id=None,
                reason=None,
                created_at=now,
                updated_at=now,
            )
            await self._repository.insert_steering(steering)
            await self._repository.bind_steering_attachments(
                command.session_id, steering.id, [item.attachment_id for item in attachments],
            )
            operation = await self.operations.record_operation(
                command.operation_id,
                command.session_id,
                "steering",
                request,
                run.id,
                steering_id=steering.id,
                accepted_attachments=attachments,
            )
            steering.attachments = attachments
            return OperationOutcome(
                created=True, operation=operation, run=None, steering=steering
            )
