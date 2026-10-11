import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agent import compaction as C
from app.ai import model_capabilities as MC
from app.ai.messages import (
    AssistantMessage,
    ImageContent,
    SystemMessage,
    TextContent,
    ThinkingContent,
    Tool,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from app.ai.types import ModelSpec
from app.application.session.service import SessionService
from app.domain.session.attachments import attachment_storage_ref
from app.domain.session.branch import build_session_path
from app.domain.session.models import (
    AttachmentMetadata,
    CompactionAttachmentReference,
    CompactionDetails,
    CompactionEntry,
    SendCommand,
    SendRequest,
    SessionMessageEntry,
)
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from app.model_config import ModelConfig

BACKEND = Path(__file__).resolve().parents[1]
TEMP_ROOT = BACKEND / "temp" / "compaction-core"
SESSION = "11111111-1111-4111-8111-111111111111"
ATT_A = "22222222-2222-4222-8222-222222222222"
ATT_B = "33333333-3333-4333-8333-333333333333"

USAGE = Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2)
SMALL = C.CompactionSettings(reserve_tokens=16384, keep_recent_tokens=100)

_clock = [1000]


def tick() -> int:
    _clock[0] += 1
    return _clock[0]


def system_message(text: str = "系统指令", tools=None) -> SystemMessage:
    fields = {"tools_added": tools} if tools is not None else {}
    return SystemMessage(role="system", content=text, timestamp=tick(), **fields)


def user(text: str) -> UserMessage:
    return UserMessage(role="user", content=text, timestamp=tick())


def assistant(
    text: str = "回答",
    calls: tuple[ToolCall, ...] = (),
    *,
    usage: Usage | None = USAGE,
    stop_reason: str = "stop",
) -> AssistantMessage:
    content = [TextContent(type="text", text=text)] if text else []
    content.extend(calls)
    return AssistantMessage(
        role="assistant",
        content=content,
        api="openai-completions",
        provider="test",
        model="test",
        usage=usage,
        stop_reason=stop_reason,
        timestamp=tick(),
    )


def tool_result(call_id: str, text: str = "结果") -> ToolResultMessage:
    return ToolResultMessage(
        role="toolResult",
        tool_call_id=call_id,
        tool_name="echo",
        content=[TextContent(type="text", text=text)],
        is_error=False,
        timestamp=tick(),
    )


def entry(entry_id: str, parent_id: str | None, message, attachments=()) -> SessionMessageEntry:
    return SessionMessageEntry(
        session_id=SESSION,
        id=entry_id,
        parent_id=parent_id,
        run_id=None,
        type="message",
        messages=[message],
        created_at=tick(),
        attachments=list(attachments),
    )


def attachment(attachment_id: str, file_name: str = "note.md") -> AttachmentMetadata:
    return AttachmentMetadata(
        attachment_id=attachment_id,
        session_id=SESSION,
        file_name=file_name,
        size_bytes=3,
        storage_ref=attachment_storage_ref(SESSION, attachment_id, file_name),
        created_at=tick(),
    )


def reference(attachment_id: str, file_name: str = "note.md") -> CompactionAttachmentReference:
    return CompactionAttachmentReference(
        attachment_id=attachment_id,
        file_name=file_name,
        path=attachment_storage_ref(SESSION, attachment_id, file_name).removeprefix("tmp/"),
    )


def compaction_entry(
    entry_id: str, parent_id: str, first_kept: str, *, summary: str = "旧摘要", details=None
) -> CompactionEntry:
    return CompactionEntry(
        session_id=SESSION,
        id=entry_id,
        parent_id=parent_id,
        run_id="run",
        type="compaction",
        summary=summary,
        first_kept_entry_id=first_kept,
        tokens_before=1000,
        usage=USAGE,
        system_message=system_message(),
        details=details,
        created_at=tick(),
    )


