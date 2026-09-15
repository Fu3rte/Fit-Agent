"""Stage 1 子任务 02 §5：用户画像 domain/profile —— 七字段三态、值域、外键与整份更新。

对照 02 清单：按新字段重写画像模型、删除 context_version、保证「未填写」与「明确为空」可区分、
禁用动作必须引用有效稳定 exercise_id、实现单用户读取与更新、添加结构／范围／外键／空值语义测试。

测试只使用 pytest tmp_path 下的独立临时库，不 import tests.support。
"""

import dataclasses
import json
from pathlib import Path

import pytest

from domain.profile.repo import ProfileRepo
from domain.profile.rules import (
    WEEKLY_FREQUENCY_MAX,
    WEEKLY_FREQUENCY_MIN,
    InvalidProfile,
    UnknownExerciseReference,
    validate_profile_structure,
)
from domain.profile.schema import (
    PROFILE_FIELDS,
    Fact,
    InvalidProfileRow,
    Profile,
    profile_from_json,
    profile_to_json,
)
from domain.profile.service import ProfileService
from storage.db import Database

EXPECTED_FIELDS = (
    "training_goal",
    "weekly_frequency",
    "available_equipment",
    "explicit_preferences",
    "current_level",
    "known_injuries",
    "forbidden_exercise_ids",
)


async def _migrated(path: Path) -> Database:
    db = Database(path)
    await db.open()
    await db.migrate()
    return db


async def _read_raw_profile_json(db: Database) -> str | None:
    async def op(conn):
        async with conn.execute(
            "SELECT profile_json FROM athlete_profile WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None or row["profile_json"] is None else str(
            row["profile_json"]
        )

    return await db.under_lock(op)


def test_profile_fields_are_exactly_the_seven_agreed_names() -> None:
    """七字段键集即 profile_json 键集：不保留旧字段（训练经验／时长／动作限制／身体情况／体重）。"""
    assert PROFILE_FIELDS == EXPECTED_FIELDS
    assert {field.name for field in dataclasses.fields(Profile)} == set(EXPECTED_FIELDS)


async def test_profile_is_unset_before_first_write(tmp_path: Path) -> None:
    """未建档：读取返回 None（NULL 技术载体），不预填任何事实。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        assert await ProfileService(db).read() is None
        assert await _read_raw_profile_json(db) is None
    finally:
        await db.close()


async def test_update_writes_all_seven_fields_as_three_state_facts(
    tmp_path: Path,
) -> None:
    """整份更新：七字段一次写入，库里是 {state, value} 三态对象，只有 known 携带值。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        service = ProfileService(db)
        await service.update(
            Profile(
                training_goal=Fact.known("增肌"),
                weekly_frequency=Fact.known(4),
                available_equipment=Fact.known(("barbell", "dumbbell")),
                explicit_preferences=Fact.known(("不喜欢跑步",)),
                current_level=Fact.known("中级"),
                known_injuries=Fact.denied(),
                forbidden_exercise_ids=Fact.unknown(),
            )
        )
        raw = await _read_raw_profile_json(db)
        assert raw is not None
        decoded = json.loads(raw)
        assert sorted(decoded) == sorted(EXPECTED_FIELDS)
        for name, entry in decoded.items():
            assert sorted(entry) == ["state", "value"], name
            assert entry["state"] in {"unknown", "denied", "known"}, name
            if entry["state"] != "known":
                assert entry["value"] is None, name
        assert decoded["training_goal"] == {"state": "known", "value": "增肌"}
        assert decoded["weekly_frequency"] == {"state": "known", "value": 4}
        assert decoded["available_equipment"] == {
            "state": "known",
            "value": ["barbell", "dumbbell"],
        }
        assert decoded["known_injuries"] == {"state": "denied", "value": None}
        assert decoded["forbidden_exercise_ids"] == {"state": "unknown", "value": None}

        read_back = await service.read()
        assert read_back == Profile(
            training_goal=Fact.known("增肌"),
            weekly_frequency=Fact.known(4),
            available_equipment=Fact.known(("barbell", "dumbbell")),
            explicit_preferences=Fact.known(("不喜欢跑步",)),
            current_level=Fact.known("中级"),
            known_injuries=Fact.denied(),
            forbidden_exercise_ids=Fact.unknown(),
        )
    finally:
        await db.close()


