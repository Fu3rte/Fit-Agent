import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import signal
import sys
import time
from functools import partial
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND / "temp" / "recovery-langfuse"

import test.regression_support as regression_support

regression_support.TEMP_ROOT = ROOT / "fixture"

from app.agent import telemetry
from app.application.business.profile import business_date
from app.domain.business.models import SaveConfirmationRequest
from app.infrastructure.persistence.sqlite import database as database_module
from app.interfaces import http
from test.check_structured_confirmation import Fixture, PLAN, PROFILE, WORKOUT, now_ms

if not __debug__:
    raise RuntimeError("恢复测试禁止 Python 优化模式")

CASES = ("running-steering",) + tuple(f"{kind}-{phase}" for kind in ("profile", "plan", "workout") for phase in ("before_commit", "after_commit"))


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def configure(root: Path) -> None:
    from app import model_config

    database_module.default_database_path = lambda: root / "case.db"
    telemetry.trace_database_path = lambda: root / "trace.db"
    model_config.DATA_ROOT = root / "model-config"
    from app.agent.tools.files import create_file_tools
    from app.interfaces.http_api.routes import agent

    agent.create_file_tools = partial(create_file_tools, tmp_root=root / "attachments")
    os.environ["FIT_TRACE_CLOUD_ENABLED"] = "false"


async def integrity(database) -> None:
    async with database.transaction_scope():
        conn = database.connection
        foreign = await (await conn.execute("PRAGMA foreign_keys")).fetchone()
        violations = await (await conn.execute("PRAGMA foreign_key_check")).fetchall()
        result = await (await conn.execute("PRAGMA integrity_check")).fetchall()
        assert foreign[0] == 1
        assert violations == []
        assert [row[0] for row in result] == ["ok"]


def monitor_model(root: Path, entered: asyncio.Event | None = None) -> None:
    from app import model_config

    origin = urlsplit(model_config.load_model_config().base_url)
    original = httpx.AsyncClient.send

    async def monitored(client, request, *args, **kwargs):
        url = urlsplit(str(request.url))
        if (url.scheme, url.netloc) != (origin.scheme, origin.netloc):
            return await original(client, request, *args, **kwargs)
        path = root / "model-requests.json"
        count = json.loads(path.read_text(encoding="utf-8"))["count"] + 1
        write_json(path, {"count": count})
        response = await original(client, request, *args, **kwargs)
        if entered is not None:
            info = {"status_code": response.status_code}
            if not 200 <= response.status_code < 300:
                body = (await response.aread()).decode("utf-8", errors="replace").lower()
                info["diagnostic_flags"] = [term for term in ("model", "parameter", "stream_options", "tools", "tool", "unsupported", "invalid", "max_tokens", "quota", "authentication", "thinking", "temperature", "messages", "not exist", "billing", "balance", "api key", "request", "url", "region", "payment", "expired", "size", "long", "content", "json", "format") if term in body]
                payload = json.loads(body)
                info["error_keys"] = sorted(payload)
                error = payload.get("error")
                if isinstance(error, dict):
                    info["error_fields"] = sorted(error)
                    for key in ("code", "type"):
                        value = error.get(key)
                        if isinstance(value, str) and value.replace("_", "").isalnum() and len(value) < 40:
                            info[key] = value
                write_json(root / "model-response.json", info)
                raise RuntimeError("真实模型请求未成功")
            write_json(root / "model-response.json", info)
            entered.set()
            await asyncio.Event().wait()
        return response

    httpx.AsyncClient.send = monitored