def check_summary_validation() -> None:
    def result_message(**overrides) -> AssistantMessage:
        values = {
            "role": "assistant",
            "content": [TextContent(type="text", text="## Goal\n目标")],
            "api": "openai-completions",
            "provider": "test",
            "model": "test",
            "usage": USAGE,
            "stop_reason": "stop",
            "timestamp": tick(),
        }
        return AssistantMessage.model_validate({**values, **overrides})

    valid = C._validated_summary(result_message(), "历史摘要")
    assert valid.summary.startswith("## Goal") and valid.usage == USAGE
    with pytest.raises(C.CompactionSummaryError):
        C._validated_summary(
            result_message(stop_reason="error", error_message="boom"), "历史摘要"
        )
    with pytest.raises(C.CompactionSummaryError):
        C._validated_summary(result_message(stop_reason="length"), "历史摘要")
    with pytest.raises(C.CompactionSummaryError):
        C._validated_summary(
            result_message(
                stop_reason="aborted",
                usage=None,
                content=[],
            ),
            "历史摘要",
        )
    with pytest.raises(C.CompactionSummaryError):
        C._validated_summary(
            result_message(
                content=[
                    TextContent(type="text", text="摘要"),
                    ToolCall(type="toolCall", id="c1", name="read", arguments={}),
                ]
            ),
            "历史摘要",
        )
    with pytest.raises(C.CompactionSummaryError):
        C._validated_summary(result_message(content=[TextContent(type="text", text="   ")]), "历史摘要")
    with pytest.raises(ValidationError):
        result_message(stop_reason="stop", usage=None)
    print("摘要校验：error、length、aborted、工具调用、空结果与缺失用量拒绝通过")


def check_estimate_tokens() -> None:
    assert C.estimate_tokens(user("中文内容")) == 1
    assert C.estimate_tokens(user("中" * 400)) == 100
    plan = json.dumps(
        {"plan": [{"day": index, "exercises": ["深蹲", "卧推"] * 4} for index in range(300)]},
        ensure_ascii=False,
    )
    assert C.estimate_tokens(tool_result("c1", plan)) == (len(plan) + 3) // 4
    assert C.estimate_tokens(tool_result("c1", plan)) >= 2000
    image = UserMessage(
        role="user",
        content=[ImageContent(type="image", data="x", mime_type="image/png")],
        timestamp=tick(),
    )
    assert C.estimate_tokens(image) == C.ESTIMATED_IMAGE_CHARS // 4
    tool = Tool(
        name="read",
        description="读取文件",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )
    with_tools = system_message("系统", [tool])
    assert C.estimate_tokens(with_tools) > C.estimate_tokens(system_message("系统"))
    call = ToolCall(type="toolCall", id="c1", name="read", arguments={"path": "tmp/a.txt"})
    assert C.estimate_tokens(assistant("", (call,))) > C.estimate_tokens(assistant("", ()))
    thinking = AssistantMessage(
        role="assistant",
        content=[ThinkingContent(type="thinking", thinking="推理" * 50)],
        api="openai-completions",
        provider="test",
        model="test",
        usage=USAGE,
        stop_reason="stop",
        timestamp=tick(),
    )
    assert C.estimate_tokens(thinking) == (len("推理") * 50 + 3) // 4
    print("estimate_tokens：中文、长 JSON 计划、工具 schema、图片与思考计数通过")


