"""Stage 1 S1-04：档案事实结构与三态表达（验收 1）。

验收对照（stage1.md §5 S1-04 验收 1）：未知与明确否认可区分；不补造训练经验或身体情况；
``body_weight_kg`` 属完整档案必填，缺失时不生成完整档案、不填默认值；其余必填阈值与默认
处方条件不擅定。身体情况只承载报告原文与三态表达，分类 / 红旗判定归 S1-05。

本模块不访问数据库（结构层用例如实跑纯函数）；写入与持久化见
``tests/test_stage1_profile_write.py``。
"""

import json

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
    assert profile.missing_required_fields == ("body_weight_kg",)
    assert profile.body_conditions.is_unknown
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
    # 其余事实仍为未收集：不得因补齐体重而补造目标/经验/身体情况
    assert profile.training_goal.is_unknown
    assert profile.training_experience.is_unknown
    assert profile.body_conditions.is_unknown


def test_profile_has_exactly_the_eight_new_fact_fields() -> None:
    """身体情况合并后的档案字段恰八项：两项旧身体情况字段已不在档案结构里。"""
    assert schema.FACT_FIELDS == (
        "training_goal",
        "training_experience",
        "weekly_frequency",
        "session_duration_minutes",
        "available_equipment",
        "action_restrictions",
        "body_conditions",
        "body_weight_kg",
    )
    assert len(schema.FACT_FIELDS) == 8
    assert set(Profile.__dataclass_fields__) == set(schema.FACT_FIELDS)
    for removed in (
        "body_state",
        "red_flags",
        "reported_red_flags",
        "unlisted_red_flag_labels",
    ):
        assert not hasattr(Profile, removed)
        assert not hasattr(Profile.empty(), removed)


def test_body_conditions_keeps_the_three_states() -> None:
    """三态不得退化：未收集 ≠ 明确无 ≠ 有报告原文。"""
    not_asked = Profile.empty().body_conditions
    none_reported = Profile(body_conditions=Fact.denied()).body_conditions
    reported = Profile(body_conditions=Fact.known(("深蹲时膝盖锐痛",))).body_conditions
    assert (not_asked.is_unknown, not_asked.is_denied, not_asked.is_known) == (
        True,
        False,
        False,
    )
    assert (
        none_reported.is_unknown,
        none_reported.is_denied,
        none_reported.is_known,
    ) == (
        False,
        True,
        False,
    )
    assert (reported.is_unknown, reported.is_denied, reported.is_known) == (
        False,
        False,
        True,
    )
    assert not_asked.value is None and none_reported.value is None
    assert reported.value == ("深蹲时膝盖锐痛",)
    # 显式空集合是第三种表达，不等同 denied
    empty = Profile(body_conditions=Fact.known(())).body_conditions
    assert empty.is_known and empty.value == ()


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
        body_conditions=Fact.known(("肩部偶有不适",)),
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
        ("body_conditions", "肩部不适"),
        ("body_conditions", ("深蹲时膝盖锐痛", "深蹲时膝盖锐痛")),
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


def test_body_conditions_carry_raw_reports_and_unknown_is_not_denied() -> None:
    reported = Profile(body_conditions=Fact.known(("胸部异常不适", "麻木")))
    assert reported.body_conditions.value == ("胸部异常不适", "麻木")
    none_reported = Profile(body_conditions=Fact.denied())
    assert none_reported.body_conditions.is_denied
    not_asked = Profile.empty()
    assert not_asked.body_conditions.is_unknown
    assert not_asked.body_conditions.is_denied is False


def test_body_conditions_store_raw_text_without_classification() -> None:
    """只保存报告原文：清单内外都不在 schema 层分类（判定归 S1-05）。"""
    profile = Profile(body_conditions=Fact.known(("深蹲时膝盖锐痛", "头晕")))
    assert profile.body_conditions.value == ("深蹲时膝盖锐痛", "头晕")


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
        body_conditions=Fact.known(("肩部偶有不适",)),
        body_weight_kg=Fact.known(70.0),
    )


def test_profile_json_roundtrip_preserves_tri_state_and_restrictions() -> None:
    profile = _sample_profile()
    assert profile_from_json(profile_to_json(profile)) == profile


def test_profile_to_json_writes_only_the_eight_new_fields() -> None:
    """新写入只产生八字段结构：键集固定，旧两项身体情况字段不再写回。"""
    payload = json.loads(profile_to_json(_sample_profile()))
    assert sorted(payload) == sorted(schema.FACT_FIELDS)
    assert payload["body_conditions"] == {
        "state": "known",
        "value": ["肩部偶有不适"],
    }
    assert "body_state" not in payload and "red_flags" not in payload


