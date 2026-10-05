from pathlib import Path

from dotenv import dotenv_values
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.ai.stream import ADAPTERS

DEFAULT_MODEL_API = "openai-completions"


class ModelConfig(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    OPENAI_API_KEY: str = Field(min_length=1, repr=False)
    OPENAI_BASE_URL: str = Field(min_length=1)
    OPENAI_MODEL: str = Field(min_length=1)
    OPENAI_PROVIDER: str = Field(min_length=1)
    MODEL_API: str = DEFAULT_MODEL_API

    @field_validator("MODEL_API")
    @classmethod
    def registered(cls, value: str) -> str:
        if value not in ADAPTERS:
            raise ValueError(f"未注册的协议: {value}")
        return value

    def create_client(self) -> OpenAI:
        return OpenAI(
            api_key=self.OPENAI_API_KEY,
            base_url=self.OPENAI_BASE_URL,
            max_retries=0,
            timeout=60,
        )


def load_model_config(path: Path | None = None) -> ModelConfig:
    source = path if path is not None else Path(__file__).resolve().parents[1] / ".env"
    return ModelConfig.model_validate(dotenv_values(source.resolve(strict=True)))
