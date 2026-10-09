import base64
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from app.domain.session.attachments import (
    MAX_ATTACHMENT_BYTES,
    AttachmentError,
    AttachmentMetadata,
    AttachmentReference,
    attachment_extension,
    attachment_inputs_adapter,
    attachment_storage_ref,
    validate_uuid,
)


@dataclass(frozen=True)
class PreparedUpload:
    attachment_id: str
    file_name: str
    data: bytes


@dataclass(frozen=True)
class CreatedAttachment:
    project_root: Path
    session_id: str
    attachment_id: str
    file_name: str
    device: int
    inode: int


class AttachmentFiles:
    def __init__(self, project_root: Path, *, check_credentials: Callable[[object], None]) -> None:
        self.project_root = project_root.absolute()
        self.check_credentials = check_credentials
        self._check_path(self.project_root)
        if not self.project_root.is_dir():
            raise AttachmentError("internal_error", "项目根目录不存在")

    def _check_path(self, path: Path) -> None:
        for component in (*reversed(path.parents), path):
            if component.is_symlink():
                raise AttachmentError("internal_error", "附件路径包含链接")
            if component.exists():
                attributes = component.lstat()
                if getattr(attributes, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise AttachmentError("internal_error", "附件路径包含 reparse point")

    def _path(self, session_id: str, attachment_id: str, file_name: str) -> Path:
        path = self.project_root / attachment_storage_ref(session_id, attachment_id, file_name)
        self._check_path(path)
        return path

    def read(self, session_id: str, metadata: AttachmentMetadata) -> str:
        session_id = validate_uuid(session_id)
        if metadata.session_id != session_id:
            raise AttachmentError("attachment_access_denied", "附件不属于当前会话")
        expected = attachment_storage_ref(session_id, metadata.attachment_id, metadata.file_name)
        if metadata.storage_ref != expected:
            raise AttachmentError("internal_error", "附件存储引用损坏")
        path = self._path(session_id, metadata.attachment_id, metadata.file_name)
        if not path.is_file():
            raise AttachmentError("internal_error", "附件原文件缺失或损坏")
        data = path.read_bytes()
        if len(data) != metadata.size_bytes:
            raise AttachmentError("internal_error", "附件原文件大小与元数据不一致")
        text = data.decode("utf-8", errors="strict")
        self.check_credentials({"file_name": metadata.file_name, "text": text})
        return text

    def prepare(
        self,
        session_id: str,
        inputs: object,
        *,
        existing: Mapping[str, AttachmentMetadata],
    ) -> tuple[PreparedUpload | AttachmentMetadata, ...]:
        # existing 必须来自可信上层，包含全部输入 ID 的全局占用及引用元数据。
        session_id = validate_uuid(session_id)
        items = attachment_inputs_adapter.validate_python(inputs, strict=True)
        ids = [item.attachment_id for item in items]
        if len(set(ids)) != len(ids):
            raise AttachmentError("invalid_request", "附件 ID 不得重复")
        prepared: list[PreparedUpload | AttachmentMetadata] = []
        total = 0
        for item in items:
            metadata = existing.get(item.attachment_id)
            if isinstance(item, AttachmentReference):
                if metadata is None:
                    raise AttachmentError("attachment_not_found", "找不到引用附件")
                if metadata.attachment_id != item.attachment_id:
                    raise AttachmentError("internal_error", "引用元数据 ID 不一致")
                self.read(session_id, metadata)
                total += metadata.size_bytes
                prepared.append(metadata)
            else:
                if metadata is not None:
                    raise AttachmentError("attachment_conflict", "附件 ID 已被占用")
                attachment_extension(item.file_name)
                data = base64.b64decode(item.data_base64, validate=True)
                text = data.decode("utf-8", errors="strict")
                self.check_credentials({"file_name": item.file_name, "text": text})
                total += len(data)
                prepared.append(PreparedUpload(item.attachment_id, item.file_name, data))
            if total > MAX_ATTACHMENT_BYTES:
                raise AttachmentError("attachment_size_exceeded", "附件原始字节总大小超过 100000")
        return tuple(prepared)

    def remove_created(self, created: CreatedAttachment) -> None:
        if created.project_root != self.project_root:
            raise AttachmentError("internal_error", "附件创建根目录无法核实")
        path = self._path(created.session_id, created.attachment_id, created.file_name)
        identity = path.stat()
        if not stat.S_ISREG(identity.st_mode) or (
            identity.st_dev, identity.st_ino
        ) != (created.device, created.inode):
            raise AttachmentError("internal_error", "附件创建身份无法核实")
        path.chmod(stat.S_IWUSR | stat.S_IRUSR)
        path.unlink()

    def write(
        self, session_id: str, upload: PreparedUpload, *, created_at: int,
        on_created: Callable[[CreatedAttachment], None] | None = None,
    ) -> AttachmentMetadata:
        # 接收层须在统一受理锁内完成全局 ID 查重、prepare、write 及数据库提交。
        # 跨后缀 ID 唯一性与失败清理由可信事务层负责；xb 保证同路径禁止覆盖。
        metadata = AttachmentMetadata(
            attachment_id=upload.attachment_id,
            session_id=session_id,
            file_name=upload.file_name,
            size_bytes=len(upload.data),
            storage_ref=attachment_storage_ref(session_id, upload.attachment_id, upload.file_name),
            created_at=created_at,
        )
        self.check_credentials({"file_name": upload.file_name, "text": upload.data.decode("utf-8", errors="strict")})
        path = self._path(metadata.session_id, metadata.attachment_id, metadata.file_name)
        for extension in (".md", ".txt"):
            candidate = path.with_suffix(extension)
            self._check_path(candidate)
            if candidate.exists():
                raise AttachmentError("attachment_conflict", "附件 ID 已被占用")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._check_path(path)
        with path.open("xb") as stream:
            identity = os.fstat(stream.fileno())
            if on_created is not None:
                on_created(CreatedAttachment(
                    self.project_root, metadata.session_id, metadata.attachment_id, metadata.file_name,
                    identity.st_dev, identity.st_ino,
                ))
            stream.write(upload.data)
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        return metadata
