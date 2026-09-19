"""body_metrics 业务表手写 SQL：新增、按身份查询、全量查询、修改、删除。"""

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
            if record is None:
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
