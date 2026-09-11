"""Stage 3 S3-09：记录侧领域 schema 与负重比较键（换算键策略在领域层约定并测试）。

验收对照（stage3.md §5 S3-09）：

- ``record_type`` 对齐目录三类（拍板 A）；辅助／热身／RIR 可空语义（``None`` 不补造）。
- 不同负重口径不混比的存储字段齐备：原始十进制原文 + 单位 + 换算整数键；换算规则唯一实现
  （报告 §4.4：lb × 0.45359237 → ×1000 ROUND_HALF_EVEN），不引入隐藏容差或模糊合并。
- 加权键把负重口径绑进比较键，单只哑铃与双只总重的键结构上永不相等。

边界：本文件只测领域类型与纯规则，不写库、不建草稿（S3-10）；写入侧列约束见
test_stage3_record_migrations.py。所有用例只操作 ``tmp_path`` 下的临时文件库，不触碰真实用户库。
"""

from decimal import Decimal

import pytest

from domain.actions.rules import LOAD_CONVENTIONS, RECORD_TYPES
from domain.records.rules import (
    LB_TO_KG,
    MAX_LOAD_KG_KEY,
    InvalidRecordFact,
    comparison_key,
    load_kg_key,
    optional_rir,
)
from domain.records.schema import (
    ASSISTANCE_VALUES,
    LOAD_UNITS,
    SET_TYPES,
    Assistance,
    ExerciseLogFacts,
    LoadComparisonKey,
    RawLoad,
    SetFacts,
)

# ---------- 词汇：记录侧拼写与目录一致 ----------


def test_record_vocabulary_matches_catalog_and_decided_sets() -> None:
    assert RECORD_TYPES == ("reps_weight", "reps_bodyweight", "time")
    assert SET_TYPES == ("warmup", "work")
    assert ASSISTANCE_VALUES == ("none", "spotter_only", "assisted")
    assert LOAD_UNITS == ("kg", "lb")
    # 负重口径复用目录五种，不另立第二套（报告 §4.4、2026-09-11 拍板 A）
    assert len(LOAD_CONVENTIONS) == 5
    assert LoadComparisonKey.__dataclass_fields__["load_notation"].type is not None


# ---------- 可空语义：未明确保持为空，不补造默认值 ----------


def test_set_facts_defaults_are_unknown_not_assumed_values() -> None:
    facts = SetFacts(set_no=1, reps=5)
    assert facts.set_type is None  # 不是工作组
    assert facts.rir is None  # 不是 RIR 0
    assert facts.assistance is None  # 不是「无辅助」
    assert facts.load is None  # 自重／计时不虚构 0kg
    assert facts.assisted_reps is None
    assert facts.duration_seconds is None
    assert facts.quality_text is None


def test_nullable_fields_accept_every_decided_value() -> None:
    for set_type in SET_TYPES:
        assert SetFacts(set_no=1, set_type=set_type).set_type == set_type
    for assistance in ASSISTANCE_VALUES:
        typed: Assistance = assistance
        assert SetFacts(set_no=1, assistance=typed).assistance == typed
    # 热身摘要属动作事实，未提及时为空（不展开为虚构组数据）
    log = ExerciseLogFacts(exercise_id="barbell-back-squat", record_type="reps_weight")
    assert log.warmup_summary_text is None
    assert (
        ExerciseLogFacts(
            exercise_id="barbell-back-squat",
            record_type="reps_weight",
            load_notation="barbell_includes_bar_total",
            warmup_summary_text="递增至 60kg",
        ).warmup_summary_text
        == "递增至 60kg"
    )
    # 自重／计时型不得携带负重口径
    assert (
        ExerciseLogFacts(
            exercise_id="hanging-leg-raise", record_type="reps_bodyweight"
        ).load_notation
        is None
    )
    assert (
        ExerciseLogFacts(exercise_id="plank", record_type="time").load_notation is None
    )


def test_optional_rir_keeps_unknown_empty_and_rejects_invalid_values() -> None:
    assert optional_rir(None) is None  # 未报告不等于 0
    assert optional_rir(0) == 0.0
    assert optional_rir(2.5) == 2.5
    for invalid in (-1, float("nan"), float("inf"), float("-inf"), "2", True):
        with pytest.raises(InvalidRecordFact):
            optional_rir(invalid)  # type: ignore[arg-type]


# ---------- 换算键：报告 §4.4 已定稿尺度 ----------