async def test_unfilled_and_explicitly_empty_stay_distinguishable(
    tmp_path: Path,
) -> None:
    """未填写（unknown）与明确为空（denied）往返后可区分，不静默混为默认值。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        service = ProfileService(db)
        await service.update(
            Profile(
                available_equipment=Fact.unknown(),
                explicit_preferences=Fact.denied(),
                known_injuries=Fact.denied(),
                forbidden_exercise_ids=Fact.unknown(),
            )
        )
        profile = await service.read()
        assert profile is not None
        assert profile.available_equipment.is_unknown
        assert not profile.available_equipment.is_denied
        assert not profile.available_equipment.is_known
        assert profile.explicit_preferences.is_denied
        assert not profile.explicit_preferences.is_known
        assert profile.known_injuries.is_denied
        assert profile.forbidden_exercise_ids.is_unknown
        assert profile.available_equipment != profile.explicit_preferences
        assert profile.explicit_preferences.value is None  # denied 不携带值，不是空列表
    finally:
        await db.close()


async def test_update_replaces_the_whole_profile(tmp_path: Path) -> None:
    """整份覆盖（PUT）：第二次写入不保留第一次的字段值。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        service = ProfileService(db)
        await service.update(
            Profile(
                training_goal=Fact.known("增肌"),
                weekly_frequency=Fact.known(4),
                current_level=Fact.known("中级"),
            )
        )
        await service.update(Profile.empty())
        read_back = await service.read()
        assert read_back == Profile.empty()
    finally:
        await db.close()


