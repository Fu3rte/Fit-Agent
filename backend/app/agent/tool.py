from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel

from app.ai.messages import Tool as ToolDeclaration


@dataclass(frozen=True)
class ToolExecutionResult:
    content: str
    isError: bool


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    arguments: type[BaseModel]
    execute: Callable[..., ToolExecutionResult]
    max_output_chars: int | None = 50_000

    def definition(self) -> ToolDeclaration:
        return ToolDeclaration(
            name=self.name,
            description=self.description,
            parameters=self.arguments.model_json_schema(),
        )

    def invoke(self, arguments: str) -> ToolExecutionResult:
        values = self.arguments.model_validate_json(arguments)
        result = self.execute(**values.model_dump())
        if (
            self.max_output_chars is not None
            and len(result.content) > self.max_output_chars
        ):
            return ToolExecutionResult(
                result.content[: self.max_output_chars]
                + f"\n[输出截断：超过 {self.max_output_chars} 字符]",
                result.isError,
            )
        return result
