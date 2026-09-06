"""Ported from pi-package/ai/src/types.ts @ pi snapshot 9841914c71a74d81abe07f751aefd271fd924e63

Core data contracts: messages, content blocks, usage, tools, model, and the
assistant-message event protocol.

Deliberate trims relative to types.ts (non-target API surface, see
pre-prj/pi-python-harness-plan.md §3.2):
- image generation types (ImagesModel / AssistantImages / ImagesContext / ...)
- provider request option interfaces (StreamOptions / ProviderRequestOptions /
  SimpleStreamOptions / ApiOptionsMap / ProviderStreams / StreamFunction / ...)
- provider compat interfaces (OpenAICompletionsCompat / OpenAIResponsesCompat /
  AnthropicMessagesCompat / BedrockCompat / OpenRouterRouting /
  VercelGatewayRouting); Model.compat is kept as a raw JSON dict placeholder
- constrained-sampling / chat-template-kwarg / thinking-budget helper types
  (only meaningful with the trimmed option interfaces)

Serialization contract (plan §4.1): dataclasses use snake_case fields;
``to_dict()`` emits pi camelCase wire keys. TS optional fields are ``None``
here and are OMITTED from output (never serialized as JSON null); an explicit
0 is always preserved. ``reasoning`` is a subset of ``output`` and
``cache_write_1h`` a subset of ``cache_write``; neither is added again into
``total_tokens``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Union, cast

# ---------------------------------------------------------------------------
# String / literal aliases
# ---------------------------------------------------------------------------

Api = str
"""TS: ``KnownApi | (string & {})`` — known values are openai-completions,
anthropic-messages, ...; custom API strings are allowed. Python keeps it a
plain ``str`` alias."""

ProviderId = str
"""TS: ``KnownProvider | string``."""

ToolChoice = Literal["auto", "none"]
ThinkingLevel = Literal["minimal", "low", "medium", "high", "xhigh", "max"]
ModelThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]
CacheRetention = Literal["none", "short", "long"]

StopReason = Literal["pending", "stop", "length", "toolUse", "error", "aborted", "deferred"]

# TS JsonValue is a recursive union; Python side just requires JSON-compat data.
JsonValue = Any

ThinkingLevelMap = dict[str, Union[str, None]]
"""TS: ``Partial<Record<ModelThinkingLevel, string | null>>``. Missing keys use
provider defaults; ``None`` (JSON null) marks a level as unsupported."""

# ---------------------------------------------------------------------------
# Content blocks
# ---------------------------------------------------------------------------


@dataclass
class TextContent:
    """TS TextContent. ``text_signature`` holds e.g. OpenAI Responses message
    metadata (legacy id string or TextSignatureV1 JSON)."""

    text: str
    text_signature: str | None = None
    type: Literal["text"] = field(default="text", init=False)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": "text", "text": self.text}
        if self.text_signature is not None:
            d["textSignature"] = self.text_signature
        return d


@dataclass
class ThinkingContent:
    """TS ThinkingContent. ``thinking_signature`` carries provider-specific
    opaque/serialized reasoning replay data. When ``redacted`` is true the
    opaque encrypted payload is stored in ``thinking_signature`` so it can be
    passed back to the API for multi-turn continuity."""

    thinking: str
    thinking_signature: str | None = None
    redacted: bool | None = None
    type: Literal["thinking"] = field(default="thinking", init=False)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": "thinking", "thinking": self.thinking}
        if self.thinking_signature is not None:
            d["thinkingSignature"] = self.thinking_signature
        if self.redacted is not None:
            d["redacted"] = self.redacted
        return d


@dataclass
class ImageContent:
    """TS ImageContent. ``data`` is base64-encoded image data."""

    data: str
    mime_type: str  # e.g. "image/jpeg", "image/png"
    type: Literal["image"] = field(default="image", init=False)

    def to_dict(self) -> dict[str, Any]:
        return {"type": "image", "data": self.data, "mimeType": self.mime_type}


@dataclass
class ToolCall:
    """TS ToolCall. ``thought_signature`` is Google-specific (opaque signature
    for reusing thought context); ``namespace`` is the OpenAI Responses
    namespace for dynamically loaded / namespaced tools."""

    id: str
    name: str
    arguments: dict[str, Any]
    thought_signature: str | None = None
    namespace: str | None = None
    type: Literal["toolCall"] = field(default="toolCall", init=False)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "type": "toolCall",
            "id": self.id,
            "name": self.name,
            "arguments": self.arguments,
        }
        if self.thought_signature is not None:
            d["thoughtSignature"] = self.thought_signature
        if self.namespace is not None:
            d["namespace"] = self.namespace
        return d


ContentBlock = Union[TextContent, ThinkingContent, ImageContent, ToolCall]
AssistantContent = Union[TextContent, ThinkingContent, ToolCall]
UserContent = Union[TextContent, ImageContent]
ToolResultContent = Union[TextContent, ImageContent]

# ---------------------------------------------------------------------------
# Usage / cost
# ---------------------------------------------------------------------------


@dataclass
class UsageCost:
    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0
    total: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "input": self.input,
            "output": self.output,
            "cacheRead": self.cache_read,
            "cacheWrite": self.cache_write,
            "total": self.total,
        }


@dataclass
class Usage:
    """TS Usage. ``reasoning`` is a subset of ``output`` (``output`` already
    includes these tokens); providers that expose a reasoning breakdown set a
    number (possibly 0), others leave it undefined. ``cache_write_1h`` is the
    subset of ``cache_write`` written with 1h retention (only Anthropic reports
    the split). Both are omitted from serialization when ``None`` (plan §4.1);
    an explicit 0 is preserved."""

    input: int
    output: int
    cache_read: int
    cache_write: int
    total_tokens: int
    reasoning: int | None = None
    cache_write_1h: int | None = None
    cost: UsageCost = field(default_factory=UsageCost)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "input": self.input,
            "output": self.output,
            "cacheRead": self.cache_read,
            "cacheWrite": self.cache_write,
            "totalTokens": self.total_tokens,
            "cost": self.cost.to_dict(),
        }
        if self.reasoning is not None:
            d["reasoning"] = self.reasoning
        if self.cache_write_1h is not None:
            d["cacheWrite1h"] = self.cache_write_1h
        return d


# ---------------------------------------------------------------------------
# Model & pricing
# ---------------------------------------------------------------------------


@dataclass
class ModelCostRates:
    input: float  # $/million tokens
    output: float  # $/million tokens
    cache_read: float  # $/million tokens
    cache_write: float  # $/million tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "input": self.input,
            "output": self.output,
            "cacheRead": self.cache_read,
            "cacheWrite": self.cache_write,
        }


@dataclass
class ModelCostTier(ModelCostRates):
    """Use this tier for requests whose total input usage exceeds
    ``input_tokens_above``."""

    input_tokens_above: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["inputTokensAbove"] = self.input_tokens_above
        return d


@dataclass
class ModelCost(ModelCostRates):
    """Request-wide pricing tiers: the highest matching input threshold
    applies to the full request."""

    tiers: list[ModelCostTier] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        if self.tiers:
            d["tiers"] = [t.to_dict() for t in self.tiers]
        return d


@dataclass
class Model:
    """TS Model<TApi> with the api generic erased. ``compat`` keeps the raw
    JSON dict; the typed per-API compat interfaces are trimmed (see module
    docstring). ``thinking_level_map`` maps pi thinking levels to
    provider/model-specific values; missing keys use provider defaults and
    None marks a level as unsupported."""

    id: str
    name: str
    api: Api
    provider: ProviderId
    base_url: str
    reasoning: bool
    input: list[str]  # ("text" | "image")[]
    cost: ModelCost
    context_window: int
    max_tokens: int
    thinking_level_map: ThinkingLevelMap | None = None
    # Default sampling parameters; per-request keys override these.
    sampling_params: dict[str, Any] | None = None
    headers: dict[str, str] | None = None
    compat: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "api": self.api,
            "provider": self.provider,
            "baseUrl": self.base_url,
            "reasoning": self.reasoning,
            "input": list(self.input),
            "cost": self.cost.to_dict(),
            "contextWindow": self.context_window,
            "maxTokens": self.max_tokens,
        }
        if self.thinking_level_map is not None:
            d["thinkingLevelMap"] = self.thinking_level_map
        if self.sampling_params is not None:
            d["samplingParams"] = self.sampling_params
        if self.headers is not None:
            d["headers"] = self.headers
        if self.compat is not None:
            d["compat"] = self.compat
        return d


# ---------------------------------------------------------------------------
# Deferred responses
# ---------------------------------------------------------------------------


@dataclass
class DeferredHandle:
    """TS DeferredHandle — provider token for deferred (async) responses."""

    provider: str
    model_id: str
    api: str
    id: str
    expires_at: int | None = None
    poll_after_ms: int | None = None
    # Provider conversion data required to reconstruct the final assistant message.
    data: JsonValue = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "provider": self.provider,
            "modelId": self.model_id,
            "api": self.api,
            "id": self.id,
        }
        if self.expires_at is not None:
            d["expiresAt"] = self.expires_at
        if self.poll_after_ms is not None:
            d["pollAfterMs"] = self.poll_after_ms
        if self.data is not None:
            d["data"] = self.data
        return d


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


@dataclass
class UserMessage:
    content: Union[str, list[UserContent]]
    timestamp: int  # Unix timestamp in milliseconds
    role: Literal["user"] = field(default="user", init=False)

    def to_dict(self) -> dict[str, Any]:
        content = (
            self.content
            if isinstance(self.content, str)
            else [block.to_dict() for block in self.content]
        )
        return {"role": "user", "content": content, "timestamp": self.timestamp}


@dataclass
class AssistantMessage:
    """TS AssistantMessage. ``diagnostics`` is kept as a raw list of JSON
    objects; the typed AssistantMessageDiagnostic port is deferred (non-core).
    ``provider_thinking_level`` is the exact provider-native effort level used
    for this response (absent for legacy/unmanaged responses)."""

    content: list[AssistantContent]
    api: Api
    provider: ProviderId
    model: str
    usage: Usage
    stop_reason: StopReason
    timestamp: int  # Unix timestamp in milliseconds
    # Concrete `chunk.model` when different from the requested `model` (e.g. OpenRouter auto).
    response_model: str | None = None
    # Provider-specific response/message identifier when the upstream API exposes one.
    response_id: str | None = None
    provider_thinking_level: str | None = None
    diagnostics: list[dict[str, Any]] | None = None
    deferred: DeferredHandle | None = None
    error_message: str | None = None
    raw_stop_reason: str | None = None
    # Provider indication of whether the model explicitly ended its turn.
    # Preserved for debugging; does not affect agent control flow.
    end_turn: bool | None = None
    role: Literal["assistant"] = field(default="assistant", init=False)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "role": "assistant",
            "content": [block.to_dict() for block in self.content],
            "api": self.api,
            "provider": self.provider,
            "model": self.model,
            "usage": self.usage.to_dict(),
            "stopReason": self.stop_reason,
            "timestamp": self.timestamp,
        }
        if self.response_model is not None:
            d["responseModel"] = self.response_model
        if self.response_id is not None:
            d["responseId"] = self.response_id
        if self.provider_thinking_level is not None:
            d["providerThinkingLevel"] = self.provider_thinking_level
        if self.diagnostics is not None:
            d["diagnostics"] = self.diagnostics
        if self.deferred is not None:
            d["deferred"] = self.deferred.to_dict()
        if self.error_message is not None:
            d["errorMessage"] = self.error_message
        if self.raw_stop_reason is not None:
            d["rawStopReason"] = self.raw_stop_reason
        if self.end_turn is not None:
            d["endTurn"] = self.end_turn
        return d


@dataclass
class ToolResultMessage:
    """TS ToolResultMessage. ``details`` is tool-defined (TS generic
    ``TDetails``); ``usage`` is from the tool execution itself, not part of main
    LLM context accounting; ``added_tool_names`` is the deferred-tool load point
    for providers with native deferred tool loading."""

    tool_call_id: str
    tool_name: str
    content: list[ToolResultContent]
    is_error: bool
    timestamp: int  # Unix timestamp in milliseconds
    details: Any = None
    usage: Usage | None = None
    added_tool_names: list[str] | None = None
    role: Literal["toolResult"] = field(default="toolResult", init=False)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "role": "toolResult",
            "toolCallId": self.tool_call_id,
            "toolName": self.tool_name,
            "content": [block.to_dict() for block in self.content],
            "isError": self.is_error,
            "timestamp": self.timestamp,
        }
        if self.details is not None:
            d["details"] = self.details
        if self.usage is not None:
            d["usage"] = self.usage.to_dict()
        if self.added_tool_names is not None:
            d["addedToolNames"] = self.added_tool_names
        return d


Message = Union[UserMessage, AssistantMessage, ToolResultMessage]

# ---------------------------------------------------------------------------
# Tools & context
# ---------------------------------------------------------------------------


@dataclass
class Tool:
    """TS Tool (with the typebox TSchema generic erased). ``parameters`` is the
    tool's JSON Schema. TS ``constrainedSampling`` is trimmed (non-target)."""

    name: str
    description: str
    parameters: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


