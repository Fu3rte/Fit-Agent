from uuid import uuid4

from app.ai.messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    Usage,
)
from app.domain.session.models import CompactionEntry, SessionMessageEntry
from test.p0_contract import (
    CATEGORY_QUOTAS,
    ROUNDS,
    CaseCheck,
    CaseRoundObservation,
    CaseTurn,
    CheckResult,
    P0Case,
    TurnObservation,
)
from test.p0_eval_support import aggregate, summarize_run_usage, validate_cases

USAGE = {"input": 10, "output": 5, "cache_read": 2, "cache_write": 1, "total_tokens": 18}


def build_case(case_id: str, category: str, *, turns=None, checks=None, manual_review=None) -> P0Case:
    return P0Case(
        case_id=case_id,
        category=category,
        turns=turns if turns is not None else [CaseTurn(text="请求")],
        checks=checks
        if checks is not None
        else [CaseCheck(check_id="c1", kind="deterministic", target="t", rule="r")],
        manual_review=manual_review or [],
        product_basis="依据",
    )


def full_case_set() -> list[P0Case]:
    from test.p0_contract import CASE_ID_PREFIXES

    cases = []
    for category, quota in CATEGORY_QUOTAS.items():
        prefix = CASE_ID_PREFIXES[category]
        for index in range(quota):
            cases.append(build_case(f"{prefix}{index + 1:02d}", category))
    return cases


def turn_obs(
    case_id: str,
    round_index: int,
    *,
    run_id: str | None = None,
    usage: dict | None = USAGE,
    terminal: str = "done",
    duration_ms: float = 100.0,
    first_text_delta_ms: float | None = 40.0,
    turn_index: int = 0,
) -> TurnObservation:
    return TurnObservation(
        case_id=case_id,
        round_index=round_index,
        turn_index=turn_index,
        request_text="请求",
        session_id=str(uuid4()),
        operation_id=str(uuid4()),
        run_id=run_id or str(uuid4()),
        request_entry_id=str(uuid4()),
        terminal=terminal,
        events=[],
        tool_calls=[],
        before_state={},
        after_state={},
        usage=usage,
        first_text_delta_ms=first_text_delta_ms,
        duration_ms=duration_ms,
    )


def check_result(case_id: str, round_index: int, status: str) -> CheckResult:
    return CheckResult(
        case_id=case_id,
        round_index=round_index,
        check_id="c1",
        kind="deterministic",
        status=status,
        reason="原因",
    )


def round_obs(case_id: str, round_index: int, status: str, **kwargs) -> CaseRoundObservation:
    turns = kwargs.get("turns")
    checks = kwargs.get("checks")
    return CaseRoundObservation(
        case_id=case_id,
        round_index=round_index,
        session_id=str(uuid4()),
        turns=turns if turns is not None else [turn_obs(case_id, round_index)],
        checks=checks if checks is not None else [],
        usage=None,
        duration_ms=kwargs.get("duration_ms", 200.0),
        status=status,
    )


def test_validate_cases_accepts_full_quota_and_reports_errors():
    cases = full_case_set()
    assert validate_cases(cases) == []
    duplicate = [*cases, build_case("P01", "profile")]
    assert any("重复" in error for error in validate_cases(duplicate))
    wrong_prefix = [build_case("Z99", "profile"), *cases[1:]]
    prefix_errors = validate_cases(wrong_prefix)
    assert any("前缀" in error for error in prefix_errors)
    assert not any("数量" in error for error in prefix_errors)
    quota_errors = validate_cases(cases[:-1])
    assert any("数量" in error for error in quota_errors)


def test_validate_cases_rejects_empty_turns_checks_and_blank_reference():
    from test.p0_contract import ManualReviewItem

    empty_turns = [build_case("P01", "profile", turns=[])]
    assert any("缺少用户回合" in error for error in validate_cases(empty_turns))
    empty_checks = [build_case("P01", "profile", checks=[])]
    assert any("缺少检查项" in error for error in validate_cases(empty_checks))
    review = [build_case("P01", "profile", manual_review=[ManualReviewItem(review_id="m1", standard="s", evidence_refs=[""])])]
    assert any("空证据引用" in error for error in validate_cases(review))
    unknown_category = [build_case("P01", "unknown")]
    assert any("未知类别" in error for error in validate_cases(unknown_category))


def test_failure_denominator_keeps_failed_rounds():
    rounds = [
        round_obs("P01", 0, "pass"),
        round_obs("P01", 1, "pass"),
        round_obs("P02", 0, "fail", checks=[check_result("P02", 0, "fail")]),
        round_obs("P02", 1, "pass"),
    ]
    summary = aggregate(rounds)
    assert summary["executions"] == 4
    assert summary["failure_denominator"] == 4
    assert summary["passed_executions"] == 3
    assert summary["execution_pass_rate"] == 3 / 4
    assert summary["independent_cases"] == 2
    assert summary["expected_cases"] == sum(CATEGORY_QUOTAS.values())
    assert summary["per_round"][0] == {"round_index": 0, "observed": 2, "passed": 1, "rate": 0.5}
    assert {(item["case_id"], item["round_index"]) for item in summary["failures"]} == {
        ("P02", 0)
    }


def test_stability_distribution_over_configured_rounds():
    rounds = []
    for round_index in range(ROUNDS):
        rounds.append(round_obs("P01", round_index, "pass"))
        rounds.append(round_obs("P02", round_index, "pass" if round_index == 0 else "fail"))
        rounds.append(round_obs("P03", round_index, "fail"))
    summary = aggregate(rounds)
    assert summary["stability_distribution"] == {"0/2": 1, "1/2": 1, "2/2": 1}
    assert summary["stable_cases"] == 1
    assert summary["stable_pass_rate"] == 1 / 3
    assert summary["executions"] == 6


