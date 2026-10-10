import json
import os
from pathlib import Path
from threading import RLock

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from app.ai.stream import ADAPTERS

DATA_ROOT = Path(__file__).resolve().parents[1] / "data"
MODELS_FILE = "models.json"
AUTH_FILE = "auth.json"
JOURNAL_FILE = "provider.journal"
API_VALUES = tuple(ADAPTERS)

_HTTP_URL = TypeAdapter(AnyHttpUrl)
_LOCK = RLock()


class ModelConfigError(Exception):
    pass


class ModelNotConfigured(Exception):
    pass


class StoredModelSettings(BaseModel):
    # models.json 的持久化形状：协议、Base URL、模型 ID 与实际生效服务标识。
    model_config = ConfigDict(extra="forbid", strict=True)

    api: str
    base_url: str
    model: str
    provider: str

    @field_validator("api")
    @classmethod
    def registered(cls, value: str) -> str:
        if value not in ADAPTERS:
            raise ValueError(f"未注册的协议: {value}")
        return value

    @field_validator("base_url")
    @classmethod
    def http_url(cls, value: str) -> str:
        return validate_http_url(value)

    @field_validator("model", "provider")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("字段必须包含非空白内容")
        return value


class StoredAuth(BaseModel):
    # auth.json 的持久化形状：单独保存全局凭据。
    model_config = ConfigDict(extra="forbid", strict=True)

    api_key: str | None

    @field_validator("api_key")
    @classmethod
    def nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("api_key 必须包含非空白内容")
        return value


class ModelConfig(BaseModel):
    # 单次运行取得并固定的完整配置快照。
    model_config = ConfigDict(hide_input_in_errors=True)

    api: str
    base_url: str
    model: str
    provider: str
    api_key: str = Field(repr=False)


class ProviderStatus(BaseModel):
    # 配置读取、保存与清除的统一响应形状；未配置时五个字段均为 null。
    api: str | None = None
    base_url: str | None = None
    model: str | None = None
    provider: str | None = None
    api_key: str | None = None


def validate_http_url(value: str) -> str:
    _HTTP_URL.validate_python(value)
    return value


def models_path() -> Path:
    return DATA_ROOT / MODELS_FILE


def auth_path() -> Path:
    return DATA_ROOT / AUTH_FILE


def journal_path() -> Path:
    return DATA_ROOT / JOURNAL_FILE


def effective_provider(api: str, provider: str | None) -> str:
    if provider is None:
        return api
    stripped = provider.strip()
    return stripped or api


def _read_text(path: Path) -> str | None:
    # 仅文件不存在表示未配置；其余读取结果一律交给调用方严格校验。
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise ModelConfigError(f"读取配置文件失败: {path.name}") from error


def _stored_settings() -> StoredModelSettings | None:
    text = _read_text(models_path())
    if text is None:
        return None
    try:
        return StoredModelSettings.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as error:
        raise ModelConfigError("models.json 内容不合法") from error


def _stored_api_key() -> str | None:
    text = _read_text(auth_path())
    if text is None:
        return None
    try:
        return StoredAuth.model_validate(json.loads(text)).api_key
    except (json.JSONDecodeError, ValidationError) as error:
        raise ModelConfigError("auth.json 内容不合法") from error


def _dump(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False)


def _stage(path: Path, text: str) -> Path:
    # 同目录临时文件先落盘并收紧权限，编码与写盘失败发生在替换之前。
    temporary = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(text, encoding="utf-8")
        os.chmod(temporary, 0o600)
    except OSError as error:
        raise ModelConfigError(f"写入配置文件失败: {path.name}") from error
    return temporary


def _replace(staged: Path, path: Path) -> None:
    try:
        os.replace(staged, path)
    except OSError as error:
        raise ModelConfigError(f"写入配置文件失败: {path.name}") from error


def _write(path: Path, text: str | None) -> None:
    if text is None:
        if path.exists():
            try:
                path.unlink()
            except OSError as error:
                raise ModelConfigError(f"写入配置文件失败: {path.name}") from error
        return
    _replace(_stage(path, text), path)


def _recover() -> None:
    # 存在提交日志说明上次保存中断；按日志回滚后再对外提供一致状态。
    text = _read_text(journal_path())
    if text is None:
        return
    try:
        previous = json.loads(text)
        models = previous["models"]
        auth = previous["auth"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise ModelConfigError("配置提交日志损坏") from error
    _write(models_path(), models)
    _write(auth_path(), auth)
    _write(journal_path(), None)


def _status(settings: StoredModelSettings, api_key: str | None) -> ProviderStatus:
    return ProviderStatus(
        api=settings.api,
        base_url=settings.base_url,
        model=settings.model,
        provider=settings.provider,
        api_key=api_key,
    )


def load_provider_status() -> ProviderStatus:
    with _LOCK:
        _recover()
        settings = _stored_settings()
        if settings is None:
            return ProviderStatus()
        return _status(settings, _stored_api_key())


def saved_api_key() -> str | None:
    with _LOCK:
        _recover()
        return _stored_api_key()


def load_model_config() -> ModelConfig:
    with _LOCK:
        _recover()
        settings = _stored_settings()
        if settings is None:
            raise ModelNotConfigured("尚未保存模型配置")
        api_key = _stored_api_key()
        if not api_key:
            raise ModelNotConfigured("模型凭据缺失")
        return ModelConfig(
            api=settings.api,
            base_url=settings.base_url,
            model=settings.model,
            provider=settings.provider,
            api_key=api_key,
        )


def save_provider(
    *,
    api: str,
    base_url: str,
    model: str,
    provider: str | None,
    api_key: str,
) -> ProviderStatus:
    settings = StoredModelSettings(
        api=api,
        base_url=base_url,
        model=model,
        provider=effective_provider(api, provider),
    )
    auth = StoredAuth(api_key=api_key)
    with _LOCK:
        _recover()
        previous = {"models": _read_text(models_path()), "auth": _read_text(auth_path())}
        models_staged = _stage(models_path(), _dump(settings.model_dump()))
        auth_staged = _stage(auth_path(), _dump(auth.model_dump()))
        _write(journal_path(), _dump(previous))
        try:
            _replace(models_staged, models_path())
            _replace(auth_staged, auth_path())
        except ModelConfigError:
            _recover()
            raise
        _write(journal_path(), None)
    return _status(settings, auth.api_key)


def clear_provider() -> ProviderStatus:
    # 先校验已存文件，损坏文件按错误处理，不进入清除流程。
    with _LOCK:
        _recover()
        _stored_settings()
        _stored_api_key()
        previous = {"models": _read_text(models_path()), "auth": _read_text(auth_path())}
        _write(journal_path(), _dump(previous))
        try:
            _write(models_path(), None)
            _write(auth_path(), None)
        except ModelConfigError:
            _recover()
            raise
        _write(journal_path(), None)
    return ProviderStatus()
