import hashlib
import multiprocessing
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from test.run_v2_eval import (
    build_execution_ledger,
    current_identity,
    load_round_evidence,
    write_reports,
)
from test.test_v2_metrics import CASES, _round
from test.v2_attempts import (
    attempt_summary,
    budget_path,
    claim_slot,
    create_attempt,
    read_record,
    reserve_experiment,
    write_record,
)
from test.v2_budget import DEFAULT_LIMITS, BudgetLedger
from test.v2_contract import ROUNDS, ExperimentEnv
from test.v2_eval_support import (
    BACKEND,
    aggregate,
    experiment_env,
    load_cases,
    slot_plan,
)


def env_for(root):
    return experiment_env("experiment", load_cases(CASES)[0], 0, 0, 0, root, {}, "2026-01-01")


def hashes(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def claim_child(payload, barrier, queue):
    barrier.wait()
    try:
        claim_slot(ExperimentEnv.model_validate(payload))
        queue.put("claimed")
    except FileExistsError:
        queue.put("refused")


def test_claim_preserves_partial_and_completed_artifacts(tmp_path):
    env = env_for(tmp_path)
    claim_slot(env)
    evidence = Path(env.evidence_dir)
    evidence.mkdir(parents=True)
    (evidence / "step-1.json").write_text('{"probe":"interrupted"}', encoding="utf-8")
    for database in (env.database_path, env.trace_path):
        path = Path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE validation_probe (value TEXT)")
            connection.execute("INSERT INTO validation_probe VALUES ('preserve')")
    attachments = Path(env.attachment_root)
    attachments.mkdir(parents=True)
    (attachments / "probe.txt").write_text("preserve", encoding="utf-8")
    before = hashes(tmp_path)
    with pytest.raises(FileExistsError):
        claim_slot(env)
    assert hashes(tmp_path) == before
    assert build_execution_ledger(slot_plan(load_cases(CASES)), [], tmp_path)["slots"][0]["status"] == "claimed_interrupted"
    (evidence / "round.json").write_text("completed evidence probe", encoding="utf-8")
    before = hashes(tmp_path)
    with pytest.raises(FileExistsError):
        claim_slot(env)
    assert hashes(tmp_path) == before


def test_two_real_processes_only_one_claim(tmp_path):
    env = env_for(tmp_path)
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    processes = [context.Process(target=claim_child, args=(env.model_dump(), barrier, queue)) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(60)
        assert process.exitcode == 0
    assert sorted(queue.get(timeout=5) for _ in processes) == ["claimed", "refused"]


@pytest.mark.parametrize("artifact", ["evidence_dir", "database_path", "trace_path", "attachment_root"])
def test_legacy_artifacts_refused(tmp_path, artifact):
    env = env_for(tmp_path)
    path = Path(getattr(env, artifact))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("legacy evidence", encoding="utf-8")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(FileExistsError, match="已有证据"):
        claim_slot(env)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    with pytest.raises(FileExistsError):
        claim_slot(env)


def source_experiment(root):
    root.mkdir()
    env = env_for(root)
    claim_slot(env)
    identity = {"source_sha256": "identity-unit-check", "model_identity": {}}
    write_record(root / "manifest.json", {"experiment_id": "experiment", "execution_identity": identity,
                                         "cases_path": str(CASES), "rounds": ROUNDS,
                                         "budget_ledger_path": str(root.parent / "budget-ledger.json")})
    return env, identity


def test_attempt_isolation_and_shared_real_budget(tmp_path):
    root = tmp_path / "experiment"
    env, identity = source_experiment(root)
    before = hashes(root)
    first, record1 = create_attempt(root, env.case_id, 0, "reproduction", "local validation", identity)
    second, record2 = create_attempt(root, env.case_id, 0, "extra", "local validation", identity)
    assert first != second
    assert set(record1["evidence_refs"].values()).isdisjoint(record2["evidence_refs"].values())
    assert record1["budget_ledger_path"] == record2["budget_ledger_path"] == str(budget_path(root))
    model = next(iter(DEFAULT_LIMITS))
    BudgetLedger(record1["budget_ledger_path"], experiment_id="experiment").reserve(model, record1["attempt_id"], 11, "local reservation validation; no provider call")
    BudgetLedger(record2["budget_ledger_path"], experiment_id="experiment").reserve(model, record2["attempt_id"], 13, "local reservation validation; no provider call")
    snapshot = BudgetLedger(budget_path(root), experiment_id="experiment").snapshot()
    assert budget_path(tmp_path / "new-experiment") == budget_path(root)
    assert snapshot["total"]["reserved"] == 24
    assert snapshot["total"]["actual"] == 0
    assert snapshot["models"][model]["attempts"] == 2
    assert all(hashes(root)[path] == value for path, value in before.items())
    assert attempt_summary(root)["count"] == 2
    assert load_round_evidence(root) == []
    result = aggregate([], [case.case_id for case in load_cases(CASES)])
    assert result["executions"] == 150
    assert result["execution_pass_rate"] == result["stable_pass_rate"] == 0
    with pytest.raises(ValueError, match="身份不匹配"):
        create_attempt(root, env.case_id, 0, "extra", "changed code", {})


def test_reproductions_excluded_and_duplicate_formal_detected(tmp_path):
    root = tmp_path / "experiment"
    env, identity = source_experiment(root)
    attempt, _ = create_attempt(root, env.case_id, 0, "extra", "path validation", identity)
    observation = _round(env.case_id, 0, "fail")
    formal = Path(env.evidence_dir) / "round.json"
    formal.parent.mkdir(parents=True)
    write_record(formal, observation.model_dump())
    reproduction = attempt / "cases" / env.case_id / "round-0" / "round.json"
    reproduction.parent.mkdir(parents=True)
    write_record(reproduction, observation.model_dump())
    assert len(load_round_evidence(root)) == 1
    duplicate = root / "cases" / "duplicate" / "round-0" / "round.json"
    duplicate.parent.mkdir(parents=True)
    write_record(duplicate, observation.model_dump())
    with pytest.raises(ValueError, match="重复正式槽位"):
        load_round_evidence(root)
    with pytest.raises(ValueError, match="重复正式槽位"):
        aggregate([observation, observation])
    with pytest.raises(ValueError, match="重复正式槽位"):
        build_execution_ledger(load_cases(CASES), [observation, observation])


def test_cli_reentry_before_manifest_or_config_write(tmp_path):
    root = tmp_path / "existing"
    root.mkdir()
    (root / "manifest.json").write_text("preserve manifest", encoding="utf-8")
    config = root / "model-config"
    config.mkdir()
    (config / "models.json").write_text("preserve config", encoding="utf-8")
    before = hashes(root)
    command = [sys.executable, "-X", "utf8", "-m", "test.run_v2_eval", "--experiment-root", str(tmp_path), "--experiment-id", "existing"]
    result = subprocess.run(command, cwd=BACKEND, capture_output=True, encoding="utf-8")
    assert result.returncode != 0
    assert "FileExistsError" in result.stderr
    assert hashes(root) == before
    with pytest.raises(FileExistsError):
        reserve_experiment(root)


def test_additional_cli_refuses_before_touching_source(tmp_path):
    root = tmp_path / "experiment"
    env, _ = source_experiment(root)
    config = root / "model-config"
    config.mkdir()
    model = {"provider": "stepfun", "api": "openai-completions", "model": "step-3.7-flash", "base_url": "https://api.stepfun.com/step_plan/v1/"}
    write_record(config / "models.json", model)
    write_record(config / "auth.json", {"api_key": "local-config-validation"})
    manifest = read_record(root / "manifest.json")
    manifest["execution_identity"] = current_identity(CASES, model)
    manifest["business_date"] = "2026-01-01"
    write_record(root / "manifest.json", manifest)

    def command(case_id):
        return [sys.executable, "-X", "utf8", "-m", "test.run_v2_eval", "--experiment-root", str(tmp_path),
                "--experiment-id", "experiment", "--case-id", case_id,
                "--round-index", "0", "--purpose", "reproduction", "--reason", "local validation"]

    # 未知原用例与冻结身份失配都在建立追加运行目录之前就地失败，零写入且不触碰预算账本。
    before = hashes(root)
    unknown = subprocess.run(command("NOPE"), cwd=BACKEND, capture_output=True, encoding="utf-8")
    assert unknown.returncode == 1, unknown.stdout
    assert "未知原用例" in unknown.stderr
    assert hashes(root) == before

    stale = read_record(root / "manifest.json")
    stale["execution_identity"] = {"source_sha256": "stale", "model_identity": model}
    write_record(root / "manifest.json", stale)
    before_mismatch = hashes(root)
    mismatched = subprocess.run(command(env.case_id), cwd=BACKEND, capture_output=True, encoding="utf-8")
    assert mismatched.returncode == 1, mismatched.stdout
    assert "身份不匹配" in mismatched.stderr
    assert hashes(root) == before_mismatch
    assert attempt_summary(root)["count"] == 0
    assert not (root / "reproductions").exists()
    assert not Path(stale["budget_ledger_path"]).exists()


def test_reports_keep_raw_shared_budget_and_planned_denominators(tmp_path):
    root = tmp_path / "experiment"
    env, identity = source_experiment(root)
    plan = slot_plan(load_cases(CASES))
    create_attempt(root, env.case_id, 0, "extra", "report validation", identity)
    ledger = BudgetLedger(budget_path(root), experiment_id="experiment")
    ledger.reserve(next(iter(DEFAULT_LIMITS)), "reservation-probe", 17, "local validation; no provider call")
    before = ledger.path.read_bytes()
    summary = write_reports(root, "experiment", plan, [], {}, {})
    assert ledger.path.read_bytes() == before
    assert summary["status"] == "incomplete"
    assert summary["planned_slots"] == 150
    assert summary["aggregate"]["executions"] == 150
    assert summary["aggregate"]["observed_executions"] == 0
    assert summary["aggregate"]["independent_cases"] == 50
    assert summary["additional_runs"]["count"] == 1
    assert read_record(root / "budget-summary.json")["total"]["reserved"] == 17
    assert read_record(root / "execution-ledger.json")["slots"][0]["status"] == "claimed_interrupted"


def test_legacy_budget_requires_explicit_parent_for_new_experiment(tmp_path):
    old = tmp_path / "old"
    old.mkdir()
    model = next(iter(DEFAULT_LIMITS))
    BudgetLedger(old / "budget-ledger.json", experiment_id="experiment").reserve(model, "reservation-probe", 7, "local validation")
    with pytest.raises(ValueError, match="显式关联"):
        budget_path(tmp_path / "new")
    assert budget_path(old) == old / "budget-ledger.json"
