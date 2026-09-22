import json
from collections.abc import Mapping
from typing import Any

import aiosqlite

from app.application.ports import ConversationNotFound
from app.domain.conversations.schema import (
    Conversation,
    ConversationEntry,
    ConversationRun,
    EntryType,
    InvalidConversationPayload,
    RunEvent,
    RunStatus,
    dump_event_payload,
    require_entry_type,
    require_run_status,
    require_run_transition,
    require_text,
    validate_payload,
)
from app.infrastructure.database.connection import Database, require_outer_transaction

_SELECT_CONVERSATION = "SELECT id, title, created_at, updated_at FROM conversations"
_SELECT_ENTRY = (
    "SELECT id, conversation_id, sequence, entry_type, payload_json, created_at"
    " FROM conversation_entries"
)
_SELECT_ENTRY_ID = "SELECT id FROM conversation_entries WHERE id = ? AND conversation_id = ?"
_SELECT_NEXT_SEQUENCE = (
    "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM conversation_entries"
    " WHERE conversation_id = ?"
)
_SELECT_RUN = (
    "SELECT id, conversation_id, thread_id, client_request_id, user_entry_id,"
    " assistant_entry_id, status, error_code, created_at, updated_at FROM conversation_runs"
)
_SELECT_EVENT = (
    "SELECT id, run_id, sequence, event_type, payload_json, created_at"
    " FROM conversation_run_events"
)

_UNFINISHED_STATUSES = ("pending", "running")

_INSERT_CONVERSATION = (
    "INSERT INTO conversations (id, title, created_at, updated_at)"
    " VALUES (?, ?, ?, ?)"
)
_INSERT_ENTRY = (
    "INSERT INTO conversation_entries (id, conversation_id, sequence, entry_type,"
    " payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)"
)
_TOUCH_CONVERSATION = "UPDATE conversations SET updated_at = ? WHERE id = ?"
_INSERT_RUN = (
    "INSERT INTO conversation_runs (id, conversation_id, thread_id, client_request_id,"
    " user_entry_id, assistant_entry_id, status, error_code, created_at, updated_at)"
    " VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?)"
)
_INSERT_EVENT = (
    "INSERT INTO conversation_run_events (run_id, sequence, event_type, payload_json,"
    " created_at) VALUES (?, ?, ?, ?, ?)"
)


class RunNotFound(LookupError):
    """Run 身份不存在：调用方给了不存在的 run_id。"""


async def _next_sequence_in_transaction(
    conn: aiosqlite.Connection, conversation_id: str
) -> int:
    """同一会话的下一个追加序号（空会话为 1）：由会话事务锁串行化，不并发取号。"""
    async with conn.execute(_SELECT_NEXT_SEQUENCE, (conversation_id,)) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise RuntimeError(f"会话序号读取失败：{conversation_id}")
    return int(row["next_sequence"])


