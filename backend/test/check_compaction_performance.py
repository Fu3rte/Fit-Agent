import asyncio
import json
import os
import time
from pathlib import Path
from statistics import median
from uuid import uuid4

from app.agent.compaction import (
    DEFAULT_SETTINGS,
    estimate_context_tokens,
    estimate_summary_input_tokens,
    prepare_compaction,
)
from app.ai.model_capabilities import resolve_model_spec
from app.domain.session.models import CompactionEntry
from app.interfaces.http import active, app
from app.model_config import load_model_config
from test.check_compaction_joint import PARTS, call, seed
from test.check_http import events, wait_idle
from test.regression_support import (
    Server,
    client,
    install_test_model_config,
    patch_default_database,
)

ROOT = Path(__file__).resolve().parents[2] / "tmp" / "agent-compaction-joint-acceptance"
REPS = 3
TRIGGER = "基于以上历史继续，请只用一句话确认你能看到最近的保留原文。"


def submit(http, session_id: str):
    body = {"session_id": session_id, "operation_id": str(uuid4()), "request": TRIGGER}
    events_named = []
    started = time.perf_counter()
    with http.stream("POST", "/api/agent/run", json=body) as response:
        assert response.status_code == 200, response.text
        request_id = response.headers["X-Request-Entry-ID"]
        for event in events(response):
            events_named.append((time.perf_counter(), event))
    wait_idle()
    finished = time.perf_counter()
    return request_id, events_named, started, finished


def first_after(events_named, event_name, after_index):
    for index in range(after_index + 1, len(events_named)):
        if events_named[index][1]["event"] == event_name:
            return index, events_named[index][0]
    return None, None


def measure(http, spec, settings, ids: dict) -> dict:
    session_id = ids["session"]
    service = app.state.session_service
    loop = app.state.loop

    projection_pre = call(service.get_projection(session_id, ids["leaf"]), loop)
    pre_messages_tokens = estimate_context_tokens(projection_pre.context.messages).tokens
    preparation = prepare_compaction(
        projection_pre.path, spec.context_window, spec.max_tokens, settings
    )
    estimated_summary_input = estimate_summary_input_tokens(
        preparation.messages_to_summarize,
        previous_summary=preparation.previous_summary,
        turn_prefix=preparation.is_split_turn,
    )

    _, events_named, started, finished = submit(http, session_id)
    names = [event["event"] for _, event in events_named]
    assert names[-1] == "done", names[-1]
    start_index = names.index("compaction_start")
    end_index = names.index("compaction_end")
    assert names[start_index] == "compaction_start" and names[end_index] == "compaction_end"

    first_token_index, first_token_at = first_after(events_named, "message_update", end_index)
    assert first_token_index is not None
    checkpoint = next(
        entry for entry in call(service.list_entries(session_id), loop)
        if isinstance(entry, CompactionEntry)
    )
    projection_post = call(service.get_projection(session_id, checkpoint.id), loop)
    post_messages_tokens = estimate_context_tokens(projection_post.context.messages).tokens

    assistant = [
        entry.messages[0]
        for entry in call(service.get_current_branch(session_id), loop)
        if entry.messages and entry.messages[0].role == "assistant"
    ]
    trigger_usage = assistant[-1].usage
    summary_usage = checkpoint.usage
    real_summary_input = summary_usage.input + summary_usage.cache_read
    return {
        "session": session_id,
        "before_full_request_estimate": checkpoint.tokens_before,
        "before_messages_estimate": pre_messages_tokens,
        "after_messages_estimate": post_messages_tokens,
        "compression_ratio": round(1 - post_messages_tokens / pre_messages_tokens, 4),
        "first_kept_entry_id": checkpoint.first_kept_entry_id,
        "summary": {
            "estimated_input_tokens": estimated_summary_input,
            "usage": summary_usage.model_dump(),
            "real_input_tokens": real_summary_input,
            "estimate_error_ratio": round(real_summary_input / estimated_summary_input, 4),
            "chars": len(checkpoint.summary),
        },
        "trigger_request_usage": trigger_usage.model_dump(),
        "timing_ms": {
            "summary": round((events_named[end_index][0] - events_named[start_index][0]) * 1000),
            "first_token_after_compaction": round((first_token_at - events_named[end_index][0]) * 1000),
            "task_total": round((finished - started) * 1000),
        },
        "counts": {
            "compactions": sum(name == "compaction_end" for name in names),
            "model_requests": sum(name == "message_end" for name in names),
            "tool_executions": sum(name == "tool_execution_end" for name in names),
            "overflow_recoveries": sum(
                event["event"] == "compaction_start" and event["data"]["reason"] == "overflow"
                for _, event in events_named
            ),
        },
    }


