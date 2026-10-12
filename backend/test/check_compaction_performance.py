import asyncio
import json
import platform
import subprocess
import sys
import time
from pathlib import Path
from statistics import median
from uuid import uuid4

from app.agent.budget import SAFETY_MARGIN_TOKENS
from app.agent.compaction import (
    DEFAULT_SETTINGS,
    KEEP_RECENT_TOKENS,
    RESERVE_TOKENS,
    estimate_context_tokens,
    estimate_summary_input_tokens,
    prepare_compaction,
)
from app.ai.model_capabilities import resolve_model_spec
from app.domain.session.models import CompactionEntry
from app.interfaces.http import active, app
from app.model_config import load_model_config
from test.check_compaction_joint import (
    PARTS,
    append_part,
    call,
    part_text,
    seed,
)
from test.check_http import events, final_text, wait_idle
from test.check_profile_confirmation import payload
from test.p0_contract import BenchmarkResult
from test.regression_support import Server, client, patch_default_database

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROOT = PROJECT_ROOT / "temp" / "p0-evaluation" / "compaction"
# 真实 Stepfun 配置位于仓库 backend/data。
REPO_DATA_ROOT = Path(__file__).resolve().parents[1] / "data"
REPS = 3
# check_compaction_joint 的 PARTS=6 在 128k 窗口下摘要输入超出预算；按窗口适配取 4 段。
HISTORY_PARTS = 4
TRIGGER = (
    "请只根据当前上下文用简体中文逐条回答，不要调用任何工具："
    "1) 我的训练目标是什么；2) 我需要避开哪类动作限制；"
    "3) 上周卧推和深蹲各自的训练表现（重量、组数、次数）；"
    "4) 当前是否存在待确认的画像提案。"
)
FACT_TEXT = (
    "补充我的真实训练表现记录，用于后续调整："
    "上周一卧推完成 60 公斤 3 组 8 次；上周四深蹲完成 80 公斤 5 组 5 次。"
    "肩部仍不适，继续避免肩部过顶的推举动作。训练目标是增肌。"
)
FACTS = (
    {"id": "goal", "needle": "增肌", "origin": "profile payload.goal"},
    {"id": "movement_restriction", "needle": "肩部过顶", "origin": "profile payload.movement_restrictions"},
    {"id": "training_bench", "needle": "60", "origin": "训练表现事实：卧推 60 公斤"},
    {"id": "training_squat", "needle": "80", "origin": "训练表现事实：深蹲 80 公斤"},
    {"id": "pending_state", "needle": "待确认", "origin": "待确认画像提案状态"},
)


def real_over_estimate(estimated_input_tokens: int, real_input_tokens: int) -> float:
    return real_input_tokens / estimated_input_tokens


def absolute_relative_error(estimated_input_tokens: int, real_input_tokens: int) -> float:
    return abs(estimated_input_tokens - real_input_tokens) / real_input_tokens


def fact_check(body: str, facts=FACTS) -> dict:
    checks = [
        {"id": item["id"], "needle": item["needle"], "present": item["needle"] in body}
        for item in facts
    ]
    return {
        "passed": sum(1 for item in checks if item["present"]),
        "required": len(checks),
        "checks": checks,
    }


def spread(values: list[float]) -> dict:
    return {"min": min(values), "median": median(values), "max": max(values)}


def git_state() -> dict:
    def run(arguments: list[str]) -> str:
        completed = subprocess.run(
            ["git", *arguments], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True
        )
        return completed.stdout.strip()

    return {"commit": run(["rev-parse", "HEAD"]), "status_porcelain": run(["status", "--porcelain"])}


def submit(http, session_id: str):
    body = {"session_id": session_id, "operation_id": str(uuid4()), "request": TRIGGER}
    named = []
    started = time.perf_counter()
    with http.stream("POST", "/api/agent/run", json=body) as response:
        assert response.status_code == 200, response.text
        request_id = response.headers["X-Request-Entry-ID"]
        for event in events(response):
            named.append((time.perf_counter(), event))
    wait_idle()
    finished = time.perf_counter()
    return request_id, [event for _, event in named], named, started, finished


def verify_sources(session_id: str, ids: dict, fact_id: str) -> dict:
    # 核对原始记录：事实节点原文与待确认画像 payload 必须与测量输入一致。
    loop = app.state.loop
    service = app.state.session_service
    fact_entry = call(service.get_entry(session_id, fact_id), loop)
    fact_body = fact_entry.messages[0].content
    assert fact_body == FACT_TEXT, fact_body
    display_entry = call(service.get_entry(session_id, ids["display"]), loop)
    body = json.loads(display_entry.messages[0].content[0].text)
    assert body["payload"] == payload(), body["payload"]
    assert body["proposal_id"] == ids["proposal"]
    return {
        "fact_entry_id": fact_id,
        "fact_text": fact_body,
        "proposal_id": ids["proposal"],
        "payload": body["payload"],
    }


