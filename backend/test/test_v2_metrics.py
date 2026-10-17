"""v2 自检：用例校验、分位数与通过率聚合、缓存公式、预算预占/对账。"""

import argparse
import asyncio
import copy
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

import test.v2_eval_support as eval_support
from app.agent import telemetry
from app.agent.telemetry import SpanName, SpanRecord, SpanStatus
from test.run_v2_eval import (
    LEDGER_STATUSES,
    budget_pause_guard,
    build_comparison,
    build_execution_ledger,
    current_identity,
    load_round_evidence,
    load_v1_recalc,
    pairs_from_plan,
    release_check,
    slot_pairs,
)
from test.v2_attempts import budget_path, claim_slot, read_record, write_record
from test.v2_budget import DEFAULT_LIMITS, BudgetExceeded, BudgetExhausted, BudgetLedger
from test.v2_business_cases import read_state
from test.v2_contract import (
    CATEGORY_QUOTAS,
    ROUNDS,
    CheckResult,
    ConfirmationObservation,
    RoundObservation,
    TurnObservation,
    V2Step,
)
from test.v2_eval_support import (
    BACKEND,
    USAGE_FIELDS,
    _quantile,
    _round_status,
    _run_confirm,
    _select_target,
    _span,
    _usage_totals,
    aggregate,
    apply_v2_review,
    build_review_packet,
    compare_baseline,
    experiment_env,
    first_non_empty_body_ms,
    install_embedding_recorder,
    load_cases,
    slot_plan,
    validate_cases,
)
from test.v2_test_paths import OUTPUT_ROOT

CASES = Path(__file__).resolve().parent / "v2_cases.json"
# 中间产物一律写入 gitignore 的本地目录，禁止使用 /tmp。
SELFCHECK_ROOT = OUTPUT_ROOT / "selfcheck" / uuid4().hex[:12]
@pytest.fixture
def tmp_path():
    root = OUTPUT_ROOT / "pytest-r2/m" / uuid4().hex[:12] / "case"
    root.mkdir(parents=True)
    from app import model_config

    original = model_config.DATA_ROOT
    try:
        yield root
    finally:
        model_config.DATA_ROOT = original


SESSION = "00000000-0000-4000-8000-000000000001"
EXPERIMENT_ID = "selfcheck-release"


def _round(case_id, round_index, status, slot=None, usage=None, turns=None, confirmations=None):
    from test.v2_contract import RoundObservation

    return RoundObservation(
        case_id=case_id,
        round_index=round_index,
        slot=slot if slot is not None else 0,
        session_id="00000000-0000-4000-8000-000000000001",
        process_index=0,
        turns=turns or [],
        confirmations=confirmations or [],
        checks=[],
        usage=usage,
        duration_ms=1000.0,
        status=status,
    )


def test_cases_valid_and_quotas():
    cases = load_cases(CASES)
    assert len(cases) == 50
    assert validate_cases(cases) == []
    assert len({c.case_id for c in cases}) == 50


def test_cases_reject_wrong_quota():
    cases = load_cases(CASES)
    trimmed = cases[:-1]
    errors = validate_cases(trimmed)
    assert any("配额" in e for e in errors)


def test_quantile_and_span_range():
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    span = _span(values)
    assert span["samples"] == 5
    assert span["max"] == 50.0
    assert span["p50"] == 30.0
    assert span["p95"] >= 40.0 and span["p95"] <= 50.0
    assert _span([]) == {"samples": 0, "mean": None, "p50": None, "p95": None, "max": None}
    single = _span([7.0])
    assert single["p50"] == single["p95"] == single["mean"] == 7.0


def test_quantile_single_sample_equals_value():
    assert _quantile([3.0], 0.95) == 3.0


def test_aggregate_rates_and_stability():
    rounds = []
    # 3 个用例：3/3、2/3、0/3。
    for index in range(ROUNDS):
        rounds.append(_round("P01", index, "pass"))
    for index in range(ROUNDS):
        rounds.append(_round("P02", index, "pass" if index < 2 else "fail"))
    for index in range(ROUNDS):
        rounds.append(_round("P03", index, "fail"))
    result = aggregate(rounds)
    assert result["independent_cases"] == 3
    assert result["executions"] == 9
    assert result["passed_executions"] == 5
    assert result["stable_cases"] == 1
    assert abs(result["stable_pass_rate"] - 1 / 3) < 1e-9
    assert result["stability_distribution"]["3/3"] == 1
    assert result["stability_distribution"]["2/3"] == 1
    assert result["stability_distribution"]["0/3"] == 1
    assert result["categories"]["profile"]["cases"] == 3


def test_cache_hit_rate_formula_and_samples():
    def usage(input_, cache_read, cache_write):
        return {"input": input_, "output": 0, "cache_read": cache_read, "cache_write": cache_write, "total_tokens": 0}

    from test.v2_contract import TurnObservation

    reported = {field: "reported" for field in USAGE_FIELDS}

    def turn(run_id, u):
        return TurnObservation(
            case_id="P01", round_index=0, step_index=1, kind="user",
            session_id="00000000-0000-4000-8000-000000000001",
            operation_id=run_id, run_id=run_id, terminal="done", status="done",
            usage=u, usage_sources=reported,
        )

    rounds = [
        _round("P01", 0, "pass", turns=[turn("00000000-0000-4000-8000-0000000000aa", usage(10, 5, 5))]),
        _round("P01", 1, "pass", turns=[turn("00000000-0000-4000-8000-0000000000bb", usage(0, 0, 0))]),
    ]
    result = aggregate(rounds)
    totals = result["usage"]
    assert totals["cache_samples"] == 2
    assert totals["cache_read_sum"] == 5
    assert totals["cache_input_sum"] == 20
    assert abs(totals["cache_hit_rate"] - 0.25) < 1e-9


def test_cache_hit_null_when_denominator_zero():
    from test.v2_contract import TurnObservation

    turn = TurnObservation(
        case_id="P01", round_index=0, step_index=1, kind="user",
        session_id="00000000-0000-4000-8000-000000000001",
        operation_id="00000000-0000-4000-8000-0000000000aa", run_id="00000000-0000-4000-8000-0000000000aa",
        terminal="done", status="done",
        usage={"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total_tokens": 0},
    )
    result = aggregate([_round("P01", 0, "pass", turns=[turn])])
    assert result["usage"]["cache_hit_rate"] is None


def test_execution_ledger_counts_not_executed(tmp_path):
    cases = load_cases(CASES)
    rounds = [_round(cases[0].case_id, r, "pass", slot=r) for r in range(2)]
    ledger = build_execution_ledger(slot_plan(cases), rounds, tmp_path)
    assert ledger["planned_slots"] == 150
    assert ledger["executed_slots"] == 2
    assert sum(1 for s in ledger["slots"] if s["status"] == "not_executed") == 148


def test_comparison_columns_fixed():
    cases = load_cases(CASES)
    summary = {"aggregate": aggregate([_round(cases[0].case_id, r, "pass") for r in range(ROUNDS)])}
    rows = build_comparison({}, summary)
    assert rows[0] == ["指标", "v1 原始值", "v1 同口径重算值", "v2 实测值", "变化量", "相对变化", "两侧样本数与可比性", "证据位置"]
    assert all(len(row) == 8 for row in rows)


