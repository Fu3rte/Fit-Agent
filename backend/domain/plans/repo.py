"""plans 业务表手写 SQL：计划版本与计划日程的读查询、最小写原语与激活／归档事务原语。"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

import aiosqlite

from domain.plans.schema import Plan, PlanSession, PlanStatus
from storage.db import Database, require_outer_transaction

_SELECT_PLAN = (
    "SELECT id, version, status, source_plan_id, structured_content, evaluator_result,"
    " created_at, confirmed_at, archived_at FROM plans"
)
_SELECT_SESSION = "SELECT id, plan_id, scheduled_on, cancelled_at FROM plan_sessions"
_SELECT_NEXT_VERSION = "SELECT COALESCE(MAX(version), 0) + 1 FROM plans"
_INSERT_NEW_VERSION = (
    "INSERT INTO plans (version, status, source_plan_id, structured_content,"
    " evaluator_result, created_at, confirmed_at, archived_at)"
    " VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)"
)
_UPDATE_DRAFT = (
    "UPDATE plans SET structured_content = ?, evaluator_result = ?"
    " WHERE id = ? AND status = 'draft'"
)
_ARCHIVE_ACTIVE = (
    "UPDATE plans SET status = 'archived', archived_at = ?"
    " WHERE id = ? AND status = 'active'"
)
_ACTIVATE_DRAFT = (
    "UPDATE plans SET status = 'active', confirmed_at = ?"
    " WHERE id = ? AND status = 'draft'"
)
_ARCHIVE_DRAFT = (
    "UPDATE plans SET status = 'archived', archived_at = ?"
    " WHERE id = ? AND status = 'draft'"
)
_CANCEL_SESSIONS = (
    "UPDATE plan_sessions SET cancelled_at = ?"
    " WHERE plan_id = ? AND scheduled_on >= ? AND cancelled_at IS NULL"
)
_INSERT_SESSION = (
    "INSERT INTO plan_sessions (plan_id, scheduled_on, cancelled_at) VALUES (?, ?, NULL)"
)


@dataclass(frozen=True, slots=True)
class ActivePlanSnapshot:
    """写路径前后的原 active 快照（stage4.md §9.2）：身份／状态／版本／内容／确认时间 + active 行数。"""

    count: int
    id: int | None
    status: PlanStatus | None
    version: int | None
    structured_content: Any
    confirmed_at: str | None


async def _read_plans(
    conn: aiosqlite.Connection, tail: str, params: tuple[object, ...]
) -> tuple[Plan, ...]:
    async with conn.execute(_SELECT_PLAN + tail, params) as cursor:
        rows = await cursor.fetchall()
    return tuple(Plan.from_row(dict(row)) for row in rows)


async def _read_plan_by_id_in_transaction(
    conn: aiosqlite.Connection, plan_id: int
) -> Plan:
    """事务内按身份读回刚写入的行；写成功却读不到即存储状态异常（随外层事务回滚）。"""
    plans = await _read_plans(conn, " WHERE id = ?", (plan_id,))
    if not plans:
        raise RuntimeError(f"计划写入后读回失败：{plan_id}")
    return plans[0]


class PlanRepo:
    """``plans``／``plan_sessions`` 的读查询、最小写原语与 Stage 5 状态／日程原语。"""

    def __init__(self, db: Database):
        self._db = db

    async def read_active(self) -> Plan | None:
        """当前 active 计划；没有正式启用的计划时返回 None。"""

        async def op(conn: aiosqlite.Connection) -> Plan | None:
            plans = await _read_plans(conn, " WHERE status = ?", ("active",))
            return plans[0] if plans else None

        return await self._db.under_lock(op)

    async def read_by_id(self, plan_id: int) -> Plan | None:
        """按身份读取一个计划版本（含历史版本与 rejected）；不存在即 None。"""

        async def op(conn: aiosqlite.Connection) -> Plan | None:
            plans = await _read_plans(conn, " WHERE id = ?", (plan_id,))
            return plans[0] if plans else None

        return await self._db.under_lock(op)

    async def read_draft(self) -> Plan | None:
        """当前唯一可确认 draft；没有即 None。"""

        async def op(conn: aiosqlite.Connection) -> Plan | None:
            plans = await _read_plans(conn, " WHERE status = ?", ("draft",))
            return plans[0] if plans else None

        return await self._db.under_lock(op)

    async def list_versions(self) -> tuple[Plan, ...]:
        """全部计划版本（按 ``version`` 升序）。"""
        return await self._db.under_lock(
            lambda conn: _read_plans(conn, " ORDER BY version", ())
        )

    async def list_sessions(self, plan_id: int) -> tuple[PlanSession, ...]:
        """某个计划的全部日程（含已取消行，按应训练日排序）。"""

        async def op(conn: aiosqlite.Connection) -> tuple[PlanSession, ...]:
            async with conn.execute(
                _SELECT_SESSION + " WHERE plan_id = ? ORDER BY scheduled_on, id",
                (plan_id,),
            ) as cursor:
                rows = await cursor.fetchall()
            return tuple(PlanSession.from_row(dict(row)) for row in rows)

        return await self._db.under_lock(op)

    # ---------- 事务内写原语（调用方持有 Database.transaction()） ----------

    async def write_draft_in_transaction(
        self,
        conn: aiosqlite.Connection,
        *,
        structured_content_json: str,
        evaluator_result_json: str,
        created_at: str,
        source_plan_id: int | None = None,
    ) -> Plan:
        """生成／调整通过：同一事务内取 ``max(version)+1`` 并插入一条 draft。"""
        return await self._append_new_version_in_transaction(
            conn,
            status="draft",
            structured_content_json=structured_content_json,
            evaluator_result_json=evaluator_result_json,
            created_at=created_at,
            source_plan_id=source_plan_id,
        )

    async def write_rejected_in_transaction(
        self,
        conn: aiosqlite.Connection,
        *,
        structured_content_json: str,
        evaluator_result_json: str,
        created_at: str,
    ) -> Plan:
        """二次阻断失败且无原 draft：插入一条 rejected 终态（``confirmed_at``／``archived_at`` 为 NULL）。"""
        return await self._append_new_version_in_transaction(
            conn,
            status="rejected",
            structured_content_json=structured_content_json,
            evaluator_result_json=evaluator_result_json,
            created_at=created_at,
        )

    async def replace_draft_in_transaction(
        self,
        conn: aiosqlite.Connection,
        plan_id: int,
        *,
        structured_content_json: str,
        evaluator_result_json: str,
    ) -> Plan | None:
        """显式重新生成通过：一条条件 ``UPDATE`` 原子替换同一 draft 的内容与评估结果；未命中即 None。"""
        require_outer_transaction(conn, "draft 安全替换")
        cursor = await conn.execute(
            _UPDATE_DRAFT,
            (structured_content_json, evaluator_result_json, plan_id),
        )
        try:
            changed = cursor.rowcount
        finally:
            await cursor.close()
        if changed != 1:
            return None
        return await _read_plan_by_id_in_transaction(conn, plan_id)

    async def read_active_snapshot_in_transaction(
        self, conn: aiosqlite.Connection
    ) -> ActivePlanSnapshot:
        """事务内读原 active 快照：写路径前后比对，保证 Stage 4 不修改原 active（stage4.md §9.2）。"""
        require_outer_transaction(conn, "原 active 快照读取")
        plans = await _read_plans(conn, " WHERE status = ?", ("active",))
        first = plans[0] if plans else None
        return ActivePlanSnapshot(
            count=len(plans),
            id=None if first is None else first.id,
            status=None if first is None else first.status,
            version=None if first is None else first.version,
            structured_content=None if first is None else first.structured_content,
            confirmed_at=None if first is None else first.confirmed_at,
        )

    async def archive_active_in_transaction(
        self, conn: aiosqlite.Connection, plan_id: int, *, archived_at: str
    ) -> Plan | None:
        """``active -> archived``＋``archived_at``；目标不再是 active 即 None（条件更新未命中）。"""
        return await self._transition_in_transaction(
            conn, _ARCHIVE_ACTIVE, archived_at, plan_id
        )

    async def activate_draft_in_transaction(
        self, conn: aiosqlite.Connection, plan_id: int, *, confirmed_at: str
    ) -> Plan | None:
        """``draft -> active``＋``confirmed_at``；目标不再是 draft 即 None（条件更新未命中）。"""
        return await self._transition_in_transaction(
            conn, _ACTIVATE_DRAFT, confirmed_at, plan_id
        )

    async def archive_draft_in_transaction(
        self, conn: aiosqlite.Connection, plan_id: int, *, archived_at: str
    ) -> Plan | None:
        """``draft -> archived``＋``archived_at``（用户拒绝）；目标不再是 draft 即 None。"""
        return await self._transition_in_transaction(
            conn, _ARCHIVE_DRAFT, archived_at, plan_id
        )

    async def cancel_sessions_in_transaction(
        self,
        conn: aiosqlite.Connection,
        plan_id: int,
        *,
        business_day: date,
        cancelled_at: str,
    ) -> int:
        """取消某个计划 ``scheduled_on >= business_day`` 且尚未取消的日程，返回被取消行数。"""
        require_outer_transaction(conn, "计划日程取消")
        cursor = await conn.execute(
            _CANCEL_SESSIONS, (cancelled_at, plan_id, business_day.isoformat())
        )
        try:
            return cursor.rowcount
        finally:
            await cursor.close()

    async def create_sessions_in_transaction(
        self,
        conn: aiosqlite.Connection,
        plan_id: int,
        *,
        scheduled_on: Iterable[date],
    ) -> int:
        """为某个计划按训练日各插一条日程（未取消），返回写入行数。"""
        require_outer_transaction(conn, "计划日程写入")
        written = 0
        for day in scheduled_on:
            await conn.execute(_INSERT_SESSION, (plan_id, day.isoformat()))
            written += 1
        return written

    async def _transition_in_transaction(
        self,
        conn: aiosqlite.Connection,
        statement: str,
        timestamp: str,
        plan_id: int,
    ) -> Plan | None:
        """一条带来源状态条件的状态迁移；未命中（0 行）即返回 None，不改任何行。"""
        require_outer_transaction(conn, "计划状态迁移")
        cursor = await conn.execute(statement, (timestamp, plan_id))
        try:
            changed = cursor.rowcount
        finally:
            await cursor.close()
        if changed != 1:
            return None
        return await _read_plan_by_id_in_transaction(conn, plan_id)

    async def _append_new_version_in_transaction(
        self,
        conn: aiosqlite.Connection,
        *,
        status: PlanStatus,
        structured_content_json: str,
        evaluator_result_json: str,
        created_at: str,
        source_plan_id: int | None = None,
    ) -> Plan:
        """事务内分配 ``max(version)+1`` 并插入一条新版本行，再读回。"""
        require_outer_transaction(conn, "计划版本写入")
        async with conn.execute(_SELECT_NEXT_VERSION) as cursor:
            row = await cursor.fetchone()
        version = int(row[0])
        cursor = await conn.execute(
            _INSERT_NEW_VERSION,
            (
                version,
                status,
                source_plan_id,
                structured_content_json,
                evaluator_result_json,
                created_at,
            ),
        )
        try:
            plan_id = int(cursor.lastrowid or 0)
        finally:
            await cursor.close()
        return await _read_plan_by_id_in_transaction(conn, plan_id)
