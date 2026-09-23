from typing import Any, Literal

from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from pydantic import model_validator

from app.application.agent.harness.declaration import HarnessState
from app.application.agent.harness.tools.common import (
    HarnessToolArgs,
    StrictModel,
    TrainingHarnessContext,
    dump_tool_payload,
)
from app.domain.profile.schema import Fact, Profile


class ReadUserProfileArgs(HarnessToolArgs):
    """画像事实：无模型参数。"""


class ProfileFact(StrictModel):
    """一个画像字段的三态事实：known 必须携带非空值，unknown 与 denied 都不携带值。"""

    state: Literal["known", "unknown", "denied"]
    value: str | int | tuple[str, ...] | None = None

    @model_validator(mode="after")
    def validate_state_value(self) -> "ProfileFact":
        if self.state != "known":
            if self.value is not None:
                raise ValueError(f"{self.state} 状态不得携带值：{self.value!r}")
            return self
        if self.value is None or (
            isinstance(self.value, (str, tuple)) and not self.value
        ):
            raise ValueError(f"known 状态必须携带非空值：{self.value!r}")
        return self


class UserProfileView(StrictModel):
    """训练者七字段三态画像的稳定投影：字段名与领域画像逐字一致。"""

    training_goal: ProfileFact
    weekly_frequency: ProfileFact
    training_mode: ProfileFact
    explicit_preferences: ProfileFact
    current_level: ProfileFact
    known_injuries: ProfileFact
    forbidden_exercise_ids: ProfileFact


class ReadUserProfilePayload(StrictModel):
    """读取结果：未建档时为 null。"""

    profile: UserProfileView | None


@tool(args_schema=ReadUserProfileArgs)
async def read_user_profile(
    runtime: ToolRuntime[TrainingHarnessContext, HarnessState],
) -> str:
    """读取当前画像七字段的三态事实；未建档返回 ``profile=null``。

    ``known`` 表示用户已给出值，``unknown`` 表示尚未获得事实，``denied`` 表示用户拒绝提供或列表型
    事实明确为空。不补造任何默认目标、频率、训练方式、水平、偏好、伤病与禁用动作。
    """
    profile = await runtime.context.profiles.read()
    if profile is None:
        return dump_tool_payload(ReadUserProfilePayload(profile=None))
    return dump_tool_payload(ReadUserProfilePayload(profile=_profile_view(profile)))


def _profile_view(profile: Profile) -> UserProfileView:
    """领域画像 → 严格视图：七个字段逐个显式映射，不整体编码领域对象。"""
    return UserProfileView(
        training_goal=_profile_fact(profile.training_goal),
        weekly_frequency=_profile_fact(profile.weekly_frequency),
        training_mode=_profile_fact(profile.training_mode),
        explicit_preferences=_profile_fact(profile.explicit_preferences),
        current_level=_profile_fact(profile.current_level),
        known_injuries=_profile_fact(profile.known_injuries),
        forbidden_exercise_ids=_profile_fact(profile.forbidden_exercise_ids),
    )


def _profile_fact(fact: Fact[Any]) -> ProfileFact:
    """保留领域层已有的三态含义：state 直接透传，值原样携带。"""
    return ProfileFact(state=fact.state, value=fact.value)