@dataclass
class Context:
    """TS Context. Field init order differs from TS (required fields first);
    wire shape is unchanged."""

    messages: list[Message]
    system_prompt: str | None = None
    tools: list[Tool] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"messages": [message_to_dict(m) for m in self.messages]}
        if self.system_prompt is not None:
            d["systemPrompt"] = self.system_prompt
        if self.tools is not None:
            d["tools"] = [t.to_dict() for t in self.tools]
        return d


# ---------------------------------------------------------------------------
# Assistant message event protocol
# ---------------------------------------------------------------------------


@dataclass
class StartEvent:
    partial: AssistantMessage
    type: Literal["start"] = field(default="start", init=False)

    def to_dict(self) -> dict[str, Any]:
        return {"type": "start", "partial": self.partial.to_dict()}


@dataclass
class TextStartEvent:
    content_index: int
    partial: AssistantMessage
    type: Literal["text_start"] = field(default="text_start", init=False)

    def to_dict(self) -> dict[str, Any]:
        return {"type": "text_start", "contentIndex": self.content_index, "partial": self.partial.to_dict()}


@dataclass
class TextDeltaEvent:
    content_index: int
    delta: str
    partial: AssistantMessage
    type: Literal["text_delta"] = field(default="text_delta", init=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "text_delta",
            "contentIndex": self.content_index,
            "delta": self.delta,
            "partial": self.partial.to_dict(),
        }


