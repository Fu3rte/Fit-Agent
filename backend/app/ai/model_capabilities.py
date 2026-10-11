import json
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app.ai.types import ModelSpec
from app.model_config import ModelConfig

# 项目自带的能力目录：Pi 聊天模型身份与能力副本，运行时只读本地文件。
DATA_ROOT = Path(__file__).resolve().parent / "data"
MODELS_FILE = "models.json"
ENDPOINTS_FILE = "endpoints.json"

DEFAULT_CONTEXT_WINDOW = 128000
DEFAULT_MAX_TOKENS = 16384


class CapabilityCatalogError(Exception):
    pass


class ModelCapability(BaseModel):
    # 单条模型能力：字段缺失时独立取默认值，显式 null、布尔与非正整数被严格拒绝。
    model_config = ConfigDict(extra="forbid", strict=True)

    context_window: Annotated[int, Field(gt=0)] = DEFAULT_CONTEXT_WINDOW
    max_tokens: Annotated[int, Field(gt=0)] = DEFAULT_MAX_TOKENS


class CapabilityCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    models: dict[str, dict[str, ModelCapability]]
    endpoints: dict[str, dict[str, str]]


_models_adapter = TypeAdapter(dict[str, dict[str, ModelCapability]])
_endpoints_adapter = TypeAdapter(dict[str, dict[str, str]])


def _read(path: Path) -> object:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise CapabilityCatalogError(f"读取能力目录失败: {path.name}") from error
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise CapabilityCatalogError(f"能力目录不是合法 JSON: {path.name}") from error


def normalize_base_url(url: str) -> str:
    # 能力匹配使用规范化地址：小写 scheme/host，去路径尾斜杠，保留路径大小写与 query/fragment。
    parts = urlsplit(url)
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/"),
            parts.query,
            parts.fragment,
        )
    )


def load_capability_catalog() -> CapabilityCatalog:
    models_payload = _read(DATA_ROOT / MODELS_FILE)
    endpoints_payload = _read(DATA_ROOT / ENDPOINTS_FILE)
    try:
        models = _models_adapter.validate_python(models_payload, strict=True)
        endpoints = _endpoints_adapter.validate_python(endpoints_payload, strict=True)
    except ValidationError as error:
        raise CapabilityCatalogError("能力目录内容不合法") from error
    for api, mapping in endpoints.items():
        for url, provider in mapping.items():
            if not url or not provider:
                raise CapabilityCatalogError(f"端点映射存在空值: {api}")
            if provider not in models:
                raise CapabilityCatalogError(f"端点 {url} 指向未知 provider: {provider}")
    return CapabilityCatalog(models=models, endpoints=endpoints)


def resolve_capabilities(
    catalog: CapabilityCatalog, api: str, base_url: str, model_id: str
) -> ModelCapability:
    provider = catalog.endpoints.get(api, {}).get(normalize_base_url(base_url))
    if provider is None:
        return ModelCapability()
    entry = catalog.models.get(provider, {}).get(model_id)
    if entry is None:
        return ModelCapability()
    return entry


def resolve_model_spec(config: ModelConfig) -> ModelSpec:
    capability = resolve_capabilities(
        load_capability_catalog(), config.api, config.base_url, config.model
    )
    return ModelSpec(
        api=config.api,
        provider=config.provider,
        id=config.model,
        base_url=config.base_url,
        context_window=capability.context_window,
        max_tokens=capability.max_tokens,
    )
