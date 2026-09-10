"""Stage 1 S1-05：动作限制与红旗安全校验（验收 1–6）。

验收对照（stage1.md §5 S1-05）：

1. 具体动作限制命中该动作；模式限制按集合交集覆盖多归属动作，未命中的动作不被错报；
   限制未命中不等于完整训练安全许可。
2. 6 类已明确红旗各有结构化样例，任一出现即阻断常规处方并附线下专业评估提示，不输出
   疾病诊断；清单外症状只返回未知/需澄清。
3. 长期、拟议、当次任一来源有红旗时，另一来源的「无红旗」不能覆盖；限制检查通过也不能
   消除红旗。
4. 校验不写档案、不新增/删除永久限制、不自行解除红旗，也不提供解除能力。
5. 阻断针对处方/指导；本模块不含记录写入，也不带历史阻断语义。
6. 用拟议补丁后的条件校验候选动作，能发现只看旧档案会漏掉的冲突。

候选动作经 ``domain.actions.rules.modes_for`` 取共享词表，不另造模式映射；断言中的标准名
与目录身份对应已拍 24 项清单。
"""

from dataclasses import replace
from pathlib import Path

import pytest

from domain.actions.rules import InvalidMode, modes_for
from domain.actions.schema import Exercise
from domain.profile import safety
from domain.profile.rules import InvalidRestriction, UnknownExerciseReference
from domain.profile.schema import (
    RED_FLAG_KINDS,
    ActionRestriction,
    Fact,
    Profile,
    ProfilePatch,
    SessionConditions,
)
from domain.profile.service import ProfileService
from tests.support import open_database

# 已拍清单标准名 → 目录稳定身份（与 003 种子一致；用于纯规则样例与临时库接线样例）。
_EXERCISE_IDS: dict[str, str] = {
    "杠铃背蹲": "barbell-back-squat",
    "45°腿举": "leg-press-45",
    "保加利亚分腿蹲": "bulgarian-split-squat",
    "哑铃上斜卧推": "dumbbell-incline-bench-press",
    "坐姿哑铃肩推": "seated-dumbbell-shoulder-press",
    "自重双杠臂屈伸": "parallel-bar-dip",
    "腿屈伸": "leg-extension",
    "高位下拉": "lat-pulldown",
    "悬垂举腿": "hanging-leg-raise",
}

SPECIFIC_SQUAT = ActionRestriction("specific_action", "barbell-back-squat")
BY_SQUAT = ActionRestriction("movement_pattern", "深蹲")
BY_KNEE_EXTENSION = ActionRestriction("movement_pattern", "膝伸")


def _action(standard_name: str) -> Exercise:
    """构造目录身份样例：模式取自 S1-03 共享词表，不在此重复映射。"""
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


async def _write_profile(db, profile: Profile) -> None:
    async with db.transaction() as conn:
        await ProfileService(db).write_profile_in_transaction(conn, profile)