def test_load_key_uses_decided_scale_and_rounds_half_even() -> None:
    assert Decimal("0.45359237") == LB_TO_KG
    assert load_kg_key(RawLoad("60", "kg")) == 60000
    assert load_kg_key(RawLoad("40", "kg")) == 40000
    assert load_kg_key(RawLoad("88", "lb")) == 39916
    assert load_kg_key(RawLoad("88.2", "lb")) == 40007
    # 等价归一：45.359237kg == 100lb（同一键，不额外引入容差）
    assert load_kg_key(RawLoad("45.359237", "kg")) == load_kg_key(RawLoad("100", "lb"))
    # HALF_EVEN 半值边界：.5 向偶数取整（非四舍五入）
    assert load_kg_key(RawLoad("0.0005", "kg")) == 0
    assert load_kg_key(RawLoad("0.0015", "kg")) == 2
    # 原始精度不限：多余小数与等值写法收敛到同一键
    assert load_kg_key(RawLoad("60.0", "kg")) == load_kg_key(RawLoad("60", "kg"))
    assert load_kg_key(RawLoad("060", "kg")) == 60000


def test_load_key_is_null_for_selfweight_and_timed_sets() -> None:
    assert load_kg_key(None) is None


def test_load_key_rejects_non_finite_negative_unknown_unit_and_overflow() -> None:
    for value_text in ("not-a-number", "", "NaN", "Infinity", "-Infinity", "-1", "  "):
        with pytest.raises(InvalidRecordFact):
            load_kg_key(RawLoad(value_text, "kg"))
    with pytest.raises(InvalidRecordFact):
        load_kg_key(RawLoad("60", "stone"))  # type: ignore[arg-type]
    with pytest.raises(InvalidRecordFact):
        load_kg_key(RawLoad(str(MAX_LOAD_KG_KEY), "kg"))  # kg×1000 越界
    # 上限本身可存（不越界即放行）
    assert load_kg_key(RawLoad(str(Decimal(MAX_LOAD_KG_KEY) / 1000), "kg")) == (
        MAX_LOAD_KG_KEY
    )


def test_load_key_rejects_oversized_text_as_invalid_fact() -> None:
    """超长原文按量级先拒（S3-09 证据残留 ⑤）：不泄露 decimal 上下文异常。

    ``quantize`` 先于范围检查时，默认 decimal 上下文的 28 位精度会抛
    ``decimal.InvalidOperation``；量级检查前置后，拒绝路径统一为 :class:`InvalidRecordFact`。
    """
    with pytest.raises(InvalidRecordFact):
        load_kg_key(RawLoad("1" + "0" * 30, "kg"))
    with pytest.raises(InvalidRecordFact):
        load_kg_key(RawLoad("1" + "0" * 30, "lb"))


# ---------- 比较键：口径进键，不同口径不混比 ----------


def test_comparison_key_is_deterministic_within_same_notation() -> None:
    first = comparison_key(
        exercise_id="barbell-bench-press",
        load_notation="barbell_includes_bar_total",
        load=RawLoad("60", "kg"),
    )
    second = comparison_key(
        exercise_id="barbell-bench-press",
        load_notation="barbell_includes_bar_total",
        load=RawLoad("60.0", "kg"),
    )
    third = comparison_key(
        exercise_id="barbell-bench-press",
        load_notation="barbell_includes_bar_total",
        load=RawLoad("132.277", "lb"),  # 60kg 的等价磅值
    )
    assert first == second == third
    assert first is not None and first.kg_key == 60000


def test_comparison_key_separates_load_notations_and_exercises() -> None:
    single_dumbbell = comparison_key(
        exercise_id="dumbbell-curl",
        load_notation="dumbbell_per_hand",
        load=RawLoad("20", "kg"),
    )
    both_dumbbells = comparison_key(
        exercise_id="dumbbell-curl",
        load_notation="plate_loaded_total_excluding_empty",
        load=RawLoad("20", "kg"),
    )
    other_exercise = comparison_key(
        exercise_id="barbell-bench-press",
        load_notation="dumbbell_per_hand",
        load=RawLoad("20", "kg"),
    )
    assert single_dumbbell is not None and both_dumbbells is not None
    # 同重量但口径不同：结构上不相等（不混比）
    assert single_dumbbell != both_dumbbells
    assert single_dumbbell.kg_key == both_dumbbells.kg_key == 20000
    assert single_dumbbell != other_exercise
    assert len({single_dumbbell, both_dumbbells, other_exercise}) == 3


def test_comparison_key_is_null_without_load_and_rejects_unknown_notation() -> None:
    assert (
        comparison_key(
            exercise_id="hanging-leg-raise",
            load_notation="barbell_includes_bar_total",
            load=None,
        )
        is None
    )
    with pytest.raises(InvalidRecordFact):
        comparison_key(
            exercise_id="barbell-bench-press",
            load_notation="barbell_total",  # 目录外拼写
            load=RawLoad("60", "kg"),
        )