def test_budget_reserve_reconcile_and_exceeded(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.json", {"m": 100}, experiment_id="e")
    ledger.reserve("m", "a1", 40, basis="provider-count")
    snapshot = ledger.snapshot()
    assert snapshot["models"]["m"]["reserved"] == 40
    ledger.reconcile("m", "a1", 30)
    snapshot = ledger.snapshot()
    assert snapshot["models"]["m"]["actual"] == 30
    assert snapshot["models"]["m"]["reserved"] == 0
    with pytest.raises(BudgetExceeded):
        ledger.reserve("m", "a2", 80, basis="provider-count")


def test_budget_unknown_retains_reservation(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.json", {"m": 100}, experiment_id="e")
    ledger.reserve("m", "a1", 25, basis="provider-count")
    ledger.mark_unknown("m", "a1")
    snapshot = ledger.snapshot()
    assert snapshot["models"]["m"]["unknown"] == 25
    assert snapshot["models"]["m"]["reserved"] == 25


def test_budget_cross_process_file_persists(tmp_path):
    path = tmp_path / "budget.json"
    first = BudgetLedger(path, {"m": 100}, experiment_id="e")
    first.reserve("m", "a1", 10, basis="provider-count")
    second = BudgetLedger(path, {"m": 100}, experiment_id="e")
    assert second.snapshot()["models"]["m"]["reserved"] == 10
    assert json.loads(path.read_text(encoding="utf-8"))["models"]["m"]["reserved"] == 10


# ---------- 槽位隔离与负向断言材料 ----------


def _force_remove(path: Path) -> None:
    # 附件存储会把上传文件置为只读，Windows 删除前需清除只读位。
    def reopen(func, target, error):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onexc=reopen)


def _fresh(name: str) -> Path:
    # 目录名带进程号：并行自检进程各自持有独立证据根，互不覆盖。
    path = SELFCHECK_ROOT / f"{name}-p{os.getpid()}"
    if path.exists():
        _force_remove(path)
    path.mkdir(parents=True)
    return path


def _record(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_record(path, payload)
    return path


def test_slot_paths_are_isolated(tmp_path):
    cases = load_cases(CASES)
    assert validate_cases(cases) == []
    assert [sum(1 for item in cases if item.category == category) for category in CATEGORY_QUOTAS] == list(
        CATEGORY_QUOTAS.values()
    )
    plan = slot_plan(cases)
    assert len(plan) == 150
    assert [record["slot"] for record in plan] == list(range(150))
    assert {(record["case_id"], record["round_index"]) for record in plan} == {
        (case.case_id, round_index) for case in cases for round_index in range(ROUNDS)
    }
    assert [record["slot"] for record in slot_plan(cases, smoke=True)] == list(range(50))

    root = tmp_path / "experiment"
    root.mkdir()
    identity = {"provider": "stepfun", "api": "openai-completions", "model": "step-3.7-flash", "base_url": "https://x/"}
    by_id = {case.case_id: case for case in cases}
    left_slot, left_case, left_round = slot_pairs(cases)[0]
    right_slot, right_case, right_round = slot_pairs(cases)[ROUNDS]
    left = experiment_env("experiment", by_id[left_case], left_round, left_slot, 0, root, identity, "2026-01-01")
    right = experiment_env("experiment", by_id[right_case], right_round, right_slot, 1, root, identity, "2026-01-01")
    assert (left.case_id, left.round_index, left.slot) != (right.case_id, right.round_index, right.slot)
    for field in ("database_path", "trace_path", "attachment_root", "evidence_dir"):
        for env in (left, right):
            path = Path(getattr(env, field)).resolve()
            assert path.is_relative_to(root.resolve()), field
        first, second = Path(getattr(left, field)), Path(getattr(right, field))
        assert first != second, field
    # 同一实验共用一个执行根与一本预算账本，槽位隔离由上述四类路径承担。
    assert left.execution_root == right.execution_root == str(root)
    assert Path(left.budget_ledger_path) == Path(right.budget_ledger_path) == budget_path(root)


class _StubUrl:
    def __init__(self, path: str) -> None:
        self.path = path


class _StubResponse:
    def __init__(self, url: _StubUrl, status_code: int, payload: dict) -> None:
        self.url = url
        self.status_code = status_code
        self._payload = payload
        self.headers = {"content-type": "application/json"}

    def json(self) -> dict:
        return self._payload


class _StubHttp:
    # 只记录 (method, path, json) 并回放预设响应：真实端点派发由 _run_confirm 决定。

    def __init__(self, on_post=None) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self._on_post = on_post

    def post(self, path, json=None):
        self.calls.append(("POST", path, json))
        if self._on_post is not None:
            self._on_post(path, json)
        return _StubResponse(
            _StubUrl(path), 422, {"detail": {"code": "invalid_request", "message": "请求体字段非法"}}
        )


def _empty_database(path: Path) -> Path:
    # 用应用自身的建库入口生成空库：负向步骤的受保护状态由真实 read_state 读取。
    from app.infrastructure.persistence.sqlite import database as database_module

    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)

    async def build() -> None:
        store = await database_module.open_database(path)
        await store.close()

    asyncio.run(build())
    return path


def _seed_profile_row(path: Path) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("INSERT INTO profile (id, version, content, updated_at) VALUES (1, 1, '{}', 1)")
        connection.commit()
    finally:
        connection.close()


def _negative(step_kwargs: dict) -> V2Step:
    return V2Step.model_validate({"kind": "confirm_negative", "text": "确认保存", "operation": "neg-1", **step_kwargs})


def _prepared_candidates(count: int = 1) -> list[dict]:
    return [
        {
            "business_kind": "profile",
            "proposal_id": str(uuid4()),
            "display_entry_id": str(uuid4()),
            "tool": "prepare_profile_update",
        }
        for _ in range(count)
    ]


def test_negative_steps_carry_assertion_material():
    cases = load_cases(CASES)
    negatives = [
        (case.case_id, step)
        for case in cases
        for step in case.steps
        if step.kind == "confirm_negative"
    ]
    assert negatives
    assert all(step.break_field and step.expect_code for _cid, step in negatives)
    assert {step.entry for _cid, step in negatives} >= {"run", "edit", "steering"}

    mutated = [case.model_copy(deep=True) for case in cases]
    target = next(case for case in mutated if any(step.kind == "confirm_negative" for step in case.steps))
    step = next(step for step in target.steps if step.kind == "confirm_negative")
    step.break_field = None
    assert any("break_field" in error and target.case_id in error for error in validate_cases(mutated))
    step.break_field = "authorization_injection"
    step.expect_code = None
    assert any("expect_code" in error and target.case_id in error for error in validate_cases(mutated))
    step.expect_code = "invalid_request"
    step.entry = "confirmation"
    step.overrides = {"path_session": "other"}
    errors = validate_cases(mutated)
    assert any("跨会话目标" in error for error in errors)
    # 授权注入只能由非确认入口承载，否则该条件在这一步无法成立。
    assert any("授权注入" in error for error in errors)
    step.break_field = "display_entry_id"
    step.entry = "teleport"
    assert any("未知派发入口 teleport" in error for error in validate_cases(mutated))
    step.entry = "confirmation"
    step.overrides = {}
    assert validate_cases(mutated) == []

    database_path = _empty_database(_fresh("negative-steps") / "app.db")
    session_id = str(uuid4())
    run_id = str(uuid4())
    entry_id = str(uuid4())
    expected = {
        "run": "/api/agent/run",
        "edit": "/api/agent/edit",
        "steering": f"/api/agent/runs/{run_id}/steering",
    }
    observed_paths: list[str] = []
    for entry, path in expected.items():
        http = _StubHttp()
        prepared = _prepared_candidates()
        observation = _run_confirm(
            http, session_id, case_id="C01", round_index=0, step_index=2,
            step=_negative({"entry": entry, "business_kind": "profile", "target": "synthetic",
                            "break_field": "authorization_injection", "expect_code": "invalid_request"}),
            prepared=prepared, operations={}, negative=True,
            last_run_id=run_id, last_entry_id=entry_id, database_path=str(database_path),
        )
        assert [call[0] for call in http.calls] == ["POST"]
        assert http.calls[0][1] == path
        observed_paths.append(path)
        body = http.calls[0][2]
        assert body["operation_id"]
        assert body["business_kind"] == "profile"
        assert body["proposal_id"]
        assert body["display_entry_id"]
        if entry in ("run", "steering"):
            assert body["session_id"] == session_id
        if entry == "edit":
            assert body["target_entry_id"] == entry_id
        assert observation.endpoint == path
        assert observation.entry == entry
        assert observation.status_code == 422
        assert observation.rejection_code == "invalid_request"
        assert observation.path_session_id == session_id
        assertions = observation.assertions
        assert assertions["endpoint_hit"] is True
        assert assertions["condition_broken"] is True
        assert assertions["controlled_error"] is True
        assert assertions["zero_write"] is True
        assert assertions["observed_code"] == "invalid_request"
        assert assertions["expected_endpoint"] == path
        assert assertions["condition_details"]["injected_proposal_unknown"] is True
    assert len(set(observed_paths)) == 3

    # 受保护状态发生变化时零写入判定必须随之翻转。
    def writer(path, body):
        _seed_profile_row(database_path)

    http = _StubHttp(on_post=writer)
    observation = _run_confirm(
        http, session_id, case_id="C01", round_index=0, step_index=2,
        step=_negative({"entry": "run", "business_kind": "profile", "target": "synthetic",
                        "break_field": "authorization_injection", "expect_code": "invalid_request"}),
        prepared=_prepared_candidates(), operations={}, negative=True,
        last_run_id=run_id, last_entry_id=entry_id, database_path=str(database_path),
    )
    assert observation.assertions["zero_write"] is False
    assert "profile" in observation.assertions["state_changed_keys"]


def test_ambiguous_target_blocks_without_http(monkeypatch):
    prepared = _prepared_candidates(2)
    selected, reason = _select_target(prepared, [], "profile", "pending")
    assert selected is None
    assert "待确认目标数量" in reason and "2" in reason
    assert _select_target(prepared, [], "profile", None)[0] is None
    single, none_reason = _select_target(prepared[:1], [], "profile", "pending")
    assert none_reason is None and single == prepared[0]

    def forbidden() -> dict:
        raise AssertionError("目标未解析时不得读取业务状态")

    monkeypatch.setattr(eval_support, "business_state", forbidden)
    http = _StubHttp()
    observation = _run_confirm(
        http, str(uuid4()), case_id="C01", round_index=0, step_index=3,
        step=_negative({"entry": "run", "business_kind": "profile", "target": "pending",
                        "break_field": "authorization_injection", "expect_code": "invalid_request"}),
        prepared=prepared, operations={}, negative=True, last_run_id=str(uuid4()), last_entry_id=str(uuid4()),
    )
    assert http.calls == []
    assert observation.rejected is True
    assert observation.rejection_code == "target_unresolved"
    assert observation.error == reason


def test_first_body_requires_non_empty_text():
    def delta(t_ms: float, text: str) -> dict:
        return {
            "t_ms": t_ms,
            "event": "message_update",
            "data": {"update_type": "text_delta", "content": [{"type": "text", "text": text}]},
        }

    events = [
        {"t_ms": 1.0, "event": "message_start", "data": {"content": []}},
        {"t_ms": 4.0, "event": "tool_start",
         "data": {"tool_call_id": "call-1", "name": "prepare_profile_update", "arguments": {}}},
        {"t_ms": 6.0, "event": "message_update",
         "data": {"update_type": "thinking_delta", "content": [{"type": "thinking", "thinking": "思考内容"}]}},
        delta(8.0, ""),
        delta(10.0, "  \n\t "),
        {"t_ms": 11.0, "event": "message_update", "data": {"update_type": "text_delta", "content": []}},
        delta(12.5, "目"),
        delta(20.0, "标已确认"),
    ]
    assert first_non_empty_body_ms(events) == 12.5
    assert first_non_empty_body_ms(events[:6]) is None
    assert first_non_empty_body_ms([]) is None


def test_usage_dedupe_by_run_id():
    def usage(input_, cache_read):
        return {"input": input_, "output": 3, "cache_read": cache_read, "cache_write": 1,
                "total_tokens": input_ + cache_read + 4}

    def turn(run_id, u):
        return TurnObservation(
            case_id="P01", round_index=0, step_index=1, kind="user", session_id=SESSION,
            operation_id=str(uuid4()), run_id=run_id, terminal="done", status="done", usage=u,
        )

    run_id = str(uuid4())
    first, second = usage(100, 40), usage(700, 900)
    rounds = [
        _round("P01", 0, "pass", turns=[turn(run_id, first), turn(run_id, second), turn(str(uuid4()), None)]),
        _round("P01", 1, "pass", turns=[turn(str(uuid4()), usage(50, 10)), turn(str(uuid4()), None)]),
    ]
    totals = _usage_totals(rounds)
    assert totals["runs"] == 2
    assert totals["input"] == 150
    assert totals["cache_read"] == 50
    assert totals["output"] == 6
    assert totals["duplicate_runs"] == [run_id]
    assert totals["missing_turns"] == 2
    aggregated = aggregate(rounds)["usage"]
    assert aggregated["duplicate_runs"] == [run_id]
    assert aggregated["missing_turns"] == 2


def test_cache_common_sample_rules():
    reported = {field: "reported" for field in USAGE_FIELDS}

    def turn(run_id, u):
        return TurnObservation(
            case_id="P01", round_index=0, step_index=1, kind="user", session_id=SESSION,
            operation_id=run_id, run_id=run_id, terminal="done", status="done",
            usage=u, usage_sources=reported,
        )

    complete = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total_tokens": 0}
    totals = _usage_totals([_round("P01", 0, "pass", turns=[turn(str(uuid4()), complete)])])
    # 显式零值属确知样本：计入公共样本，分母为零时命中率记 null。
    assert totals["cache_samples"] == 1
    assert totals["cache_hit_rate"] is None

    with_key = {"input": 20, "output": 1, "cache_read": 10, "cache_write": 5, "total_tokens": 36}
    totals = _usage_totals([_round("P01", 0, "pass", turns=[turn(str(uuid4()), with_key)])])
    assert totals["cache_samples"] == 1
    assert abs(totals["cache_hit_rate"] - 10 / 35) < 1e-12

    # 缺少 cache_write 键的用量记录无法参与任何求和，就地失败而不是静默计入样本集。
    partial = {"input": 20, "output": 1, "cache_read": 10, "total_tokens": 31}
    with pytest.raises(KeyError):
        _usage_totals([_round("P01", 0, "pass", turns=[turn(str(uuid4()), partial)])])


def test_budget_threshold_uses_settled_actual_only(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.json", {"m": 100, "u": 50}, experiment_id="e")
    ledger.record_attempt("m", "a1", inputs=2, basis="真实 usage 记账")
    settled = ledger.settle("m", "a1", 100)
    assert settled["actual"] == 100
    assert settled["paused_at"]
    assert ledger.is_paused("m") is True
    with pytest.raises(BudgetExhausted) as refused:
        ledger.record_attempt("m", "a2", inputs=1, basis="真实 usage 记账")
    assert refused.value.actual == 100 and refused.value.limit == 100

    # 用量未知不移动阈值。
    ledger.record_attempt("u", "b1", inputs=1, basis="真实 usage 记账")
    ledger.settle("u", "b1", None, reason="CancelledError")
    assert ledger.is_paused("u") is False
    unknown = read_record(ledger.path)["models"]["u"]
    assert unknown["actual"] == 0
    assert unknown["paused_at"] is None
    assert unknown["attempts"][0]["state"] == "unknown"
    assert unknown["attempts"][0]["unknown_reason"] == "CancelledError"

    # 跨进程：同一路径的另一个实例看到同一暂停状态。
    other = BudgetLedger(tmp_path / "budget.json", {"m": 100, "u": 50}, experiment_id="e")
    assert other.is_paused("m") is True
    snapshot = other.snapshot()
    assert snapshot["models"]["m"]["trigger_actual"] == 100
    assert snapshot["models"]["u"]["unknown"] == 0

    # 阈值达成前已在途的请求仍可结算，真实用量继续如实累加。
    inflight = BudgetLedger(tmp_path / "inflight.json", {"m": 100}, experiment_id="e")
    inflight.record_attempt("m", "c1", inputs=1, basis="真实 usage 记账")
    inflight.record_attempt("m", "c2", inputs=1, basis="真实 usage 记账")
    inflight.settle("m", "c1", 100)
    assert inflight.is_paused("m") is True
    assert inflight.settle("m", "c2", 30)["actual"] == 130


_EMBEDDING_STATE_ATTRS = ("_v2_recorder_owner", "_v2_original_embed", "_v2_recorder_installed")
_MISSING = object()


def _embedding_state(EmbeddingClient):
    return {name: getattr(EmbeddingClient, name, _MISSING) for name in _EMBEDDING_STATE_ATTRS}


def _restore_embedding_state(EmbeddingClient, state):
    for name, value in state.items():
        if value is _MISSING:
            if hasattr(EmbeddingClient, name):
                delattr(EmbeddingClient, name)
        else:
            setattr(EmbeddingClient, name, value)


@pytest.fixture
def embedding_class_state(request):
    from app.application.business.retrieval_embedding import EmbeddingClient

    # 进入 fixture 立即保存真实原始状态；参数化前置只用于构造测试条件。
    original = _embedding_state(EmbeddingClient)
    initial = getattr(request, "param", "current")
    if initial == "absent":
        for name in _EMBEDDING_STATE_ATTRS:
            if hasattr(EmbeddingClient, name):
                delattr(EmbeddingClient, name)
        expected = {name: _MISSING for name in _EMBEDDING_STATE_ATTRS}
    elif initial == "present":
        EmbeddingClient._v2_recorder_owner = "seed-owner"
        EmbeddingClient._v2_original_embed = EmbeddingClient._embed
        EmbeddingClient._v2_recorder_installed = True
        expected = _embedding_state(EmbeddingClient)
    else:
        expected = original
    assert _embedding_state(EmbeddingClient) == expected

    for name in ("_v2_recorder_owner", "_v2_original_embed"):
        if hasattr(EmbeddingClient, name):
            delattr(EmbeddingClient, name)
    EmbeddingClient._v2_recorder_installed = False

    try:
        yield EmbeddingClient
    finally:
        _restore_embedding_state(EmbeddingClient, original)
        after = _embedding_state(EmbeddingClient)
        assert after == original, f"recorder 类状态未还原: {original} -> {after}"


def test_embedding_recorder_settles_real_usage_and_is_idempotent(tmp_path, monkeypatch, embedding_class_state):
    EmbeddingClient = embedding_class_state

    model = next(iter(DEFAULT_LIMITS))
    calls: list[int] = []
    mode = {"cancel": False}

    class _Usage:
        total_tokens = 1234
        prompt_tokens = 999

    class _Response:
        usage = _Usage()

    response = _Response()

    async def fake_embed(self, texts):
        calls.append(len(texts))
        if mode["cancel"]:
            raise asyncio.CancelledError
        return response

    class _Config:
        pass

    config = _Config()
    config.model = model
    fake_self = type("Fake", (), {"_config": config})()

    # fixture 记录调用前的完整类状态，并按全新类状态安装；teardown 精确还原。
    monkeypatch.setattr(EmbeddingClient, "_embed", fake_embed)
    ledger = BudgetLedger(_fresh("embedding-recorder") / "budget.json", DEFAULT_LIMITS, experiment_id="selfcheck")
    install_embedding_recorder(ledger, experiment_id="selfcheck", process_index=0)
    assert EmbeddingClient._embed is not fake_embed
    # 同一执行单元的包装器幂等：重复安装保持首次安装的进程归属，不产生第二层嵌套。
    install_embedding_recorder(ledger, experiment_id="selfcheck", process_index=1)

    assert asyncio.run(EmbeddingClient._embed(fake_self, ["第一条", "第二条"])) is response
    recorded = read_record(ledger.path)["models"][model]
    assert recorded["actual"] == 1234
    attempt = recorded["attempts"][0]
    assert attempt["state"] == "settled"
    assert attempt["inputs"] == 2
    assert attempt["actual"] == 1234
    assert attempt["prompt_tokens"] == 999
    assert attempt["elapsed_ms"] >= 0.0
    assert len(recorded["attempts"]) == 1 and len(calls) == 1

    mode["cancel"] = True
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(EmbeddingClient._embed(fake_self, ["取消"]))
    after = read_record(ledger.path)["models"][model]
    assert after["actual"] == 1234
    assert len(after["attempts"]) == 2
    cancelled = after["attempts"][1]
    assert cancelled["state"] == "unknown"
    assert "CancelledError" in cancelled["unknown_reason"]
    assert cancelled["elapsed_ms"] >= 0.0
    assert ledger.snapshot()["models"][model]["unknown_attempts"][0]["context"] == {
        "experiment_id": "selfcheck", "process_index": 0
    }


@pytest.mark.parametrize("embedding_class_state", ["absent", "present"], indirect=True)
def test_embedding_recorder_class_state_teardown(request, embedding_class_state, monkeypatch):
    EmbeddingClient = embedding_class_state

    async def fake_embed(self, texts):
        raise AssertionError("teardown 检查不应发起编码请求")

    monkeypatch.setattr(EmbeddingClient, "_embed", fake_embed)
    ledger = BudgetLedger(
        _fresh(f"embedding-state-{request.node.callspec.id}") / "budget.json",
        DEFAULT_LIMITS,
        experiment_id="state-check",
    )
    install_embedding_recorder(ledger, experiment_id="state-check", process_index=0)
    assert EmbeddingClient._v2_recorder_owner == "state-check"
    assert EmbeddingClient._v2_recorder_installed is True


def test_embedding_recorder_fresh_process_state():
    result_path = OUTPUT_ROOT / "embedding-class-state" / f"{uuid4().hex}.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join((str(BACKEND / "test"), str(BACKEND),
                                                 environment.get("PYTHONPATH", "")))
    environment["FIT_AGENT_B6_TEST_OUTPUT_ROOT"] = str(OUTPUT_ROOT)
    environment["EMBEDDING_CLASS_STATE_RESULT"] = str(result_path)
    completed = subprocess.run([sys.executable, str(BACKEND / "test/check_embedding_class_state.py")],
                               cwd=BACKEND, env=environment, capture_output=True, timeout=300)
    assert completed.returncode == 0, completed.stdout.decode(errors="replace") + completed.stderr.decode(errors="replace")
    result = read_record(result_path)
    assert result["exit_code"] == 0
    assert result["equal"] is True
    assert result["before"] == result["after"]


