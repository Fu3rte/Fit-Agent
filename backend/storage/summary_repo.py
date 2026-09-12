"""历史摘要持久化：覆盖范围、来源关联与「提交成功才启用」（07 7.4 摘要持久化；stage4.md S4-06a）。

- 摘要只是派生的上下文辅助，不是业务事实源；**原消息永不删除**，摘要行只追加。
- **提交成功才启用**：``commit_summary`` 在同一事务内条件校验生成它的 Run 仍是 ``running``；
  取消先发生则抛 :class:`~storage.errors.RunStateConflict`，不落库、也不补写任何诊断快照
  （07 7.4 取消后不补存晚到快照）。
- **存储失败不降级继续**：任一步失败整体回滚并把异常原样上抛；调用方（压缩流程）据此终止，
  不得吞掉异常后拿旧上下文装作摘要成功。
- **覆盖范围是会话前缀**：首条摘要覆盖会话最早消息起的一段，后续摘要必须包含旧覆盖并向前
  扩展，因此最新有效摘要唯一（``covered_to_seq`` 最大，同值按插入顺序取后写的一条），
  投影才能用「摘要 + seq 大于覆盖终点的原消息」重建历史而不静默丢消息。原消息仍在
  ``messages`` 中逐条可追溯，``summary_sources`` 记录该摘要输入用到的消息行。

物理行粒度与字段为 07 允许的实现细节；本层不 import PydanticAI，也不生成摘要文本。
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import aiosqlite

from storage.db import Database
from storage.errors import InvalidInput, NotFound, RunStateConflict

_SUMMARY_COLUMNS = (
    "id, conversation_id, run_id, content, covered_from_seq, covered_to_seq, created_at"
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _require_positive_int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidInput(f"{what} 必须是正整数: {value!r}")
    return value


def _stored_int(raw: Any) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError("摘要整数列无法解析") from exc


class SummaryRepo:
    """摘要两表读写；全部访问经 Database 的唯一锁串行化（07 7.1）。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ---------- 提交（唯一启用路径） ----------

    async def commit_summary(
        self,
        *,
        run_id: str,
        content: str,
        covered_from_seq: int,
        covered_to_seq: int,
        source_message_ids: Sequence[int],
    ) -> dict[str, Any]:
        """在一个事务内提交一条摘要；成功即成为（新的）有效摘要。

        顺序即契约：

        1. 生成该摘要的 Run 必须仍为 ``running``——取消先提交则 :class:`RunStateConflict`，
           本摘要不启用，且本方法不写任何迟到诊断快照；
        2. 覆盖区间必须完整落在此会话已保存的消息上（``from ≤ to``、区间无空洞）；
        3. 覆盖必须是会话前缀的扩展：首条覆盖从会话最早消息起，后续覆盖包含旧覆盖并且终点
           前移，否则 :class:`InvalidInput`（防止投影静默丢历史）；
        4. 每条来源消息必须真实存在且落在覆盖区间内（不落区间外的悬空来源）。

        任一步失败（含底层存储错误）整体回滚并把异常上抛：调用方必须终止压缩，不得降级继续。
        """
        if not isinstance(content, str) or not content:
            raise InvalidInput("摘要内容必须是非空字符串")
        from_seq = _require_positive_int(covered_from_seq, "覆盖起点 seq")
        to_seq = _require_positive_int(covered_to_seq, "覆盖终点 seq")
        if from_seq > to_seq:
            raise InvalidInput(f"覆盖区间不合法（起点 {from_seq} > 终点 {to_seq}）")
        source_ids = list(source_message_ids)
        if not source_ids:
            raise InvalidInput("摘要至少关联一条来源消息")
        if len(set(source_ids)) != len(source_ids):
            raise InvalidInput("来源消息 id 不得重复")
        for message_id in source_ids:
            _require_positive_int(message_id, "来源消息 id")

        summary_id = uuid4().hex
        created_at = _now()
        async with self._db.transaction() as conn:
            run = await self._run_locked(conn, run_id)
            if run["status"] != "running":
                raise RunStateConflict(
                    f"仅 running Run 可提交摘要（当前 {run['status']}）: {run_id}"
                )
            conversation_id = str(run["conversation_id"])
            covered_ids = await self._covered_message_ids_locked(
                conn, conversation_id, from_seq, to_seq
            )
            previous = await self._active_summary_locked(conn, conversation_id)
            if previous is None:
                first_seq = await self._first_message_seq_locked(conn, conversation_id)
                required_from = _stored_int(first_seq)
            else:
                required_from = _stored_int(previous["covered_from_seq"])
            if from_seq > required_from:
                raise InvalidInput(
                    f"摘要覆盖必须从会话最早未摘要消息（seq {required_from}）起，"
                    f"不得留下更早消息: {from_seq}"
                )
            if previous is not None and to_seq <= _stored_int(
                previous["covered_to_seq"]
            ):
                raise InvalidInput(
                    f"新摘要必须扩展覆盖范围（旧覆盖终点 {previous['covered_to_seq']}，"
                    f"新终点 {to_seq}）"
                )
            unknown = [
                message_id for message_id in source_ids if message_id not in covered_ids
            ]
            if unknown:
                raise InvalidInput(f"来源消息不在覆盖区间内: {unknown}")
            await self._insert_summary_locked(
                conn,
                summary_id=summary_id,
                conversation_id=conversation_id,
                run_id=run_id,
                content=content,
                covered_from_seq=from_seq,
                covered_to_seq=to_seq,
                created_at=created_at,
            )
            # 单独方法便于故障注入测试“摘要行已插入、来源关联步骤失败”的中途回滚。
            await self._insert_summary_sources_locked(
                conn, summary_id=summary_id, message_ids=source_ids
            )
            return {
                "id": summary_id,
                "conversation_id": conversation_id,
                "run_id": run_id,
                "content": content,
                "covered_from_seq": from_seq,
                "covered_to_seq": to_seq,
                "created_at": created_at,
            }

    async def _insert_summary_locked(
        self,
        conn: aiosqlite.Connection,
        *,
        summary_id: str,
        conversation_id: str,
        run_id: str,
        content: str,
        covered_from_seq: int,
        covered_to_seq: int,
        created_at: str,
    ) -> None:
        await conn.execute(
            "INSERT INTO summaries (id, conversation_id, run_id, content,"
            " covered_from_seq, covered_to_seq, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                summary_id,
                conversation_id,
                run_id,
                content,
                covered_from_seq,
                covered_to_seq,
                created_at,
            ),
        )

    async def _insert_summary_sources_locked(
        self,
        conn: aiosqlite.Connection,
        *,
        summary_id: str,
        message_ids: Sequence[int],
    ) -> None:
        for message_id in message_ids:
            await conn.execute(
                "INSERT INTO summary_sources (summary_id, message_id) VALUES (?, ?)",
                (summary_id, message_id),
            )

    # ---------- 事务内校验读取 ----------

    async def _run_locked(
        self, conn: aiosqlite.Connection, run_id: str
    ) -> aiosqlite.Row:
        async with conn.execute(
            "SELECT status, conversation_id FROM runs WHERE id = ?", (run_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise NotFound(f"提交摘要的 Run 不存在: {run_id}")
        return row

    async def _covered_message_ids_locked(
        self,
        conn: aiosqlite.Connection,
        conversation_id: str,
        from_seq: int,
        to_seq: int,
    ) -> list[int]:
        """覆盖区间的会话消息行 id；区间必须无空洞且非空，否则拒绝（覆盖范围必须精确）。"""
        async with conn.execute(
            "SELECT id, seq FROM messages WHERE conversation_id = ? AND seq BETWEEN ? AND ?"
            " ORDER BY seq",
            (conversation_id, from_seq, to_seq),
        ) as cursor:
            rows = list(await cursor.fetchall())
        if len(rows) != to_seq - from_seq + 1:
            raise InvalidInput(
                f"摘要覆盖区间必须完整落在已保存消息上（会话 {conversation_id}，"
                f"seq {from_seq}–{to_seq}，实际 {len(rows)} 条）"
            )
        return [_stored_int(row["id"]) for row in rows]

    async def _first_message_seq_locked(
        self, conn: aiosqlite.Connection, conversation_id: str
    ) -> int:
        async with conn.execute(
            "SELECT MIN(seq) FROM messages WHERE conversation_id = ?",
            (conversation_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or row[0] is None:
            raise InvalidInput(f"会话没有可摘要的消息: {conversation_id}")
        return _stored_int(row[0])

    async def _active_summary_locked(
        self, conn: aiosqlite.Connection, conversation_id: str
    ) -> aiosqlite.Row | None:
        """最新有效摘要：覆盖终点最大；同终点按插入顺序取后写的一条（确定性投影键）。"""
        async with conn.execute(
            f"SELECT {_SUMMARY_COLUMNS} FROM summaries WHERE conversation_id = ?"
            " ORDER BY covered_to_seq DESC, rowid DESC LIMIT 1",
            (conversation_id,),
        ) as cursor:
            return await cursor.fetchone()

    # ---------- 读取 ----------

    async def get_active_summary(self, conversation_id: str) -> dict[str, Any] | None:
        """当前有效摘要（投影只用这一条 + 覆盖终点之后的原消息）；无则 ``None``。"""

        async def op(conn: aiosqlite.Connection) -> aiosqlite.Row | None:
            return await self._active_summary_locked(conn, conversation_id)

        row = await self._db.under_lock(op)
        return None if row is None else dict(row)

    async def list_summaries(self, conversation_id: str) -> list[dict[str, Any]]:
        """已提交摘要的完整序列（按覆盖终点升序）；供追溯，不删旧行、不改原消息。"""

        async def op(conn: aiosqlite.Connection) -> list[aiosqlite.Row]:
            async with conn.execute(
                f"SELECT {_SUMMARY_COLUMNS} FROM summaries WHERE conversation_id = ?"
                " ORDER BY covered_to_seq, rowid",
                (conversation_id,),
            ) as cursor:
                return list(await cursor.fetchall())

        return [dict(row) for row in await self._db.under_lock(op)]

    async def list_source_message_ids(self, summary_id: str) -> list[int]:
        """该摘要的来源消息行 id（按消息顺序）；原消息被删除不可能留下悬空来源（外键拒绝）。"""

        async def op(conn: aiosqlite.Connection) -> list[aiosqlite.Row]:
            async with conn.execute(
                "SELECT message_id FROM summary_sources WHERE summary_id = ?"
                " ORDER BY message_id",
                (summary_id,),
            ) as cursor:
                return list(await cursor.fetchall())

        return [_stored_int(row["message_id"]) for row in await self._db.under_lock(op)]