def check_context_tokens() -> None:
    messages = [user("问题"), assistant("回答", usage=Usage(input=100, output=50, cache_read=10, cache_write=5, total_tokens=165))]
    estimate = C.estimate_context_tokens(messages)
    assert estimate.last_usage_index == 1
    assert estimate.usage_tokens == 165
    assert estimate.trailing_tokens == 0
    assert estimate.tokens == 165
    trailing = [*messages, user("追问")]
    estimate = C.estimate_context_tokens(trailing)
    assert estimate.usage_tokens == 165 and estimate.trailing_tokens == 1 and estimate.tokens == 166
    ignored = [user("问题"), assistant("回答", usage=USAGE, stop_reason="aborted"), user("追问")]
    estimate = C.estimate_context_tokens(ignored)
    assert estimate.last_usage_index is None and estimate.usage_tokens == 0
    assert estimate.tokens == sum(C.estimate_tokens(message) for message in ignored)
    zero = [user("问题"), assistant("回答", usage=Usage(input=0, output=0, cache_read=0, cache_write=0, total_tokens=0))]
    assert C.estimate_context_tokens(zero).last_usage_index is None

    window, reserve = 128000, 16384
    threshold = window - reserve
    assert C.should_compact(threshold, window, reserve) is False
    assert C.should_compact(threshold + 1, window, reserve) is True
    with pytest.raises(ValueError):
        C.should_compact(-1, window, reserve)
    with pytest.raises(ValueError):
        C.should_compact(0, window, 0)

    C.check_budget(128000, 16384, SMALL)
    with pytest.raises(C.ContextBudgetExceeded):
        C.check_budget(8192, 8192, SMALL)
    with pytest.raises(C.ContextBudgetExceeded):
        C.check_budget(20000, 16384, C.CompactionSettings())
    print("上下文用量：适用 usage、估算路径、阈值边界与预算可行性通过")


def check_projection_invalidation() -> None:
    path = [entry("sys", None, system_message()), entry("u1", "sys", user("问题"))]
    path.append(entry("a1", "u1", assistant("回答", usage=Usage(input=9, output=9, cache_read=0, cache_write=0, total_tokens=999))))
    context = C.build_effective_context(path)
    assert [item for item in context.source_entry_ids] == ["sys", "u1", "a1"]
    estimate = C.estimate_projected_context_tokens(context, path)
    assert estimate.last_usage_index == 2 and estimate.usage_tokens == 999

    with_compaction = [
        entry("sys", None, system_message()),
        entry("u1", "sys", user("问题")),
        entry("a1", "u1", assistant("回答", usage=Usage(input=9, output=9, cache_read=0, cache_write=0, total_tokens=999))),
    ]
    context = C.EffectiveContext(
        messages=[*with_compaction[1].messages, *with_compaction[2].messages],
        source_entry_ids=["u1", "a1"],
    )
    fresh = [*with_compaction, compaction_entry("c1", "a1", "u1")]
    invalid = C.estimate_projected_context_tokens(context, fresh)
    assert invalid.last_usage_index is None and invalid.usage_tokens == 0
    assert invalid.tokens == sum(C.estimate_tokens(message) for message in context.messages)
    print("投影用量基准：压缩之后的 usage 失效并重新估算通过")


def check_catalog_resolution() -> None:
    catalog = MC.load_capability_catalog()
    assert len(catalog.models) == 32
    assert sum(len(entries) for entries in catalog.models.values()) == 1074
    assert sum(len(mapping) for mapping in catalog.endpoints.values()) == 32

    hit = MC.resolve_capabilities(
        catalog, "openai-completions", "https://api.xiaomimimo.com/v1", "mimo-v2.6-pro"
    )
    assert hit.context_window == 1048576 and hit.max_tokens == 131072

    normalized = MC.resolve_capabilities(
        catalog, "openai-completions", "https://API.Xiaomimimo.com/v1/", "mimo-v2.6-pro"
    )
    assert normalized == hit

    resolved = MC.resolve_model_spec(
        ModelConfig(
            api="anthropic-messages",
            base_url="https://api.anthropic.com/",
            model="claude-fable-5",
            provider="anthropic",
            api_key="k",
        )
    )
    assert resolved.context_window == 1000000 and resolved.max_tokens == 128000

    assert MC.resolve_capabilities(catalog, "openai-completions", "https://api.xiaomimimo.com/v1", "MIMO-V2.6-PRO").context_window == MC.DEFAULT_CONTEXT_WINDOW
    assert MC.resolve_capabilities(catalog, "openai-completions", "https://api.xiaomimimo.com/v1", "absent").max_tokens == MC.DEFAULT_MAX_TOKENS
    assert MC.resolve_capabilities(catalog, "openai-completions", "https://example.invalid/v1", "mimo-v2.6-pro") == MC.ModelCapability()
    assert MC.resolve_capabilities(catalog, "anthropic-messages", "https://api.xiaomimimo.com/v1", "mimo-v2.6-pro") == MC.ModelCapability()
    colliding = "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"
    assert MC.resolve_capabilities(catalog, "openai-completions", colliding, "qwen3.7-max") == MC.ModelCapability()
    print("能力目录：命中、未知端点、未收录模型、大小写精确匹配、端点冲突与规范化通过")


