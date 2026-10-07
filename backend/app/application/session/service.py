import time
import uuid
from collections.abc import Sequence
from typing import Protocol

from app.ai.context import validate_tool_pairs
from app.ai.messages import Message, SystemMessage, UserMessage, serialize_message
from app.domain.session.branch import build_session_context, build_session_path
from app.domain.session.errors import (
    CredentialDetected,
    EntryNotFound,
    IncompleteToolChain,
    InvalidTargetEntry,
    OperationConflict,
    OperationExpired,
    RunClosed,
    RunNotFound,
    SessionConflict,
    SessionMismatch,
    SessionNotFound,
    SteeringConsumptionConflict,
)
from app.domain.session.models import (
    AppendOutcome,
    EditCommand,
    EditRequest,
    OperationOutcome,
    RegenerateCommand,
    RegenerateRequest,
    RunOutcome,
    SendCommand,
    SendRequest,
    Session,
    SessionHistory,
    SessionMessageEntry,
    SessionOperation,
    SessionRun,
    SteeringCommand,
    SteeringConsumption,
    SteeringInput,
    SteeringRequest,
    SteeringWithdrawal,
    TerminalRunStatus,
)
from app.domain.session.repository import SessionRepository

CREDENTIAL_ERROR_CODE = "credential_detected"
CREDENTIAL_SAFE_MESSAGE = "检测到受保护凭据，操作已拒绝"


class SnapshotStore(Protocol):
    # 会话内容替换与删除时的画像快照协作接口，由业务服务实现。
    async def remove_for_entries(
        self, session_id: str, entry_ids: set[str]
    ) -> None: ...

    async def remove_for_session(self, session_id: str) -> None: ...


_REQUEST_MODELS = {
    "send": SendRequest,
    "edit": EditRequest,
    "regenerate": RegenerateRequest,
    "steering": SteeringRequest,
}


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _new_id() -> str:
    return str(uuid.uuid4())


def _collect_text(value: object, collected: list[str]) -> None:
    if isinstance(value, str):
        collected.append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            collected.append(key)
            _collect_text(item, collected)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_text(item, collected)


def _contains_credential(value: object, credentials: Sequence[str]) -> bool:
    secrets = [secret for secret in credentials if secret]
    if not secrets:
        return False
    collected: list[str] = []
    _collect_text(value, collected)
    return any(secret in text for secret in secrets for text in collected)


def _reject_credentials(value: object, credentials: Sequence[str]) -> None:
    if _contains_credential(value, credentials):
        raise CredentialDetected("请求命中受保护凭据")


def _same_request(kind: str, stored_request: object, incoming: object) -> bool:
    model = _REQUEST_MODELS[kind]
    return model.model_validate(stored_request) == incoming


def _is_role(entry: SessionMessageEntry, role: str) -> bool:
    return entry.messages[0].role == role


def _subtree_ids(
    entries: Sequence[SessionMessageEntry], root_id: str, *, include_root: bool
) -> set[str]:
    children: dict[str | None, list[str]] = {}
    for entry in entries:
        children.setdefault(entry.parent_id, []).append(entry.id)
    ids: set[str] = set()
    stack = list(children.get(root_id, []))
    if include_root:
        stack.append(root_id)
    while stack:
        node = stack.pop()
        if node in ids:
            continue
        ids.add(node)
        stack.extend(children.get(node, []))
    return ids


