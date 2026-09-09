"""profile 类型定义：档案事实三态、两类动作限制、当次条件与拟议补丁（正本 architecture/02）。

本模块只定义结构，不做判定（校验与纯内存应用在 ``domain.profile.rules``，读写经
``domain.profile.repo``）。四条硬边界：

- **三态事实**：``Fact`` 区分 unknown（尚未收集）／denied（用户明确否认）／known
  （已收集值）。不得把「未询问」写成「无」，也不得反向补造（stage1.md §5 S1-04 验收 1；
  02 2.1）。红旗只承载用户报告原文，清单外文本同样只是原文，不判安全（判定归 S1-05）。
- **完整档案必填**：``REQUIRED_FACT_FIELDS`` 只含 ``body_weight_kg``（2026-09-09 已拍：
  缺失时不生成完整档案、不填默认值）；其余必填阈值与默认处方条件未拍，本模块不新增。
- **两类动作限制**：``ActionRestriction`` 用 ``scope`` 区分具体动作（引用 ``exercises.id``
  稳定身份）与动作模式（引用 13 项已拍词表原词）；只表达「当前有效」，不带观察中／暂禁／
  永久等状态语义（stage1.md §7 已拍）。
- **长期与当次分流**：``ProfilePatch`` 是长期拟议变更；``SessionConditions`` 是当次条件
  （如「今天只能用哑铃」），两种类型互不兼容，当次条件不进入长期补丁（02 2.4）。
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Generic, Literal, TypeVar

FactState = Literal["unknown", "denied", "known"]
FACT_STATES: tuple[FactState, ...] = ("unknown", "denied", "known")

RestrictionScope = Literal["specific_action", "movement_pattern"]
RESTRICTION_SCOPES: tuple[RestrictionScope, ...] = (
    "specific_action",
    "movement_pattern",
)

T = TypeVar("T")

# 完整档案必填字段：2026-09-09 已拍只有体重（stage1.md §5 S1-04 验收 1、§7）。
REQUIRED_FACT_FIELDS: tuple[str, ...] = ("body_weight_kg",)

# 6 类已明确红旗原词（stage1.md §5 S1-05 验收 2；02 2.3）。本阶段只承载，不判定、
# 不扩充医学规则；清单以「等」收尾，清单外报告文本按未知/需澄清处理（判定归 S1-05）。
RED_FLAG_KINDS: tuple[str, ...] = (
    "胸部异常不适",
    "晕厥",
    "异常气短",
    "锐痛",
    "麻木",
    "放射痛",
)

# 档案事实字段 → 值类型标记；rules 据此做结构校验，codec 据此编解码。
FACT_VALUE_KINDS: dict[str, str] = {
    "training_goal": "text",
    "training_experience": "text",
    "weekly_frequency": "integer",
    "session_duration_minutes": "integer",
    "available_equipment": "text_list",
    "action_restrictions": "restrictions",
    "body_state": "text_list",
    "red_flags": "text_list",
    "body_weight_kg": "number",
}
FACT_FIELDS: tuple[str, ...] = tuple(FACT_VALUE_KINDS)


class InvalidProfileRow(ValueError):
    """``user_profile.profile_json`` 无法解析为档案结构：档案数据损坏，不静默吞掉。"""


@dataclass(frozen=True, slots=True)
class Fact(Generic[T]):
    """档案事实的三态承载：unknown／denied／known；后两者都不携带值。"""

    state: FactState = "unknown"
    value: T | None = None

    def __post_init__(self) -> None:
        if self.state not in FACT_STATES:
            raise ValueError(f"未知的档案事实状态：{self.state!r}")
        if self.state == "known" and self.value is None:
            raise ValueError("known 状态必须携带值")
        if self.state != "known" and self.value is not None:
            raise ValueError(f"{self.state} 状态不得携带值")

    @classmethod
    def unknown(cls) -> "Fact[T]":
        """尚未收集：不得当作「无」。"""
        return cls("unknown", None)

    @classmethod
    def denied(cls) -> "Fact[T]":
        """用户明确否认（如明确说明无红旗症状、无动作限制）。"""
        return cls("denied", None)

    @classmethod
    def known(cls, value: T) -> "Fact[T]":
        """用户已给出的值；不补造、不填默认值。"""
        return cls("known", value)

    @property
    def is_known(self) -> bool:
        return self.state == "known"

    @property
    def is_denied(self) -> bool:
        return self.state == "denied"

    @property
    def is_unknown(self) -> bool:
        return self.state == "unknown"


@dataclass(frozen=True, slots=True)
class ActionRestriction:
    """一条当前有效的动作限制（02 2.2）。

    ``scope='specific_action'`` 时 ``target`` 是动作稳定身份（``exercises.id``）；
    ``scope='movement_pattern'`` 时 ``target`` 取 13 项已拍模式词表原词。两种粒度分别
    表示、可同时存在。本类型不带状态字段：删除只能表达为拟议补丁中的 remove，不直接生效。
    """

    scope: RestrictionScope
    target: str


@dataclass(frozen=True, slots=True)
class Profile:
    """正式档案事实（02 2.1 的类别）。字段默认三态为 unknown，即「未收集」。"""

    training_goal: Fact[str] = Fact.unknown()
    training_experience: Fact[str] = Fact.unknown()
    weekly_frequency: Fact[int] = Fact.unknown()
    session_duration_minutes: Fact[int] = Fact.unknown()
    available_equipment: Fact[tuple[str, ...]] = Fact.unknown()
    action_restrictions: Fact[tuple[ActionRestriction, ...]] = Fact.unknown()
    body_state: Fact[tuple[str, ...]] = Fact.unknown()
    red_flags: Fact[tuple[str, ...]] = Fact.unknown()
    body_weight_kg: Fact[float] = Fact.unknown()

    @classmethod
    def empty(cls) -> "Profile":
        """全未知档案：未建档时的读取基线，不补造任何事实。"""
        return cls()

    @property
    def missing_required_fields(self) -> tuple[str, ...]:
        """仍缺失的必填字段：只有 known 才算满足（denied 不是数值事实的取值）。"""
        return tuple(
            name for name in REQUIRED_FACT_FIELDS if not getattr(self, name).is_known
        )

    @property
    def is_complete(self) -> bool:
        """完整档案：全部必填字段已收集（缺失时不生成完整档案、不填默认值）。"""
        return not self.missing_required_fields

    @property
    def restrictions(self) -> tuple[ActionRestriction, ...]:
        """当前有效限制；未收集或明确无限制时返回空元组（两者用 ``action_restrictions`` 区分）。"""
        fact = self.action_restrictions
        return fact.value if fact.is_known and fact.value is not None else ()

    @property
    def reported_red_flags(self) -> tuple[str, ...]:
        """用户报告的红旗原文；未收集或明确无红旗时为空元组。"""
        fact = self.red_flags
        return fact.value if fact.is_known and fact.value is not None else ()

    @property
    def unlisted_red_flag_labels(self) -> tuple[str, ...]:
        """不在 6 类已明确清单内的报告原文：只承载，不判安全/不安全（判定归 S1-05）。"""
        return tuple(
            label for label in self.reported_red_flags if label not in RED_FLAG_KINDS
        )


@dataclass(frozen=True, slots=True)
class ProfileSnapshot:
    """正式档案读取快照：``profile is None`` = 未建档技术载体（profile_json 为 NULL）。

    ``context_version`` 与档案在同一快照读出；本阶段只读，不提供推进入口（归 Stage 2）。
    """

    profile: Profile | None
    context_version: int


@dataclass(frozen=True, slots=True)
class ProfilePatch:
    """长期拟议档案变更（02 2.4）：确认前只存在于草稿，不修改正式档案。

    ``facts``：字段名 → 新事实，只允许 known／denied（补丁不表达「重新变为未知」）。
    ``add_restrictions``／``remove_restrictions``：限制的增删（删除只能这样表达）。
    本类型不含任何当次条件字段；当次条件用 :class:`SessionConditions`。
    """

    facts: Mapping[str, Fact[Any]] = field(default_factory=dict)
    add_restrictions: tuple[ActionRestriction, ...] = ()
    remove_restrictions: tuple[ActionRestriction, ...] = ()


@dataclass(frozen=True, slots=True)
class SessionConditions:
    """当次训练条件（02 2.4）：「今天只能用哑铃」只在此表达。

    与 :class:`ProfilePatch` 是互不兼容的两种类型；本阶段只处理已被上游分流的结构化
    输入，不解析自然语言意图（stage1.md §5 S1-04 验收 3）。
    """

    available_equipment: Fact[tuple[str, ...]] | None = None
    red_flags: Fact[tuple[str, ...]] | None = None


def profile_to_json(profile: Profile) -> str:
    """档案 → ``user_profile.profile_json`` 文本；键集固定为 ``FACT_FIELDS``。"""
    return json.dumps(
        {name: _encode_fact(getattr(profile, name)) for name in FACT_FIELDS},
        ensure_ascii=False,
        sort_keys=True,
    )


def profile_from_json(raw: str) -> Profile:
    """``profile_json`` 文本 → 档案；缺字段/未知字段/类型不符一律视为数据损坏。"""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidProfileRow(f"档案 JSON 无法解析：{raw!r}") from exc
    if not isinstance(payload, dict):
        raise InvalidProfileRow(f"档案 JSON 不是对象：{raw!r}")
    unknown_keys = sorted(set(payload) - set(FACT_FIELDS))
    if unknown_keys:
        raise InvalidProfileRow(f"档案 JSON 含未登记字段：{unknown_keys}")
    missing_keys = sorted(set(FACT_FIELDS) - set(payload))
    if missing_keys:
        raise InvalidProfileRow(f"档案 JSON 缺字段：{missing_keys}")
    return Profile(**{name: _decode_fact(name, payload[name]) for name in FACT_FIELDS})


def _encode_fact(fact: Fact[Any]) -> dict[str, Any]:
    encoded = _encode_value(fact.value)
    return {"state": fact.state, "value": encoded if fact.is_known else None}


def _decode_fact(name: str, raw: Any) -> Fact[Any]:
    if not isinstance(raw, dict):
        raise InvalidProfileRow(f"档案字段 {name} 不是三态对象：{raw!r}")
    if set(raw) != {"state", "value"}:
        raise InvalidProfileRow(f"档案字段 {name} 三态对象键不符：{sorted(raw)}")
    state = raw["state"]
    if state not in FACT_STATES:
        raise InvalidProfileRow(f"档案字段 {name} 状态非法：{state!r}")
    value = raw["value"]
    if state != "known":
        if value is not None:
            raise InvalidProfileRow(f"档案字段 {name} 在 {state} 状态不得有值")
        return Fact(state=state, value=None)
    return Fact.known(_decode_value(name, value))


def _encode_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return [
            {"scope": item.scope, "target": item.target}
            if isinstance(item, ActionRestriction)
            else item
            for item in value
        ]
    return value


def _decode_value(name: str, raw: Any) -> Any:
    kind = FACT_VALUE_KINDS[name]
    if kind == "text":
        return _require_text(name, raw)
    if kind == "integer":
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise InvalidProfileRow(f"档案字段 {name} 需要整数：{raw!r}")
        return raw
    if kind == "number":
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise InvalidProfileRow(f"档案字段 {name} 需要数值：{raw!r}")
        return float(raw)
    if kind == "text_list":
        return _require_text_list(name, raw)
    if kind == "restrictions":
        if not isinstance(raw, list):
            raise InvalidProfileRow(f"档案字段 {name} 需要限制数组：{raw!r}")
        return tuple(_decode_restriction(item) for item in raw)
    raise InvalidProfileRow(f"档案字段 {name} 的值类型标记未知：{kind}")


def _require_text(name: str, raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidProfileRow(f"档案字段 {name} 需要非空文本：{raw!r}")
    return raw


def _require_text_list(name: str, raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise InvalidProfileRow(f"档案字段 {name} 需要文本数组：{raw!r}")
    return tuple(_require_text(name, item) for item in raw)


def _decode_restriction(raw: Any) -> ActionRestriction:
    if not isinstance(raw, dict) or set(raw) != {"scope", "target"}:
        raise InvalidProfileRow(f"限制条目结构不符：{raw!r}")
    scope = raw["scope"]
    if scope not in RESTRICTION_SCOPES:
        raise InvalidProfileRow(f"限制粒度非法：{scope!r}")
    return ActionRestriction(
        scope=scope, target=_require_text("restriction", raw["target"])
    )
