from types import SimpleNamespace

from app.ai.messages import TextContent, ToolResultMessage
from test.check_compaction_performance import (
    FACTS,
    HISTORY_PARTS,
    REPS,
    absolute_relative_error,
    aggregate,
    fact_check,
    real_over_estimate,
    spread,
)
from test.check_tool_parallel_performance import (
    FILES,
    PAIRS,
    batch_timings,
    correctness_of,
    mode_stats,
    sample_of,
    summarize,
)
from test.p0_contract import BenchmarkResult


def tool_result(call_id: str, body: str, *, is_error: bool = False) -> ToolResultMessage:
    return ToolResultMessage(
        role="toolResult",
        tool_call_id=call_id,
        tool_name="read",
        content=[TextContent(type="text", text=body)],
        is_error=is_error,
        timestamp=0,
    )


def batch(prepare_started_at: float, first_result_at, finalized_at: float, messages):
    return SimpleNamespace(
        prepare_started_at=prepare_started_at,
        first_result_at=first_result_at,
        finalized_at=finalized_at,
        messages=messages,
    )


def full_body() -> str:
    return "1: line-00000 " + "x" * 8 + "\n800: line-00799 " + "x" * 8


def test_summarize_reports_sample_size_and_percentiles():
    values = [float(index) for index in range(1, 101)]
    stats = summarize(values)
    assert stats["n"] == 100
    assert stats["p50"] == 50.5
    assert stats["p95"] == 95.05


def test_batch_timings_converts_timestamps_to_ms():
    outcome = batch(0.0, 0.010, 0.050, [])
    timings = batch_timings(outcome)
    assert timings["batch_ms"] == 50.0
    assert timings["first_ms"] == 10.0
    assert batch_timings(batch(1.0, None, 1.5, []))["first_ms"] is None


def test_correctness_counts_denominator_and_failures():
    messages = [
        tool_result("c00", full_body()),
        tool_result("c01", full_body(), is_error=True),
        tool_result("c02", "1: line-00000 缺少末行"),
    ]
    verdict = correctness_of(messages)
    assert verdict["results"] == 3
    assert verdict["correct"] == 1
    assert verdict["errors"] == 1
    assert [item["has_last_line"] for item in verdict["details"]] == [True, True, False]


def test_sample_of_keeps_paired_samples_distinct():
    result = batch(0.0, 0.005, 0.100, [tool_result(f"c{index:02d}", full_body()) for index in range(FILES)])
    finalized = [{"tool_call_id": f"c{index:02d}", "exec_ms": 1.0, "post_ms": 0.5} for index in range(FILES)]
    serial = sample_of("sequential", 0, 0, result, finalized)
    parallel = sample_of("parallel", 0, 1, result, finalized)
    assert serial["batch_ms"] == 100.0
    assert serial["first_ms"] == 5.0
    assert serial["result_count"] == FILES
    assert serial["correct"] == FILES and serial["errors"] == 0
    keys = {(sample["pair"], sample["mode"]) for sample in (serial, parallel)}
    assert len(keys) == 2


def test_mode_stats_and_serial_over_parallel_ratio():
    samples = [
        {"pair": 0, "mode": "sequential", "batch_ms": 100.0, "first_ms": 1.0},
        {"pair": 0, "mode": "parallel", "batch_ms": 50.0, "first_ms": 5.0},
        {"pair": 1, "mode": "sequential", "batch_ms": 120.0, "first_ms": 2.0},
        {"pair": 1, "mode": "parallel", "batch_ms": 60.0, "first_ms": 6.0},
    ]
    serial = mode_stats(samples, "sequential")
    parallel = mode_stats(samples, "parallel")
    assert serial["batch"]["n"] == 2 and parallel["batch"]["n"] == 2
    assert serial["batch"]["p50"] == 110.0
    assert parallel["batch"]["p50"] == 55.0
    assert serial["batch"]["p50"] / parallel["batch"]["p50"] == 2.0


def test_tool_protocol_constants_anchor_denominator():
    assert PAIRS == 30 and FILES == 64
    assert 2 * PAIRS * FILES == 3840


def test_estimate_ratios_use_real_and_estimate_denominators():
    assert real_over_estimate(100, 71) == 0.71
    assert absolute_relative_error(100, 71) == 29 / 71
    assert absolute_relative_error(71, 71) == 0


def test_fact_check_reports_passed_over_required():
    body = "增肌 肩部过顶 60 80 待确认"
    verdict = fact_check(body)
    assert verdict["required"] == len(FACTS) == 5
    assert verdict["passed"] == 5
    assert fact_check("增肌")["passed"] == 1
    assert all("present" in item for item in verdict["checks"])


def test_compaction_aggregate_counts_each_sample_once():
    def sample(session: str, reduction: float, passed: int) -> dict:
        return {
            "sample": session,
            "context_reduction_ratio": reduction,
            "summary": {"real_over_estimate_ratio": 0.7, "absolute_relative_error": 0.4},
            "timing_ms": {"summary_ms": 1000},
            "fact_retention": {
                "summary": {"passed": passed, "required": len(FACTS)},
                "answer": {"passed": passed, "required": len(FACTS)},
            },
        }

    samples = [sample("a", 0.7, 5), sample("b", 0.71, 4), sample("c", 0.72, 5)]
    stats = aggregate(samples)
    assert stats["n"] == len(samples) == REPS
    assert stats["fact_retention"]["summary"] == {"passed": 14, "required": 15}
    assert stats["fact_retention"]["answer"] == {"passed": 14, "required": 15}
    assert stats["context_reduction_ratio"]["median"] == 0.71
    assert spread([1.0, 2.0, 3.0]) == {"min": 1.0, "median": 2.0, "max": 3.0}


def test_compaction_protocol_constants_anchor_history_length():
    assert REPS == 3
    assert HISTORY_PARTS == 5
    assert len(FACTS) == 5


def test_benchmark_result_contract_accepts_both_topics():
    result = BenchmarkResult(
        topic="tool_parallel",
        env={"scope": "固定文件读取负载的工具调度性能"},
        samples=[],
        stats={},
        correctness={"denominator": 3840},
    )
    assert result.topic == "tool_parallel"
    assert result.status == "complete"
    assert result.missing == [] and result.review == []
    compaction = BenchmarkResult(
        topic="compaction",
        env={},
        samples=[],
        stats={},
        correctness={},
        missing=["费用金额"],
        status="incomplete",
    )
    assert compaction.topic == "compaction" and compaction.status == "incomplete"
