"""001_initial.sql athlete_profile：长期画像七字段与三态 JSON 编解码（讨论总结 §5.1、REFACTOR_PLAN §5.1；Stage 1 子任务 02 §5）。

七字段（已拍口径，不增不减）：训练目标、每周训练次数、可用器械、明确偏好、当前水平、已知
伤病、禁用动作 ID。三条硬边界：

- **每字段三态**（01 决策 3A）：``{state: unknown|denied|known, value: 值或 null}``，只有
  ``known`` 携带值。未填写＝``unknown``、明确为空＝``denied``，两者必须可区分，不得静默
  混为默认值或补造事实。列表字段的 ``known`` 不得为空：明确为空只能用 ``denied`` 表达
  （2026-09-15 02 拍板 A）。
- **整份覆盖**：``profile_json`` 一次写入全部七字段的三态事实（PUT 语义），未填写用
  ``unknown`` 表达；不做字段级合并、不读回旧值。
- **无 ``context_version``**：讨论总结 §8 删除全局版本过期机制，画像只承载事实本身。

本模块只做结构与编解码：值域与字段校验归 ``domain.profile.rules``，读写归
``domain.profile.repo``。解码时缺字段／多字段／状态非法一律视为档案数据损坏，大声失败。
"""

import json
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar

FactState = Literal["unknown", "denied", "known"]
FACT_STATES: tuple[FactState, ...] = ("unknown", "denied", "known")

T = TypeVar("T")

#: 字段名 → 值类型标记；rules 据此做结构校验，编解码据此解析（键集即 ``profile_json`` 键集）。
FIELD_VALUE_KINDS: dict[str, str] = {
    "training_goal": "text",
    "weekly_frequency": "integer",
    "available_equipment": "text_list",
    "explicit_preferences": "text_list",
    "current_level": "text",
    "known_injuries": "text_list",
    "forbidden_exercise_ids": "text_list",
}
PROFILE_FIELDS: tuple[str, ...] = tuple(FIELD_VALUE_KINDS)


class InvalidProfileRow(ValueError):
    """``athlete_profile.profile_json`` 无法解析为七字段三态结构：档案数据损坏。"""


@dataclass(frozen=True, slots=True)
class Fact(Generic[T]):
    """一个画像字段的三态承载：只有 ``known`` 携带值。"""

    state: FactState = "unknown"
    value: T | None = None

    def __post_init__(self) -> None:
        if self.state not in FACT_STATES:
            raise ValueError(f"未知的画像字段状态：{self.state!r}")
        if self.state == "known" and self.value is None:
            raise ValueError("known 状态必须携带值")
        if self.state != "known" and self.value is not None:
            raise ValueError(f"{self.state} 状态不得携带值")

    @classmethod
    def unknown(cls) -> "Fact[T]":
        """未填写：不得当作「明确为空」。"""
        return cls("unknown", None)

    @classmethod
    def denied(cls) -> "Fact[T]":
        """明确为空（如用户明确表示无可用器械、无已知伤病、无禁用动作）。"""
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
class Profile:
    """长期画像：七个字段的三态事实，默认全部 ``unknown``（未填写）。"""

    training_goal: Fact[str] = Fact.unknown()
    weekly_frequency: Fact[int] = Fact.unknown()
    available_equipment: Fact[tuple[str, ...]] = Fact.unknown()
    explicit_preferences: Fact[tuple[str, ...]] = Fact.unknown()
    current_level: Fact[str] = Fact.unknown()
    known_injuries: Fact[tuple[str, ...]] = Fact.unknown()
    forbidden_exercise_ids: Fact[tuple[str, ...]] = Fact.unknown()

    @classmethod
    def empty(cls) -> "Profile":
        """全未填写画像：未建档时的写入基线，不补造任何事实。"""
        return cls()


def profile_to_json(profile: Profile) -> str:
    """画像 → ``athlete_profile.profile_json`` 文本；键集固定为 :data:`PROFILE_FIELDS`。"""
    return json.dumps(
        {name: _encode_fact(getattr(profile, name)) for name in PROFILE_FIELDS},
        ensure_ascii=False,
        sort_keys=True,
    )


def profile_from_json(raw: str) -> Profile:
    """``profile_json`` 文本 → 画像；缺字段／多字段／状态或值类型不符一律视为数据损坏。"""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidProfileRow(f"画像 JSON 无法解析：{raw!r}") from exc
    if not isinstance(payload, dict):
        raise InvalidProfileRow(f"画像 JSON 不是对象：{raw!r}")
    unknown_keys = sorted(set(payload) - set(PROFILE_FIELDS))
    if unknown_keys:
        raise InvalidProfileRow(f"画像 JSON 含未登记字段：{unknown_keys}")
    missing_keys = sorted(set(PROFILE_FIELDS) - set(payload))
    if missing_keys:
        raise InvalidProfileRow(f"画像 JSON 缺字段：{missing_keys}")
    return Profile(**{name: _decode_fact(name, payload[name]) for name in PROFILE_FIELDS})


def _encode_fact(fact: Fact[Any]) -> dict[str, Any]:
    return {
        "state": fact.state,
        "value": _encode_value(fact.value) if fact.is_known else None,
    }


def _encode_value(value: Any) -> Any:
    """列表类事实在内存中是元组，写出 JSON 时转回数组（其余值原样）。"""
    return list(value) if isinstance(value, tuple) else value


def _decode_fact(name: str, raw: Any) -> Fact[Any]:
    if not isinstance(raw, dict):
        raise InvalidProfileRow(f"画像字段 {name} 不是三态对象：{raw!r}")
    if set(raw) != {"state", "value"}:
        raise InvalidProfileRow(f"画像字段 {name} 三态对象键不符：{sorted(raw)}")
    state = raw["state"]
    if state not in FACT_STATES:
        raise InvalidProfileRow(f"画像字段 {name} 状态非法：{state!r}")
    value = raw["value"]
    if state != "known":
        if value is not None:
            raise InvalidProfileRow(f"画像字段 {name} 在 {state} 状态不得有值")
        return Fact(state=state, value=None)
    return Fact.known(_decode_value(name, value))


def _decode_value(name: str, raw: Any) -> Any:
    kind = FIELD_VALUE_KINDS[name]
    if kind == "text":
        return _require_text(name, raw)
    if kind == "integer":
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise InvalidProfileRow(f"画像字段 {name} 需要整数：{raw!r}")
        return raw
    if kind == "text_list":
        if not isinstance(raw, list):
            raise InvalidProfileRow(f"画像字段 {name} 需要文本数组：{raw!r}")
        if not raw:
            raise InvalidProfileRow(
                f"画像字段 {name} 的 known 列表不得为空：明确为空必须用 denied 表达"
            )
        return tuple(_require_text(name, item) for item in raw)
    raise InvalidProfileRow(f"画像字段 {name} 的值类型标记未知：{kind}")


def _require_text(name: str, raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidProfileRow(f"画像字段 {name} 需要非空文本：{raw!r}")
    return raw
