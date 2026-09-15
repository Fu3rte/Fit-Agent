"""业务时间口径：固定业务时区下的自然日与 IANA 时区校验（07 7.3）。

从旧 ``storage/setting_repo.py`` 迁出的纯函数语义（Stage 1 子任务 01 §3）：

- :func:`business_date` 把带时区的绝对时刻按 IANA 地区规则解释为业务自然日；不使用启动时的
  固定 UTC 偏移替代（含夏令时全年规则）。
- :func:`require_iana_timezone` 校验 IANA 地区名可被 ZoneInfo 解析；失败时大声上抛，不静默
  回退 UTC。

应用 lifespan 启动时只采样一次本机时区并注入各用例；repo、domain service 禁止直接调用
``date.today()``，也不各自读取系统时区。本模块是纯函数：不碰 IO、不依赖数据库或 Agent 框架。
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

__all__ = ["business_date", "require_iana_timezone"]


def require_iana_timezone(timezone_name: str) -> str:
    """校验并返回 IANA 地区名；无法解析时抛 ZoneInfoNotFoundError（不回退 UTC）。"""
    ZoneInfo(timezone_name)
    return timezone_name


def business_date(instant: datetime, timezone_name: str) -> date:
    """固定业务时区下的自然日（今天／训练日期／日程到期的解释口径基础）。

    ``instant`` 必须是带时区的绝对时刻；时区名无法解析时异常向上抛，不静默回退 UTC。
    """
    if instant.tzinfo is None:
        raise ValueError("instant 必须是带时区的绝对时刻")
    return instant.astimezone(ZoneInfo(timezone_name)).date()
