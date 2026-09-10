"""Stage 1 S1-04：拟议补丁隔离与当次条件分流（验收 2、3）。

验收对照（stage1.md §5 S1-04 验收 2–3）：计算「以后只能用哑铃」的拟议条件后，正式档案
和版本保持原样；错误补丁不部分修改输入对象或数据库；「今天只能用哑铃」的结构化当次条件
不进入长期补丁。本阶段只处理已被上游分流的结构化输入，不解析自然语言意图。
"""

from pathlib import Path

import pytest

from domain.profile import rules
from domain.profile.schema import (
    ActionRestriction,
    Fact,
    Profile,
    ProfilePatch,
    SessionConditions,
    profile_to_json,
)
from domain.profile.service import ProfileService
from tests.support import open_database

# 「以后只能用哑铃」：长期器械变更，保存在拟议补丁中，确认前不改正式档案。
LONG_TERM_DUMBBELL_PATCH = ProfilePatch(
    facts={"available_equipment": Fact.known(("哑铃",))}
)
# 「今天只能用哑铃」：当次训练条件，只对当次有效（02 2.4）。
TODAY_DUMBBELL_ONLY = SessionConditions(available_equipment=Fact.known(("哑铃",)))


def _seeded_profile() -> Profile:
    return Profile(
        training_goal=Fact.known("增肌"),
        body_weight_kg=Fact.known(70.0),
    )


async def _write_profile(db, service: ProfileService, profile: Profile) -> None:
    async with db.transaction() as conn:
        await service.write_profile_in_transaction(conn, profile)


