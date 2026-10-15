import math
import os
from collections.abc import Mapping

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

# 动作检索 Embedding 配置：仅由检索相关客户端创建与编码入口读取。
# 模块导入不要求密钥存在，也不触发网络调用；浏览、画像、历史与有效快照确认不依赖本模块。

API_KEY_VARIABLE = "DASHSCOPE_API_KEY"
WORKSPACE_VARIABLE = "DASHSCOPE_WORKSPACE_ID"
MODEL_VARIABLE = "FIT_AGENT_EMBEDDING_MODEL"
DIMENSIONS_VARIABLE = "FIT_AGENT_EMBEDDING_DIMENSIONS"

DEFAULT_EMBEDDING_MODEL = "qwen3.7-text-embedding-flash"
DEFAULT_EMBEDDING_DIMENSIONS = 1024
# 本任务只支持契约要求的 1024 维，其他值明确失败，不自动切换维度。
SUPPORTED_EMBEDDING_DIMENSIONS = (1024,)

WORKSPACE_DOMAIN = "cn-beijing.maas.aliyuncs.com"
COMPATIBLE_MODE_PATH = "/compatible-mode/v1"

_HTTP_URL = TypeAdapter(AnyHttpUrl)


class RetrievalConfigError(Exception):
    pass


class MissingRetrievalConfig(RetrievalConfigError):
    # 携带缺失变量名，便于调用方与验证报告明确阻塞项；不包含任何配置值。
    def __init__(self, variables: list[str]) -> None:
        self.variables = tuple(variables)
        super().__init__("缺少必需检索配置: " + ", ".join(self.variables))


def workspace_base_url(workspace_id: str) -> str:
    # 按契约的工作空间域名生成 Base URL，并用成熟 URL 校验工具确认结果合法。
    workspace = workspace_id.strip()
    if not workspace:
        raise RetrievalConfigError("工作空间 ID 不得为空白")
    url = f"https://{workspace}.{WORKSPACE_DOMAIN}{COMPATIBLE_MODE_PATH}"
    try:
        parsed = _HTTP_URL.validate_python(url)
    except ValidationError as error:
        raise RetrievalConfigError("工作空间生成域名不合法") from error
    if not parsed.host or not parsed.host.endswith(WORKSPACE_DOMAIN):
        raise RetrievalConfigError("工作空间生成域名不符合预期")
    return url


def validate_timeout(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout 必须是有限正数")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("timeout 必须是有限正数")
    return number


class EmbeddingConfig(BaseModel):
    # 单次编码固定的配置快照；hide_input_in_errors 与 repr=False 避免密钥进入校验错误或对象表示。
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    api_key: str = Field(repr=False)
    base_url: str
    model: str
    dimensions: int
    timeout: float | None = None

    @field_validator("api_key", "base_url", "model", "dimensions", mode="before")
    @classmethod
    def nonblank(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("检索配置字段必须包含非空白内容")
        return value

    @field_validator("base_url")
    @classmethod
    def http_url(cls, value: str) -> str:
        _HTTP_URL.validate_python(value)
        return value

    @field_validator("dimensions")
    @classmethod
    def supported_dimensions(cls, value: int) -> int:
        if value not in SUPPORTED_EMBEDDING_DIMENSIONS:
            raise ValueError("不支持的 Embedding 维度")
        return value

    @field_validator("timeout", mode="before")
    @classmethod
    def timeout_number(cls, value: object) -> float | None:
        return validate_timeout(value)


def load_embedding_config(environ: Mapping[str, str] | None = None) -> EmbeddingConfig:
    # 读取已加载的环境变量，不自行解析 dotenv；必需项缺失或为空白时在发请求前失败。
    source: Mapping[str, str] = os.environ if environ is None else environ
    missing = [
        name
        for name in (API_KEY_VARIABLE, WORKSPACE_VARIABLE)
        if not (source.get(name) or "").strip()
    ]
    if missing:
        raise MissingRetrievalConfig(missing)

    workspace = source[WORKSPACE_VARIABLE].strip()
    model = (source.get(MODEL_VARIABLE) or "").strip() or DEFAULT_EMBEDDING_MODEL
    raw_dimensions = (source.get(DIMENSIONS_VARIABLE) or "").strip()
    if raw_dimensions:
        try:
            dimensions = int(raw_dimensions)
        except ValueError as error:
            raise RetrievalConfigError(f"{DIMENSIONS_VARIABLE} 必须为整数") from error
    else:
        dimensions = DEFAULT_EMBEDDING_DIMENSIONS

    try:
        return EmbeddingConfig(
            api_key=source[API_KEY_VARIABLE],
            base_url=workspace_base_url(workspace),
            model=model,
            dimensions=dimensions,
        )
    except ValidationError as error:
        fields = ",".join(str(item["loc"][0]) for item in error.errors())
        raise RetrievalConfigError(f"检索配置校验失败: {fields}") from error
