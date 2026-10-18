from dataclasses import dataclass

from app.agent.compaction import EffectiveContext, build_effective_context
from app.ai.context import validate_tool_pairs
from app.ai.messages import Message, serialize_message
from app.domain.session.attachments import AttachmentError
from app.domain.session.branch import (
    build_session_context,
    build_session_path,
    message_entries,
)
from app.domain.session.errors import (
    EntryNotFound,
    IncompleteToolChain,
    InvalidTargetEntry,
    RunBusy,
    RunNotFound,
    SessionNotFound,
)
from app.domain.session.models import (
    Session,
    SessionEntry,
    SessionHistory,
    SessionMessageEntry,
    SessionRun,
    SteeringInput,
)
from app.domain.session.repository import SessionRepository


@dataclass(frozen=True)
class SessionProjection:
    # 模型投影读取结果：当前完整祖先链与摘要感知有效上下文。
    # context.source_entry_ids 与 path 中的真实节点一一对应，供第二部分生成引用。
    path: list[SessionEntry]
    context: EffectiveContext


def is_role(entry: SessionEntry | None, role: str) -> bool:
    # 真实消息节点才带 messages；压缩节点及缺失节点一律不匹配业务角色。
    return isinstance(entry, SessionMessageEntry) and entry.messages[0].role == role


