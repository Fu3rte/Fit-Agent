import asyncio
import json
import time
from pathlib import Path
from uuid import uuid4

from app.agent import compaction as C
from app.agent.budget import RequestBudget
from app.agent.prompts import SYSTEM_PROMPT
from app.ai.messages import SystemMessage, UserMessage
from app.ai.model_capabilities import resolve_model_spec
from app.domain.session.models import SessionEntry, SessionMessageEntry
from app.model_config import load_model_config

ROOT = Path(__file__).resolve().parents[2] / "tmp" / "agent-compaction-task3"
EVIDENCE = ROOT / "budget-counting-live.json"

# 英文填充：真实 token 与 chars/3.5 接近，可在生产阈值下把完整请求控制在窗口内。
FILLER = "the quick brown fox jumps over the lazy dog and keeps training hard. "
HISTORY_CHARS = 20_000
RETAINED_CHARS = 2_000
LIVE_SETTINGS = C.CompactionSettings(
    reserve_tokens=C.RESERVE_TOKENS, keep_recent_tokens=400
)


def build_path(session_id: str) -> list[SessionEntry]:
    def user(entry_id: str, parent_id: str | None, chars: int, timestamp: int):
        text = FILLER * (chars // len(FILLER) + 1)
        return SessionMessageEntry(
            session_id=session_id,
            id=entry_id,
            parent_id=parent_id,
            run_id=None,
            type="message",
            messages=[UserMessage(role="user", content=text[:chars], timestamp=timestamp)],
            created_at=timestamp,
        )

    return [
        SessionMessageEntry(
            session_id=session_id,
            id="system",
            parent_id=None,
            run_id=None,
            type="message",
            messages=[SystemMessage(role="system", content=SYSTEM_PROMPT, tools_added=[], timestamp=1)],
            created_at=1,
        ),
        user("u1", "system", HISTORY_CHARS, 2),
        user("u2", "u1", RETAINED_CHARS, 3),
    ]


async def measure() -> dict:
    config = load_model_config()
    spec = resolve_model_spec(config)
    session_id = str(uuid4())
    path = build_path(session_id)
    preparation = C.prepare_compaction(path, spec.context_window, spec.max_tokens, LIVE_SETTINGS)
    estimated_input = C.estimate_summary_input_tokens(
        preparation.messages_to_summarize,
        previous_summary=preparation.previous_summary,
        turn_prefix=preparation.is_split_turn,
    )
    budget = RequestBudget()
    started = time.perf_counter()
    result = await C.generate_compaction_summary(
        preparation, spec, api_key=config.api_key, settings=LIVE_SETTINGS, budget=budget
    )
    latency_ms = int((time.perf_counter() - started) * 1000)
    assert result.summary.strip(), "摘要为空"
    assert result.usage.total_tokens > 0, "缺少真实用量"
    assert budget.used == 1, "单个摘要请求应占用一个名额"
    real_input = result.usage.input + result.usage.cache_read + result.usage.cache_write
    return {
        "model": {
            "api": config.api,
            "provider": config.provider,
            "id": config.model,
            "base_url": config.base_url,
            "context_window": spec.context_window,
            "max_tokens": spec.max_tokens,
        },
        "settings": {
            "reserve_tokens": LIVE_SETTINGS.reserve_tokens,
            "live_keep_recent_tokens": LIVE_SETTINGS.keep_recent_tokens,
            "production_keep_recent_tokens": C.KEEP_RECENT_TOKENS,
            "safety_margin_tokens": C.SAFETY_MARGIN_TOKENS,
        },
        "preparation": {
            "first_kept_entry_id": preparation.first_kept_entry_id,
            "is_split_turn": preparation.is_split_turn,
            "summarized_messages": len(preparation.messages_to_summarize),
            "turn_prefix_messages": len(preparation.turn_prefix_messages),
            "estimated_input_tokens": estimated_input,
        },
        "summary": {
            "chars": len(result.summary),
            "usage": result.usage.model_dump(),
            "real_input_tokens": real_input,
            "estimate_error_ratio": round(real_input / estimated_input, 4),
            "latency_ms": latency_ms,
        },
        "budget": {"used": budget.used, "limit": budget.limit},
    }


def check() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    evidence = asyncio.run(measure())
    EVIDENCE.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = evidence["summary"]
    print(
        "真实摘要计数："
        f"model={evidence['model']['id']} "
        f"used={evidence['budget']['used']}/{evidence['budget']['limit']} "
        f"est_input={evidence['preparation']['estimated_input_tokens']} "
        f"real_input={summary['real_input_tokens']} "
        f"output={summary['usage']['output']} "
        f"estimate_error={summary['estimate_error_ratio']} "
        f"latency={summary['latency_ms']}ms"
    )


if __name__ == "__main__":
    check()
