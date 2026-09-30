from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    arguments: type[BaseModel]
    execute: Callable[..., str]
    max_output_chars: int | None = 50_000

    def definition(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.arguments.model_json_schema(),
            },
        }

    def invoke(self, arguments: str) -> str:
        values = self.arguments.model_validate_json(arguments)
        result = self.execute(**values.model_dump())
        if self.max_output_chars is not None and len(result) > self.max_output_chars:
            return (
                result[: self.max_output_chars]
                + f"\n[输出截断：超过 {self.max_output_chars} 字符]"
            )
        return result
