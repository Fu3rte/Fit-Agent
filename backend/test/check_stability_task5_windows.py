import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from uuid import uuid4

import httpx

BACKEND = "http://127.0.0.1:8000"
FRONTEND = "http://127.0.0.1:15173"
MODEL = "http://127.0.0.1:18081"


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


class Evidence:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.data: dict = {"phases": []}

    def phase(self, name: str, **values) -> None:
        item = {"phase": name, "at": now(), **values}
        self.data["phases"].append(item)
        # 完整证据只写文件；stdout 仅输出短行，避免调用方未消费管道时阻塞。
        print(json.dumps({"phase": name, "at": item["at"]}, ensure_ascii=False))
        self.write()

    def write(self) -> None:
        (self.directory / "evidence.json").write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def ps(script: Path, *args: str) -> subprocess.CompletedProcess:
    # PowerShell 子进程可能派生继承句柄的 WMI/conhost 子进程；不捕获管道，结果从 OutFile 读取。
    command = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(script), *args,
    ]
    return subprocess.run(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
    )


def snapshot(script: Path, root_pid: int, ports: list[int], out: Path) -> dict:
    completed = ps(
        script, "snapshot", "-RootPid", str(root_pid),
        "-Ports", ",".join(str(port) for port in ports), "-OutFile", str(out),
    )
    if completed.returncode != 0 or not out.exists():
        raise RuntimeError(f"snapshot 失败，退出码 {completed.returncode}")
    return json.loads(out.read_text(encoding="utf-8-sig"))


def stop_tree(script: Path, snapshot_file: Path, ports: list[int], out: Path) -> dict:
    completed = ps(
        script, "stop", "-SnapshotFile", str(snapshot_file),
        "-Ports", ",".join(str(port) for port in ports), "-OutFile", str(out),
    )
    if not out.exists():
        raise RuntimeError(f"stop 失败，退出码 {completed.returncode}")
    return json.loads(out.read_text(encoding="utf-8-sig"))


