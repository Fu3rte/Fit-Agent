"""运行时四表 conversations/messages/runs/run_events 读写：幂等查重、条件终态、同事务配对（07 7.4/7.5）。

消息物理行粒度（07 允许实现者确定）：
- ``kind='user_request'``：用户请求应用事实，与 pending Run 同事务创建；
  payload_json = ``{"text": ...}``。
- ``kind='framework'``：单行存单条框架消息；payload_json 由 runtime 层（Stage 4）
  经 PydanticAI ``ModelMessagesTypeAdapter`` 产出（本层不 import PydanticAI，
  只校验 JSON 可解析）；读取按 seq 还原顺序。
- ``kind='partial'``：流式合并文本分批落盘的部分回答，payload_json = ``{"text": ...}``；
  与完整成功 Assistant 消息区分，不回灌为正常回答（07 7.4）。

取消后不补存晚到框架快照；终态后不补写轨迹事件（07 7.4 消息契约）。
``run_events.id`` 仅作数据库行身份，无恢复游标或重放接口。
SQL 全部在本 repo 内以字面量书写并参数化（README 硬规则 3），动态值一律经参数绑定。
"""

import json
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from storage.db import Database
from storage.errors import InvalidInput, NotFound, RunStateConflict

_CANCELLABLE_STATUSES = ("pending", "running")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _validate_json_payload(payload: str) -> None:
    """框架消息负载必须是可解析 JSON（本层不解释其结构，反序列化归 runtime 层）。"""
    try:
        json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise InvalidInput("框架消息负载必须是合法 JSON 字符串") from exc


def _require_rowid(cursor: aiosqlite.Cursor) -> int:
    """INSERT 成功后 AUTOINCREMENT 必有 rowid；None 只可能来自类型宽度，显式拒绝。"""
    if cursor.lastrowid is None:
        raise RuntimeError("INSERT 成功但未返回 rowid")
    return int(cursor.lastrowid)


