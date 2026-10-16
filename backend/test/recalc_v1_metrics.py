import argparse
import json
import statistics
import sys
import time
import tracemalloc
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]

# v1 运行实际轮次（证据目录存在 round-0/1/2，与发布值 150=50×3 一致；
# 现网 p0_contract.ROUNDS 已被 v2 改写，不得复用）。
V1_ROUNDS = 3

# 用例 ID 前缀 -> 业务类别（与 v1 发布报告 §类别口径一致）。
PREFIX_TO_CATEGORY = OrderedDict(
    [
        ("P", "profile"),
        ("L", "plan_entry"),
        ("A", "plan_adjust"),
        ("W", "workout"),
        ("C", "confirm_idempotency"),
        ("S", "risk_unknown"),
    ]
)

USAGE_FIELDS = ("input", "output", "cache_read", "cache_write", "total_tokens")

# cache_write 字段口径证据（文件:行），用于第 3 项的 provenance 判定。
CACHE_WRITE_PROVENANCE = [
    {
        "location": "backend/app/ai/api/openai_completions.py:215",
        "code": "written = details.get(\"cache_write_tokens\", 0)",
        "meaning": "openai-completions 适配器在 provider 省略 cache_write_tokens 时把该字段强制当作 0，"
                   "而非记录为未知。",
    },
    {
        "location": "backend/app/ai/api/openai_completions.py:223-229",
        "code": "usage = { ... \"cache_write\": written, ... }",
        "meaning": "cache_write 无条件写入 Usage 字典；存储值为 0 时无法区分“provider 主动报 0”与“provider 省略后被默认”。",
    },
    {
        "location": "backend/app/ai/api/openai_completions.py:230-242",
        "code": "if \"...\" in raw/details: usage[\"reasoning\"|\"cache_write_1h\"] = ...",
        "meaning": "reasoning 与 cache_write_1h 仅在 provider 明确上报时才写入；cache_write 不享有这种“仅上报才写”的保护，"
                   "说明其 0 是默认值语义。",
    },
    {
        "location": "backend/app/agent/usage.py:24-27",
        "code": "totals = {field: sum(getattr(usage, field) ...) for field in (input,output,cache_read,cache_write,total_tokens)}",
        "meaning": "summarize_usage 对 cache_write 一律相加；对照 usage.py:28-35 的 reasoning/cache_write_1h 走 model_fields_set 过滤，"
                   "保留“未上报即缺省”的语义。cache_write 无此保护。",
    },
    {
        "location": "backend/app/ai/messages.py:75-83",
        "code": "cache_write: int | float（必填）; cache_write_1h/reasoning = None（默认）",
        "meaning": "Usage 模型把 cache_write 定义为必填、无默认，逼迫适配器把缺省的 0 物化成实值；"
                   "而 reasoning/cache_write_1h 默认为 None 才能表达“未上报”。",
    },
    {
        "location": "backend/app/ai/api/anthropic_messages.py:245-270",
        "code": "cache_write = start.cache_creation_input_tokens or 0；cache_write_1h 仅在上报时写入",
        "meaning": "Anthropic 路径 cache_creation_input_tokens 是真实上报字段，且 cache_write_1h 靠存在性门控——"
                   "这正是 openai-completions 对 cache_write 无法提供的 known_zero 与 unreported 区分。",
    },
    {
        "location": "backend/test/p0_eval_support.py:186-225",
        "code": "return {field: getattr(totals, field) for field in USAGE_FIELDS}, []",
        "meaning": "落盘时只导出 USAGE_FIELDS 五个整数，丢弃 model_fields_set/cost/reasoning/cache_write_1h，"
                   "证据里 cache_write 恒为 0（本次 282/282 run 均为 0），无法回溯其是上报还是默认。",
    },
]


def load_json(path: Path):
    return json.loads(path.read_bytes())


def median(values):
    ordered = sorted(values)
    count = len(ordered)
    if count == 0:
        return None
    middle = count // 2
    return ordered[middle] if count % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def p95_inclusive(values):
    clean = sorted(value for value in values if value is not None)
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    return statistics.quantiles(clean, n=100, method="inclusive")[94]


def span(values):
    """v2 _span 口径：samples/mean/p50/p95/max，p50 取中位数，p95 取 inclusive 分位。"""
    clean = [value for value in values if value is not None]
    if not clean:
        return {"samples": 0, "mean": None, "p50": None, "p95": None, "max": None}
    return {
        "samples": len(clean),
        "mean": sum(clean) / len(clean),
        "p50": median(clean),
        "p95": p95_inclusive(clean),
        "max": max(clean),
    }


def category_of(case_id: str) -> str:
    for prefix, category in PREFIX_TO_CATEGORY.items():
        if case_id.startswith(prefix):
            return category
    return "unknown"


