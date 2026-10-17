import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from app.domain.business.models import BusinessContext
from app.infrastructure.persistence.sqlite.database import open_database
from test.run_v2_eval import current_identity, load_round_evidence, release_check
from test.v2_attempts import write_record
from test.v2_business_cases import check_coverage, evaluate
from test.v2_contract import CheckResult, RoundObservation
from test.v2_eval_support import (
    BACKEND,
    _round_status,
    aggregate,
    load_cases,
    validate_cases,
)

CASES = Path(__file__).resolve().parent / "v2_cases.json"
SESSION = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def tmp_path():
    root = BACKEND / "temp/pytest-v2-check-integrity" / uuid4().hex[:12]
    root.mkdir(parents=True)
    return root


def context() -> BusinessContext:
    return BusinessContext(
        timezone="Asia/Shanghai",
        business_date="2026-01-01",
        session_id=SESSION,
        run_id="00000000-0000-4000-8000-000000000002",
        request_entry_id="00000000-0000-4000-8000-000000000003",
        source_entry_id="00000000-0000-4000-8000-000000000004",
    )


def real_database(path: Path) -> Path:
    async def create():
        connection = await open_database(path)
        await connection.close()

    asyncio.run(create())
    return path


def observation(case_id: str, round_index: int = 0) -> RoundObservation:
    return RoundObservation(
        case_id=case_id,
        round_index=round_index,
        slot=0,
        step_index=1,
        session_id=SESSION,
        process_index=0,
        status="pass",
    )


def cases_by_id():
    return {case.case_id: case for case in load_cases(CASES)}


def results_for(case, database):
    return evaluate(
        case,
        observation=observation(case.case_id),
        service=None,
        database_path=database,
        business_context=context(),
    )


def declared_ids(case):
    return [check.check_id for check in case.checks]


@pytest.mark.parametrize(
    ("case_id", "deterministic", "manual"),
    [
        ("S02", ["S02-S1-01", "S02-S1-02"], "S02-S1-03"),
        ("S03", ["S03-S1-01", "S03-S1-02"], "S03-S1-03"),
        ("S04", ["S04-S1-01", "S04-S1-02"], "S04-S1-03"),
        ("S05", ["S05-S1-01", "S05-S1-02", "S05-S1-03", "S05-S1-04"], "S05-S1-05"),
    ],
)
def test_frozen_check_mapping_generates_all_required_checks(tmp_path, case_id, deterministic, manual):
    case = cases_by_id()[case_id]
    database = real_database(tmp_path / f"{case_id}.db")
    results = results_for(case, database)
    assert [item.check_id for item in results] == declared_ids(case)
    assert [item.check_id for item in results if item.kind == "deterministic"] == deterministic
    assert all(item.status == "pass" for item in results if item.kind == "deterministic")
    assert [item.check_id for item in results if item.kind == "manual"] == [manual]
    assert all(item.status == "pending_review" for item in results if item.kind == "manual")
    integrity = check_coverage(case, results, round_index=0)
    assert integrity["complete"] is True
    assert _round_status([], [], results, integrity) == "pending_review"


def test_check_coverage_rejects_empty_missing_duplicate_and_misowned(tmp_path):
    case = cases_by_id()["S02"]
    database = real_database(tmp_path / "coverage.db")
    complete = results_for(case, database)

    empty = check_coverage(case, [], round_index=0)
    assert empty["complete"] is False
    assert empty["missing_check_ids"] == declared_ids(case)

    missing = check_coverage(case, complete[1:], round_index=0)
    assert missing["complete"] is False
    assert missing["missing_check_ids"] == [declared_ids(case)[0]]

    duplicated = check_coverage(case, [*complete, complete[0]], round_index=0)
    assert duplicated["complete"] is False
    assert duplicated["duplicate_check_ids"] == [complete[0].check_id]

    wrong_case = [complete[0].model_copy(update={"case_id": "S03"}), *complete[1:]]
    misowned = check_coverage(case, wrong_case, round_index=0)
    assert misowned["complete"] is False
    assert any("归属用例 S03" in item for item in misowned["identity_errors"])

    wrong_round = [complete[0].model_copy(update={"round_index": 1}), *complete[1:]]
    rotated = check_coverage(case, wrong_round, round_index=0)
    assert rotated["complete"] is False
    assert any("归属轮次 1" in item for item in rotated["identity_errors"])

    wrong_kind = [complete[0].model_copy(update={"kind": "manual"}), *complete[1:]]
    kind_error = check_coverage(case, wrong_kind, round_index=0)
    assert kind_error["complete"] is False
    assert any("类型 manual" in item for item in kind_error["identity_errors"])

    unexpected = [*complete, CheckResult(
        case_id=case.case_id, round_index=0, check_id="S02-S1-99",
        kind="deterministic", status="pass", reason="计划外",
    )]
    outside = check_coverage(case, unexpected, round_index=0)
    assert outside["complete"] is False
    assert outside["unexpected_check_ids"] == ["S02-S1-99"]