def check_catalog_defaults_and_errors() -> None:
    partial = MC.CapabilityCatalog(
        models={"p": {"m": MC.ModelCapability(context_window=4096)}},
        endpoints={"openai-completions": {"https://p.example/v1": "p"}},
    )
    assert MC.resolve_capabilities(partial, "openai-completions", "https://p.example/v1", "m").max_tokens == MC.DEFAULT_MAX_TOKENS
    other = MC.CapabilityCatalog(
        models={"p": {"m": MC.ModelCapability(max_tokens=512)}},
        endpoints={"openai-completions": {"https://p.example/v1": "p"}},
    )
    assert MC.resolve_capabilities(other, "openai-completions", "https://p.example/v1", "m").context_window == MC.DEFAULT_CONTEXT_WINDOW

    for payload in (
        {"context_window": None},
        {"context_window": True},
        {"context_window": 0},
        {"context_window": -1},
        {"context_window": "4096"},
        {"max_tokens": 0},
        {"unknown": 1},
    ):
        with pytest.raises(ValidationError):
            MC.ModelCapability.model_validate({**payload})

    root = TEMP_ROOT / "catalog-temp" / uuid4().hex
    root.mkdir(parents=True)
    original = MC.DATA_ROOT
    try:
        MC.DATA_ROOT = root
        with pytest.raises(MC.CapabilityCatalogError):
            MC.load_capability_catalog()

        (root / MC.MODELS_FILE).write_text("{", encoding="utf-8")
        (root / MC.ENDPOINTS_FILE).write_text("{}", encoding="utf-8")
        with pytest.raises(MC.CapabilityCatalogError):
            MC.load_capability_catalog()

        (root / MC.MODELS_FILE).write_text(
            json.dumps({"p": {"m": {"context_window": None}}}), encoding="utf-8"
        )
        with pytest.raises(MC.CapabilityCatalogError):
            MC.load_capability_catalog()

        (root / MC.MODELS_FILE).write_text(
            json.dumps({"p": {"m": {"context_window": 4096, "max_tokens": 512}}}),
            encoding="utf-8",
        )
        (root / MC.ENDPOINTS_FILE).write_text(
            json.dumps({"openai-completions": {"https://p.example/v1": "ghost"}}),
            encoding="utf-8",
        )
        with pytest.raises(MC.CapabilityCatalogError):
            MC.load_capability_catalog()

        (root / MC.ENDPOINTS_FILE).write_text(
            json.dumps({"openai-completions": {"https://p.example/v1": "p"}}),
            encoding="utf-8",
        )
        frozen = MC.resolve_model_spec(
            ModelConfig(
                api="openai-completions",
                base_url="https://p.example/v1",
                model="m",
                provider="p",
                api_key="k",
            )
        )
        assert frozen.context_window == 4096 and frozen.max_tokens == 512
        (root / MC.MODELS_FILE).write_text(
            json.dumps({"p": {"m": {"context_window": 8192, "max_tokens": 1024}}}),
            encoding="utf-8",
        )
        updated = MC.resolve_model_spec(
            ModelConfig(
                api="openai-completions",
                base_url="https://p.example/v1",
                model="m",
                provider="p",
                api_key="k",
            )
        )
        assert updated.context_window == 8192 and frozen.context_window == 4096
    finally:
        MC.DATA_ROOT = original
    print("能力目录：字段独立默认、非法能力、损坏 JSON、读取失败与快照固定通过")