def _slot_trace(path: Path, *, session_id: str, run_id: str, operation_id: str, confirmations: list[str]) -> None:
    # 用真实观测库 API 写入与槽位观测一致的根记录，供 Trace 对账核对。
    now = time.time_ns() // 1_000_000
    agent = SpanRecord(
        trace_id=uuid4().hex, span_id=1, parent_span_id=None, session_id=session_id, run_id=run_id,
        operation_id=operation_id, name=SpanName.agent_run, started_at=now - 900, finished_at=now,
        duration_ms=900.0, status=SpanStatus.completed, error_code=None,
        attributes={"first_text_ms": 120.0},
    )
    model = SpanRecord(
        trace_id=agent.trace_id, span_id=2, parent_span_id=1, session_id=session_id, run_id=run_id,
        operation_id=operation_id, name=SpanName.model, started_at=now - 800, finished_at=now - 100,
        duration_ms=700.0, status=SpanStatus.completed, error_code=None,
        attributes={"provider": "stepfun", "model": "step-3.7-flash", "call_kind": "normal",
                    "stop_reason": "stop", "first_text_ms": 95.0, "input_tokens": 100, "output_tokens": 7,
                    "cache_read_tokens": 40, "cache_write_tokens": 10},
    )
    roots = [
        SpanRecord(
            trace_id=uuid4().hex, span_id=1, parent_span_id=None, session_id=session_id, run_id=None,
            operation_id=operation, name=SpanName.confirmation, started_at=now - 40, finished_at=now,
            duration_ms=40.0, status=SpanStatus.completed, error_code=None,
            attributes={"business_kind": "profile"},
        )
        for operation in confirmations
    ]

    async def save() -> None:
        store = await telemetry.open_trace_store(path)
        try:
            await store.save([agent, model])
            for root in roots:
                await store.save([root])
        finally:
            await store.close()

    asyncio.run(save())


def _release_rounds(root: Path):
    from test.v2_contract import ToolCallRecord

    cases = load_cases(CASES)[:2]
    case_a, case_b = cases[0].case_id, cases[1].case_id
    session_a, session_b = str(uuid4()), str(uuid4())
    run_a, run_b = str(uuid4()), str(uuid4())
    op_a, op_b = str(uuid4()), str(uuid4())
    confirm_a, confirm_b = str(uuid4()), str(uuid4())
    negative = {
        "endpoint_hit": True, "condition_broken": True, "controlled_error": True, "zero_write": True,
        "observed_code": "invalid_request", "expected_code": "invalid_request",
        "expected_endpoint": "/api/agent/run", "business_kind": "profile",
    }

    def user_turn(case_id, round_index, session_id, run_id, operation_id):
        return TurnObservation(
            case_id=case_id, round_index=round_index, step_index=1, kind="user", request_text="整理画像",
            session_id=session_id, operation_id=operation_id, run_id=run_id, request_entry_id=str(uuid4()),
            terminal="done", status="done",
            tool_calls=[ToolCallRecord(tool_call_id="call-1", name="prepare_profile_update", arguments={},
                                       is_error=False, content="{}", entry_id=str(uuid4()))],
            usage={"input": 100, "output": 7, "cache_read": 40, "cache_write": 10, "total_tokens": 157},
            client_first_body_ms=120.0, duration_ms=900.0,
        )

    def accepted(case_id, round_index, session_id, operation_id):
        return ConfirmationObservation(
            case_id=case_id, round_index=round_index, step_index=2, kind="confirm", business_kind="profile",
            operation_label="保存画像", operation_id=operation_id, target="pending",
            request={"operation_id": operation_id, "business_kind": "profile"},
            status_code=200, response={"status": "saved"}, rejected=False,
            endpoint=f"/api/sessions/{session_id}/confirmations", entry="confirmation",
            path_session_id=session_id, duration_ms=30.0,
        )

    def injection(case_id, round_index, session_id):
        return ConfirmationObservation(
            case_id=case_id, round_index=round_index, step_index=3, kind="confirm_negative",
            business_kind="profile", operation_label="注入授权", operation_id=str(uuid4()), target="pending",
            request={"session_id": session_id}, status_code=422,
            response={"detail": {"code": "invalid_request"}}, rejected=True, rejection_code="invalid_request",
            endpoint="/api/agent/run", entry="run", break_field="authorization_injection",
            expect_code="invalid_request", path_session_id=session_id, duration_ms=12.0, assertions=negative,
        )

    rounds = [
        RoundObservation(
            case_id=case_a, round_index=0, slot=0, session_id=session_a, process_index=0,
            turns=[user_turn(case_a, 0, session_a, run_a, op_a)],
            confirmations=[accepted(case_a, 0, session_a, confirm_a), injection(case_a, 0, session_a)],
            checks=[
                CheckResult(
                    case_id=case_a, round_index=0, check_id=check.check_id, kind=check.kind,
                    status="fail" if check.check_id == "P01-S1-01" else "pass",
                    reason="越界写入未被拦截" if check.check_id == "P01-S1-01" else "落库状态与期望一致",
                )
                for check in cases[0].checks
            ],
            usage={"input": 100, "output": 7, "cache_read": 40, "cache_write": 10, "total_tokens": 157},
            duration_ms=9000.0, startup_ms=1500.0, precondition_ms=2200.0, status="pass",
        ),
        RoundObservation(
            case_id=case_b, round_index=0, slot=1, session_id=session_b, process_index=1,
            turns=[user_turn(case_b, 0, session_b, run_b, op_b)],
            confirmations=[accepted(case_b, 0, session_b, confirm_b)],
            checks=[
                CheckResult(
                    case_id=case_b, round_index=0, check_id=check.check_id, kind=check.kind,
                    status="pending_review" if check.check_id == "P02-S2-05" else "pass",
                    reason="公开正文口径需人工审阅" if check.check_id == "P02-S2-05" else "口径符合",
                )
                for check in cases[1].checks
            ],
            usage={"input": 100, "output": 7, "cache_read": 40, "cache_write": 10, "total_tokens": 157},
            duration_ms=9000.0, startup_ms=1600.0, precondition_ms=2300.0, status="pending_review",
        ),
    ]
    for observation in rounds:
        _record(root / "cases" / observation.case_id / f"round-{observation.round_index}" / "round.json",
                observation.model_dump())
        _record(root / "claims" / f"{observation.case_id}-r{observation.round_index}.json", {
            "case_id": observation.case_id, "round_index": observation.round_index, "status": "claimed",
        })
    return cases, {case_a: (session_a, run_a, op_a, [confirm_a]), case_b: (session_b, run_b, op_b, [confirm_b])}