@dataclass
class TextEndEvent:
    content_index: int
    content: str
    partial: AssistantMessage
    type: Literal["text_end"] = field(default="text_end", init=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "text_end",
            "contentIndex": self.content_index,
            "content": self.content,
            "partial": self.partial.to_dict(),
        }


@dataclass
class ThinkingStartEvent:
    content_index: int
    partial: AssistantMessage
    type: Literal["thinking_start"] = field(default="thinking_start", init=False)

    def to_dict(self) -> dict[str, Any]:
        return {"type": "thinking_start", "contentIndex": self.content_index, "partial": self.partial.to_dict()}


@dataclass
class ThinkingDeltaEvent:
    content_index: int
    delta: str
    partial: AssistantMessage
    type: Literal["thinking_delta"] = field(default="thinking_delta", init=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "thinking_delta",
            "contentIndex": self.content_index,
            "delta": self.delta,
            "partial": self.partial.to_dict(),
        }


@dataclass
class ThinkingEndEvent:
    content_index: int
    content: str
    partial: AssistantMessage
    type: Literal["thinking_end"] = field(default="thinking_end", init=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "thinking_end",
            "contentIndex": self.content_index,
            "content": self.content,
            "partial": self.partial.to_dict(),
        }


@dataclass
class ToolCallStartEvent:
    content_index: int
    partial: AssistantMessage
    type: Literal["toolcall_start"] = field(default="toolcall_start", init=False)

    def to_dict(self) -> dict[str, Any]:
        return {"type": "toolcall_start", "contentIndex": self.content_index, "partial": self.partial.to_dict()}


