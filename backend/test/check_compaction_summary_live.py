import asyncio
import json
import time
from concurrent.futures import CancelledError
from pathlib import Path
from threading import Event

from app.agent import compaction as C
from app.ai.messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    Tool,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from app.ai.model_capabilities import resolve_model_spec
from app.domain.session.attachments import attachment_storage_ref
from app.domain.session.models import (
    AttachmentMetadata,
    CompactionAttachmentReference,
    CompactionDetails,
    CompactionEntry,
    SessionMessageEntry,
)
from app.model_config import load_model_config

BACKEND = Path(__file__).resolve().parents[1]
EVIDENCE = BACKEND.parent / "tmp" / "agent-compaction-core"
SESSION = "11111111-1111-4111-8111-111111111111"
ATTACHMENT = "22222222-2222-4222-8222-222222222222"
ATTACHMENT_NAME = "四个训练日.md"
PROPOSAL_ID = "PLAN-9f2c7d"

USAGE = Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2)
# 真实调用只需较小保留区间即可产生合法切点；生产固定 keep_recent_tokens 仍为 20000。
LIVE_SETTINGS = C.CompactionSettings(reserve_tokens=C.RESERVE_TOKENS, keep_recent_tokens=400)
SECTION_HEADINGS = (
    "goal",
    "constraints",
    "progress",
    "key decisions",
    "next steps",
    "critical context",
)

_clock = [2000]


def tick() -> int:
    _clock[0] += 1
    return _clock[0]


def business_system() -> SystemMessage:
    read_tool = Tool(
        name="get_pending_proposal",
        description="按 business_kind 与 proposal_id 读取待确认提案的完整内容。",
        parameters={
            "type": "object",
            "properties": {
                "business_kind": {"type": "string"},
                "proposal_id": {"type": "string"},
            },
            "required": ["business_kind", "proposal_id"],
            "additionalProperties": False,
        },
    )
    return SystemMessage(
        role="system",
        content="你是个人力量训练助手，按业务规则整理画像、计划与训练记录。",
        tools_added=[read_tool],
        timestamp=tick(),
    )


def entry(entry_id, parent_id, message, attachments=()) -> SessionMessageEntry:
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


def user(text: str) -> UserMessage:
    return UserMessage(role="user", content=text, timestamp=tick())


def assistant(text: str = "", calls: tuple[ToolCall, ...] = ()) -> AssistantMessage:
    content = [TextContent(type="text", text=text)] if text else []
    content.extend(calls)
    return AssistantMessage(
        role="assistant",
        content=content,
        api="openai-completions",
        provider="test",
        model="test",
        usage=USAGE,
        stop_reason="toolUse" if calls else "stop",
        timestamp=tick(),
    )


def tool_result(call_id: str, text: str) -> ToolResultMessage:
    return ToolResultMessage(
        role="toolResult",
        tool_call_id=call_id,
        tool_name="get_pending_proposal",
        content=[TextContent(type="text", text=text)],
        is_error=False,
        timestamp=tick(),
    )


def plan_attachment() -> AttachmentMetadata:
    return AttachmentMetadata(
        attachment_id=ATTACHMENT,
        session_id=SESSION,
        file_name=ATTACHMENT_NAME,
        size_bytes=128,
        storage_ref=attachment_storage_ref(SESSION, ATTACHMENT, ATTACHMENT_NAME),
        created_at=tick(),
    )


def plan_reference() -> CompactionAttachmentReference:
    return CompactionAttachmentReference(
        attachment_id=ATTACHMENT,
        file_name=ATTACHMENT_NAME,
        path=attachment_storage_ref(SESSION, ATTACHMENT, ATTACHMENT_NAME).removeprefix("tmp/"),
    )


def phase_one():
    plan_payload = json.dumps(
        {
            "proposal_id": PROPOSAL_ID,
            "days": [
                {"theme": "推", "exercises": ["哑铃卧推", "哑铃肩推", "弹力带夹胸"]},
                {"theme": "拉", "exercises": ["哑铃划船", "弹力带下拉"]},
                {"theme": "腿", "exercises": ["高脚杯深蹲", "罗马尼亚硬拉"]},
                {"theme": "休", "exercises": []},
            ],
        },
        ensure_ascii=False,
    )
    call = ToolCall(
        type="toolCall",
        id="call-plan",
        name="get_pending_proposal",
        arguments={"business_kind": "plan", "proposal_id": PROPOSAL_ID},
    )
    return [
        ("sys", business_system(), ()),
        (
            "u1",
            user(
                "我左膝半月板术后三个月，暂时不能负重深跳；家里没有杠铃，"
                "只有一对可调哑铃和弹力带；每周只能练四天。请给我一个四日循环。"
            ),
            (plan_attachment(),),
        ),
        (
            "a1",
            assistant("已记录限制：左膝术后、无杠铃、每周四练。先按推/拉/腿/休安排，避免跳跃与负重深蹲。"),
            (),
        ),
        ("u2", user("另外我特别想提高频率，直接改成一周六练可以吗？"), ()),
        ("a2", assistant("需要先确认恢复情况与器械条件再决定频率。"), ()),
        ("u3", user("算了，撤回六练的想法，保持每周四练，膝盖优先。"), ()),
        ("a3", assistant("已按每周四练继续，膝盖保护放在首位。"), ()),
        ("u4", user("把调整后的四日循环展示给我，先不要保存，我要确认。"), ()),
        ("a4", assistant("", (call,)), ()),
        ("t1", tool_result("call-plan", plan_payload), ()),
        ("a5", assistant("以上是待确认计划，请确认后再保存。"), ()),
    ]


