"""S0-05：固定业务时区——首次采样持久化、重开不变、跨午夜与 DST 地区规则（07 7.3）。

全部使用 pytest tmp_path 下的独立文件库（不用内存库，重开测试依赖真实文件），
不触碰真实用户数据目录（stage0.md 第 6 节）。系统时区改变以注入不同
``source`` 采样来源模拟；Windows 实机切换时区的人工步骤归 stage0.md 第 5 节。
"""

from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from api.app import create_app
from storage.db import Database
from storage.setting_repo import SettingRepo, business_date

TZ_SHANGHAI = "Asia/Shanghai"
TZ_NEW_YORK = "America/New_York"
TZ_BERLIN = "Europe/Berlin"


def source_of(name: str):
    """注入式本机时区采样来源：返回固定地区名，模拟不同系统时区设置。"""
    return lambda: name


def failing_source() -> str:
    raise ZoneInfoNotFoundError("模拟本机时区检测失败")


async def _migrated_repo(path: Path) -> SettingRepo:
    db = Database(path)
    await db.open()
    await db.migrate()
    return SettingRepo(db)


async def test_first_init_persists_and_reopen_keeps_timezone(tmp_path: Path) -> None:
    """首次初始化保存后，重开数据库不更换时区；再次初始化不重取样（07 7.3）。"""
    path = tmp_path / "app.db"
    repo = await _migrated_repo(path)
    result = await repo.initialize_business_timezone(source_of(TZ_SHANGHAI))
    assert result == {"timezone": TZ_SHANGHAI, "initialized": True}
    await repo._db.close()

    repo = await _migrated_repo(path)  # 重开同一文件库
    result = await repo.initialize_business_timezone(source_of(TZ_NEW_YORK))
    assert result == {"timezone": TZ_SHANGHAI, "initialized": False}
    assert await repo.get_business_timezone() == TZ_SHANGHAI
    await repo._db.close()


async def test_simulated_system_tz_change_keeps_saved_timezone_and_business_date(
    tmp_path: Path,
) -> None:
    """模拟系统时区改变（换采样来源）后：已保存值不变；同一绝对时刻业务日期不变。"""
    path = tmp_path / "app.db"
    repo = await _migrated_repo(path)
    await repo.initialize_business_timezone(source_of(TZ_SHANGHAI))
    await repo._db.close()

    # "系统时区改为 New York"后重开：仍读已保存值，不重新采样
    repo = await _migrated_repo(path)
    result = await repo.initialize_business_timezone(source_of(TZ_NEW_YORK))
    saved = await repo.get_business_timezone()
    assert saved == TZ_SHANGHAI
    assert result["timezone"] == TZ_SHANGHAI

    # 同一绝对时刻：业务日期按已保存时区解释，不随模拟的系统时区改变
    instant = datetime(2026, 6, 1, 16, 30, tzinfo=UTC)  # 上海 6/2 00:30；纽约 6/1 12:30
    assert business_date(instant, saved) == date(2026, 6, 2)
    assert business_date(instant, TZ_NEW_YORK) == date(2026, 6, 1)
    await repo._db.close()


async def test_cross_midnight_business_date_uses_saved_zone(tmp_path: Path) -> None:
    """跨午夜业务日期：UTC 侧未过午夜的同一时刻，业务时区内已进入次日。"""
    path = tmp_path / "app.db"
    repo = await _migrated_repo(path)
    await repo.initialize_business_timezone(source_of(TZ_SHANGHAI))
    saved = await repo.get_business_timezone()
    assert saved is not None
    await repo._db.close()

    instant = datetime(2026, 6, 1, 16, 30, tzinfo=UTC)
    assert instant.astimezone(ZoneInfo("UTC")).date() == date(2026, 6, 1)
    assert business_date(instant, saved) == date(2026, 6, 2)  # 上海已跨午夜到 6/2