def wait_log_marker(path: Path, needle: str, timeout: float) -> float | None:
    """在启动日志里等待标记出现，返回 monotonic 时刻；超时返回 None。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and needle in path.read_text(encoding="utf-8", errors="replace"):
            return time.monotonic()
        time.sleep(0.2)
    return None


def port_listeners(port: int) -> int:
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command",
         f"@(Get-NetTCPConnection -State Listen -LocalPort {port} -ErrorAction SilentlyContinue).Count"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    return int((completed.stdout or "0").strip() or 0)


def http_client(timeout: float = 60.0) -> httpx.Client:
    # trust_env=False 避免本机代理变量给回环请求引入额外延迟。
    return httpx.Client(
        base_url=BACKEND, timeout=timeout, headers={"Host": "127.0.0.1:8000"},
        trust_env=False,
    )


def wait_backend(timeout: float = 150.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with http_client(timeout=2.0) as client:
                response = client.get("/api/sessions")
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError("后端未在时限内就绪")


def wait_frontend(port: int, timeout: float = 150.0) -> None:
    # Vite 监听 localhost 时可能只绑定 IPv6；同时探测两个协议栈。
    urls = [f"http://127.0.0.1:{port}", f"http://[::1]:{port}"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for url in urls:
            try:
                response = httpx.get(url, timeout=2.0, trust_env=False)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
        time.sleep(0.2)
    raise RuntimeError("前端未在时限内就绪")


def create_session(client: httpx.Client, title: str) -> str:
    session_id = str(uuid4())
    response = client.post("/api/sessions", json={"session_id": session_id, "title": title})
    assert response.status_code in {200, 201}, response.text
    return session_id


def configure_model(client: httpx.Client) -> None:
    response = client.put("/api/provider", json={
        "api": "openai-completions",
        "base_url": MODEL + "/v1",
        "model": "test-model",
        "api_key": "test-key",
        "provider": "local",
    })
    assert response.status_code == 200, response.text


def model_control(path: str, payload: dict | None = None) -> dict:
    if payload is None:
        response = httpx.get(MODEL + path, timeout=10.0, trust_env=False)
    else:
        response = httpx.post(MODEL + path, json=payload, timeout=10.0, trust_env=False)
    response.raise_for_status()
    return response.json()


def set_mode(mode: str, base_version=None) -> None:
    model_control("/control/mode", {"mode": mode, "base_version": base_version})


def read_sse(response, events: list[dict], holder: dict) -> None:
    holder["status"] = response.status_code
    if response.status_code != 200:
        holder["body"] = response.read().decode("utf-8", "replace")
        return
    holder["run_id"] = response.headers.get("X-Run-ID")
    holder["operation_id"] = response.headers.get("X-Operation-ID")
    event_name = None
    data_lines: list[str] = []
    for raw in response.iter_lines():
        line = raw.rstrip("\r")
        if line == "":
            if event_name is not None:
                payload = "\n".join(data_lines)
                try:
                    events.append({"event": event_name, "data": json.loads(payload)})
                except json.JSONDecodeError:
                    events.append({"event": event_name, "data": payload})
            event_name = None
            data_lines = []
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())


def run_stream(client: httpx.Client, session_id: str, text: str) -> tuple[dict, list[dict]]:
    events: list[dict] = []
    holder: dict = {}
    body = {"session_id": session_id, "operation_id": str(uuid4()), "request": text}
    with client.stream("POST", "/api/agent/run", json=body) as response:
        read_sse(response, events, holder)
    return holder, events


def start_run_thread(client: httpx.Client, session_id: str, text: str) -> tuple[threading.Thread, dict, list[dict]]:
    events: list[dict] = []
    holder: dict = {}
    body = {"session_id": session_id, "operation_id": str(uuid4()), "request": text}

    def target() -> None:
        try:
            with client.stream("POST", "/api/agent/run", json=body) as response:
                read_sse(response, events, holder)
        except Exception as error:  # 进程被终止时连接中断，保留实际异常类型。
            holder["error"] = type(error).__name__

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, holder, events


def wait_for(predicate, timeout: float, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except Exception as error:
            last = error
        time.sleep(interval)
    raise RuntimeError(f"等待条件超时，最后结果：{last!r}")


def db_rows(path: Path, sql: str, parameters: tuple = ()) -> list[tuple]:
    connection = sqlite3.connect(path, timeout=0.2, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        return [tuple(row) for row in connection.execute(sql, parameters).fetchall()]
    finally:
        connection.close()


def start_model_server(backend: Path, ready: Path, log: Path) -> subprocess.Popen:
    python = backend / ".venv" / "Scripts" / "python.exe"
    handle = log.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [str(python), "-m", "test.stability_task5_model_server",
         "--port", "18081", "--ready-file", str(ready)],
        cwd=str(backend), stdout=handle, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    process._log_handle = handle
    return process


def start_lock_helper(root: Path, db: Path, mode: str, ready: Path, release: Path | None,
                      hold_seconds: float, log: Path) -> subprocess.Popen:
    python = root / "backend" / ".venv" / "Scripts" / "python.exe"
    args = [
        str(python), str(root / "scripts" / "stability-task5" / "lock_helper.py"),
        "--db", str(db), "--mode", mode, "--hold-seconds", str(hold_seconds),
        "--ready-file", str(ready),
    ]
    if release is not None:
        args.extend(["--release-file", str(release)])
    handle = log.open("w", encoding="utf-8")
    process = subprocess.Popen(
        args, cwd=str(root), stdout=handle, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    process._log_handle = handle
    return process


def start_app(root: Path, env: dict, log: Path) -> subprocess.Popen:
    handle = log.open("w", encoding="utf-8")
    process = subprocess.Popen(
        ["cmd.exe", "/c", "npm run dev"], cwd=str(root), env=env,
        stdout=handle, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    process._log_handle = handle
    return process


def terminate(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
    handle = getattr(process, "_log_handle", None)
    if handle is not None:
        handle.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--frontend-port", type=int, default=15173)
    args = parser.parse_args()

    root = Path(args.root)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if root.name == "fit-agent-task5":
        # 只清理授权副本的独立数据；不触碰其他检出。
        shutil.rmtree(root / "backend" / "data", ignore_errors=True)
        shutil.rmtree(root / "backend" / "temp", ignore_errors=True)
        shutil.rmtree(root / "tmp" / "sessions", ignore_errors=True)
    evidence = Evidence(out)
    process_script = root / "scripts" / "stability-task5" / "process.ps1"
    db_path = root / "backend" / "data" / "fit-agent.db"
    ports = [8000, args.frontend_port]

    env = dict(os.environ)
    for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ):
        env.pop(name, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    env["FIT_AGENT_FRONTEND_PORT"] = str(args.frontend_port)
    env["FIT_AGENT_RUN_BUDGET_SECONDS"] = "20"
    # 诊断写入的重试窗口为 3 次 × 1 秒；SQLite 超时须短于该窗口，
    # 否则持锁期间被阻塞的终态写入会在锁释放后迟到成功，运行行不再保持 running。
    env["FIT_AGENT_SQLITE_TIMEOUT_SECONDS"] = "2"
    env["PYTHONUNBUFFERED"] = "1"
    (root / "backend" / ".env").write_text(
        "FIT_AGENT_RUN_BUDGET_SECONDS=20\nFIT_AGENT_SQLITE_TIMEOUT_SECONDS=2\n",
        encoding="utf-8",
    )

    model = None
    app = None
    lock_helpers: list[subprocess.Popen] = []
    try:
        for port in [8000, args.frontend_port, 18081]:
            if port_listeners(port):
                raise RuntimeError(f"端口 {port} 已被占用，停止验收")
        evidence.phase("preflight", ports_free=True, root=str(root), db=str(db_path))

        model = start_model_server(
            root / "backend", out / "model.ready", out / "model.log"
        )
        wait_for(lambda: (out / "model.ready").exists(), 20)
        evidence.phase("model_server", pid=model.pid, state=model_control("/control/state"))

        app = start_app(root, env, out / "app-first.log")
        start_snapshot_path = out / "process-start.json"
        wait_backend()
        wait_frontend(args.frontend_port)
        start_snapshot = snapshot(process_script, app.pid, ports, start_snapshot_path)
        evidence.phase(
            "app_started", root_pid=app.pid,
            process_count=len(start_snapshot["processes"]),
            listeners=start_snapshot["listeners"],
            processes=start_snapshot["processes"],
        )

        client = http_client()
        configure_model(client)
        # 首次模型调用包含客户端构造与系统提示装配；预热后不计入被验收的运行预算。
        set_mode("final")
        warmup_session = create_session(client, "任务五预热")
        warmup_started = time.monotonic()
        warmup_holder, warmup_events = run_stream(client, warmup_session, "预热")
        evidence.phase(
            "warmup", session=warmup_session, run_id=warmup_holder.get("run_id"),
            elapsed_s=round(time.monotonic() - warmup_started, 2),
            terminal=warmup_events[-1] if warmup_events else None,
        )
        baseline_session = create_session(client, "任务五基线")
        set_mode("tool_profile", base_version=None)
        holder, events = run_stream(client, baseline_session, "整理画像")
        (out / "baseline-events.json").write_text(
            json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tool_result = next(
            (item for item in events if item["event"] == "tool_result"), None
        )
        if tool_result is None:
            raise RuntimeError(
                "baseline 缺少 tool_result："
                + json.dumps(events[-12:], ensure_ascii=False)
            )
        proposal = json.loads(tool_result["data"]["content"])
        proposal_id = proposal["proposal_id"]
        pending = client.get(
            f"/api/sessions/{baseline_session}/history"
        ).json()["pending_proposals"]
        display_entry_id = next(
            item["display_entry_id"]
            for item in pending
            if item["business_kind"] == "profile"
            and item["proposal_id"] == proposal_id
        )
        confirmation_operation = str(uuid4())
        confirmed = client.post(
            f"/api/sessions/{baseline_session}/confirmations",
            json={
                "operation_id": confirmation_operation,
                "business_kind": "profile",
                "proposal_id": proposal_id,
                "display_entry_id": display_entry_id,
            },
        )
        assert confirmed.status_code == 200, confirmed.text
        profile_baseline = client.get("/api/profile").json()
        baseline_save_rows = db_rows(
            db_path, "SELECT COUNT(*) FROM profile_save_records WHERE proposal_id = ?", (proposal_id,)
        )[0][0]
        evidence.phase(
            "baseline_saved", session=baseline_session, run_id=holder.get("run_id"),
            proposal_id=proposal_id, display_entry_id=display_entry_id,
            confirmation_operation=confirmation_operation,
            profile_version=profile_baseline["version"],
            save_rows=baseline_save_rows,
        )

        set_mode("final")
        normal_session = create_session(client, "任务五正常运行")
        normal_holder, normal_events = run_stream(client, normal_session, "只回复完成。")
        normal_terminal = normal_events[-1] if normal_events else None
        evidence.phase(
            "normal_run_completed", session=normal_session,
            run_id=normal_holder.get("run_id"), terminal=normal_terminal,
        )

        fault_session = create_session(client, "任务五故障注入")
        set_mode("hold")
        fault_thread, fault_holder, fault_events = start_run_thread(
            client, fault_session, "阻塞保存"
        )
        wait_for(
            lambda: model_control("/control/state")["held"] >= 1, 30
        )
        lock_ready = out / "fault-lock.ready"
        lock_release = out / "fault-lock.release"
        lock = start_lock_helper(
            root, db_path, "exclusive", lock_ready, lock_release, 90,
            out / "fault-lock.log",
        )
        lock_helpers.append(lock)
        wait_for(lambda: lock_ready.exists(), 20)
        evidence.phase("fault_locked", lock_pid=lock.pid, run_id=fault_holder.get("run_id"))
        model_control("/control/release", {})
        fault_thread.join(60)
        # 诊断写入只有 3 次 1 秒的重试窗口；SSE 一关闭就释放锁，诊断才有落库机会。
        lock_release.write_text("release", encoding="utf-8")
        lock.wait(timeout=10)
        trajectory: list = []
        for index in range(60):
            rows = db_rows(
                db_path,
                "SELECT status, stop_diagnostic FROM session_runs WHERE id = ?",
                (fault_holder.get("run_id"),),
            )
            item = list(rows[0]) if rows else None
            if not trajectory or trajectory[-1][1] != item:
                trajectory.append([round(index * 0.25, 2), item])
            if item and item[1] in {"worker_stuck", "terminal_persist_failed"}:
                break
            time.sleep(0.25)
        fault_snapshot_path = out / "process-fault.json"
        fault_snapshot = snapshot(process_script, app.pid, ports, fault_snapshot_path)
        evidence.phase(
            "fault_closed",
            run_id=fault_holder.get("run_id"),
            stream_events=len(fault_events),
            has_terminal=any(item["event"] in {"done", "error"} for item in fault_events),
            trajectory=trajectory,
            process_count=len(fault_snapshot["processes"]),
        )

        # 故障期不新建会话：会话创建需要写库。使用故障前已存在的空闲会话命中 503 门。
        rejected = client.post(
            "/api/agent/run",
            json={"session_id": normal_session, "operation_id": str(uuid4()), "request": "拒绝"},
        )
        read_only = {}
        for name, path in (
            ("sessions", "/api/sessions"),
            ("stuck_run", f"/api/sessions/{fault_session}/runs/{fault_holder.get('run_id')}"),
            ("history", f"/api/sessions/{fault_session}/history"),
            ("operation", f"/api/sessions/{normal_session}/operations/{normal_holder.get('operation_id')}"),
            ("profile", "/api/profile"),
        ):
            try:
                read_only[name] = client.get(path).status_code
            except Exception as error:
                read_only[name] = type(error).__name__
        evidence.phase(
            "fault_admission",
            status=rejected.status_code,
            code=(rejected.json().get("detail") or {}).get("code"),
            read_only=read_only,
        )
        assert rejected.status_code == 503, rejected.status_code
        assert all(value == 200 for value in read_only.values()), read_only

        run_rows = db_rows(
            db_path,
            "SELECT status, stop_diagnostic FROM session_runs WHERE id = ?",
            (fault_holder.get("run_id"),),
        )
        runs_before = [
            list(row) for row in db_rows(
                db_path,
                "SELECT id, status, stop_reason, stop_diagnostic FROM session_runs ORDER BY started_at",
            )
        ]
        evidence.phase(
            "fault_persisted", run_id=fault_holder.get("run_id"),
            rows=run_rows, runs_before_restart=runs_before,
        )
        assert run_rows and run_rows[0][0] == "running", run_rows
        # SQLite 超时决定收尾阻塞形态：短超时得到 terminal_persist_failed，
        # 长超时得到 worker_stuck；两类都算故障封闭，诊断按实际落库值核对。
        assert run_rows[0][1] in {"worker_stuck", "terminal_persist_failed"}, run_rows
        fault_reason = run_rows[0][1]

        stop_result = stop_tree(
            process_script, fault_snapshot_path, ports, out / "process-worker-stuck-stop.json"
        )
        evidence.phase("fault_stopped", **stop_result)
        assert not stop_result["remaining_pids"], stop_result
        assert not stop_result["listeners"], stop_result
        terminate(app)
        app = None

        gate_ready = out / "startup-gate.ready"
        gate_release = out / "startup-gate.release"
        gate = start_lock_helper(
            root, db_path, "exclusive", gate_ready, gate_release, 90,
            out / "startup-gate.log",
        )
        lock_helpers.append(gate)
        wait_for(lambda: gate_ready.exists(), 20)
        # 重启实例改用 30 秒 SQLite 超时：恢复首笔读取在锁上等待而非立即报错，
        # 观察窗口才能落在「已启动、恢复未完成」这段时间上。
        (root / "backend" / ".env").write_text(
            "FIT_AGENT_RUN_BUDGET_SECONDS=20\nFIT_AGENT_SQLITE_TIMEOUT_SECONDS=30\n",
            encoding="utf-8",
        )
        env_restart = dict(env, FIT_AGENT_SQLITE_TIMEOUT_SECONDS="30")
        restart_log = out / "app-restart.log"
        app = start_app(root, env_restart, restart_log)
        marker_at = wait_log_marker(restart_log, "Waiting for application startup", 120)
        gate_observations: dict = {"probes": 0, "by_outcome": {}, "first": None, "last": None}
        observe_deadline = (marker_at if marker_at is not None else time.monotonic()) + 8.0
        while time.monotonic() < observe_deadline:
            started = time.monotonic()
            try:
                with http_client(timeout=1.0) as probe:
                    response = probe.get("/api/sessions")
                outcome = f"status_{response.status_code}"
            except Exception as error:
                outcome = type(error).__name__
            entry = {"elapsed_ms": round((time.monotonic() - started) * 1000), "outcome": outcome}
            gate_observations["probes"] += 1
            gate_observations["by_outcome"][outcome] = gate_observations["by_outcome"].get(outcome, 0) + 1
            if gate_observations["first"] is None:
                gate_observations["first"] = entry
            gate_observations["last"] = entry
            time.sleep(0.3)
        gate_listeners = port_listeners(8000)
        released_at = time.monotonic()
        gate_release.write_text("release", encoding="utf-8")
        gate.wait(timeout=20)
        wait_backend()
        admitted_after_release = round(time.monotonic() - released_at, 2)
        restart_snapshot = snapshot(
            process_script, app.pid, ports, out / "process-restart.json"
        )
        evidence.phase(
            "restart_admitted", root_pid=app.pid,
            startup_marker_found=marker_at is not None,
            gate_observations=gate_observations,
            listeners_during_gate=gate_listeners,
            admitted_after_release_s=admitted_after_release,
            process_count=len(restart_snapshot["processes"]),
            listeners=restart_snapshot["listeners"],
        )
        assert gate_listeners == 0, gate_observations
        assert all(
            not key.startswith("status_2") for key in gate_observations["by_outcome"]
        ), gate_observations

        recovered_run = client.get(
            f"/api/sessions/{fault_session}/runs/{fault_holder.get('run_id')}"
        ).json()
        recovered_run_db = db_rows(
            db_path,
            "SELECT status, stop_diagnostic FROM session_runs WHERE id = ?",
            (fault_holder.get("run_id"),),
        )
        recovered_confirmation = client.get(
            f"/api/sessions/{baseline_session}/confirmations/{confirmation_operation}"
        ).json()
        recovered_profile = client.get("/api/profile").json()
        recovered_history = client.get(
            f"/api/sessions/{fault_session}/history"
        ).json()
        recovered_conversation = client.get(
            f"/api/sessions/{baseline_session}/history"
        ).json()
        duplicate_rows = db_rows(
            db_path, "SELECT COUNT(*) FROM profile_save_records WHERE proposal_id = ?", (proposal_id,)
        )[0][0]
        runs_after = [
            list(row) for row in db_rows(
                db_path,
                "SELECT id, status, stop_reason, stop_diagnostic FROM session_runs ORDER BY started_at",
            )
        ]
        before_status = {row[0]: row[1] for row in runs_before}
        after_status = {row[0]: row[1] for row in runs_after}
        changed = [key for key in before_status if before_status[key] != after_status.get(key)]
        evidence.phase(
            "recovery_verified",
            fault_reason=fault_reason,
            run=recovered_run,
            run_db=recovered_run_db,
            confirmation_status=recovered_confirmation.get("status"),
            confirmation_result=recovered_confirmation.get("result"),
            profile=recovered_profile,
            history_run=[item for item in recovered_history.get("runs", []) if item["run_id"] == fault_holder.get("run_id")],
            history_confirmation=[
                item for item in recovered_conversation.get("confirmations", [])
                if item["operation_id"] == confirmation_operation
            ],
            duplicate_save_rows=duplicate_rows,
            runs_after_restart=runs_after,
            status_changed=changed,
        )
        assert recovered_run["status"] == "interrupted", recovered_run
        assert recovered_run_db and recovered_run_db[0][1] == fault_reason, recovered_run_db
        assert recovered_confirmation["status"] == "saved", recovered_confirmation
        assert duplicate_rows == 1, duplicate_rows
        # 停机前后对照：仅故障运行发生 running→interrupted，已提交终态保持原值，无新增行。
        assert set(before_status) == set(after_status), (before_status, after_status)
        assert changed == [fault_holder.get("run_id")], (changed, runs_after)
        assert all(
            value == "completed" for key, value in after_status.items()
            if key != fault_holder.get("run_id")
        ), after_status

        set_mode("final")
        after_session = create_session(client, "任务五恢复后运行")
        after_holder, after_events = run_stream(client, after_session, "恢复后回复。")
        evidence.phase(
            "post_recovery_run", session=after_session,
            run_id=after_holder.get("run_id"),
            terminal=after_events[-1] if after_events else None,
        )

        set_mode("tool_profile_v1", base_version=1)
        new_session = create_session(client, "任务五恢复后确认")
        new_holder, new_events = run_stream(client, new_session, "整理新画像")
        (out / "new-profile-events.json").write_text(
            json.dumps(new_events, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        new_result = next(
            (item for item in new_events if item["event"] == "tool_result"), None
        )
        if new_result is None:
            raise RuntimeError(
                "恢复后画像缺少 tool_result："
                + json.dumps(new_events[-12:], ensure_ascii=False)
            )
        new_proposal = json.loads(new_result["data"]["content"])
        new_pending = client.get(
            f"/api/sessions/{new_session}/history"
        ).json()["pending_proposals"]
        new_display = next(
            item["display_entry_id"]
            for item in new_pending
            if item["business_kind"] == "profile"
            and item["proposal_id"] == new_proposal["proposal_id"]
        )
        new_operation = str(uuid4())
        new_confirmed = client.post(
            f"/api/sessions/{new_session}/confirmations",
            json={
                "operation_id": new_operation,
                "business_kind": "profile",
                "proposal_id": new_proposal["proposal_id"],
                "display_entry_id": new_display,
            },
        )
        assert new_confirmed.status_code == 200, new_confirmed.text
        evidence.phase(
            "post_recovery_confirmation", session=new_session,
            operation_id=new_operation, status=new_confirmed.json()["status"],
            profile_version=new_confirmed.json()["result"]["version"],
        )

        # 中断确认场景：真实确认入口与大快照，在保存事务未提交时终止整个应用树。
        seed_out = out / "interrupted-seed.json"
        seeded = subprocess.run(
            [str(root / "backend" / ".venv" / "Scripts" / "python.exe"),
             "-m", "test.check_stability_task5_seed", "--db", str(db_path), "--out", str(seed_out)],
            cwd=str(root / "backend"), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
        evidence.phase(
            "interrupted_seed", returncode=seeded.returncode,
            stdout=seeded.stdout[-2000:], stderr=seeded.stderr[-2000:],
        )
        if seeded.returncode != 0 or not seed_out.exists():
            raise RuntimeError(f"中断确认种子准备失败，退出码 {seeded.returncode}")
        seed = json.loads(seed_out.read_text(encoding="utf-8"))
        interruption_snapshot_path = out / "process-interrupted-confirmation.json"
        snapshot(process_script, app.pid, ports, interruption_snapshot_path)
        interruption_operation = str(uuid4())
        confirmation_result: dict = {}

        def send_confirmation() -> None:
            try:
                with http_client(timeout=60.0) as local:
                    response = local.post(
                        f"/api/sessions/{seed['session']}/confirmations",
                        json={
                            "operation_id": interruption_operation,
                            "business_kind": "profile",
                            "proposal_id": seed["proposal"],
                            "display_entry_id": seed["display"],
                        },
                    )
                    confirmation_result["status"] = response.status_code
            except Exception as error:
                confirmation_result["error"] = type(error).__name__

        thread = threading.Thread(target=send_confirmation, daemon=True)
        thread.start()
        observed = None
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            try:
                rows = db_rows(
                    db_path,
                    "SELECT status FROM save_confirmation_operations WHERE operation_id = ?",
                    (interruption_operation,),
                )
            except sqlite3.Error:
                rows = []
            if rows:
                observed = rows[0][0]
                if observed in {"processing", "saved"}:
                    break
            time.sleep(0.005)
        stop_interrupted = stop_tree(
            process_script, interruption_snapshot_path, ports,
            out / "process-interrupted-confirmation-stop.json",
        )
        terminate(app)
        app = None
        evidence.phase(
            "interrupted_confirmation_crash",
            operation=interruption_operation, observed=observed,
            post=confirmation_result, stop=stop_interrupted,
        )
        # 事务未提交即终止进程树，才能核对恢复端的 confirmation_interrupted 与回滚语义。
        assert observed == "processing", observed
        app = start_app(root, env_restart, out / "app-after-interruption.log")
        wait_backend()
        recovered = client.get(
            f"/api/sessions/{seed['session']}/confirmations/{interruption_operation}"
        )
        snapshot_status = db_rows(
            db_path,
            "SELECT status, confirmation_entry_id FROM profile_snapshots WHERE proposal_id = ?",
            (seed["proposal"],),
        )
        save_rows = db_rows(
            db_path,
            "SELECT COUNT(*) FROM profile_save_records WHERE proposal_id = ?",
            (seed["proposal"],),
        )[0][0]
        evidence.phase(
            "interrupted_confirmation_recovered",
            status=recovered.status_code,
            operation=recovered.json() if recovered.status_code == 200 else recovered.text,
            snapshot=snapshot_status,
            save_rows=save_rows,
        )
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["status"] == "failed", recovered.text
        assert recovered.json()["error"]["code"] == "confirmation_interrupted", recovered.text
        assert snapshot_status and snapshot_status[0][0] == "pending", snapshot_status
        assert save_rows == 0, save_rows

        evidence.phase("completed", result="PASS")
    except Exception as error:
        evidence.phase("failed", error=type(error).__name__, message=str(error))
        raise
    finally:
        if app is not None:
            try:
                final_snapshot = snapshot(
                    process_script, app.pid, ports, out / "process-final.json"
                )
                stop_tree(process_script, out / "process-final.json", ports, out / "process-final-stop.json")
                evidence.phase("final_stop", process_count=len(final_snapshot["processes"]))
            except Exception as error:
                evidence.phase("final_stop_error", error=type(error).__name__, message=str(error))
            terminate(app)
        for helper in lock_helpers:
            terminate(helper)
        terminate(model)
        evidence.write()


if __name__ == "__main__":
    main()
