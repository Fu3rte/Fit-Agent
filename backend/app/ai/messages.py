import json
from typing import Annotated, Literal, Self, TypeAlias

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


Api: TypeAlias = str
ProviderId: TypeAlias = str
JsonObject: TypeAlias = dict[str, JsonValue]
ThinkingLevel: TypeAlias = Literal["minimal", "low", "medium", "high", "xhigh", "max"]
ModelThinkingLevel: TypeAlias = Literal["off"] | ThinkingLevel
StopReason: TypeAlias = Literal[
    "pending", "stop", "length", "toolUse", "error", "aborted"
]


class TextContent(Model):
    type: Literal["text"]
    text: str
    text_signature: str = None


class ImageContent(Model):
    type: Literal["image"]
    data: str
    mime_type: str


class ThinkingContent(Model):
    type: Literal["thinking"]
    thinking: str
    thinking_signature: str = None
    redacted: bool = None


class ToolCall(Model):
    type: Literal["toolCall"]
    id: str
    name: str
    arguments: JsonObject
    thought_signature: str = None
    namespace: str = None


UserContent: TypeAlias = Annotated[
    TextContent | ImageContent,
    Field(discriminator="type"),
]
AssistantContent: TypeAlias = Annotated[
    TextContent | ThinkingContent | ToolCall,
    Field(discriminator="type"),
]
ToolResultContent: TypeAlias = UserContent


class UsageCost(Model):
    input: int | float
    output: int | float
    cache_read: int | float
    cache_write: int | float
    total: int | float


class Usage(Model):
    input: int | float
    output: int | float
    cache_read: int | float
    cache_write: int | float
    cache_write_1h: int | float = None
    reasoning: int | float = None
    total_tokens: int | float
    cost: UsageCost = None


class DeferredHandle(Model):
    provider: str
    model_id: str
    api: str
    id: str
    expires_at: int | float = None
    poll_after_ms: int | float = None
    data: JsonValue = None


class DiagnosticErrorInfo(Model):
    name: str = None
    message: str
    stack: str = None
    code: str | int | float = None


class AssistantMessageDiagnostic(Model):
    type: str
    timestamp: int | float
    error: DiagnosticErrorInfo = None
    details: JsonObject = None


class NestedToolCallRecord(Model):
    id: str
    name: str
    arguments: JsonObject = None
    arguments_bytes: int | float = None
    status: Literal["ok", "error", "unfinished"]
    duration_ms: int | float = None
    error: str = None


class NestedToolCalls(Model):
    calls: list[NestedToolCallRecord]
    complete: bool


class ToolReference(Model):
    name: str


GrammarFormat: TypeAlias = Literal["openai_lark", "openai_regex"]


class GrammarVariants(Model):
    openai_lark: str = None
    openai_regex: str = None


class JsonSchemaSampling(Model):
    type: Literal["json_schema"]
    strict: Literal["prefer", "require"]


class GrammarSampling(Model):
    type: Literal["grammar"]
    variants: GrammarVariants


ConstrainedSamplingConfig: TypeAlias = Annotated[
    JsonSchemaSampling | GrammarSampling,
    Field(discriminator="type"),
]


def _validate_false(value: object) -> Literal[False]:
    if value is not False:
        raise ValueError("constrained_sampling 必须为 false 或受限采样配置")
    return value


class Tool(Model):
    name: str
    description: str
    parameters: JsonObject
    constrained_sampling: (
        Annotated[Literal[False], BeforeValidator(_validate_false)]
        | ConstrainedSamplingConfig
    ) = None


class SystemMessage(Model):
    role: Literal["system"]
    content: str | list[TextContent]
    sections: dict[str, str | None] = None
    tools_added: list[Tool] = None
    tools_removed: list[ToolReference] = None
    timestamp: int | float


class UserMessage(Model):
    role: Literal["user"]
    content: str | list[UserContent]
    timestamp: int | float


class AssistantMessage(Model):
    role: Literal["assistant"]
    content: list[AssistantContent]
    api: Api
    provider: ProviderId
    model: str
    response_model: str = None
    response_id: str = None
    provider_thinking_level: str = None
    thinking_level: ModelThinkingLevel = None
    diagnostics: list[AssistantMessageDiagnostic] = None
    usage: Usage | None
    stop_reason: StopReason
    deferred: DeferredHandle = None
    error_message: str = None
    raw_stop_reason: str = None
    end_turn: bool = None
    timestamp: int | float

    @model_validator(mode="after")
    def validate_usage(self) -> Self:
        if self.usage is None and self.stop_reason not in {"pending", "aborted"}:
            raise ValueError("完整助手消息必须包含真实 usage")
        if self.stop_reason == "error" and not self.error_message:
            raise ValueError("协议 error 必须包含 error_message")
        return self


class ToolResultMessage(Model):
    role: Literal["toolResult"]
    tool_call_id: str
    tool_name: str
    content: list[ToolResultContent]
    is_error: bool
    timestamp: int | float
    details: JsonValue = None
    usage: Usage = None
    nested_calls: NestedToolCalls = None


Message: TypeAlias = Annotated[
    SystemMessage | UserMessage | AssistantMessage | ToolResultMessage,
    Field(discriminator="role"),
]


def text_projection(content) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if not isinstance(block, TextContent):
            raise ValueError("消息包含无法公开投影的内容块")
        parts.append(block.text)
    return "".join(parts)


def serialize_message(message: Message) -> str:
    return json.dumps(
        message.model_dump(exclude_unset=True), ensure_ascii=False, allow_nan=False
    )