@dataclass
class ToolCallDeltaEvent:
    content_index: int
    delta: str
    partial: AssistantMessage
    type: Literal["toolcall_delta"] = field(default="toolcall_delta", init=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "toolcall_delta",
            "contentIndex": self.content_index,
            "delta": self.delta,
            "partial": self.partial.to_dict(),
        }


@dataclass
class ToolCallEndEvent:
    content_index: int
    tool_call: ToolCall
    partial: AssistantMessage
    type: Literal["toolcall_end"] = field(default="toolcall_end", init=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "toolcall_end",
            "contentIndex": self.content_index,
            "toolCall": self.tool_call.to_dict(),
            "partial": self.partial.to_dict(),
        }


@dataclass
class DoneEvent:
    reason: Literal["stop", "length", "toolUse", "deferred"]
    message: AssistantMessage
    type: Literal["done"] = field(default="done", init=False)

    def to_dict(self) -> dict[str, Any]:
        return {"type": "done", "reason": self.reason, "message": self.message.to_dict()}


@dataclass
class ErrorEvent:
    reason: Literal["aborted", "error"]
    error: AssistantMessage
    type: Literal["error"] = field(default="error", init=False)

    def to_dict(self) -> dict[str, Any]:
        return {"type": "error", "reason": self.reason, "error": self.error.to_dict()}


