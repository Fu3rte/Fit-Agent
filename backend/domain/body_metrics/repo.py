"""body_metrics 业务表手写 SQL：新增、按身份查询、全量查询、修改、删除（Stage 1 子任务 02 §6）。

全部访问经 ``storage.db.Database`` 的唯一连接与锁（07 7.1）。三条硬边界：

- **写入前校验归 rules**：本层只把已校验的值落库；不猜重量、不补体脂。库内 CHECK 与
  ``REAL NOT NULL`` 是兜底，不是唯一防线。
- **无数据保持 NULL**：体脂为 None 时插入／更新为 NULL，不补 0。
- **多语句走事务**：新增与修改都要「写回后读回」，两条语句必须原子（db.py：多语句原子性必须
  走 ``transaction()``）；单语句的查询与删除走 ``under_lock``。

SQL 一律以字面量书写并参数化（storage/README 硬规则 3）；语句常量复用同一份列清单。
"""

from datetime import date

import aiosqlite

from domain.body_metrics.schema import BodyMetric
from storage.db import Database

_COLUMNS = "id, measured_on, weight_kg, body_fat_pct"


async def _read_by_id(
    conn: aiosqlite.Connection, metric_id: int
) -> BodyMetric | None:
    async with conn.execute(
        "SELECT " + _COLUMNS + " FROM body_metrics WHERE id = ?", (metric_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return None if row is None else BodyMetric.from_row(dict(row))


class BodyMetricsRepo:
    """``body_metrics`` 表的读写（无软删除：删除即物理删除该行）。"""

    def __init__(self, db: Database):
        self._db = db

    async def insert(
        self, measured_on: date, weight_kg: float, body_fat_pct: float | None
    ) -> BodyMetric:
        """新增一条身体指标并读回（同一事务）。"""
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO body_metrics (measured_on, weight_kg, body_fat_pct)"
                " VALUES (?, ?, ?)",
                (measured_on.isoformat(), weight_kg, body_fat_pct),
            )
            try:
                metric_id = int(cursor.lastrowid or 0)
            finally:
                await cursor.close()
            record = await _read_by_id(conn, metric_id)
            if record is None:  # 写成功却读不到即存储状态异常，回滚并显式失败
                raise RuntimeError(f"身体指标写入后读回失败：{metric_id}")
        return record

    async def read(self, metric_id: int) -> BodyMetric | None:
        """按身份读取；不存在即 None。"""
        return await self._db.under_lock(lambda conn: _read_by_id(conn, metric_id))

    async def list_all(self) -> tuple[BodyMetric, ...]:
        """全部身体指标（按发生日期、身份排序）。"""

        async def op(conn: aiosqlite.Connection) -> tuple[BodyMetric, ...]:
            async with conn.execute(
                "SELECT " + _COLUMNS + " FROM body_metrics ORDER BY measured_on, id"
            ) as cursor:
                rows = await cursor.fetchall()
            return tuple(BodyMetric.from_row(dict(row)) for row in rows)

        return await self._db.under_lock(op)

    async def update(
        self,
        metric_id: int,
        measured_on: date,
        weight_kg: float,
        body_fat_pct: float | None,
    ) -> BodyMetric | None:
        """整条覆盖修改（PUT 语义）并读回；身份不存在即 None。"""
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE body_metrics SET measured_on = ?, weight_kg = ?,"
                " body_fat_pct = ? WHERE id = ?",
                (measured_on.isoformat(), weight_kg, body_fat_pct, metric_id),
            )
            return await _read_by_id(conn, metric_id)

    async def delete(self, metric_id: int) -> bool:
        """物理删除一条身体指标；返回是否删除了行（表内无软删除列）。"""

        async def op(conn: aiosqlite.Connection) -> bool:
            cursor = await conn.execute(
                "DELETE FROM body_metrics WHERE id = ?", (metric_id,)
            )
            try:
                return cursor.rowcount == 1
            finally:
                await cursor.close()

        return await self._db.under_lock(op)
