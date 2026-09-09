"""Stage 1 S1-03：动作模式词表与记录／负重口径的已拍契约（纯规则，不碰 IO）。

验收对照（stage1.md §5 S1-03「已确认的动作模式映射」）：13 项模式词表、24 项清单
归属、仅两处多归属、三类记录口径、五种负重口径。本模块不访问数据库；词表是
S1-04／S1-05 复用的共享契约（稳定导入路径：``domain.actions.rules``）。
"""

import pytest

from domain.actions import rules

# 逐字取自 stage1.md §5 S1-03「已确认的动作模式映射」（模式 → 动作清单原词）。
DECIDED_MODE_MEMBERSHIP: dict[str, tuple[str, ...]] = {
    "深蹲": ("杠铃背蹲", "45°腿举", "保加利亚分腿蹲"),
    "髋铰链": ("杠铃传统硬拉", "杠铃罗马尼亚硬拉"),
    "水平推": ("杠铃平板卧推", "哑铃平板卧推", "哑铃上斜卧推"),
    "垂直推": ("坐姿哑铃肩推", "自重双杠臂屈伸"),
    "水平拉": ("杠铃俯身划船", "坐姿绳索划船", "单臂哑铃划船"),
    "垂直拉": ("高位下拉", "自重引体向上"),
    "肩孤立": ("哑铃侧平举", "哑铃反向飞鸟"),
    "膝屈": ("坐姿腿弯举",),
    "膝伸": ("腿屈伸",),
    "小腿（踝跖屈）": ("器械站姿提踵",),
    "肘屈": ("哑铃弯举",),
    "肘伸": ("绳索下压", "绳索过顶臂屈伸"),
    "核心": ("悬垂举腿",),
}

# stage1.md 打印表的「另属」注释：仅两处跨模式归属（不是两个额外动作）。
DECIDED_CROSS_OVER_MODES: dict[str, tuple[str, ...]] = {
    "哑铃上斜卧推": ("垂直推",),
    "自重双杠臂屈伸": ("肘伸",),
}


def test_mode_vocabulary_is_exactly_the_thirteen_decided_modes() -> None:
    assert tuple(DECIDED_MODE_MEMBERSHIP) == rules.MODE_VOCABULARY
    assert len(rules.MODE_VOCABULARY) == 13


def test_catalog_standard_names_are_the_twenty_four_decided_items() -> None:
    decided = {name for names in DECIDED_MODE_MEMBERSHIP.values() for name in names}
    assert len(decided) == 24
    assert set(rules.CATALOG_STANDARD_NAMES) == decided
    assert len(set(rules.CATALOG_STANDARD_NAMES)) == 24  # 一个中文标准名一个身份


def test_exercise_modes_match_decided_membership_table() -> None:
    # 已拍表是「模式 → 动作」；契约常量是「动作 → 模式集合」，此处互为正反。
    derived = {name: [] for name in rules.CATALOG_STANDARD_NAMES}
    for mode, names in DECIDED_MODE_MEMBERSHIP.items():
        for name in names:
            derived[name].append(mode)
    for name, extra_modes in DECIDED_CROSS_OVER_MODES.items():
        derived[name].extend(extra_modes)
    assert {
        name: tuple(modes) for name, modes in derived.items()
    } == rules.EXERCISE_MODES


def test_only_two_cross_over_exercises_have_multiple_modes() -> None:
    multi = {
        name: modes for name, modes in rules.EXERCISE_MODES.items() if len(modes) > 1
    }
    assert multi == {
        "哑铃上斜卧推": ("水平推", "垂直推"),
        "自重双杠臂屈伸": ("垂直推", "肘伸"),
    }


def test_modes_for_returns_decided_modes_and_rejects_unknown_names() -> None:
    assert rules.modes_for("悬垂举腿") == ("核心",)
    assert rules.modes_for("哑铃上斜卧推") == ("水平推", "垂直推")
    with pytest.raises(rules.UnknownCatalogExercise):
        rules.modes_for("保加利亚深蹲")  # 相似名不自动替换为清单原词


def test_validate_modes_rejects_unknown_and_empty_sets() -> None:
    rules.validate_modes(("深蹲",))
    rules.validate_modes(["水平推", "垂直推"])
    with pytest.raises(rules.InvalidMode):
        rules.validate_modes(())
    with pytest.raises(rules.InvalidMode):
        rules.validate_modes(("髋屈",))


def test_record_types_are_exactly_the_three_decided_kinds() -> None:
    assert rules.RECORD_TYPES == ("reps_weight", "reps_bodyweight", "time")
    assert "reps_assisted" not in rules.RECORD_TYPES  # 不新增辅助负重型


def test_load_conventions_are_exactly_the_five_decided_kinds() -> None:
    assert rules.LOAD_CONVENTIONS == (
        "barbell_includes_bar_total",
        "dumbbell_per_hand",
        "machine_pin_displayed_value",
        "plate_loaded_total_excluding_empty",
        "unilateral_setting_per_side",
    )


def test_unilateral_helpers_cover_decided_single_sided_items() -> None:
    assert rules.UNILATERAL_STANDARD_NAMES == (
        "保加利亚分腿蹲",
        "单臂哑铃划船",
    )
    assert rules.is_unilateral("保加利亚分腿蹲") is True
    assert rules.is_unilateral("杠铃背蹲") is False
    with pytest.raises(rules.UnknownCatalogExercise):
        rules.is_unilateral("罗马尼亚硬拉")
