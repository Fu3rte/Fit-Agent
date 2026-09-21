"""records 业务表手写 SQL：训练及其组的读写与计划日程关联候选。"""

from collections.abc import Sequence
from datetime import date

import aiosqlite

from app.domain.plans.schema import PlanSession
from app.domain.records.schema import WorkoutSession, WorkoutSet, WorkoutSetInput
from app.infrastructure.database.connection import Database, require_outer_transaction

_SELECT_SESSION = "SELECT id, performed_on, plan_session_id FROM workout_sessions"

_SELECT_SET = (
    "SELECT id, workout_session_id, exercise_id, set_no, set_type, load_convention,"
    " weight_kg, reps, duration_seconds FROM workout_sets"
)

_SELECT_PLAN_SESSION = "SELECT ps.id, ps.plan_id, ps.scheduled_on, ps.cancelled_at FROM plan_sessions ps"

_RECENT_SESSION_ORDER = "performed_on DESC, id DESC"


def _plan_session(row: aiosqlite.Row) -> PlanSession:
    return PlanSession.from_row(dict(row))


async def _read_session(
    conn: aiosqlite.Connection, session_id: int
) -> WorkoutSession | None:
    """读一次训练及其全部组；训练行不存在即 None。"""
    async with conn.execute(
        _SELECT_SESSION + " WHERE id = ?", (session_id,)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    async with conn.execute(
        _SELECT_SET + " WHERE workout_session_id = ? ORDER BY exercise_id, set_no",
        (session_id,),
    ) as cursor:
        set_rows = await cursor.fetchall()
    return WorkoutSession.from_row(
        dict(row), tuple(WorkoutSet.from_row(dict(set_row)) for set_row in set_rows)
    )


async def _read_all_sessions(
    conn: aiosqlite.Connection,
) -> tuple[WorkoutSession, ...]:
    async with conn.execute(
        _SELECT_SESSION + " ORDER BY performed_on, id"
    ) as cursor:
        session_rows = await cursor.fetchall()
    async with conn.execute(
        _SELECT_SET + " ORDER BY workout_session_id, exercise_id, set_no"
    ) as cursor:
        set_rows = await cursor.fetchall()
    grouped: dict[int, list[WorkoutSet]] = {}
    for set_row in set_rows:
        fact = WorkoutSet.from_row(dict(set_row))
        grouped.setdefault(fact.workout_session_id, []).append(fact)
    return tuple(
        WorkoutSession.from_row(
            dict(row), grouped.get(int(row["id"]), [])
        )
        for row in session_rows
    )


async def _read_recent_sessions(
    conn: aiosqlite.Connection, limit: int
) -> tuple[WorkoutSession, ...]:
    """最近 ``limit`` 次训练及其全部组（最新在前）。"""
    recent = (
        "WITH recent AS (SELECT id, performed_on, plan_session_id FROM workout_sessions"
        " ORDER BY "
        + _RECENT_SESSION_ORDER
        + " LIMIT ?)"
    )
    statement = (
        recent
        + " SELECT s.id AS session_id, s.performed_on, s.plan_session_id,"
        " ws.id, ws.workout_session_id, ws.exercise_id, ws.set_no, ws.set_type,"
        " ws.load_convention, ws.weight_kg, ws.reps, ws.duration_seconds"
        " FROM recent AS s LEFT JOIN workout_sets AS ws"
        " ON ws.workout_session_id = s.id"
        " ORDER BY s.performed_on DESC, s.id DESC, ws.exercise_id, ws.set_no"
    )
    async with conn.execute(statement, (limit,)) as cursor:
        rows = await cursor.fetchall()
    grouped: dict[int, list[WorkoutSet]] = {}
    session_rows: dict[int, dict[str, object]] = {}
    order: list[int] = []
    for row in rows:
        session_id = int(row["session_id"])
        if session_id not in session_rows:
            session_rows[session_id] = {
                "id": session_id,
                "performed_on": row["performed_on"],
                "plan_session_id": row["plan_session_id"],
            }
            grouped[session_id] = []
            order.append(session_id)
        if row["id"] is not None:
            grouped[session_id].append(WorkoutSet.from_row(dict(row)))
    return tuple(
        WorkoutSession.from_row(session_rows[session_id], grouped[session_id])
        for session_id in order
    )


async def _read_link_candidates(
    conn: aiosqlite.Connection, scheduled_on: date
) -> tuple[PlanSession, ...]:
    """当天未取消、且未被任何训练占用的计划日程（讨论总结 §9 关联规则）。"""
    async with conn.execute(
        _SELECT_PLAN_SESSION
        + " WHERE ps.scheduled_on = ?"
        " AND ps.cancelled_at IS NULL"
        " AND NOT EXISTS (SELECT 1 FROM workout_sessions ws WHERE ws.plan_session_id = ps.id)"
        " ORDER BY ps.id",
        (scheduled_on.isoformat(),),
    ) as cursor:
        rows = await cursor.fetchall()
    return tuple(_plan_session(row) for row in rows)


async def _insert_sets(
    conn: aiosqlite.Connection, session_id: int, facts: Sequence[WorkoutSetInput]
) -> None:
    """写入一次训练的全部组行（复用外层事务；失败即整条训练一起回滚）。"""
    for fact in facts:
        await conn.execute(
            "INSERT INTO workout_sets (workout_session_id, exercise_id, set_no, set_type,"
            " load_convention, weight_kg, reps, duration_seconds)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                fact.exercise_id,
                fact.set_no,
                fact.set_type,
                fact.load_convention,
                fact.weight_kg,
                fact.reps,
                fact.duration_seconds,
            ),
        )