def test_pending_review_is_not_a_pass():
    rounds = [
        round_obs("P01", 0, "pending_review", checks=[check_result("P01", 0, "pending_review")]),
        round_obs("P01", 1, "pass"),
    ]
    summary = aggregate(rounds)
    assert summary["passed_executions"] == 1
    assert summary["stable_cases"] == 0
    assert summary["stability_distribution"]["1/2"] == 1
    assert summary["pending_review"][0]["check_id"] == "c1"
    assert {item["round_index"] for item in summary["failures"]} == set()


def test_usage_deduplicated_by_run_id():
    shared_run = "00000000-0000-4000-8000-000000000001"
    rounds = [
        round_obs(
            "P01",
            0,
            "pass",
            turns=[turn_obs("P01", 0, run_id=shared_run, usage=USAGE)],
        ),
        round_obs(
            "P01",
            1,
            "pass",
            turns=[turn_obs("P01", 1, run_id=shared_run, usage=USAGE)],
        ),
    ]
    summary = aggregate(rounds)
    assert summary["usage"]["input"] == 10
    assert summary["usage"]["total_tokens"] == 18
    assert summary["usage"]["runs"] == 1
    assert summary["usage"]["duplicate_runs"] == [shared_run]


def test_summary_usage_counted_once():
    run_id = "00000000-0000-4000-8000-000000000002"
    assistant_usage = Usage(input=10, output=4, cache_read=2, cache_write=1, total_tokens=17)
    compaction_usage = Usage(input=6, output=3, cache_read=0, cache_write=0, total_tokens=9)
    assistant = AssistantMessage(
        role="assistant",
        content=[TextContent(type="text", text="完成")],
        api="openai-completions",
        provider="stepfun",
        model="step-3.7-flash",
        usage=assistant_usage,
        stop_reason="stop",
        timestamp=1,
    )
    entries = [
        SessionMessageEntry(
            session_id=str(uuid4()),
            id="e1",
            parent_id=None,
            run_id=run_id,
            type="message",
            messages=[assistant],
            created_at=1,
        ),
        CompactionEntry(
            session_id=str(uuid4()),
            id="c1",
            parent_id="e1",
            run_id=run_id,
            type="compaction",
            summary="摘要",
            first_kept_entry_id="e1",
            tokens_before=100,
            usage=compaction_usage,
            system_message=SystemMessage(role="system", content="", timestamp=1),
            details=None,
            created_at=2,
        ),
    ]
    usage, missing = summarize_run_usage(entries, run_id)
    assert missing == []
    assert usage == {"input": 16, "output": 7, "cache_read": 2, "cache_write": 1, "total_tokens": 26}
    other_usage, other_missing = summarize_run_usage(entries, "00000000-0000-4000-8000-0000000000ff")
    assert other_usage is None
    assert other_missing == [
        {"entry_id": None, "stop_reason": None, "usage": None, "reason": "本次 run 无持久化条目"}
    ]


def test_missing_usage_is_kept_as_missing():
    run_id = "00000000-0000-4000-8000-000000000003"
    aborted = AssistantMessage(
        role="assistant",
        content=[TextContent(type="text", text="中断")],
        api="openai-completions",
        provider="stepfun",
        model="step-3.7-flash",
        usage=None,
        stop_reason="aborted",
        timestamp=1,
    )
    entries = [
        SessionMessageEntry(
            session_id=str(uuid4()),
            id="e1",
            parent_id=None,
            run_id=run_id,
            type="message",
            messages=[aborted],
            created_at=1,
        )
    ]
    usage, missing = summarize_run_usage(entries, run_id)
    assert usage is None
    assert missing == [
        {
            "entry_id": "e1",
            "stop_reason": "aborted",
            "usage": None,
            "reason": "助手消息缺少真实 usage",
        }
    ]
    rounds = [
        round_obs("P01", 0, "pass", turns=[turn_obs("P01", 0, run_id=run_id, usage=None)])
    ]
    summary = aggregate(rounds)
    assert summary["usage"]["missing_turns"] == 1
    assert summary["missing_usage"][0]["run_id"] == run_id


def test_duration_statistics_use_real_values():
    rounds = [
        round_obs("P01", 0, "pass", duration_ms=100.0, turns=[turn_obs("P01", 0, duration_ms=100.0, first_text_delta_ms=30.0)]),
        round_obs("P01", 1, "pass", duration_ms=300.0, turns=[turn_obs("P01", 1, duration_ms=300.0, first_text_delta_ms=90.0)]),
    ]
    summary = aggregate(rounds)
    durations = summary["durations"]
    assert durations["round_ms"] == {"n": 2, "median": 200.0, "min": 100.0, "max": 300.0}
    assert durations["first_text_delta_ms"] == {"n": 2, "median": 60.0, "min": 30.0, "max": 90.0}


def test_missing_rounds_are_reported():
    rounds = [
        round_obs("W01", 0, "pass"),
        round_obs("W02", 0, "pass"),
        round_obs("W02", 1, "pass"),
    ]
    summary = aggregate(rounds)
    assert summary["missing_rounds"] == [{"case_id": "W01", "missing_rounds": [1]}]
    assert summary["categories"]["workout"]["cases"] == 2
    assert summary["categories"]["workout"]["stable_cases"] == 1