def control_run(http, ids: dict) -> dict:
    # 对照：同一输入关闭阈值压缩（缩小测试预留预算使阈值高于当前输入），原样发送完整上下文。
    os.environ["FIT_AGENT_COMPACTION_RESERVE_TOKENS"] = "1024"
    try:
        _, events_named, started, finished = submit(http, ids["session"])
        names = [event["event"] for _, event in events_named]
        final = events_named[-1][1]
        return {
            "session": ids["session"],
            "final_event": names[-1],
            "final_data": final["data"],
            "compactions": sum(name == "compaction_end" for name in names),
            "total_ms": round((finished - started) * 1000),
        }
    finally:
        os.environ.pop("FIT_AGENT_COMPACTION_RESERVE_TOKENS", None)


def aggregate(samples: list[dict]) -> dict:
    ratios = [s["compression_ratio"] for s in samples]
    errors = [s["summary"]["estimate_error_ratio"] for s in samples]
    summary_ms = [s["timing_ms"]["summary"] for s in samples]
    total_ms = [s["timing_ms"]["task_total"] for s in samples]
    return {
        "n": len(samples),
        "compression_ratio": {"min": min(ratios), "median": median(ratios), "max": max(ratios)},
        "estimate_error_ratio": {"min": min(errors), "median": median(errors), "max": max(errors)},
        "summary_ms": {"min": min(summary_ms), "median": median(summary_ms), "max": max(summary_ms)},
        "task_total_ms": {"min": min(total_ms), "median": median(total_ms), "max": max(total_ms)},
        "completion_rate": {
            "numerator": sum(1 for s in samples if s["counts"]["compactions"] >= 1),
            "denominator": len(samples),
        },
    }


def check() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    install_test_model_config()
    config = load_model_config()
    spec = resolve_model_spec(config)
    settings = DEFAULT_SETTINGS
    threshold = spec.context_window - settings.reserve_tokens
    assert settings.reserve_tokens == 16384 and settings.keep_recent_tokens == 20000
    path = patch_default_database("compaction-performance")
    sessions = [asyncio.run(seed(path, str(uuid4()), PARTS)) for _ in range(REPS)]
    control_session = asyncio.run(seed(path, str(uuid4()), PARTS))
    evidence: dict = {
        "model": {"id": spec.id, "provider": spec.provider, "api": spec.api,
                  "context_window": spec.context_window, "max_tokens": spec.max_tokens},
        "settings": {"reserve_tokens": settings.reserve_tokens,
                     "keep_recent_tokens": settings.keep_recent_tokens,
                     "threshold": threshold},
        "reps": REPS,
    }
    with Server(app) as server, client(server.base_url) as http:
        evidence["samples"] = [measure(http, spec, settings, ids) for ids in sessions]
        evidence["control"] = control_run(http, control_session)
    assert not active.locked()
    evidence["aggregate"] = aggregate(evidence["samples"])
    (ROOT / "performance-samples.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    first = evidence["aggregate"]["compression_ratio"]["median"]
    print(f"PASS: 生产参数性能测量 n={REPS} 压缩率中位数={first} 对照={evidence['control']['final_event']}")


if __name__ == "__main__":
    check()