def test_complete_checks_pass_and_pending_semantics_stay_pending(tmp_path):
    case = cases_by_id()["S05"]
    database = real_database(tmp_path / "status.db")
    results = results_for(case, database)
    integrity = check_coverage(case, results, round_index=0)
    assert integrity["complete"] is True
    assert _round_status([], [], results, integrity) == "pending_review"

    reviewed = [
        item.model_copy(update={"status": "pass"}) if item.status == "pending_review" else item
        for item in results
    ]
    reviewed_integrity = check_coverage(case, reviewed, round_index=0)
    assert reviewed_integrity["complete"] is True
    assert _round_status([], [], reviewed, reviewed_integrity) == "pass"


def test_missing_step_contract_raises_instead_of_empty_pass(tmp_path):
    case = cases_by_id()["S02"].model_copy(update={"checks": []})
    database = real_database(tmp_path / "empty-contract.db")
    with pytest.raises(ValueError, match="缺少冻结检查项"):
        results_for(case, database)


def test_validate_cases_rejects_step_gap_and_invalid_id():
    case = cases_by_id()["P01"]
    gap = case.model_copy(update={
        "checks": [item for item in case.checks if not item.check_id.startswith("P01-S2-")],
    })
    errors = validate_cases([gap])
    assert any("步骤 2 缺少检查项" in error for error in errors)

    invalid = case.model_copy(update={
        "checks": [
            *case.checks,
            case.checks[0].model_copy(update={"check_id": "A-state"}),
        ],
    })
    errors = validate_cases([invalid])
    assert any("检查项 ID 格式非法" in error for error in errors)


def test_historical_empty_checks_pass_is_marked_missing_without_rewrite(tmp_path):
    root = tmp_path / "historical"
    round_path = root / "cases" / "S02" / "round-0" / "round.json"
    round_path.parent.mkdir(parents=True)
    original = observation("S02").model_copy(update={"checks": [], "status": "pass"})
    write_record(round_path, original.model_dump())
    write_record(root / "manifest.json", {"cases_path": str(CASES)})
    before = round_path.read_bytes()

    rounds = load_round_evidence(root)
    assert len(rounds) == 1
    assert rounds[0].status == "pass"
    assert rounds[0].checks == []
    assert rounds[0].check_integrity["complete"] is False
    assert rounds[0].check_integrity["missing_check_ids"] == declared_ids(cases_by_id()["S02"])
    assert round_path.read_bytes() == before

    case_ids = [case.case_id for case in load_cases(CASES)]
    result = aggregate(rounds, case_ids, 3)
    assert result["executions"] == 150
    assert result["passed_executions"] == 0
    assert result["missing_evidence_executions"] == 1
    assert result["missing_evidence"][0]["case_id"] == "S02"
    assert result["missing_evidence"][0]["missing_check_ids"] == declared_ids(cases_by_id()["S02"])
    assert result["per_case"]["S02"]["rounds"]["0"] == "insufficient_evidence"


def test_release_blocks_historical_missing_checks(tmp_path):
    root = tmp_path / "release"
    round_path = root / "cases" / "S02" / "round-0" / "round.json"
    round_path.parent.mkdir(parents=True)
    write_record(round_path, observation("S02").model_copy(update={"checks": [], "status": "pass"}).model_dump())
    write_record(root / "manifest.json", {
        "experiment_id": "integrity-check",
        "slot_plan": [{"slot": 0, "case_id": "S02", "category": "risk_unknown", "round_index": 0}],
        "cases_path": str(CASES),
        "rounds": 1,
        "model_identity": {},
        "execution_identity": current_identity(CASES, {}),
        "budget_ledger_path": str(root / "budget-ledger.json"),
    })
    write_record(root / "budget-ledger.json", {"models": {}, "experiments": {}})

    result = release_check(root)
    assert result["released"] is False
    assert any("检查证据不完整" in blocker and "S02-S1-01" in blocker for blocker in result["blockers"])
