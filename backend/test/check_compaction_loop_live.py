import asyncio
import json
import time
from pathlib import Path
from uuid import uuid4

from app.agent.compaction import (
    DEFAULT_SETTINGS,
    ContextBudgetExceeded,
    estimate_context_tokens,
    estimate_summary_input_tokens,
    generate_compaction_summary,
    prepare_compaction,
    should_compact,
)
from app.agent.prompts import SYSTEM_PROMPT
from app.ai.messages import SystemMessage, UserMessage
from app.ai.model_capabilities import resolve_model_spec
from app.domain.session.models import SessionEntry, SessionMessageEntry
from app.model_config import load_model_config

ROOT = Path(__file__).resolve().parents[2] / "tmp" / "agent-compaction-loop-part-2"

# 英文填充：chars/4 估算与真实 token 接近，可在生产阈值下保持真实请求落在模型窗口内。
FILLER = "the quick brown fox jumps over the lazy dog and keeps training hard. "
SUMMARIZED_CHARS = 400_000
RETAINED_CHARS = 95_000


def build_path(session_id: str) -> list[SessionEntry]:
    def user(entry_id: str, parent_id: str | None, chars: int, timestamp: int):
        text = FILLER * (chars // len(FILLER) + 1)
        return SessionMessageEntry(
            session_id=session_id, id=entry_id, parent_id=parent_id, run_id=None,
            type="message",
            messages=[UserMessage(role="user", content=text[:chars], timestamp=timestamp)],
            created_at=timestamp,
        )

    return [
        SessionMessageEntry(
            session_id=session_id, id="system", parent_id=None, run_id=None, type="message",
            messages=[SystemMessage(role="system", content=SYSTEM_PROMPT, tools_added=[], timestamp=1)],
            created_at=1,
        ),
        user("u1", "system", SUMMARIZED_CHARS, 2),
        user("u2", "u1", RETAINED_CHARS, 3),
    ]


async def measure() -> dict:
    config = load_model_config()
    spec = resolve_model_spec(config)
    settings = DEFAULT_SETTINGS
    assert settings.reserve_tokens == 16384 and settings.keep_recent_tokens == 20000
    session_id = str(uuid4())
    path = build_path(session_id)
    projection = estimate_context_tokens([entry.messages[0] for entry in path])
    assert should_compact(projection.tokens, spec.context_window, settings.reserve_tokens), (
        projection.tokens, spec.context_window
    )
    preparation = prepare_compaction(path, spec.context_window, spec.max_tokens, settings)
    estimated_summary_input = estimate_summary_input_tokens(
        preparation.messages_to_summarize,
        previous_summary=preparation.previous_summary,
        turn_prefix=preparation.is_split_turn,
    )
    started = time.perf_counter()
    result = await generate_compaction_summary(
        preparation, spec, api_key=config.api_key, settings=settings
    )
    latency_ms = int((time.perf_counter() - started) * 1000)
    assert result.summary.strip() and result.usage.total_tokens > 0
    return {
        "capability_source": {
            "provider": config.provider, "model": config.model,
            "context_window": spec.context_window, "max_tokens": spec.max_tokens,
        },
        "production_settings": {
            "reserve_tokens": settings.reserve_tokens,
            "keep_recent_tokens": settings.keep_recent_tokens,
            "threshold": spec.context_window - settings.reserve_tokens,
        },
        "before": {
            "estimated_context_tokens": projection.tokens,
            "usage_tokens": projection.usage_tokens,
            "trailing_tokens": projection.trailing_tokens,
        },
        "preparation": {
            "first_kept_entry_id": preparation.first_kept_entry_id,
            "is_split_turn": preparation.is_split_turn,
            "estimated_tokens_before": preparation.tokens_before,
            "estimated_summary_input_tokens": estimated_summary_input,
            "summarized_messages": len(preparation.messages_to_summarize),
        },
        "summary": {
            "chars": len(result.summary),
            "usage": result.usage.model_dump(),
            # 真实提示规模含缓存命中：input + cache_read 才是实际送入的 token。
            "input_error_ratio": round(
                (result.usage.input + result.usage.cache_read) / estimated_summary_input, 4
            ) if estimated_summary_input else None,
            "latency_ms": latency_ms,
        },
        "requests": {"summary_requests": 1, "compactions": 1, "overflow_recoveries": 0},
    }


def check() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    try:
        evidence = asyncio.run(measure())
    except ContextBudgetExceeded as error:
        (ROOT / "live-evidence.json").write_text(
            json.dumps({"blocked": str(error)}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        raise
    (ROOT / "live-evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = evidence["summary"]
    print(
        "PASS: 生产阈值真实压缩测量："
        f"model={evidence['capability_source']['model']} "
        f"threshold={evidence['production_settings']['threshold']} "
        f"estimated_before={evidence['before']['estimated_context_tokens']} "
        f"summary_input(est={evidence['preparation']['estimated_summary_input_tokens']} "
        f"real={summary['usage']['input'] + summary['usage']['cache_read']}) "
        f"output={summary['usage']['output']} latency={summary['latency_ms']}ms"
    )


if __name__ == "__main__":
    check()