class WorkoutRecordsRepo:
    """``workout_sessions``／``workout_sets`` 的读写与计划日程关联候选查询。"""

    def __init__(self, db: Database):
        self._db = db

    # ---------- 事务内写原语（调用方持有 Database.transaction()） ----------

    async def create_in_transaction(
        self,
        conn: aiosqlite.Connection,
        performed_on: date,
        plan_session_id: int | None,
        facts: Sequence[WorkoutSetInput],
    ) -> WorkoutSession:
        """写入一次训练及其全部组并读回；必须复用外层事务（原子新增）。"""
        require_outer_transaction(conn, "训练记录新增")
        cursor = await conn.execute(
            "INSERT INTO workout_sessions (performed_on, plan_session_id) VALUES (?, ?)",
            (performed_on.isoformat(), plan_session_id),
        )
        try:
            session_id = int(cursor.lastrowid or 0)
        finally:
            await cursor.close()
        await _insert_sets(conn, session_id, facts)
        return await self._read_written_in_transaction(conn, session_id)

    async def replace_in_transaction(
        self,
        conn: aiosqlite.Connection,
        session_id: int,
        performed_on: date,
        plan_session_id: int | None,
        facts: Sequence[WorkoutSetInput],
    ) -> WorkoutSession | None:
        """整条覆盖一次训练（UPDATE 训练行 + 重建组行）并读回；身份不存在即 None。"""
        require_outer_transaction(conn, "训练记录整条覆盖")
        if await _read_session(conn, session_id) is None:
            return None
        await conn.execute(
            "UPDATE workout_sessions SET performed_on = ?, plan_session_id = ? WHERE id = ?",
            (performed_on.isoformat(), plan_session_id, session_id),
        )
        await conn.execute(
            "DELETE FROM workout_sets WHERE workout_session_id = ?", (session_id,)
        )
        await _insert_sets(conn, session_id, facts)
        return await self._read_written_in_transaction(conn, session_id)

    async def read_plan_session_in_transaction(
        self, conn: aiosqlite.Connection, plan_session_id: int
    ) -> PlanSession | None:
        """按身份读一条计划日程（含已取消行）；不存在即 None。"""
        require_outer_transaction(conn, "训练记录关联日程读取")
        async with conn.execute(
            _SELECT_PLAN_SESSION + " WHERE ps.id = ?", (plan_session_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else _plan_session(row)

    async def link_candidates_in_transaction(
        self, conn: aiosqlite.Connection, scheduled_on: date
    ) -> tuple[PlanSession, ...]:
        """当天可关联的日程候选（事务内版本：与写入同一份快照）。"""
        require_outer_transaction(conn, "训练记录关联候选读取")
        return await _read_link_candidates(conn, scheduled_on)

    async def read_linked_session_id_in_transaction(
        self, conn: aiosqlite.Connection, plan_session_id: int
    ) -> int | None:
        """占用该计划日程的训练身份；未被占用即 None（库内 UNIQUE 保证至多一条）。"""
        require_outer_transaction(conn, "训练记录关联占用读取")
        async with conn.execute(
            "SELECT id FROM workout_sessions WHERE plan_session_id = ?",
            (plan_session_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else int(row["id"])

    async def _read_written_in_transaction(
        self, conn: aiosqlite.Connection, session_id: int
    ) -> WorkoutSession:
        record = await _read_session(conn, session_id)
        if record is None:
            raise RuntimeError(f"训练记录写入后读回失败：{session_id}")
        return record

    async def delete_in_transaction(
        self, conn: aiosqlite.Connection, session_id: int
    ) -> bool:
        """物理删除一次训练（组行由 ON DELETE CASCADE 一并删除）；返回是否删除了行。"""
        require_outer_transaction(conn, "训练记录删除")
        cursor = await conn.execute(
            "DELETE FROM workout_sessions WHERE id = ?", (session_id,)
        )
        try:
            return cursor.rowcount >= 1
        finally:
            await cursor.close()

    # ---------- 只读公开出口（under_lock） ----------

    async def read(self, session_id: int) -> WorkoutSession | None:
        """按身份读取一次训练及其全部组；不存在即 None。"""
        return await self._db.under_lock(lambda conn: _read_session(conn, session_id))

    async def list_all(self) -> tuple[WorkoutSession, ...]:
        """全部训练及其全部组（按发生日期、身份排序）。"""
        return await self._db.under_lock(_read_all_sessions)

    async def list_recent(self, limit: int) -> tuple[WorkoutSession, ...]:
        """最近 ``limit`` 次训练及其全部组（最新在前）。"""
        if limit < 1:
            raise ValueError(f"list_recent 的 limit 必须为正数：{limit!r}")
        return await self._db.under_lock(
            lambda conn: _read_recent_sessions(conn, limit)
        )

    async def list_unfinished_plan_sessions(
        self, scheduled_on: date
    ) -> tuple[PlanSession, ...]:
        """当天未完成日程候选：未取消且未被任何训练关联（供表单选择，不猜）。"""
        return await self._db.under_lock(
            lambda conn: _read_link_candidates(conn, scheduled_on)
        )
