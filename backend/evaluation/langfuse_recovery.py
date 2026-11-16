import argparse
import hashlib
import importlib
import json
import os
import subprocess
import signal
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from dotenv import load_dotenv

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND / "temp" / "recovery-langfuse"
SUITES = ("test/test_governance_contract.py", "test/test_stability_task2.py", "test/test_stability_task3.py")
CHECKS = ("test.check_session_store", "test.check_profile_crash", "test.check_workout_crash", "test.check_structured_confirmation")
TIMEOUT = 210

if not __debug__:
    raise RuntimeError("恢复测试禁止 Python 优化模式")


def save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def child_environment(root: Path) -> dict:
    env = os.environ.copy()
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1", "FIT_TRACE_CLOUD_ENABLED": "false", "FIT_RECOVERY_ROOT": str(root), "TMP": str(root), "TEMP": str(root), "TMPDIR": str(root)})
    return env


def terminate_owned(process: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=True, capture_output=True, timeout=15)
    else:
        pairs = [tuple(map(int, line.split())) for line in subprocess.check_output(["ps", "-eo", "pid=,ppid="], text=True, timeout=10).splitlines()]
        owned = {process.pid}
        while True:
            descendants = owned | {pid for pid, parent in pairs if parent in owned}
            if descendants == owned:
                break
            owned = descendants
        for pid in owned - {process.pid}:
            try:
                if os.getpgid(pid) == pid:
                    os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=15)


def execute(command: list[str], root: Path, *, timeout: int = TIMEOUT) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with (root / "stdout.log").open("wb") as output:
        process = subprocess.Popen(command, cwd=BACKEND, env=child_environment(root), stdout=output, stderr=subprocess.STDOUT, start_new_session=os.name != "nt")
        try:
            code = process.wait(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            terminate_owned(process)
            code = process.returncode
            timed_out = True
    return {"command": command, "exit_code": code, "timed_out": timed_out, "duration_seconds": round(time.monotonic() - started, 3)}


class PytestPlugin:
    def __init__(self, root):
        self.root = root
        self.items = []
        self.reports = []

    def pytest_collection_modifyitems(self, session, config, items):
        self.items = [item.nodeid for item in items]
        for item in items:
            if item.module.__name__.endswith("test_stability_task3"):
                item.module.TMP_ROOT = self.root / "task3"
                item.module.TMP_ROOT.mkdir(parents=True, exist_ok=True)

    def pytest_runtest_logreport(self, report):
        self.reports.append({"phase": report.when, "outcome": report.outcome})


def collect(root: Path) -> dict:
    command = [sys.executable, "-m", "evaluation.langfuse_recovery", "collect-child", str(root)]
    run = execute(command, root)
    assert run["exit_code"] == 0 and not run["timed_out"], "pytest 收集失败"
    nodeids = json.loads((root / "nodeids.json").read_text(encoding="utf-8"))
    assert nodeids and len(nodeids) == len(set(nodeids))
    from test.check_recovery_cases import CASES
    cases = [{"case_id": nodeid, "type": "pytest"} for nodeid in nodeids]
    cases += [{"case_id": name, "type": "check"} for name in CHECKS]
    cases += [{"case_id": name, "type": "recovery"} for name in CASES]
    encoded = json.dumps(cases, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    counts = {suite: sum(item.startswith(suite + "::") for item in nodeids) for suite in SUITES}
    result = {"cases": cases, "counts": {**counts, "checks": len(CHECKS), "recovery": len(CASES), "total": len(cases)}, "sha256": digest(encoded), "collection": run}
    save(root / "manifest.json", result)
    return result


def prepare_imports(root: Path) -> None:
    import test.regression_support as support
    support.TEMP_ROOT = root


def collect_child(root: Path) -> None:
    prepare_imports(root)
    import pytest
    plugin = PytestPlugin(root)
    code = pytest.main(["--collect-only", "-q", "-p", "no:cacheprovider", "--assert=plain", *SUITES, f"--basetemp={root / 'pytest'}"], plugins=[plugin])
    save(root / "nodeids.json", plugin.items)
    if code:
        raise SystemExit(code)


def existing_check(name: str, root: Path) -> None:
    prepare_imports(root)
    module = importlib.import_module(name)
    if name == "test.check_session_store":
        module.TEMP_ROOT = root / "session-store"
    module.check()


def child(case_type: str, case_id: str, root: Path) -> None:
    prepare_imports(root)
    reports = []
    code = 1
    try:
        if case_type == "pytest":
            plugin = PytestPlugin(root)
            code = int(pytest.main(["-q", "-p", "no:cacheprovider", "--assert=plain", case_id, f"--basetemp={root / 'pytest'}"], plugins=[plugin]))
            reports = plugin.reports
        elif case_type == "check":
            existing_check(case_id, root)
            code = 0
        elif case_type == "recovery":
            from test.check_recovery_cases import run_case
            evidence = run_case(case_id, root / "scenario")
            save(root / "recovery.json", evidence)
            code = 0
        else:
            raise ValueError(case_type)
    finally:
        save(root / "child-result.json", {"exit_code": code, "pytest_reports": reports})
    if code:
        raise SystemExit(code)


def case_run(case: dict, root: Path) -> dict:
    command = [sys.executable, "-m", "evaluation.langfuse_recovery", "child", case["type"], case["case_id"], str(root)]
    run = execute(command, root)
    if case["case_id"] == "running-steering":
        for name in ("auth.json", "provider.journal"):
            (root / "scenario" / "model-config" / name).unlink(missing_ok=True)
    path = root / "child-result.json"
    measured = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    skipped = bool(measured and any(report["outcome"] == "skipped" for report in measured["pytest_reports"]))
    completed = not run["timed_out"] and measured is not None and run["exit_code"] == 0 and measured["exit_code"] == 0 and not skipped
    call_passed = measured is not None and (case["type"] != "pytest" or sum(report["phase"] == "call" and report["outcome"] == "passed" for report in measured["pytest_reports"]) == 1)
    passed = completed and call_passed
    evidence_path = root / "recovery.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8")) if evidence_path.exists() else None
    failure_kind = None if passed else ("case_failure" if measured is not None and not run["timed_out"] else "harness_failure")
    counts = None
    if measured is not None and not run["timed_out"]:
        reports = measured["pytest_reports"]
        if case["type"] == "pytest":
            counts = {"executed": int(any(report["phase"] == "call" for report in reports)), "passed": int(any(report["phase"] == "call" and report["outcome"] == "passed" for report in reports)), "failed": int(any(report["outcome"] == "failed" for report in reports)), "skipped": int(skipped)}
        else:
            counts = {"executed": 1, "passed": int(passed), "failed": int(not passed), "skipped": 0}
    output = {"test_counts": counts, "case_id": case["case_id"], "type": case["type"], "execution_completed": int(completed), "assertion_passed": int(passed), "failure_kind": failure_kind, "skipped": skipped, "exit_code": run["exit_code"], "timed_out": run["timed_out"], "duration_seconds": run["duration_seconds"], "pytest_reports": measured["pytest_reports"] if measured else [], "evidence": evidence}
    save(root / "case-result.json", {**output, "command": command})
    return output