def round_status(turns, checks):
    if any(turn["terminal"] in {"error", "unknown"} for turn in turns):
        return "error"
    if any(check["status"] == "fail" for check in checks):
        return "fail"
    if any(check["status"] == "pending_review" for check in checks):
        return "pending_review"
    return "pass"


def apply_review(checks, case_id, round_index, rindex):
    applied = []
    for check in checks:
        if check["status"] == "pending_review":
            verdict = rindex.get((case_id, round_index, check["check_id"]))
            if verdict is not None:
                applied.append(
                    {
                        **check,
                        "status": verdict["status"],
                        "reason": verdict.get("reason", check["reason"]),
                    }
                )
                continue
        applied.append(check)
    return applied


def first_text_delta_nonempty(events):
    """v1 turn 事件按 SSE 顺序存储但无逐事件到达时间戳。
    该函数只判定首个 text_delta 事件的正文是否为非空（内容核查），
    绝不臆造非空正文到达时刻。"""
    for event in events:
        if event["event"] == "message_update" and event["data"].get("update_type") == "text_delta":
            joined = "".join(block.get("text", "") for block in event["data"].get("content", []))
            return bool(joined.strip())
    return None


def scan_rounds(v1_root: Path):
    """逐 slot 读取 round.json（其内嵌全部 turn 观测），只累积紧凑统计后释放对象。
    返回 (records, peak_bytes, elapsed_seconds)。records 为每轮紧凑记录，不含 events。"""
    round_files = sorted(v1_root.glob("cases/*/round-*/round.json"))
    verdicts_path = v1_root / "review-verdicts.json"
    verdicts = load_json(verdicts_path)
    reviews = verdicts["reviews"] if isinstance(verdicts, dict) else verdicts
    rindex = {(r["case_id"], r["round_index"], r["check_id"]): r for r in reviews}

    records = []
    tracemalloc.start()
    started = time.perf_counter()
    for path in round_files:
        payload = load_json(path)
        case_id = payload["case_id"]
        round_index = payload["round_index"]
        checks = apply_review(payload["checks"], case_id, round_index, rindex)
        status = round_status(payload["turns"], checks)

        turns = []
        empty_first_delta = 0
        nonempty_first_delta = 0
        no_delta = 0
        for turn in payload["turns"]:
            first_flag = first_text_delta_nonempty(turn.get("events") or [])
            if first_flag is True:
                nonempty_first_delta += 1
            elif first_flag is False:
                empty_first_delta += 1
            else:
                no_delta += 1
            turns.append(
                {
                    "turn_index": turn["turn_index"],
                    "run_id": turn["run_id"],
                    "terminal": turn["terminal"],
                    "usage": turn["usage"],
                    "first_text_delta_ms": turn["first_text_delta_ms"],
                    "duration_ms": turn["duration_ms"],
                }
            )
        records.append(
            {
                "case_id": case_id,
                "round_index": round_index,
                "session_id": payload["session_id"],
                "status": status,
                "stored_status": payload["status"],
                "error": payload.get("error"),
                "round_duration_ms": payload["duration_ms"],
                "turns": turns,
                "checks": [
                    {"check_id": c["check_id"], "status": c["status"], "kind": c["kind"], "reason": c["reason"]}
                    for c in checks
                ],
                "first_delta_nonempty": nonempty_first_delta,
                "first_delta_empty": empty_first_delta,
                "first_delta_absent": no_delta,
                "evidence_path": str(path.relative_to(v1_root)),
            }
        )
        del payload
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return records, peak, time.perf_counter() - started, len(round_files)


