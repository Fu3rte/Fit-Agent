# read_user_profile：七字段三态画像的严格投影，未建档返回 profile=null。
# state 直接透传领域层含义；列表值序列化为稳定 JSON array；known 必须有非空值。
# 事实用 tmp_path 下的真实迁移库与真实 Repo／Service；模型入口只用不可调用的替身。

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, fields
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import aiosqlite
import pytest
from pydantic import ValidationError

from app.application.agent.budget import ModelRequestBudget
from app.application.agent.contracts import ToolExecutionContext
from app.application.agent.harness.tools.common import TrainingHarnessContext
from app.application.agent.harness.tools.read_user_profile import (
    ProfileFact,
    ReadUserProfileArgs,
    UserProfileView,
    read_user_profile,
)
from app.application.ports import ModelGateway
from app.bootstrap import (
    ReadRepositories,
    SqliteHealthProbe,
    build_repositories,
    build_services,
)
from app.domain.profile.schema import PROFILE_FIELDS, Fact, Profile
from app.infrastructure.database.connection import Database

BUSINESS_DAY = date(2026, 6, 1)


async def _unavailable_model(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("read_user_profile 只读，不触达模型入口")


_MODEL = ModelGateway(
    text=_unavailable_model,
    structured=_unavailable_model,
    tools=_unavailable_model,
)


@dataclass(frozen=True, slots=True)
class ProfileHarness:
    """一次测试的迁移库、真实服务与注入上下文。"""

    db: Database
    repositories: ReadRepositories
    services: Any

    def context(self) -> TrainingHarnessContext:
        return TrainingHarnessContext(
            model=_MODEL,
            budget=ModelRequestBudget(),
            business_day=BUSINESS_DAY,
            profiles=self.repositories.profiles,
            plans=self.repositories.plans,
            catalog=self.repositories.exercises,
            records=self.services.records,
            stats=self.services.stats,
        )

    async def write(self, profile: Profile) -> None:
        await self.services.profile.update(profile)


@asynccontextmanager
async def _harness(tmp_path: Path) -> AsyncIterator[ProfileHarness]:
    db = Database(tmp_path / "fit_agent.db")
    await db.open()
    await db.migrate()
    try:
        repositories = build_repositories(db)
        services = build_services(
            repositories, db, SqliteHealthProbe(db, db.path.parent)
        )
        yield ProfileHarness(db=db, repositories=repositories, services=services)
    finally:
        await db.close()


async def _call(harness: ProfileHarness) -> Any:
    content = await read_user_profile.ainvoke(
        {"runtime": SimpleNamespace(context=harness.context())}
    )
    return json.loads(content)


def _full_profile() -> Profile:
    """七字段各覆盖一种状态与值形态：文本、整数、文本列表、明确拒绝。"""
    return Profile(
        training_goal=Fact.known("增肌"),
        weekly_frequency=Fact.known(3),
        available_equipment=Fact.known(("cable", "barbell")),
        explicit_preferences=Fact.denied(),
        current_level=Fact.known("中级"),
        known_injuries=Fact.denied(),
        forbidden_exercise_ids=Fact.known(("pull-up",)),
    )


async def test_without_profile_returns_null(tmp_path: Path) -> None:
    """未建档：顶层只有 null，不补造任何字段。"""
    async with _harness(tmp_path) as harness:
        assert await _call(harness) == {"profile": None}


async def test_full_profile_projects_exactly_the_seven_fields(tmp_path: Path) -> None:
    """已建档：字段集严格锁定，三态原样保留，每个字段只带 state 与 value。"""
    async with _harness(tmp_path) as harness:
        await harness.write(_full_profile())

        payload = await _call(harness)

        assert set(payload) == {"profile"}
        assert set(payload["profile"]) == set(PROFILE_FIELDS)
        assert all(
            set(fact) == {"state", "value"} for fact in payload["profile"].values()
        )
        assert UserProfileView.model_validate(payload["profile"]).training_goal == (
            ProfileFact(state="known", value="增肌")
        )
        profile = payload["profile"]
        assert profile["training_goal"] == {"state": "known", "value": "增肌"}
        assert profile["weekly_frequency"] == {"state": "known", "value": 3}
        assert profile["current_level"] == {"state": "known", "value": "中级"}
        assert profile["explicit_preferences"] == {"state": "denied", "value": None}
        assert profile["known_injuries"] == {"state": "denied", "value": None}


async def test_unknown_fields_are_not_defaulted(tmp_path: Path) -> None:
    """只填一个字段：其余六个仍是 unknown 且无值，不补默认目标、频率、器械与水平。"""
    async with _harness(tmp_path) as harness:
        await harness.write(Profile(training_goal=Fact.known("增肌")))

        profile = (await _call(harness))["profile"]

        assert profile["training_goal"] == {"state": "known", "value": "增肌"}
        for name in PROFILE_FIELDS:
            if name == "training_goal":
                continue
            assert profile[name] == {"state": "unknown", "value": None}


async def test_list_values_are_stable_json_arrays(tmp_path: Path) -> None:
    """列表型事实序列化为 JSON array，顺序稳定且两次调用逐字节一致。"""
    async with _harness(tmp_path) as harness:
        await harness.write(_full_profile())

        first = await _call(harness)
        second = await _call(harness)

        assert first == second
        equipment = first["profile"]["available_equipment"]
        assert equipment == {"state": "known", "value": ["cable", "barbell"]}
        assert isinstance(equipment["value"], list)
        assert first["profile"]["forbidden_exercise_ids"]["value"] == ["pull-up"]
        assert UserProfileView.model_validate(first["profile"]).available_equipment == (
            ProfileFact(state="known", value=("cable", "barbell"))
        )


def test_known_requires_a_non_empty_value() -> None:
    """known 必须有值：null、空文本与空列表都被严格模型拒绝。"""
    assert ProfileFact(state="known", value="增肌").value == "增肌"
    assert ProfileFact(state="known", value=0).value == 0

    for empty in (None, "", ()):
        with pytest.raises(ValidationError):
            ProfileFact(state="known", value=empty)
        with pytest.raises(ValidationError):
            ProfileFact.model_validate({"state": "known", "value": empty})


def test_unknown_and_denied_reject_values_and_state_is_closed() -> None:
    """unknown 与 denied 不得携带值；状态集合只有 known、unknown、denied。"""
    for state in ("unknown", "denied"):
        assert ProfileFact.model_validate({"state": state, "value": None}).value is None
        with pytest.raises(ValidationError):
            ProfileFact.model_validate({"state": state, "value": "增肌"})

    with pytest.raises(ValidationError):
        ProfileFact.model_validate({"state": "missing", "value": None})
    with pytest.raises(ValidationError):
        ProfileFact.model_validate(
            {"state": "known", "value": "增肌", "revision": 1}
        )


async def test_repository_failure_propagates(tmp_path: Path) -> None:
    """Repository 异常原样上抛：单例载体缺失不被吞成 null 或空画像。"""
    async with _harness(tmp_path) as harness:
        await harness.write(_full_profile())

        async def drop_singleton(conn: aiosqlite.Connection) -> None:
            cursor = await conn.execute("DELETE FROM athlete_profile WHERE id = 1")
            await cursor.close()

        await harness.db.under_lock(drop_singleton)

        with pytest.raises(RuntimeError, match="athlete_profile"):
            await _call(harness)


def test_runtime_is_hidden_from_the_model_visible_schema() -> None:
    """模型可见 Schema 没有参数；runtime、身份、revision 与七个画像字段名都不出现。"""
    visible = cast(Any, read_user_profile.tool_call_schema).model_json_schema()

    assert visible["properties"] == {}
    assert visible.get("required", []) == []
    assert read_user_profile.args_schema is ReadUserProfileArgs
    assert set(ReadUserProfileArgs.model_json_schema()["properties"]) == {"runtime"}
    assert ReadUserProfileArgs.model_json_schema()["additionalProperties"] is False
    injected = {field.name for field in fields(ToolExecutionContext)} | {"revision"}
    assert injected.isdisjoint(visible["properties"])
    assert set(PROFILE_FIELDS).isdisjoint(visible["properties"])


def test_args_reject_undeclared_fields() -> None:
    """无参工具拒绝未声明字段：画像只能来自注入上下文。"""
    ReadUserProfileArgs(runtime=None)
    with pytest.raises(ValidationError):
        ReadUserProfileArgs.model_validate({"runtime": None, "user_id": "local-user"})
