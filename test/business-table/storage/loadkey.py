"""统一负重比较键（报告 §4.4）。

规则：lb 先按 1 lb = 0.45359237 kg 换算，再 ×1000（SCALE）后按 ROUND_HALF_EVEN 取整。
所有写入与查询入口必须使用本函数，禁止各自实现舍入；倍率或舍入规则变更须整体迁移重算。
"""

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from .errors import ValidationError

LB_TO_KG = Decimal("0.45359237")
SCALE = Decimal(1000)
MAX_KEY = 2**63 - 1  # SQLite 整数上限


def load_kg_key(load: dict | None) -> int | None:
    if load is None:
        return None
    try:
        value = Decimal(str(load["value"]))
        if not value.is_finite():
            raise ValidationError("负重必须为有限数值")
        if value < 0:
            raise ValidationError("负重不能为负")
        unit = load.get("unit")
        if unit == "lb":
            value = value * LB_TO_KG
        elif unit != "kg":
            raise ValidationError(f"未知负重单位: {unit!r}")
        key = int((value * SCALE).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationError(f"负重数值无法解析: {load.get('value')!r}") from exc
    if key > MAX_KEY:
        raise ValidationError("负重超出可存储范围")
    return key