async def test_weekly_frequency_accepts_range_boundaries(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        service = ProfileService(db)
        for value in (WEEKLY_FREQUENCY_MIN, WEEKLY_FREQUENCY_MAX):
            await service.update(Profile(weekly_frequency=Fact.known(value)))
            read_back = await service.read()
            assert read_back is not None
            assert read_back.weekly_frequency.value == value
    finally:
        await db.close()


async def test_weekly_frequency_out_of_range_rejected(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        service = ProfileService(db)
        for value in (0, 8, -1, 100):
            with pytest.raises(InvalidProfile):
                await service.update(Profile(weekly_frequency=Fact.known(value)))
        assert await _read_raw_profile_json(db) is None  # 拒绝即不落盘
    finally:
        await db.close()


async def test_invalid_known_values_rejected(tmp_path: Path) -> None:
    """结构非法即整体拒绝：空文本、布尔冒充整数、重复项、空元素。"""
    invalid = (
        Profile(training_goal=Fact.known("  ")),
        Profile(weekly_frequency=Fact.known(True)),
        Profile(available_equipment=Fact.known(("barbell", "barbell"))),
        Profile(available_equipment=Fact.known(())),
        Profile(known_injuries=Fact.known(("",))),
        Profile(forbidden_exercise_ids=Fact.known(("barbell-back-squat", ""))),
    )
    db = await _migrated(tmp_path / "x.db")
    try:
        for profile in invalid:
            with pytest.raises(InvalidProfile):
                await ProfileService(db).update(profile)
    finally:
        await db.close()


def test_known_empty_list_is_rejected_as_denied_spelling() -> None:
    """P2-1 拍板 A：列表 known 不得为空，明确为空只能用 denied 表达（写入与解码两侧）。"""
    with pytest.raises(InvalidProfile):
        validate_profile_structure(Profile(available_equipment=Fact.known(())))
    payload = json.loads(profile_to_json(Profile(available_equipment=Fact.denied())))
    payload["available_equipment"] = {"state": "known", "value": []}
    with pytest.raises(InvalidProfileRow):
        profile_from_json(json.dumps(payload))


async def test_forbidden_exercise_ids_must_reference_the_catalog(
    tmp_path: Path,
) -> None:
    """禁用动作引用有效稳定 exercise_id：目录内通过，目录外拒绝，未填写／明确为空无需校验。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        service = ProfileService(db)
        await service.update(
            Profile(
                forbidden_exercise_ids=Fact.known(
                    ("barbell-back-squat", "hanging-leg-raise")
                )
            )
        )
        with pytest.raises(UnknownExerciseReference):
            await service.update(
                Profile(forbidden_exercise_ids=Fact.known(("no-such-exercise",)))
            )
        await service.update(Profile(forbidden_exercise_ids=Fact.denied()))
        read_back = await service.read()
        assert read_back is not None
        assert read_back.forbidden_exercise_ids.value is None
    finally:
        await db.close()


async def test_profile_json_round_trips_through_codec() -> None:
    profile = Profile(
        training_goal=Fact.known("减脂"),
        weekly_frequency=Fact.known(3),
        available_equipment=Fact.denied(),
        explicit_preferences=Fact.known(("偏好自由重量",)),
        current_level=Fact.unknown(),
        known_injuries=Fact.known(("左肩旧伤",)),
        forbidden_exercise_ids=Fact.known(("seated-dumbbell-shoulder-press",)),
    )
    assert profile_from_json(profile_to_json(profile)) == profile


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '{"training_goal": {"state": "known", "value": null}}',
        '{"training_goal": {"state": "known", "value": "增肌", "extra": 1}}',
        '{"training_goal": {"state": "maybe", "value": null}}',
        '{"training_goal": {"state": "unknown", "value": "增肌"}}',
        '{"training_goal": {"state": "known", "value": "增肌"}, "body_weight_kg": null}',
    ],
)
def test_broken_profile_json_is_rejected(raw: str) -> None:
    """数据损坏（缺字段／旧字段／状态或值形状不符）大声失败，不静默补默认值。"""
    with pytest.raises(InvalidProfileRow):
        profile_from_json(raw)


async def test_corrupt_stored_profile_json_fails_loudly(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        async with db.transaction() as conn:
            await conn.execute(
                "UPDATE athlete_profile SET profile_json = ? WHERE id = 1",
                ('{"training_goal": {"state": "known", "value": "增肌"}}',),
            )
        with pytest.raises(InvalidProfileRow):
            await ProfileRepo(db).read()
    finally:
        await db.close()


@pytest.mark.parametrize(
    ("state", "value"),
    [("unknown", "增肌"), ("denied", 4), ("known", None), ("maybe", None)],
)
def test_fact_state_and_value_must_agree(state: str, value: object) -> None:
    """三态自洽：非 known 不得携带值，known 必须携带值。"""
    with pytest.raises(ValueError):
        Fact(state=state, value=value)  # type: ignore[arg-type]


async def test_athlete_profile_has_no_context_version(tmp_path: Path) -> None:
    """context_version 已删除：表里没有该列，画像 JSON 里也没有该键。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        async def op(conn):
            async with conn.execute("PRAGMA table_info(athlete_profile)") as cursor:
                return {str(row["name"]) for row in await cursor.fetchall()}

        assert await db.under_lock(op) == {"id", "profile_json"}
        await ProfileService(db).update(Profile(training_goal=Fact.known("增肌")))
        raw = await _read_raw_profile_json(db)
        assert raw is not None
        assert "context_version" not in json.loads(raw)
    finally:
        await db.close()


def test_structure_validation_rejects_non_profile_values() -> None:
    with pytest.raises(InvalidProfile):
        validate_profile_structure("not a profile")  # type: ignore[arg-type]
    with pytest.raises(InvalidProfile):
        validate_profile_structure(Profile(training_goal="增肌"))  # type: ignore[arg-type]