async def _profile_row(db) -> tuple[str | None, int]:
    async def op(conn):
        async with conn.execute(
            "SELECT profile_json, context_version FROM user_profile WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return (row["profile_json"], row["context_version"])

    return await db.under_lock(op)


# ---------- 验收 1：两类限制命中口径 ----------


@pytest.mark.parametrize("standard_name", ["杠铃背蹲", "45°腿举", "保加利亚分腿蹲"])
def test_pattern_restriction_hits_every_action_sharing_the_mode(
    standard_name: str,
) -> None:
    hits = safety.check_action_restrictions((BY_SQUAT,), (_action(standard_name),))
    assert [hit.standard_name for hit in hits] == [standard_name]
    assert hits[0].restriction == BY_SQUAT
    assert hits[0].exercise_id == _EXERCISE_IDS[standard_name]
    assert hits[0].matched_modes == ("深蹲",)


def test_specific_action_restriction_hits_that_identity_only() -> None:
    hits = safety.check_action_restrictions(
        (SPECIFIC_SQUAT,), (_action("杠铃背蹲"), _action("45°腿举"))
    )
    assert [hit.standard_name for hit in hits] == ["杠铃背蹲"]
    assert hits[0].matched_modes == ()


@pytest.mark.parametrize(
    ("standard_name", "pattern"),
    [
        ("哑铃上斜卧推", "水平推"),
        ("哑铃上斜卧推", "垂直推"),
        ("自重双杠臂屈伸", "垂直推"),
        ("自重双杠臂屈伸", "肘伸"),
    ],
)
def test_multi_mode_actions_hit_each_belonging_pattern(
    standard_name: str, pattern: str
) -> None:
    restriction = ActionRestriction("movement_pattern", pattern)
    hits = safety.check_action_restrictions((restriction,), (_action(standard_name),))
    assert [hit.matched_modes for hit in hits] == [(pattern,)]


@pytest.mark.parametrize(
    ("standard_name", "pattern"),
    [
        ("哑铃上斜卧推", "肘伸"),
        ("自重双杠臂屈伸", "水平推"),
        ("坐姿哑铃肩推", "水平推"),
        ("悬垂举腿", "深蹲"),
        ("高位下拉", "膝伸"),
    ],
)
def test_non_matching_action_is_not_reported(standard_name: str, pattern: str) -> None:
    restriction = ActionRestriction("movement_pattern", pattern)
    assert (
        safety.check_action_restrictions((restriction,), (_action(standard_name),))
        == ()
    )


def test_multiple_restrictions_report_only_covered_actions() -> None:
    restrictions = (
        SPECIFIC_SQUAT,
        BY_SQUAT,
        ActionRestriction("movement_pattern", "垂直拉"),
    )
    actions = (
        _action("杠铃背蹲"),
        _action("45°腿举"),
        _action("高位下拉"),
        _action("悬垂举腿"),
    )
    hits = safety.check_action_restrictions(restrictions, actions)
    assert [
        (hit.standard_name, hit.restriction.scope, hit.restriction.target)
        for hit in hits
    ] == [
        ("杠铃背蹲", "specific_action", "barbell-back-squat"),
        ("杠铃背蹲", "movement_pattern", "深蹲"),
        ("45°腿举", "movement_pattern", "深蹲"),
        ("高位下拉", "movement_pattern", "垂直拉"),
    ]


def test_restriction_clearance_is_not_full_safety_clearance() -> None:
    """限制未命中不等于完整安全许可：结果没有安全放行字段，未收集红旗仍要澄清。"""
    result = safety.evaluate_safety(Profile.empty(), (_action("高位下拉"),))
    assert result.restrictions.hits == ()
    assert not hasattr(result, "is_safe")
    assert not hasattr(result.restrictions, "is_safe")
    assert result.needs_clarification
    assert result.red_flags.unknown_sources == ("formal_profile",)


def test_unknown_restrictions_are_not_read_as_no_restrictions() -> None:
    result = safety.evaluate_safety(Profile.empty(), (_action("杠铃背蹲"),))
    assert result.restrictions.state == "unknown"
    assert result.restrictions.is_unknown
    assert safety.UNKNOWN_RESTRICTION_REASON in result.clarification_reasons


def test_denied_restrictions_are_distinct_from_unknown() -> None:
    profile = Profile(action_restrictions=Fact.denied())
    result = safety.evaluate_safety(profile, (_action("杠铃背蹲"),))
    assert result.restrictions.state == "denied"
    assert not result.restrictions.is_unknown
    assert safety.UNKNOWN_RESTRICTION_REASON not in result.clarification_reasons


def test_patch_touching_restrictions_does_not_upgrade_unknown_to_known() -> None:
    """P1 回归：正式限制未收集时，触及限制的补丁不得升级为「已知无限制」。"""
    profile = Profile(body_conditions=Fact.denied())
    patch = ProfilePatch(add_restrictions=(BY_KNEE_EXTENSION,))
    result = safety.evaluate_safety(profile, (_action("高位下拉"),), patch=patch)
    assert result.restrictions.state == "unknown"
    assert result.restrictions.is_unknown
    assert result.restrictions.hits == ()
    assert result.needs_clarification
    assert safety.UNKNOWN_RESTRICTION_REASON in result.clarification_reasons


def test_unknown_formal_restrictions_still_hit_under_post_patch_conditions() -> None:
    """P1 回归：正式未收集不得当 denied 放行，命中集仍按补丁后条件计算。"""
    profile = Profile(body_conditions=Fact.denied())
    patch = ProfilePatch(add_restrictions=(BY_KNEE_EXTENSION,))
    result = safety.evaluate_safety(profile, (_action("腿屈伸"),), patch=patch)
    assert result.restrictions.state == "unknown"
    assert [hit.standard_name for hit in result.restrictions.hits] == ["腿屈伸"]
    assert result.restrictions.is_blocked
    assert safety.UNKNOWN_RESTRICTION_REASON in result.clarification_reasons


def test_unrelated_patch_keeps_unknown_restrictions_unknown() -> None:
    patch = ProfilePatch(facts={"body_conditions": Fact.denied()})
    result = safety.evaluate_safety(
        Profile.empty(), (_action("杠铃背蹲"),), patch=patch
    )
    assert result.restrictions.state == "unknown"
    assert result.needs_clarification


def test_invalid_pattern_restriction_is_rejected_not_silently_ignored() -> None:
    restriction = ActionRestriction("movement_pattern", "膝关节")
    with pytest.raises(InvalidRestriction):
        safety.check_action_restrictions((restriction,), (_action("杠铃背蹲"),))


def test_candidate_modes_must_be_in_shared_vocabulary() -> None:
    bad = replace(_action("杠铃背蹲"), modes=("自创模式",))
    with pytest.raises(InvalidMode):
        safety.check_action_restrictions((BY_SQUAT,), (bad,))


# ---------- 验收 2：红旗阻断与清单外症状 ----------


@pytest.mark.parametrize("kind", RED_FLAG_KINDS)
def test_each_listed_red_flag_blocks_prescription_with_offline_evaluation(
    kind: str,
) -> None:
    profile = Profile(body_conditions=Fact.known((kind,)))
    result = safety.evaluate_safety(profile, ())
    assert result.red_flags.confirmed == (
        safety.RedFlagFinding("formal_profile", kind),
    )
    assert result.is_blocked
    assert result.advice == (safety.RED_FLAG_BLOCK_ADVICE,)
    assert "线下专业评估" in result.advice[0]


def test_red_flag_advice_is_fixed_text_without_disease_diagnosis() -> None:
    assert safety.RED_FLAG_BLOCK_ADVICE == (
        "存在已明确红旗症状：不生成常规训练处方，建议线下专业评估。"
    )
    for word in ("诊断", "疾病", "可能为"):
        assert word not in safety.RED_FLAG_BLOCK_ADVICE


def test_unlisted_symptom_only_returns_clarification() -> None:
    profile = Profile(body_conditions=Fact.known(("心悸",)))
    result = safety.evaluate_safety(profile, ())
    assert result.red_flags.confirmed == ()
    assert result.red_flags.unlisted == (
        safety.RedFlagFinding("formal_profile", "心悸"),
    )
    assert result.red_flags.needs_clarification
    assert result.needs_clarification
    assert any(
        safety.UNLISTED_RED_FLAG_REASON in reason
        for reason in result.clarification_reasons
    )
    assert result.advice == ()


def test_listed_and_unlisted_reports_are_kept_apart() -> None:
    profile = Profile(body_conditions=Fact.known(("晕厥", "心悸")))
    result = safety.evaluate_safety(profile, ())
    assert [finding.label for finding in result.red_flags.confirmed] == ["晕厥"]
    assert [finding.label for finding in result.red_flags.unlisted] == ["心悸"]
    assert result.is_blocked


def test_denied_red_flags_need_no_clarification() -> None:
    profile = Profile(body_conditions=Fact.denied())
    result = safety.evaluate_safety(profile, ())
    assert result.red_flags.confirmed == ()
    assert result.red_flags.unknown_sources == ()
    assert not result.red_flags.needs_clarification


def test_plain_body_condition_text_does_not_block_red_flags() -> None:
    """普通身体情况非空但未命中六类：不阻断红旗，也不判为安全（进澄清）。"""
    profile = Profile(body_conditions=Fact.known(("肩部偶有酸胀感",)))
    result = safety.evaluate_safety(profile, ())
    assert result.red_flags.confirmed == ()
    assert result.red_flags.unknown_sources == ()
    assert not result.is_blocked
    assert result.needs_clarification
    assert [finding.label for finding in result.red_flags.unlisted] == [
        "肩部偶有酸胀感"
    ]


def test_listed_kind_inside_a_raw_report_is_classified_at_read_time() -> None:
    """分类在读取时执行：原文包含清单原词即命中，命中标签取清单原词。"""
    profile = Profile(body_conditions=Fact.known(("深蹲时膝盖锐痛",)))
    result = safety.evaluate_safety(profile, ())
    assert result.red_flags.confirmed == (
        safety.RedFlagFinding("formal_profile", "锐痛"),
    )
    assert result.red_flags.unlisted == ()
    assert result.is_blocked
    # 分类不写档案：输入档案原样，原文不被分类结果替换
    assert profile.body_conditions == Fact.known(("深蹲时膝盖锐痛",))


# ---------- 验收 3：来源独立，无红旗不能覆盖有红旗 ----------


@pytest.mark.parametrize(
    ("formal", "session"),
    [
        (Fact.known(("晕厥",)), Fact.denied()),
        (Fact.denied(), Fact.known(("晕厥",))),
        (Fact.known(("晕厥",)), Fact.known(())),
    ],
)
def test_other_source_without_red_flag_cannot_override(
    formal: Fact[tuple[str, ...]], session: Fact[tuple[str, ...]]
) -> None:
    result = safety.evaluate_safety(
        Profile(body_conditions=formal),
        (),
        session=SessionConditions(body_conditions=session),
    )
    assert result.is_blocked
    assert "晕厥" in [finding.label for finding in result.red_flags.confirmed]


def test_proposed_patch_denial_cannot_clear_formal_red_flag() -> None:
    profile = Profile(body_conditions=Fact.known(("锐痛",)))
    patch = ProfilePatch(facts={"body_conditions": Fact.denied()})
    result = safety.evaluate_safety(profile, (), patch=patch)
    assert result.red_flags.confirmed == (
        safety.RedFlagFinding("formal_profile", "锐痛"),
    )
    assert result.is_blocked


def test_proposed_patch_red_flag_blocks_even_when_formal_denies() -> None:
    profile = Profile(body_conditions=Fact.denied())
    patch = ProfilePatch(facts={"body_conditions": Fact.known(("异常气短",))})
    result = safety.evaluate_safety(profile, (), patch=patch)
    assert result.red_flags.confirmed == (
        safety.RedFlagFinding("proposed_patch", "异常气短"),
    )
    assert result.is_blocked


def test_passing_restriction_check_does_not_clear_red_flag() -> None:
    profile = Profile(
        action_restrictions=Fact.known((BY_SQUAT,)),
        body_conditions=Fact.known(("麻木",)),
    )
    result = safety.evaluate_safety(profile, (_action("高位下拉"),))
    assert result.restrictions.hits == ()
    assert result.red_flags.is_blocked
    assert result.is_blocked


def test_unknown_session_red_flags_need_clarification() -> None:
    profile = Profile(body_conditions=Fact.denied())
    session = SessionConditions(body_conditions=Fact.unknown())
    result = safety.evaluate_safety(profile, (), session=session)
    assert result.red_flags.unknown_sources == ("session_conditions",)
    assert result.red_flags.needs_clarification
    assert any(
        "session_conditions" in reason for reason in result.clarification_reasons
    )


def test_each_source_reports_its_own_red_flag() -> None:
    profile = Profile(body_conditions=Fact.known(("晕厥",)))
    patch = ProfilePatch(facts={"body_conditions": Fact.known(("锐痛",))})
    session = SessionConditions(body_conditions=Fact.known(("放射痛",)))
    result = safety.evaluate_safety(profile, (), patch=patch, session=session)
    assert [
        (finding.source, finding.label) for finding in result.red_flags.confirmed
    ] == [
        ("formal_profile", "晕厥"),
        ("proposed_patch", "锐痛"),
        ("session_conditions", "放射痛"),
    ]


# ---------- 验收 4：只读、不增删永久限制、不解除红旗 ----------


def test_evaluation_does_not_mutate_inputs() -> None:
    profile = Profile(
        action_restrictions=Fact.known((BY_SQUAT,)),
        body_conditions=Fact.known(("晕厥",)),
    )
    patch = ProfilePatch(
        add_restrictions=(BY_KNEE_EXTENSION,), facts={"body_conditions": Fact.denied()}
    )
    session = SessionConditions(body_conditions=Fact.known(("麻木",)))
    actions = (_action("腿屈伸"),)
    before = (profile, patch, session, actions)
    safety.evaluate_safety(profile, actions, patch=patch, session=session)
    assert (profile, patch, session, actions) == before
    assert profile.action_restrictions == Fact.known((BY_SQUAT,))
    assert profile.body_conditions == Fact.known(("晕厥",))


def test_safety_module_has_no_write_or_red_flag_clearing_capability() -> None:
    source = Path(safety.__file__).read_text(encoding="utf-8")
    for forbidden in ("aiosqlite", "storage", "INSERT", "UPDATE", "DELETE", "commit"):
        assert forbidden not in source, forbidden
    names = [name for name in vars(safety) if not name.startswith("__")]
    assert not [name for name in names if "clear" in name.lower()]
    assert not [name for name in names if "解除" in name]


async def test_service_check_does_not_write_profile_or_version(tmp_path: Path) -> None:
    async with open_database(tmp_path / "safety.db") as db:
        await _write_profile(
            db,
            Profile(
                body_weight_kg=Fact.known(70.0),
                action_restrictions=Fact.known((BY_SQUAT,)),
                body_conditions=Fact.denied(),
            ),
        )
        before = await _profile_row(db)
        result = await ProfileService(db).check_candidate_actions_safety(
            ["leg-press-45", "lat-pulldown"],
            patch=ProfilePatch(add_restrictions=(BY_KNEE_EXTENSION,)),
        )
        after = await _profile_row(db)
        assert before == after
        assert [hit.standard_name for hit in result.restrictions.hits] == ["45°腿举"]


# ---------- 验收 5：阻断处方，不阻断历史读取/已发生事实 ----------


def test_result_exposes_only_prescription_blocking_outputs() -> None:
    assert tuple(safety.SafetyCheckResult.__dataclass_fields__) == (
        "restrictions",
        "red_flags",
    )
    assert tuple(safety.RestrictionCheck.__dataclass_fields__) == ("state", "hits")
    assert tuple(safety.RedFlagCheck.__dataclass_fields__) == (
        "confirmed",
        "unlisted",
        "unknown_sources",
    )


def test_advice_scope_is_prescription_not_history() -> None:
    assert "不生成常规训练处方" in safety.RED_FLAG_BLOCK_ADVICE
    assert "禁止" not in safety.RED_FLAG_BLOCK_ADVICE


def test_safety_module_contains_no_record_write_flow() -> None:
    source = Path(safety.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "training_record",
        "record_write",
        "insert_record",
        "log_workout",
    ):
        assert forbidden not in source, forbidden


# ---------- 验收 6：按拟议补丁后的条件校验 ----------


def test_post_patch_conditions_reveal_conflict_missed_by_old_profile() -> None:
    profile = Profile(action_restrictions=Fact.denied())
    patch = ProfilePatch(add_restrictions=(BY_KNEE_EXTENSION,))
    candidates = (_action("腿屈伸"),)
    old_conditions = safety.evaluate_safety(profile, candidates)
    proposed_conditions = safety.evaluate_safety(profile, candidates, patch=patch)
    assert old_conditions.restrictions.hits == ()
    assert [hit.standard_name for hit in proposed_conditions.restrictions.hits] == [
        "腿屈伸"
    ]
    assert proposed_conditions.restrictions.state == "known"


def test_post_patch_specific_action_restriction_hits() -> None:
    patch = ProfilePatch(
        add_restrictions=(ActionRestriction("specific_action", "leg-extension"),)
    )
    result = safety.evaluate_safety(Profile.empty(), (_action("腿屈伸"),), patch=patch)
    assert [hit.restriction.scope for hit in result.restrictions.hits] == [
        "specific_action"
    ]


def test_post_patch_removal_is_only_proposed_conditions() -> None:
    profile = Profile(action_restrictions=Fact.known((BY_SQUAT,)))
    patch = ProfilePatch(remove_restrictions=(BY_SQUAT,))
    result = safety.evaluate_safety(profile, (_action("杠铃背蹲"),), patch=patch)
    assert result.restrictions.hits == ()
    # 正式限制未被删除：删除只是拟议条件，生效归 Stage 2 确认事务。
    assert profile.action_restrictions == Fact.known((BY_SQUAT,))


async def test_seeded_multi_mode_actions_hit_pattern_restriction(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "seed.db") as db:
        await _write_profile(
            db,
            Profile(
                body_weight_kg=Fact.known(70.0),
                action_restrictions=Fact.known(
                    (ActionRestriction("movement_pattern", "垂直推"),)
                ),
            ),
        )
        result = await ProfileService(db).check_candidate_actions_safety(
            [
                "dumbbell-incline-bench-press",  # 水平推 + 垂直推
                "parallel-bar-dip",  # 垂直推 + 肘伸
                "leg-extension",  # 膝伸：未命中
            ]
        )
        assert [
            (hit.standard_name, hit.matched_modes) for hit in result.restrictions.hits
        ] == [
            ("哑铃上斜卧推", ("垂直推",)),
            ("自重双杠臂屈伸", ("垂直推",)),
        ]


async def test_service_rejects_unknown_candidate_identity(tmp_path: Path) -> None:
    async with open_database(tmp_path / "unknown.db") as db:
        with pytest.raises(UnknownExerciseReference):
            await ProfileService(db).check_candidate_actions_safety(
                ["no-such-exercise"]
            )


async def test_unbuilt_profile_is_unknown_not_safe(tmp_path: Path) -> None:
    async with open_database(tmp_path / "empty.db") as db:
        result = await ProfileService(db).check_candidate_actions_safety(
            ["barbell-back-squat"]
        )
        assert result.restrictions.state == "unknown"
        assert result.red_flags.unknown_sources == ("formal_profile",)
        assert result.needs_clarification


async def test_service_session_red_flag_blocks_without_writing(tmp_path: Path) -> None:
    async with open_database(tmp_path / "session.db") as db:
        await _write_profile(
            db,
            Profile(body_weight_kg=Fact.known(70.0), body_conditions=Fact.denied()),
        )
        before = await _profile_row(db)
        result = await ProfileService(db).check_candidate_actions_safety(
            ["barbell-back-squat"],
            session=SessionConditions(body_conditions=Fact.known(("晕厥",))),
        )
        assert result.is_blocked
        assert result.advice == (safety.RED_FLAG_BLOCK_ADVICE,)
        assert await _profile_row(db) == before
