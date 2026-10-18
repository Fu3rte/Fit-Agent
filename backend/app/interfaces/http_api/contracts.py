from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.domain.business.models import WorkoutListArguments
from app.domain.session.attachments import AttachmentInput
from app.model_config import API_VALUES, validate_http_url


def path_uuid(value: str) -> str:
    # 路径身份只接受标准带连字符 UUID；紧凑、URN 及花括号形式按非法请求拒绝。
    if not isinstance(value, str):
        raise ValueError("身份必须为字符串")
    canonical = str(UUID(value))
    if value.lower() != canonical:
        raise ValueError("路径身份必须为标准带连字符的 UUID")
    return canonical


PathUUID = Annotated[str, BeforeValidator(path_uuid)]


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: PathUUID
    title: str = Field(strict=True, min_length=1, max_length=32000)

    @field_validator("title")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title 必须包含非空文本")
        return value


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: PathUUID
    operation_id: PathUUID
    request: str = Field(max_length=32000)
    attachments: list[AttachmentInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_content(self):
        if not self.request.strip() and not self.attachments:
            raise ValueError("请求必须包含文字或附件")
        return self


class EditPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: PathUUID
    operation_id: PathUUID
    target_entry_id: PathUUID
    request: str = Field(max_length=32000)
    attachments: list[AttachmentInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_content(self):
        if "attachments" in self.model_fields_set and not self.request.strip() and not self.attachments:
            raise ValueError("请求必须包含文字或附件")
        return self


class RegeneratePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: PathUUID
    operation_id: PathUUID
    target_entry_id: PathUUID


class SteeringRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: PathUUID
    operation_id: PathUUID
    message: str = Field(max_length=32000)
    attachments: list[AttachmentInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_content(self):
        if not self.message.strip() and not self.attachments:
            raise ValueError("请求必须包含文字或附件")
        return self


class WithdrawRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: PathUUID


class SaveConfirmationWire(BaseModel):
    # §7.2：确认请求恰好四字段。确认节点、业务正文、基础版本、训练记录 ID、
    # 保存工具名、运行 ID 及文字确认内容一律由后端从已绑定快照取得，客户端不得提交。
    model_config = ConfigDict(extra="forbid", strict=True)
    operation_id: PathUUID
    business_kind: Literal["profile", "plan", "workout"]
    proposal_id: PathUUID
    display_entry_id: PathUUID


class ProviderWriteBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    api: str
    base_url: str
    model: str
    api_key: str
    provider: str = ""

    @field_validator("api")
    @classmethod
    def registered(cls, value: str) -> str:
        if value not in API_VALUES:
            raise ValueError("仅支持 openai-completions 与 anthropic-messages")
        return value

    @field_validator("base_url")
    @classmethod
    def http_url(cls, value: str) -> str:
        return validate_http_url(value)

    @field_validator("model", "api_key")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("字段必须包含非空白内容")
        return value


class WorkoutQuery(WorkoutListArguments):
    model_config = ConfigDict(extra="forbid", strict=False, allow_inf_nan=False)