class SessionQueries:
    def __init__(self, repository: SessionRepository) -> None:
        self._repository = repository

    async def get_session(self, session_id: str) -> Session:
        return await self.require_session(session_id)

    async def list_sessions(self) -> list[Session]:
        return await self._repository.list_sessions()

    async def get_entry(self, session_id: str, entry_id: str) -> SessionEntry:
        async with self._repository.transaction():
            return await self.require_entry(session_id, entry_id)

    async def list_entries(self, session_id: str) -> list[SessionEntry]:
        await self.require_session(session_id)
        return await self._repository.list_entries(session_id)

    async def get_current_branch(
        self, session_id: str
    ) -> list[SessionMessageEntry]:
        async with self._repository.transaction():
            session = await self.require_session(session_id)
            return await self._branch(session_id, session.active_leaf_id)

    async def get_session_history(self, session_id: str) -> SessionHistory:
        # 单次读取事务内取得会话、完整祖先链、关联运行及输入，保证同一快照。
        async with self._repository.transaction():
            session = await self.require_session(session_id)
            entries = await self._repository.list_entries(session_id)
            path = build_session_path(session_id, entries, session.active_leaf_id)
            nodes = {entry.id: entry for entry in path}
            for entry in path:
                # 附件只在真实用户消息节点投影；压缩节点不携带附件集合。
                if is_role(entry, "user"):
                    entry.attachments = await self._repository.list_entry_attachments(
                        session_id, entry.id
                    )
            referenced = {
                entry.run_id for entry in path if entry.run_id is not None
            }
            runs = [
                run
                for run in await self._repository.list_runs(session_id)
                if run.id in referenced
                or (
                    run.request_entry_id in nodes
                    and (run.last_entry_id is None or run.status == "running")
                )
            ]
            for run in runs:
                # 运行依据必须仍是真实用户消息节点，压缩节点不承担请求身份。
                if not is_role(nodes.get(run.request_entry_id), "user"):
                    raise AttachmentError("internal_error", "运行请求节点不是用户消息")
            runs.sort(key=lambda run: (run.started_at, run.id))
            steering: list[SteeringInput] = []
            for run in runs:
                steering.extend(
                    await self._repository.list_steering(session_id, run.id)
                )
            steering.sort(key=lambda item: (item.created_at, item.id))
            for item in steering:
                item.attachments = await self._repository.list_steering_attachments(session_id, item.id)
                if item.status == "consumed":
                    consumed = nodes.get(item.entry_id)
                    if not is_role(consumed, "user"):
                        raise AttachmentError("internal_error", "Steering 消费节点不是用户消息")
                    if consumed.attachments != item.attachments or serialize_message(consumed.messages[0]) != serialize_message(item.message):
                        raise AttachmentError("internal_error", "Steering 附件关联损坏")
            return SessionHistory(
                session=session,
                entries=path,
                runs=runs,
                steering=steering,
            )

    async def get_branch(
        self, session_id: str, leaf_id: str | None
    ) -> list[SessionMessageEntry]:
        async with self._repository.transaction():
            await self.require_session(session_id)
            return await self._branch(session_id, leaf_id)

    async def get_context(self, session_id: str, leaf_id: str | None) -> list[Message]:
        entries = await self.list_entries(session_id)
        return build_session_context(session_id, entries, leaf_id)

    async def get_projection(
        self, session_id: str, leaf_id: str | None
    ) -> SessionProjection:
        # 模型投影入口：最新检查点系统状态与摘要、first_kept_entry_id 起的保留原文及
        # 检查点之后的新增消息。原始路径语义不变，继续由 get_branch/get_context 提供。
        async with self._repository.transaction():
            await self.require_session(session_id)
            entries = await self._repository.list_entries(session_id)
            path = build_session_path(session_id, entries, leaf_id)
            for entry in path:
                # 附件先按真实用户节点投影，供模型请求投影表达为用户内容；
                # 累计历史附件仍由检查点 details 提供。
                if is_role(entry, "user"):
                    entry.attachments = await self._repository.list_entry_attachments(
                        session_id, entry.id
                    )
            context = build_effective_context(path)
            return SessionProjection(path=path, context=context)

    async def get_run(self, session_id: str, run_id: str) -> SessionRun:
        return await self.require_run(session_id, run_id)

    async def find_run_any(self, run_id: str) -> SessionRun | None:
        return await self._repository.get_run_by_id(run_id)

    async def list_runs(self, session_id: str) -> list[SessionRun]:
        # 存在性检查与列表读取在同一读取事务内，返回一致快照。
        async with self._repository.transaction():
            await self.require_session(session_id)
            return await self._repository.list_runs(session_id)

    async def get_steering(self, session_id: str, steering_id: str) -> SteeringInput:
        async with self._repository.transaction():
            return await self.require_steering(session_id, steering_id)

    async def list_steering(
        self, session_id: str, run_id: str
    ) -> list[SteeringInput]:
        # 会话、运行存在性与列表读取在同一读取事务内，返回一致快照。
        async with self._repository.transaction():
            await self.require_session(session_id)
            await self.require_run(session_id, run_id)
            inputs = await self._repository.list_steering(session_id, run_id)
            for item in inputs:
                item.attachments = await self._repository.list_steering_attachments(session_id, item.id)
            return inputs

    async def find_running_run_for_session(
        self, session_id: str
    ) -> SessionRun | None:
        # 会话级冲突检查：在全局名额与频率额度之前判定同会话运行冲突。
        return await self._repository.find_running_run_for_session(session_id)

    async def ensure_idle(self) -> None:
        # 新确认与新 Agent 运行互斥：确认受理在空闲时校验，占用时拒绝登记。
        running = await self._repository.find_running_run()
        if running is not None:
            raise RunBusy(f"已有运行正在执行: {running.id}")

    async def ensure_pairable(
        self,
        session_id: str,
        entries: list[SessionEntry],
        leaf_id: str | None,
    ) -> None:
        context = build_session_context(session_id, entries, leaf_id)
        try:
            validate_tool_pairs(context)
        except ValueError as error:
            raise IncompleteToolChain(str(error)) from error

    async def require_session(self, session_id: str) -> Session:
        session = await self._repository.get_session(session_id)
        if session is None:
            raise SessionNotFound(f"会话不存在: {session_id}")
        return session

    async def require_entry(
        self, session_id: str, entry_id: str | None
    ) -> SessionEntry:
        if entry_id is None:
            raise EntryNotFound("节点 ID 为空")
        entry = await self._repository.get_entry(session_id, entry_id)
        if entry is None:
            raise EntryNotFound(f"节点不存在: {entry_id}")
        if is_role(entry, "user"):
            entry.attachments = await self._repository.list_entry_attachments(session_id, entry_id)
        return entry

    async def require_message_entry(
        self, session_id: str, entry_id: str | None
    ) -> SessionMessageEntry:
        # 需要 messages[0] 的调用者先确认节点为真实消息节点，压缩节点不参与业务确认链。
        entry = await self.require_entry(session_id, entry_id)
        if not isinstance(entry, SessionMessageEntry):
            raise InvalidTargetEntry("目标节点必须是消息节点")
        return entry

    async def require_run(self, session_id: str, run_id: str) -> SessionRun:
        run = await self._repository.get_run(session_id, run_id)
        if run is None:
            raise RunNotFound(f"运行不存在: {run_id}")
        return run

    async def require_steering(
        self, session_id: str, steering_id: str
    ) -> SteeringInput:
        steering = await self._repository.get_steering(session_id, steering_id)
        if steering is None:
            raise SessionNotFound(f"Steering 输入不存在: {steering_id}")
        steering.attachments = await self._repository.list_steering_attachments(session_id, steering_id)
        return steering

    async def _branch(
        self, session_id: str, leaf_id: str | None
    ) -> list[SessionMessageEntry]:
        entries = await self._repository.list_entries(session_id)
        branch = message_entries(build_session_path(session_id, entries, leaf_id))
        for entry in branch:
            if entry.messages[0].role == "user":
                entry.attachments = await self._repository.list_entry_attachments(session_id, entry.id)
        return branch