def phase_two():
    return [
        ("u6", user("腿部那天的高脚杯深蹲我担心膝盖，能不能换成靠墙静蹲和腿弯举？"), ()),
        ("a6", assistant("可以替换为靠墙静蹲与腿弯举，下肢训练量基本持平。"), ()),
        ("u7", user("那就按这个改，请更新计划内容。"), ()),
        ("a7", assistant("已更新腿部训练日，等待你确认后保存。"), ()),
    ]


def chain(items, start_parent):
    entries = []
    parent = start_parent
    for entry_id, message, attachments in items:
        entries.append(entry(entry_id, parent, message, attachments))
        parent = entry_id
    return entries, parent


def build(name: str):
    if name == "split":
        tail = [
            ("u8", user("现在请说明改动理由。"), ()),
            ("a8", assistant("步骤说明" * 500), ()),
        ]
    else:
        tail = [
            ("u8", user("请把这次改动落成新的四日循环，并说明对训练量的影响。" * 65), ()),
            ("a8", assistant("改动已整理完成。"), ()),
        ]
    if name == "update":
        first, last = chain(phase_one(), None)
        previous = CompactionEntry(
            session_id=SESSION,
            id="c1",
            parent_id=last,
            run_id="run",
            type="compaction",
            summary=previous_summary(),
            first_kept_entry_id="u4",
            tokens_before=5000,
            usage=USAGE,
            system_message=business_system(),
            details=CompactionDetails(attachments=[plan_reference()]),
            created_at=tick(),
        )
        second, _ = chain(phase_two() + tail, "c1")
        return [*first, previous, *second]
    first, last = chain(phase_one(), None)
    second, _ = chain(phase_two() + tail, last)
    return [*first, *second]


def previous_summary() -> str:
    return (
        "## Goal\n完成四日循环计划的确认与保存。\n\n"
        "## Constraints & Preferences\n"
        "- 左膝半月板术后三个月，避免跳跃与负重深蹲。\n"
        "- 无杠铃，仅有可调哑铃与弹力带；每周四练。\n\n"
        "## Progress\n### Done\n- [x] 整理限制与四日循环结构\n"
        "### In Progress\n- [ ] 等待用户确认四日循环\n"
        "### Blocked\n- 无\n\n"
        "## Key Decisions\n- **保持每周四练**：膝盖优先，撤回六练提议。\n\n"
        "## Next Steps\n1. 确认后保存计划。\n\n"
        f"## Critical Context\n- 待确认提案 {PROPOSAL_ID}；用户已撤回六练要求。"
    )


model_key = ""


def run_scenario(name: str, model) -> dict:
    entries = build(name)
    preparation = C.prepare_compaction(
        entries, model.context_window, model.max_tokens, LIVE_SETTINGS
    )
    history_input = C.estimate_summary_input_tokens(
        preparation.messages_to_summarize,
        previous_summary=preparation.previous_summary,
    )
    turn_prefix_input = (
        C.estimate_summary_input_tokens(
            preparation.turn_prefix_messages, turn_prefix=True
        )
        if preparation.turn_prefix_messages
        else 0
    )
    estimated_input = history_input + turn_prefix_input
    history_quota = C._summary_max_tokens(
        LIVE_SETTINGS.reserve_tokens, model.max_tokens, C.HISTORY_SUMMARY_RATIO
    )
    turn_prefix_quota = C._summary_max_tokens(
        LIVE_SETTINGS.reserve_tokens, model.max_tokens, C.TURN_PREFIX_SUMMARY_RATIO
    )
    started = time.perf_counter()
    result = asyncio.run(
        C.generate_compaction_summary(preparation, model, api_key=model_key)
    )
    latency_ms = int((time.perf_counter() - started) * 1000)
    lowered = result.summary.lower()
    missing = [heading for heading in SECTION_HEADINGS if heading not in lowered]
    assert not missing, (name, missing, result.summary[:600])
    assert result.usage.total_tokens > 0, name
    for keyword in ("膝", "哑铃", PROPOSAL_ID, "四日"):
        assert keyword in result.summary, (name, keyword, result.summary[:600])
    actual_input = result.usage.input + result.usage.cache_read + result.usage.cache_write
    context = C.build_effective_context(entries)
    return {
        "messages_to_summarize": len(preparation.messages_to_summarize),
        "turn_prefix_messages": len(preparation.turn_prefix_messages),
        "is_split_turn": preparation.is_split_turn,
        "previous_summary": preparation.previous_summary is not None,
        "attachments": [item.model_dump() for item in preparation.attachments],
        "first_kept_entry_id": preparation.first_kept_entry_id,
        "effective_context_chars_over_4": sum(
            C.estimate_tokens(message) for message in context.messages
        ),
        "tokens_before_usage_basis": preparation.tokens_before,
        "estimated_input_tokens": estimated_input,
        "context_window": model.context_window,
        "history_input_tokens": history_input,
        "history_output_quota": history_quota,
        "turn_prefix_input_tokens": turn_prefix_input,
        "turn_prefix_output_quota": turn_prefix_quota,
        "history_budget_headroom": (
            model.context_window - history_quota - history_input
            if preparation.messages_to_summarize
            else None
        ),
        "turn_prefix_budget_headroom": (
            model.context_window - turn_prefix_quota - turn_prefix_input
            if preparation.turn_prefix_messages
            else None
        ),
        "actual_input_tokens": actual_input,
        "estimate_error": (
            abs(estimated_input - actual_input) / actual_input if actual_input else None
        ),
        "usage": result.usage.model_dump(),
        "request_count": 2 if preparation.turn_prefix_messages else 1,
        "latency_ms": latency_ms,
        "summary_chars": len(result.summary),
        "summary": result.summary,
    }


