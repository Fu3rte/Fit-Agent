"""profile 确定性规则：七字段结构校验与每周训练次数值域。"""

from typing import Any

from domain.profile.schema import FIELD_VALUE_KINDS, PROFILE_FIELDS, Fact, Profile

WEEKLY_FREQUENCY_MIN = 1
WEEKLY_FREQUENCY_MAX = 7


class InvalidProfile(ValueError):
    """画像结构不合法（类型不符、字段未登记、值域越界）。"""


class UnknownExerciseReference(ValueError):
    """禁用动作引用的稳定身份不在动作目录内。"""


def validate_profile_structure(profile: Profile) -> None:
    """校验画像结构：七字段都是三态事实，``known`` 值按字段类型与已拍值域校验。"""
    if not isinstance(profile, Profile):
        raise InvalidProfile(f"不是画像结构：{type(profile).__name__}")
    for name in PROFILE_FIELDS:
        fact = getattr(profile, name)
        if not isinstance(fact, Fact):
            raise InvalidProfile(f"画像字段 {name} 不是三态事实：{fact!r}")
        if fact.is_known:
            _validate_value(name, fact.value)


def _validate_value(name: str, value: Any) -> None:
    kind = FIELD_VALUE_KINDS[name]
    if kind == "text":
        if not isinstance(value, str) or not value.strip():
            raise InvalidProfile(f"画像字段 {name} 需要非空文本：{value!r}")
        return
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidProfile(f"画像字段 {name} 需要整数：{value!r}")
        if name == "weekly_frequency" and not (
            WEEKLY_FREQUENCY_MIN <= value <= WEEKLY_FREQUENCY_MAX
        ):
            raise InvalidProfile(
                f"每周训练次数必须在 {WEEKLY_FREQUENCY_MIN}–{WEEKLY_FREQUENCY_MAX} 之间：{value!r}"
            )
        return
    if kind == "text_list":
        _validate_text_list(name, value)
        return
    raise InvalidProfile(f"画像字段 {name} 的值类型标记未知：{kind}")


def _validate_text_list(name: str, value: Any) -> None:
    if not isinstance(value, tuple):
        raise InvalidProfile(f"画像字段 {name} 需要文本元组：{value!r}")
    if not value:
        raise InvalidProfile(
            f"画像字段 {name} 的 known 列表不得为空：明确为空必须用 denied 表达"
        )
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise InvalidProfile(f"画像字段 {name} 含非空文本要求不满足项：{item!r}")
    if len(set(value)) != len(value):
        raise InvalidProfile(f"画像字段 {name} 含重复项：{value!r}")