def code_identity() -> dict:
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=BACKEND, text=True).strip()
    status = subprocess.check_output(["git", "status", "--porcelain"], cwd=BACKEND, text=True)
    paths = sorted([*BACKEND.joinpath("app").rglob("*.py"), *BACKEND.joinpath("test").glob("*.py"), Path(__file__)])
    hashed = hashlib.sha256()
    for path in paths:
        hashed.update(path.relative_to(BACKEND).as_posix().encode())
        hashed.update(path.read_bytes())
    return {"commit": commit, "dirty": bool(status.strip()), "source_sha256": hashed.hexdigest()}


def required_scores(output: dict) -> dict:
    metrics = {key: output[key] for key in ("execution_completed", "assertion_passed")}
    if output["type"] == "recovery" and output["assertion_passed"]:
        evidence = output["evidence"]
        assert evidence["case_id"] == output["case_id"]
        assert evidence["recovery_idempotent"] == 1
        metrics["recovery_idempotent"] = evidence["recovery_idempotent"]
        if output["case_id"].endswith("-after_commit"):
            assert evidence["save_exactly_once"] == 1
            metrics["save_exactly_once"] = evidence["save_exactly_once"]
    return metrics


def score_set_matches(expected: dict, scores: list[tuple]) -> bool:
    return len(scores) == len(expected) and len({name for name, _ in scores}) == len(scores) and dict(scores) == expected


def evaluations(output: dict) -> list:
    from langfuse import Evaluation
    return [Evaluation(name=name, value=value) for name, value in required_scores(output).items()]


