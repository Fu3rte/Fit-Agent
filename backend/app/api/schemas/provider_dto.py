"""模型配置端点的请求体模型。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ProviderPutBody(BaseModel):
    """模型配置整份覆盖请求体（PUT ``/api/provider``）。"""

    model_config = ConfigDict(extra="forbid")

    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    api: Literal["openai_compatible", "anthropic_messages"] | None = None
    structured_output: Literal["json_schema", "function_calling_strict"] | None = None
