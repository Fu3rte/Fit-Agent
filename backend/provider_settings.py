import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from config import PROVIDER_CONFIG_FILENAME

#: 客户端 transport：只决定用哪个 LangChain Chat 客户端，与结构化输出方式正交。
API_OPENAI_COMPATIBLE = "openai_compatible"
API_ANTHROPIC_MESSAGES = "anthropic_messages"

#: 结构化输出机制：只决定 with_structured_output 的原生 kwargs。
STRUCTURED_OUTPUT_JSON_SCHEMA = "json_schema"
STRUCTURED_OUTPUT_FUNCTION_CALLING_STRICT = "function_calling_strict"
STRUCTURED_OUTPUTS = (
    STRUCTURED_OUTPUT_JSON_SCHEMA,
    STRUCTURED_OUTPUT_FUNCTION_CALLING_STRICT,
)

#: 合法组合：Anthropic Messages 没有 strict function calling 形态。
STRUCTURED_OUTPUTS_BY_API: dict[str, tuple[str, ...]] = {
    API_OPENAI_COMPATIBLE: (
        STRUCTURED_OUTPUT_JSON_SCHEMA,
        STRUCTURED_OUTPUT_FUNCTION_CALLING_STRICT,
    ),
    API_ANTHROPIC_MESSAGES: (STRUCTURED_OUTPUT_JSON_SCHEMA,),
}

#: 旧 structured_protocol →（api, structured_output）的一次性兼容映射。
LEGACY_STRUCTURED_PROTOCOLS: dict[str, tuple[str, str]] = {
    "openai_chat_json_schema": (
        API_OPENAI_COMPATIBLE,
        STRUCTURED_OUTPUT_JSON_SCHEMA,
    ),
    "deepseek_tools_strict": (
        API_OPENAI_COMPATIBLE,
        STRUCTURED_OUTPUT_FUNCTION_CALLING_STRICT,
    ),
    "anthropic_messages_json_schema": (
        API_ANTHROPIC_MESSAGES,
        STRUCTURED_OUTPUT_JSON_SCHEMA,
    ),
}

DEFAULT_API = API_OPENAI_COMPATIBLE
DEFAULT_STRUCTURED_OUTPUT = STRUCTURED_OUTPUT_JSON_SCHEMA


class ModelConfigurationError(ValueError):
    """模型配置缺失或非法：provider.json 缺失或字段为空即配置错误，不落默认端点、不猜 URL。"""


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """provider.json 的五字段形状：凭据缺省与清空一律空串，api／structured_output 缺省为 openai_compatible／json_schema。"""

    api_key: str = ""
    base_url: str = ""
    model: str = ""
    api: str = DEFAULT_API
    structured_output: str = DEFAULT_STRUCTURED_OUTPUT


def provider_config_path(data_dir: Path) -> Path:
    return data_dir / PROVIDER_CONFIG_FILENAME


def read_provider_config(data_dir: Path) -> ProviderConfig:
    """读取 provider.json；文件不存在即未配置（凭据空串、协议默认），形状非法就地崩溃（fast-fail）。"""
    path = provider_config_path(data_dir)
    if not path.is_file():
        return ProviderConfig()
    raw = json.loads(path.read_text(encoding="utf-8"))
    api, structured_output = _read_protocol_pair(raw)
    return ProviderConfig(
        api_key=str(raw.get("api_key", "")),
        base_url=str(raw.get("base_url", "")),
        model=str(raw.get("model", "")),
        api=api,
        structured_output=structured_output,
    )


def _read_protocol_pair(raw: dict[str, Any]) -> tuple[str, str]:
    """两个新字段存在即整体生效（必须完整合法）；否则对旧 structured_protocol 做一次确定性映射。"""
    if "api" in raw or "structured_output" in raw:
        api = str(raw.get("api") or "").strip()
        structured_output = str(raw.get("structured_output") or "").strip()
        if not api or not structured_output:
            raise ModelConfigurationError(
                "provider.json 的 api 与 structured_output 必须同时给出"
            )
        return validated_protocol_pair(api, structured_output)
    legacy = str(raw.get("structured_protocol", "")).strip()
    if not legacy:
        return DEFAULT_API, DEFAULT_STRUCTURED_OUTPUT
    if legacy not in LEGACY_STRUCTURED_PROTOCOLS:
        raise ModelConfigurationError(f"未知的结构化输出协议：{legacy}")
    return LEGACY_STRUCTURED_PROTOCOLS[legacy]


def validated_protocol_pair(api: str, structured_output: str) -> tuple[str, str]:
    """api／structured_output 的合法性与组合校验收敛点：读盘、写盘与 resolve 共用同一判定。"""
    if api not in STRUCTURED_OUTPUTS_BY_API:
        raise ModelConfigurationError(f"未知的 api：{api}")
    if structured_output not in STRUCTURED_OUTPUTS:
        raise ModelConfigurationError(
            f"未知的 structured_output：{structured_output}"
        )
    if structured_output not in STRUCTURED_OUTPUTS_BY_API[api]:
        raise ModelConfigurationError(
            f"api={api} 不支持 structured_output={structured_output}"
        )
    return api, structured_output


def write_provider_config(data_dir: Path, config: ProviderConfig) -> None:
    """整份覆盖写入 provider.json：五字段恒定写出；组合非法即拒绝落盘，POSIX 创建即 0600。"""
    validated_protocol_pair(config.api, config.structured_output)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = provider_config_path(data_dir)
    payload = json.dumps(asdict(config), ensure_ascii=False).encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
    if os.name == "posix":
        # O_CREAT 的 mode 只在新建时生效：每次写入都强制回 0600。
        os.chmod(path, 0o600)


def resolve_provider_config(
    data_dir: Path, override: ProviderConfig | None = None
) -> ProviderConfig:
    """合并 override 与 provider.json 后解析完整配置：凭据只取这两处，协议只看配置本身。"""
    config = read_provider_config(data_dir)
    if override is not None:
        config = ProviderConfig(
            api_key=override.api_key or config.api_key,
            base_url=override.base_url or config.base_url,
            model=override.model or config.model,
            api=override.api or config.api,
            structured_output=override.structured_output or config.structured_output,
        )
    api, structured_output = validated_protocol_pair(
        config.api.strip() or DEFAULT_API,
        config.structured_output.strip() or DEFAULT_STRUCTURED_OUTPUT,
    )
    return ProviderConfig(
        api_key=_required_field(config.api_key, "api_key"),
        base_url=_required_field(config.base_url, "base_url"),
        model=_required_field(config.model, "model"),
        api=api,
        structured_output=structured_output,
    )


def _required_field(file_value: str, field: str) -> str:
    if value := file_value.strip():
        return value
    raise ModelConfigurationError(
        f"缺少模型配置：provider.json 的 {field} 为空，请在前端模型配置页填写后保存"
    )


def provider_api_key_configured(data_dir: Path) -> bool:
    """Key 是否已配置（与 /api/provider 同源）：只看 provider.json 的非空 api_key。"""
    return bool(read_provider_config(data_dir).api_key.strip())