async def _profile_row(db) -> tuple[str | None, int]:
    async def op(conn):
        async with conn.execute(
            "SELECT profile_json, context_version FROM user_profile WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return (row["profile_json"], row["context_version"])

    return await db.under_lock(op)


# ---------- 验收 2：拟议条件不改正式档案与版本 ----------


async def test_proposed_patch_preview_keeps_formal_profile_and_version(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service = ProfileService(db)
        await _write_profile(db, service, _seeded_profile())
        before = await _profile_row(db)

        proposed = await service.preview_patch(LONG_TERM_DUMBBELL_PATCH)

        assert proposed.available_equipment == Fact.known(("哑铃",))
        assert proposed.body_weight_kg == Fact.known(70.0)
        # 正式档案与版本保持原样（逐字节比较库内值）
        assert await _profile_row(db) == before
        snapshot = await service.read_formal_profile()
        assert snapshot.profile is not None
        assert snapshot.profile.available_equipment.is_unknown
        assert snapshot.context_version == 0


async def test_preview_without_formal_profile_returns_proposal_only(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service = ProfileService(db)
        proposed = await service.preview_patch(LONG_TERM_DUMBBELL_PATCH)
        assert proposed.available_equipment == Fact.known(("哑铃",))
        assert await _profile_row(db) == (None, 0)


async def test_apply_patch_does_not_modify_input_objects() -> None:
    profile = Profile(
        training_goal=Fact.known("增肌"),
        action_restrictions=Fact.known(
            (ActionRestriction("specific_action", "barbell-back-squat"),)
        ),
    )
    profile = Profile(
        training_goal=Fact.known("增肌"),
        action_restrictions=Fact.known(
            (ActionRestriction("specific_action", "barbell-back-squat"),)
        ),
        body_conditions=Fact.known(("肩部偶有不适",)),
    )
    patch = ProfilePatch(
        facts={"body_conditions": Fact.known(("深蹲时膝盖锐痛",))},
        add_restrictions=(ActionRestriction("movement_pattern", "深蹲"),),
    )
    facts_snapshot = dict(patch.facts)

    proposed = rules.apply_patch(profile, patch)

    assert profile.training_goal == Fact.known("增肌")
    assert profile.restrictions == (
        ActionRestriction("specific_action", "barbell-back-squat"),
    )
    assert profile.available_equipment.is_unknown
    assert profile.body_conditions == Fact.known(("肩部偶有不适",))
    assert proposed.body_conditions == Fact.known(("深蹲时膝盖锐痛",))
    assert dict(patch.facts) == facts_snapshot
    assert patch.add_restrictions == (ActionRestriction("movement_pattern", "深蹲"),)
    assert proposed.restrictions == (
        ActionRestriction("specific_action", "barbell-back-squat"),
        ActionRestriction("movement_pattern", "深蹲"),
    )


@pytest.mark.parametrize(
    "patch",
    [
        ProfilePatch(facts={"not_a_fact": Fact.known("值")}),
        ProfilePatch(facts={"weekly_frequency": Fact.known("3")}),
        ProfilePatch(facts={"training_goal": Fact.unknown()}),
        ProfilePatch(facts={"action_restrictions": Fact.known(())}),
        ProfilePatch(add_restrictions=(ActionRestriction("movement_pattern", "髋屈"),)),
        ProfilePatch(add_restrictions=(ActionRestriction("specific_action", ""),)),
        ProfilePatch(
            add_restrictions=(ActionRestriction("movement_pattern", "深蹲"),),
            remove_restrictions=(ActionRestriction("movement_pattern", "深蹲"),),
        ),
    ],
)
def test_invalid_patches_are_rejected_before_any_change(patch: ProfilePatch) -> None:
    profile = _seeded_profile()
    with pytest.raises(rules.InvalidProfilePatch):
        rules.apply_patch(profile, patch)
    # 错误补丁不部分修改输入对象
    assert profile == _seeded_profile()


async def test_invalid_patch_preview_leaves_database_unchanged(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service = ProfileService(db)
        await _write_profile(db, service, _seeded_profile())
        before = await _profile_row(db)
        bad_patch = ProfilePatch(
            facts={
                "available_equipment": Fact.known(("哑铃",)),
                "weekly_frequency": Fact.known("3"),  # 类型非法 → 整体拒绝
            }
        )
        with pytest.raises(rules.InvalidProfilePatch):
            await service.preview_patch(bad_patch)
        assert await _profile_row(db) == before


# ---------- 验收 3：当次条件不进入长期补丁 ----------


def test_session_conditions_are_not_a_long_term_patch() -> None:
    rules.validate_session_conditions(TODAY_DUMBBELL_ONLY)
    with pytest.raises(TypeError):
        rules.validate_patch(TODAY_DUMBBELL_ONLY)  # type: ignore[arg-type]


async def test_preview_rejects_session_conditions_before_touching_database(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service = ProfileService(db)
        with pytest.raises(TypeError):
            await service.preview_patch(TODAY_DUMBBELL_ONLY)  # type: ignore[arg-type]
        assert await _profile_row(db) == (None, 0)


def test_body_conditions_patch_is_memory_only() -> None:
    """长期身体情况补丁纯内存：apply_patch 只返回新对象，输入档案与序列化结果不动。"""
    profile = Profile(body_conditions=Fact.denied(), body_weight_kg=Fact.known(70.0))
    patch = ProfilePatch(facts={"body_conditions": Fact.known(("深蹲时膝盖锐痛",))})
    proposed = rules.apply_patch(profile, patch)
    assert proposed.body_conditions == Fact.known(("深蹲时膝盖锐痛",))
    assert profile.body_conditions == Fact.denied()
    assert profile_to_json(profile) == profile_to_json(
        Profile(body_conditions=Fact.denied(), body_weight_kg=Fact.known(70.0))
    )


def test_session_body_conditions_do_not_enter_long_term_patch() -> None:
    """当次身体情况只由 SessionConditions 承载，长期补丁不读它、也不写进档案。"""
    today = SessionConditions(body_conditions=Fact.known(("今天膝盖锐痛",)))
    rules.validate_session_conditions(today)
    with pytest.raises(TypeError):
        rules.validate_patch(today)  # type: ignore[arg-type]
    proposed = rules.apply_patch(_seeded_profile(), ProfilePatch())
    assert proposed.body_conditions.is_unknown
    assert "今天膝盖锐痛" not in profile_to_json(proposed)


def test_session_condition_shape_is_disjoint_from_patch_and_profile() -> None:
    patch_fields = set(ProfilePatch.__dataclass_fields__)
    session_fields = set(SessionConditions.__dataclass_fields__)
    assert patch_fields == {"facts", "add_restrictions", "remove_restrictions"}
    assert session_fields == {"available_equipment", "body_conditions"}
    assert patch_fields.isdisjoint(session_fields)


def test_today_only_condition_does_not_enter_patch_or_formal_profile() -> None:
    profile = _seeded_profile()
    proposed = rules.apply_patch(profile, LONG_TERM_DUMBBELL_PATCH)
    # 当次条件单独承载，不进补丁、不进档案：长期器械条件仍为未收集
    assert proposed.available_equipment == Fact.known(("哑铃",))
    # 当次条件由 SessionConditions 承载（今天只能用哑铃）；长期补丁应用不读它
    rules.validate_session_conditions(TODAY_DUMBBELL_ONLY)
    session_profile = rules.apply_patch(profile, ProfilePatch())
    assert session_profile.available_equipment.is_unknown
    assert profile_to_json(session_profile) == profile_to_json(profile)
    assert "哑铃" not in profile_to_json(session_profile)


def test_session_conditions_validate_structure() -> None:
    rules.validate_session_conditions(
        SessionConditions(
            available_equipment=Fact.denied(),
            body_conditions=Fact.known(("肩部偶有不适",)),
        )
    )
    with pytest.raises(rules.InvalidProfile):
        rules.validate_session_conditions(
            SessionConditions(available_equipment=Fact.known(("哑铃", "哑铃")))
        )
    # 当次身体情况按与长期档案同口径校验（text_list：非空文本、不重复）；输入对象不被修改
    for bad in (Fact.known(("深蹲时膝盖锐痛", "深蹲时膝盖锐痛")), Fact.known("锐痛")):
        conditions = SessionConditions(body_conditions=bad)
        with pytest.raises(rules.InvalidProfile):
            rules.validate_session_conditions(conditions)
        assert conditions.body_conditions is bad
    with pytest.raises(TypeError):
        rules.validate_session_conditions(ProfilePatch())  # type: ignore[arg-type]
