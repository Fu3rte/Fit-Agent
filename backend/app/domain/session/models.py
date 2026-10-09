import re
from typing import Annotated, Literal, Self, TypeAlias

from pydantic import AfterValidator, Field, model_validator

from app.ai.messages import (
    AssistantMessage,
    JsonObject,
    Message,
    Model,
    UserMessage,
)
from app.domain.session.attachments import AttachmentInput, AttachmentMetadata

_MAX_TEXT_LENGTH = 32000

_OPERATION_ID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _validate_operation_id(value: str) -> str:
    if _OPERATION_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("operation_id 必须为标准 UUID")
    return value.lower()


def _validate_text(value: str) -> str:
    if not value.strip():
        raise ValueError("请求文本不得为空白")
    return value


OperationId: TypeAlias = Annotated[str, AfterValidator(_validate_operation_id)]
RequestText: TypeAlias = Annotated[
    str,
    Field(min_length=1, max_length=_MAX_TEXT_LENGTH),
    AfterValidator(_validate_text),
]


class SessionMessageEntry(Model):
    session_id: str
    id: str
    parent_id: str | None
    run_id: str | None
    type: Literal["message"]
    messages: list[Message]
    created_at: int
    attachments: list[AttachmentMetadata] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_messages(self) -> Self:
        if len(self.messages) != 1:
            raise ValueError("会话节点的 messages 数组长度必须为 1")
        message = self.messages[0]
        if isinstance(message, AssistantMessage) and message.stop_reason == "pending":
            raise ValueError("会话节点拒绝 stop_reason 为 pending 的助手消息")
        return self


class Session(Model):
    id: str
    title: str
    active_leaf_id: str | None
    created_at: int
    updated_at: int


RunStatus: TypeAlias = Literal[
    "running", "completed", "failed", "cancelled", "interrupted"
]

TerminalRunStatus: TypeAlias = Literal["completed", "failed", "cancelled"]


class SessionRun(Model):
    session_id: str
    id: str
    request_entry_id: str
    last_entry_id: str | None
    status: RunStatus
    started_at: int
    finished_at: int | None
    error_code: str | None
    error_message: str | None


SteeringStatus: TypeAlias = Literal[
    "pending", "consumed", "withdrawn", "discarded"
]


class SteeringInput(Model):
    session_id: str
    id: str
    run_id: str
    message: UserMessage
    status: SteeringStatus
    entry_id: str | None
    reason: str | None
    created_at: int
    updated_at: int
    attachments: list[AttachmentMetadata] = Field(default_factory=list)


OperationKind: TypeAlias = Literal["send", "edit", "regenerate", "steering"]


class SessionOperation(Model):
    operation_id: str
    session_id: str
    kind: OperationKind
    request: JsonObject
    run_id: str
    steering_id: str | None
    created_at: int


class SendRequest(Model):
    text: str = Field(max_length=_MAX_TEXT_LENGTH)
    attachments: list[AttachmentInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        if not self.text.strip() and not self.attachments:
            raise ValueError("请求必须包含文字或附件")
        return self


class EditRequest(Model):
    target_entry_id: str
    text: str = Field(max_length=_MAX_TEXT_LENGTH)
    attachments: list[AttachmentInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        if "attachments" in self.model_fields_set and not self.text.strip() and not self.attachments:
            raise ValueError("请求必须包含文字或附件")
        return self


class RegenerateRequest(Model):
    target_entry_id: str


class SteeringRequest(SendRequest):
    target_run_id: str


class SendCommand(Model):
    operation_id: OperationId
    session_id: str
    request: SendRequest


class EditCommand(Model):
    operation_id: OperationId
    session_id: str
    request: EditRequest


class RegenerateCommand(Model):
    operation_id: OperationId
    session_id: str
    request: RegenerateRequest


class SteeringCommand(Model):
    operation_id: OperationId
    session_id: str
    request: SteeringRequest


class OperationOutcome(Model):
    created: bool
    operation: SessionOperation
    run: SessionRun | None
    steering: SteeringInput | None


class AppendOutcome(Model):
    created: bool
    credential_detected: bool
    entry: SessionMessageEntry | None
    run: SessionRun


class SteeringConsumption(Model):
    created: bool
    steering: SteeringInput
    entry: SessionMessageEntry | None


class SteeringWithdrawal(Model):
    changed: bool
    steering: SteeringInput


class RunOutcome(Model):
    changed: bool
    run: SessionRun


class SessionHistory(Model):
    session: Session
    entries: list[SessionMessageEntry]
    runs: list[SessionRun]
    steering: list[SteeringInput]
