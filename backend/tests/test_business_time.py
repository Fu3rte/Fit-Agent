"""Stage 1 子任务 01：业务时间口径——跨 UTC 日期边界、DST 地区规则与无效 IANA 时区。

纯函数测试，不碰数据库；固定时钟由显式构造的带时区绝对时刻给出，不依赖系统当前时间。
"""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from business_time import business_date, require_iana_timezone

TZ_SHANGHAI = "Asia/Shanghai"
TZ_NEW_YORK = "America/New_York"
TZ_BERLIN = "Europe/Berlin"


def test_business_date_across_utc_date_boundary() -> None:
    """同一绝对时刻：UTC 尚在 6/1，上海业务时区已跨午夜到 6/2。"""
    instant = datetime(2026, 6, 1, 16, 30, tzinfo=UTC)
    assert instant.astimezone(ZoneInfo("UTC")).date() == date(2026, 6, 1)
    assert business_date(instant, TZ_SHANGHAI) == date(2026, 6, 2)


def test_business_date_uses_region_rules_not_fixed_offset() -> None:
    """DST 地区：同一时区冬夏产生不同 UTC 偏移，固定偏移替代必然算错其中一个日期。"""
    summer = datetime(2026, 7, 1, 4, 0, tzinfo=UTC)
    winter = datetime(2026, 1, 1, 4, 0, tzinfo=UTC)
    zone = ZoneInfo(TZ_NEW_YORK)
    assert summer.astimezone(zone).utcoffset() != winter.astimezone(zone).utcoffset()
    assert business_date(summer, TZ_NEW_YORK) == date(2026, 7, 1)
    assert business_date(winter, TZ_NEW_YORK) == date(2025, 12, 31)


def test_business_date_rejects_naive_instant() -> None:
    """无时区的本地时间不是绝对时刻：拒绝，不猜测它的含义。"""
    with pytest.raises(ValueError):
        business_date(datetime(2026, 6, 1, 16, 30), TZ_SHANGHAI)


def test_invalid_iana_timezone_fails_instead_of_falling_back_to_utc() -> None:
    """无效 IANA 时区明确失败，不回退 UTC：校验与业务日期两条路径都拒绝。"""
    with pytest.raises(ZoneInfoNotFoundError):
        require_iana_timezone("Not/ARealZone")
    instant = datetime(2026, 6, 1, 16, 30, tzinfo=UTC)
    with pytest.raises(ZoneInfoNotFoundError):
        business_date(instant, "Not/ARealZone")


def test_require_iana_timezone_accepts_valid_region() -> None:
    assert require_iana_timezone(TZ_BERLIN) == TZ_BERLIN
