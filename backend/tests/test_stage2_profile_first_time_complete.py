"""Stage 2 S2-01：首次建档完整性契约（stage2.md §4.3 已拍 1B、§8 已拍补充）。

验收对照（stage2.md §5 S2-01「完整性契约」）：

- 八项事实（目标、经验、频率、时长、器械、体重、动作限制、身体情况）全部要求明确回答；
  「未知」不算完整。2026-09-10 用户拍板 A：「明确无」只对器械、动作限制、身体情况三项
  有效，训练目标与训练经验必须是有效文本，其余数值字段必须是有效数值。
- 每类信息缺失／未知时拒绝首次确认；全部明确回答时可进入后续确认校验。
- 明确回答仍须符合对应字段的类型与领域校验，不能以「无」替代必需的有效数值。
- 完整性只表示信息齐备，不等于没有症状或已获训练安全许可：身体情况原文命中六类清单仍
  独立阻断，确认成功不自动解除报告或限制。
- Stage 1 的「部分事实可保存」能力不被当作生产确认规则，也不被本契约取消。

确认事务编排（app/confirm.py）与 HTTP 接线（api/routes_drafts.py）归 S2-05／S2-07；本模块
只覆盖完整性契约本身及其自动化。纯规则样例不访问数据库，持久化样例用临时文件库。
"""

from pathlib import Path

import pytest

from domain.actions.rules import modes_for
from domain.actions.schema import Exercise
from domain.profile import rules, schema
from domain.profile.safety import RED_FLAG_BLOCK_ADVICE, evaluate_safety
from domain.profile.schema import ActionRestriction, Fact, Profile
from domain.profile.service import ProfileService
from tests.support import open_database

# 已拍八项清单（stage2.md §4.3，2026-09-10 身体情况合并后九项变八项）：本文件硬编码，
# 字段清单漂移时大声失败。
DECIDED_FIRST_TIME_FIELDS = (
    "training_goal",
    "training_experience",
    "weekly_frequency",
    "session_duration_minutes",
    "available_equipment",
    "body_weight_kg",
    "action_restrictions",
    "body_conditions",
)

# 必需有效数值的字段：denied（明确无）不能替代有效数值（stage2.md §4.3）。
REQUIRED_VALUE_FIELDS = (
    "weekly_frequency",
    "session_duration_minutes",
    "body_weight_kg",
)

# 「明确无」可满足的字段（2026-09-10 用户拍板 A：仅集合／限制类事实）：同样硬编码，
# 避免常量被改空后逐字段用例静默消失。
EXPLICIT_NONE_FIELDS = (
    "available_equipment",
    "action_restrictions",
    "body_conditions",
)

# 必须给出有效文本的字段：2026-09-10 拍板 A，denied（明确无）不算回答。
REQUIRED_TEXT_FIELDS = (
    "training_goal",
    "training_experience",
)

_EXERCISE_IDS = {"杠铃背蹲": "barbell-back-squat"}


def _action(standard_name: str) -> Exercise:
    """构造候选动作样例：模式取自 S1-03 共享词表，不在此重复映射。"""
    return Exercise(
        id=_EXERCISE_IDS[standard_name],
        standard_name_zh=standard_name,
        equipment_variant="barbell",
        record_type="reps_weight",
        load_convention="barbell_includes_bar_total",
        unilateral=False,
        recommendable=False,
        active=True,
        aliases=(),
        modes=modes_for(standard_name),
        source_ref="test-fixture",
        attribution="test fixture",
        instructions_zh=None,
    )


def _answered_profile(**overrides: object) -> Profile:
    """八项均已明确回答的档案；overrides 用于把指定字段替换为待验证状态/取值。"""
    base: dict[str, object] = {
        "training_goal": Fact.known("增肌"),
        "training_experience": Fact.known("新手"),
        "weekly_frequency": Fact.known(3),
        "session_duration_minutes": Fact.known(60),
        "available_equipment": Fact.known(("杠铃",)),
        "body_weight_kg": Fact.known(72.5),
        "action_restrictions": Fact.known(()),
        "body_conditions": Fact.known(("肩部偶有不适",)),
    }
    base.update(overrides)
    return Profile(**base)  # type: ignore[arg-type]


async def _write_profile(db, profile: Profile) -> None:
    async with db.transaction() as conn:
        await ProfileService(db).write_profile_in_transaction(conn, profile)


# ---------- 契约内容：九项清单与「明确无」可满足范围 ----------


def test_first_time_contract_covers_the_nine_decided_facts() -> None:
    assert schema.FIRST_TIME_REQUIRED_FACT_FIELDS == DECIDED_FIRST_TIME_FIELDS
    # 清单覆盖全部已登记档案事实：没有事实字段被漏在明确回答要求之外
    assert set(schema.FIRST_TIME_REQUIRED_FACT_FIELDS) == set(schema.FACT_FIELDS)


