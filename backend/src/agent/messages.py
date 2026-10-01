import json
from typing import Annotated, Literal, Never, TypeAlias

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


Api: TypeAlias = str
ProviderId: TypeAlias = str
JsonObject: TypeAlias = dict[str, JsonValue]
ThinkingLevel: TypeAlias = Literal["minimal", "low", "medium", "high", "xhigh", "max"]
ModelThinkingLevel: TypeAlias = Literal["off"] | ThinkingLevel
StopReason: TypeAlias = Literal[
    "pending", "stop", "length", "toolUse", "error", "aborted", "deferred"
]


class TextContent(Model):
    type: Literal["text"]
    text: str
    textSignature: str = None


class ImageContent(Model):
    type: Literal["image"]
    data: str
    mimeType: str


class ThinkingContent(Model):
    type: Literal["thinking"]
    thinking: str
    thinkingSignature: str = None
    redacted: bool = None


class ToolCall(Model):
    type: Literal["toolCall"]
    id: str
    name: str
    arguments: JsonObject
    thoughtSignature: str = None
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
    cacheRead: int | float
    cacheWrite: int | float
    total: int | float


class Usage(Model):
    input: int | float
    output: int | float
    cacheRead: int | float
    cacheWrite: int | float
    cacheWrite1h: int | float = None
    reasoning: int | float = None
    totalTokens: int | float
    cost: UsageCost = None


class DeferredHandle(Model):
    provider: str
    modelId: str
    api: str
    id: str
    expiresAt: int | float = None
    pollAfterMs: int | float = None
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
    argumentsBytes: int | float = None
    status: Literal["ok", "error", "unfinished"]
    durationMs: int | float = None
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
        raise ValueError("constrainedSampling 必须为 false 或受限采样配置")
    return value


class Tool(Model):
    name: str
    description: str
    parameters: JsonObject
    constrainedSampling: (
        Annotated[Literal[False], BeforeValidator(_validate_false)]
        | ConstrainedSamplingConfig
    ) = None


class SystemMessage(Model):
    role: Literal["system"]
    content: str | list[TextContent]
    sections: dict[str, str | None] = None
    toolsAdded: list[Tool] = None
    toolsRemoved: list[ToolReference] = None
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
    responseModel: str = None
    responseId: str = None
    providerThinkingLevel: str = None
    thinkingLevel: ModelThinkingLevel = None
    diagnostics: list[AssistantMessageDiagnostic] = None
    usage: Usage
    stopReason: StopReason
    deferred: DeferredHandle = None
    errorMessage: str = None
    rawStopReason: str = None
    endTurn: bool = None
    timestamp: int | float


class ToolResultMessage(Model):
    role: Literal["toolResult"]
    toolCallId: str
    toolName: str
    content: list[ToolResultContent]
    isError: bool
    timestamp: int | float
    details: JsonValue = None
    usage: Usage = None
    nestedCalls: NestedToolCalls = None


Message: TypeAlias = Annotated[
    SystemMessage | UserMessage | AssistantMessage | ToolResultMessage,
    Field(discriminator="role"),
]
CustomMessage: TypeAlias = Never
AgentMessage: TypeAlias = Message

_agent_message_adapter = TypeAdapter(AgentMessage)


def parse_agent_message(value: str | bytes) -> AgentMessage:
    return _agent_message_adapter.validate_python(json.loads(value))


def serialize_message(message: AgentMessage) -> str:
    return json.dumps(
        message.model_dump(exclude_unset=True), ensure_ascii=False, allow_nan=False
    )
