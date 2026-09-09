"""Stage 1 S1-04：两类动作限制的表示、引用校验与跨重开保留（验收 4）。

验收对照（stage1.md §5 S1-04 验收 4）：具体动作限制与动作模式限制可分别表示、校验引用
并跨重开保留；只保存当前有效限制，不新增观察中／暂禁／永久等状态语义；删除只能表达为
拟议变更，不直接生效。限制是否命中动作由 S1-05 判定，本模块只做结构与引用校验。
"""

import json
from pathlib import Path

import pytest

from domain.actions import rules as action_rules
from domain.profile import rules
from domain.profile.schema import (
    RESTRICTION_SCOPES,
    ActionRestriction,
    Fact,
    Profile,
    ProfilePatch,
    profile_from_json,
    profile_to_json,
)
from domain.profile.service import ProfileService
from tests.support import open_database

SPECIFIC = ActionRestriction("specific_action", "barbell-back-squat")
BY_MODE = ActionRestriction("movement_pattern", "深蹲")


def _restricted_profile() -> Profile:
    return Profile(
        body_weight_kg=Fact.known(70.0),
        action_restrictions=Fact.known((SPECIFIC, BY_MODE)),
    )


async def _insert_disabled_exercise(conn, exercise_id: str) -> None:
    """插入一条已停用的测试虚构动作：停用不删除，限制仍可按身份引用。"""
    await conn.execute(
        "INSERT INTO exercises (id, standard_name_zh, equipment_variant,"
        " record_type, load_convention, unilateral, recommendable, active,"
        " aliases_json, modes_json, source_ref, attribution, instructions_zh)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            exercise_id,
            "示例动作-已停用",
            "barbell",
            "reps_weight",
            "barbell_includes_bar_total",
            0,
            0,
            0,
            json.dumps(["test only disabled"], ensure_ascii=False),
            json.dumps(["深蹲"], ensure_ascii=False),
            "test-fixture",
            "test fixture",
            "测试用虚构动作说明",
        ),
    )


async def _read_profile(db) -> tuple[Profile | None, int]:
    snapshot = await ProfileService(db).read_formal_profile()
    return (snapshot.profile, snapshot.context_version)


# ---------- 两类限制分别表示与校验 ----------


def test_two_restriction_scopes_are_representable() -> None:
    assert RESTRICTION_SCOPES == ("specific_action", "movement_pattern")
    rules.validate_restriction(SPECIFIC)
    rules.validate_restriction(BY_MODE)
    assert {SPECIFIC.scope, BY_MODE.scope} == {"specific_action", "movement_pattern"}


def test_restriction_carries_no_status_semantics() -> None:
    assert set(ActionRestriction.__dataclass_fields__) == {"scope", "target"}
    for forbidden in ("status", "active", "permanent", "observing", "suspended"):
        assert forbidden not in ActionRestriction.__dataclass_fields__


def test_mode_restriction_must_come_from_the_decided_vocabulary() -> None:
    for mode in action_rules.MODE_VOCABULARY:
        rules.validate_restriction(ActionRestriction("movement_pattern", mode))
    with pytest.raises(rules.InvalidRestriction):
        rules.validate_restriction(ActionRestriction("movement_pattern", "髋屈"))
    with pytest.raises(rules.InvalidProfilePatch):
        rules.validate_patch(
            ProfilePatch(
                add_restrictions=(ActionRestriction("movement_pattern", "髋屈"),)
            )
        )
    with pytest.raises(rules.InvalidProfile):
        rules.validate_profile_structure(
            Profile(
                action_restrictions=Fact.known(
                    (ActionRestriction("movement_pattern", "髋屈"),)
                )
            )
        )


@pytest.mark.parametrize(
    "restriction",
    [
        ActionRestriction("movement_pattern", ""),
        ActionRestriction("specific_action", "   "),
        ActionRestriction("mode", "深蹲"),  # type: ignore[arg-type]
    ],
)
def test_invalid_restriction_structure_is_rejected(
    restriction: ActionRestriction,
) -> None:
    with pytest.raises(rules.InvalidRestriction):
        rules.validate_restriction(restriction)


async def test_specific_action_reference_is_checked_against_catalog(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service = ProfileService(db)
        await service.validate_patch(ProfilePatch(add_restrictions=(SPECIFIC,)))
        with pytest.raises(rules.UnknownExerciseReference):
            await service.validate_patch(
                ProfilePatch(
                    add_restrictions=(
                        ActionRestriction("specific_action", "not-in-catalog"),
                    )
                )
            )
        # 停用动作仍可按身份引用（停用不删除，03 3.1）
        async with db.transaction() as conn:
            await _insert_disabled_exercise(conn, "test-only-disabled")
        await service.validate_patch(
            ProfilePatch(
                add_restrictions=(
                    ActionRestriction("specific_action", "test-only-disabled"),
                )
            )
        )


# ---------- 跨重开保留 ----------


async def test_restrictions_survive_database_reopen(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    async with open_database(path) as db:
        service = ProfileService(db)
        async with db.transaction() as conn:
            await service.write_profile_in_transaction(conn, _restricted_profile())
    async with open_database(path) as db:
        profile, version = await _read_profile(db)
        assert profile is not None
        assert profile.restrictions == (SPECIFIC, BY_MODE)
        assert profile.action_restrictions == Fact.known((SPECIFIC, BY_MODE))
        assert version == 0  # 档案写入不推进业务版本


def test_restriction_json_roundtrip_keeps_scope_and_target() -> None:
    payload = json.loads(profile_to_json(_restricted_profile()))
    entries = payload["action_restrictions"]["value"]
    assert entries == [
        {"scope": "specific_action", "target": "barbell-back-squat"},
        {"scope": "movement_pattern", "target": "深蹲"},
    ]
    assert (
        profile_from_json(profile_to_json(_restricted_profile()))
        == _restricted_profile()
    )


def test_unknown_and_denied_restrictions_are_distinguishable() -> None:
    not_asked = Profile.empty()
    explicitly_none = Profile(action_restrictions=Fact.denied())
    assert not_asked.action_restrictions.is_unknown
    assert explicitly_none.action_restrictions.is_denied
    assert not_asked.restrictions == explicitly_none.restrictions == ()


# ---------- 删除只作为拟议变更 ----------


async def test_removal_is_only_a_proposed_change(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service = ProfileService(db)
        async with db.transaction() as conn:
            await service.write_profile_in_transaction(conn, _restricted_profile())

        proposed = await service.preview_patch(
            ProfilePatch(remove_restrictions=(BY_MODE,))
        )
        assert proposed.restrictions == (SPECIFIC,)
        # 拟议删除不直接生效：正式档案仍保留该限制
        formal, version = await _read_profile(db)
        assert formal is not None and formal.restrictions == (SPECIFIC, BY_MODE)
        assert version == 0

        # 只有经内部写入（外层确认事务语义）才落库
        async with db.transaction() as conn:
            await service.write_profile_in_transaction(conn, proposed)
        stored, _ = await _read_profile(db)
        assert stored is not None and stored.restrictions == (SPECIFIC,)


async def test_unrelated_patch_does_not_fabricate_no_restrictions(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service = ProfileService(db)
        async with db.transaction() as conn:
            await service.write_profile_in_transaction(
                conn, Profile(body_weight_kg=Fact.known(70.0))
            )
        proposed = await service.preview_patch(
            ProfilePatch(facts={"available_equipment": Fact.known(("哑铃",))})
        )
        # 无关补丁不得把「未收集限制」写成「明确无限制」
        assert proposed.action_restrictions.is_unknown