def test_explicit_none_fields_are_exactly_the_collection_fields() -> None:
    numeric = {
        name
        for name, kind in schema.FACT_VALUE_KINDS.items()
        if kind in ("integer", "number")
    }
    assert numeric == set(REQUIRED_VALUE_FIELDS)
    assert schema.EXPLICIT_NONE_FACT_FIELDS == EXPLICIT_NONE_FIELDS
    # 拍板 A：只有集合／限制类字段允许明确无；文本与数值字段都必须给有效值
    assert set(schema.EXPLICIT_NONE_FACT_FIELDS) == (
        set(schema.FIRST_TIME_REQUIRED_FACT_FIELDS)
        - numeric
        - set(REQUIRED_TEXT_FIELDS)
    )
    assert set(schema.EXPLICIT_NONE_FACT_FIELDS).isdisjoint(REQUIRED_TEXT_FIELDS)


def test_all_nine_explicit_answers_pass_the_first_time_gate() -> None:
    profile = _answered_profile()
    assert profile.first_time_missing_fields == ()
    assert profile.is_first_time_complete is True
    assert rules.missing_first_time_fields(profile) == ()
    assert rules.ensure_first_time_complete(profile) is profile


# ---------- 每类信息缺失／未知即拒绝首次确认 ----------


@pytest.mark.parametrize("field", DECIDED_FIRST_TIME_FIELDS)
def test_unknown_field_blocks_first_time_confirmation(field: str) -> None:
    profile = _answered_profile(**{field: Fact.unknown()})
    assert profile.first_time_missing_fields == (field,)
    assert profile.is_first_time_complete is False
    with pytest.raises(rules.IncompleteProfile, match=field):
        rules.ensure_first_time_complete(profile)


def test_unanswered_fields_are_not_filled_with_defaults() -> None:
    # 只收集了体重与目标：其余七项保持未知，不被补造成默认值，也不构成首次确认条件
    profile = Profile(training_goal=Fact.known("增肌"), body_weight_kg=Fact.known(70.0))
    expected = tuple(
        name
        for name in DECIDED_FIRST_TIME_FIELDS
        if name not in ("training_goal", "body_weight_kg")
    )
    assert profile.first_time_missing_fields == expected
    for name in expected:
        assert getattr(profile, name).is_unknown, name
        assert getattr(profile, name).value is None, name
    with pytest.raises(rules.IncompleteProfile):
        rules.ensure_first_time_complete(profile)


def test_empty_profile_reports_every_field_missing() -> None:
    assert Profile.empty().first_time_missing_fields == DECIDED_FIRST_TIME_FIELDS
    with pytest.raises(rules.IncompleteProfile):
        rules.ensure_first_time_complete(Profile.empty())


# ---------- 「明确无」与「未知」的区别 ----------


@pytest.mark.parametrize("field", EXPLICIT_NONE_FIELDS)
def test_explicit_none_is_distinct_from_unknown(field: str) -> None:
    assert field in schema.EXPLICIT_NONE_FACT_FIELDS
    denied = _answered_profile(**{field: Fact.denied()})
    unknown = _answered_profile(**{field: Fact.unknown()})
    assert denied.first_time_missing_fields == ()
    assert rules.ensure_first_time_complete(denied) is denied
    assert getattr(denied, field).value is None
    assert unknown.first_time_missing_fields == (field,)
    with pytest.raises(rules.IncompleteProfile, match=field):
        rules.ensure_first_time_complete(unknown)


@pytest.mark.parametrize("field", REQUIRED_VALUE_FIELDS)
def test_explicit_none_cannot_replace_a_required_numeric_value(field: str) -> None:
    profile = _answered_profile(**{field: Fact.denied()})
    assert profile.first_time_missing_fields == (field,)
    with pytest.raises(rules.IncompleteProfile, match=field):
        rules.ensure_first_time_complete(profile)


@pytest.mark.parametrize("field", REQUIRED_TEXT_FIELDS)
def test_explicit_none_cannot_replace_a_required_text_value(field: str) -> None:
    # 拍板 A（2026-09-10）：训练目标与训练经验必须是有效 known 文本，「无」不算回答
    assert field not in schema.EXPLICIT_NONE_FACT_FIELDS
    profile = _answered_profile(**{field: Fact.denied()})
    assert profile.first_time_missing_fields == (field,)
    with pytest.raises(rules.IncompleteProfile, match=field):
        rules.ensure_first_time_complete(profile)


