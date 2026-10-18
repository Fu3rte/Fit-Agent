import base64
from collections.abc import Sequence

from app.domain.session.attachments import AttachmentUpload
from app.domain.session.errors import CredentialDetected

CREDENTIAL_ERROR_CODE = "credential_detected"
CREDENTIAL_SAFE_MESSAGE = "检测到受保护凭据，操作已拒绝"


class CredentialFilter:
    def __init__(self, secrets):
        self._secrets = tuple(secret for secret in secrets if secret)
        self._hold = max((len(secret) for secret in self._secrets), default=0) - 1

    def contains(self, value) -> bool:
        if isinstance(value, str):
            return any(secret in value for secret in self._secrets)
        if isinstance(value, list):
            return any(self.contains(item) for item in value)
        if isinstance(value, dict):
            return any(
                self.contains(key) or self.contains(item)
                for key, item in value.items()
            )
        return False

    def tail_safe(self, text: str) -> str:
        if self._hold <= 0:
            return text
        return text[: max(0, len(text) - self._hold)]


def _collect_text(value: object, collected: list[str]) -> None:
    if isinstance(value, str):
        collected.append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            collected.append(key)
            _collect_text(item, collected)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_text(item, collected)


def contains_credential(value: object, credentials: Sequence[str]) -> bool:
    secrets = [secret for secret in credentials if secret]
    if not secrets:
        return False
    collected: list[str] = []
    _collect_text(value, collected)
    return any(secret in text for secret in secrets for text in collected)


def reject_credentials(value: object, credentials: Sequence[str]) -> None:
    if contains_credential(value, credentials):
        raise CredentialDetected("请求命中受保护凭据")


def reject_request_credentials(request: object, credentials: Sequence[str]) -> None:
    reject_credentials(request.model_dump(exclude_unset=True), credentials)
    for item in getattr(request, "attachments", []):
        if isinstance(item, AttachmentUpload):
            reject_credentials({
                "file_name": item.file_name,
                "text": base64.b64decode(item.data_base64, validate=True).decode("utf-8", errors="strict"),
            }, credentials)