def _legacy_payload(
    body_state: dict[str, object] | None,
    red_flags: dict[str, object] | None,
    *,
    include_body_conditions: bool = False,
    include_all_others: bool = True,
) -> str:
    """旧版九字段 JSON：两项旧身体情况 + 其余七项（``body_conditions`` 只在混用处出现）。"""
    payload: dict[str, object] = json.loads(profile_to_json(_sample_profile()))
    del payload["body_conditions"]
    payload["body_weight_kg"] = {"state": "known", "value": 70.0}
    if not include_all_others:
        del payload["training_goal"]
    if body_state is not None:
        payload["body_state"] = body_state
    if red_flags is not None:
        payload["red_flags"] = red_flags
    if include_body_conditions:
        payload["body_conditions"] = {"state": "known", "value": ["肩部偶有不适"]}
    return json.dumps(payload, ensure_ascii=False)


@pytest.mark.parametrize(
    ("body_state", "red_flags", "expected"),
    [
        (
            {"state": "unknown", "value": None},
            {"state": "unknown", "value": None},
            Fact.unknown(),
        ),
        (
            {"state": "denied", "value": None},
            {"state": "denied", "value": None},
            Fact.denied(),
        ),
        (
            {"state": "known", "value": []},
            {"state": "denied", "value": None},
            Fact.denied(),
        ),
        (
            {"state": "known", "value": ["肩部偶有不适"]},
            {"state": "denied", "value": None},
            Fact.known(("肩部偶有不适",)),
        ),
        (
            {"state": "unknown", "value": None},
            {"state": "known", "value": ["晕厥"]},
            Fact.known(("晕厥",)),
        ),
        (
            {"state": "unknown", "value": None},
            {"state": "known", "value": []},
            Fact.denied(),
        ),
        (
            {"state": "unknown", "value": None},
            {"state": "denied", "value": None},
            Fact.denied(),
        ),
        (
            {"state": "known", "value": ["深蹲时膝盖锐痛", "麻木"]},
            {"state": "known", "value": ["麻木", "晕厥"]},
            Fact.known(("深蹲时膝盖锐痛", "麻木", "晕厥")),
        ),
    ],
)
def test_legacy_nine_field_json_is_read_and_merged(
    body_state: dict[str, object], red_flags: dict[str, object], expected: Fact[object]
) -> None:
    """旧九字段 JSON 能读取并合并为 ``body_conditions``（保序去重，读旧写新）。

    「明确无」的两种旧写法（``denied`` 与显式空集合 ``known(())``）一律归一为 ``denied``：
    两者语义相同、行为一致，不为历史信息保真而新增运行期标记（规格 §1.1）。
    """
    profile = profile_from_json(_legacy_payload(body_state, red_flags))
    assert profile.body_conditions == expected
    # 读入即合并：再次写出只产生新八字段结构
    assert "body_state" not in profile_to_json(profile)
    assert "red_flags" not in profile_to_json(profile)


@pytest.mark.parametrize(
    "payload",
    [
        # 旧字段与 body_conditions 混用
        _legacy_payload(
            {"state": "known", "value": ["肩部偶有不适"]},
            {"state": "denied", "value": None},
            include_body_conditions=True,
        ),
        # 旧身体情况只给一半
        _legacy_payload({"state": "denied", "value": None}, None),
        _legacy_payload(None, {"state": "denied", "value": None}),
        # 旧字段 + 未登记字段
        json.dumps(
            {
                **json.loads(
                    _legacy_payload(
                        {"state": "denied", "value": None},
                        {"state": "denied", "value": None},
                    )
                ),
                "symptoms": {"state": "denied", "value": None},
            },
            ensure_ascii=False,
        ),
        # 旧字段 + 缺其他字段
        _legacy_payload(
            {"state": "denied", "value": None},
            {"state": "denied", "value": None},
            include_all_others=False,
        ),
    ],
)
def test_illegal_legacy_or_mixed_body_condition_json_is_rejected(payload: str) -> None:
    with pytest.raises(schema.InvalidProfileRow):
        profile_from_json(payload)


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
        '{"body_conditions": "肩部不适"}',  # 身体情况非三态对象
        '{"body_conditions": {"state": "known", "value": "肩部不适"}}',  # text_list 值类型不符
    ],
)
def test_profile_from_json_rejects_corrupt_payloads(payload: str) -> None:
    with pytest.raises(schema.InvalidProfileRow):
        profile_from_json(payload)