@pytest.mark.parametrize(
    "field",
    ["available_equipment", "action_restrictions", "body_conditions"],
)
def test_known_empty_collection_is_an_explicit_answer(field: str) -> None:
    assert field in schema.EXPLICIT_NONE_FACT_FIELDS
    # 显式空集合与前端契约的 equipment: []／body_conditions: [] 同义，都是明确回答
    profile = _answered_profile(**{field: Fact.known(())})
    assert profile.first_time_missing_fields == ()
    assert rules.ensure_first_time_complete(profile) is profile


# ---------- 明确回答仍须通过字段类型与领域校验 ----------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("weekly_frequency", "3"),
        ("session_duration_minutes", "60"),
        ("body_weight_kg", "七十"),
        ("training_goal", "   "),
        ("available_equipment", ["杠铃"]),
        (
            "action_restrictions",
            (ActionRestriction("movement_pattern", "不存在的模式"),),
        ),
    ],
)
def test_explicit_answers_still_need_valid_type(field: str, value: object) -> None:
    profile = _answered_profile(**{field: Fact.known(value)})  # type: ignore[arg-type]
    with pytest.raises(rules.InvalidProfile):
        rules.ensure_first_time_complete(profile)
    with pytest.raises(rules.InvalidProfile):
        rules.missing_first_time_fields(profile)


# ---------- 与 Stage 1「部分事实可保存」能力并存 ----------


def test_stage1_partial_profile_stays_saveable_but_is_not_confirmable() -> None:
    partial = Profile(training_goal=Fact.known("增肌"), body_weight_kg=Fact.known(70.0))
    # Stage 1 口径不变：唯一必填只有体重，部分事实仍可保存
    assert schema.REQUIRED_FACT_FIELDS == ("body_weight_kg",)
    assert partial.is_complete is True
    assert rules.ensure_complete_profile(partial) is partial
    # 但首次确认入口按九项清单拒绝：部分事实可保存不等于可正式确认
    assert partial.is_first_time_complete is False
    with pytest.raises(rules.IncompleteProfile):
        rules.ensure_first_time_complete(partial)


async def test_persisted_partial_profile_still_fails_the_first_time_gate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    async with open_database(path) as db:
        await _write_profile(
            db,
            Profile(training_goal=Fact.known("增肌"), body_weight_kg=Fact.known(70.0)),
        )
        snapshot = await ProfileService(db).read_formal_profile()
        assert snapshot.profile is not None
        assert snapshot.context_version == 0  # 完整性判定不写库、不推进版本
        with pytest.raises(rules.IncompleteProfile):
            rules.ensure_first_time_complete(snapshot.profile)
    async with open_database(path) as db:
        snapshot = await ProfileService(db).read_formal_profile()
        assert snapshot.profile is not None
        assert snapshot.context_version == 0


async def test_unbuilt_profile_is_not_confirmable(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        snapshot = await ProfileService(db).read_formal_profile()
        assert (snapshot.profile, snapshot.context_version) == (None, 0)
        base = snapshot.profile if snapshot.profile is not None else Profile.empty()
        # 未建档不得被当成「无限制、无症状」：全未知档案同样不满足首次确认
        assert base.first_time_missing_fields == DECIDED_FIRST_TIME_FIELDS
        with pytest.raises(rules.IncompleteProfile):
            rules.ensure_first_time_complete(base)


# ---------- 完整性不等于安全许可 ----------


def test_reported_red_flag_counts_as_answered_but_still_blocks() -> None:
    profile = _answered_profile(body_conditions=Fact.known(("晕厥",)))
    assert profile.first_time_missing_fields == ()
    assert (
        rules.ensure_first_time_complete(profile) is profile
    )  # 已报告症状不因非空被当作缺失
    result = evaluate_safety(profile, ())
    assert result.red_flags.is_blocked is True
    assert result.advice == (RED_FLAG_BLOCK_ADVICE,)


def test_unlisted_symptom_is_answered_but_not_treated_as_safe() -> None:
    profile = _answered_profile(body_conditions=Fact.known(("最近训练有点头晕",)))
    assert rules.ensure_first_time_complete(profile) is profile
    result = evaluate_safety(profile, ())
    assert result.red_flags.is_blocked is False
    assert result.needs_clarification is True


def test_complete_profile_keeps_restriction_blocking() -> None:
    profile = _answered_profile(
        action_restrictions=Fact.known((ActionRestriction("movement_pattern", "深蹲"),))
    )
    assert rules.ensure_first_time_complete(profile) is profile
    result = evaluate_safety(profile, (_action("杠铃背蹲"),))
    assert result.restrictions.is_blocked is True
    assert result.is_blocked is True
