from datetime import date
from typing import Annotated, Literal, Self, TypeAlias

from jsonpointer import JsonPointer, JsonPointerException
from jsonschema import FormatChecker
from pydantic import (
    AfterValidator,
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


WorkoutProposalStatus: TypeAlias = Literal[
    "pending", "processing", "saved", "invalidated", "conflicted"
]


class WorkoutSet(BusinessModel):
    reps: int | None = Field(gt=0)
    weight_kg: float | None = Field(ge=0)
    duration_seconds: float | None = Field(gt=0)


class WorkoutExercise(BusinessModel):
    exercise_id: str | None
    name: str
    load_convention: LoadConvention | None
    sets: list[WorkoutSet]

    @field_validator("exercise_id", "name")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("必须包含非空白内容")
        return value

    @model_validator(mode="after")
    def validate_load_convention(self) -> Self:
        if self.load_convention is None and any(
            item.weight_kg is not None for item in self.sets
        ):
            raise ValueError("重量有值时必须明确重量口径。")
        return self


class WorkoutContent(BusinessModel):
    exercises: list[WorkoutExercise] = Field(min_length=1)
    notes: str | None


class WorkoutDateModel(BusinessModel):
    performed_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("performed_on")
    @classmethod
    def validate_date(cls, value: str) -> str:
        date.fromisoformat(value)
        return value


class WorkoutRecord(WorkoutDateModel):
    id: str = Field(pattern=STANDARD_UUID)
    version: int = Field(gt=0)
    content: WorkoutContent
    created_at: int = Field(gt=0)
    updated_at: int = Field(gt=0)


class WorkoutGetArguments(BusinessModel):
    workout_id: str = Field(pattern=STANDARD_UUID)


class WorkoutListArguments(BusinessModel):
    date_from: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    date_to: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    page: int = Field(default=1, gt=0)
    page_size: int = Field(default=10, ge=1, le=100)

    @field_validator("date_from", "date_to")
    @classmethod
    def validate_date(cls, value: str | None) -> str | None:
        if value is not None:
            date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.date_from is not None and self.date_to is not None:
            if self.date_from > self.date_to:
                raise ValueError("起始日期不能晚于结束日期。")
        return self


class WorkoutListResult(BusinessModel):
    items: list[WorkoutRecord]
    page: int = Field(gt=0)
    page_size: int = Field(ge=1, le=100)
    total: int = Field(ge=0)


class WorkoutProposalArguments(WorkoutDateModel):
    base_workout_id: str | None = Field(pattern=STANDARD_UUID)
    base_workout_version: int | None = Field(gt=0)
    payload: WorkoutContent

    @model_validator(mode="after")
    def validate_base_pair(self) -> Self:
        if (self.base_workout_id is None) != (self.base_workout_version is None):
            raise ValueError("基础记录 ID 与版本必须同时为空或同时有值。")
        return self


class WorkoutProposal(WorkoutProposalArguments):
    proposal_id: str = Field(pattern=STANDARD_UUID)


class WorkoutSaveArguments(BusinessModel):
    proposal_id: str = Field(pattern=STANDARD_UUID)
    display_entry_id: str = Field(pattern=STANDARD_UUID)
    confirmation_entry_id: str = Field(pattern=STANDARD_UUID)


class WorkoutSaveResult(WorkoutRecord):
    proposal_id: str = Field(pattern=STANDARD_UUID)
    saved_at: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_saved_time(self) -> Self:
        if self.saved_at != self.updated_at:
            raise ValueError("保存时间必须与记录更新时间一致。")
        return self


class WorkoutStatusArguments(BusinessModel):
    proposal_id: str = Field(pattern=STANDARD_UUID)


class WorkoutStatusResult(BusinessModel):
    proposal_id: str = Field(pattern=STANDARD_UUID)
    status: WorkoutProposalStatus
    result: WorkoutSaveResult | None

    @model_validator(mode="after")
    def validate_result_pair(self) -> Self:
        if (self.status == "saved") != (self.result is not None):
            raise ValueError("仅 saved 状态携带完整保存结果。")
        if self.result is not None and self.result.proposal_id != self.proposal_id:
            raise ValueError("保存结果的快照标识不一致。")
        return self


class WorkoutSnapshot(WorkoutProposal):
    session_id: str = Field(pattern=STANDARD_UUID)
    request_entry_id: str = Field(pattern=STANDARD_UUID)
    source_entry_id: str = Field(pattern=STANDARD_UUID)
    display_entry_id: str | None = Field(default=None, pattern=STANDARD_UUID)
    confirmation_entry_id: str | None = Field(default=None, pattern=STANDARD_UUID)
    status: WorkoutProposalStatus
    created_at: int = Field(gt=0)


class WorkoutSaveRecord(BusinessModel):
    proposal_id: str = Field(pattern=STANDARD_UUID)
    session_id: str = Field(pattern=STANDARD_UUID)
    display_entry_id: str = Field(pattern=STANDARD_UUID)
    confirmation_entry_id: str = Field(pattern=STANDARD_UUID)
    result: WorkoutSaveResult
    saved_at: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_result_identity(self) -> Self:
        if self.proposal_id != self.result.proposal_id or self.saved_at != self.result.saved_at:
            raise ValueError("固定结果的快照标识与保存时间必须一致。")
        return self


PlanProposalStatus: TypeAlias = Literal[
    "pending", "processing", "saved", "invalidated", "conflicted"
]


# suggested_fields 标记助手补充或修改的字段，沿用 RFC 6901 JSON Pointer；jsonpointer 完整解析语法，
# jsonschema 格式检查器以不抛异常的 conforms 暴露判定结果。
_JSON_POINTER_FORMAT = FormatChecker()
_JSON_POINTER_FORMAT.checks("json-pointer", raises=JsonPointerException)(JsonPointer)


def _validate_json_pointer(value: str) -> str:
    if not _JSON_POINTER_FORMAT.conforms(value, "json-pointer"):
        raise ValueError("必须为合法的 JSON Pointer 字段路径。")
    return value


PlanFieldPath: TypeAlias = Annotated[str, AfterValidator(_validate_json_pointer)]


class PlanExercise(BusinessModel):
    exercise_id: str | None
    name: str
    sets: int | None = Field(gt=0)
    reps: int | None = Field(gt=0)
    duration_seconds: float | None = Field(gt=0)
    weight_kg: float | None = Field(ge=0)
    load_convention: LoadConvention | None
    rest_seconds: float | None = Field(ge=0)

    @field_validator("exercise_id", "name")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("必须包含非空白内容")
        return value

    @model_validator(mode="after")
    def validate_load_convention(self) -> Self:
        if self.weight_kg is not None and self.load_convention is None:
            raise ValueError("重量有值时必须明确重量口径。")
        return self


class PlanDay(BusinessModel):
    kind: Literal["training", "rest"]
    focus: str | None
    exercises: list[PlanExercise]
    notes: str | None

    @model_validator(mode="after")
    def validate_rest(self) -> Self:
        if self.kind == "rest" and self.exercises:
            raise ValueError("休息日动作列表必须为空。")
        return self


class PlanContent(BusinessModel):
    repeat: bool | None
    days: list[PlanDay] = Field(min_length=1)
    notes: str | None
    suggested_fields: list[PlanFieldPath]


class CurrentPlan(BusinessModel):
    id: str | None = Field(pattern=STANDARD_UUID)
    content: PlanContent | None

    @model_validator(mode="after")
    def validate_content_pair(self) -> Self:
        if (self.id is None) != (self.content is None):
            raise ValueError("当前计划 ID 与内容必须同时为空或同时有值。")
        return self


class PlanRecord(BusinessModel):
    id: str = Field(pattern=STANDARD_UUID)
    is_current: bool
    created_at: int = Field(gt=0)
    content: PlanContent


class PlanGetArguments(BusinessModel):
    plan_id: str = Field(pattern=STANDARD_UUID)


class PlanProposalArguments(BusinessModel):
    base_profile_version: int = Field(gt=0)
    base_plan_id: str | None = Field(pattern=STANDARD_UUID)
    payload: PlanContent


class PlanProposal(PlanProposalArguments):
    proposal_id: str = Field(pattern=STANDARD_UUID)


class PlanImportArguments(BusinessModel):
    base_profile_version: int | None = Field(gt=0)
    base_plan_id: str | None = Field(pattern=STANDARD_UUID)
    payload: PlanContent


class PlanAdjustmentArguments(PlanImportArguments):
    pass


class PlanImportProposal(PlanImportArguments):
    proposal_id: str = Field(pattern=STANDARD_UUID)
    preparation_kind: Literal["import"]


class PlanAdjustmentProposal(PlanAdjustmentArguments):
    proposal_id: str = Field(pattern=STANDARD_UUID)
    preparation_kind: Literal["adjustment"]


PlanPreparationKind: TypeAlias = Literal["generation", "import", "adjustment"]


class PlanSaveArguments(BusinessModel):
    proposal_id: str = Field(pattern=STANDARD_UUID)
    display_entry_id: str = Field(pattern=STANDARD_UUID)
    confirmation_entry_id: str = Field(pattern=STANDARD_UUID)


class PlanSaveResult(BusinessModel):
    proposal_id: str = Field(pattern=STANDARD_UUID)
    id: str = Field(pattern=STANDARD_UUID)
    content: PlanContent
    created_at: int = Field(gt=0)
    saved_at: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_saved_time(self) -> Self:
        if self.created_at != self.saved_at:
            raise ValueError("创建与保存时间必须一致。")
        return self


class PlanStatusArguments(BusinessModel):
    proposal_id: str = Field(pattern=STANDARD_UUID)


class PlanStatusResult(BusinessModel):
    proposal_id: str = Field(pattern=STANDARD_UUID)
    status: PlanProposalStatus
    result: PlanSaveResult | None

    @model_validator(mode="after")
    def validate_result_pair(self) -> Self:
        if (self.status == "saved") != (self.result is not None):
            raise ValueError("仅 saved 状态携带完整保存结果。")
        if self.result is not None and self.result.proposal_id != self.proposal_id:
            raise ValueError("保存结果的快照标识不一致。")
        return self


class PlanSnapshot(PlanImportArguments):
    proposal_id: str = Field(pattern=STANDARD_UUID)
    preparation_kind: PlanPreparationKind
    session_id: str = Field(pattern=STANDARD_UUID)
    request_entry_id: str = Field(pattern=STANDARD_UUID)
    source_entry_id: str = Field(pattern=STANDARD_UUID)
    display_entry_id: str | None = Field(default=None, pattern=STANDARD_UUID)
    confirmation_entry_id: str | None = Field(default=None, pattern=STANDARD_UUID)
    status: PlanProposalStatus
    created_at: int = Field(gt=0)


    @model_validator(mode="after")
    def validate_generation_basis(self) -> Self:
        if self.preparation_kind == "generation" and self.base_profile_version is None:
            raise ValueError("生成快照必须有已保存画像版本。")
        return self


class PlanSaveRecord(BusinessModel):
    proposal_id: str = Field(pattern=STANDARD_UUID)
    session_id: str = Field(pattern=STANDARD_UUID)
    display_entry_id: str = Field(pattern=STANDARD_UUID)
    confirmation_entry_id: str = Field(pattern=STANDARD_UUID)
    result: PlanSaveResult
    saved_at: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_result_identity(self) -> Self:
        if self.proposal_id != self.result.proposal_id or self.saved_at != self.result.saved_at:
            raise ValueError("固定结果的快照标识与保存时间必须一致。")
        return self


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