def build_path_entries() -> list:
    return [
        entry("sys", None, system_message()),
        entry("u1", "sys", user("问题一" * 20)),
        entry("a1", "u1", assistant("回答一" * 20)),
        entry("u2", "a1", user("问题二" * 20)),
        entry("a2", "u2", assistant("回答二" * 20)),
        entry("u3", "a2", user("问题三" * 400)),
        entry("a3", "u3", assistant("回答三")),
    ]


def check_user_boundary() -> None:
    path = build_path_entries()
    preparation = C.prepare_compaction(path, 128000, 16384, SMALL)
    assert preparation.first_kept_entry_id == "u3"
    assert preparation.is_split_turn is False
    assert preparation.turn_prefix_messages == []
    assert preparation.previous_summary is None
    assert [message.role for message in preparation.messages_to_summarize] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    summarized_text = "".join(
        message.content for message in preparation.messages_to_summarize if isinstance(message, UserMessage)
    )
    assert "问题一" in summarized_text and "问题二" in summarized_text
    assert all(
        not (isinstance(message, UserMessage) and "问题三" in message.content)
        for message in preparation.messages_to_summarize
    )
    assert preparation.tokens_before > 0
    print("切点：用户边界切分、必要用户原文进入总结范围通过")


def check_split_turn() -> None:
    path = [
        entry("sys", None, system_message()),
        entry("u1", "sys", user("执行长任务")),
        entry("a1", "u1", assistant("步骤" * 800)),
    ]
    preparation = C.prepare_compaction(path, 128000, 16384, SMALL)
    assert preparation.is_split_turn is True
    assert preparation.first_kept_entry_id == "a1"
    assert preparation.messages_to_summarize == []
    assert [message.content for message in preparation.turn_prefix_messages] == ["执行长任务"]
    print("切点：单次长任务内部切分与任务前缀准备通过")


def check_tool_batch_integrity() -> None:
    calls = (
        ToolCall(type="toolCall", id="c1", name="echo", arguments={"n": 1}),
        ToolCall(type="toolCall", id="c2", name="echo", arguments={"n": 2}),
    )
    path = [
        entry("sys", None, system_message()),
        entry("u1", "sys", user("批量调用")),
        entry("a1", "u1", assistant("", calls)),
        entry("t1", "a1", tool_result("c1", "结果一")),
        entry("t2", "a1", tool_result("c2", "结果二")),
        entry("u2", "t2", user("下一轮" * 400)),
        entry("a2", "u2", assistant("收尾")),
    ]
    preparation = C.prepare_compaction(path, 128000, 16384, SMALL)
    assert preparation.first_kept_entry_id == "u2"
    kept = [message.role for message in preparation.messages_to_summarize]
    assert kept == ["user", "assistant", "toolResult", "toolResult"]
    summarized_calls = [
        block.id
        for message in preparation.messages_to_summarize
        if isinstance(message, AssistantMessage)
        for block in message.content
        if isinstance(block, ToolCall)
    ]
    assert summarized_calls == ["c1", "c2"]

    for keep in range(1, 4000, 137):
        cut = C.find_cut_point(path, 0, len(path), keep)
        assert path[cut.first_kept_entry_index].messages[0].role in {"user", "assistant"}
    print("切点：完整并行工具批次不拆分、切点不落在工具结果通过")


def check_repeated_compaction() -> None:
    path = [
        entry("sys", None, system_message()),
        entry("u1", "sys", user("历史问题")),
        entry("a1", "u1", assistant("历史回答")),
        compaction_entry("c1", "a1", "a1"),
        entry("u2", "c1", user("问题二" * 20)),
        entry("a2", "u2", assistant("回答二" * 20)),
        entry("u3", "a2", user("问题三" * 400)),
        entry("a3", "u3", assistant("回答三")),
    ]
    preparation = C.prepare_compaction(path, 128000, 16384, SMALL)
    assert preparation.previous_summary == "旧摘要"
    assert preparation.first_kept_entry_id == "u3"
    summarized = [
        message
        for message in preparation.messages_to_summarize
        if isinstance(message, (UserMessage, AssistantMessage))
    ]
    texts = [str(getattr(message, "content", "")) for message in summarized]
    assert any("问题二" in item for item in texts)
    assert any("历史回答" in item for item in texts)
    assert not any("历史问题" in item for item in texts)
    context = C.build_effective_context(path)
    assert context.source_entry_ids == ["c1", "c1", "a1", "u2", "a2", "u3", "a3"]
    assert len(context.messages) == len(context.source_entry_ids)
    assert C._content_text(context.messages[2].content) == "历史回答"
    print("切点：有效历史保留区间、旧摘要滚动更新、不重新总结已覆盖前缀通过")


