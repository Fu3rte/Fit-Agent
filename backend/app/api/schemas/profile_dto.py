"""画像表单的请求体模型与响应 DTO。"""

from typing import Any

from pydantic import BaseModel, ConfigDict

from app.api.errors import InvalidRequestShape
from app.domain.profile.schema import (
    FIELD_VALUE_KINDS,
    PROFILE_FIELDS,
    Fact,
    FactState,
    Profile,
)


class ProfileFactIn(BaseModel):
    """画像单字段的三态事实：``known`` 携带值，``unknown``／``denied`` 的值必须为 null。"""

    model_config = ConfigDict(extra="forbid")

    state: FactState
    value: Any = None


class ProfileBody(BaseModel):
    """画像整份覆盖请求体（PUT 语义）：七字段一次写入，无 context_version。"""

    model_config = ConfigDict(extra="forbid")

    training_goal: ProfileFactIn
    weekly_frequency: ProfileFactIn
    available_equipment: ProfileFactIn
    explicit_preferences: ProfileFactIn
    current_level: ProfileFactIn
    known_injuries: ProfileFactIn
    forbidden_exercise_ids: ProfileFactIn


def profile_from_dto(body: ProfileBody) -> Profile:
    """画像请求体 → :class:`Profile`（整份覆盖）。"""
    return Profile(
        **{
            name: _fact_from_in(name, getattr(body, name))
            for name in PROFILE_FIELDS
        }
    )


def _fact_from_in(name: str, raw: ProfileFactIn) -> Fact[Any]:
    if raw.state != "known":
        if raw.value is not None:
            raise InvalidRequestShape(f"画像字段 {name} 在 {raw.state} 状态不得带值")
        return Fact(state=raw.state, value=None)
    if raw.value is None:
        raise InvalidRequestShape(f"画像字段 {name} 的 known 值不能为空")
    value = raw.value
    if FIELD_VALUE_KINDS[name] == "text_list":
        if not isinstance(value, list):
            raise InvalidRequestShape(f"画像字段 {name} 需要文本数组：{value!r}")
        return Fact.known(tuple(value))
    return Fact.known(value)


def _fact_dto(fact: Fact[Any]) -> dict[str, Any]:
    """单个三态事实 → 传输对象：``unknown``／``denied`` 不带值，``known`` 带值。"""
    value = fact.value
    if isinstance(value, tuple):
        value = list(value)
    return {"state": fact.state, "value": value if fact.is_known else None}


def profile_facts_dto(profile: Profile) -> dict[str, Any]:
    """画像 → 七字段三态对象；未填写（unknown）与明确为空（denied）保持可分。"""
    return {name: _fact_dto(getattr(profile, name)) for name in PROFILE_FIELDS}