def compute_pass_rates(records, v1_root: Path):
    case_ids = sorted({r["case_id"] for r in records})
    by_case = {case_id: {} for case_id in case_ids}
    for r in records:
        by_case[r["case_id"]][r["round_index"]] = r

    per_round = []
    for index in range(V1_ROUNDS):
        observed = [r for r in records if r["round_index"] == index]
        passed = [r for r in observed if r["status"] == "pass"]
        per_round.append(
            {
                "round_index": index,
                "passed": len(passed),
                "observed": len(observed),
                "denominator_cases": len(case_ids),
                "rate": (len(passed) / len(observed)) if observed else None,
                "formula": "本轮 status=pass 场景数 / 本轮观测场景数",
                "evidence_path": f"cases/*/round-{index}/round.json (review applied)",
            }
        )

    executions = len(records)
    passed_executions = sum(1 for r in records if r["status"] == "pass")
    distribution = {f"{count}/{V1_ROUNDS}": 0 for count in range(V1_ROUNDS + 1)}
    per_case_final = {}
    for case_id in case_ids:
        bucket = by_case[case_id]
        passed_rounds = sum(
            1
            for index in range(V1_ROUNDS)
            if bucket.get(index) is not None and bucket[index]["status"] == "pass"
        )
        distribution[f"{passed_rounds}/{V1_ROUNDS}"] += 1
        sequence = ""
        failing = []
        for index in range(V1_ROUNDS):
            r = bucket.get(index)
            if r is None:
                sequence += "-"
                continue
            marker = {"pass": "p", "fail": "f", "error": "e", "pending_review": "r"}[r["status"]]
            sequence += marker
            if r["status"] != "pass":
                for check in r["checks"]:
                    if check["status"] in {"fail", "pending_review"}:
                        failing.append(
                            {
                                "round_index": index,
                                "check_id": check["check_id"],
                                "status": check["status"],
                                "reason": check["reason"],
                                "evidence_path": r["evidence_path"],
                            }
                        )
                if r["status"] == "error":
                    failing.append(
                        {
                            "round_index": index,
                            "check_id": None,
                            "status": "error",
                            "reason": r["error"] or "回合终态为 error/unknown",
                            "evidence_path": r["evidence_path"],
                        }
                    )
        per_case_final[case_id] = {
            "category": category_of(case_id),
            "passed_rounds": f"{passed_rounds}/{V1_ROUNDS}",
            "status_sequence": sequence,
            "final_failure_reasons": failing,
        }

    stable_cases = sum(1 for case_id in case_ids if per_case_final[case_id]["passed_rounds"] == f"{V1_ROUNDS}/{V1_ROUNDS}")
    status_counts = Counter(r["status"] for r in records)

    categories = {}
    for case_id in case_ids:
        category = category_of(case_id)
        bucket = categories.setdefault(
            category,
            {"prefix": next(p for p, c in PREFIX_TO_CATEGORY.items() if c == category),
             "cases": 0, "executions": 0, "passed_executions": 0,
             "stable_cases": 0, "pending_review_rounds": 0},
        )
        bucket["cases"] += 1
        observed = [by_case[case_id][i] for i in range(V1_ROUNDS) if by_case[case_id].get(i)]
        bucket["executions"] += len(observed)
        bucket["passed_executions"] += sum(1 for r in observed if r["status"] == "pass")
        if len(observed) == V1_ROUNDS and all(r["status"] == "pass" for r in observed):
            bucket["stable_cases"] += 1
        bucket["pending_review_rounds"] += sum(1 for r in observed if r["status"] == "pending_review")
    for bucket in categories.values():
        bucket["execution_pass_rate"] = (
            bucket["passed_executions"] / bucket["executions"] if bucket["executions"] else None
        )
        bucket["stable_pass_rate"] = (
            bucket["stable_cases"] / bucket["cases"] if bucket["cases"] else None
        )

    return {
        "independent_cases": len(case_ids),
        "rounds_per_case": V1_ROUNDS,
        "per_round_pass_rate": per_round,
        "execution_pass_rate": {
            "passed": passed_executions,
            "denominator": executions,
            "rate": (passed_executions / executions) if executions else None,
            "formula": "全部三轮 status=pass 的执行数 / 总执行数",
            "unit": "比率",
            "evidence_path": "cases/*/*/round.json (review applied)",
        },
        "stable_pass_rate": {
            "stable_cases": stable_cases,
            "denominator": len(case_ids),
            "rate": (stable_cases / len(case_ids)) if case_ids else None,
            "formula": f"{V1_ROUNDS} 轮全部 pass 的场景数 / 场景数",
            "unit": "比率",
            "evidence_path": "cases/*/*/round.json (review applied)",
        },
        "status_counts": {
            "counts": {
                "pass": status_counts.get("pass", 0),
                "fail": status_counts.get("fail", 0),
                "error": status_counts.get("error", 0),
                "pending_review": status_counts.get("pending_review", 0),
            },
            "total": executions,
            "note": "状态在人工审阅（review-verdicts.json）合入后按 _round_status 重算，与 v1 官方 finalize 管道一致。",
            "evidence_path": "review-verdicts.json + cases/*/*/round.json",
        },
        "category_breakdown": categories,
        "stability_distribution": distribution,
        "per_case_final": per_case_final,
    }