def check_no_summarizable_and_no_cut_point() -> None:
    with pytest.raises(C.NoSummarizableHistory):
        C.prepare_compaction([], 128000, 16384, SMALL)
    closed = [
        entry("sys", None, system_message()),
        entry("u1", "sys", user("问题")),
        entry("a1", "u1", assistant("回答")),
        compaction_entry("c1", "a1", "u1"),
    ]
    with pytest.raises(C.NoSummarizableHistory):
        C.prepare_compaction(closed, 128000, 16384, SMALL)
    only_tool = [
        entry("sys", None, system_message()),
        entry("t1", "sys", tool_result("c1", "结果")),
    ]
    with pytest.raises(C.NoCutPoint):
        C.prepare_compaction(only_tool, 128000, 16384, SMALL)
    with pytest.raises(C.ContextBudgetExceeded):
        C.prepare_compaction(build_path_entries(), 8192, 8192, SMALL)
    print("切点：无合法切点、无可总结前缀与必要输入超预算通过")


def check_attachments() -> None:
    previous = compaction_entry(
        "c1",
        "a1",
        "a1",
        details=CompactionDetails(attachments=[reference(ATT_A)]),
    )
    path = [
        entry("sys", None, system_message()),
        entry("u1", "sys", user("问题一" * 20), attachments=[attachment(ATT_A)]),
        entry("a1", "u1", assistant("回答一" * 20)),
        previous,
        entry("u2", "c1", user("问题二" * 20), attachments=[attachment(ATT_B, "plan.txt")]),
        entry("a2", "u2", assistant("回答二" * 20)),
        entry("u3", "a2", user("问题三" * 400)),
        entry("a3", "u3", assistant("回答三")),
    ]
    preparation = C.prepare_compaction(path, 128000, 16384, SMALL)
    assert preparation.first_kept_entry_id == "u3"
    assert [item.attachment_id for item in preparation.attachments] == [ATT_A, ATT_B]
    assert all(item.path.startswith("sessions/") for item in preparation.attachments)
    assert reference(ATT_A) in preparation.attachments
    assert reference(ATT_B, "plan.txt") in preparation.attachments
    assert all("tmp/" not in item.path for item in preparation.attachments)
    print("附件：真实元数据路径、旧摘要累计与去重通过")


def check_serialization() -> None:
    plan = json.dumps({"plan": [{"day": 1, "items": ["深蹲"] * 30}]}, ensure_ascii=False)
    call = ToolCall(type="toolCall", id="c1", name="echo", arguments={"path": "tmp/a.txt"})
    messages = [
        system_message(),
        user("你好"),
        assistant("回答", (call,)),
        tool_result("c1", plan),
    ]
    serialized = C.serialize_conversation(messages)
    assert "[User]: 你好" in serialized
    assert "[Assistant]: 回答" in serialized
    assert '[Assistant tool calls]: echo(path="tmp/a.txt")' in serialized
    assert plan in serialized
    assert serialized.count("[Tool result]:") == 1
    assert "系统指令" not in serialized
    tokens = C.estimate_summary_input_tokens(messages)
    assert tokens > 0
    with_previous = C.estimate_summary_input_tokens(messages, previous_summary="旧摘要" * 100)
    assert with_previous > tokens
    assert C.estimate_summary_input_tokens(messages, turn_prefix=True) > 0
    print("序列化：角色标记、JSON、完整工具结果与提示词开销计数通过")


