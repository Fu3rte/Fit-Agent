"""Stage 1 S1-04：档案事实结构与三态表达（验收 1）。

验收对照（stage1.md §5 S1-04 验收 1）：未知与明确否认可区分；不补造训练经验、身体状态
或「无红旗」；``body_weight_kg`` 属完整档案必填，缺失时不生成完整档案、不填默认值；其余
必填阈值与默认处方条件不擅定。红旗只承载与三态表达（判定归 S1-05）。

本模块不访问数据库（结构层用例如实跑纯函数）；写入与持久化见
``tests/test_stage1_profile_write.py``。
"""

import pytest

from domain.profile import rules, schema
from domain.profile.schema import (
    ActionRestriction,
    Fact,
    Profile,
    profile_from_json,
    profile_to_json,
)

# ---------- 三态：未知 ≠ 明确否认 ----------


def test_fact_states_are_three_and_carry_values_only_when_known() -> None:
    unknown = Fact.unknown()
    denied = Fact.denied()
    known = Fact.known("增肌")
    assert (unknown.is_unknown, unknown.is_denied, unknown.is_known) == (
        True,
        False,
        False,
    )
    assert (denied.is_unknown, denied.is_denied, denied.is_known) == (
        False,
        True,
        False,
    )
    assert (known.is_unknown, known.is_denied, known.is_known) == (False, False, True)
    assert unknown.value is None and denied.value is None and known.value == "增肌"


@pytest.mark.parametrize(
    ("state", "value"),
    [("known", None), ("unknown", "值"), ("denied", "值"), ("something", None)],
)
def test_fact_rejects_inconsistent_state_and_value(state: str, value: object) -> None:
    with pytest.raises(ValueError):
        Fact(state=state, value=value)  # type: ignore[arg-type]


def test_empty_profile_fabricates_nothing() -> None:
    profile = Profile.empty()
    for name in schema.FACT_FIELDS:
        fact = getattr(profile, name)
        assert fact.is_unknown, name
        assert fact.value is None, name
    assert profile.restrictions == ()
    assert profile.reported_red_flags == ()
    assert profile.missing_required_fields == ("body_weight_kg",)
    assert profile.is_complete is False


def test_denied_is_not_unknown_and_not_a_required_value() -> None:
    profile = Profile(training_experience=Fact.denied(), body_weight_kg=Fact.denied())
    assert profile.training_experience.is_denied
    assert not profile.training_experience.is_unknown
    # 明确否认不能充当必填数值事实：仍不构成完整档案，也不填默认值
    assert profile.missing_required_fields == ("body_weight_kg",)
    assert profile.is_complete is False
    with pytest.raises(rules.IncompleteProfile):
        rules.ensure_complete_profile(profile)


def test_body_weight_kg_is_the_only_required_fact() -> None:
    assert schema.REQUIRED_FACT_FIELDS == ("body_weight_kg",)
    profile = Profile(body_weight_kg=Fact.known(72.5))
    assert profile.missing_required_fields == ()
    assert profile.is_complete is True
    assert rules.ensure_complete_profile(profile) is profile
    # 其余事实仍为未收集：不得因补齐体重而补造目标/经验/身体状态/红旗
    assert profile.training_goal.is_unknown
    assert profile.training_experience.is_unknown
    assert profile.body_state.is_unknown
    assert profile.red_flags.is_unknown


def test_missing_required_fields_validates_structure_first() -> None:
    with pytest.raises(rules.InvalidProfile):
        rules.missing_required_fields(Profile(body_weight_kg=Fact.known("七十")))  # type: ignore[arg-type]


# ---------- 结构校验 ----------


def test_valid_profile_structure_passes() -> None:
    profile = Profile(
        training_goal=Fact.known("增肌"),
        training_experience=Fact.known("半年，每周三次"),
        weekly_frequency=Fact.known(3),
        session_duration_minutes=Fact.known(60),
        available_equipment=Fact.known(("哑铃", "卧推架")),
        action_restrictions=Fact.known(
            (
                ActionRestriction("specific_action", "barbell-back-squat"),
                ActionRestriction("movement_pattern", "深蹲"),
            )
        ),
        body_state=Fact.known(("肩部偶有不适",)),
        red_flags=Fact.denied(),
        body_weight_kg=Fact.known(70),
    )
    rules.validate_profile_structure(profile)
    assert profile.restrictions == (
        ActionRestriction("specific_action", "barbell-back-squat"),
        ActionRestriction("movement_pattern", "深蹲"),
    )


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("training_goal", 1),
        ("training_goal", ""),
        ("training_experience", ("x",)),
        ("weekly_frequency", "3"),
        ("weekly_frequency", True),
        ("session_duration_minutes", 1.5),
        ("available_equipment", "哑铃"),
        ("available_equipment", ("哑铃", "哑铃")),
        ("available_equipment", ("",)),
        ("body_state", "肩部不适"),
        ("red_flags", ("胸部异常不适", "胸部异常不适")),
        ("body_weight_kg", "70"),
        ("body_weight_kg", True),
        ("action_restrictions", (("specific_action", "x"),)),
    ],
)
def test_invalid_profile_values_are_rejected(field: str, bad_value: object) -> None:
    profile = Profile(**{field: Fact.known(bad_value)})
    with pytest.raises(rules.InvalidProfile):
        rules.validate_profile_structure(profile)