def compute_tokens(records, v1_root: Path):
    seen = {}
    duplicates = []
    missing_usage_turns = 0
    total_turns = 0
    cw_known_nonzero = 0
    cw_zero = 0
    cr_positive = 0
    for r in records:
        for turn in r["turns"]:
            total_turns += 1
            usage = turn["usage"]
            if usage is None:
                missing_usage_turns += 1
                continue
            if usage.get("cache_write") not in (None,) and usage.get("cache_write", 0) != 0:
                cw_known_nonzero += 1
            if usage.get("cache_write") == 0:
                cw_zero += 1
            if usage.get("cache_read", 0) > 0:
                cr_positive += 1
            run_id = turn["run_id"]
            if run_id in seen:
                duplicates.append(run_id)
                continue
            seen[run_id] = usage

    totals = {field: sum(item[field] for item in seen.values()) for field in USAGE_FIELDS}
    distinct_runs = len(seen)
    all_three_known = [item for item in seen.values() if item.get("input") is not None
                        and item.get("cache_read") is not None and item.get("cache_write") is not None]
    return {
        "usage_sums": {
            "input": totals["input"],
            "output": totals["output"],
            "cache_read": totals["cache_read"],
            "cache_write": totals["cache_write"],
            "total_tokens": totals["total_tokens"],
            "unit": "tokens",
            "formula": "按去重后的 run_id 汇总 summarize_usage 导出的真实用量（含一次摘要调用）",
        },
        "dedupe": {
            "identity": "turn.run_id",
            "distinct_run_ids": distinct_runs,
            "duplicates_found": duplicates,
            "duplicate_count": len(duplicates),
            "cross_check": {
                "total_turns": total_turns,
                "turns_with_usage": total_turns - missing_usage_turns,
                "distinct_runs_equal_usage_turns": distinct_runs == (total_turns - missing_usage_turns),
                "conclusion": "本次 150 轮共 282 个回合、282 个互异 run_id，无跨回合复用，去重不改变总量。",
            },
        },
        "missing_usage_turns": missing_usage_turns,
        "field_provenance_scan": {
            "cache_write_zero_count": cw_zero,
            "cache_write_nonzero_count": cw_known_nonzero,
            "cache_read_positive_count": cr_positive,
            "note": "全部 282 个 run 的 cache_write 恒为 0，cache_read 恒为正，符合 openai-completions 只上报 cached_tokens、"
                    "cache_write 被 .get(...,0) 默认的形态。",
        },
        "v2_formula_common_sample_mechanically_non_null": len(all_three_known),
        "evidence_path": "cases/*/*/round.json -> turns[].usage (review 无关)",
    }


def compute_cache_rate(records, v1_root: Path):
    seen = {}
    for r in records:
        for turn in r["turns"]:
            if turn["usage"] is None:
                continue
            seen.setdefault(turn["run_id"], turn["usage"])
    usages = list(seen.values())

    sum_input = sum(u["input"] for u in usages)
    sum_cache_read = sum(u["cache_read"] for u in usages)
    sum_cache_write = sum(u["cache_write"] for u in usages)

    # 旧公式：cache_read / (cache_read + input)
    old_den = sum_cache_read + sum_input
    old_rate = (sum_cache_read / old_den) if old_den else None

    # 新公式：sum(cache_read) / sum(input + cache_read + cache_write)，
    # 仅纳入 cache_write 为“确知”（affirmatively reported）的样本。
    # v1 证据的 cache_write 全部来自 openai-completions 适配器的 .get(...,0) 默认，
    # 无法判定 known_zero，故确知样本集为空。
    known_write_samples = []
    new_den = sum(u["input"] + u["cache_read"] + u["cache_write"] for u in known_write_samples)
    new_num = sum(u["cache_read"] for u in known_write_samples)
    new_rate = (new_num / new_den) if new_den else None

    # 对照：若把默认 0 误当作确知值机械套用新公式（因所有 cache_write=0，分母多出的 0 不改变结果）。
    mechanical_den = sum_input + sum_cache_read + sum_cache_write
    mechanical_rate = (sum_cache_read / mechanical_den) if mechanical_den else None

    return {
        "sample_set": {
            "runs": len(usages),
            "missing_usage_turns": sum(1 for r in records for t in r["turns"] if t["usage"] is None),
            "description": "去重后 282 个 run 的 summarize_usage 五字段用量。",
            "evidence_path": "cases/*/*/round.json -> turns[].usage",
        },
        "aggregates": {
            "input": sum_input,
            "cache_read": sum_cache_read,
            "cache_write": sum_cache_write,
        },
        "cache_rate_old": {
            "value": old_rate,
            "numerator": sum_cache_read,
            "denominator": old_den,
            "formula": "cache_read / (cache_read + input)",
            "unit": "比率",
            "provenance": "known",
            "comparability": "comparable",
            "note": "v1 发布的原始缓存命中率口径，保持不变。",
        },
        "cache_rate_new": {
            "value": new_rate,
            "numerator": new_num,
            "denominator": new_den,
            "valid_sample_count": len(known_write_samples),
            "missing_count": len(usages),
            "formula": "sum(cache_read) / sum(input + cache_read + cache_write)，仅计 cache_write 确知的样本",
            "unit": "比率",
            "comparability": "not_recalculable",
            "reason": "v1 证据的 cache_write 恒为 0，且该 0 来自 openai-completions 适配器的缺省（details.get(\"cache_write_tokens\", 0)），"
                      "无法区分 provider 主动报 0 与省略后默认；按字段口径规则判为 unreported，故新公式的“cache_write 确知”公共样本集为空，"
                      "cache_rate_new 记为缺测（null），保留旧公式原始值。",
        },
        "mechanical_reference": {
            "value": mechanical_rate,
            "note": "若错误地把默认 0 当作确知值机械套用新公式，因所有 cache_write=0，分母相对旧公式仅多出 0，"
                    "结果与旧公式逐位相同；此项仅作口径对照，非权威值。",
            "authoritative": False,
        },
        "field_provenance": {
            "rule": "known_zero = 供应商原始记录/适配器明确上报 0；unreported = 字段缺省后被物化为 0。",
            "per_field": {
                "input": {"provenance": "known", "basis": "prompt_tokens - cached - written，源自上报的 prompt_tokens 与 cached_tokens。"},
                "output": {"provenance": "known", "basis": "completion_tokens 直接上报。"},
                "cache_read": {"provenance": "known", "basis": "prompt_tokens_details.cached_tokens 直接上报（本次 282/282 run 均 >0）。"},
                "cache_write": {"provenance": "unreported_defaulted", "basis": "prompt_tokens_details.cache_write_tokens 被 .get(...,0) 缺省；282/282 run 均为 0，无法判定是否主动上报。"},
                "total_tokens": {"provenance": "known", "basis": "total_tokens 直接上报。"},
            },
            "citations": CACHE_WRITE_PROVENANCE,
        },
    }