async def running_worker(root: Path) -> None:
    from app import model_config

    with model_config._LOCK:
        source = model_config.DATA_ROOT
        assert not (source / model_config.JOURNAL_FILE).exists(), "模型配置提交未完成"
        paths = [source / model_config.MODELS_FILE, source / model_config.AUTH_FILE]
        # shortcut: 跨进程无世代号时无法保证完整 ABA，升级须由生产配置协议提供同步。
        def sample():
            stamps = [(s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns) for s in (path.stat() for path in paths)]
            contents = [path.read_bytes() for path in paths]
            assert not (source / model_config.JOURNAL_FILE).exists(), "模型配置正在变更"
            assert stamps == [(s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns) for s in (path.stat() for path in paths)]
            return stamps, contents
        first, second = sample(), sample()
        assert first == second, "模型配置正在变更"
        settings, auth = first[1]
        fingerprint = hashlib.sha256(settings + b"\0" + auth).hexdigest()
        stored = model_config.StoredModelSettings.model_validate_json(settings)
        key = model_config.StoredAuth.model_validate_json(auth).api_key
        assert key
        model = model_config.ModelConfig(**stored.model_dump(), api_key=key)
        assert model.api == "openai-completions"
        regression_support.install_test_model_config(root / "model-config", model=model)
    configure(root)
    model_identity = {"api": model.api, "provider": model.provider, "model": model.model, "endpoint_sha256": hashlib.sha256(model.base_url.encode()).hexdigest(), "source_fingerprint": fingerprint, "source_check": "journal_absent_identity_mtime_size_content_twice", "source_changed": False}
    write_json(root / "model-requests.json", {"count": 0})
    entered = asyncio.Event()
    monitor_model(root, entered)
    session, send_op, steering_op = (str(uuid4()) for _ in range(3))
    transport = httpx.ASGITransport(app=http.app)
    headers = {"Host": "127.0.0.1:8000", "Origin": "http://localhost:5173"}
    async with http.lifespan(http.app):
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000", headers=headers, timeout=120) as client:
            response = await client.post("/api/sessions", json={"session_id": session, "title": "恢复测试"})
            assert response.status_code == 201
            payload = {"session_id": session, "operation_id": send_op, "request": "请简短回复你好。"}
            request_task = asyncio.create_task(client.post("/api/agent/run", json=payload))
            entered_task = asyncio.create_task(entered.wait())
            done, _ = await asyncio.wait({entered_task, request_task}, timeout=100, return_when=asyncio.FIRST_COMPLETED)
            assert entered_task in done and entered.is_set(), "模型请求未到达屏障"
            assert not request_task.done()
            operation = await http.app.state.session_operations.get_operation(send_op)
            assert operation is not None
            run = await http.app.state.session_queries.get_run(session, operation.run_id)
            assert run.status == "running"
            response = await client.post(f"/api/agent/runs/{run.id}/steering", json={"session_id": session, "operation_id": steering_op, "message": "请保持简短。"})
            assert response.status_code == 200, response.status_code
            steering = response.json()
            assert steering["status"] == "accepted"
            pending = await http.app.state.session_queries.get_steering(session, steering["steering_id"])
            assert pending.status == "pending"
            entries = await http.app.state.session_queries.list_entries(session)
            identity = {"session_id": session, "send_operation_id": send_op, "steering_operation_id": steering_op, "run_id": run.id, "steering_id": pending.id, "entries": [entry.model_dump(mode="json") for entry in entries], "send_request": payload["request"], "model_identity": model_identity}
            write_json(root / "identity.json", identity)
            write_json(root / "ready.json", {"pid": os.getpid(), "parent_pid": os.getppid(), "phase": "running", "model_requests": json.loads((root / "model-requests.json").read_text(encoding="utf-8"))["count"]})
            await asyncio.Event().wait()