def check_summary_limits() -> None:
    assert C._summary_max_tokens(16384, 16384, C.HISTORY_SUMMARY_RATIO) == 13107
    assert C._summary_max_tokens(16384, 16384, C.TURN_PREFIX_SUMMARY_RATIO) == 8192
    assert C._summary_max_tokens(16384, 4096, C.HISTORY_SUMMARY_RATIO) == 4096
    with pytest.raises(ValueError):
        C._summary_max_tokens(16384, 0, C.HISTORY_SUMMARY_RATIO)
    combined = C._combine_usage(
        Usage(input=10, output=5, cache_read=1, cache_write=0, total_tokens=16),
        Usage(input=20, output=8, cache_read=2, cache_write=1, total_tokens=31),
    )
    assert combined.input == 30 and combined.output == 13 and combined.total_tokens == 47

    messages = [user("摘要预算问题" * 100)]
    prompt = C._summary_prompt(messages, previous_summary="旧摘要", turn_prefix=False)
    input_tokens = C.estimate_summary_input_tokens(messages, previous_summary="旧摘要")
    assert input_tokens == C._summary_input_tokens(prompt)
    output_tokens = C._summary_max_tokens(
        C.RESERVE_TOKENS, 16384, C.HISTORY_SUMMARY_RATIO
    )

    def spec(context_window: int) -> ModelSpec:
        return ModelSpec(
            api="openai-completions",
            provider="test",
            id="test",
            base_url="https://example.invalid/v1",
            context_window=context_window,
            max_tokens=16384,
        )

    C._check_summary_budget(
        prompt, spec(input_tokens + output_tokens), output_tokens, "历史摘要"
    )
    with pytest.raises(C.ContextBudgetExceeded):
        C._check_summary_budget(
            prompt, spec(input_tokens + output_tokens - 1), output_tokens, "历史摘要"
        )
    print("摘要预算：输出额度受模型能力限制、输入加输出窗口边界、用量合并通过")


async def scenario_real_path() -> None:
    root = TEMP_ROOT / "real-path" / uuid4().hex
    root.mkdir(parents=True)
    database = await open_database(root / "database.sqlite")
    repository = SqliteSessionRepository(database)
    service = SessionService(repository)
    try:
        await service.create_session(SESSION, "压缩核心")
        send = await service.accept_send(
            SendCommand(
                operation_id=str(uuid4()),
                session_id=SESSION,
                request=SendRequest(text="开始训练计划" * 30),
            ),
            system_message=system_message(),
        )
        run = send.run
        request_entry = await service.get_entry(SESSION, run.request_entry_id)
        await service.append_entry(
            SessionMessageEntry(
                session_id=SESSION,
                id="a1",
                parent_id=request_entry.id,
                run_id=run.id,
                type="message",
                messages=[assistant("计划已生成")],
                created_at=tick(),
            )
        )
        entries = await service.list_entries(SESSION)
        path = build_session_path(SESSION, entries, "a1")
        preparation = C.prepare_compaction(
            path, 128000, 16384, C.CompactionSettings(reserve_tokens=16384, keep_recent_tokens=1)
        )
        assert preparation.first_kept_entry_id in {item.id for item in entries}
        assert preparation.first_kept_entry_id == "a1"
        assert preparation.tokens_before > 0
    finally:
        await database.close()


def check_real_path() -> None:
    asyncio.run(scenario_real_path())
    print("隔离真实 SQLite：真实来源节点 ID 与预算计数通过")


def check() -> None:
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    check_summary_validation()
    check_estimate_tokens()
    check_context_tokens()
    check_projection_invalidation()
    check_catalog_resolution()
    check_catalog_defaults_and_errors()
    check_user_boundary()
    check_split_turn()
    check_tool_batch_integrity()
    check_repeated_compaction()
    check_no_summarizable_and_no_cut_point()
    check_attachments()
    check_serialization()
    check_summary_limits()
    check_real_path()
    print("压缩核心：目录、预算、切点、序列化与真实路径自检通过")


if __name__ == "__main__":
    check()