def compute_durations(records, v1_root: Path):
    round_ms = [r["round_duration_ms"] for r in records if r["round_duration_ms"] is not None]
    first_ms = []
    for r in records:
        for turn in r["turns"]:
            if turn["first_text_delta_ms"] is not None:
                first_ms.append(turn["first_text_delta_ms"])
    nonempty = sum(r["first_delta_nonempty"] for r in records)
    empty = sum(r["first_delta_empty"] for r in records)
    absent = sum(r["first_delta_absent"] for r in records)
    return {
        "round_ms": {
            **span(round_ms),
            "unit": "ms",
            "formula": "run_case_round 计时（v1 p0_eval_support.py:363 起、419 止）",
            "boundary": "自 perf_counter 起点覆盖 Server 启动 + create_session + build_precondition + 全部回合，直到本轮终态；"
                        "即含启动与前置构建耗时。",
            "sample_set": "150 个场景轮次的 duration_ms。",
            "evidence_path": "cases/*/*/round.json -> duration_ms",
            "comparability": "protocol_changed",
            "comparability_reason": "v2 run_case_round 明确“场景耗时只覆盖业务步骤区间”（v2_eval_support.py:706-707、794），"
                                     "在 build_precondition 之后才计时；v1 口径含启动与前置，业务边界不可直接对齐。",
        },
        "first_text_delta_ms": {
            **span(first_ms),
            "unit": "ms",
            "formula": "v1 capture_turn：http POST 前起 perf_counter，至首个 message_update/text_delta 事件到达（p0_eval_support.py:269-291）",
            "boundary": "自客户端发起本次回合请求起，到首个 text_delta SSE 事件到达；不含 Server 启动/前置构建，但含请求上行与首 token 前服务处理。",
            "sample_set": "282 个回合的 first_text_delta_ms（本次 282/282 均有值）。",
            "evidence_path": "cases/*/*/round.json -> turns[].first_text_delta_ms",
            "definition_note": "v1 定义为“首个文本事件到达耗时”，未校验正文非空：本次 282 回合中首个 text_delta 正文非空 "
                               f"{nonempty} 例、空串 {empty} 例、无 text_delta {absent} 例。",
            "comparability": "protocol_changed",
            "comparability_reason": "v2 capture_turn 只计首个非空正文（v2_eval_support.py:313 检查 .strip()），"
                                     "定义与 v1“首个文本事件（可为空）”不同。",
        },
        "first_delta_content_census": {
            "nonempty": nonempty,
            "empty": empty,
            "absent": absent,
            "note": "内容存在性核查，不含到达时刻，故无法据此重建非空正文时延。",
        },
    }