async def running_recover(root: Path) -> dict:
    from app.domain.session.models import SendCommand, SendRequest, SteeringCommand, SteeringRequest
    from test.check_session_store import system_message

    configure(root)
    monitor_model(root)
    identity = json.loads((root / "identity.json").read_text(encoding="utf-8"))
    before_requests = json.loads((root / "model-requests.json").read_text(encoding="utf-8"))["count"]
    assert before_requests >= 1
    observed = []
    for _ in range(2):
        async with http.lifespan(http.app):
            queries = http.app.state.session_queries
            service = http.app.state.session_service
            run = await queries.get_run(identity["session_id"], identity["run_id"])
            steering = await queries.get_steering(identity["session_id"], identity["steering_id"])
            entries = await queries.list_entries(identity["session_id"])
            assert run.status == "interrupted" and run.finished_at is None
            assert steering.status == "discarded" and steering.reason == "interrupted"
            assert [entry.model_dump(mode="json") for entry in entries] == identity["entries"]
            send = await service.accept_send(SendCommand(operation_id=identity["send_operation_id"], session_id=identity["session_id"], request=SendRequest(text=identity["send_request"])), system_message=system_message())
            assert not send.created and send.run.id == identity["run_id"]
            steer = await service.accept_steering(SteeringCommand(operation_id=identity["steering_operation_id"], session_id=identity["session_id"], request=SteeringRequest(target_run_id=identity["run_id"], text="请保持简短。")))
            assert not steer.created and steer.steering.id == identity["steering_id"]
            assert len(await queries.list_runs(identity["session_id"])) == 1
            await integrity(http.app.state.database)
            observed.append((run.model_dump(mode="json"), steering.model_dump(mode="json")))
    assert observed[0] == observed[1]
    after_requests = json.loads((root / "model-requests.json").read_text(encoding="utf-8"))["count"]
    assert before_requests == after_requests
    response_status = json.loads((root / "model-response.json").read_text(encoding="utf-8"))["status_code"]
    return {"case_id": "running-steering", "identity": {key: value for key, value in identity.items() if key.endswith("_id")}, "model_identity": identity["model_identity"], "kill_phase": "running", "model_requests_before": before_requests, "model_requests_after": after_requests, "model_response_status": response_status, "recovery_idempotent": 1, "lifespan_starts": 2}


async def worker(root: Path, kind: str, phase: str) -> None:
    fixture = await Fixture(root / "case.db").seeded()
    session = await fixture.session()
    if kind == "plan":
        profile = await fixture.stage(session, "profile", {"profile_id": 1, "base_profile_version": None, "payload": PROFILE}, "prepare_profile_update")
        result = await fixture.confirmations.confirm(session, await fixture.request(session, "profile", profile))
        assert result.status == "saved"
    if kind == "profile":
        ids = await fixture.stage(session, kind, {"profile_id": 1, "base_profile_version": None, "payload": PROFILE}, "prepare_profile_update")
    elif kind == "plan":
        ids = await fixture.stage(session, "plan_import", {"base_profile_version": 1, "base_plan_id": None, "payload": PLAN}, "prepare_plan_import")
    else:
        ids = await fixture.stage(session, kind, {"performed_on": business_date(now_ms()), "base_workout_id": None, "base_workout_version": None, "payload": WORKOUT}, "prepare_workout")
    request = await fixture.request(session, kind, ids)
    baseline = await fixture.state()
    baseline_rows = await business_rows(fixture.database)
    operation, accepted = await fixture.confirmations._accept(session, request, now_ms())
    assert accepted and operation.status == "processing"
    identity = {"ids": ids, "request": request.model_dump(), "confirmation_id": operation.confirmation_entry_id, "baseline": baseline, "baseline_rows": baseline_rows}
    write_json(root / "identity.json", identity)
    original_commit = fixture.database._commit
    commits = 0

    async def barrier(veto):
        nonlocal commits
        commits += 1
        if commits == 2 and phase == "before_commit":
            write_json(root / "ready.json", {"pid": os.getpid(), "parent_pid": os.getppid(), "phase": phase, "commits": commits})
            await asyncio.Event().wait()
        await original_commit(veto)
        if commits == 2 and phase == "after_commit":
            write_json(root / "ready.json", {"pid": os.getpid(), "parent_pid": os.getppid(), "phase": phase, "commits": commits})
            await asyncio.Event().wait()

    fixture.database._commit = barrier
    await fixture.confirmations._execute(operation, request, None)
    raise AssertionError("提交屏障未命中")


async def business_rows(database) -> dict:
    result = {}
    for table in ("profile", "plans", "workouts", "profile_save_records", "plan_save_records", "workout_save_records"):
        cursor = await database.connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
        columns = [column[0] for column in cursor.description]
        result[table] = [dict(zip(columns, row)) for row in await cursor.fetchall()]
        await cursor.close()
    return result


