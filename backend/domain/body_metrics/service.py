"""body_metrics 用例编排：体重／体脂的新增、查询、修改、删除（Stage 1 子任务 02 §6；REFACTOR_PLAN §6.1）。

边界：不接 HTTP／Agent／CLI，表单直接调用本服务，不创建草稿。每次写入前做日期、必填与数值
范围校验（``domain.body_metrics.rules``）；「今天」不由本层决定——业务日期由调用方按业务时区
注入（REFACTOR_PLAN §5.5）。
"""

from datetime import date

from domain.body_metrics.repo import BodyMetricsRepo
from domain.body_metrics.rules import (
    validate_body_fat_pct,
    validate_measured_on,
    validate_weight_kg,
)
from domain.body_metrics.schema import BodyMetric
from storage.db import Database


class BodyMetricNotFound(ValueError):
    """按身份修改或删除时该条身体指标不存在。"""


class BodyMetricsService:
    """身体指标 CRUD（写入前统一校验）。"""

    def __init__(self, db: Database):
        self._repo = BodyMetricsRepo(db)

    async def create(
        self,
        measured_on: date,
        weight_kg: float,
        body_fat_pct: float | None = None,
    ) -> BodyMetric:
        """新增一条身体指标；体脂未记录时传 None（落库为 NULL，不补 0）。"""
        return await self._repo.insert(
            validate_measured_on(measured_on),
            validate_weight_kg(weight_kg),
            validate_body_fat_pct(body_fat_pct),
        )

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
        """整条覆盖修改；身份不存在抛 :class:`BodyMetricNotFound`。

        体脂传 None 表示把该次的体脂改为未记录（写回 NULL），不是补 0。
        """
        record = await self._repo.update(
            metric_id,
            validate_measured_on(measured_on),
            validate_weight_kg(weight_kg),
            validate_body_fat_pct(body_fat_pct),
        )
        if record is None:
            raise BodyMetricNotFound(f"身体指标不存在：{metric_id}")
        return record

    async def delete(self, metric_id: int) -> None:
        """删除一条身体指标；身份不存在抛 :class:`BodyMetricNotFound`。"""
        if not await self._repo.delete(metric_id):
            raise BodyMetricNotFound(f"身体指标不存在：{metric_id}")