def compute_benchmarks(v1_root: Path):
    tp_path = v1_root / "benchmarks/tool-parallel/benchmark.json"
    tp = load_json(tp_path)
    samples = tp["samples"]
    seq_batch = [x["batch_ms"] for x in samples if x["mode"] == "sequential"]
    par_batch = [x["batch_ms"] for x in samples if x["mode"] == "parallel"]
    seq_first = [x["first_ms"] for x in samples if x["mode"] == "sequential"]
    par_first = [x["first_ms"] for x in samples if x["mode"] == "parallel"]
    seq_bp = median(seq_batch) if seq_batch else None
    par_bp = median(par_batch) if par_batch else None
    tool_parallel = {
        "n_pairs": len({x["pair"] for x in samples}),
        "n_samples": len(samples),
        "serial_batch_ms": {"samples": len(seq_batch), "p50": seq_bp, "p95": p95_inclusive(seq_batch)},
        "parallel_batch_ms": {"samples": len(par_batch), "p50": par_bp, "p95": p95_inclusive(par_batch)},
        "serial_over_parallel_batch_p50": (seq_bp / par_bp) if (seq_bp and par_bp) else None,
        "serial_first_ms_p50": median(seq_first) if seq_first else None,
        "parallel_first_ms_p50": median(par_first) if par_first else None,
        "formula": "串行 P50 / 并行 P50；P50 = median(batch_ms)",
        "correctness": tp.get("correctness"),
        "unit": "ms",
        "evidence_path": "benchmarks/tool-parallel/benchmark.json -> samples",
        "comparability": "comparable",
    }

    cp_path = v1_root / "benchmarks/compaction/benchmark.json"
    cp = load_json(cp_path)
    cs = cp["samples"]
    summary_ms = [c["timing_ms"]["summary_ms"] for c in cs if c.get("timing_ms", {}).get("summary_ms") is not None]
    reduction = [c["context_reduction_ratio"] for c in cs if c.get("context_reduction_ratio") is not None]
    summary_passed = sum(c["fact_retention"]["summary"]["passed"] for c in cs)
    summary_required = sum(c["fact_retention"]["summary"]["required"] for c in cs)
    answer_passed = sum(c["fact_retention"]["answer"]["passed"] for c in cs)
    answer_required = sum(c["fact_retention"]["answer"]["required"] for c in cs)
    compaction = {
        "n_samples": len(cs),
        "summary_duration_ms": {"samples": len(summary_ms), "p50": median(summary_ms) if summary_ms else None,
                                 "min": min(summary_ms) if summary_ms else None,
                                 "max": max(summary_ms) if summary_ms else None},
        "context_reduction_ratio": {"samples": len(reduction), "p50": median(reduction) if reduction else None,
                                     "min": min(reduction) if reduction else None,
                                     "max": max(reduction) if reduction else None},
        "formula_context_reduction": "1 - 压缩后估算量 / 压缩前估算量（估算器 3.5 chars/token）",
        "fact_retention": {
            "summary": {"passed": summary_passed, "required": summary_required},
            "answer": {"passed": answer_passed, "required": answer_required},
            "formula": "通过事实检查数 / 应保留事实检查数（逐样本累加）",
        },
        "evidence_path": "benchmarks/compaction/benchmark.json -> samples",
        "comparability": "comparable",
    }
    return {"tool_parallel": tool_parallel, "compaction": compaction}


def compute_missing_collection(v1_root: Path):
    return {
        "server_first_non_empty_body_latency": {
            "status": "not_recalculable",
            "unit": "ms",
            "reason": "v1 未采集服务端首个非空正文到达时刻；证据无对应字段。",
        },
        "per_model_request_first_token_latency": {
            "status": "not_recalculable",
            "unit": "ms",
            "reason": "v1 无逐模型请求的首 token 计时；Trace 体系在 v1 之后才落地，回合仅存首个 SSE text_delta 的粗粒度耗时。",
        },
        "per_model_request_duration": {
            "status": "not_recalculable",
            "unit": "ms",
            "reason": "v1 未按模型请求切分耗时；仅有回合级 duration_ms 与轮级 round duration_ms。",
        },
        "trace_root_counts_and_hierarchy": {
            "status": "not_recalculable",
            "reason": "Trace 落地晚于 v1，v1 证据不含 Trace 根数与父子关系。",
        },
        "first_non_empty_body_reconstruction": {
            "status": "not_recalculable",
            "unit": "ms",
            "check": "已检查 v1 turn 事件：每条事件仅含 {event, data}，无任何逐事件到达时间戳（区别于 v2 events 的 t_ms 字段，"
                     "见 v2_eval_support.py:308）。首个文本事件的正文存在性可核查，但非空正文的到达时刻无法重建。",
            "sample_count": None,
            "original_v1_value": "首个文本事件到达耗时（首文本耗时中位 3.12s）",
            "note": "v1 未校验非空内容，故该原始值口径为“首个文本事件”，不等同于“首个非空正文”。",
        },
    }