AssistantMessageEvent = Union[
    StartEvent,
    TextStartEvent,
    TextDeltaEvent,
    TextEndEvent,
    ThinkingStartEvent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ToolCallStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    DoneEvent,
    ErrorEvent,
]

# ---------------------------------------------------------------------------
# from_dict reconstruction (fixture / golden-trace loading)
# ---------------------------------------------------------------------------


def content_block_from_dict(d: dict[str, Any]) -> ContentBlock:
    block_type = d["type"]
    if block_type == "text":
        return TextContent(text=d["text"], text_signature=d.get("textSignature"))
    if block_type == "thinking":
        return ThinkingContent(
            thinking=d["thinking"],
            thinking_signature=d.get("thinkingSignature"),
            redacted=d.get("redacted"),
        )
    if block_type == "image":
        return ImageContent(data=d["data"], mime_type=d["mimeType"])
    if block_type == "toolCall":
        return ToolCall(
            id=d["id"],
            name=d["name"],
            arguments=d.get("arguments", {}),
            thought_signature=d.get("thoughtSignature"),
            namespace=d.get("namespace"),
        )
    raise ValueError(f"unknown content block type: {block_type!r}")


def usage_cost_from_dict(d: dict[str, Any] | None) -> UsageCost:
    if d is None:
        return UsageCost()
    return UsageCost(
        input=d["input"],
        output=d["output"],
        cache_read=d["cacheRead"],
        cache_write=d["cacheWrite"],
        total=d["total"],
    )


def usage_from_dict(d: dict[str, Any]) -> Usage:
    return Usage(
        input=d["input"],
        output=d["output"],
        cache_read=d["cacheRead"],
        cache_write=d["cacheWrite"],
        total_tokens=d["totalTokens"],
        reasoning=d.get("reasoning"),
        cache_write_1h=d.get("cacheWrite1h"),
        cost=usage_cost_from_dict(d.get("cost")),
    )


def deferred_handle_from_dict(d: dict[str, Any]) -> DeferredHandle:
    return DeferredHandle(
        provider=d["provider"],
        model_id=d["modelId"],
        api=d["api"],
        id=d["id"],
        expires_at=d.get("expiresAt"),
        poll_after_ms=d.get("pollAfterMs"),
        data=d.get("data"),
    )


def message_from_dict(d: dict[str, Any]) -> Message:
    role = d["role"]
    if role == "user":
        content = d["content"]
        if not isinstance(content, str):
            content = [cast(UserContent, content_block_from_dict(b)) for b in content]
        return UserMessage(content=content, timestamp=d["timestamp"])
    if role == "assistant":
        deferred = d.get("deferred")
        return AssistantMessage(
            content=[cast(AssistantContent, content_block_from_dict(b)) for b in d["content"]],
            api=d["api"],
            provider=d["provider"],
            model=d["model"],
            usage=usage_from_dict(d["usage"]),
            stop_reason=d["stopReason"],
            timestamp=d["timestamp"],
            response_model=d.get("responseModel"),
            response_id=d.get("responseId"),
            provider_thinking_level=d.get("providerThinkingLevel"),
            diagnostics=d.get("diagnostics"),
            deferred=deferred_handle_from_dict(deferred) if deferred is not None else None,
            error_message=d.get("errorMessage"),
            raw_stop_reason=d.get("rawStopReason"),
            end_turn=d.get("endTurn"),
        )
    if role == "toolResult":
        return ToolResultMessage(
            tool_call_id=d["toolCallId"],
            tool_name=d["toolName"],
            content=[cast(ToolResultContent, content_block_from_dict(b)) for b in d["content"]],
            is_error=d["isError"],
            timestamp=d["timestamp"],
            details=d.get("details"),
            usage=usage_from_dict(d["usage"]) if d.get("usage") is not None else None,
            added_tool_names=d.get("addedToolNames"),
        )
    raise ValueError(f"unknown message role: {role!r}")


def message_to_dict(message: Message) -> dict[str, Any]:
    return message.to_dict()
