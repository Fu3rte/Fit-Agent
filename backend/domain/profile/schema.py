"""profile 类型定义：档案事实三态、两类动作限制、当次条件与拟议补丁（正本 architecture/02）。

本模块只定义结构，不做判定（校验与纯内存应用在 ``domain.profile.rules``，读写经
``domain.profile.repo``）。四条硬边界：

- **三态事实**：``Fact`` 区分 unknown（尚未收集）／denied（用户明确否认）／known
  （已收集值）。不得把「未询问」写成「无」，也不得反向补造（stage1.md §5 S1-04 验收 1；
  02 2.1）。身体情况（``body_conditions``）只承载用户报告原文，不分「症状」与「其他身体
  状态」；6 类清单在读取时匹配（``domain.profile.safety``），本模块不做医学判断
  （2026-09-10 拍板：原「当前身体状态」与「红旗症状」两项合并）。
- **完整档案必填**：``REQUIRED_FACT_FIELDS`` 只含 ``body_weight_kg``（2026-09-09 已拍：
  缺失时不生成完整档案、不填默认值）；其余必填阈值与默认处方条件未拍，本模块不新增。
- **首次建档完整性**：``FIRST_TIME_REQUIRED_FACT_FIELDS`` 是确认入口的八项明确回答清单
  （stage2.md §4.3 已拍 1B，2026-09-09；2026-09-10 身体情况合并后九项变八项）；
  ``EXPLICIT_NONE_FACT_FIELDS`` 是其中可用「明确无」
  回答的集合／限制类字段（2026-09-10 用户拍板 A）。两者与建档保存条件语义不同、并存：
  建档过程仍允许保存部分事实，但首次正式确认按八项清单拒绝不完整档案；本模块只承载字段
  清单与缺口，判定归 ``domain.profile.rules``。
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

# 首次建档完整性清单（stage2.md §4.3 已拍 1B，2026-09-09）：目标、经验、频率、时长、
# 器械、体重、动作限制、身体情况八项都必须有明确回答（2026-09-10 身体情况合并后九项
# 变八项）。与
# ``REQUIRED_FACT_FIELDS`` 语义不同：本清单是确认入口的完整性条件，不是建档过程的
# 保存条件——建档允许逐步补全（保存部分事实），但首次正式确认必须齐备。完整性只表示
# 信息齐备，不表示没有症状或已获训练安全许可（红旗阻断仍归 ``domain.profile.safety``）。
FIRST_TIME_REQUIRED_FACT_FIELDS: tuple[str, ...] = (
    "training_goal",
    "training_experience",
    "weekly_frequency",
    "session_duration_minutes",
    "available_equipment",
    "body_weight_kg",
    "action_restrictions",
    "body_conditions",
)

# 「明确无」可以满足首次建档的字段（2026-09-10 用户拍板 A）：只有集合／限制类事实能用
# 明确无回答——器械、动作限制、身体情况既可用 denied，也可用显式空集合（前端契约
# equipment: [] 无器械、body_conditions: [] 明确无身体情况报告）。其余五项必须给出有效值：
# 训练目标与训练经验必须是有效文本，频率、时长、体重必须是有效数值（stage2.md §4.3：
# 不能以「无」替代必需的有效数值）。
EXPLICIT_NONE_FACT_FIELDS: tuple[str, ...] = (
    "available_equipment",
    "action_restrictions",
    "body_conditions",
)

# 6 类已明确红旗原词（stage1.md §5 S1-05 验收 2；02 2.3）。本模块只承载清单，不判定、
# 不扩充医学规则；匹配在读取时按身体情况原文进行（``domain.profile.safety``），清单以
# 「等」收尾，清单外原文按未知/需澄清处理。
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
    "body_conditions": "text_list",
    "body_weight_kg": "number",
}
FACT_FIELDS: tuple[str, ...] = tuple(FACT_VALUE_KINDS)

# 旧版九字段中的两项身体情况（2026-09-10 已拍 1A：读旧写新、不新增迁移）。解码时按
# text_list 处理，读入后合并为 ``body_conditions``；写入侧只输出新版八字段。
LEGACY_BODY_CONDITION_FIELDS: tuple[str, ...] = ("body_state", "red_flags")
_DECODE_VALUE_KINDS: dict[str, str] = {
    **FACT_VALUE_KINDS,
    "body_state": "text_list",
    "red_flags": "text_list",
}


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
        """用户明确否认（如明确说明无身体情况报告、无动作限制）。"""
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
    body_conditions: Fact[tuple[str, ...]] = Fact.unknown()
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
    def first_time_missing_fields(self) -> tuple[str, ...]:
        """首次建档仍未明确回答的字段（按 :data:`FIRST_TIME_REQUIRED_FACT_FIELDS` 顺序）。

        ``unknown``（未收集）一律算缺；``denied``（明确无）只在
        :data:`EXPLICIT_NONE_FACT_FIELDS`（器械、动作限制、身体情况）上算已回答。
        其余五项必须有有效值：训练目标与训练经验必须是有效文本，频率、时长、体重必须是
        有效数值（2026-09-10 用户拍板 A）。本属性只做缺口计算，不校验结构（结构校验归
        ``domain.profile.rules``）。
        """
        missing: list[str] = []
        for name in FIRST_TIME_REQUIRED_FACT_FIELDS:
            fact = getattr(self, name)
            if fact.is_known:
                continue
            if fact.is_denied and name in EXPLICIT_NONE_FACT_FIELDS:
                continue
            missing.append(name)
        return tuple(missing)

    @property
    def is_first_time_complete(self) -> bool:
        """首次建档八项均已明确回答（「明确无」有效；不表示安全许可）。"""
        return not self.first_time_missing_fields

    @property
    def restrictions(self) -> tuple[ActionRestriction, ...]:
        """当前有效限制；未收集或明确无限制时返回空元组（两者用 ``action_restrictions`` 区分）。"""
        fact = self.action_restrictions
        return fact.value if fact.is_known and fact.value is not None else ()


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
    body_conditions: Fact[tuple[str, ...]] | None = None


def profile_to_json(profile: Profile) -> str:
    """档案 → ``user_profile.profile_json`` 文本；键集固定为 ``FACT_FIELDS``。

    只输出新版八字段；旧版九字段在 :func:`profile_from_json` 读入时即合并，不再写回。
    """
    return json.dumps(
        {name: _encode_fact(getattr(profile, name)) for name in FACT_FIELDS},
        ensure_ascii=False,
        sort_keys=True,
    )


def profile_from_json(raw: str) -> Profile:
    """``profile_json`` 文本 → 档案；缺字段/未知字段/类型不符一律视为数据损坏。

    同时接受旧版九字段（``body_state`` + ``red_flags``）：读入时保序去重合并为
    ``body_conditions``（2026-09-10 已拍 1A 读旧写新、不加迁移）；新旧字段混用视为结构非法。
    """
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidProfileRow(f"档案 JSON 无法解析：{raw!r}") from exc
    if not isinstance(payload, dict):
        raise InvalidProfileRow(f"档案 JSON 不是对象：{raw!r}")
    unknown_keys = sorted(
        set(payload) - set(FACT_FIELDS) - set(LEGACY_BODY_CONDITION_FIELDS)
    )
    if unknown_keys:
        raise InvalidProfileRow(f"档案 JSON 含未登记字段：{unknown_keys}")
    if any(name in payload for name in LEGACY_BODY_CONDITION_FIELDS):
        return _profile_from_legacy_json(payload)
    missing_keys = sorted(set(FACT_FIELDS) - set(payload))
    if missing_keys:
        raise InvalidProfileRow(f"档案 JSON 缺字段：{missing_keys}")
    return Profile(**{name: _decode_fact(name, payload[name]) for name in FACT_FIELDS})


def _profile_from_legacy_json(payload: dict[str, Any]) -> Profile:
    """旧版九字段 JSON → 档案：两项身体情况合并，其余字段照常解码。"""
    legacy_keys = [name for name in LEGACY_BODY_CONDITION_FIELDS if name in payload]
    if (
        len(legacy_keys) != len(LEGACY_BODY_CONDITION_FIELDS)
        or "body_conditions" in payload
    ):
        raise InvalidProfileRow(
            f"档案 JSON 身体情况字段新旧混用或不完整：{sorted(legacy_keys)}"
        )
    missing_keys = sorted(set(FACT_FIELDS) - {"body_conditions"} - set(payload))
    if missing_keys:
        raise InvalidProfileRow(f"档案 JSON 缺字段：{missing_keys}")
    facts = {
        name: _decode_fact(name, payload[name])
        for name in FACT_FIELDS
        if name != "body_conditions"
    }
    facts["body_conditions"] = _merge_legacy_body_conditions(
        _decode_fact("body_state", payload["body_state"]),
        _decode_fact("red_flags", payload["red_flags"]),
    )
    return Profile(**facts)


def _merge_legacy_body_conditions(
    body_state: Fact[Any], red_flags: Fact[Any]
) -> Fact[tuple[str, ...]]:
    """旧版两项身体情况合并为 ``body_conditions``（保序去重，两者都是 text_list）。

    - 两项都 ``unknown`` → ``unknown``（不得当作「无」）。
    - 任一项有报告原文 → ``known(保序去重后的原文)``。
    - 有回答但都无内容（``denied`` 或 ``known(())``）→ ``denied``。
    历史信息不完整（一项 unknown、另一项已回答）在合并后无法再区分，只在此说明，不新增
    运行期标记字段（2026-09-10 已拍 1A）。
    """
    contents: list[str] = []
    answered = False
    for fact in (body_state, red_flags):
        if fact.is_unknown:
            continue
        answered = True
        if fact.is_known and fact.value is not None:
            contents.extend(fact.value)
    if contents:
        return Fact.known(tuple(dict.fromkeys(contents)))
    return Fact.denied() if answered else Fact.unknown()


def _encode_fact(fact: Fact[Any]) -> dict[str, Any]:
    encoded = _encode_value(fact.value)
    return {"state": fact.state, "value": encoded if fact.is_known else None}


def _encode_restriction(restriction: ActionRestriction) -> dict[str, str]:
    return {"scope": restriction.scope, "target": restriction.target}


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
    kind = _DECODE_VALUE_KINDS[name]
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


def patch_to_json(patch: ProfilePatch) -> str:
    """长期拟议补丁 → ``business_drafts.proposed_profile_patch_json`` 文本。

    只输出三类可表达项：字段事实（补丁不表达 unknown，由 rules.validate_patch 保证）、
    限制增删。当次条件（``SessionConditions``）不是补丁，不在这里伪造字段。
    """
    return json.dumps(
        {
            "facts": {
                name: _encode_fact(fact) for name, fact in sorted(patch.facts.items())
            },
            "add_restrictions": [
                _encode_restriction(item) for item in patch.add_restrictions
            ],
            "remove_restrictions": [
                _encode_restriction(item) for item in patch.remove_restrictions
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def patch_from_json(raw: str) -> ProfilePatch:
    """``proposed_profile_patch_json`` 文本 → 补丁（与 :func:`patch_to_json` 互为逆）。

    只做结构解码：字段集与三态形状不符即抛 :class:`InvalidProfileRow`；补丁语义
    （不允许改为 unknown、限制只用增删表达、同项不增删并存）仍归 ``rules.validate_patch``。
    """
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidProfileRow(f"拟议补丁 JSON 无法解析：{raw!r}") from exc
    if not isinstance(payload, dict):
        raise InvalidProfileRow(f"拟议补丁 JSON 不是对象：{raw!r}")
    unknown_keys = sorted(
        set(payload) - {"facts", "add_restrictions", "remove_restrictions"}
    )
    if unknown_keys:
        raise InvalidProfileRow(f"拟议补丁含未登记字段：{unknown_keys}")
    missing_keys = sorted(
        {"facts", "add_restrictions", "remove_restrictions"} - set(payload)
    )
    if missing_keys:
        raise InvalidProfileRow(f"拟议补丁缺字段：{missing_keys}")
    raw_facts = payload["facts"]
    if not isinstance(raw_facts, dict):
        raise InvalidProfileRow(f"拟议补丁 facts 不是对象：{raw_facts!r}")
    unknown_facts = sorted(set(raw_facts) - set(FACT_FIELDS))
    if unknown_facts:
        raise InvalidProfileRow(f"拟议补丁含未登记字段：{unknown_facts}")
    return ProfilePatch(
        facts={name: _decode_fact(name, fact) for name, fact in raw_facts.items()},
        add_restrictions=_decode_restriction_list(
            "add_restrictions", payload["add_restrictions"]
        ),
        remove_restrictions=_decode_restriction_list(
            "remove_restrictions", payload["remove_restrictions"]
        ),
    )


def _decode_restriction_list(label: str, raw: Any) -> tuple[ActionRestriction, ...]:
    if not isinstance(raw, list):
        raise InvalidProfileRow(f"拟议补丁 {label} 需要限制数组：{raw!r}")
    return tuple(_decode_restriction(item) for item in raw)
