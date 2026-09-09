"""profile 确定性规则：结构校验、缺失表达、补丁校验与纯内存应用（正本 architecture/02）。

纯函数、不碰 IO（不读库、不写库、不自行提交）。共享契约复用 ``domain.actions.rules``
的模式词表与校验，不另造词表。

边界（stage1.md §5 S1-04）：

- 只做结构表示与引用校验；限制是否命中动作、红旗是否阻断由 S1-05 判定。
- 不新增业务必填规则、医学阈值或红旗解除语义：必填只有已拍的 ``body_weight_kg``。
- 补丁计算不得修改输入对象；错误补丁在构造新档案之前失败，不产生部分修改。
- 拟议补丁与当次条件是两种类型；当次条件传入补丁接口即拒绝（02 2.4）。
"""

from collections.abc import Mapping
from typing import Any

from domain.actions.rules import InvalidMode, validate_modes
from domain.profile.schema import (
    FACT_FIELDS,
    FACT_VALUE_KINDS,
    RESTRICTION_SCOPES,
    ActionRestriction,
    Fact,
    Profile,
    ProfilePatch,
    SessionConditions,
)


class InvalidProfile(ValueError):
    """档案结构不合法（类型不符、字段未登记、限制结构非法）。"""


class InvalidProfilePatch(ValueError):
    """拟议补丁结构不合法：整体拒绝，不做部分应用。"""


class InvalidRestriction(ValueError):
    """单条动作限制结构非法（粒度、空目标、模式不在已拍词表内）。"""


class IncompleteProfile(ValueError):
    """必填事实缺失：不生成完整档案、不填默认值（2026-09-09 已拍）。"""


class UnknownExerciseReference(ValueError):
    """限制引用的具体动作身份不在动作目录内（含停用动作；停用不删除）。"""


def validate_profile_structure(profile: Profile) -> None:
    """校验档案结构：字段类型与限制结构；不校验值域阈值（未拍，不新增）。"""
    if not isinstance(profile, Profile):
        raise InvalidProfile(f"不是档案结构：{type(profile).__name__}")
    for name in FACT_FIELDS:
        fact = getattr(profile, name)
        if not isinstance(fact, Fact):
            raise InvalidProfile(f"档案字段 {name} 不是三态事实：{fact!r}")
        if not fact.is_known:
            continue
        _validate_value(name, fact.value)


def _validate_value(name: str, value: Any) -> None:
    kind = FACT_VALUE_KINDS[name]
    if kind == "text":
        if not isinstance(value, str) or not value.strip():
            raise InvalidProfile(f"档案字段 {name} 需要非空文本：{value!r}")
        return
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidProfile(f"档案字段 {name} 需要整数：{value!r}")
        return
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidProfile(f"档案字段 {name} 需要数值：{value!r}")
        return
    if kind == "text_list":
        _validate_text_list(name, value)
        return
    if kind == "restrictions":
        if not isinstance(value, tuple):
            raise InvalidProfile(f"档案字段 {name} 需要限制元组：{value!r}")
        for restriction in value:
            try:
                validate_restriction(restriction)
            except InvalidRestriction as exc:
                raise InvalidProfile(f"档案字段 {name} 含非法限制：{exc}") from exc
        return
    raise InvalidProfile(f"档案字段 {name} 的值类型标记未知：{kind}")


def _validate_text_list(name: str, value: Any) -> None:
    if not isinstance(value, tuple):
        raise InvalidProfile(f"档案字段 {name} 需要文本元组：{value!r}")
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise InvalidProfile(f"档案字段 {name} 含非空文本要求不满足项：{item!r}")
    if len(set(value)) != len(value):
        raise InvalidProfile(f"档案字段 {name} 含重复项：{value!r}")


def validate_restriction(restriction: ActionRestriction) -> None:
    """校验一条限制的结构：粒度合法、目标非空、模式取自 13 项已拍词表。"""
    if not isinstance(restriction, ActionRestriction):
        raise InvalidRestriction(f"不是动作限制结构：{restriction!r}")
    if restriction.scope not in RESTRICTION_SCOPES:
        raise InvalidRestriction(f"限制粒度非法：{restriction.scope!r}")
    target = restriction.target
    if not isinstance(target, str) or not target.strip():
        raise InvalidRestriction(f"限制目标不能为空：{target!r}")
    if restriction.scope == "movement_pattern":
        try:  # 13 项词表是唯一取值域（共享契约），越界即结构非法
            validate_modes((target,))
        except InvalidMode as exc:
            raise InvalidRestriction(str(exc)) from exc


