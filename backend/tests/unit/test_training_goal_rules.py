# 训练目标闭集：写入与解码两侧都只接受增肌／增力／减脂三值。
# 依据：app/domain/profile/schema.py 的 TRAINING_GOALS 与 app/domain/profile/rules.py 的 _validate_value。

import json

import pytest

from app.domain.profile.rules import InvalidProfile, validate_profile_structure
from app.domain.profile.schema import (
    TRAINING_GOALS,
    Fact,
    InvalidProfileRow,
    Profile,
    profile_from_json,
    profile_to_json,
)

REJECTED_GOALS = ("减肥", "想变壮", "增肌减脂", "增肌 ", "", "hypertrophy")


def _profile(goal: str) -> Profile:
    """七字段画像：只有训练目标有值，其余字段保持未填写。"""
    return Profile(training_goal=Fact.known(goal))


def _stored(goal: str) -> str:
    """一份七字段 profile_json：训练目标是给定的 known 值。"""
    payload = json.loads(profile_to_json(Profile.empty()))
    payload["training_goal"] = {"state": "known", "value": goal}
    return json.dumps(payload, ensure_ascii=False)


@pytest.mark.parametrize("goal", TRAINING_GOALS)
def test_closed_goals_are_accepted_on_write_and_decode(goal: str) -> None:
    validate_profile_structure(_profile(goal))

    assert profile_from_json(_stored(goal)).training_goal == Fact.known(goal)


@pytest.mark.parametrize("goal", REJECTED_GOALS)
def test_other_goals_are_rejected_on_write(goal: str) -> None:
    with pytest.raises(InvalidProfile):
        validate_profile_structure(_profile(goal))


@pytest.mark.parametrize("goal", REJECTED_GOALS)
def test_other_goals_are_rejected_on_decode(goal: str) -> None:
    with pytest.raises(InvalidProfileRow):
        profile_from_json(_stored(goal))


@pytest.mark.parametrize("goal", TRAINING_GOALS)
def test_closed_goals_round_trip(goal: str) -> None:
    """三值编解码往返后与直接构造的画像同形。"""
    assert profile_to_json(profile_from_json(_stored(goal))) == profile_to_json(
        _profile(goal)
    )