def run_cancellation(model) -> dict:
    preparation = C.prepare_compaction(
        build("plain"), model.context_window, model.max_tokens, LIVE_SETTINGS
    )
    signal = Event()
    signal.set()
    started = time.perf_counter()
    try:
        asyncio.run(
            C.generate_compaction_summary(
                preparation, model, api_key=model_key, signal=signal
            )
        )
    except CancelledError:
        outcome = "cancelled"
    else:
        outcome = "unexpected-success"
    return {
        "outcome": outcome,
        "request_count": 0,
        "latency_ms": int((time.perf_counter() - started) * 1000),
    }


def check() -> None:
    global model_key
    config = load_model_config()
    model = resolve_model_spec(config)
    model_key = config.api_key
    matched = (model.context_window, model.max_tokens) != (
        128000,
        16384,
    )
    evidence = {
        "source": {
            "pi_ai_version": "1.1.0",
            "copied_at": "2026-10-10",
            "note": "能力目录为 Pi 数据副本；stepfun 端点不在目录内，使用默认能力。",
        },
        "model": {
            "api": config.api,
            "provider": config.provider,
            "id": config.model,
            "base_url": config.base_url,
            "context_window": model.context_window,
            "max_tokens": model.max_tokens,
            "capability_source": "catalog" if matched else "default",
        },
        "settings": {
            "reserve_tokens": LIVE_SETTINGS.reserve_tokens,
            "live_keep_recent_tokens": LIVE_SETTINGS.keep_recent_tokens,
            "production_keep_recent_tokens": C.KEEP_RECENT_TOKENS,
        },
        "scenarios": {},
        "cancellation": run_cancellation(model),
        "cost": "缺少供应商单价，仅记录 token",
        "notes": [
            "历史为合成夹具，助手 usage 为固定占位值；tokens_before_usage_basis 反映该占位基准，上下文规模以 effective_context_chars_over_4 为准。",
            "estimate_error 为 chars/4 估算相对真实输入 token 的偏差，中文对话偏低约 2 倍。",
            "每次摘要调用前按 estimate_summary_input_tokens + 输出额度 <= context_window 校验；history/turn_prefix_budget_headroom 为对应调用的窗口余量。",
        ],
        "gaps": [
            "缺用量、工具调用、error、length 终态无法由真实服务稳定触发；由 check_compaction_core 的 _validated_summary 纯逻辑校验覆盖。",
            "取消仅覆盖请求前信号，未覆盖生成中途取消。",
            "超预算拒绝路径由 check_compaction_summary_budget 的确定性用例覆盖，真实服务未触发。",
        ],
    }
    for name in ("plain", "update", "split"):
        evidence["scenarios"][name] = run_scenario(name, model)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "compaction-summary-live.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("真实摘要：普通历史、旧摘要更新、任务前缀、关键约束保留、真实用量与请求前取消通过")
    for name, record in evidence["scenarios"].items():
        usage = record["usage"]
        print(
            f"  {name}: requests={record['request_count']} "
            f"input={usage['input']} cache_read={usage['cache_read']} "
            f"cache_write={usage['cache_write']} output={usage['output']} "
            f"latency={record['latency_ms']}ms "
            f"estimate_error={record['estimate_error']}"
        )
    print(f"  取消: {evidence['cancellation']}")


if __name__ == "__main__":
    check()
