# 动作数据集领域层的纯测试：facet 中英归一（中文／英文／大小写／折叠长名）、越界值抛可修正错误、
# 规范码的等价原始取值集合、DatasetExercise 行映射与检索视图。依据：本轮拍板的"闭集 facet 预建中英
# 对照、以数据集字段为准、长名折叠为规范码别名"。全部为纯函数与纯数据，不触达 IO 与仓储。

import pytest

from app.domain.actions.dataset import (
    BODY_PART_VALUES,
    EQUIPMENT_VALUES,
    FACET_FIELDS,
    MUSCLE_GROUP_VALUES,
    TARGET_VALUES,
    DatasetExercise,
    UnknownFacetField,
    UnknownFacetValue,
    equivalent_values,
    facet_label,
    facet_options_zh,
    normalize_facet,
)


def _row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "0001",
        "name": "3/4 sit-up",
        "category": "waist",
        "body_part": "waist",
        "equipment": "body weight",
        "target": "abs",
        "muscle_group": "hip flexors",
        "secondary_muscles": ["hip flexors", "lower back"],
        "instructions": {"zh": "中文指导", "en": "English instruction"},
        "steps": {"zh": ["第一步", "第二步"], "en": ["Step one", "Step two"]},
    }
    base.update(overrides)
    return base


def test_normalize_facet_accepts_chinese_english_and_case() -> None:
    """中文、规范英文与英文大小写都归一到数据集规范英文。"""
    assert normalize_facet("body_part", "胸部") == "chest"
    assert normalize_facet("body_part", "chest") == "chest"
    assert normalize_facet("body_part", "  Chest  ") == "chest"
    assert normalize_facet("equipment", "杠铃") == "barbell"
    assert normalize_facet("equipment", "EZ 杆") == "ez barbell"
    assert normalize_facet("target", "背阔肌") == "lats"


def test_normalize_facet_folds_muscle_group_long_names_to_canonical_codes() -> None:
    """muscle_group 的规范码与长名都归一到短形；中文同样落到短形。"""
    assert normalize_facet("muscle_group", "lats") == "lats"
    assert normalize_facet("muscle_group", "latissimus dorsi") == "lats"
    assert normalize_facet("muscle_group", "trapezius") == "traps"
    assert normalize_facet("muscle_group", "背阔肌") == "lats"
    assert normalize_facet("muscle_group", "斜方肌") == "traps"


def test_equivalent_values_expands_folded_aliases() -> None:
    """规范码召回它的折叠长名；无别名的 facet 只召回自身。"""
    assert equivalent_values("muscle_group", "lats") == frozenset(
        {"lats", "latissimus dorsi"}
    )
    assert equivalent_values("muscle_group", "traps") == frozenset(
        {"traps", "trapezius"}
    )
    assert equivalent_values("body_part", "chest") == frozenset({"chest"})


def test_normalize_facet_rejects_unknown_field() -> None:
    """过滤字段越界即抛错，指明可过滤字段名单。"""
    with pytest.raises(UnknownFacetField) as exc:
        normalize_facet("movement_pattern", "深蹲")
    assert "body_part" in str(exc.value)


def test_normalize_facet_rejects_value_outside_the_vocabulary() -> None:
    """词表外的取值抛可修正错误，附带中文可选值，引导重发合法参数。"""
    with pytest.raises(UnknownFacetValue) as exc:
        normalize_facet("body_part", "不存在")
    message = str(exc.value)
    assert "body_part" in message
    assert "胸部" in message  # 中文可选值列举在错误信息里


@pytest.mark.parametrize("field", FACET_FIELDS)
def test_facet_options_zh_lists_distinct_sorted_labels(field: str) -> None:
    """每个 facet 的中文可选值去重且升序，与规范取值集合一一对应。"""
    options = facet_options_zh(field)
    assert list(options) == sorted(options)
    assert len(set(options)) == len(options)


def test_canonical_value_sets_match_dataset_sizes() -> None:
    """规范取值集合的数量与数据集观测一致：10／28／19／27。"""
    assert len(BODY_PART_VALUES) == 10
    assert len(EQUIPMENT_VALUES) == 28
    assert len(TARGET_VALUES) == 19
    assert len(MUSCLE_GROUP_VALUES) == 27


def test_dataset_exercise_maps_row_and_exposes_views() -> None:
    """行映射保留数据集字段，紧凑视图给出中英 facet，详情视图补齐二级肌群与双语要领。"""
    exercise = DatasetExercise.from_row(_row())

    assert exercise.id == "0001"
    assert exercise.secondary_muscles == ("hip flexors", "lower back")
    assert exercise.steps_zh == ("第一步", "第二步")

    compact = exercise.search_view()
    assert compact["name"] == "3/4 sit-up"
    assert compact["body_part"] == "waist"
    assert compact["body_part_zh"] == facet_label("body_part", "waist")
    assert "instructions" not in compact

    detail = exercise.detail_view()
    assert detail["instructions"] == {"zh": "中文指导", "en": "English instruction"}
    assert detail["steps"] == {
        "zh": ["第一步", "第二步"],
        "en": ["Step one", "Step two"],
    }
    assert detail["secondary_muscles"] == ["hip flexors", "lower back"]


def test_dataset_exercise_matches_english_name_case_insensitively() -> None:
    """文本匹配针对英文动作名做大小写无关子串命中。"""
    exercise = DatasetExercise.from_row(_row(name="Barbell Back Squat"))
    assert exercise.matches_text("back squat") is True
    assert exercise.matches_text("BARBELL") is True
    assert exercise.matches_text("deadlift") is False


def test_from_row_rejects_missing_fields() -> None:
    """缺字段直接抛错，不静默补默认（fast-fail）。"""
    row = _row()
    del row["body_part"]
    with pytest.raises(KeyError):
        DatasetExercise.from_row(row)
