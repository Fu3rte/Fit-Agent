from collections.abc import Sequence

from app.agent.compaction import build_effective_context
from app.ai.messages import UserMessage, serialize_message
from app.application.session.credentials import (
    contains_credential,
)
from app.application.session.positions import new_id, now_ms, run_position, set_leaf
from app.application.session.queries import SessionQueries
from app.application.session.runs import SessionRuns
from app.domain.session.branch import build_session_path
from app.domain.session.errors import SessionConflict
from app.domain.session.models import (
    AppendOutcome,
    BusinessContextEntry,
    BusinessContextOutcome,
    BusinessContextSnapshot,
    CompactionEntry,
    CompactionOutcome,
    SaveConfirmation,
    SessionEntry,
    SessionMessageEntry,
    validate_business_context_projection,
)
from app.domain.session.repository import SessionRepository


class SessionEntries:
    def __init__(
        self,
        repository: SessionRepository,
        queries: SessionQueries,
        runs: SessionRuns,
    ) -> None:
        self._repository = repository
        self._queries = queries
        self._runs = runs

    async def append_confirmation_entry(
        self, session_id: str, confirmation: SaveConfirmation,
    ) -> SessionMessageEntry:
        # 独立确认用户节点：run_id 为空、不创建运行、无附件；由确认受理在同一事务内调用。
        session = await self._queries.require_session(session_id)
        entries = await self._repository.list_entries(session_id)
        await self._queries.ensure_pairable(session_id, entries, session.active_leaf_id)
        now = now_ms()
        entry = SessionMessageEntry(
            session_id=session_id,
            id=new_id(),
            parent_id=session.active_leaf_id,
            run_id=None,
            type="message",
            messages=[
                UserMessage(role="user", content=confirmation.text, timestamp=now)
            ],
            confirmation=confirmation,
            created_at=now,
        )
        await self._repository.insert_entry(entry)
        await set_leaf(self._repository, session, entry.id, now)
        return entry

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
        if contains_credential(
            entry.model_dump(exclude_unset=True), credentials
        ):
            run = await self._runs.fail_on_credential_hit(entry.session_id, entry.run_id)
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
                run = await self._queries.require_run(entry.session_id, entry.run_id)
                return AppendOutcome(
                    created=False,
                    credential_detected=False,
                    entry=existing,
                    run=run,
                )
            run = await self._queries.require_run(entry.session_id, entry.run_id)
            if run.status != "running":
                raise SessionConflict("运行未在运行，拒绝追加节点")
            session = await self._queries.require_session(entry.session_id)
            position = run_position(run)
            if entry.parent_id != position:
                raise SessionConflict("父节点与运行追加位置不一致")
            if session.active_leaf_id != position:
                raise SessionConflict("当前分支指针与运行进度不一致")
            now = now_ms()
            await self._repository.insert_entry(entry)
            run = run.model_copy(update={"last_entry_id": entry.id})
            await self._repository.update_run(run)
            await set_leaf(self._repository, session, entry.id, now)
            return AppendOutcome(
                created=True,
                credential_detected=False,
                entry=entry,
                run=run,
            )

    async def append_business_context(
        self,
        entry: BusinessContextEntry,
        *,
        credentials: Sequence[str] = (),
    ) -> BusinessContextOutcome:
        # 内部资料节点受控写入：写入前核对固定投影，父节点必须等于运行追加位置。
        validate_business_context_projection(entry.message)
        if contains_credential(entry.model_dump(exclude_unset=True), credentials):
            run = await self._runs.fail_on_credential_hit(entry.session_id, entry.run_id)
            return BusinessContextOutcome(
                created=False,
                credential_detected=True,
                entry=None,
                run=run,
            )
        async with self._repository.transaction():
            existing = await self._repository.get_entry(entry.session_id, entry.id)
            if existing is not None:
                if existing.model_dump() != entry.model_dump():
                    raise SessionConflict(f"节点 ID 冲突: {entry.id}")
                run = await self._queries.require_run(entry.session_id, entry.run_id)
                return BusinessContextOutcome(
                    created=False,
                    credential_detected=False,
                    entry=entry,
                    run=run,
                )
            run = await self._queries.require_run(entry.session_id, entry.run_id)
            if run.status != "running":
                raise SessionConflict("运行未在运行，拒绝追加内部资料节点")
            session = await self._queries.require_session(entry.session_id)
            position = run_position(run)
            if entry.parent_id != position:
                raise SessionConflict("父节点与运行追加位置不一致")
            if session.active_leaf_id != position:
                raise SessionConflict("当前分支指针与运行进度不一致")
            now = now_ms()
            await self._repository.insert_entry(entry)
            run = run.model_copy(update={"last_entry_id": entry.id})
            await self._repository.update_run(run)
            await set_leaf(self._repository, session, entry.id, now)
            return BusinessContextOutcome(
                created=True,
                credential_detected=False,
                entry=entry,
                run=run,
            )

    async def commit_compaction(
        self,
        entry: CompactionEntry,
        baseline: BusinessContextEntry,
        *,
        prepared_position: str,
        credentials: Sequence[str] = (),
    ) -> CompactionOutcome:
        # 检查点提交：摘要、真实 usage、系统状态、tokens_before 与附件详情由调用方在事务外生成；
        # 新基线为压缩后完整业务投影，同样事务外构造。事务内核对摘要准备位置未变化，原子写入
        # 检查点、新基线节点、运行末节点与分支指针；基线失败则检查点与位置更新一并回滚。
        validate_business_context_projection(baseline.message)
        if not isinstance(baseline.message.details, BusinessContextSnapshot):
            raise SessionConflict("压缩新基线必须是完整业务快照")
        if (
            baseline.parent_id != entry.id
            or baseline.session_id != entry.session_id
            or baseline.run_id != entry.run_id
        ):
            raise SessionConflict("新基线节点必须紧跟其压缩检查点")
        if contains_credential(
            {"checkpoint": entry.model_dump(), "baseline": baseline.model_dump()}, credentials
        ):
            run = await self._runs.fail_on_credential_hit(entry.session_id, entry.run_id)
            return CompactionOutcome(
                created=False,
                credential_detected=True,
                entry=None,
                baseline=None,
                run=run,
            )
        async with self._repository.transaction():
            existing = await self._repository.get_entry(entry.session_id, entry.id)
            if existing is not None:
                if not self._same_compaction(existing, entry):
                    raise SessionConflict(f"节点 ID 冲突: {entry.id}")
                committed = await self._repository.get_entry(entry.session_id, baseline.id)
                if committed is None or committed.model_dump() != baseline.model_dump():
                    raise SessionConflict(f"基线节点冲突: {baseline.id}")
                run = await self._queries.require_run(entry.session_id, entry.run_id)
                return CompactionOutcome(
                    created=False,
                    credential_detected=False,
                    entry=existing,
                    baseline=committed,
                    run=run,
                )
            run = await self._queries.require_run(entry.session_id, entry.run_id)
            if run.status != "running":
                raise SessionConflict("运行未在运行，拒绝提交压缩检查点")
            session = await self._queries.require_session(entry.session_id)
            if entry.parent_id != prepared_position:
                raise SessionConflict("压缩检查点父节点与摘要准备位置不一致")
            position = run_position(run)
            if position != prepared_position:
                raise SessionConflict("摘要准备位置与运行当前进度不一致")
            if session.active_leaf_id != position:
                raise SessionConflict("当前分支指针与运行进度不一致")
            entries = await self._repository.list_entries(entry.session_id)
            path = build_session_path(entry.session_id, entries, position)
            # 复用已批准的保留起点结构语义：非法起点在此抛出 ValueError，事务不写入。
            build_effective_context([*path, entry])
            now = now_ms()
            await self._repository.insert_entry(entry)
            await self._repository.insert_entry(baseline)
            run = run.model_copy(update={"last_entry_id": baseline.id})
            await self._repository.update_run(run)
            await set_leaf(self._repository, session, baseline.id, now)
            return CompactionOutcome(
                created=True,
                credential_detected=False,
                entry=entry,
                baseline=baseline,
                run=run,
            )

    def _same_entry(
        self, existing: SessionMessageEntry, incoming: SessionMessageEntry
    ) -> bool:
        return (
            existing.session_id == incoming.session_id
            and existing.parent_id == incoming.parent_id
            and existing.run_id == incoming.run_id
            and existing.type == incoming.type
            and existing.model_omitted == incoming.model_omitted
            and serialize_message(existing.messages[0])
            == serialize_message(incoming.messages[0])
        )

    def _same_compaction(
        self, existing: SessionEntry, incoming: CompactionEntry
    ) -> bool:
        # 重复提交同一检查点按既有节点身份规则处理：载荷一致视为已提交，created_at 不参与比较。
        if not isinstance(existing, CompactionEntry):
            return False
        return existing.model_dump(exclude={"created_at"}) == incoming.model_dump(
            exclude={"created_at"}
        )