async def state(app, identity: dict, kind: str) -> dict:
    ids = identity["ids"]
    repository = app.state.business_repository
    snapshot = await {"profile": repository.get_snapshot, "plan": repository.get_plan_snapshot, "workout": repository.get_workout_snapshot}[kind](ids["proposal"])
    record = await app.state.confirmations._save_record(kind, ids["proposal"])
    operation = await app.state.confirmations.get(ids["session"], identity["request"]["operation_id"])
    tables = {"profile": "profile_save_records", "plan": "plan_save_records", "workout": "workout_save_records"}
    cursor = await app.state.database.connection.execute(f"SELECT count(*) FROM {tables[kind]} WHERE proposal_id = ?", (ids["proposal"],))
    count = (await cursor.fetchone())[0]
    await cursor.close()
    business = {}
    for label, table in (("profile", "profile"), ("profile_records", "profile_save_records"), ("plans", "plans"), ("plan_records", "plan_save_records"), ("workouts", "workouts"), ("workout_records", "workout_save_records")):
        cursor = await app.state.database.connection.execute(f"SELECT count(*) FROM {table}")
        business[label] = (await cursor.fetchone())[0]
        await cursor.close()
    await integrity(app.state.database)
    return {"snapshot": snapshot.model_dump(mode="json"), "operation": operation.model_dump(mode="json"), "record": None if record is None else record.model_dump(mode="json"), "count": count, "business": business, "rows": await business_rows(app.state.database)}


def assert_saved(current: dict, identity: dict, kind: str, result: dict, confirmation_id: str) -> None:
    ids = identity["ids"]
    assert result["proposal_id"] == ids["proposal"]
    assert current["snapshot"]["status"] == "saved"
    assert current["snapshot"]["confirmation_entry_id"] == confirmation_id
    assert current["record"]["confirmation_entry_id"] == confirmation_id
    assert current["record"]["result"] == result
    assert current["snapshot"]["payload"] == {"profile": PROFILE, "plan": PLAN, "workout": WORKOUT}[kind]
    table = {"profile": "profile", "plan": "plans", "workout": "workouts"}[kind]
    record_table = {"profile": "profile_save_records", "plan": "plan_save_records", "workout": "workout_save_records"}[kind]
    for name, rows in identity["baseline_rows"].items():
        assert current["rows"][name][:len(rows)] == rows
        assert len(current["rows"][name]) == len(rows) + (1 if name in (table, record_table) else 0)
    row = current["rows"][table][-1]
    assert json.loads(row["content"]) == current["snapshot"]["payload"] == result["content"]
    assert row["id"] == (result["profile_id"] if kind == "profile" else result["id"])
    if kind != "plan":
        assert row["version"] == result["version"] == 1
    if kind == "workout":
        assert row["performed_on"] == result["performed_on"] == current["snapshot"]["performed_on"]
    if kind == "plan":
        assert row["is_current"] == 1


async def recover(root: Path, kind: str, phase: str) -> dict:
    configure(root)
    identity = json.loads((root / "identity.json").read_text(encoding="utf-8"))
    request = SaveConfirmationRequest.model_validate(identity["request"])
    ids = identity["ids"]
    observed = []
    for _ in range(2):
        async with http.lifespan(http.app):
            current = await state(http.app, identity, kind)
            op = current["operation"]
            assert op["confirmation_entry_id"] == identity["confirmation_id"]
            assert current["snapshot"]["payload"] == {"profile": PROFILE, "plan": PLAN, "workout": WORKOUT}[kind]
            baseline = {key: value for key, value in identity["baseline"].items() if key != "operations"}
            if phase == "before_commit":
                assert current["business"] == baseline
                assert current["rows"] == identity["baseline_rows"]
                assert current["snapshot"]["status"] == "pending"
                assert current["snapshot"]["confirmation_entry_id"] is None
                assert current["record"] is None and current["count"] == 0
                assert op["status"] == "failed" and op["error"]["code"] == "confirmation_interrupted"
            else:
                assert current["snapshot"]["status"] == "saved"
                assert current["record"] is not None and current["count"] == 1
                assert op["status"] == "saved" and op["result"] == current["record"]["result"]
                business_key, record_key = {"profile": ("profile", "profile_records"), "plan": ("plans", "plan_records"), "workout": ("workouts", "workout_records")}[kind]
                baseline[business_key] += 1
                baseline[record_key] += 1
                assert current["business"] == baseline
                assert_saved(current, identity, kind, op["result"], identity["confirmation_id"])
            replay = await http.app.state.confirmations.confirm(ids["session"], request)
            assert replay.model_dump(mode="json") == op
            assert await state(http.app, identity, kind) == current
            observed.append(current)
    assert observed[0] == observed[1]
    if phase == "before_commit":
        async with http.lifespan(http.app):
            fresh = request.model_copy(update={"operation_id": str(uuid4())})
            saved = await http.app.state.confirmations.confirm(ids["session"], fresh)
            assert saved.status == "saved"
            assert saved.result.id if kind == "plan" else saved.result.version == 1
            after = await state(http.app, identity, kind)
            assert after["count"] == 1
            assert_saved(after, identity, kind, saved.result.model_dump(mode="json"), saved.confirmation_entry_id)
            assert (await http.app.state.confirmations.confirm(ids["session"], request)).model_dump(mode="json") == observed[0]["operation"]
    return {"case_id": f"{kind}-{phase}", "identity": {"session_id": ids["session"], "proposal_id": ids["proposal"], "operation_id": request.operation_id}, "kill_phase": phase, "recovery_idempotent": 1, "save_exactly_once": 1 if phase == "after_commit" else None, "lifespan_starts": 3 if phase == "before_commit" else 2}