async def _read_conversation_in_transaction(
    conn: aiosqlite.Connection, conversation_id: str
) -> Conversation:
    """事务内读会话；不存在即 :class:`ConversationNotFound`。"""
    async with conn.execute(
        _SELECT_CONVERSATION + " WHERE id = ?", (conversation_id,)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise ConversationNotFound(f"会话不存在：{conversation_id}")
    return Conversation.from_row(dict(row))


async def _read_entry_in_transaction(
    conn: aiosqlite.Connection, entry_id: str
) -> ConversationEntry | None:
    async with conn.execute(_SELECT_ENTRY + " WHERE id = ?", (entry_id,)) as cursor:
        row = await cursor.fetchone()
    return None if row is None else ConversationEntry.from_row(dict(row))


async def _read_run_in_transaction(
    conn: aiosqlite.Connection, run_id: str
) -> ConversationRun:
    async with conn.execute(_SELECT_RUN + " WHERE id = ?", (run_id,)) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise RunNotFound(f"Run 不存在：{run_id}")
    return ConversationRun.from_row(dict(row))


async def _require_compaction_boundary_in_transaction(
    conn: aiosqlite.Connection, conversation_id: str, first_kept_entry_id: str
) -> None:
    """compaction 边界必须是本会话已落库的 Entry（Pi buildContextEntries 的保留起点语义）。"""
    async with conn.execute(
        _SELECT_ENTRY_ID, (first_kept_entry_id, conversation_id)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise InvalidConversationPayload(
            "compaction.first_kept_entry_id 必须属于本会话："
            f"{first_kept_entry_id!r}"
        )


async def _append_entry_in_transaction(
    conn: aiosqlite.Connection,
    *,
    conversation_id: str,
    entry_id: str,
    entry_type: EntryType,
    payload: Mapping[str, Any],
    created_at: str,
) -> ConversationEntry:
    """append-only 写入：序号在事务内取当前最大值 +1，同事务推进会话 ``updated_at``。"""
    require_text(entry_id, "entry_id")
    require_text(created_at, "created_at")
    await _read_conversation_in_transaction(conn, conversation_id)
    sequence = await _next_sequence_in_transaction(conn, conversation_id)
    payload_json = json.dumps(validate_payload(entry_type, payload), ensure_ascii=False)
    if entry_type == "compaction":
        await _require_compaction_boundary_in_transaction(
            conn,
            conversation_id,
            str(payload["first_kept_entry_id"]),
        )
    cursor = await conn.execute(
        _INSERT_ENTRY,
        (entry_id, conversation_id, sequence, entry_type, payload_json, created_at),
    )
    await cursor.close()
    touched_cursor = await conn.execute(
        _TOUCH_CONVERSATION, (created_at, conversation_id)
    )
    try:
        touched = touched_cursor.rowcount
    finally:
        await touched_cursor.close()
    if touched != 1:
        raise RuntimeError(f"会话 updated_at 推进影响行数异常：{touched}")
    return ConversationEntry(
        id=entry_id,
        conversation_id=conversation_id,
        sequence=sequence,
        entry_type=entry_type,
        payload=json.loads(payload_json),
        created_at=created_at,
    )


class ConversationRepo:
    """对话历史的读写与事务原语；本层不生成身份、不读系统时间。"""

    def __init__(self, db: Database):
        self._db = db

    # ---------- 读查询 ----------

    async def list_conversations(self) -> tuple[Conversation, ...]:
        """全部会话，按 ``updated_at`` 降序（plan §3.7：列表排序）。"""

        async def op(conn: aiosqlite.Connection) -> tuple[Conversation, ...]:
            async with conn.execute(
                _SELECT_CONVERSATION + " ORDER BY updated_at DESC, id DESC"
            ) as cursor:
                rows = await cursor.fetchall()
            return tuple(Conversation.from_row(dict(row)) for row in rows)

        return await self._db.under_lock(op)

    async def read_conversation(self, conversation_id: str) -> Conversation | None:
        """按身份读取会话；不存在即 None。"""

        async def op(conn: aiosqlite.Connection) -> Conversation | None:
            async with conn.execute(
                _SELECT_CONVERSATION + " WHERE id = ?", (conversation_id,)
            ) as cursor:
                row = await cursor.fetchone()
            return None if row is None else Conversation.from_row(dict(row))

        return await self._db.under_lock(op)

    async def list_entries(self, conversation_id: str) -> tuple[ConversationEntry, ...]:
        """会话的全部 Entry（按 ``sequence`` 升序）。"""

        async def op(conn: aiosqlite.Connection) -> tuple[ConversationEntry, ...]:
            async with conn.execute(
                _SELECT_ENTRY + " WHERE conversation_id = ? ORDER BY sequence",
                (conversation_id,),
            ) as cursor:
                rows = await cursor.fetchall()
            return tuple(ConversationEntry.from_row(dict(row)) for row in rows)

        return await self._db.under_lock(op)

    async def read_latest_compaction(
        self, conversation_id: str
    ) -> ConversationEntry | None:
        """序号最大的 compaction Entry；会话中没有即 None。"""

        async def op(conn: aiosqlite.Connection) -> ConversationEntry | None:
            await _read_conversation_in_transaction(conn, conversation_id)
            async with conn.execute(
                _SELECT_ENTRY
                + " WHERE conversation_id = ? AND entry_type = 'compaction'"
                " ORDER BY sequence DESC LIMIT 1",
                (conversation_id,),
            ) as cursor:
                row = await cursor.fetchone()
            return None if row is None else ConversationEntry.from_row(dict(row))

        return await self._db.under_lock(op)

    async def read_run(self, run_id: str) -> ConversationRun | None:
        """按身份读取 Run；不存在即 None。"""

        async def op(conn: aiosqlite.Connection) -> ConversationRun | None:
            async with conn.execute(_SELECT_RUN + " WHERE id = ?", (run_id,)) as cursor:
                row = await cursor.fetchone()
            return None if row is None else ConversationRun.from_row(dict(row))

        return await self._db.under_lock(op)

    async def read_run_by_client_request_id(
        self, client_request_id: str
    ) -> ConversationRun | None:
        """幂等读取：同一 ``client_request_id`` 对应唯一 Run；没有即 None。"""
        require_text(client_request_id, "client_request_id")
        return await self._db.under_lock(
            lambda conn: _read_run_by_request_id_in_transaction(conn, client_request_id)
        )

    async def list_runs(self, conversation_id: str) -> tuple[ConversationRun, ...]:
        """会话的全部 Run（按 ``created_at, id`` 升序）。"""

        async def op(conn: aiosqlite.Connection) -> tuple[ConversationRun, ...]:
            async with conn.execute(
                _SELECT_RUN
                + " WHERE conversation_id = ? ORDER BY created_at, id",
                (conversation_id,),
            ) as cursor:
                rows = await cursor.fetchall()
            return tuple(ConversationRun.from_row(dict(row)) for row in rows)

        return await self._db.under_lock(op)

    async def read_run_by_thread_id(self, thread_id: str) -> ConversationRun | None:
        """按 LangGraph thread 身份读取 Run；没有即 None（确认动作据此解析来源 Run）。"""
        require_text(thread_id, "thread_id")

        async def op(conn: aiosqlite.Connection) -> ConversationRun | None:
            async with conn.execute(
                _SELECT_RUN + " WHERE thread_id = ?", (thread_id,)
            ) as cursor:
                row = await cursor.fetchone()
            return None if row is None else ConversationRun.from_row(dict(row))

        return await self._db.under_lock(op)

    async def read_confirmations(
        self, run_id: str, action: str
    ) -> tuple[ConversationEntry, ...]:
        """来源 Run 的某类确认 Entry（按 ``sequence`` 升序）；没有即空元组。"""
        require_text(run_id, "run_id")
        return await self._db.under_lock(
            lambda conn: _read_confirmations_in_transaction(conn, run_id, action)
        )

    async def read_confirmations_in_transaction(
        self, conn: aiosqlite.Connection, run_id: str, action: str
    ) -> tuple[ConversationEntry, ...]:
        """事务内版本：与紧随其后的写入同一份快照，确认幂等判定因此原子。"""
        require_outer_transaction(conn, "确认 Entry 事务内读取")
        return await _read_confirmations_in_transaction(conn, run_id, action)

    async def list_run_events(self, run_id: str) -> tuple[RunEvent, ...]:
        """某 Run 的全部事件（按 ``sequence`` 升序）。"""

        async def op(conn: aiosqlite.Connection) -> tuple[RunEvent, ...]:
            async with conn.execute(
                _SELECT_EVENT + " WHERE run_id = ? ORDER BY sequence", (run_id,)
            ) as cursor:
                rows = await cursor.fetchall()
            return tuple(RunEvent.from_row(dict(row)) for row in rows)

        return await self._db.under_lock(op)

    # ---------- 事务原语（调用方持有 Database.transaction()） ----------

    async def append_entry_in_transaction(
        self,
        conn: aiosqlite.Connection,
        *,
        conversation_id: str,
        entry_id: str,
        entry_type: str,
        payload: Mapping[str, Any],
        created_at: str,
    ) -> ConversationEntry:
        """追加一个 Entry（append-only）：序号在事务内推进，会话 ``updated_at`` 同步更新。"""
        require_outer_transaction(conn, "Entry 追加")
        return await _append_entry_in_transaction(
            conn,
            conversation_id=conversation_id,
            entry_id=entry_id,
            entry_type=require_entry_type(entry_type),
            payload=payload,
            created_at=created_at,
        )

    async def begin_run_in_transaction(
        self,
        conn: aiosqlite.Connection,
        *,
        conversation_id: str,
        run_id: str,
        thread_id: str,
        client_request_id: str,
        entry_id: str,
        content: str,
        created_at: str,
    ) -> tuple[ConversationRun, ConversationEntry]:
        """用户 Entry 与 ``pending`` Run 同事务提交；``client_request_id`` 已存在即原样返回。"""
        require_outer_transaction(conn, "用户 Entry 与 pending Run 同事务写入")
        for value, what in (
            (run_id, "run_id"),
            (thread_id, "thread_id"),
            (client_request_id, "client_request_id"),
            (created_at, "created_at"),
        ):
            require_text(value, what)
        existing = await _read_run_by_request_id_in_transaction(conn, client_request_id)
        if existing is not None:
            if (
                existing.conversation_id != conversation_id
                or existing.thread_id != thread_id
            ):
                raise ValueError(
                    "client_request_id 已属于另一轮请求："
                    f"request={client_request_id!r} run={existing.id!r}"
                )
            user_entry = await _read_entry_in_transaction(conn, existing.user_entry_id)
            if user_entry is None:
                raise RuntimeError(f"Run 关联的用户 Entry 缺失：{existing.user_entry_id}")
            return existing, user_entry
        user_entry = await _append_entry_in_transaction(
            conn,
            conversation_id=conversation_id,
            entry_id=entry_id,
            entry_type="message",
            payload={
                "role": "user",
                "content": content,
                "status": "complete",
                "run_id": run_id,
            },
            created_at=created_at,
        )
        cursor = await conn.execute(
            _INSERT_RUN,
            (
                run_id,
                conversation_id,
                thread_id,
                client_request_id,
                entry_id,
                "pending",
                created_at,
                created_at,
            ),
        )
        await cursor.close()
        return (
            ConversationRun(
                id=run_id,
                conversation_id=conversation_id,
                thread_id=thread_id,
                client_request_id=client_request_id,
                user_entry_id=entry_id,
                assistant_entry_id=None,
                status="pending",
                error_code=None,
                created_at=created_at,
                updated_at=created_at,
            ),
            user_entry,
        )

    async def complete_run_in_transaction(
        self,
        conn: aiosqlite.Connection,
        run_id: str,
        *,
        entry_id: str,
        content: str,
        created_at: str,
        usage: Mapping[str, Any] | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> tuple[ConversationRun, ConversationEntry]:
        """完整 Assistant Entry 与 Run ``completed`` 同事务提交。"""
        require_outer_transaction(conn, "Assistant Entry 与 completed Run 同事务写入")
        run = await _read_run_in_transaction(conn, run_id)
        require_run_transition(run.status, "completed")
        assistant_entry = await _append_entry_in_transaction(
            conn,
            conversation_id=run.conversation_id,
            entry_id=entry_id,
            entry_type="message",
            payload={
                "role": "assistant",
                "content": content,
                "status": "complete",
                "run_id": run_id,
                "usage": usage,
                "provider": provider,
                "model": model,
            },
            created_at=created_at,
        )
        updated = await _update_run_status_in_transaction(
            conn,
            run_id,
            status="completed",
            updated_at=created_at,
            assistant_entry_id=entry_id,
        )
        return updated, assistant_entry

    async def update_run_with_event_in_transaction(
        self,
        conn: aiosqlite.Connection,
        run_id: str,
        *,
        event_type: str,
        sequence: int,
        payload: Mapping[str, Any],
        status: str,
        created_at: str,
        error_code: str | None = None,
    ) -> tuple[ConversationRun, RunEvent]:
        """Event 与对应 Run 状态同事务提交（``waiting`` Event + ``waiting``，``error`` Event + ``failed``）。"""
        require_outer_transaction(
            conn, f"{event_type} Event 与 Run {status} 同事务写入"
        )
        event = await self.append_event_in_transaction(
            conn,
            run_id=run_id,
            sequence=sequence,
            event_type=event_type,
            payload=payload,
            created_at=created_at,
        )
        run = await self.update_run_status_in_transaction(
            conn,
            run_id,
            status=status,
            updated_at=created_at,
            error_code=error_code,
        )
        return run, event

    async def finish_run_in_transaction(
        self,
        conn: aiosqlite.Connection,
        run_id: str,
        *,
        sequence: int,
        payload: Mapping[str, Any],
        content: str,
        entry_id: str,
        created_at: str,
    ) -> tuple[ConversationRun, RunEvent]:
        """``done`` Event 与终态同事务提交。

        已发出 ``waiting`` 的 Run 保持 ``waiting``（确认动作才是它的终态），不写完整 Assistant
        Entry；``waiting`` 的 ``message`` Event 仍留在事件流里供展示。

        非 ``waiting`` 的空白内容不是有效终态：写 ``done`` 之前即失败，事务回滚，Run 不会
        停在 ``running``。
        """
        require_outer_transaction(conn, "done Event 与 Run 终态同事务写入")
        current = await _read_run_in_transaction(conn, run_id)
        if current.status != "waiting" and not content.strip():
            raise InvalidConversationPayload(
                f"非 waiting 的 Run 收到空白 Assistant 结果，拒绝提交 done：run={run_id!r}"
            )
        event = await self.append_event_in_transaction(
            conn,
            run_id=run_id,
            sequence=sequence,
            event_type="done",
            payload=payload,
            created_at=created_at,
        )
        if current.status == "waiting":
            return current, event
        run, _entry = await self.complete_run_in_transaction(
            conn,
            run_id,
            entry_id=entry_id,
            content=content,
            created_at=created_at,
        )
        return run, event

    async def append_confirmation_once_in_transaction(
        self,
        conn: aiosqlite.Connection,
        *,
        conversation_id: str,
        run_id: str,
        entry_id: str,
        action: str,
        text: str,
        draft_plan_id: int | None = None,
        workout_session_id: int | None = None,
        created_at: str,
    ) -> ConversationEntry | None:
        """同一来源 Run、同一动作、同一业务身份的确认 Entry 只写一次。

        已存在时返回 ``None``；检查与追加在同一事务内完成，不嵌套取锁。
        """
        require_outer_transaction(conn, "confirmation Entry 幂等追加")
        run = await _read_run_in_transaction(conn, run_id)
        if run.conversation_id != conversation_id:
            raise InvalidConversationPayload(
                f"Run 不属于该会话：{run.conversation_id!r} != {conversation_id!r}"
            )
        if await _read_confirmation_in_transaction(
            conn,
            run_id=run_id,
            action=action,
            draft_plan_id=draft_plan_id,
            workout_session_id=workout_session_id,
        ) is not None:
            return None
        payload: dict[str, Any] = {"action": action, "run_id": run_id, "text": text}
        if action in ("plan_confirmed", "plan_rejected"):
            payload["draft_plan_id"] = draft_plan_id
        else:
            payload["workout_session_id"] = workout_session_id
        return await _append_entry_in_transaction(
            conn,
            conversation_id=conversation_id,
            entry_id=entry_id,
            entry_type="confirmation",
            payload=payload,
            created_at=created_at,
        )

    async def append_event_in_transaction(
        self,
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        sequence: int,
        event_type: str,
        payload: Mapping[str, Any],
        created_at: str,
    ) -> RunEvent:
        """追加一个有序 Run Event；``(run_id, sequence)`` 重复由 UNIQUE 拒绝。"""
        require_outer_transaction(conn, "Run Event 追加")
        require_text(created_at, "created_at")
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            raise ValueError(f"sequence 必须是整数：{sequence!r}")
        payload_json = dump_event_payload(payload)
        cursor = await conn.execute(
            _INSERT_EVENT, (run_id, sequence, event_type, payload_json, created_at)
        )
        event_id = cursor.lastrowid
        await cursor.close()
        if event_id is None:
            raise RuntimeError("Run Event 写入未返回 rowid")
        return RunEvent(
            id=int(event_id),
            run_id=run_id,
            sequence=sequence,
            event_type=event_type,
            payload=dict(payload),
            created_at=created_at,
        )

    async def update_run_status_in_transaction(
        self,
        conn: aiosqlite.Connection,
        run_id: str,
        *,
        status: str,
        updated_at: str,
        error_code: str | None = None,
    ) -> ConversationRun:
        """更新 Run 状态与 ``updated_at``：按单向迁移矩阵校验，``failed`` 必带 ``error_code``。

        ``assistant_entry_id`` 不由本入口设置：只有 :meth:`complete_run_in_transaction` 能经
        私有 helper 在同事务内绑定 Assistant Entry，本入口无法绕过该校验。
        """
        require_outer_transaction(conn, "Run 状态更新")
        return await _update_run_status_in_transaction(
            conn,
            run_id,
            status=require_run_status(status),
            updated_at=updated_at,
            error_code=error_code,
        )

    # ---------- 自身事务的独立操作 ----------

    async def create_conversation(
        self, *, conversation_id: str, title: str, created_at: str
    ) -> Conversation:
        """新建会话：身份与时间戳由调用方提供，Entry 序号从 1 开始。"""
        require_text(conversation_id, "conversation_id")
        require_text(title, "title")
        require_text(created_at, "created_at")
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                _INSERT_CONVERSATION,
                (conversation_id, title, created_at, created_at),
            )
            await cursor.close()
        return Conversation(
            id=conversation_id,
            title=title,
            created_at=created_at,
            updated_at=created_at,
        )

    async def delete_conversation(self, conversation_id: str) -> bool:
        """删除会话：entries／runs／events 由外键级联清理；未命中即 False。"""
        require_text(conversation_id, "conversation_id")
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "DELETE FROM conversations WHERE id = ?", (conversation_id,)
            )
            try:
                deleted = cursor.rowcount
            finally:
                await cursor.close()
        return deleted == 1

    async def converge_unfinished_runs(
        self, *, error_code: str, updated_at: str
    ) -> int:
        """启动收敛：遗留 ``pending``／``running`` 一律标记 ``failed``，已提交 Event 保留。"""
        require_text(error_code, "error_code")
        require_text(updated_at, "updated_at")
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE conversation_runs SET status = 'failed', error_code = ?,"
                " updated_at = ? WHERE status IN (?, ?)",
                (error_code, updated_at, *_UNFINISHED_STATUSES),
            )
            try:
                converged = cursor.rowcount
            finally:
                await cursor.close()
        return converged

    async def cancel_active_run(self, run_id: str, *, updated_at: str) -> bool:
        """客户端断开收敛：仍活动的 Run 转 ``cancelled``，返回是否发生迁移；其余状态保持不动。

        只有 :data:`_UNFINISHED_STATUSES` 是活动态，可走这一迁移；终态不复活，
        ``waiting`` 已把确认事实提交在同一事务里，刷新后要按它恢复确认流程，断连不得把它
        降级为 ``cancelled``。判定与写入同事务，迁移矩阵仍由写入路径校验。
        """
        require_text(run_id, "run_id")
        require_text(updated_at, "updated_at")
        async with self._db.transaction() as conn:
            current = await _read_run_in_transaction(conn, run_id)
            if current.status not in _UNFINISHED_STATUSES:
                return False
            await _update_run_status_in_transaction(
                conn, run_id, status="cancelled", updated_at=updated_at
            )
            return True


