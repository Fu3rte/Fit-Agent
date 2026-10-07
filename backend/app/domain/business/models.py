from typing import Annotated, Literal, Self, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class BusinessModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


PROFILE_TEXT_FIELDS = (
    "goal",
    "experience",
    "environment",
    "availability",
    "health_notes",
    "movement_restrictions",
)
PROFILE_LIST_FIELDS = ("unavailable_equipment", "forbidden_exercise_ids")
PROFILE_FIELDS = PROFILE_TEXT_FIELDS + PROFILE_LIST_FIELDS

LoadConvention: TypeAlias = Literal[
    "per_implement",
    "barbell_total",
    "machine_display",
    "plates_total",
    "per_side",
    "added_weight",
    "assistance_weight",
]
# 单用户本地画像 id 固定为 1；严格整数校验同时拒绝布尔与浮点写法。
ProfileTargetId: TypeAlias = Annotated[int, Field(ge=1, le=1)]
ProfileProposalStatus: TypeAlias = Literal[
    "pending", "processing", "saved", "invalidated", "conflicted"
]

# 会话消息节点 ID 与快照 ID 的标准 UUID 形式：紧凑、URN 及花括号写法在校验阶段拒绝。
STANDARD_UUID = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class ProfileContent(BusinessModel):
    goal: str | None
    experience: str | None
    environment: str | None
    availability: str | None
    health_notes: str | None
    movement_restrictions: str | None
    unavailable_equipment: list[str] | None
    forbidden_exercise_ids: list[str] | None

    @field_validator(*PROFILE_TEXT_FIELDS)
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("必须包含非空白内容")
        return value

    @field_validator(*PROFILE_LIST_FIELDS)
    @classmethod
    def validate_list(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        if any(not item.strip() for item in value):
            raise ValueError("元素必须为非空白字符串")
        if len(set(value)) != len(value):
            raise ValueError("不能包含重复项")
        return value

    def is_empty(self) -> bool:
        return all(getattr(self, name) is None for name in PROFILE_FIELDS)


class ProfileRecord(BusinessModel):
    version: int
    content: ProfileContent
    updated_at: int


class ProfileResponse(BusinessModel):
    version: int | None
    content: ProfileContent | None


# 画像自然语言确认与保存契约 §4 的三个工具字段。

class ProfileProposalArguments(BusinessModel):
    profile_id: ProfileTargetId
    base_profile_version: int | None = Field(gt=0)
    payload: ProfileContent

    @model_validator(mode="after")
    def validate_first_build(self) -> Self:
        # 首次建档至少有一个字段非 null；空限制列表也算有效信息（DATABASE §2）。
        if self.base_profile_version is None and self.payload.is_empty():
            raise ValueError("首次建档画像至少需要一个非空字段。")
        return self


class ProfileProposal(ProfileProposalArguments):
    proposal_id: str = Field(pattern=STANDARD_UUID)


class ProfileSaveArguments(BusinessModel):
    proposal_id: str = Field(pattern=STANDARD_UUID)
    display_entry_id: str = Field(pattern=STANDARD_UUID)
    confirmation_entry_id: str = Field(pattern=STANDARD_UUID)


class ProfileSaveResult(BusinessModel):
    proposal_id: str = Field(pattern=STANDARD_UUID)
    profile_id: ProfileTargetId
    version: int = Field(gt=0)
    content: ProfileContent
    saved_at: int = Field(gt=0)


class ProfileStatusArguments(BusinessModel):
    proposal_id: str = Field(pattern=STANDARD_UUID)


class ProfileStatusResult(BusinessModel):
    proposal_id: str = Field(pattern=STANDARD_UUID)
    status: ProfileProposalStatus
    result: ProfileSaveResult | None

    @model_validator(mode="after")
    def validate_result_pair(self) -> Self:
        if (self.status == "saved") != (self.result is not None):
            raise ValueError("仅 saved 状态携带完整保存结果。")
        if self.result is not None and self.result.proposal_id != self.proposal_id:
            raise ValueError("保存结果的快照标识不一致。")
        return self


# 快照与保存幂等记录的持久化对象（DATABASE.md §5）。

class ProfileSnapshot(BusinessModel):
    proposal_id: str = Field(pattern=STANDARD_UUID)
    session_id: str = Field(pattern=STANDARD_UUID)
    request_entry_id: str = Field(pattern=STANDARD_UUID)
    source_entry_id: str = Field(pattern=STANDARD_UUID)
    profile_id: ProfileTargetId
    base_profile_version: int | None = Field(gt=0)
    payload: ProfileContent
    display_entry_id: str | None = Field(default=None, pattern=STANDARD_UUID)
    confirmation_entry_id: str | None = Field(default=None, pattern=STANDARD_UUID)
    status: ProfileProposalStatus
    created_at: int = Field(gt=0)


# 保存幂等记录归属会话及消息节点，但不设外键：会话与消息删除后仍保留追溯。
# 绑定完整性与固定结果的一致性由 profile_snapshots、profile_save_records 的
# 数据库 CHECK 约束保证。
class ProfileSaveRecord(BusinessModel):
    proposal_id: str = Field(pattern=STANDARD_UUID)
    session_id: str = Field(pattern=STANDARD_UUID)
    profile_id: ProfileTargetId
    display_entry_id: str = Field(pattern=STANDARD_UUID)
    confirmation_entry_id: str = Field(pattern=STANDARD_UUID)
    result: ProfileSaveResult
    saved_at: int = Field(gt=0)


class BusinessFieldError(BusinessModel):
    path: str
    message: str


class BusinessContext(BusinessModel):
    timezone: str
    business_date: str
    session_id: str
    run_id: str
    request_entry_id: str
    source_entry_id: str


class CatalogSteps(BusinessModel):
    zh: list[str] = Field(min_length=1)
    en: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_steps(self) -> Self:
        for name in ("zh", "en"):
            if any(not step.strip() for step in getattr(self, name)):
                raise ValueError(f"steps.{name} 的元素必须为非空白字符串")
        return self


class CatalogExercise(BusinessModel):
    id: str
    name: str
    body_part: str
    equipment: str
    target: str
    muscle_group: str
    secondary_muscles: list[str]
    load_convention: LoadConvention | None
    steps: CatalogSteps
