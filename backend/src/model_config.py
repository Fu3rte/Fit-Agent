from pathlib import Path

from dotenv import dotenv_values
from openai import OpenAI
from pydantic import BaseModel, Field


class ModelConfig(BaseModel):
    OPENAI_API_KEY: str = Field(min_length=1, repr=False)
    OPENAI_BASE_URL: str = Field(min_length=1)
    OPENAI_MODEL: str = Field(min_length=1)

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
