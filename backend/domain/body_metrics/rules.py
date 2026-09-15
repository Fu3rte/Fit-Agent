"""body_metrics 确定性规则：业务日期、必填字段与数值范围（Stage 1 子任务 02 §6）。

纯函数、不碰 IO。值域与 001_initial.sql 的 CHECK 同集合（体重 20–400kg、体脂 0–100%），
不在领域层新增任何未拍阈值（例如「不得晚于今天」）。三条口径：

- 日期必须是日期对象而非时刻：``datetime`` 是 ``date`` 的子类，但它是绝对时刻，按业务时区
  解释成自然日归 ``business_time``，不在本层猜测。
- 体重必填且必须在值域内（含有限性：JSON 与库内 REAL 都不能可靠承载 NaN／Infinity）。
- 体脂可选：``None`` 表示无数据，保持空值，不补 0、不换成任何默认值。
"""

import math
from datetime import date, datetime
from typing import Any

#: 体重允许范围（kg，与库内 CHECK 同集合）。
WEIGHT_KG_MIN = 20.0
WEIGHT_KG_MAX = 400.0

#: 体脂允许范围（%，与库内 CHECK 同集合）。
BODY_FAT_PCT_MIN = 0.0
BODY_FAT_PCT_MAX = 100.0


class InvalidBodyMetric(ValueError):
    """身体指标输入不合法（日期、必填字段或数值范围）。"""


def validate_measured_on(value: Any) -> date:
    """校验并返回业务日期对象；字符串、时刻或非日期值一律拒绝。"""
    if isinstance(value, datetime) or not isinstance(value, date):
        raise InvalidBodyMetric(f"发生日期必须是日期对象（不是时刻或文本）：{value!r}")
    return value


def validate_weight_kg(value: Any) -> float:
    """校验并返回体重（kg）：必填、有限、在 20–400 之间。"""
    number = _require_finite_number("体重", value)
    if not WEIGHT_KG_MIN <= number <= WEIGHT_KG_MAX:
        raise InvalidBodyMetric(
            f"体重必须在 {WEIGHT_KG_MIN}–{WEIGHT_KG_MAX}kg 之间：{value!r}"
        )
    return number


def validate_body_fat_pct(value: Any) -> float | None:
    """校验并返回体脂（%）：None 表示未记录（保持空值），否则必须有限且在 0–100 之间。"""
    if value is None:
        return None
    number = _require_finite_number("体脂", value)
    if not BODY_FAT_PCT_MIN <= number <= BODY_FAT_PCT_MAX:
        raise InvalidBodyMetric(
            f"体脂必须在 {BODY_FAT_PCT_MIN}–{BODY_FAT_PCT_MAX}% 之间：{value!r}"
        )
    return number


def _require_finite_number(label: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidBodyMetric(f"{label}必须是数值：{value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise InvalidBodyMetric(f"{label}必须是有限数值（JSON 无法表达 NaN／Infinity）：{value!r}")
    return number