async def _read_run_by_request_id_in_transaction(
    conn: aiosqlite.Connection, client_request_id: str
) -> ConversationRun | None:
    async with conn.execute(
        _SELECT_RUN + " WHERE client_request_id = ?", (client_request_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return None if row is None else ConversationRun.from_row(dict(row))


async def _read_confirmations_in_transaction(
    conn: aiosqlite.Connection, run_id: str, action: str
) -> tuple[ConversationEntry, ...]:
    """来源 Run ＋ 动作的全部 confirmation Entry（按 ``sequence`` 升序）。"""
    run = await _read_run_in_transaction(conn, run_id)
    async with conn.execute(
        _SELECT_ENTRY
        + " WHERE conversation_id = ? AND entry_type = 'confirmation'"
        " ORDER BY sequence",
        (run.conversation_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    entries: list[ConversationEntry] = []
    for row in rows:
        entry = ConversationEntry.from_row(dict(row))
        payload = entry.payload
        if payload["run_id"] == run_id and payload["action"] == action:
            entries.append(entry)
    return tuple(entries)


async def _read_confirmation_in_transaction(
    conn: aiosqlite.Connection,
    *,
    run_id: str,
    action: str,
    draft_plan_id: int | None,
    workout_session_id: int | None,
) -> ConversationEntry | None:
    """同一来源 Run 的同一动作与同一业务身份是否已有 confirmation Entry。"""
    for entry in await _read_confirmations_in_transaction(conn, run_id, action):
        payload = entry.payload
        if action in ("plan_confirmed", "plan_rejected"):
            if payload["draft_plan_id"] == draft_plan_id:
                return entry
            continue
        if payload["workout_session_id"] == workout_session_id:
            return entry
    return None


async def _require_completed_assistant_entry(
    conn: aiosqlite.Connection, run: ConversationRun, assistant_entry_id: str
) -> None:
    """completed 前校验 Assistant Entry：存在、同会话、assistant message、complete、run_id 匹配。"""
    require_text(assistant_entry_id, "assistant_entry_id")
    entry = await _read_entry_in_transaction(conn, assistant_entry_id)
    if entry is None:
        raise InvalidConversationPayload(f"Assistant Entry 不存在：{assistant_entry_id!r}")
    if entry.conversation_id != run.conversation_id:
        raise InvalidConversationPayload(
            "Assistant Entry 属于其它会话："
            f"{entry.conversation_id!r} != {run.conversation_id!r}"
        )
    if entry.entry_type != "message" or entry.payload.get("role") != "assistant":
        raise InvalidConversationPayload(
            f"Assistant Entry 不是 assistant message：{assistant_entry_id!r}"
        )
    if entry.payload.get("status") != "complete":
        raise InvalidConversationPayload(
            f"Assistant Entry 状态不是 complete：{entry.payload.get('status')!r}"
        )
    if entry.payload.get("run_id") != run.id:
        raise InvalidConversationPayload(
            f"Assistant Entry 的 run_id 不匹配：{entry.payload.get('run_id')!r} != {run.id!r}"
        )


async def _update_run_status_in_transaction(
    conn: aiosqlite.Connection,
    run_id: str,
    *,
    status: RunStatus,
    updated_at: str,
    error_code: str | None = None,
    assistant_entry_id: str | None = None,
) -> ConversationRun:
    """状态更新的唯一实现：矩阵／error_code 契约 + completed 必须有本次显式提供的完整 Assistant Entry。"""
    require_text(updated_at, "updated_at")
    current = await _read_run_in_transaction(conn, run_id)
    require_run_transition(current.status, status)
    if status == "failed":
        if not error_code:
            raise InvalidConversationPayload("Run 转入 failed 必须带 error_code")
    elif error_code is not None:
        raise InvalidConversationPayload(f"{status} 状态不得携带 error_code：{error_code!r}")
    if status == "completed":
        if assistant_entry_id is None:
            raise InvalidConversationPayload(
                "completed 状态必须本次显式提供 assistant_entry_id"
            )
        await _require_completed_assistant_entry(conn, current, assistant_entry_id)
    elif assistant_entry_id is not None:
        raise InvalidConversationPayload(
            f"{status} 状态不得携带 assistant_entry_id：{assistant_entry_id!r}"
        )
    cursor = await conn.execute(
        "UPDATE conversation_runs SET status = ?, error_code = ?,"
        " assistant_entry_id = COALESCE(?, assistant_entry_id), updated_at = ?"
        " WHERE id = ?",
        (status, error_code, assistant_entry_id, updated_at, run_id),
    )
    try:
        changed = cursor.rowcount
    finally:
        await cursor.close()
    if changed != 1:
        raise RuntimeError(f"Run 状态更新影响行数异常：{changed}")
    return await _read_run_in_transaction(conn, run_id)