def measure(http, spec, settings, ids: dict, fact_id: str, filler_id: str) -> dict:
    session_id = ids["session"]
    service = app.state.session_service
    loop = app.state.loop

    projection_pre = call(service.get_projection(session_id, filler_id), loop)
    pre_messages_tokens = estimate_context_tokens(projection_pre.context.messages).tokens
    preparation = prepare_compaction(
        projection_pre.path, spec.context_window, spec.max_tokens, settings
    )
    estimated_summary_input = estimate_summary_input_tokens(
        preparation.messages_to_summarize,
        previous_summary=preparation.previous_summary,
        turn_prefix=preparation.is_split_turn,
    )
    # 切点必须落在事实节点之后，确保事实确实位于被压缩区间。
    assert str(preparation.first_kept_entry_id) == filler_id

    request_id, result, named, started, finished = submit(http, session_id)
    names = [event["event"] for _, event in named]
    assert names[-1] == "done", names[-1]
    assert sum(name == "compaction_end" for name in names) == 1, names

    checkpoints = [
        entry for entry in call(service.list_entries(session_id), loop)
        if isinstance(entry, CompactionEntry)
    ]
    assert len(checkpoints) == 1
    checkpoint = checkpoints[0]
    assert str(checkpoint.first_kept_entry_id) == filler_id
    assert checkpoint.usage.total_tokens > 0 and checkpoint.summary.strip()

    projection_post = call(service.get_projection(session_id, checkpoint.id), loop)
    post_messages_tokens = estimate_context_tokens(projection_post.context.messages).tokens

    answer = final_text(result)
    summary_usage = checkpoint.usage
    real_summary_input = summary_usage.input + summary_usage.cache_read
    summary_ms_bounds = (
        next(at for at, event in named if event["event"] == "compaction_start"),
        next(at for at, event in named if event["event"] == "compaction_end"),
    )
    return {
        "sample": ids["session"],
        "request_entry_id": request_id,
        "history": {
            "seed_parts": HISTORY_PARTS,
            "fact_node": 1,
            "extra_kept_parts": 1,
            "estimated_messages_tokens_before": pre_messages_tokens,
            "summarized_messages": len(preparation.messages_to_summarize),
        },
        "before_full_request_estimate": checkpoint.tokens_before,
        "before_messages_estimate": pre_messages_tokens,
        "after_messages_estimate": post_messages_tokens,
        "context_reduction_ratio": round(1 - post_messages_tokens / pre_messages_tokens, 4),
        "summary": {
            "chars": len(checkpoint.summary),
            "text": checkpoint.summary,
            "estimated_input_tokens": estimated_summary_input,
            "real_input_tokens": real_summary_input,
            "usage": summary_usage.model_dump(),
            "real_over_estimate_ratio": round(
                real_over_estimate(estimated_summary_input, real_summary_input), 4
            ),
            "absolute_relative_error": round(
                absolute_relative_error(estimated_summary_input, real_summary_input), 4
            ),
        },
        "timing_ms": {
            "summary_ms": round((summary_ms_bounds[1] - summary_ms_bounds[0]) * 1000),
            "task_total_ms": round((finished - started) * 1000),
        },
        "first_kept_entry_id": checkpoint.first_kept_entry_id,
        "fact_entry_id": fact_id,
        "facts_in_compacted_range": str(preparation.first_kept_entry_id) == filler_id,
        "fact_retention": {
            "summary": fact_check(checkpoint.summary),
            "answer": fact_check(answer),
        },
        "answer": answer,
        "counts": {
            "compactions": sum(name == "compaction_end" for name in names),
            "model_requests": sum(name == "message_end" for name in names),
            "tool_executions": sum(name == "tool_execution_end" for name in names),
            "overflow_recoveries": sum(
                event["event"] == "compaction_start" and event["data"]["reason"] == "overflow"
                for _, event in named
            ),
        },
    }


def aggregate(samples: list[dict]) -> dict:
    summary_facts = [sample["fact_retention"]["summary"] for sample in samples]
    answer_facts = [sample["fact_retention"]["answer"] for sample in samples]
    return {
        "n": len(samples),
        "context_reduction_ratio": spread([s["context_reduction_ratio"] for s in samples]),
        "real_over_estimate_ratio": spread(
            [s["summary"]["real_over_estimate_ratio"] for s in samples]
        ),
        "absolute_relative_error": spread(
            [s["summary"]["absolute_relative_error"] for s in samples]
        ),
        "summary_ms": spread([s["timing_ms"]["summary_ms"] for s in samples]),
        "fact_retention": {
            "summary": {
                "passed": sum(item["passed"] for item in summary_facts),
                "required": sum(item["required"] for item in summary_facts),
            },
            "answer": {
                "passed": sum(item["passed"] for item in answer_facts),
                "required": sum(item["required"] for item in answer_facts),
            },
        },
    }


