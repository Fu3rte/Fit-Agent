# 模型配置存取与取值解析：数据目录 provider.json 优先，空字段回落 MODEL_* 环境变量。
# 存储边界：只落本地 provider.json（POSIX 创建权限 0600），不写业务库、不进 Checkpointer、
# 不进 WorkflowState；完整 API Key 不进 GET 响应、日志、SSE 与异常详情。
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from config import (
    MODEL_API_KEY_ENV,
    MODEL_BASE_URL_ENV,
    MODEL_MODEL_ENV,
    PROVIDER_CONFIG_FILENAME,
)


class ModelConfigurationError(ValueError):
    """模型配置缺失：provider.json 与对应 MODEL_* 环境变量皆空即配置错误，不落默认端点、不猜 URL。"""


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """provider.json 的三字段形状：缺省与清空一律空串，不携带任何默认端点。"""

    api_key: str = ""
    base_url: str = ""
    model: str = ""


def provider_config_path(data_dir: Path) -> Path:
    return data_dir / PROVIDER_CONFIG_FILENAME


def read_provider_config(data_dir: Path) -> ProviderConfig:
    """读取 provider.json；文件不存在即未配置（三字段空串），形状非法就地崩溃（fast-fail）。"""
    path = provider_config_path(data_dir)
    if not path.is_file():
        return ProviderConfig()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return ProviderConfig(
        api_key=str(raw.get("api_key", "")),
        base_url=str(raw.get("base_url", "")),
        model=str(raw.get("model", "")),
    )


def write_provider_config(data_dir: Path, config: ProviderConfig) -> None:
    """整份覆盖写入 provider.json：三字段恒定写出；POSIX 创建即 0600，Windows 走用户目录 ACL。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = provider_config_path(data_dir)
    payload = json.dumps(asdict(config), ensure_ascii=False).encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
    if os.name == "posix":
        # O_CREAT 的 mode 只在新建时生效：每次写入都强制回 0600。
        os.chmod(path, 0o600)


def resolve_model_credentials(
    data_dir: Path, override: ProviderConfig | None = None
) -> tuple[str, str, str]:
    """返回 (api_key, base_url, model)：override 非空字段覆盖 provider.json，其余回落同名 MODEL_* 环境变量。

    两者皆空即 :class:`ModelConfigurationError`（消息只含环境变量名，不含任何取值）；
    不落默认端点、不猜 URL。
    """
    config = read_provider_config(data_dir)
    if override is not None:
        config = ProviderConfig(
            api_key=override.api_key or config.api_key,
            base_url=override.base_url or config.base_url,
            model=override.model or config.model,
        )
    return (
        _resolve_field(config.api_key, MODEL_API_KEY_ENV),
        _resolve_field(config.base_url, MODEL_BASE_URL_ENV),
        _resolve_field(config.model, MODEL_MODEL_ENV),
    )


def _resolve_field(file_value: str, env_name: str) -> str:
    if value := file_value.strip():
        return value
    if value := os.environ.get(env_name, "").strip():
        return value
    raise ModelConfigurationError(
        f"缺少模型配置：provider.json 与环境变量 {env_name} 均为空"
    )


def provider_api_key_configured(data_dir: Path) -> bool:
    """Key 是否可解析（与 /api/provider 同源）：provider.json 非空 api_key，或 MODEL_API_KEY 非空。"""
    if read_provider_config(data_dir).api_key.strip():
        return True
    return bool(os.environ.get(MODEL_API_KEY_ENV, "").strip())