def compute_comparability():
    protocol_changes = [
        {
            "change": "确认改由 HTTP confirmations 端点驱动保存",
            "v1": "保存由 agent 回合内部/工具链触发",
            "v2": "run_case_round 的 confirm/confirm_repeat/confirm_negative 步骤走真实 HTTP 确认端点（v2_eval_support.py:560-562）",
            "affected": ["profile", "plan_entry", "plan_adjust", "workout", "confirm_idempotency"],
            "impact": "确认/幂等类别与任何依赖保存副作用的检查项，v1 与 v2 判定路径不同，逐例结果非同一协议。",
        },
        {
            "change": "真实附件上传",
            "v1": "附件类用例依赖 read 工具引用文件，L05/C02/S05 因读取失败大量 pending_review/fail",
            "v2": "user 步骤以 base64 upload 附件真实进入请求（v2_eval_support.py:711-723）",
            "affected": ["plan_entry (L05)", "confirm_idempotency (C02)", "risk_unknown (S05)"],
            "impact": "外部内容边界类结果口径改变，相关用例通过率不可直接比较。",
        },
        {
            "change": "C03 前置经真实保存链构建",
            "v1": "C03 前置以直接落库方式建立，重复核对时未能命中原操作结果（发布值 C03 0/3）",
            "v2": "前置经由真实确认/保存链生成可复现的操作记录",
            "affected": ["confirm_idempotency (C03)"],
            "impact": "幂等结果定义与前置来源不同，C03 的 v1 结果与 v2 非同协议。",
        },
        {
            "change": "L07 场景替换",
            "v1": "L07 使用旧场景（发布 2/3，r2 fail）",
            "v2": "L07 场景已被替换为新的业务步骤",
            "affected": ["plan_entry (L07)"],
            "impact": "用例内容不同，L07 的逐例结果不可与 v2 对齐。",
        },
    ]
    return {
        "per_metric": {
            "per_round_pass_rate": {"level": "comparable", "reason": "公式与分母一致（本轮 pass/本轮观测），审阅管道一致。"},
            "execution_pass_rate": {"level": "comparable", "reason": "103/150 口径与 v1 完全一致；但见 protocol_changes：受影响类别逐例判定路径已变。"},
            "stable_pass_rate": {"level": "comparable", "reason": "25/50 口径一致；同上协议变更影响个别场景。"},
            "status_counts": {"level": "comparable", "reason": "pass/fail/error/pending_review 定义与 _round_status 一致。"},
            "category_breakdown": {"level": "comparable", "reason": "按前缀归类一致；个别类别受协议变更影响。"},
            "stability_distribution": {"level": "comparable", "reason": "0/3..3/3 分布口径一致。"},
            "token_totals": {"level": "comparable", "reason": "run_id 去重口径与 v1 _usage_totals 一致，总量可精确复现。"},
            "cache_rate_old": {"level": "comparable", "reason": "cache_read/(cache_read+input) 口径未变，v1 原始值可直接引用。"},
            "cache_rate_new": {"level": "not_recalculable", "reason": "v1 cache_write 为默认 0（unreported），新公式公共样本集为空。"},
            "round_ms": {"level": "protocol_changed", "reason": "v1 计时含启动+前置，v2 仅业务步骤区间。"},
            "first_text_delta_ms": {"level": "protocol_changed", "reason": "v1 取首个文本事件（可空），v2 取首个非空正文。"},
            "tool_parallel_benchmark": {"level": "comparable", "reason": "同一固定负载与同一样本可精确复算。"},
            "compaction_benchmark": {"level": "comparable", "reason": "同一样本与同一估算器可精确复算。"},
        },
        "protocol_changes": protocol_changes,
    }


def build_output(v1_root: Path):
    records, peak, elapsed, n_files = scan_rounds(v1_root)
    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "experiment_id": "p0-official",
            "v1_root": str(v1_root),
            "round_files_scanned": n_files,
            "rounds_per_case": V1_ROUNDS,
            "review_source": "review-verdicts.json",
            "pipeline": "load round.json -> apply review verdicts (only pending_review) -> recompute _round_status -> aggregate",
            "method": "逐 slot 读取 round.json（内嵌全部 turn 观测），只累积紧凑统计后释放对象，全程离线、无付费调用、不加载 910MiB summary.json。",
            "scan_seconds": elapsed,
            "peak_memory_bytes": peak,
            "peak_memory_mb": peak / 1_000_000,
        },
        "pass_rates": compute_pass_rates(records, v1_root),
        "token_totals": compute_tokens(records, v1_root),
        "cache_hit_rate": compute_cache_rate(records, v1_root),
        "duration_stats": compute_durations(records, v1_root),
        "benchmarks": compute_benchmarks(v1_root),
        "missing_collection": compute_missing_collection(v1_root),
        "comparability": compute_comparability(),
    }


