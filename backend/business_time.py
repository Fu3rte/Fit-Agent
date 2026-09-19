"""业务时间口径：固定业务时区下的自然日与 IANA 时区校验。"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

__all__ = ["business_date", "require_iana_timezone"]


def require_iana_timezone(timezone_name: str) -> str:
    """校验并返回 IANA 地区名；无法解析时抛 ZoneInfoNotFoundError（不回退 UTC）。"""
    ZoneInfo(timezone_name)
    return timezone_name


def business_date(instant: datetime, timezone_name: str) -> date:
    """固定业务时区下的自然日（今天／训练日期／日程到期的解释口径基础）。"""
    if instant.tzinfo is None:
        raise ValueError("instant 必须是带时区的绝对时刻")
    return instant.astimezone(ZoneInfo(timezone_name)).date()
