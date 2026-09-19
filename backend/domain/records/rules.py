"""records 确定性规则：训练日期、组事实与组数的校验。"""

import math
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, cast

from domain.actions.rules import LOAD_CONVENTIONS, RECORD_TYPES
from domain.actions.schema import LoadConvention, RecordType
from domain.records.schema import SET_TYPES, SetType, WorkoutSetInput

REPS_MIN = 1
REPS_MAX = 100

DURATION_SECONDS_MIN = 1

WEIGHT_KG_MIN = 0.0
WEIGHT_KG_MAX = 1000.0
WEIGHT_KG_DECIMALS = 1
PRECISION_EPSILON = 1e-9

SET_NO_MIN = 1
SET_NO_MAX = 50

SETS_PER_SESSION_MIN = 1
SETS_PER_SESSION_MAX = 50


class InvalidRecordFact(ValueError):
    """训练记录输入不合法（日期、动作身份、负重口径、重量、次数、组数或组类型）。"""


def validate_performed_on(value: Any) -> date:
    """校验并返回训练日期；字符串、绝对时刻或非日期值一律拒绝。"""
    if isinstance(value, datetime) or not isinstance(value, date):
        raise InvalidRecordFact(f"训练日期必须是日期对象（不是时刻或文本）：{value!r}")
    return value


def validate_exercise_id(value: Any) -> str:
    """校验并返回动作稳定身份：必须是非空文本（是否存在归目录复验）。"""
    if not isinstance(value, str) or not value.strip():
        raise InvalidRecordFact(f"动作身份必须是非空文本：{value!r}")
    return value


def validate_set_no(value: Any) -> int:
    """校验并返回动作内组序号：整数且在 1–50 之间（``bool`` 不是序号）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRecordFact(f"组序号必须是整数：{value!r}")
    if not SET_NO_MIN <= value <= SET_NO_MAX:
        raise InvalidRecordFact(
            f"组序号必须在 {SET_NO_MIN}–{SET_NO_MAX} 之间：{value!r}"
        )
    return value


def validate_reps(value: Any) -> int | None:
    """校验并返回单组次数：``None`` 表示该组不记录次数（计时组）。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRecordFact(f"单组次数必须是整数：{value!r}")
    if not REPS_MIN <= value <= REPS_MAX:
        raise InvalidRecordFact(f"单组次数必须在 {REPS_MIN}–{REPS_MAX} 之间：{value!r}")
    return value


def validate_duration_seconds(value: Any) -> int | None:
    """校验并返回单组持续秒数：``None`` 表示该组不记录时长（外加重量／自重组）。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRecordFact(f"持续秒数必须是整数：{value!r}")
    if value < DURATION_SECONDS_MIN:
        raise InvalidRecordFact(
            f"持续秒数必须不小于 {DURATION_SECONDS_MIN} 秒：{value!r}"
        )
    return value


def validate_set_fields_for_record_type(
    record_type: RecordType,
    *,
    reps: int | None,
    duration_seconds: int | None,
) -> None:
    """按目录动作的记录口径校验组的必填／互斥字段。"""
    if record_type not in RECORD_TYPES:
        raise InvalidRecordFact(f"记录口径不在目录三类内：{record_type!r}")
    if record_type == "time":
        if duration_seconds is None:
            raise InvalidRecordFact("计时动作必须记录持续秒数")
        if reps is not None:
            raise InvalidRecordFact("计时动作不得记录次数")
        return
    if reps is None:
        raise InvalidRecordFact(f"{record_type} 型动作必须记录次数")
    if duration_seconds is not None:
        raise InvalidRecordFact(f"{record_type} 型动作不得记录持续秒数")


def validate_weight_kg(value: Any) -> float | None:
    """校验并返回重量（kg）：``None`` 表示该组无负重（自重／计时型保持空值）。"""
    if value is None:
        return None
    number = _require_finite_number("重量", value)
    if not WEIGHT_KG_MIN <= number <= WEIGHT_KG_MAX:
        raise InvalidRecordFact(
            f"重量必须在 {WEIGHT_KG_MIN}–{WEIGHT_KG_MAX}kg 之间：{value!r}"
        )
    if abs(number - round(number, WEIGHT_KG_DECIMALS)) >= PRECISION_EPSILON:
        raise InvalidRecordFact(f"重量最多一位小数：{value!r}")
    return number


def validate_set_type(value: Any) -> SetType:
    """校验并返回组类型：必须是 work／warmup／assisted 之一，无「未申报」态。"""
    if value not in SET_TYPES:
        raise InvalidRecordFact(f"组类型必须是 {SET_TYPES} 之一：{value!r}")
    return cast(SetType, value)


def validate_load_convention(value: Any) -> LoadConvention | None:
    """校验并返回负重口径：``None`` 表示无口径（自重／计时型）。"""
    if value is None:
        return None
    if value not in LOAD_CONVENTIONS:
        raise InvalidRecordFact(f"负重口径必须是六种之一：{value!r}")
    return cast(LoadConvention, value)


def validate_session_sets(
    values: Sequence[WorkoutSetInput],
) -> tuple[WorkoutSetInput, ...]:
    """校验一次训练的全部组事实并返回归一化结果（不落库、不读目录）。"""
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise InvalidRecordFact(f"一次训练的组必须是序列：{values!r}")
    if not SETS_PER_SESSION_MIN <= len(values) <= SETS_PER_SESSION_MAX:
        raise InvalidRecordFact(
            f"一次训练总组数必须在 {SETS_PER_SESSION_MIN}–{SETS_PER_SESSION_MAX} 之间："
            f"{len(values)}"
        )
    facts = tuple(_validate_set_fact(fact) for fact in values)
    _require_unique_set_numbers(facts)
    return facts


def _validate_set_fact(fact: WorkoutSetInput) -> WorkoutSetInput:
    if not isinstance(fact, WorkoutSetInput):
        raise InvalidRecordFact(f"组事实必须是 WorkoutSetInput：{fact!r}")
    load_convention = validate_load_convention(fact.load_convention)
    weight_kg = validate_weight_kg(fact.weight_kg)
    if (load_convention is None) != (weight_kg is None):
        raise InvalidRecordFact(
            f"重量与负重口径必须同现同隐：convention={load_convention!r} weight={weight_kg!r}"
        )
    return WorkoutSetInput(
        exercise_id=validate_exercise_id(fact.exercise_id),
        set_no=validate_set_no(fact.set_no),
        reps=validate_reps(fact.reps),
        set_type=validate_set_type(fact.set_type),
        load_convention=load_convention,
        weight_kg=weight_kg,
        duration_seconds=validate_duration_seconds(fact.duration_seconds),
    )


def _require_unique_set_numbers(facts: Sequence[WorkoutSetInput]) -> None:
    """同一动作内组序号不重复：库内 UNIQUE 的领域侧同集合校验，避免裸 IntegrityError。"""
    seen: set[tuple[str, int]] = set()
    for fact in facts:
        key = (fact.exercise_id, fact.set_no)
        if key in seen:
            raise InvalidRecordFact(f"同一动作的组序号重复：{key[0]} 第 {key[1]} 组")
        seen.add(key)


def _require_finite_number(label: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidRecordFact(f"{label}必须是数值：{value!r}")
    # 超大整数（如 10**400）转 float 会抛 OverflowError，同样按非法输入拒绝，不漏出原生异常。
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise InvalidRecordFact(f"{label}超出可表示范围：{value!r}") from exc
    if not math.isfinite(number):
        raise InvalidRecordFact(f"{label}必须是有限数值（JSON 无法表达 NaN／Infinity）：{value!r}")
    return number