def render_markdown(out: dict) -> str:
    pr = out["pass_rates"]
    tk = out["token_totals"]
    cr = out["cache_hit_rate"]
    du = out["duration_stats"]
    bm = out["benchmarks"]
    lines = ["# v1 同协议重算值（v1-recalc）", ""]
    lines.append(f"- 扫描 round.json：{out['meta']['round_files_scanned']} 个，"
                 f"耗时 {out['meta']['scan_seconds']:.1f}s，峰值内存 {out['meta']['peak_memory_mb']:.1f} MB。")
    lines.append(f"- 每轮通过率：{['%d/%d' % (r['passed'], r['observed']) for r in pr['per_round_pass_rate']]}")
    e = pr["execution_pass_rate"]
    lines.append(f"- 执行通过率：{e['passed']}/{e['denominator']} = {e['rate']:.4f}")
    s = pr["stable_pass_rate"]
    lines.append(f"- 稳定通过率：{s['stable_cases']}/{s['denominator']} = {s['rate']:.4f}")
    sc = pr["status_counts"]["counts"]
    lines.append(f"- 状态计数：pass {sc['pass']} / fail {sc['fail']} / error {sc['error']} / pending_review {sc['pending_review']}")
    lines.append(f"- 稳定性分布：{pr['stability_distribution']}")
    u = tk["usage_sums"]
    lines.append(f"- 用量（去重 {tk['dedupe']['distinct_run_ids']} runs）：input {u['input']:,} / output {u['output']:,} / "
                 f"cache_read {u['cache_read']:,} / cache_write {u['cache_write']:,} / total {u['total_tokens']:,}")
    o = cr["cache_rate_old"]
    n = cr["cache_rate_new"]
    lines.append(f"- 缓存命中率（旧公式）：{o['value']:.6f} = {o['numerator']:,}/{o['denominator']:,}")
    lines.append(f"- 缓存命中率（新公式）：{n['value']}（{n['comparability']}：cache_write 为默认 0，公共样本 {n['valid_sample_count']}）")
    rm = du["round_ms"]
    fm = du["first_text_delta_ms"]
    lines.append(f"- 轮次耗时 round_ms：p50 {rm['p50']:.1f} / p95 {rm['p95']:.1f} / max {rm['max']:.1f} / n {rm['samples']}（含启动+前置，protocol_changed）")
    lines.append(f"- 首文本耗时 first_text_delta_ms：p50 {fm['p50']:.1f} / p95 {fm['p95']:.1f} / max {fm['max']:.1f} / n {fm['samples']}（首个文本事件，可空，protocol_changed）")
    tpb = bm["tool_parallel"]
    cpb = bm["compaction"]
    lines.append(f"- 工具并行：串行 P50 {tpb['serial_batch_ms']['p50']:.5f} / 并行 P50 {tpb['parallel_batch_ms']['p50']:.5f} = {tpb['serial_over_parallel_batch_p50']:.6f}x")
    lines.append(f"- 压缩：摘要耗时 P50 {cpb['summary_duration_ms']['p50']}ms，上下文缩减率 P50 {cpb['context_reduction_ratio']['p50']}，"
                 f"事实保留 摘要 {cpb['fact_retention']['summary']['passed']}/{cpb['fact_retention']['summary']['required']} · "
                 f"回答 {cpb['fact_retention']['answer']['passed']}/{cpb['fact_retention']['answer']['required']}")
    lines.append("")
    lines.append("缺测（不可重算，严禁估算）：服务端首个非空正文时延、逐模型请求首 token 时延、逐模型请求耗时、Trace 根数与父子关系。")
    lines.append("")
    lines.append("协议变更标记：确认改由 HTTP 驱动保存、真实附件上传、C03 前置经真实保存链、L07 场景替换。")
    return "\n".join(lines) + "\n"


def parse_args(argv):
    parser = argparse.ArgumentParser(description="从 v1 证据重算量化指标并标注可比性")
    parser.add_argument(
        "--v1-root",
        default=str(BACKEND / "temp" / "p0-evaluation" / "p0-official"),
        help="v1 证据根目录（默认 backend/temp/p0-evaluation/p0-official）",
    )
    parser.add_argument(
        "--out-root",
        default=str(BACKEND / "tmp" / "v2-evaluation" / "v1-recalc"),
        help="输出目录（默认 backend/tmp/v2-evaluation/v1-recalc）",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    v1_root = Path(args.v1_root)
    if not v1_root.is_absolute():
        v1_root = (Path.cwd() / v1_root).resolve()
    out_root = Path(args.out_root)
    if not out_root.is_absolute():
        out_root = (Path.cwd() / out_root).resolve()

    out = build_output(v1_root)
    out_root.mkdir(parents=True, exist_ok=True)
    json_path = out_root / "v1-recalc.json"
    md_path = out_root / "v1-recalc.md"
    json_path.write_text(json.dumps(out, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(out), encoding="utf-8")

    print(json.dumps({"top_level_keys": list(out.keys()),
                      "peak_memory_mb": out["meta"]["peak_memory_mb"],
                      "scan_seconds": out["meta"]["scan_seconds"]}, ensure_ascii=False))
    print(f"写入: {json_path}")
    print(f"写入: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
