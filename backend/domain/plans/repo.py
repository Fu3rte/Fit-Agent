"""plans 业务表手写 SQL：计划版本与计划日程的只读查询（Stage 1 子任务 02 §8）。

全部访问经 ``storage.db.Database`` 的唯一连接与锁（07 7.1）。本层**只读**：

- 当前 active 计划、draft 计划、历史版本（按 ``version`` 升序的全量）与按身份读取；
- 计划日程（含已取消行）。
- 不提供创建、确认、拒绝、激活或归档入口；计划激活事务留到 Stage 5。本阶段测试与用例只用
  直接 SQL 写入计划数据。

SQL 一律以字面量书写并参数化（storage/README 硬规则 3）；语句常量只由模块内字面量拼接。
"""

import aiosqlite

from domain.plans.schema import Plan, PlanSession
from storage.db import Database

_SELECT_PLAN = (
    "SELECT id, version, status, source_plan_id, structured_content, evaluator_result,"
    " created_at, confirmed_at, archived_at FROM plans"
)
_SELECT_SESSION = "SELECT id, plan_id, scheduled_on, cancelled_at FROM plan_sessions"


async def _read_plans(
    conn: aiosqlite.Connection, tail: str, params: tuple[object, ...]
) -> tuple[Plan, ...]:
    async with conn.execute(_SELECT_PLAN + tail, params) as cursor:
        rows = await cursor.fetchall()
    return tuple(Plan.from_row(dict(row)) for row in rows)


class PlanRepo:
    """``plans``／``plan_sessions`` 的只读查询。"""

    def __init__(self, db: Database):
        self._db = db

    async def read_active(self) -> Plan | None:
        """当前 active 计划；没有正式启用的计划时返回 None（不拿 draft 当替代）。"""

        async def op(conn: aiosqlite.Connection) -> Plan | None:
            plans = await _read_plans(conn, " WHERE status = ?", ("active",))
            return plans[0] if plans else None

        return await self._db.under_lock(op)

    async def read_by_id(self, plan_id: int) -> Plan | None:
        """按身份读取一个计划版本（含历史版本）；不存在即 None。"""

        async def op(conn: aiosqlite.Connection) -> Plan | None:
            plans = await _read_plans(conn, " WHERE id = ?", (plan_id,))
            return plans[0] if plans else None

        return await self._db.under_lock(op)

    async def list_drafts(self) -> tuple[Plan, ...]:
        """全部 draft 计划（按版本号升序）；确认前的计划只有 draft。"""
        return await self._db.under_lock(
            lambda conn: _read_plans(conn, " WHERE status = ? ORDER BY version", ("draft",))
        )

    async def list_versions(self) -> tuple[Plan, ...]:
        """全部计划版本（按 ``version`` 升序）：已归档的历史版本从此读取。"""
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
