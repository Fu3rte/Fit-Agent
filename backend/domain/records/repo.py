"""records 业务表手写 SQL：训练及其组的读写与计划日程关联候选（正本讨论总结 §9、001_initial.sql）。

全部访问经 ``storage.db.Database`` 的唯一连接与锁（07 7.1）。SQL 一律以字面量书写并参数化
（storage/README 硬规则 3）；列清单只在模块常量里出现一次。四类出口：

- **写入原语必须复用外层事务**：一次训练与它的全部组行必须一起落库或一起不落库，因此
  ``create_in_transaction``／``replace_in_transaction`` 只接受 ``Database.transaction()`` 的连接
  （``storage.db.require_outer_transaction`` 守住契约），由领域服务在同一个事务里先解析关联日程
  再写，避免「解析与写入分两次取锁」之间的竞态与裸 ``IntegrityError``。
- **只读公开出口**走 ``under_lock``：按身份读取、全量读取、当天未完成且未被占用的日程候选。
- **删除是物理删除**（Stage 1 已拍 1A）：组行由库内 ``ON DELETE CASCADE`` 一并删除，
  ``Database.open()`` 已开 ``PRAGMA foreign_keys=ON``。
- **写入前校验归 rules／service**：本层不猜重量、不补口径；库内 CHECK、UNIQUE 与触发器是兜底，
  不是唯一防线。
"""

from collections.abc import Sequence
from datetime import date

import aiosqlite

from domain.plans.schema import PlanSession
from domain.records.schema import WorkoutSession, WorkoutSet, WorkoutSetInput
from storage.db import Database, require_outer_transaction

#: workout_sessions 列清单：本模块所有读取共用同一份（列顺序即 from_row 读取的键）。
_SELECT_SESSION = "SELECT id, performed_on, plan_session_id FROM workout_sessions"

#: workout_sets 列清单：同上，与 001_initial.sql 的列一一对应（无 RIR／辅助次数／时长列）。
_SELECT_SET = (
    "SELECT id, workout_session_id, exercise_id, set_no, set_type, load_convention,"
    " weight_kg, reps FROM workout_sets"
)

#: plan_sessions 列清单：只取关联校验需要的字段，计划侧读取出口在 domain.plans。
_SELECT_PLAN_SESSION = "SELECT ps.id, ps.plan_id, ps.scheduled_on, ps.cancelled_at FROM plan_sessions ps"


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
            " load_convention, weight_kg, reps) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                fact.exercise_id,
                fact.set_no,
                fact.set_type,
                fact.load_convention,
                fact.weight_kg,
                fact.reps,
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
        """按身份读一条计划日程（含已取消行）；不存在即 None。

        只在外层写事务内使用：关联校验必须与写入读同一份快照。
        """
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
        if record is None:  # 写成功却读不到即存储状态异常，随外层事务回滚并显式失败
            raise RuntimeError(f"训练记录写入后读回失败：{session_id}")
        return record

    # ---------- 只读公开出口（under_lock） ----------

    async def read(self, session_id: int) -> WorkoutSession | None:
        """按身份读取一次训练及其全部组；不存在即 None。"""
        return await self._db.under_lock(lambda conn: _read_session(conn, session_id))

    async def list_all(self) -> tuple[WorkoutSession, ...]:
        """全部训练及其全部组（按发生日期、身份排序）。"""
        return await self._db.under_lock(_read_all_sessions)

    async def list_unfinished_plan_sessions(
        self, scheduled_on: date
    ) -> tuple[PlanSession, ...]:
        """当天未完成日程候选：未取消且未被任何训练关联（供表单选择，不猜）。"""
        return await self._db.under_lock(
            lambda conn: _read_link_candidates(conn, scheduled_on)
        )

    async def delete(self, session_id: int) -> bool:
        """物理删除一次训练（组行由 ON DELETE CASCADE 一并删除），返回是否删除了训练行。

        级联删除的组行不计入 ``rowcount``（SQLite 只统计本语句直接删除的行），故这里判断的是
        训练行本身是否存在。
        """

        async def op(conn: aiosqlite.Connection) -> bool:
            cursor = await conn.execute(
                "DELETE FROM workout_sessions WHERE id = ?", (session_id,)
            )
            try:
                return cursor.rowcount >= 1
            finally:
                await cursor.close()

        return await self._db.under_lock(op)