def terminate_owned(process: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=True, capture_output=True, timeout=15)
    else:
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=15)


def _run_case(case_id: str, root: Path) -> dict:
    phase = "running" if case_id == "running-steering" else case_id.split("-", 1)[1]
    root.mkdir(parents=True, exist_ok=False)
    with (root / "worker.log").open("w", encoding="utf-8") as output:
        process = subprocess.Popen([sys.executable, "-m", "test.check_recovery_cases", "worker", case_id, str(root)], cwd=BACKEND, stdout=output, stderr=subprocess.STDOUT, start_new_session=os.name != "nt")
        try:
            deadline = time.monotonic() + 120
            while not (root / "ready.json").exists():
                assert process.poll() is None, "目标进程提前退出，详见 worker.log"
                assert time.monotonic() < deadline, "等待提交屏障超时"
                time.sleep(0.05)
            marker = json.loads((root / "ready.json").read_text(encoding="utf-8"))
            assert marker["pid"] == process.pid or marker["parent_pid"] == process.pid
            assert marker["phase"] == phase
            assert marker["model_requests"] >= 1 if phase == "running" else marker["commits"] == 2
            terminate_owned(process)
        finally:
            if process.poll() is None:
                terminate_owned(process)
    result = asyncio.run(running_recover(root)) if phase == "running" else asyncio.run(recover(root, case_id.split("-", 1)[0], phase))
    result["target_pid"] = marker["pid"]
    result["owned_root_pid"] = process.pid
    result["target_exitcode"] = process.returncode
    write_json(root / "result.json", result)
    return result


def run_case(case_id: str, root: Path) -> dict:
    from app import model_config

    original_root = model_config.DATA_ROOT
    original_send = httpx.AsyncClient.send
    try:
        return _run_case(case_id, root)
    finally:
        model_config.DATA_ROOT = original_root
        httpx.AsyncClient.send = original_send
        if case_id == "running-steering":
            for name in ("auth.json", "auth.json.tmp", "provider.journal", "provider.journal.tmp"):
                (root / "model-config" / name).unlink(missing_ok=True)


def main() -> None:
    load_dotenv(BACKEND / ".env", override=False)
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("list", "run", "worker"))
    parser.add_argument("case_id", nargs="?", choices=CASES)
    parser.add_argument("root", nargs="?", type=Path)
    args = parser.parse_args()
    if args.command == "list":
        print("\n".join(CASES))
        return
    assert args.case_id and args.root
    root = args.root.resolve()
    assert root.is_relative_to(ROOT.resolve())
    if args.command == "worker":
        if args.case_id == "running-steering":
            asyncio.run(running_worker(root))
        else:
            kind, phase = args.case_id.split("-", 1)
            asyncio.run(worker(root, kind, phase))
    else:
        print(json.dumps(run_case(args.case_id, root), ensure_ascii=False))


if __name__ == "__main__":
    main()