def run_benchmark(experiment_dir: str) -> BenchmarkResult:
    from app import model_config

    assert (REPO_DATA_ROOT / "models.json").exists() and (REPO_DATA_ROOT / "auth.json").exists()
    model_config.DATA_ROOT = REPO_DATA_ROOT
    config = load_model_config()
    spec = resolve_model_spec(config)
    settings = DEFAULT_SETTINGS
    threshold = spec.context_window - settings.reserve_tokens
    assert settings.reserve_tokens == RESERVE_TOKENS and settings.keep_recent_tokens == KEEP_RECENT_TOKENS
    assert HISTORY_PARTS <= PARTS

    path = patch_default_database("compaction-performance-p0")
    sessions = [asyncio.run(seed(path, str(uuid4()), HISTORY_PARTS)) for _ in range(REPS)]
    state = git_state()
    env = {
        "scope": "固定长历史上下文压缩测量",
        "commit": state["commit"],
        "worktree_status": state["status_porcelain"],
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "model": {
            "id": spec.id,
            "provider": spec.provider,
            "api": spec.api,
            "base_url": spec.base_url,
            "context_window": spec.context_window,
            "max_tokens": spec.max_tokens,
        },
        "settings": {
            "reserve_tokens": settings.reserve_tokens,
            "keep_recent_tokens": settings.keep_recent_tokens,
            "trigger_threshold": threshold,
            "safety_margin_tokens": SAFETY_MARGIN_TOKENS,
        },
        "history": {
            "seed_parts": HISTORY_PARTS,
            "part_chars": len(part_text()),
            "fact_chars": len(FACT_TEXT),
            "extra_kept_parts": 1,
        },
    }
    sources = []
    samples = []
    with Server(app) as server, client(server.base_url) as http:
        for ids in sessions:
            fact_id = append_part(ids["session"], FACT_TEXT)
            filler_id = append_part(ids["session"], part_text())
            sources.append(verify_sources(ids["session"], ids, fact_id))
            samples.append(measure(http, spec, settings, ids, fact_id, filler_id))
    assert not active.locked()

    summary_facts = aggregate(samples)["fact_retention"]
    correctness = {
        "samples": len(samples),
        "compacted": sum(1 for s in samples if s["counts"]["compactions"] == 1),
        "denominator": sum(
            s["fact_retention"]["summary"]["required"]
            for s in samples
        ),
        "summary_facts_passed": summary_facts["summary"]["passed"],
        "answer_facts_passed": summary_facts["answer"]["passed"],
        "facts_in_compacted_range": sum(1 for s in samples if s["facts_in_compacted_range"]),
        "overflow_recoveries": sum(s["counts"]["overflow_recoveries"] for s in samples),
        "sources": sources,
    }
    complete = (
        len(samples) == REPS
        and correctness["compacted"] == REPS
        and correctness["facts_in_compacted_range"] == REPS
    )
    result = BenchmarkResult(
        topic="compaction",
        env=env,
        samples=samples,
        stats=aggregate(samples),
        correctness=correctness,
        missing=["费用金额（未取得可靠价格与计价口径，只报告真实 tokens 与估算口径）"],
        review=[
            {
                "review_id": "context_reduction_vs_cost",
                "standard": (
                    "上下文缩减率为消息 token 估算量之比；真实费用还受摘要调用、缓存与计价影响，"
                    "不据此推断金额结论"
                ),
                "evidence_refs": [
                    "samples[].context_reduction_ratio",
                    "samples[].summary.usage",
                ],
            }
        ],
        status="complete" if complete else "incomplete",
    )
    target = Path(experiment_dir)
    target.mkdir(parents=True, exist_ok=True)
    (target / "compaction.json").write_text(
        json.dumps(result.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def check() -> None:
    result = run_benchmark(str(ROOT))
    environment = result.env
    stats = result.stats
    print(
        f"模型: {environment['model']['id']} / {environment['model']['provider']} "
        f"窗口={environment['model']['context_window']} 触发阈值={environment['settings']['trigger_threshold']} "
        f"保留预算={environment['settings']['keep_recent_tokens']}"
    )
    reduction = stats["context_reduction_ratio"]
    ratio = stats["real_over_estimate_ratio"]
    error = stats["absolute_relative_error"]
    print(
        f"上下文缩减率: 中位数={reduction['median']} 范围=[{reduction['min']}, {reduction['max']}]；"
        f"真实/估算比值中位数={ratio['median']}；绝对相对误差中位数={error['median']}"
    )
    facts = stats["fact_retention"]
    print(
        f"事实保留: 摘要 {facts['summary']['passed']}/{facts['summary']['required']}；"
        f"后续回答 {facts['answer']['passed']}/{facts['answer']['required']}"
    )
    print(f"证据: {ROOT / 'compaction.json'}")
    print(
        f"PASS: {environment['scope']} n={stats['n']} 状态={result.status}"
    )


if __name__ == "__main__":
    check()