async def test_dst_region_rules_not_fixed_offset(tmp_path: Path) -> None:
    """夏令时地区样例：全年地区规则生效，不能用启动时固定 UTC 偏移替代（07 7.3）。

    不用 pytest.mark.parametrize：anyio 插件为 async 测试重建 callspec 时只含
    anyio_backend 参数，会丢弃既有参数化（request.param 缺失），故在用例内
    逐地区循环断言。样例对固定偏移替代真实有效：NY 固定 -4 或 -5、Berlin
    固定 +1 或 +2 都必然算错其中一个日期。
    """
    cases = [
        # 夏令时地区样例：冬夏 UTC 偏移不同（-4/-5 与 +2/+1）
        (
            TZ_NEW_YORK,
            datetime(2026, 7, 1, 4, 0, tzinfo=UTC),  # EDT -4: 6/30 24:00 → 7/1 00:00
            date(2026, 7, 1),
            datetime(2026, 1, 1, 4, 0, tzinfo=UTC),  # EST -5: 12/31 23:00
            date(2025, 12, 31),
        ),
        (
            TZ_BERLIN,
            datetime(2026, 7, 1, 22, 0, tzinfo=UTC),  # CEST +2: 7/2 00:00
            date(2026, 7, 2),
            datetime(2025, 12, 31, 22, 0, tzinfo=UTC),  # CET +1: 12/31 23:00
            date(2025, 12, 31),
        ),
    ]
    for case in cases:
        (
            timezone_name,
            summer_instant,
            summer_date,
            winter_instant,
            winter_date,
        ) = case
        path = tmp_path / f"{timezone_name.replace('/', '_')}.db"
        repo = await _migrated_repo(path)
        await repo.initialize_business_timezone(source_of(timezone_name))
        saved = await repo.get_business_timezone()
        assert saved is not None
        await repo._db.close()

        zone = ZoneInfo(saved)
        assert saved == timezone_name
        # 同一业务时区在冬夏产生不同 UTC 偏移：证明存的是地区规则而非固定偏移
        # （偏移必须经业务时区 astimezone 取得；UTC 时刻自身的 utcoffset 恒为 0）
        summer_offset = summer_instant.astimezone(zone).utcoffset()
        winter_offset = winter_instant.astimezone(zone).utcoffset()
        assert summer_offset != winter_offset, timezone_name
        # 固定偏移替代（任取冬或夏偏移）无法同时满足两个日期样例
        assert business_date(summer_instant, saved) == summer_date, timezone_name
        assert business_date(winter_instant, saved) == winter_date, timezone_name


async def test_detection_failure_raises_loudly_and_persists_nothing(
    tmp_path: Path,
) -> None:
    """检测失败大声上抛：不静默选 UTC 或其他时区，也不留下半套写入。"""
    path = tmp_path / "app.db"
    repo = await _migrated_repo(path)
    with pytest.raises(ZoneInfoNotFoundError):
        await repo.initialize_business_timezone(failing_source)
    assert await repo.get_business_timezone() is None  # 未持久化任何替代值
    # 失败后库仍可用：锁与事务未被占用，后续读写成功
    recovered = await repo.initialize_business_timezone(source_of(TZ_SHANGHAI))
    assert recovered["initialized"] is True
    await repo._db.close()


async def test_unresolvable_sample_name_fails_without_persisting(
    tmp_path: Path,
) -> None:
    """采样结果无法被 ZoneInfo 解析（如 Windows 缺 tzdata）时大声失败，不写库。"""
    path = tmp_path / "app.db"
    repo = await _migrated_repo(path)
    with pytest.raises(ZoneInfoNotFoundError):
        await repo.initialize_business_timezone(source_of("Not/ARealZone"))
    assert await repo.get_business_timezone() is None
    await repo._db.close()


async def test_app_lifespan_initializes_once_and_survives_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """应用生命周期接线：首次启动采样持久化；重启（模拟系统时区已变）值不变。"""
    monkeypatch.setattr("api.app.local_timezone_name", source_of(TZ_SHANGHAI))
    data_dir = tmp_path / "data"
    first = create_app(data_dir)
    async with first.router.lifespan_context(first):
        assert first.state.business_timezone == TZ_SHANGHAI

    # 重启并模拟系统时区已改为 New York：仍读已保存值
    monkeypatch.setattr("api.app.local_timezone_name", source_of(TZ_NEW_YORK))
    second = create_app(data_dir)
    async with second.router.lifespan_context(second):
        assert second.state.business_timezone == TZ_SHANGHAI