def cli_list(resource: str, args: list[str], deadline: float) -> list:
    executable = "npx.cmd" if os.name == "nt" else "npx"
    rows = []
    cursor = None
    seen = set()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("云端回读超时")
        command = [executable, "--no-install", "langfuse-cli", "api", resource, "list", *args, "--json"]
        if cursor:
            command += ["--cursor", cursor]
        ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=ROOT) as stdout, tempfile.TemporaryFile(dir=ROOT) as stderr:
            process = subprocess.Popen(command, cwd=BACKEND, stdout=stdout, stderr=stderr, start_new_session=os.name != "nt")
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except BaseException:
                terminate_owned(process)
                raise
            stdout.seek(0)
            stderr.seek(0)
            output = stdout.read().decode("utf-8")
            errors = stderr.read().decode("utf-8")
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, command, output=output, stderr=errors)
        payload = json.loads(output)
        assert payload["status"] == 200
        body = payload["body"]
        assert isinstance(body["data"], list) and isinstance(body["meta"], dict)
        rows.extend(body["data"])
        cursor = body["meta"].get("cursor")
        if cursor is None:
            return rows
        assert isinstance(cursor, str) and cursor and cursor not in seen
        seen.add(cursor)


def verify(traces: dict, experiment_id: str, started_at: str, timeout: int = 60) -> dict:
    deadline = time.monotonic() + timeout
    pending = dict(traces)
    verified = {}
    while pending and time.monotonic() < deadline:
        items = cli_list("experiment-items", ["--experiment-id", experiment_id, "--from-start-time", started_at], deadline)
        for trace_id, expected in list(pending.items()):
            observations = cli_list("observations", ["--trace-id", trace_id, "--from-start-time", started_at, "--fields", "core,basic,metadata"], deadline)
            scores = cli_list("scores", ["--trace-id", trace_id, "--fields", "subject"], deadline)
            task = [row for row in observations if row["traceId"] == trace_id and row["id"] == expected["observation_id"] and row["name"] == "experiment-item-task" and row["metadata"].get("case_id") == expected["case_id"]]
            item = [row for row in items if row["experimentId"] == experiment_id and row["traceId"] == trace_id and row["id"] == expected["observation_id"]]
            actual = [(row["name"], row["value"]) for row in scores if row["subject"] == {"kind": "observation", "id": expected["observation_id"], "traceId": trace_id} and row["name"] in expected["scores"]]
            if len(item) == 1 and len(task) == 1 and score_set_matches(expected["scores"], actual):
                verified[trace_id] = {"observation_id": expected["observation_id"], "scores": dict(actual)}
                del pending[trace_id]
        if pending:
            time.sleep(min(2, max(0, deadline - time.monotonic())))
    return {"verified": verified, "pending": list(pending)}