def missing_required_fields(profile: Profile) -> tuple[str, ...]:
    """仍缺失的必填字段（只有 known 满足；denied 不构成数值事实）。"""
    validate_profile_structure(profile)
    return profile.missing_required_fields


def ensure_complete_profile(profile: Profile) -> Profile:
    """返回完整档案；必填缺失即拒绝，不生成完整档案、不填默认值。"""
    missing = missing_required_fields(profile)
    if missing:
        raise IncompleteProfile(f"完整档案缺少必填事实：{list(missing)}")
    return profile


def validate_patch(patch: ProfilePatch) -> None:
    """校验拟议补丁结构；当次条件（``SessionConditions``）不是补丁，直接拒绝。"""
    if not isinstance(patch, ProfilePatch):
        raise TypeError(
            "拟议补丁必须是 ProfilePatch；当次条件（SessionConditions）不进入长期补丁"
            f"（收到 {type(patch).__name__}）"
        )
    _validate_patch_facts(patch.facts)
    for restriction in patch.add_restrictions:
        _validate_patch_restriction(restriction)
    for restriction in patch.remove_restrictions:
        _validate_patch_restriction(restriction)
    overlap = set(patch.add_restrictions) & set(patch.remove_restrictions)
    if overlap:
        conflicting = sorted(overlap, key=lambda item: (item.scope, item.target))
        raise InvalidProfilePatch(f"同一限制不能同时新增与删除：{conflicting}")


def _validate_patch_facts(facts: Mapping[str, Fact[Any]]) -> None:
    if not isinstance(facts, Mapping):
        raise InvalidProfilePatch(f"补丁 facts 需要映射：{facts!r}")
    for name, fact in facts.items():
        if name not in FACT_FIELDS:
            raise InvalidProfilePatch(f"补丁含未登记字段：{name!r}")
        if name == "action_restrictions":
            raise InvalidProfilePatch(
                "限制变更只用 add_restrictions／remove_restrictions 表达"
            )
        if not isinstance(fact, Fact):
            raise InvalidProfilePatch(f"补丁字段 {name} 不是三态事实：{fact!r}")
        if fact.is_unknown:
            raise InvalidProfilePatch(f"补丁字段 {name} 不能把事实改为未知")
        if fact.is_known:
            try:
                _validate_value(name, fact.value)
            except InvalidProfile as exc:
                raise InvalidProfilePatch(str(exc)) from exc


def _validate_patch_restriction(restriction: ActionRestriction) -> None:
    try:
        validate_restriction(restriction)
    except InvalidRestriction as exc:
        raise InvalidProfilePatch(str(exc)) from exc


def apply_patch(profile: Profile, patch: ProfilePatch) -> Profile:
    """纯内存应用补丁：返回新档案，绝不修改输入对象、绝不访问数据库。

    先整体校验再构造：任一步失败即抛错，不产生部分修改（stage1.md §5 S1-04 验收 2）。
    限制的删除只在补丁中表达，应用后仅是「拟议条件」，是否落库由外层确认事务决定。
    """
    validate_profile_structure(profile)
    validate_patch(patch)
    updated: dict[str, Fact[Any]] = {
        name: patch.facts.get(name, getattr(profile, name)) for name in FACT_FIELDS
    }
    if patch.add_restrictions or patch.remove_restrictions:
        # 限制变更只在补丁显式涉及限制时改写该字段：无关补丁不得把「未收集」写成「无限制」。
        proposed = tuple(
            restriction
            for restriction in profile.restrictions
            if restriction not in patch.remove_restrictions
        ) + tuple(
            restriction
            for restriction in patch.add_restrictions
            if restriction not in patch.remove_restrictions
        )
        updated["action_restrictions"] = Fact.known(proposed)
    return Profile(**updated)


def validate_session_conditions(conditions: SessionConditions) -> None:
    """校验当次条件结构（02 2.4）：当次条件不落长期档案、不进拟议补丁。"""
    if not isinstance(conditions, SessionConditions):
        raise TypeError(f"不是当次条件结构：{type(conditions).__name__}")
    if conditions.available_equipment is not None:
        fact = conditions.available_equipment
        if not isinstance(fact, Fact):
            raise InvalidProfile(f"当次器械条件不是三态事实：{fact!r}")
        if fact.is_known:
            _validate_text_list("session.available_equipment", fact.value)
    if conditions.red_flags is not None:
        fact = conditions.red_flags
        if not isinstance(fact, Fact):
            raise InvalidProfile(f"当次红旗条件不是三态事实：{fact!r}")
        if fact.is_known:
            _validate_text_list("session.red_flags", fact.value)