class SessionService:
    def __init__(
        self,
        repository: SessionRepository,
        snapshots: SnapshotStore | None = None,
    ) -> None:
        self._repository = repository
        self._snapshots = snapshots

    def attach_snapshots(self, snapshots: SnapshotStore) -> None:
        self._snapshots = snapshots

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
        _reject_credentials(
            {"session_id": session_id, "title": title}, credentials
        )
        now = _now_ms()
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

    async def get_session(self, session_id: str) -> Session:
        return await self._require_session(session_id)

    async def list_sessions(self) -> list[Session]:
        return await self._repository.list_sessions()

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

    async def get_entry(self, session_id: str, entry_id: str) -> SessionMessageEntry:
        return await self._require_entry(session_id, entry_id)

    async def list_entries(self, session_id: str) -> list[SessionMessageEntry]:
        await self._require_session(session_id)
        return await self._repository.list_entries(session_id)

    async def get_current_branch(
        self, session_id: str
    ) -> list[SessionMessageEntry]:
        async with self._repository.transaction():
            session = await self._require_session(session_id)
            return await self._branch(session_id, session.active_leaf_id)

    async def get_session_history(self, session_id: str) -> SessionHistory:
        # 单次读取事务内取得会话、当前分支、关联运行及输入，保证同一快照。
        async with self._repository.transaction():
            session = await self._require_session(session_id)
            entries = await self._repository.list_entries(session_id)
            branch = build_session_path(session_id, entries, session.active_leaf_id)
            branch_ids = {entry.id for entry in branch}
            referenced = {
                entry.run_id for entry in branch if entry.run_id is not None
            }
            runs = [
                run
                for run in await self._repository.list_runs(session_id)
                if run.id in referenced
                or (
                    run.request_entry_id in branch_ids
                    and (run.last_entry_id is None or run.status == "running")
                )
            ]
            runs.sort(key=lambda run: (run.started_at, run.id))
            steering: list[SteeringInput] = []
            for run in runs:
                steering.extend(
                    await self._repository.list_steering(session_id, run.id)
                )
            steering.sort(key=lambda item: (item.created_at, item.id))
            return SessionHistory(
                session=session,
                entries=branch,
                runs=runs,
                steering=steering,
            )

    async def get_branch(
        self, session_id: str, leaf_id: str | None
    ) -> list[SessionMessageEntry]:
        async with self._repository.transaction():
            await self._require_session(session_id)
            return await self._branch(session_id, leaf_id)

    async def get_context(self, session_id: str, leaf_id: str | None) -> list[Message]:
        entries = await self.list_entries(session_id)
        return build_session_context(session_id, entries, leaf_id)

    async def get_run(self, session_id: str, run_id: str) -> SessionRun:
        return await self._require_run(session_id, run_id)

    async def find_run_any(self, run_id: str) -> SessionRun | None:
        return await self._repository.get_run_by_id(run_id)

    async def list_runs(self, session_id: str) -> list[SessionRun]:
        # 存在性检查与列表读取在同一读取事务内，返回一致快照。
        async with self._repository.transaction():
            await self._require_session(session_id)
            return await self._repository.list_runs(session_id)

    async def get_steering(self, session_id: str, steering_id: str) -> SteeringInput:
        return await self._require_steering(session_id, steering_id)

    async def list_steering(
        self, session_id: str, run_id: str
    ) -> list[SteeringInput]:
        # 会话、运行存在性与列表读取在同一读取事务内，返回一致快照。
        async with self._repository.transaction():
            await self._require_session(session_id)
            await self._require_run(session_id, run_id)
            return await self._repository.list_steering(session_id, run_id)

    async def get_operation(self, operation_id: str) -> SessionOperation | None:
        return await self._repository.get_operation(operation_id)

    async def get_operation_invalidation(self, operation_id: str) -> str | None:
        # 返回失效记录所属会话 ID；None 表示未失效。
        return await self._repository.get_operation_invalidation(operation_id)

    async def accept_send(
        self,
        command: SendCommand,
        *,
        system_message: SystemMessage,
        credentials: Sequence[str] = (),
    ) -> OperationOutcome:
        request = command.request
        _reject_credentials(command.model_dump(exclude_unset=True), credentials)
        _reject_credentials(
            system_message.model_dump(exclude_unset=True), credentials
        )
        async with self._repository.transaction():
            resolved = await self.resolve_operation(
                command.operation_id, command.session_id, "send", request
            )
            if resolved is not None:
                return resolved
            await self._ensure_idle()
            session = await self._require_session(command.session_id)
            entries = await self._repository.list_entries(command.session_id)
            await self._ensure_pairable(command.session_id, entries, session.active_leaf_id)
            now = _now_ms()
            parent_id = session.active_leaf_id
            user_timestamp = now
            if parent_id is None:
                system_entry = SessionMessageEntry(
                    session_id=command.session_id,
                    id=_new_id(),
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
                id=_new_id(),
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
            run = self._new_run(command.session_id, user_entry.id, now)
            await self._repository.insert_entry(user_entry)
            await self._repository.insert_run(run)
            operation = await self._record_operation(
                command.operation_id, command.session_id, "send", request, run.id
            )
            await self._set_leaf(session, user_entry.id, now)
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
        _reject_credentials(command.model_dump(exclude_unset=True), credentials)
        async with self._repository.transaction():
            resolved = await self.resolve_operation(
                command.operation_id, command.session_id, "edit", request
            )
            if resolved is not None:
                return resolved
            await self._ensure_idle()
            session = await self._require_session(command.session_id)
            target = await self._require_entry(
                command.session_id, request.target_entry_id
            )
            if not _is_role(target, "user"):
                raise InvalidTargetEntry("编辑目标必须是同会话用户消息节点")
            entries = await self._repository.list_entries(command.session_id)
            await self._ensure_pairable(
                command.session_id, entries, target.parent_id
            )
            await self._remove_content(
                command.session_id, target.id, entries, include_target=True
            )
            now = _now_ms()
            user_entry = SessionMessageEntry(
                session_id=command.session_id,
                id=_new_id(),
                parent_id=target.parent_id,
                run_id=None,
                type="message",
                messages=[
                    UserMessage(role="user", content=request.text, timestamp=now)
                ],
                created_at=now,
            )
            run = self._new_run(command.session_id, user_entry.id, now)
            await self._repository.insert_entry(user_entry)
            await self._repository.insert_run(run)
            operation = await self._record_operation(
                command.operation_id, command.session_id, "edit", request, run.id
            )
            await self._set_leaf(session, user_entry.id, now)
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
        _reject_credentials(command.model_dump(exclude_unset=True), credentials)
        async with self._repository.transaction():
            resolved = await self.resolve_operation(
                command.operation_id, command.session_id, "regenerate", request
            )
            if resolved is not None:
                return resolved
            await self._ensure_idle()
            session = await self._require_session(command.session_id)
            target = await self._require_entry(
                command.session_id, request.target_entry_id
            )
            if not _is_role(target, "user"):
                raise InvalidTargetEntry("重新生成目标必须是同会话用户消息节点")
            entries = await self._repository.list_entries(command.session_id)
            await self._ensure_pairable(command.session_id, entries, target.id)
            await self._remove_content(
                command.session_id, target.id, entries, include_target=False
            )
            now = _now_ms()
            run = self._new_run(command.session_id, target.id, now)
            await self._repository.insert_run(run)
            operation = await self._record_operation(
                command.operation_id,
                command.session_id,
                "regenerate",
                request,
                run.id,
            )
            await self._set_leaf(session, target.id, now)
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
        _reject_credentials(command.model_dump(exclude_unset=True), credentials)
        async with self._repository.transaction():
            resolved = await self.resolve_operation(
                command.operation_id, command.session_id, "steering", request
            )
            if resolved is not None:
                return resolved
            await self._require_session(command.session_id)
            run = await self._require_run(
                command.session_id, request.target_run_id
            )
            if run.status != "running":
                raise RunClosed("目标运行未在接受输入")
            now = _now_ms()
            steering = SteeringInput(
                session_id=command.session_id,
                id=_new_id(),
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
            operation = await self._record_operation(
                command.operation_id,
                command.session_id,
                "steering",
                request,
                run.id,
                steering_id=steering.id,
            )
            return OperationOutcome(
                created=True, operation=operation, run=None, steering=steering
            )

    async def append_entry(
        self,
        entry: SessionMessageEntry,
        *,
        credentials: Sequence[str] = (),
    ) -> AppendOutcome:
        if entry.run_id is None:
            raise SessionConflict("追加节点必须关联运行")
        if entry.messages[0].role not in {"assistant", "toolResult"}:
            raise SessionConflict("追加节点仅接受助手消息或工具结果")
        if _contains_credential(
            entry.model_dump(exclude_unset=True), credentials
        ):
            async with self._repository.transaction():
                run = await self._require_run(entry.session_id, entry.run_id)
                if run.status == "running":
                    now = _now_ms()
                    run = run.model_copy(
                        update={
                            "status": "failed",
                            "finished_at": now,
                            "error_code": CREDENTIAL_ERROR_CODE,
                            "error_message": CREDENTIAL_SAFE_MESSAGE,
                        }
                    )
                    await self._repository.update_run(run)
                    await self._discard_pending(
                        entry.session_id, run.id, "failed", now
                    )
                return AppendOutcome(
                    created=False,
                    credential_detected=True,
                    entry=None,
                    run=run,
                )
        async with self._repository.transaction():
            existing = await self._repository.get_entry(entry.session_id, entry.id)
            if existing is not None:
                if not self._same_entry(existing, entry):
                    raise SessionConflict(f"节点 ID 冲突: {entry.id}")
                run = await self._require_run(entry.session_id, entry.run_id)
                return AppendOutcome(
                    created=False,
                    credential_detected=False,
                    entry=existing,
                    run=run,
                )
            run = await self._require_run(entry.session_id, entry.run_id)
            if run.status != "running":
                raise SessionConflict("运行未在运行，拒绝追加节点")
            session = await self._require_session(entry.session_id)
            position = self._run_position(run)
            if entry.parent_id != position:
                raise SessionConflict("父节点与运行追加位置不一致")
            if session.active_leaf_id != position:
                raise SessionConflict("当前分支指针与运行进度不一致")
            now = _now_ms()
            await self._repository.insert_entry(entry)
            run = run.model_copy(update={"last_entry_id": entry.id})
            await self._repository.update_run(run)
            await self._set_leaf(session, entry.id, now)
            return AppendOutcome(
                created=True,
                credential_detected=False,
                entry=entry,
                run=run,
            )

    async def consume_steering(
        self, session_id: str, run_id: str, steering_id: str
    ) -> SteeringConsumption:
        async with self._repository.transaction():
            steering = await self._require_steering(session_id, steering_id)
            if steering.run_id != run_id:
                raise SessionConflict("Steering 输入不属于指定运行")
            if steering.status == "consumed":
                entry = await self._require_entry(session_id, steering.entry_id)
                return SteeringConsumption(
                    created=False, steering=steering, entry=entry
                )
            if steering.status in {"withdrawn", "discarded"}:
                return SteeringConsumption(
                    created=False, steering=steering, entry=None
                )
            run = await self._require_run(session_id, run_id)
            if run.status != "running":
                raise RunClosed("运行未在运行，拒绝消费 Steering")
            session = await self._require_session(session_id)
            position = self._run_position(run)
            if session.active_leaf_id != position:
                raise SessionConflict("当前分支指针与运行进度不一致")
            entries = await self._repository.list_entries(session_id)
            await self._ensure_pairable(session_id, entries, position)
            now = _now_ms()
            entry = SessionMessageEntry(
                session_id=session_id,
                id=_new_id(),
                parent_id=position,
                run_id=run_id,
                type="message",
                messages=[steering.message],
                created_at=now,
            )
            await self._repository.insert_entry(entry)
            run = run.model_copy(update={"last_entry_id": entry.id})
            await self._repository.update_run(run)
            await self._set_leaf(session, entry.id, now)
            steering = steering.model_copy(
                update={
                    "status": "consumed",
                    "entry_id": entry.id,
                    "updated_at": now,
                }
            )
            await self._repository.update_steering(steering)
            return SteeringConsumption(
                created=True, steering=steering, entry=entry
            )

    async def withdraw_steering(
        self, session_id: str, run_id: str, steering_id: str
    ) -> SteeringWithdrawal:
        async with self._repository.transaction():
            steering = await self._require_steering(session_id, steering_id)
            if steering.run_id != run_id:
                raise SessionMismatch("Steering 输入不属于指定运行")
            if steering.status == "consumed":
                raise SteeringConsumptionConflict("已消费的 Steering 输入无法撤回")
            if steering.status != "pending":
                return SteeringWithdrawal(changed=False, steering=steering)
            steering = steering.model_copy(
                update={"status": "withdrawn", "updated_at": _now_ms()}
            )
            await self._repository.update_steering(steering)
            return SteeringWithdrawal(changed=True, steering=steering)

    async def list_pending_steering(
        self, session_id: str, run_id: str
    ) -> list[SteeringInput]:
        await self._require_run(session_id, run_id)
        return await self._repository.list_pending_steering(session_id, run_id)

    async def finish_run(
        self,
        session_id: str,
        run_id: str,
        status: TerminalRunStatus,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        credentials: Sequence[str] = (),
    ) -> RunOutcome:
        if _contains_credential(
            {"error_code": error_code, "error_message": error_message}, credentials
        ):
            status = "failed"
            error_code = CREDENTIAL_ERROR_CODE
            error_message = CREDENTIAL_SAFE_MESSAGE
        async with self._repository.transaction():
            run = await self._require_run(session_id, run_id)
            if run.status == status:
                return RunOutcome(changed=False, run=run)
            if run.status != "running":
                raise SessionConflict("运行已结束，无法再次结束")
            now = _now_ms()
            run = run.model_copy(
                update={
                    "status": status,
                    "finished_at": now,
                    "error_code": error_code,
                    "error_message": error_message,
                }
            )
            await self._repository.update_run(run)
            await self._discard_pending(session_id, run_id, status, now)
            return RunOutcome(changed=True, run=run)

    async def recover_interrupted(self) -> list[SessionRun]:
        async with self._repository.transaction():
            runs = await self._repository.list_running_runs()
            now = _now_ms()
            recovered: list[SessionRun] = []
            for run in runs:
                run = run.model_copy(update={"status": "interrupted"})
                await self._repository.update_run(run)
                await self._discard_pending(
                    run.session_id, run.id, "interrupted", now
                )
                recovered.append(run)
            return recovered

    def _new_run(self, session_id: str, request_entry_id: str, now: int) -> SessionRun:
        return SessionRun(
            session_id=session_id,
            id=_new_id(),
            request_entry_id=request_entry_id,
            last_entry_id=None,
            status="running",
            started_at=now,
            finished_at=None,
            error_code=None,
            error_message=None,
        )

    async def _record_operation(
        self,
        operation_id: str,
        session_id: str,
        kind: str,
        request: object,
        run_id: str,
        steering_id: str | None = None,
    ) -> SessionOperation:
        operation = SessionOperation(
            operation_id=operation_id,
            session_id=session_id,
            kind=kind,
            request=request.model_dump(exclude_unset=True),
            run_id=run_id,
            steering_id=steering_id,
            created_at=_now_ms(),
        )
        await self._repository.insert_operation(operation)
        return operation

    async def _set_leaf(self, session: Session, leaf_id: str, now: int) -> None:
        await self._repository.update_session(
            session.model_copy(update={"active_leaf_id": leaf_id, "updated_at": now})
        )

    async def _branch(
        self, session_id: str, leaf_id: str | None
    ) -> list[SessionMessageEntry]:
        entries = await self._repository.list_entries(session_id)
        return build_session_path(session_id, entries, leaf_id)

    async def _ensure_idle(self) -> None:
        running = await self._repository.find_running_run()
        if running is not None:
            raise SessionConflict(f"已有运行正在执行: {running.id}")

    async def _ensure_pairable(
        self,
        session_id: str,
        entries: list[SessionMessageEntry],
        leaf_id: str | None,
    ) -> None:
        context = build_session_context(session_id, entries, leaf_id)
        try:
            validate_tool_pairs(context)
        except ValueError as error:
            raise IncompleteToolChain(str(error)) from error

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

    async def _remove_content(
        self,
        session_id: str,
        target_id: str,
        entries: list[SessionMessageEntry],
        *,
        include_target: bool,
    ) -> None:
        # 删除目标子树，并清理被移除内容关联的历史运行、Steering 与操作记录。
        deleted_ids = _subtree_ids(entries, target_id, include_root=include_target)
        if not deleted_ids:
            return
        await self._repository.defer_foreign_keys()
        if self._snapshots is not None:
            # 随消息移除画像快照；已完成保存的幂等记录保留。
            await self._snapshots.remove_for_entries(session_id, deleted_ids)
        runs = await self._repository.list_runs(session_id)
        entries_by_run: dict[str, list[SessionMessageEntry]] = {}
        for entry in entries:
            if entry.run_id is not None:
                entries_by_run.setdefault(entry.run_id, []).append(entry)
        runs_to_delete: list[SessionRun] = []
        runs_to_fix: list[tuple[SessionRun, str]] = []
        for run in runs:
            if (
                run.request_entry_id not in deleted_ids
                and run.last_entry_id not in deleted_ids
            ):
                continue
            retained = [
                entry
                for entry in entries_by_run.get(run.id, [])
                if entry.id not in deleted_ids
            ]
            if not retained:
                # 没有留存节点引用的运行随内容一并清理。
                runs_to_delete.append(run)
                continue
            # 被留存节点引用的运行保留，进度回退到留存链上最深的节点。
            parent_ids = {entry.parent_id for entry in retained}
            deepest = max(
                (entry for entry in retained if entry.id not in parent_ids),
                key=lambda item: (item.created_at, item.id),
            )
            runs_to_fix.append((run, deepest.id))
        deleted_run_ids = {run.id for run in runs_to_delete}

        steering_to_delete: list[str] = []
        for run in runs:
            for steering in await self._repository.list_steering(session_id, run.id):
                if (
                    steering.run_id in deleted_run_ids
                    or steering.entry_id in deleted_ids
                ):
                    steering_to_delete.append(steering.id)
        deleted_steering_ids = set(steering_to_delete)

        operations = await self._repository.list_operations(session_id)
        operations_to_delete = [
            operation
            for operation in operations
            if operation.run_id in deleted_run_ids
            or (
                operation.steering_id is not None
                and operation.steering_id in deleted_steering_ids
            )
        ]
        if operations_to_delete:
            await self._repository.delete_operations(
                [operation.operation_id for operation in operations_to_delete]
            )
            for operation in operations_to_delete:
                await self._repository.insert_operation_invalidation(
                    operation.operation_id, session_id
                )
        if deleted_steering_ids:
            await self._repository.delete_steering(
                session_id, sorted(deleted_steering_ids)
            )
        for run, new_last in runs_to_fix:
            await self._repository.update_run(
                run.model_copy(update={"last_entry_id": new_last})
            )
        if deleted_run_ids:
            await self._repository.delete_runs(
                session_id, sorted(deleted_run_ids)
            )
        await self._repository.delete_entries(session_id, sorted(deleted_ids))

    async def _discard_pending(
        self, session_id: str, run_id: str, reason: str, now: int
    ) -> None:
        pending = await self._repository.list_pending_steering(session_id, run_id)
        for steering in pending:
            await self._repository.update_steering(
                steering.model_copy(
                    update={
                        "status": "discarded",
                        "reason": reason,
                        "updated_at": now,
                    }
                )
            )

    def _run_position(self, run: SessionRun) -> str:
        return (
            run.last_entry_id
            if run.last_entry_id is not None
            else run.request_entry_id
        )

    def _same_entry(
        self, existing: SessionMessageEntry, incoming: SessionMessageEntry
    ) -> bool:
        return (
            existing.session_id == incoming.session_id
            and existing.parent_id == incoming.parent_id
            and existing.run_id == incoming.run_id
            and existing.type == incoming.type
            and serialize_message(existing.messages[0])
            == serialize_message(incoming.messages[0])
        )

    async def _require_session(self, session_id: str) -> Session:
        session = await self._repository.get_session(session_id)
        if session is None:
            raise SessionNotFound(f"会话不存在: {session_id}")
        return session

    async def _require_entry(
        self, session_id: str, entry_id: str | None
    ) -> SessionMessageEntry:
        if entry_id is None:
            raise EntryNotFound("节点 ID 为空")
        entry = await self._repository.get_entry(session_id, entry_id)
        if entry is None:
            raise EntryNotFound(f"节点不存在: {entry_id}")
        return entry

    async def _require_run(self, session_id: str, run_id: str) -> SessionRun:
        run = await self._repository.get_run(session_id, run_id)
        if run is None:
            raise RunNotFound(f"运行不存在: {run_id}")
        return run

    async def _require_steering(
        self, session_id: str, steering_id: str
    ) -> SteeringInput:
        steering = await self._repository.get_steering(session_id, steering_id)
        if steering is None:
            raise SessionNotFound(f"Steering 输入不存在: {steering_id}")
        return steering

    async def resolve_operation(
        self,
        operation_id: str,
        session_id: str,
        kind: str,
        request: object,
    ) -> OperationOutcome | None:
        await self._ensure_active(operation_id, session_id)
        stored = await self._repository.get_operation(operation_id)
        if stored is None:
            return None
        if stored.session_id != session_id:
            raise SessionMismatch(f"operation_id 属于其他会话: {operation_id}")
        if stored.kind != kind or not _same_request(stored.kind, stored.request, request):
            raise OperationConflict(f"operation_id 已被其他请求占用: {operation_id}")
        run: SessionRun | None = None
        steering: SteeringInput | None = None
        if stored.kind == "steering":
            steering = await self._require_steering(
                stored.session_id, stored.steering_id
            )
        else:
            run = await self._require_run(stored.session_id, stored.run_id)
        return OperationOutcome(
            created=False, operation=stored, run=run, steering=steering
        )