def test_release_check_blocks_on_fail_and_pending(tmp_path):
    from test.v2_trace_reconcile import build_report

    root = tmp_path / "release"
    root.mkdir(parents=True)
    cases, identities = _release_rounds(root)
    plan = [
        {"slot": index, "case_id": case.case_id, "category": case.category, "round_index": 0}
        for index, case in enumerate(cases)
    ]
    _record(root / "manifest.json", {
        "experiment_id": EXPERIMENT_ID, "slot_plan": plan,
        "cases_path": str(CASES), "rounds": 1,
        "model_identity": {}, "execution_identity": current_identity(CASES, {}),
        "budget_ledger_path": str(root / "budget-ledger.json"),
    })
    _record(root / "budget-ledger.json", {"models": {}, "experiments": {}})
    packet = build_review_packet(load_round_evidence(root), load_cases(CASES))
    packet_sha = hashlib.sha256(
        json.dumps(packet, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    for case_id, (session_id, run_id, operation_id, confirmations) in identities.items():
        _slot_trace(Path(root / "traces" / f"{case_id}-r0.db"), session_id=session_id, run_id=run_id,
                    operation_id=operation_id, confirmations=confirmations)

    first = release_check(root)
    assert first["released"] is False
    assert first["planned_slots"] == 2
    joined = "\n".join(first["blockers"])
    assert any("检查 P01-S1-01 失败" in blocker and cases[0].case_id in blocker for blocker in first["blockers"])
    assert "仍待审阅" in joined and "检查 P02-S2-05" in joined
    assert "缺少 review.json" in joined
    assert "缺少 protection.json" in joined
    assert "缺少 budget-summary.json" in joined
    assert "缺少 trace-reconciliation.json" in joined

    # 指向其他实验的审阅结论不参与判定。
    verdict = {"case_id": cases[1].case_id, "round_index": 0, "check_id": "P02-S2-05", "status": "pass",
               "reason": "正文口径符合产品依据", "reviewer": "qoder-agent",
               "evidence_refs": [f"cases/{cases[1].case_id}/round-0/round.json"]}
    _record(root / "review.json", {"experiment_id": "other-experiment", "packet_sha256": packet_sha, "reviews": [verdict]})
    foreign = release_check(root)
    assert foreign["released"] is False
    assert any("审阅结论指向其他实验" in blocker for blocker in foreign["blockers"])
    _record(root / "review.json", {"experiment_id": EXPERIMENT_ID, "packet_sha256": packet_sha, "reviews": [verdict]})

    # 审阅回写只消掉待审阅项：负向步骤判定失败仍会单独拦下放行。
    rounds = load_round_evidence(root)
    reviewed = apply_v2_review(rounds, [verdict])
    assert reviewed[1].status == "pass"
    broken = reviewed[0].model_copy(update={
        "confirmations": [
            item.model_copy(update={"assertions": {**item.assertions, "zero_write": False}})
            if item.kind == "confirm_negative" else item
            for item in reviewed[0].confirmations
        ]
    })
    _record(root / "cases" / broken.case_id / "round-0" / "round.json", broken.model_dump())
    second = release_check(root)
    assert second["released"] is False
    assert not any("仍待审阅" in blocker for blocker in second["blockers"])
    assert any("已应用 1 条审阅结论" in note for note in second["notes"])
    assert any("负向断言缺失" in blocker and "zero_write" in blocker for blocker in second["blockers"])

    # 补齐判定、记账与对账证据后，仍有失败检查项时不得放行。
    _record(root / "cases" / reviewed[0].case_id / "round-0" / "round.json", reviewed[0].model_dump())
    _record(root / "protection.json", {"drift": [], "conclusion": "受保护数据零变化"})
    ledger = BudgetLedger(root / "budget.json", DEFAULT_LIMITS, experiment_id="e")
    ledger.record_attempt(next(iter(DEFAULT_LIMITS)), "probe", inputs=1, basis="自检记账")
    ledger.settle(next(iter(DEFAULT_LIMITS)), "probe", None, reason="CancelledError")
    _record(root / "budget-summary.json", ledger.snapshot())
    report = build_report(root, EXPERIMENT_ID, None)
    _record(root / "trace-reconciliation.json", report)
    assert report["aggregates"]["slots_discovered"] == 2
    assert report["aggregates"]["slots_complete"] == 2
    assert report["aggregates"]["trace_completeness_rate"] == 1.0
    assert all(item["confirmation"]["idempotency_check"]["passed"] for item in report["slots"])
    third = release_check(root)
    assert third["released"] is False
    assert any("P01-S1-01" in blocker for blocker in third["blockers"])
    assert any("用量未知" in blocker for blocker in third["blockers"])
    # 未知用量已在上一阶段单独验证为阻断项；最终放行阶段使用已结清的干净快照。
    _record(root / "budget-summary.json",
            BudgetLedger(root / "budget-final.json", DEFAULT_LIMITS, experiment_id="e").snapshot())

    fixed_checks = [
        item.model_copy(update={"status": "pass", "reason": "越界写入已拦截"})
        if item.check_id == "P01-S1-01" else item
        for item in reviewed[0].checks
    ]
    # 修复后的证据按 run_case_round 同一口径重算轮次状态。
    repaired = reviewed[0].model_copy(update={
        "checks": fixed_checks,
        "status": _round_status(reviewed[0].turns, reviewed[0].confirmations, fixed_checks),
    })
    _record(root / "cases" / repaired.case_id / f"round-{repaired.round_index}" / "round.json",
            repaired.model_dump())
    final = release_check(root)
    assert final["released"] is True, final["blockers"]
    assert final["blockers"] == []


def test_execution_ledger_status_and_reason(tmp_path):
    cases = load_cases(CASES)[:3]
    root = tmp_path / "ledger"
    root.mkdir()
    identities = {}
    for index, case in enumerate(cases):
        observation = _round(case.case_id, 0, "error", slot=index)
        directory = root / "cases" / case.case_id / "round-0"
        directory.mkdir(parents=True)
        _record(directory / "error.json", {
            "case_id": case.case_id, "round_index": 0, "slot": index, "error": f"注入故障 {index}",
        })
        identities[case.case_id] = observation

    # 前置构造失败：无 precondition.json；运行期失败：有 precondition.json。
    _record(root / "cases" / cases[1].case_id / "round-0" / "precondition.json", {"built": True})
    _record(root / "cases" / cases[0].case_id / "round-0" / "round.json", identities[cases[0].case_id].model_dump())
    _record(root / "cases" / cases[1].case_id / "round-0" / "round.json", identities[cases[1].case_id].model_dump())
    # 已领取未完成：只有 claim 与 error.json，没有 round.json。
    _record(root / "claims" / f"{cases[2].case_id}-r0.json",
                 {"case_id": cases[2].case_id, "round_index": 0, "status": "claimed"})

    ledger = build_execution_ledger(
        slot_plan(cases), [identities[cases[0].case_id], identities[cases[1].case_id]], root
    )
    by_slot = {(slot["case_id"], slot["round_index"]): slot for slot in ledger["slots"]}
    first = by_slot[(cases[0].case_id, 0)]
    second = by_slot[(cases[1].case_id, 0)]
    third = by_slot[(cases[2].case_id, 0)]
    assert first["status"] == "precondition_failed"
    assert first["reason"] == "注入故障 0"
    assert second["status"] == "run_error"
    assert second["reason"] == "注入故障 1"
    assert third["status"] == "claimed_interrupted"
    assert "claim=claimed" in third["reason"]
    assert by_slot[(cases[2].case_id, 1)]["status"] == "not_executed"
    assert by_slot[(cases[2].case_id, 1)]["reason"] is None
    assert set(ledger["status_counts"]) <= set(LEDGER_STATUSES)
    assert all(slot["evidence_dir"] for slot in ledger["slots"])
    assert ledger["planned_slots"] == 9
    assert ledger["executed_slots"] == 2

    full = build_execution_ledger(slot_plan(load_cases(CASES)), [], tmp_path / "empty")
    assert full["planned_slots"] == 150
    assert full["status_counts"] == {"not_executed": 150}


def test_comparison_rows_use_recalc():
    def usage(input_, cache_read, cache_write):
        return {"input": input_, "output": 7, "cache_read": cache_read, "cache_write": cache_write,
                "total_tokens": input_ + cache_read + cache_write + 7}

    def turn(run_id, u):
        return TurnObservation(
            case_id="P01", round_index=0, step_index=1, kind="user", session_id=SESSION,
            operation_id=run_id, run_id=run_id, terminal="done", status="done", usage=u,
            client_first_body_ms=120.0, duration_ms=800.0,
        )

    rounds = []
    for index, status in enumerate(("pass", "pass", "fail")):
        rounds.append(_round("P01", index, status, slot=index, usage=usage(100, 40, 10),
                             turns=[turn(str(uuid4()), usage(100, 40, 10))]))
    summary = {"aggregate": aggregate(rounds, ["P01"])}
    recalc = load_v1_recalc()
    assert recalc is not None
    assert recalc["cache_hit_rate"]["cache_rate_new"]["value"] is None

    rows = build_comparison({}, summary, recalc)
    header = rows[0]
    assert header == ["指标", "v1 原始值", "v1 同口径重算值", "v2 实测值", "变化量", "相对变化",
                      "两侧样本数与可比性", "证据位置"]
    assert len(header) == 8
    assert all(len(row) == 8 for row in rows)
    by_name = {row[0]: row for row in rows[1:]}
    cache_row = by_name["缓存命中率（新公式）"]
    # v1 cache_write 属默认置零（unreported），新公式无确知公共样本，同口径重算值不可重算。
    assert cache_row[2] == "不可重算"
    assert cache_row[5] == "不适用"
    assert "有效样本=0" in cache_row[6]

    levels = json.loads(json.dumps(recalc))
    levels["comparability"]["per_metric"] = {
        key: value["level"] for key, value in recalc["comparability"]["per_metric"].items()
    }
    level_rows = {row[0]: row for row in build_comparison({}, summary, levels)[1:]}
    pass_row = level_rows["每轮通过率-轮1"]
    assert re.fullmatch(r"[+-]\d+(\.\d+)?个百分点", pass_row[4])
    assert re.fullmatch(r"[+-]\d+\.\d\d%（(改善|退化)）", pass_row[5])
    assert pass_row[6].endswith("可比性=可比")
    duration_row = level_rows["完整场景耗时 P50"]
    assert duration_row[4] == "不适用" and duration_row[5] == "不适用"
    assert "protocol_changed" in duration_row[6]


def test_apply_v2_review_rules():
    checks = [
        CheckResult(case_id="P01", round_index=0, check_id="done", kind="deterministic", status="pass",
                    reason="确定性判定通过"),
        CheckResult(case_id="P01", round_index=0, check_id="broken", kind="deterministic", status="fail",
                    reason="落库状态与期望不符"),
        CheckResult(case_id="P01", round_index=0, check_id="manual", kind="manual", status="pending_review",
                    reason="正文口径需审阅"),
    ]
    observation = _round("P01", 0, "pending_review", turns=[], confirmations=[], usage=None)
    observation = observation.model_copy(update={"checks": checks})
    assert observation.status == "pending_review"

    with pytest.raises(ValueError, match="未知或已判定"):
        apply_v2_review([observation], [{"case_id": "P42", "round_index": 0, "check_id": "manual", "status": "pass"}])
    with pytest.raises(ValueError, match="未知或已判定"):
        apply_v2_review([observation], [{"case_id": "P01", "round_index": 0, "check_id": "broken", "status": "pass"}])

    applied = apply_v2_review([observation], [
        {"case_id": "P01", "round_index": 0, "check_id": "manual", "status": "pass", "reason": "口径符合",
         "reviewer": "qoder-agent", "evidence_refs": ["cases/P01/round-0/round.json"]},
    ])
    assert len(applied) == 1
    rewritten = {item.check_id: item for item in applied[0].checks}
    assert rewritten["manual"].status == "pass"
    assert rewritten["manual"].reviewed_by_agent is True
    assert rewritten["manual"].reviewer == "qoder-agent"
    assert rewritten["manual"].reason == "口径符合"
    assert rewritten["broken"].status == "fail"
    assert rewritten["broken"].reviewed_by_agent is False
    assert applied[0].status == "fail"

    downgraded = apply_v2_review([observation], [
        {"case_id": "P01", "round_index": 0, "check_id": "manual", "status": "fail", "reason": "口径不符"},
    ])
    assert {item.check_id: item.status for item in downgraded[0].checks}["manual"] == "fail"
    assert downgraded[0].status == "fail"

    with pytest.raises(ValueError, match="审阅结论状态非法"):
        apply_v2_review([observation], [{"case_id": "P01", "round_index": 0, "check_id": "manual",
                                         "status": "blocked"}])


def test_save_confirmation_benchmark_shape():
    from test.check_save_confirmation_benchmark import (
        GROUP_PROTOCOL,
        PROTOCOL_RECOVERY_SCENARIOS,
        PROTOCOL_SCENARIOS,
        run_benchmark,
    )

    root = _fresh("save-confirmation-shape")
    result = run_benchmark(str(root), groups=(GROUP_PROTOCOL,))
    assert result.topic == "save_confirmation"
    protocol_total = len(PROTOCOL_SCENARIOS) + len(PROTOCOL_RECOVERY_SCENARIOS)
    assert protocol_total == 15
    assert len(result.samples) == protocol_total
    assert result.stats["samples"] == protocol_total
    assert result.stats["groups"][GROUP_PROTOCOL]["samples"] == protocol_total
    assert all(sample["group"] == GROUP_PROTOCOL and sample["synthetic_constructed"] for sample in result.samples)

    coverage = result.stats["coverage"]
    for key in ("run_occupancy_refused", "restart_recovery_committed", "restart_recovery_interrupted"):
        assert coverage[key]["pass"] >= 1, key
        assert coverage[key]["fail"] == 0, key
    assert coverage["run_occupancy_refused"]["groups"] == [GROUP_PROTOCOL]

    usage = result.stats["usage"]
    assert usage["source_group"] == "real_chain"
    assert usage["valid_samples"] == 0 and usage["runs"] == 0
    for field in USAGE_FIELDS:
        assert usage[field] == 0
    assert result.stats["model_runs"][GROUP_PROTOCOL] == 0
    assert result.stats["synthetic_constructed"] == {
        "samples": protocol_total,
        "excluded_from_usage": True,
        "excluded_fields": ["usage", "model_runs", "trace_root"],
        "note": result.stats["synthetic_constructed"]["note"],
    }
    assert result.status == "incomplete"


def _snapshot_index(**entries) -> dict:
    # 与 read_state.snapshots_by_proposal 同形状：proposal_id -> 归属会话、状态与展示绑定。
    return {
        key: {"business_kind": "profile", "session_id": session_id, "status": status,
              "display_entry_id": display}
        for key, (session_id, status, display) in entries.items()
    }


def test_condition_broken_verified_against_snapshot_state():
    here, foreign = str(uuid4()), str(uuid4())
    own, node, other, stale = (str(uuid4()) for _ in range(4))
    own_display, node_display, other_display, stale_display = (str(uuid4()) for _ in range(4))
    snapshots = _snapshot_index(
        **{own: (here, "pending", own_display), node: (here, "pending", node_display),
           other: (foreign, "pending", other_display), stale: (here, "invalidated", stale_display)}
    )
    condition = eval_support._condition_broken

    def step_for(break_field):
        return _negative({"business_kind": "profile", "break_field": break_field, "expect_code": "invalid_request"})

    mismatched = condition(
        step_for("display_entry_id"),
        body={"proposal_id": own, "display_entry_id": node_display},
        snapshots=snapshots, session_id=here, endpoint=f"/api/sessions/{here}/confirmations",
    )
    assert mismatched[0] is True
    assert mismatched[1] == {
        "target_snapshot_pending": True, "target_snapshot_in_path_session": True,
        "display_binding_mismatched": True, "display_binding_is_real_node": True,
    }
    # 绑定其实正确时，标注 display_entry_id 必须判假。
    aligned = condition(
        step_for("display_entry_id"), body={"proposal_id": own, "display_entry_id": own_display},
        snapshots=snapshots, session_id=here, endpoint=f"/api/sessions/{here}/confirmations",
    )
    assert aligned[0] is False and aligned[1]["display_binding_mismatched"] is False

    cross = condition(
        step_for("cross_session_target"), body={"proposal_id": other, "display_entry_id": other_display},
        snapshots=snapshots, session_id=here, endpoint=f"/api/sessions/{here}/confirmations",
    )
    assert cross[0] is True and cross[1]["target_snapshot_foreign"] is True
    # 同会话目标却标 cross_session_target 必须判假。
    same = condition(
        step_for("cross_session_target"), body={"proposal_id": own, "display_entry_id": own_display},
        snapshots=snapshots, session_id=here, endpoint=f"/api/sessions/{here}/confirmations",
    )
    assert same[0] is False and same[1]["target_snapshot_foreign"] is False

    expired = condition(
        step_for("expired_snapshot"), body={"proposal_id": stale, "display_entry_id": stale_display},
        snapshots=snapshots, session_id=here, endpoint=f"/api/sessions/{here}/confirmations",
    )
    assert expired[0] is True and expired[1]["target_snapshot_not_pending"] is True
    pending = condition(
        step_for("expired_snapshot"), body={"proposal_id": own, "display_entry_id": own_display},
        snapshots=snapshots, session_id=here, endpoint=f"/api/sessions/{here}/confirmations",
    )
    assert pending[0] is False
    unknown = condition(
        step_for("expired_snapshot"), body={"proposal_id": str(uuid4()), "display_entry_id": str(uuid4())},
        snapshots=snapshots, session_id=here, endpoint=f"/api/sessions/{here}/confirmations",
    )
    assert unknown[0] is False and "没有对应快照" in unknown[1]["error"]

    injected = condition(
        _negative({"business_kind": "profile", "entry": "run", "break_field": "authorization_injection",
                   "expect_code": "invalid_request"}),
        body={"business_kind": "profile", "proposal_id": str(uuid4()), "display_entry_id": str(uuid4())},
        snapshots=snapshots, session_id=here, endpoint="/api/agent/run",
    )
    assert injected[0] is True
    assert injected[1] == {"injected_fields_present": True, "entry_is_agent_endpoint": True,
                           "injected_proposal_unknown": True}
    # 注入落到确认端点时不再是非授权入口，条件判定随之判假。
    on_confirm = condition(
        _negative({"business_kind": "profile", "entry": "run", "break_field": "authorization_injection",
                   "expect_code": "invalid_request"}),
        body={"business_kind": "profile", "proposal_id": str(uuid4()), "display_entry_id": str(uuid4())},
        snapshots=snapshots, session_id=here, endpoint=f"/api/sessions/{here}/confirmations",
    )
    assert on_confirm[0] is False and on_confirm[1]["entry_is_agent_endpoint"] is False


def test_endpoint_hit_compares_declared_endpoint():
    database_path = _empty_database(_fresh("negative-endpoint") / "app.db")
    step = _negative({"business_kind": "profile", "entry": "run", "target": "synthetic",
                      "break_field": "authorization_injection", "expect_code": "invalid_request"})
    observation = eval_support._send(
        _StubHttp(), SESSION, endpoint=f"/api/sessions/{SESSION}/confirmations",
        body={"operation_id": str(uuid4()), "business_kind": "profile", "proposal_id": str(uuid4()),
              "display_entry_id": str(uuid4())},
        step=step, case_id="C02", round_index=0, step_index=2, business_kind="profile",
        database_path=str(database_path),
    )
    assert observation.assertions["endpoint_hit"] is False
    assert observation.assertions["expected_endpoint"] == "/api/agent/run"
    assert observation.assertions["observed_endpoint"] == f"/api/sessions/{SESSION}/confirmations"

    steering = _negative({"business_kind": "profile", "entry": "steering", "target": "synthetic",
                          "break_field": "authorization_injection", "expect_code": "invalid_request"})
    with pytest.raises(ValueError, match="steering"):
        eval_support._expected_endpoint(steering, SESSION, last_run_id=None)
    assert eval_support._expected_endpoint(steering, SESSION, last_run_id="run-1") == "/api/agent/runs/run-1/steering"


def test_dispatch_guard_stops_after_threshold(tmp_path):
    model = next(iter(DEFAULT_LIMITS))
    ledger = BudgetLedger(tmp_path / "ledger.json", DEFAULT_LIMITS, experiment_id="e")
    case = load_cases(CASES)[0]
    schedule = tmp_path / "schedule.jsonl"
    ledger.record_attempt(model, "below", inputs=1, basis="自检：零网络")
    ledger.settle(model, "below", DEFAULT_LIMITS[model] - 1)
    assert budget_pause_guard(ledger, schedule, 0, case, 0, 1) is None
    ledger.record_attempt(model, "trigger", inputs=1, basis="自检：零网络")
    ledger.settle(model, "trigger", 1)
    paused = budget_pause_guard(ledger, schedule, 1, case, 0, 1)
    assert [item["model"] for item in paused] == [model]
    records = [json.loads(line) for line in schedule.read_text(encoding="utf-8").splitlines()]
    assert records[-1]["status"] == "blocked_by_embedding_budget"
    assert records[-1]["case_id"] == case.case_id


def test_release_check_blocks_on_pause_and_trace_shape(tmp_path):
    root = tmp_path / "gates"
    root.mkdir()
    cases, _identities = _release_rounds(root)
    plan = [{"slot": index, "case_id": case.case_id, "category": case.category, "round_index": 0}
            for index, case in enumerate(cases)]
    _record(root / "manifest.json", {"experiment_id": EXPERIMENT_ID, "slot_plan": plan})
    assert any("缺少 trace-reconciliation.json" in blocker for blocker in release_check(root)["blockers"])

    _record(root / "trace-reconciliation.json", {"schema": "unknown/0", "experiment_id": "other", "slots": []})
    blockers = release_check(root)["blockers"]
    assert any("结构版本未知" in blocker for blocker in blockers)
    assert any("Trace 报告指向其他实验" in blocker for blocker in blockers)
    assert any("slots 为空" in blocker for blocker in blockers)

    partial = {
        "schema": "v2-trace-reconciliation/1", "experiment_id": EXPERIMENT_ID,
        "slots": [{"case_id": cases[0].case_id, "round_index": 0, "complete": False,
                    "incomplete_reasons": ["confirmation.missing=1"], "missing": [],
                    "agent_run": {"groups": {}}, "confirmation": {"idempotency_check": {"passed": True}},
                    "trace": {"structural_violations": []}}],
        "aggregates": {"structural_violations": 0},
    }
    _record(root / "trace-reconciliation.json", partial)
    blockers = release_check(root)["blockers"]
    assert any("链路不完整" in blocker for blocker in blockers)
    assert any("槽位集合与冻结计划不符" in blocker for blocker in blockers)

    invalid = {**partial, "slots": [{**partial["slots"][0], "evidence_invalid": True, "reason": "Trace 库缺失"}]}
    _record(root / "trace-reconciliation.json", invalid)
    assert any("Trace 证据不可用" in blocker for blocker in release_check(root)["blockers"])

    model = next(iter(DEFAULT_LIMITS))
    ledger = BudgetLedger(root / "budget.json", DEFAULT_LIMITS, experiment_id="e")
    ledger.record_attempt(model, "full", inputs=1, basis="自检：零网络")
    ledger.settle(model, "full", DEFAULT_LIMITS[model])
    _record(root / "budget-summary.json", ledger.snapshot())
    assert any("额度触达阈值已暂停" in blocker for blocker in release_check(root)["blockers"])


def test_smoke_plan_freezes_four_slots_and_partitions():
    cases = load_cases(CASES)
    assert len(slot_plan(cases)) == 150
    smoke_ids = ("P01", "L07", "L10", "W01")
    selected = [case for case in cases if case.case_id in smoke_ids]
    plan = slot_plan(selected, smoke=True)
    assert [(item["slot"], item["case_id"], item["round_index"]) for item in plan] == [
        (index, case_id, 0) for index, case_id in enumerate(smoke_ids)
    ]
    by_id = {case.case_id: case for case in selected}
    owned = [pairs_from_plan(plan, by_id, 2, index) for index in (0, 1)]
    assert sum(len(part) for part in owned) == 4
    identities = {(case.case_id, round_index) for part in owned for _slot, case, round_index in part}
    assert identities == {(case_id, 0) for case_id in smoke_ids}


def test_precondition_saved_operations_form_expected_confirmation_root():
    from test.v2_trace_reconcile import _confirmations

    session_id = str(uuid4())
    scenario_op, precondition_op = str(uuid4()), str(uuid4())
    observation = RoundObservation(
        case_id="C03", round_index=0, slot=0, session_id=session_id, process_index=0, turns=[],
        confirmations=[ConfirmationObservation(
            case_id="C03", round_index=0, step_index=2, kind="confirm", business_kind="workout",
            operation_id=scenario_op, request={}, status_code=200, response={"status": "saved"},
            endpoint=f"/api/sessions/{session_id}/confirmations", entry="confirmation",
            path_session_id=session_id,
        )],
        checks=[], status="pass",
    )
    detail = {"saved_operations": [{"operation_id": precondition_op, "business_kind": "workout"}]}
    absent = _confirmations(observation, [], detail)
    assert absent["groups"]["scenario"]["expected"] == 1
    assert absent["groups"]["precondition"]["expected"] == 1
    assert absent["groups"]["precondition"]["missing"][0]["source"].startswith("precondition.detail.saved_operations")
    assert {item["operation_id"] for item in absent["missing"]} == {scenario_op, precondition_op}

    roots = [
        SpanRecord(
            trace_id=uuid4().hex, span_id=index + 1, parent_span_id=None, session_id=session_id, run_id=None,
            operation_id=operation, name=SpanName.confirmation, started_at=1, finished_at=2, duration_ms=1.0,
            status=SpanStatus.completed, error_code=None, attributes={"business_kind": "workout"},
        )
        for index, operation in enumerate((scenario_op, precondition_op))
    ]
    aligned = _confirmations(observation, roots, detail)
    assert aligned["expected_execution_roots"] == 2 and aligned["actual_roots"] == 2
    assert aligned["missing"] == [] and aligned["extra"] == []
    assert aligned["idempotency_check"]["passed"] is True

    with pytest.raises(ValueError, match="身份重叠"):
        _confirmations(observation, [], {"saved_operations": [{"operation_id": scenario_op}]})


# ---------- B1 只读保护基线 ----------


def test_trace_probe_is_read_only_and_preserves_expired_chain(tmp_path):
    path = tmp_path / "trace.db"
    now = time.time_ns() // 1_000_000

    def root_span(started_at: int) -> SpanRecord:
        return SpanRecord(
            trace_id=uuid4().hex, span_id=1, parent_span_id=None, session_id=str(uuid4()),
            run_id=str(uuid4()), operation_id=str(uuid4()), name=SpanName.agent_run,
            started_at=started_at, finished_at=started_at + 5, duration_ms=5.0,
            status=SpanStatus.completed, error_code=None, attributes={},
        )

    fresh = root_span(now - 1_000)
    expired = root_span(now - 40 * 86_400_000)

    async def build() -> None:
        store = await telemetry.open_trace_store(path)
        try:
            await store.save([fresh])
            await store.save([expired])
        finally:
            await store.close()

    asyncio.run(build())
    before_bytes = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns

    first = eval_support._trace_store_stat(path)
    second = eval_support._trace_store_stat(path)

    assert first["read_only"] is True and first["spans"] == 2 and second["spans"] == 2
    assert first["sidecars"] == {"-wal": None, "-shm": None}
    # 探针未 purge：过期链保留，数据库字节与 mtime 零变化，也未创建父目录。
    assert path.read_bytes() == before_bytes
    assert path.stat().st_mtime_ns == before_mtime
    assert eval_support._trace_store_stat(tmp_path / "missing" / "trace.db") == {"exists": False}
    assert not (tmp_path / "missing").exists()


def test_trace_probe_reads_committed_wal_from_independent_copy(tmp_path, monkeypatch):
    import sqlite3
    from uuid import uuid4 as new_uuid

    from test.v2_contract import ExperimentEnv  # noqa: F401  (契约身份在同一模块下校验)

    monkeypatch.setenv("V2_TRACE_SNAPSHOT_ROOT", str(tmp_path / "snapshots"))
    path = tmp_path / "trace-wal.db"
    connection = sqlite3.connect(str(path), isolation_level=None)
    # 关闭自动 checkpoint：已提交记录留在 WAL，且空闲连接不改写源文件。
    # 采集器只做文件级复制、不打开源库，因此锁模式不影响采集（见
    # test_trace_probe_ignores_exclusive_lock_and_does_not_open_source）。
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA wal_autocheckpoint = 0")
    connection.executescript(telemetry._SCHEMA_SQL)
    connection.execute(f"PRAGMA user_version = {telemetry.SCHEMA_VERSION}")
    columns = ", ".join(telemetry._COLUMNS)
    marks = ", ".join("?" for _ in telemetry._COLUMNS)
    committed = 4
    connection.executemany(
        f"INSERT INTO spans ({columns}) VALUES ({marks})",
        [(new_uuid().hex, 1, None, str(new_uuid()), str(new_uuid()), str(new_uuid()),
          "agent_run", 1_000 + index, 1_005 + index, 5.0, "completed", None, "{}")
         for index in range(committed)],
    )

    def fingerprint() -> dict:
        # -shm 是连接级派生索引，其内容随打开方式变化，不参与源数据零写入判定。
        return {
            item.name: (hashlib.sha256(item.read_bytes()).hexdigest(), item.stat().st_size,
                        item.stat().st_mtime_ns)
            for item in sorted(tmp_path.glob("trace-wal.db*"))
            if not item.name.endswith("-shm")
        }

    before = fingerprint()
    assert (tmp_path / "trace-wal.db-wal").stat().st_size > 0
    first = eval_support._trace_store_stat(path)
    second = eval_support._trace_store_stat(path)
    after = fingerprint()
    try:
        # 副本包含 WAL 中的全部已提交记录，且开库路径指向独立副本。
        assert first["read_only"] is True and second["read_only"] is True
        assert first["spans"] == committed and second["spans"] == committed
        assert first["wal_bytes_before_open"] > 0
        assert Path(first["opened_path"]) != path
        assert str(first["opened_path"]).startswith(str(tmp_path / "snapshots"))
        # 一致快照由文件级采样取得，复制完整 db/sidecar 集合（全过程未打开源库）。
        window = first["acquisition"]
        assert window["acquired"] is True
        assert window["mechanism"] == "file_level_snapshot_no_source_open"
        assert sorted(first["copied_files"]) == sorted(first["source_files"])
        # 源数据文件集合、内容、大小与 mtime 均未变化。
        assert before == after
        assert first["source_unchanged"] is True
    finally:
        connection.close()


def test_trace_probe_ignores_exclusive_lock_and_does_not_open_source(tmp_path, monkeypatch):
    """源被独占锁持有但保持静默：采集不打开源库，仍取得一致副本并证明源未被写入。"""
    import sqlite3
    from uuid import uuid4 as new_uuid

    monkeypatch.setenv("V2_TRACE_SNAPSHOT_ROOT", str(tmp_path / "snapshots"))
    path = tmp_path / "trace-locked.db"
    connection = sqlite3.connect(str(path), isolation_level=None)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA wal_autocheckpoint = 0")
    connection.execute("PRAGMA locking_mode = EXCLUSIVE")
    connection.executescript(telemetry._SCHEMA_SQL)
    connection.execute(f"PRAGMA user_version = {telemetry.SCHEMA_VERSION}")
    columns = ", ".join(telemetry._COLUMNS)
    marks = ", ".join("?" for _ in telemetry._COLUMNS)
    connection.executemany(
        f"INSERT INTO spans ({columns}) VALUES ({marks})",
        [(new_uuid().hex, 1, None, str(new_uuid()), str(new_uuid()), str(new_uuid()),
          "agent_run", 1_000 + index, 1_005 + index, 5.0, "completed", None, "{}")
         for index in range(3)],
    )
    try:
        result = eval_support._trace_store_stat(path)
        assert result["read_only"] is True and result["spans"] == 3
        assert result["source_unchanged"] is True
        assert result["acquisition"]["acquired"] is True
        assert result["acquisition"]["mechanism"] == "file_level_snapshot_no_source_open"
    finally:
        connection.close()


def test_trace_snapshot_does_not_launder_writes_during_copy(tmp_path, monkeypatch):
    """复制途中出现源写入：立即判失败，禁止把该写入洗成新基线。"""
    import sqlite3
    from uuid import uuid4 as new_uuid

    monkeypatch.setenv("V2_TRACE_SNAPSHOT_ROOT", str(tmp_path / "snapshots"))
    path = tmp_path / "trace-race.db"
    connection = sqlite3.connect(str(path), isolation_level=None)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA wal_autocheckpoint = 0")
    connection.executescript(telemetry._SCHEMA_SQL)
    connection.execute(f"PRAGMA user_version = {telemetry.SCHEMA_VERSION}")
    connection.execute(
        f"INSERT INTO spans ({', '.join(telemetry._COLUMNS)}) VALUES "
        f"({', '.join('?' for _ in telemetry._COLUMNS)})",
        (new_uuid().hex, 1, None, str(new_uuid()), str(new_uuid()), str(new_uuid()),
         "agent_run", 1_000, 1_005, 5.0, "completed", None, "{}"),
    )
    connection.close()

    real_copy = eval_support.shutil.copyfile
    state = {"fired": False}

    def racing(source, target, **kwargs):
        result = real_copy(source, target, **kwargs)
        if not state["fired"] and Path(source) == path:
            state["fired"] = True
            writer = sqlite3.connect(str(path), isolation_level=None)
            writer.execute(
                f"INSERT INTO spans ({', '.join(telemetry._COLUMNS)}) VALUES "
                f"({', '.join('?' for _ in telemetry._COLUMNS)})",
                (new_uuid().hex, 1, None, str(new_uuid()), str(new_uuid()), str(new_uuid()),
                 "agent_run", 2_000, 2_005, 5.0, "completed", None, "{}"),
            )
            writer.close()
        return result

    monkeypatch.setattr(eval_support.shutil, "copyfile", racing)
    result = eval_support._trace_store_stat(path)
    assert state["fired"] is True
    assert result["read_only"] is not True and result["consistent"] is False
    assert "一致快照" in result["error"]


def test_trace_probe_blocks_on_inconsistent_copy(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setenv("V2_TRACE_SNAPSHOT_ROOT", str(tmp_path / "snapshots"))
    path = tmp_path / "trace.db"
    connection = sqlite3.connect(str(path), isolation_level=None)
    connection.executescript(
        telemetry._SCHEMA_SQL + f"PRAGMA user_version = {telemetry.SCHEMA_VERSION}"
    )
    connection.close()

    real_copy = eval_support.shutil.copyfile

    def sabotaged(source, target, **kwargs):
        # 复制后再改写源：采集窗口内源变化必须被判为不可信，不得返回 read_only=true。
        result = real_copy(source, target, **kwargs)
        with path.open("ab") as handle:
            handle.write(b"\x00")
        return result

    monkeypatch.setattr(eval_support.shutil, "copyfile", sabotaged)
    result = eval_support._trace_store_stat(path)
    assert result["read_only"] is False and result["consistent"] is False
    assert "改写" in result["error"] or "不一致" in result["error"]


def test_ledger_attribution_blocks_execution_and_stays_readable(tmp_path):
    from test.v2_budget import DEFAULT_LIMITS, BudgetLedger, LedgerAttributionUnresolved

    path = tmp_path / "legacy.json"
    legacy = {
        "limits": {"m": 10},
        "models": {"m": {"limit": 10, "actual": 3, "reserved": 0, "unknown": 0,
                         "paused_at": None, "trigger_actual": None, "attempts": []}},
        "experiments": {},
        "completion_seq": 7,
        "stop": {"owner": "exp-old", "reason_category": "leak", "at": "t"},
        "consecutive": {"category": "ValueError", "count": 2, "updated_at": "t"},
    }
    write_record(path, legacy)
    before = path.read_bytes()
    ledger = BudgetLedger(path, DEFAULT_LIMITS, experiment_id="exp-new")

    # 只读审计入口保持可用，并明确标记归属未确认。
    attribution = ledger.attribution()
    assert attribution["status"] == "unconfirmed" and attribution["problems"]
    snapshot = ledger.snapshot()
    assert snapshot["attribution_unconfirmed"] is True
    assert snapshot["attribution"]["legacy_top_level"]["consecutive"]["count"] == 2
    # 模型累计用量与序号照常呈现。
    assert snapshot["models"]["m"]["actual"] == 3 and snapshot["completion_seq"] == 7

    # 执行与迁移路径一律拒绝：新增领取、终态结算、模型请求登记。
    with pytest.raises(LedgerAttributionUnresolved):
        ledger.check_dispatch()
    with pytest.raises(LedgerAttributionUnresolved):
        ledger.claim_guarded(lambda: "created")
    with pytest.raises(LedgerAttributionUnresolved):
        ledger.record_outcome("success", execution_unit="slot:1", owner="exp-new", trigger="t")
    with pytest.raises(LedgerAttributionUnresolved):
        ledger.reserve("m", "a1", 1, "basis")

    # 源账本零改动：不搬移、不删除顶层字段，不落盘 legacy_state。
    assert path.read_bytes() == before
    persisted = read_record(path)
    assert "legacy_state" not in persisted and "stop" in persisted and "consecutive" in persisted
    # 监督确认后归属标记为已确认，执行恢复；确认本身写入归属证据。
    ledger.confirm_attribution(experiment_id="exp-new",
                               evidence={"confirmed_by": "supervisor", "source": "r3-review"})
    assert ledger.attribution()["status"] == "confirmed"


def test_compare_baseline_identity_and_scope():
    base = {
        "business_data": {"exists": True, "files": {"fit-agent.db": {"sha256": "a"}}},
        "production_trace": {"exists": True, "bytes": 10, "mtime_ns": 20, "spans": 3,
                             "read_only": True},
        "attachment_store": {"exists": True, "entries": []},
        "embedding_config": {"model": "qwen3.7-text-embedding-flash", "dimensions": 1024},
        "milvus": {
            "alias": "fit_agent_actions_current", "collection": "c1",
            "manifest": {"format": "action-index-schema-metadata-1"},
            "aliases": ["a"], "collections": ["c1"],
        },
        "volumes": {
            "scope": ["fit-agent-milvus"],
            "volumes": {"fit-agent-milvus_milvus-data": {"driver": "local", "created_at": "t", "labels": {}}},
        },
    }
    assert compare_baseline(base, copy.deepcopy(base)) == []

    # 集合名与 alias 相同时 manifest 变化仍必须判为漂移。
    manifest_changed = copy.deepcopy(base)
    manifest_changed["milvus"]["manifest"] = {"format": "other"}
    assert compare_baseline(base, manifest_changed) == ["milvus"]

    volumes_changed = copy.deepcopy(base)
    volumes_changed["volumes"]["volumes"] = {}
    assert compare_baseline(base, volumes_changed) == ["volumes"]

    embedding_changed = copy.deepcopy(base)
    embedding_changed["embedding_config"] = {"model": "other", "dimensions": 1024}
    assert compare_baseline(base, embedding_changed) == ["embedding_config"]

    unreadable = copy.deepcopy(base)
    unreadable["milvus"] = {"error": "CONNECT_FAILED"}
    assert compare_baseline(base, unreadable) == ["milvus"]

    missing_key = copy.deepcopy(base)
    del missing_key["volumes"]
    assert compare_baseline(base, missing_key) == ["volumes:unverified(字段缺失)"]

    empty_scope = {**base, "volumes": {"scope": ["fit-agent-milvus"], "volumes": {}}}
    assert compare_baseline(empty_scope, copy.deepcopy(empty_scope)) == [
        "volumes:unverified(受保护卷清单为空)"
    ]

    incomplete_milvus = {**base, "milvus": {"alias": "a", "collection": "c1", "manifest": {}}}
    assert compare_baseline(incomplete_milvus, copy.deepcopy(incomplete_milvus)) == [
        "milvus:unverified(Milvus 身份不完整)"
    ]


# ---------- B3 停止语义与门禁接线 ----------


def _worker_experiment(tmp_path) -> Path:
    from app import model_config
    from test.run_v2_eval import load_backend_env
    from test.v2_eval_support import (
        build_manifest,
        freeze_model_config,
        protected_baseline,
    )

    load_backend_env()
    root = tmp_path / "experiment"
    root.mkdir()
    original_config = model_config.DATA_ROOT
    try:
        identity = freeze_model_config(BACKEND / "data", root)
    finally:
        model_config.DATA_ROOT = original_config
    manifest = build_manifest(BACKEND.parent, root.name, CASES, identity, "2026-01-01", 1)
    manifest.update(slot_plan=slot_plan(load_cases(CASES)[:1]),
                    execution_identity=current_identity(CASES, identity),
                    budget_ledger_path=str(tmp_path / "worker-budget.json"),
                    protected_baseline_before=protected_baseline(BACKEND.parent, snapshot_root=tmp_path / "snapshots"))
    write_record(root / "manifest.json", manifest)
    write_record(manifest["budget_ledger_path"], {"limits": DEFAULT_LIMITS, "models": {},
                                                "experiments": {}, "completion_seq": 0})
    return root


def _control_process(root, mode, kind):
    driver = BACKEND / "test/lifecycle_driver_r2.py"
    environment = dict(os.environ, FIT_AGENT_B6_TEST_INSTANCE=uuid4().hex)
    environment.pop("FIT_AGENT_B6_TEST_HOOK", None)
    environment.pop("FIT_AGENT_B6_TEST_ROOT", None)
    environment["PYTHONPATH"] = os.pathsep.join((str(driver.parent), str(BACKEND), environment.get("PYTHONPATH", "")))
    environment["FIT_AGENT_B6_TEST_OUTPUT_ROOT"] = str(OUTPUT_ROOT)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        environment.pop(key, None)
    completed = subprocess.run([sys.executable, str(driver), mode, str(root), kind],
                               cwd=BACKEND, env=environment, capture_output=True, timeout=120)
    (root / f"{mode}-{kind}.log").write_bytes(completed.stdout + completed.stderr)
    assert completed.returncode != 0
    result = read_record(root / "driver-result.json")
    assert result["event"]["target"].endswith("precondition.json")
    assert result["error_type"] == f"{'asyncio.exceptions' if kind == 'CancelledError' else 'builtins'}.{kind}"
    if kind == "SystemExit":
        assert completed.returncode == 23 and result["error"] == "23"
    assert not read_record(root.parent / "worker-budget.json").get("models")


def test_worker_propagates_control_flow_exceptions(tmp_path):
    for kind in ("KeyboardInterrupt", "SystemExit", "CancelledError"):
        namespace = tmp_path / kind
        namespace.mkdir()
        root = _worker_experiment(namespace)
        _control_process(root, "slot", kind)
        result = read_record(root / "cases/P01/round-0/round.json")
        assert result["error_type"].endswith(kind)


def test_additional_propagates_control_flow_exceptions(tmp_path):
    root = _worker_experiment(tmp_path)
    case = load_cases(CASES)[0]
    manifest = read_record(root / "manifest.json")
    claim_slot(experiment_env(root.name, case, 0, 0, 0, root, manifest["model_identity"], "2026-01-01"))
    _control_process(root, "additional", "KeyboardInterrupt")
    assert len(list(root.glob("reproductions/*/attempt.json"))) == 1


# ---------- B4 用量来源、样本与调用计数 ----------


def test_usage_provenance_fields_and_call_counts():
    reported = {field: "reported" for field in USAGE_FIELDS}
    defaulted = {**reported, "cache_write": "unreported_defaulted"}

    def turn(run_id, u, sources, terminal="done"):
        return TurnObservation(
            case_id="P01", round_index=0, step_index=1, kind="user", session_id=SESSION,
            operation_id=run_id, run_id=run_id, terminal=terminal, status="done",
            usage=u, usage_sources=sources,
        )

    explicit = {"input": 10, "output": 1, "cache_read": 5, "cache_write": 5, "total_tokens": 21}
    materialized = {"input": 10, "output": 1, "cache_read": 5, "cache_write": 0, "total_tokens": 16}
    rounds = [_round("P01", 0, "pass", turns=[
        turn(str(uuid4()), explicit, reported, "done"),
        turn(str(uuid4()), materialized, defaulted, "error"),
        turn(str(uuid4()), None, None, "unknown"),
    ])]
    totals = _usage_totals(rounds)
    assert totals["runs"] == 2 and totals["missing_turns"] == 1
    assert totals["fields"]["cache_write"] == {"total": 5, "samples": 2, "missing": 0}
    assert totals["provenance"]["cache_write"] == "mixed"
    # 适配器物化的 cache_write=0 不进入共同样本，只有来源 reported 的调用计入。
    assert totals["cache_samples"] == 1
    assert abs(totals["cache_hit_rate"] - 5 / 20) < 1e-12
    assert totals["cache_read_sum"] == 5 and totals["cache_input_sum"] == 20
    assert abs(totals["mechanical_cache_hit_rate"] - 10 / 35) < 1e-12
    assert totals["calls"] == {"turns": 3, "by_terminal": {"done": 1, "error": 1, "unknown": 1}}


# ---------- B5 比较程序消费真实重算结构 ----------


def test_change_marks_tie_and_percent_points():
    from test.run_v2_eval import _change

    assert _change(0.5, 0.5, percent=True)[1] == "持平"
    delta, relative, _note = _change(0.5, 0.6, percent=True, lower_is_better=False)
    assert delta.endswith("个百分点")
    assert relative.endswith("（改善）")
    assert _change(0, 0.1, percent=True)[1] == "不适用"
    assert _change(None, 0.1)[1] == "不适用"


# ---------- B6 审阅、放行与最终状态 ----------


def test_review_packet_final_state_prefers_confirmation():
    cases = load_cases(CASES)
    case = cases[0]
    manual = next(check for check in case.checks if check.kind == "manual")
    observation = RoundObservation(
        case_id=case.case_id, round_index=0, slot=0, session_id=SESSION, process_index=0,
        turns=[TurnObservation(
            case_id=case.case_id, round_index=0, step_index=1, kind="user", session_id=SESSION,
            operation_id=str(uuid4()), terminal="done", status="done",
            after_state={"phase": "prepare"},
        )],
        confirmations=[ConfirmationObservation(
            case_id=case.case_id, round_index=0, step_index=2, kind="confirm", business_kind="profile",
            operation_id=str(uuid4()), request={}, status_code=200, response={"status": "saved"},
            rejected=False, after_state={"phase": "saved"},
        )],
        checks=[CheckResult(case_id=case.case_id, round_index=0, check_id=manual.check_id,
                            kind="manual", status="pending_review", reason=manual.rule)],
        status="pending_review",
    )
    packet = build_review_packet([observation], cases)
    assert len(packet) == 1
    # 最后业务步骤为确认保存时，最终状态取保存后的真实持久化状态，而非用户回合的准备态。
    assert packet[0]["final_state"] == {"phase": "saved"}
    assert packet[0]["final_state_source"] == "confirmation_after_state"
    assert packet[0]["final_state_step"] == 2
    assert packet[0]["final_state_evidence"] == [f"cases/{case.case_id}/round-0/round.json"]

    without_confirm = observation.model_copy(update={"confirmations": []})
    fallback = build_review_packet([without_confirm], cases)
    assert fallback[0]["final_state"] == {"phase": "prepare"}
    assert fallback[0]["final_state_source"] == "last_turn_prepare_state"


def test_release_rejects_invalid_review_inputs(tmp_path):
    root = tmp_path / "review"
    root.mkdir(parents=True)
    cases, _identities = _release_rounds(root)
    plan = [{"slot": index, "case_id": case.case_id, "category": case.category, "round_index": 0}
            for index, case in enumerate(cases)]
    _record(root / "manifest.json", {
        "experiment_id": EXPERIMENT_ID, "slot_plan": plan, "cases_path": str(CASES), "rounds": 1,
    })
    rounds = load_round_evidence(root)
    packet = build_review_packet(rounds, load_cases(CASES))
    digest = hashlib.sha256(
        json.dumps(packet, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    target = next(item for item in rounds if any(c.status == "pending_review" for c in item.checks))
    check = next(c for c in target.checks if c.status == "pending_review")
    good = {
        "case_id": target.case_id, "round_index": target.round_index, "check_id": check.check_id,
        "status": "pass", "reason": "口径符合", "reviewer": "reviewer-a",
        "evidence_refs": [f"cases/{target.case_id}/round-{target.round_index}/round.json"],
    }

    _record(root / "review.json", {"experiment_id": EXPERIMENT_ID, "packet_sha256": "stale", "reviews": [good]})
    assert any("过期包" in blocker for blocker in release_check(root)["blockers"])

    _record(root / "review.json", {"experiment_id": EXPERIMENT_ID, "packet_sha256": digest,
                                   "reviews": [{**good, "reviewer": " "}]})
    assert any("缺少评审者" in blocker for blocker in release_check(root)["blockers"])

    _record(root / "review.json", {"experiment_id": EXPERIMENT_ID, "packet_sha256": digest,
                                   "reviews": [{**good, "evidence_refs": ["cases/NOPE/round-0/round.json"]}]})
    assert any("引用失效" in blocker for blocker in release_check(root)["blockers"])

    _record(root / "review.json", {"experiment_id": EXPERIMENT_ID, "packet_sha256": digest,
                                   "reviews": [{**good, "check_id": "NOPE-1"}]})
    assert any("未知或已判定" in blocker for blocker in release_check(root)["blockers"])

    _record(root / "review.json", {"experiment_id": EXPERIMENT_ID, "packet_sha256": digest,
                                   "reviews": [{**good, "status": "blocked"}]})
    assert any("状态非法" in blocker for blocker in release_check(root)["blockers"])

    # 有效审阅：包身份、检查身份、评审者与引用齐备后不再产生审阅相关阻塞。
    _record(root / "review.json", {"experiment_id": EXPERIMENT_ID, "packet_sha256": digest, "reviews": [good]})
    review_blockers = release_check(root)["blockers"]
    assert not any(
        marker in blocker
        for blocker in review_blockers
        for marker in ("过期包", "未知或已判定", "缺少评审者", "引用失效", "状态非法", "身份重复")
    )


# ---------- r2 剩余缺口回归：共享停止、审阅输入、最终状态、内容级零写入、计时边界 ----------


def test_shared_stop_state_persists_and_counts_consecutive(tmp_path):
    from test.v2_budget import CONSECUTIVE_LIMIT

    ledger = BudgetLedger(tmp_path / "ledger.json", DEFAULT_LIMITS, experiment_id="e")
    assert ledger.stop_info() is None
    # 成功与业务不通过均重置连续计数；每个执行单元只结算一次，各自携带稳定身份。
    ledger.record_outcome("run_error", execution_unit="slot:1", error_type="ValueError", owner="e", trigger="t")
    ledger.record_outcome("run_error", execution_unit="slot:2", error_type="ValueError", owner="e", trigger="t")
    reset = ledger.record_outcome("business_failure", execution_unit="slot:3", owner="e", trigger="t")
    assert reset["triggered"] is False and reset["consecutive"]["count"] == 0
    # 不同类别运行错误重置为 1；同类才连续累积。
    assert ledger.record_outcome("run_error", execution_unit="slot:4", error_type="RuntimeError",
                                 owner="e", trigger="t")["consecutive"]["count"] == 1
    same = ledger.record_outcome("run_error", execution_unit="slot:5", error_type="Other", owner="e", trigger="t")
    assert same["consecutive"] == {"category": "Other", "count": 1, "updated_at": same["consecutive"]["updated_at"]}
    triggered = None
    for offset in range(CONSECUTIVE_LIMIT - 1):
        triggered = ledger.record_outcome("run_error", execution_unit=f"slot:{6 + offset}",
                                          error_type="Other", owner="e", trigger="t", slot=7)
    assert triggered["triggered"] is True
    assert triggered["stop"]["reason_category"] == "consecutive_run_errors"
    assert triggered["stop"]["detail"]["count"] == CONSECUTIVE_LIMIT
    # 停止保持置位：后续成功不自动解除；首次触发不被新类别覆盖；跨实例可见。
    ledger.record_outcome("success", execution_unit="slot:9", owner="e", trigger="t")
    # 停止置位后成功不改写计数记录：连续类别的证据与触发次数保持不变。
    assert ledger.stop_info()["reason_category"] == "consecutive_run_errors"
    assert ledger.snapshot()["consecutive"]["category"] == "Other"
    # 实验隔离：另一个实验可独立置位，首个实验的停止不被覆盖。
    other_scope = BudgetLedger(tmp_path / "ledger.json", DEFAULT_LIMITS, experiment_id="e2")
    assert other_scope.request_stop("leak", owner="e2", trigger="t2")["reason_category"] == "leak"
    assert ledger.stop_info()["reason_category"] == "consecutive_run_errors"
    assert other_scope.stop_info()["reason_category"] == "leak"
    assert BudgetLedger(tmp_path / "ledger.json", DEFAULT_LIMITS, experiment_id="e").stop_info() is not None
    snapshot = ledger.snapshot()
    assert snapshot["stop"]["reason_category"] == "consecutive_run_errors"
    # 停止置位后的成功只推进终态与序号：连续计数保持触发时的类别，且不自动解除停止。
    assert snapshot["consecutive"]["category"] == "Other"
    assert snapshot["consecutive"]["count"] == CONSECUTIVE_LIMIT
    assert snapshot["stop"]["detail"]["count"] == CONSECUTIVE_LIMIT


def test_run_counts_isolated_per_experiment_on_shared_usage_ledger(tmp_path):
    from test.v2_budget import CONSECUTIVE_LIMIT

    path = tmp_path / "shared.json"
    a = BudgetLedger(path, DEFAULT_LIMITS, experiment_id="exp-a")
    b = BudgetLedger(path, DEFAULT_LIMITS, experiment_id="exp-b")
    # 同一模型用量账本被两个实验共用；A 的连续错误与停止不污染 B。
    for offset in range(CONSECUTIVE_LIMIT):
        a.record_outcome("run_error", execution_unit=f"slot:{offset}", error_type="ValueError",
                         owner="exp-a", trigger="t")
    assert a.stop_info()["reason_category"] == "consecutive_run_errors"
    assert b.stop_info() is None
    b.record_outcome("run_error", execution_unit="slot:0", error_type="ValueError", owner="exp-b", trigger="t")
    state_b = b.snapshot()
    assert state_b["consecutive"]["count"] == 1
    # 两条终态各自推进同一 completion_seq，落盘顺序与序号一致。
    assert state_b["completion_seq"] == CONSECUTIVE_LIMIT + 1
    assert sorted(state_b["experiments"]) == ["exp-a", "exp-b"]


def test_completion_seq_matches_persisted_order_and_duplicate_is_idempotent(tmp_path):
    path = tmp_path / "seq.json"
    ledger = BudgetLedger(path, DEFAULT_LIMITS, experiment_id="exp")
    first = ledger.record_outcome("success", execution_unit="slot:1", owner="exp", trigger="t")
    second = ledger.record_outcome("success", execution_unit="slot:2", owner="exp", trigger="t")
    duplicate = ledger.record_outcome("success", execution_unit="slot:2", owner="exp", trigger="t")
    assert (first["completion_seq"], second["completion_seq"]) == (1, 2)
    # 相同终态重复上报幂等：不推进序号、不新增计数。
    assert duplicate["completion_seq"] == 2 and duplicate["duplicate"] is True
    # 同一执行单元的冲突终态明确拒绝。
    with pytest.raises(ValueError):
        ledger.record_outcome("business_failure", execution_unit="slot:2", owner="exp", trigger="t")
    # 序号与实际落盘顺序对应：重开账本读到同一记录。
    persisted = read_record(path)
    assert persisted["completion_seq"] == 2
    assert set(persisted["experiments"]["exp"]["outcomes"]) == {"slot:1", "slot:2"}


def test_error_category_distinguishes_same_name_different_module(tmp_path):
    path = tmp_path / "module.json"
    ledger = BudgetLedger(path, DEFAULT_LIMITS, experiment_id="exp")
    ledger.record_outcome("run_error", execution_unit="slot:1",
                          error_type="package_a.CustomError", owner="exp", trigger="t")
    # 同名不同 module 属于不同类别：类别变化从 1 开始。
    second = ledger.record_outcome("run_error", execution_unit="slot:2",
                                   error_type="package_b.CustomError", owner="exp", trigger="t")
    assert second["consecutive"] == {"category": "package_b.CustomError", "count": 1,
                                     "updated_at": second["consecutive"]["updated_at"]}


def test_excluded_outcomes_keep_count_and_unclassified_counts_equally(tmp_path):
    from test.v2_budget import CONSECUTIVE_LIMIT

    path = tmp_path / "excluded.json"
    ledger = BudgetLedger(path, DEFAULT_LIMITS, experiment_id="exp")
    ledger.record_outcome("run_error", execution_unit="slot:1", error_type="ValueError",
                          owner="exp", trigger="t")
    # 预算暂停与取消/中断属于排除项：保持计数且不重置。
    kept = ledger.record_outcome("budget_paused", execution_unit="slot:2", owner="exp", trigger="t")
    assert kept["consecutive"]["count"] == 1
    kept = ledger.record_outcome("cancelled", execution_unit="slot:3", owner="exp", trigger="t")
    assert kept["consecutive"]["count"] == 1
    # 契约内预期故障同属排除项：保持计数且不重置。
    kept = ledger.record_outcome("expected_failure", execution_unit="slot:3b", owner="exp", trigger="t")
    assert kept["consecutive"]["count"] == 1
    # 缺少可靠现场类型时固定为 unclassified_run_error，并与已分类同权参与连续判定。
    counts = []
    for offset in range(CONSECUTIVE_LIMIT):
        last = ledger.record_outcome("run_error", execution_unit=f"slot:{4 + offset}",
                                     error_type=None, owner="exp", trigger="t")
        counts.append(last["consecutive"]["count"])
    assert counts == [1, 2, CONSECUTIVE_LIMIT]
    assert last["triggered"] is True
    assert last["stop"]["detail"]["error_type"] == "unclassified_run_error"


def test_claim_slot_rejected_after_shared_stop(tmp_path):
    from test.v2_budget import StopTriggered

    env = experiment_env("experiment", load_cases(CASES)[0], 0, 0, 0, tmp_path, {}, "2026-01-01")
    env = env.model_copy(update={"budget_ledger_path": str(tmp_path / "shared.json")})
    BudgetLedger(tmp_path / "shared.json", DEFAULT_LIMITS, experiment_id="experiment").request_stop("leak", owner="experiment", trigger="unit")
    with pytest.raises(StopTriggered):
        claim_slot(env)
    # 停止置位后不创建领取记录：领取与置位共用同一把锁。
    assert not (tmp_path / "claims").exists()


def test_worker_stops_when_shared_stop_set(tmp_path):
    from test import run_v2_eval

    root = _worker_experiment(tmp_path)
    manifest = read_record(root / "manifest.json")
    manifest["budget_ledger_path"] = str(tmp_path / "shared-stop.json")
    write_record(root / "manifest.json", manifest)
    BudgetLedger(tmp_path / "shared-stop.json", DEFAULT_LIMITS, experiment_id="experiment").request_stop(
        "leak", owner="experiment", trigger="unit")

    args = argparse.Namespace(experiment_root=str(tmp_path), experiment_id="experiment",
                              cases=str(CASES), processes=1, process_index=0, smoke_cases=None)
    assert run_v2_eval.run_worker(args, load_cases(CASES)[:1]) == 3
    assert (root / "stop-report.json").exists()
    assert not (root / "claims").exists()
    assert not (root / "preflight").exists()


def test_capture_turn_preserves_control_flow_exceptions(monkeypatch):
    monkeypatch.setattr(eval_support, "business_state", lambda: {})
    for exc in (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        class _Http:
            def stream(self, *_a, **_k):
                raise exc()

        with pytest.raises(exc):
            eval_support.capture_turn(_Http(), SESSION, case_id="P01", round_index=0,
                                      step_index=1, request_text="x", attachments=[])


def test_final_state_follows_last_step_order():
    observation = RoundObservation(
        case_id="P01", round_index=0, slot=0, session_id=SESSION, process_index=0,
        turns=[TurnObservation(case_id="P01", round_index=0, step_index=1, kind="user", session_id=SESSION,
                               operation_id=str(uuid4()), terminal="done", status="done",
                               after_state={"state": 1})],
        confirmations=[
            ConfirmationObservation(case_id="P01", round_index=0, step_index=2, kind="confirm",
                                    business_kind="profile", operation_id=str(uuid4()), request={},
                                    status_code=200, response={"status": "saved"}, rejected=False,
                                    after_state={"state": 2}),
            ConfirmationObservation(case_id="P01", round_index=0, step_index=3, kind="edit",
                                    operation_id=str(uuid4()), request={}, rejected=False,
                                    after_state={"state": 3}),
        ],
        checks=[], status="pass",
    )
    # 确认之后仍有 edit：最终状态取更晚的 edit 后状态，而非一律优先最后确认。
    assert eval_support._final_state(observation) == ({"state": 3}, "edit_after_state", 3)
    follow = observation.model_copy(update={"turns": [
        *observation.turns,
        TurnObservation(case_id="P01", round_index=0, step_index=4, kind="user", session_id=SESSION,
                        operation_id=str(uuid4()), terminal="done", status="done", after_state={"state": 4}),
    ]})
    # 确认之后继续用户回合：取更晚的用户回合后状态。
    assert eval_support._final_state(follow) == ({"state": 4}, "last_turn_prepare_state", 4)


def test_review_stage_rejects_and_accepts_through_real_entry(tmp_path):
    from test import run_v2_eval

    root = tmp_path / "review"
    root.mkdir()
    cases, _identities = _release_rounds(root)
    plan = [{"slot": index, "case_id": case.case_id, "category": case.category, "round_index": 0}
            for index, case in enumerate(cases)]
    _record(root / "manifest.json", {
        "experiment_id": EXPERIMENT_ID, "slot_plan": plan, "cases_path": str(CASES), "rounds": 1,
    })
    rounds = load_round_evidence(root)
    target = next(item for item in rounds if any(c.status == "pending_review" for c in item.checks))
    check = next(c for c in target.checks if c.status == "pending_review")
    good = {
        "case_id": target.case_id, "round_index": target.round_index, "check_id": check.check_id,
        "status": "pass", "reason": "口径符合", "reviewer": "reviewer-a",
        "evidence_refs": [f"cases/{target.case_id}/round-{target.round_index}/round.json"],
    }
    input_path = tmp_path / "review-in.json"

    # 外实验输入：经真入口拒绝，不生成当前实验有效 review.json。
    _record(input_path, {"experiment_id": "other", "packet_sha256": "x", "reviews": [good]})
    with pytest.raises(ValueError, match="身份校验"):
        run_v2_eval._review_stage(root, cases, rounds, str(input_path))
    assert not (root / "review.json").exists()
    assert (root / "review-rejected.json").exists()

    # 过期包：同样拒绝。
    packet = build_review_packet(rounds, cases)
    digest = hashlib.sha256(json.dumps(packet, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    _record(input_path, {"experiment_id": EXPERIMENT_ID, "packet_sha256": "stale", "reviews": [good]})
    with pytest.raises(ValueError, match="过期包"):
        run_v2_eval._review_stage(root, cases, rounds, str(input_path))
    assert not (root / "review.json").exists()

    # 合法输入：应用并写入绑定当前实验身份与重算哈希的 review.json。
    _record(input_path, {"experiment_id": EXPERIMENT_ID, "packet_sha256": digest, "reviews": [good]})
    applied, reviews = run_v2_eval._review_stage(root, cases, rounds, str(input_path))
    written = read_record(root / "review.json")
    assert written["experiment_id"] == EXPERIMENT_ID and written["packet_sha256"] == digest
    assert reviews == [good] and len(applied) == len(rounds)


def test_protected_state_captures_content_not_just_counts(tmp_path):
    def digest_for(payload: str) -> tuple[str, dict]:
        path = tmp_path / f"db-{abs(hash(payload))}.db"
        _empty_database(path)
        connection = sqlite3.connect(str(path))
        try:
            connection.execute(
                "INSERT INTO plans (id, is_current, content, created_at) VALUES (1, 1, ?, 1)", (payload,)
            )
            connection.commit()
        finally:
            connection.close()
        state = read_state(str(path), SESSION)
        return eval_support._digest(eval_support._protected_state(str(path), SESSION)), state

    first, state_a = digest_for('{"days": []}')
    second, state_b = digest_for('{"days": [{"kind": "rest"}]}')
    # 行数、ID、状态完全一致，仅内容不同：受保护摘要必须随之不同。
    assert state_a["plans"]["count"] == state_b["plans"]["count"] == 1
    assert state_a["plans"]["versions"] == state_b["plans"]["versions"] == ["1"]
    assert first != second


def test_milvus_identity_reads_collections_list(monkeypatch):
    from app.application.business import retrieval_index as ri

    class _Conn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def call(self, method, *_a, **_k):
            if method == "list_aliases":
                return {"aliases": ["a"]}
            if method == "list_collections":
                return ["c1", "c2"]
            raise AssertionError(method)

    monkeypatch.setattr(ri.MilvusConnection, "from_env", classmethod(lambda _cls: _Conn()))

    async def _published(_db):
        return "c1"

    async def _manifest(_db, _target):
        return {"format": "f"}

    monkeypatch.setattr(ri, "published_collection", _published)
    monkeypatch.setattr(ri, "read_manifest", _manifest)
    identity = eval_support._milvus_identity()
    # list_collections() 返回列表（不是 {"collections": [...]}），身份探针必须按列表读取。
    assert identity["collections"] == ["c1", "c2"]
    assert identity["collection"] == "c1" and identity["alias"] == ri.PUBLISHED_ALIAS

