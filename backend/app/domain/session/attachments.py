import base64
import re
from pathlib import Path, PureWindowsPath
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
)
from pydantic_core import PydanticCustomError

MAX_ATTACHMENT_BYTES = 100000
# 项目根由源码位置定位，不依赖启动 cwd；会话附件与文件工具统一以该目录下的 tmp 为根。
PROJECT_ROOT = Path(__file__).resolve().parents[4]
TMP_ROOT = PROJECT_ROOT / "tmp"
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


class AttachmentError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def validate_uuid(value: str) -> str:
    if _UUID.fullmatch(value) is None:
        raise ValueError("必须为标准 UUID")
    return value.lower()


def validate_file_name(value: str) -> str:
    if not value or any(character in value for character in ("/", "\\", "\x00")):
        raise ValueError("必须为非空文件名")
    if PureWindowsPath(value).drive or value in (".", "..") or ":" in value:
        raise ValueError("文件名不得包含路径形式")
    return value


def attachment_extension(file_name: str) -> str:
    extension = PureWindowsPath(file_name).suffix.lower()
    if extension not in (".md", ".txt"):
        raise AttachmentError("attachment_format_invalid", "附件扩展名须为 .md 或 .txt")
    return extension


AttachmentId: TypeAlias = Annotated[str, AfterValidator(validate_uuid)]
AttachmentFileName: TypeAlias = Annotated[str, AfterValidator(validate_file_name)]


class AttachmentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class AttachmentUpload(AttachmentModel):
    kind: Literal["upload"]
    attachment_id: AttachmentId
    file_name: AttachmentFileName
    data_base64: str

    @field_validator("file_name")
    @classmethod
    def validate_extension(cls, value: str) -> str:
        if PureWindowsPath(value).suffix.lower() not in (".md", ".txt"):
            raise PydanticCustomError("attachment_format_invalid", "附件格式不合法")
        return value

    @field_validator("data_base64")
    @classmethod
    def validate_data(cls, value: str) -> str:
        base64.b64decode(value, validate=True).decode("utf-8", errors="strict")
        return value


class AttachmentReference(AttachmentModel):
    kind: Literal["reference"]
    attachment_id: AttachmentId


AttachmentInput: TypeAlias = Annotated[
    AttachmentUpload | AttachmentReference, Field(discriminator="kind")
]
attachment_inputs_adapter = TypeAdapter(list[AttachmentInput])


class AttachmentMetadata(AttachmentModel):
    attachment_id: AttachmentId
    session_id: AttachmentId
    file_name: AttachmentFileName
    size_bytes: Annotated[int, Field(ge=0, le=MAX_ATTACHMENT_BYTES)]
    storage_ref: str
    created_at: Annotated[int, Field(ge=0)]


def attachment_storage_ref(session_id: str, attachment_id: str, file_name: str) -> str:
    session_id = validate_uuid(session_id)
    attachment_id = validate_uuid(attachment_id)
    validate_file_name(file_name)
    extension = attachment_extension(file_name)
    return f"tmp/sessions/{session_id}/attachments/{attachment_id}{extension}"
