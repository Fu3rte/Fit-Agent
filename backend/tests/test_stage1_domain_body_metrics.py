"""Stage 1 子任务 02 §6：身体指标 domain/body_metrics —— 完整 CRUD、非法输入与空值语义。

对照 02 清单：新建 schema/repo/rules/service、实现体重／体脂的新增查询修改删除、校验业务日期
与必填字段与数值范围、无数据保持空值不补零、添加完整 CRUD 和非法输入测试。

测试只使用 pytest tmp_path 下的独立临时库，不 import tests.support。
"""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from domain.body_metrics.rules import (
    BODY_FAT_PCT_MAX,
    BODY_FAT_PCT_MIN,
    WEIGHT_KG_MAX,
    WEIGHT_KG_MIN,
    InvalidBodyMetric,
)
from domain.body_metrics.service import BodyMetricNotFound, BodyMetricsService
from storage.db import Database


async def _migrated(path: Path) -> Database:
    db = Database(path)
    await db.open()
    await db.migrate()
    return db


async def _raw_row(db: Database, metric_id: int) -> dict[str, object] | None:
    async def op(conn):
        async with conn.execute(
            "SELECT measured_on, weight_kg, body_fat_pct FROM body_metrics WHERE id = ?",
            (metric_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else dict(row)

    return await db.under_lock(op)


async def test_create_query_update_delete_round_trip(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        service = BodyMetricsService(db)
        created = await service.create(date(2026, 6, 1), 70.5, 18.2)
        assert created.measured_on == date(2026, 6, 1)
        assert created.weight_kg == 70.5
        assert created.body_fat_pct == 18.2
        assert await service.get(created.id) == created

        updated = await service.update(created.id, date(2026, 6, 2), 69.8, 17.9)
        assert updated.measured_on == date(2026, 6, 2)
        assert updated.weight_kg == 69.8
        assert updated.body_fat_pct == 17.9
        assert await service.get(created.id) == updated

        await service.delete(created.id)
        assert await service.get(created.id) is None
        assert await _raw_row(db, created.id) is None
        assert await service.list_all() == ()
    finally:
        await db.close()


async def test_query_lists_all_measurements_ordered_by_date(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        service = BodyMetricsService(db)
        later = await service.create(date(2026, 6, 10), 70.0)
        earlier = await service.create(date(2026, 6, 1), 71.0)
        listed = await service.list_all()
        assert [item.id for item in listed] == [earlier.id, later.id]
    finally:
        await db.close()


async def test_absent_body_fat_stays_null_and_is_not_zeroed(tmp_path: Path) -> None:
    """体脂无数据保持 NULL：新增、改回未记录都不补 0。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        service = BodyMetricsService(db)
        created = await service.create(date(2026, 6, 1), 70.0)
        assert created.body_fat_pct is None
        raw = await _raw_row(db, created.id)
        assert raw is not None
        assert raw["body_fat_pct"] is None

        recorded = await service.update(created.id, date(2026, 6, 1), 70.0, 16.0)
        assert recorded.body_fat_pct == 16.0
        cleared = await service.update(created.id, date(2026, 6, 1), 70.0, None)
        assert cleared.body_fat_pct is None
        raw = await _raw_row(db, created.id)
        assert raw is not None
        assert raw["body_fat_pct"] is None
    finally:
        await db.close()


async def test_weight_range_boundaries_accepted(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        service = BodyMetricsService(db)
        for value in (WEIGHT_KG_MIN, WEIGHT_KG_MAX):
            created = await service.create(date(2026, 6, 1), value)
            assert created.weight_kg == value
    finally:
        await db.close()


async def test_weight_out_of_range_rejected(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        service = BodyMetricsService(db)
        for value in (WEIGHT_KG_MIN - 0.1, WEIGHT_KG_MAX + 0.1):
            with pytest.raises(InvalidBodyMetric):
                await service.create(date(2026, 6, 1), value)
        assert await service.list_all() == ()
    finally:
        await db.close()


async def test_body_fat_range_boundaries_accepted(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        service = BodyMetricsService(db)
        for value in (BODY_FAT_PCT_MIN, BODY_FAT_PCT_MAX):
            created = await service.create(date(2026, 6, 1), 70.0, value)
            assert created.body_fat_pct == value
    finally:
        await db.close()


async def test_body_fat_out_of_range_rejected(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        service = BodyMetricsService(db)
        for value in (BODY_FAT_PCT_MIN - 0.1, BODY_FAT_PCT_MAX + 0.1):
            with pytest.raises(InvalidBodyMetric):
                await service.create(date(2026, 6, 1), 70.0, value)
    finally:
        await db.close()


async def test_invalid_inputs_rejected(tmp_path: Path) -> None:
    """日期、必填字段与数值范围逐项拒绝：日期必须为日期对象、体重必填、体脂有限且在范围内。"""
    invalid: list[tuple[object, object, object]] = [
        # 日期必须是日期对象：文本与绝对时刻都不是业务自然日。
        ("2026-06-01", 70.0, None),
        (datetime(2026, 6, 1, tzinfo=timezone(timedelta(hours=8))), 70.0, None),
        # 体重必填且必须是数值。
        (date(2026, 6, 1), None, None),
        (date(2026, 6, 1), "70", None),
        (date(2026, 6, 1), True, None),
        (date(2026, 6, 1), float("nan"), None),
        (date(2026, 6, 1), float("inf"), None),
        # 体脂给了值就必须是有限数值且在范围内。
        (date(2026, 6, 1), 70.0, "18"),
        (date(2026, 6, 1), 70.0, float("nan")),
    ]
    db = await _migrated(tmp_path / "x.db")
    try:
        service = BodyMetricsService(db)
        for measured_on, weight_kg, body_fat_pct in invalid:
            with pytest.raises(InvalidBodyMetric):
                await service.create(  # type: ignore[arg-type]
                    measured_on, weight_kg, body_fat_pct
                )
        assert await service.list_all() == ()  # 拒绝即不落盘
    finally:
        await db.close()


async def test_update_and_delete_reject_unknown_identity(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        service = BodyMetricsService(db)
        with pytest.raises(BodyMetricNotFound):
            await service.update(999, date(2026, 6, 1), 70.0)
        with pytest.raises(BodyMetricNotFound):
            await service.delete(999)
    finally:
        await db.close()