def test_non_profile_objects_are_rejected() -> None:
    with pytest.raises(rules.InvalidProfile):
        rules.validate_profile_structure({"training_goal": "增肌"})  # type: ignore[arg-type]


# ---------- 红旗承载（不判定） ----------


def test_red_flag_kinds_are_the_six_decided_labels() -> None:
    assert schema.RED_FLAG_KINDS == (
        "胸部异常不适",
        "晕厥",
        "异常气短",
        "锐痛",
        "麻木",
        "放射痛",
    )


def test_red_flags_carry_reports_and_unknown_is_not_denied() -> None:
    reported = Profile(red_flags=Fact.known(("胸部异常不适", "麻木")))
    assert reported.reported_red_flags == ("胸部异常不适", "麻木")
    assert reported.unlisted_red_flag_labels == ()
    none_reported = Profile(red_flags=Fact.denied())
    assert none_reported.reported_red_flags == ()
    assert none_reported.red_flags.is_denied
    not_asked = Profile.empty()
    assert not_asked.reported_red_flags == ()
    assert not_asked.red_flags.is_unknown
    assert not_asked.red_flags.is_denied is False


def test_unlisted_symptom_text_is_carried_without_safety_verdict() -> None:
    profile = Profile(red_flags=Fact.known(("肩部偶有刺痛感", "头晕")))
    # 清单外文本只按原文返回，不判为无红旗也不判为已明确红旗（判定归 S1-05）
    assert profile.unlisted_red_flag_labels == ("肩部偶有刺痛感", "头晕")
    assert profile.reported_red_flags == ("肩部偶有刺痛感", "头晕")


# ---------- profile_json 编解码 ----------


def _sample_profile() -> Profile:
    return Profile(
        training_goal=Fact.known("增肌"),
        training_experience=Fact.denied(),
        weekly_frequency=Fact.known(4),
        session_duration_minutes=Fact.unknown(),
        available_equipment=Fact.known(("哑铃",)),
        action_restrictions=Fact.known(
            (ActionRestriction("movement_pattern", "水平推"),)
        ),
        body_state=Fact.known(("肩部偶有不适",)),
        red_flags=Fact.denied(),
        body_weight_kg=Fact.known(70.0),
    )


def test_profile_json_roundtrip_preserves_tri_state_and_restrictions() -> None:
    profile = _sample_profile()
    assert profile_from_json(profile_to_json(profile)) == profile


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        '{"training_goal": {"state": "known", "value": "增肌"}}',  # 缺字段
        '{"training_goal": {"state": "known", "value": "增肌"}, "extra": 1}',  # 未登记字段
        '{"training_goal": {"state": "maybe", "value": null}}',  # 状态非法
        '{"training_goal": {"state": "denied", "value": "增肌"}}',  # denied 带值
        '{"training_goal": {"state": "known", "value": null}}',  # known 无值
        '{"training_goal": "增肌"}',  # 非三态对象
        '{"training_goal": {"state": "known"}}',  # 键不符
        '{"weekly_frequency": {"state": "known", "value": "3"}}',  # 类型不符
        '{"body_weight_kg": {"state": "known", "value": true}}',
        '{"action_restrictions": {"state": "known", "value": []}}',
        '{"action_restrictions": {"state": "known", "value": [{"scope": "mode", "target": "深蹲"}]}}',
        '{"action_restrictions": {"state": "known", "value": [{"scope": "movement_pattern", "target": "深蹲", "status": "permanent"}]}}',
    ],
)
def test_profile_from_json_rejects_corrupt_payloads(payload: str) -> None:
    with pytest.raises(schema.InvalidProfileRow):
        profile_from_json(payload)