def experiment(manifest: dict, root: Path, selected: list[dict]) -> dict:
    from langfuse import Langfuse
    identity = code_identity()
    started_at = datetime.now(UTC).isoformat()
    run_name = f"recovery-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
    receipt = {"status": "incomplete", "stage": "authentication", "run_name": run_name, "experiment_id": None, "experiment_url": None, "code_identity": identity, "manifest_sha256": manifest["sha256"], "selection": selected, "items": [{"case_id": case["case_id"], "stage": "pending"} for case in selected]}
    save(root / "receipt.json", receipt)
    client = None
    try:
        client = Langfuse(environment="recovery-test")
        assert client.auth_check(), "Langfuse 认证失败"
        data = [{"input": case, "metadata": {"case_id": case["case_id"], "source_sha256": identity["source_sha256"]}} for case in selected]
        rows = {row["case_id"]: row for row in receipt["items"]}

        def task(*, item, **kwargs):
            case = item["input"]
            row = rows[case["case_id"]]
            row.update(stage="task", trace_id=client.get_current_trace_id(), observation_id=client.get_current_observation_id())
            save(root / "receipt.json", receipt)
            try:
                assert row["trace_id"] and row["observation_id"]
                case_dir = root / "cases" / digest(case["case_id"].encode())[:20]
                output = case_run(case, case_dir)
                evidence = output.get("evidence") or {}
                client.update_current_span(metadata={"case_id": case["case_id"], "kill_phase": evidence.get("kill_phase"), "business_identity": evidence.get("identity"), **identity})
                row.update(stage="evaluation", failure_kind=output["failure_kind"], output=output)
                save(root / "receipt.json", receipt)
                return output
            except Exception as error:
                row.update(stage="task_failed", failure_kind="harness_failure", error_type=type(error).__name__)
                save(root / "receipt.json", receipt)
                raise

        def evaluator(*, output, **kwargs):
            return evaluations(output)

        receipt["stage"] = "experiment"
        save(root / "receipt.json", receipt)
        result = client.run_experiment(name="fit-agent-recovery", run_name=run_name, data=data, task=task, evaluators=[evaluator], max_concurrency=1, metadata={**identity, "manifest_sha256": manifest["sha256"]})
        receipt.update(experiment_id=result.experiment_id, experiment_url=result.experiment_url, run_name=result.run_name)
        expected = {}
        for item in result.item_results:
            if item.output is None or not item.trace_id:
                continue
            row = rows[item.output["case_id"]]
            metrics = required_scores(item.output)
            scores = [(score.name, score.value) for score in item.evaluations]
            row["sdk_scores"] = scores
            if item.trace_id == row.get("trace_id") and score_set_matches(metrics, scores):
                row["stage"] = "remote"
                expected[item.trace_id] = {"case_id": row["case_id"], "observation_id": row["observation_id"], "scores": metrics}
            else:
                row["stage"] = "evaluation_failed"
                row["failure_kind"] = "harness_failure"
            save(root / "receipt.json", receipt)
        receipt["stage"] = "remote"
        save(root / "receipt.json", receipt)
        client.flush()
        remote = verify(expected, result.experiment_id, started_at)
        receipt["remote"] = remote
        receipt["status"] = "confirmed" if len(expected) == len(selected) and len(remote["verified"]) == len(selected) and all(row.get("output", {}).get("assertion_passed") for row in rows.values()) else "incomplete"
        receipt["stage"] = "complete"
        save(root / "receipt.json", receipt)
        return receipt
    except Exception as error:
        receipt["failure_kind"] = "harness_failure"
        receipt["error_type"] = type(error).__name__
        save(root / "receipt.json", receipt)
        raise
    finally:
        if client is not None:
            client.shutdown()


def main() -> None:
    if not __debug__:
        raise RuntimeError("恢复测试禁止 Python 优化模式")
    load_dotenv(BACKEND / ".env", override=False)
    if os.environ.get("LANGFUSE_BASE_URL") and not os.environ.get("LANGFUSE_HOST"):
        os.environ["LANGFUSE_HOST"] = os.environ["LANGFUSE_BASE_URL"]
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("list", "offline", "experiment"):
        command = sub.add_parser(name)
        command.add_argument("--case", action="append", default=[])
    sub.add_parser("collect-child").add_argument("root", type=Path)
    worker = sub.add_parser("child")
    worker.add_argument("case_type")
    worker.add_argument("case_id")
    worker.add_argument("root", type=Path)
    args = parser.parse_args()
    if args.command == "child":
        return child(args.case_type, args.case_id, args.root)
    if args.command == "collect-child":
        return collect_child(args.root)
    root = ROOT / "runs" / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    manifest = collect(root / "collection")
    selected = [case for case in manifest["cases"] if not args.case or case["case_id"] in args.case]
    if args.case:
        assert set(args.case) == {case["case_id"] for case in selected}, "未知 case_id"
    if args.command == "list":
        print(json.dumps({"manifest": str(root / "collection" / "manifest.json"), **manifest}, ensure_ascii=False))
        return
    if args.command == "offline":
        results = [case_run(case, root / "cases" / digest(case["case_id"].encode())[:20]) for case in selected]
        save(root / "offline.json", results)
        print(json.dumps({"artifact": str(root / "offline.json"), "cases": len(results), "passed": sum(row["assertion_passed"] for row in results)}, ensure_ascii=False))
        if not all(row["assertion_passed"] for row in results):
            raise SystemExit(1)
        return
    assert len(selected) == 1, "实验每批须明确选择一个 case"
    receipt = experiment(manifest, root, selected)
    print(json.dumps({"artifact": str(root / "receipt.json"), "status": receipt["status"], "experiment_url": receipt["experiment_url"], "experiment_id": receipt["experiment_id"], "items": len(receipt["items"])}, ensure_ascii=False))
    if receipt["status"] != "confirmed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