class RunRepo:
    """运行时四表存储操作；全部访问经 Database 的唯一锁串行化（07 7.1）。"""

    def __init__(self, db: Database):
        self._db = db

    # ---------- 会话 ----------

    async def create_conversation(self, conversation_id: str) -> dict[str, Any]:
        async def op(conn: aiosqlite.Connection) -> None:
            await conn.execute(
                "INSERT INTO conversations (id, created_at) VALUES (?, ?)",
                (conversation_id, _now()),
            )

        await self._db.under_lock(op)
        return {"id": conversation_id}

    async def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        async def op(conn: aiosqlite.Connection) -> aiosqlite.Row | None:
            async with conn.execute(
                "SELECT id, created_at FROM conversations WHERE id = ?",
                (conversation_id,),
            ) as cursor:
                return await cursor.fetchone()

        row = await self._db.under_lock(op)
        return None if row is None else dict(row)

    # ---------- Run 创建与幂等 ----------

    async def create_run_with_user_message(
        self,
        conversation_id: str,
        run_id: str,
        client_request_id: str,
        user_text: str,
        retry_of_run_id: str | None = None,
    ) -> dict[str, Any]:
        """用户消息与 pending Run 同一事务创建（07 7.5）。

        相同 ``client_request_id``（全局唯一幂等键）的重复或并发调用只产生一组记录，
        返回已有 Run（``created=False``）；活跃 Run 检查与 409 归 Stage 4 并发互斥。
        """
        if not isinstance(user_text, str) or not user_text:
            raise InvalidInput("用户请求文本必须是非空字符串")
        async with self._db.transaction() as conn:
            async with conn.execute(
                "SELECT id FROM runs WHERE client_request_id = ?",
                (client_request_id,),
            ) as cursor:
                existing = await cursor.fetchone()
            if existing is not None:
                return {"created": False, "run": await self._get_run_locked(conn, str(existing["id"]))}
            now = _now()
            await conn.execute(
                "INSERT INTO runs (id, conversation_id, client_request_id, status,"
                " error_code, retry_of_run_id, created_at, updated_at)"
                " VALUES (?, ?, ?, 'pending', NULL, ?, ?, ?)",
                (run_id, conversation_id, client_request_id, retry_of_run_id, now, now),
            )
            # 单独方法便于故障注入测试“Run 已插入、用户消息步骤失败”的中途回滚。
            await self._insert_user_message_locked(
                conn,
                conversation_id=conversation_id,
                run_id=run_id,
                user_text=user_text,
            )
            return {"created": True, "run": await self._get_run_locked(conn, run_id)}

    async def _insert_user_message_locked(
        self,
        conn: aiosqlite.Connection,
        *,
        conversation_id: str,
        run_id: str,
        user_text: str,
    ) -> None:
        seq = await self._next_seq_locked(conn, conversation_id)
        await conn.execute(
            "INSERT INTO messages (conversation_id, run_id, seq, role, kind, payload_json)"
            " VALUES (?, ?, ?, 'user', 'user_request', ?)",
            (
                conversation_id,
                run_id,
                seq,
                json.dumps({"text": user_text}, ensure_ascii=False),
            ),
        )

    # ---------- Run 读取 ----------

    async def _get_run_locked(self, conn: aiosqlite.Connection, run_id: str) -> dict[str, Any]:
        async with conn.execute(
            "SELECT id, conversation_id, client_request_id, status, error_code,"
            " retry_of_run_id, created_at, updated_at FROM runs WHERE id = ?",
            (run_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise NotFound(f"Run 不存在: {run_id}")
        return dict(row)

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        async def op(conn: aiosqlite.Connection) -> aiosqlite.Row | None:
            async with conn.execute(
                "SELECT id, conversation_id, client_request_id, status, error_code,"
                " retry_of_run_id, created_at, updated_at FROM runs WHERE id = ?",
                (run_id,),
            ) as cursor:
                return await cursor.fetchone()

        row = await self._db.under_lock(op)
        return None if row is None else dict(row)

    async def get_run_by_client_request_id(self, client_request_id: str) -> dict[str, Any] | None:
        async def op(conn: aiosqlite.Connection) -> aiosqlite.Row | None:
            async with conn.execute(
                "SELECT id FROM runs WHERE client_request_id = ?",
                (client_request_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return None
            async with conn.execute(
                "SELECT id, conversation_id, client_request_id, status, error_code,"
                " retry_of_run_id, created_at, updated_at FROM runs WHERE id = ?",
                (row["id"],),
            ) as cursor:
                return await cursor.fetchone()

        row = await self._db.under_lock(op)
        return None if row is None else dict(row)

    # ---------- 条件终态（07 7.5） ----------

    async def start_run(self, run_id: str) -> dict[str, Any]:
        """条件 pending→running。调度时机与互斥归 Stage 4（08 章）；本层只提供
        7.5 完成路径所需的条件写入。
        """
        async def op(conn: aiosqlite.Connection) -> dict[str, Any]:
            cursor = await conn.execute(
                "UPDATE runs SET status = 'running', updated_at = ?"
                " WHERE id = ? AND status = 'pending'",
                (_now(), run_id),
            )
            if cursor.rowcount != 1:
                raise RunStateConflict(f"仅 pending Run 可进入 running: {run_id}")
            return await self._get_run_locked(conn, run_id)

        return await self._db.under_lock(op)

    async def complete_run(
        self,
        run_id: str,
        framework_messages: list[tuple[str, str]],
    ) -> dict[str, Any]:
        """仅 running Run 可在同一事务写完整框架消息并改 completed（07 7.5）。

        ``framework_messages`` 为 (role, payload_json) 列表，payload 由 runtime 层
        序列化；任一步骤失败整体回滚，不得只完成一半。
        """
        if not framework_messages:
            raise InvalidInput("完成 Run 必须同事务写入至少一条完整框架消息")
        for role, payload in framework_messages:
            if role not in ("user", "assistant"):
                raise InvalidInput(f"未知消息角色: {role!r}")
            _validate_json_payload(payload)
        now = _now()

        async with self._db.transaction() as conn:
            run = await self._get_run_locked(conn, run_id)
            if run["status"] != "running":
                raise RunStateConflict(
                    f"仅 running Run 可完成（当前 {run['status']}）: {run_id}"
                )
            await conn.execute(
                "UPDATE runs SET status = 'completed', updated_at = ?"
                " WHERE id = ? AND status = 'running'",
                (now, run_id),
            )
            for role, payload in framework_messages:
                seq = await self._next_seq_locked(conn, str(run["conversation_id"]))
                await conn.execute(
                    "INSERT INTO messages (conversation_id, run_id, seq, role, kind, payload_json)"
                    " VALUES (?, ?, ?, ?, 'framework', ?)",
                    (run["conversation_id"], run_id, seq, role, payload),
                )
            return await self._get_run_locked(conn, run_id)

    async def cancel_run(self, run_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """取消仅能从 pending/running 条件更新为 cancelled，并同事务追加取消事件（07 7.5）。

        取消成功后，迟到完成不得写入完整 Assistant 消息或覆盖终态（本层由
        complete_run 的条件检查保证）；不得因取消失败留下“取消成功”事件（同事务）。
        """
        now = _now()
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE runs SET status = 'cancelled', updated_at = ?"
                " WHERE id = ? AND status IN ('pending', 'running')",
                (now, run_id),
            )
            if cursor.rowcount != 1:
                raise RunStateConflict(
                    f"仅 pending/running Run 可取消（07 7.5）: {run_id}"
                )
            await conn.execute(
                "INSERT INTO run_events (run_id, event_type, payload_json, created_at)"
                " VALUES (?, 'cancelled', ?, ?)",
                (run_id, json.dumps(payload or {}, ensure_ascii=False), now),
            )
            return await self._get_run_locked(conn, run_id)

    # ---------- 部分回答与轨迹 ----------

    async def save_partial_answer(self, run_id: str, text: str) -> dict[str, Any]:
        """流式合并文本分批落盘；仅 running Run 可写（取消后不补存，07 7.4）。"""
        if not isinstance(text, str) or not text:
            raise InvalidInput("部分回答批次必须是非空字符串")
        async with self._db.transaction() as conn:
            run = await self._get_run_locked(conn, run_id)
            if run["status"] != "running":
                raise RunStateConflict(
                    f"仅 running Run 可保存部分回答（当前 {run['status']}）: {run_id}"
                )
            seq = await self._next_seq_locked(conn, str(run["conversation_id"]))
            await conn.execute(
                "INSERT INTO messages (conversation_id, run_id, seq, role, kind, payload_json)"
                " VALUES (?, ?, ?, 'assistant', 'partial', ?)",
                (
                    run["conversation_id"],
                    run_id,
                    seq,
                    json.dumps({"text": text}, ensure_ascii=False),
                ),
            )
            return {"run_id": run_id, "seq": seq, "text": text}

    async def append_run_events(
        self,
        run_id: str,
        events: list[tuple[str, dict[str, Any]]],
    ) -> list[int]:
        """追加通用轨迹事件（event_type + payload_json）；仅 pending/running 可写，
        终态后不补写轨迹（07 7.4 取消后不补存晚到框架运行快照）。
        """
        if not events:
            raise InvalidInput("至少一条事件")
        now = _now()
        async with self._db.transaction() as conn:
            run = await self._get_run_locked(conn, run_id)
            if run["status"] not in _CANCELLABLE_STATUSES:
                raise RunStateConflict(
                    f"终态 Run 不再接收轨迹事件（当前 {run['status']}）: {run_id}"
                )
            ids: list[int] = []
            for event_type, event_payload in events:
                cursor = await conn.execute(
                    "INSERT INTO run_events (run_id, event_type, payload_json, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (
                        run_id,
                        event_type,
                        json.dumps(event_payload, ensure_ascii=False),
                        now,
                    ),
                )
                ids.append(_require_rowid(cursor))
            return ids

    # ---------- 读取 ----------

    async def _next_seq_locked(self, conn: aiosqlite.Connection, conversation_id: str) -> int:
        async with conn.execute(
            "SELECT MAX(seq) FROM messages WHERE conversation_id = ?",
            (conversation_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return 1 if row is None or row[0] is None else int(row[0]) + 1

    async def list_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        async def op(conn: aiosqlite.Connection) -> list[aiosqlite.Row]:
            async with conn.execute(
                "SELECT id, seq, role, kind, run_id, payload_json FROM messages"
                " WHERE conversation_id = ? ORDER BY seq",
                (conversation_id,),
            ) as cursor:
                return list(await cursor.fetchall())

        return [dict(row) for row in await self._db.under_lock(op)]

    async def list_run_messages(self, run_id: str) -> list[dict[str, Any]]:
        async def op(conn: aiosqlite.Connection) -> list[aiosqlite.Row]:
            async with conn.execute(
                "SELECT id, seq, role, kind, run_id, payload_json FROM messages"
                " WHERE run_id = ? ORDER BY seq",
                (run_id,),
            ) as cursor:
                return list(await cursor.fetchall())

        return [dict(row) for row in await self._db.under_lock(op)]

    async def list_framework_messages(self, run_id: str) -> list[dict[str, Any]]:
        """本 Run 的完整框架消息（payload_json 原样，反序列化归 runtime 层）。"""
        async def op(conn: aiosqlite.Connection) -> list[aiosqlite.Row]:
            async with conn.execute(
                "SELECT id, seq, role, kind, run_id, payload_json FROM messages"
                " WHERE run_id = ? AND kind = 'framework' ORDER BY seq",
                (run_id,),
            ) as cursor:
                return list(await cursor.fetchall())

        return [dict(row) for row in await self._db.under_lock(op)]

    async def list_partial_answers(self, run_id: str) -> list[dict[str, Any]]:
        """已保存部分回答，按落盘顺序；可区分于完整成功 Assistant 消息（07 7.4）。"""
        async def op(conn: aiosqlite.Connection) -> list[aiosqlite.Row]:
            async with conn.execute(
                "SELECT id, seq, role, kind, run_id, payload_json FROM messages"
                " WHERE run_id = ? AND kind = 'partial' ORDER BY seq",
                (run_id,),
            ) as cursor:
                return list(await cursor.fetchall())

        partials = []
        for row in await self._db.under_lock(op):
            text = str(json.loads(row["payload_json"])["text"])
            partials.append({"id": row["id"], "seq": row["seq"], "text": text})
        return partials

    async def list_run_events(self, run_id: str) -> list[dict[str, Any]]:
        async def op(conn: aiosqlite.Connection) -> list[aiosqlite.Row]:
            async with conn.execute(
                "SELECT id, event_type, payload_json, created_at FROM run_events"
                " WHERE run_id = ? ORDER BY id",
                (run_id,),
            ) as cursor:
                return list(await cursor.fetchall())

        return [dict(row) for row in await self._db.under_lock(op)]
