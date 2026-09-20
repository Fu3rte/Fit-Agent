"""body_metrics 用例编排：体重／体脂的新增、查询、修改、删除。"""

from datetime import date

from domain.body_metrics.repo import BodyMetricsRepo
from domain.body_metrics.rules import (
    validate_body_fat_pct,
    validate_measured_on,
    validate_weight_kg,
)
from domain.body_metrics.schema import BodyMetric
from domain.tool_cache.repo import ToolCacheRevisionsRepo
from storage.db import Database


class BodyMetricNotFound(ValueError):
    """按身份修改或删除时该条身体指标不存在。"""


class BodyMetricsService:
    """身体指标 CRUD（写入前统一校验）。"""

    def __init__(self, db: Database):
        self._db = db
        self._repo = BodyMetricsRepo(db)
        self._revisions = ToolCacheRevisionsRepo(db)

    async def create(
        self,
        measured_on: date,
        weight_kg: float,
        body_fat_pct: float | None = None,
    ) -> BodyMetric:
        """新增一条身体指标；体脂未记录时传 None（落库为 NULL，不补 0）。"""
        measured_on = validate_measured_on(measured_on)
        weight_kg = validate_weight_kg(weight_kg)
        body_fat_pct = validate_body_fat_pct(body_fat_pct)
        async with self._db.transaction() as conn:
            record = await self._repo.create_in_transaction(
                conn, measured_on, weight_kg, body_fat_pct
            )
            await self._revisions.bump_in_transaction(conn, "metrics")
            return record

    async def get(self, metric_id: int) -> BodyMetric | None:
        """按身份查询；不存在即 None。"""
        return await self._repo.read(metric_id)

    async def list_all(self) -> tuple[BodyMetric, ...]:
        """查询全部身体指标（按发生日期排序）。"""
        return await self._repo.list_all()

    async def update(
        self,
        metric_id: int,
        measured_on: date,
        weight_kg: float,
        body_fat_pct: float | None = None,
    ) -> BodyMetric:
        """整条覆盖修改；身份不存在抛 :class:`BodyMetricNotFound`。"""
        measured_on = validate_measured_on(measured_on)
        weight_kg = validate_weight_kg(weight_kg)
        body_fat_pct = validate_body_fat_pct(body_fat_pct)
        async with self._db.transaction() as conn:
            record = await self._repo.update_in_transaction(
                conn, metric_id, measured_on, weight_kg, body_fat_pct
            )
            if record is None:
                raise BodyMetricNotFound(f"身体指标不存在：{metric_id}")
            await self._revisions.bump_in_transaction(conn, "metrics")
            return record

    async def delete(self, metric_id: int) -> None:
        """删除一条身体指标；身份不存在抛 :class:`BodyMetricNotFound`。"""
        async with self._db.transaction() as conn:
            if not await self._repo.delete_in_transaction(conn, metric_id):
                raise BodyMetricNotFound(f"身体指标不存在：{metric_id}")
            await self._revisions.bump_in_transaction(conn, "metrics")
