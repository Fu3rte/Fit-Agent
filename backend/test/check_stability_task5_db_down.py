import argparse
import json
import os
import subprocess
import time
from pathlib import Path

import httpx

BACKEND = "http://127.0.0.1:8000"
FRONTEND = "http://127.0.0.1:15173"


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def run_ps(script: Path, *args: str) -> subprocess.CompletedProcess:
    command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args]
    return subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)


def listener_count(port: int) -> int:
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command",
         f"@(Get-NetTCPConnection -State Listen -LocalPort {port} -ErrorAction SilentlyContinue).Count"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    return int((completed.stdout or "0").strip() or 0)


def snapshot(script: Path, root_pid: int, out: Path) -> dict:
    completed = run_ps(script, "snapshot", "-RootPid", str(root_pid), "-Ports", "8000,15173",
                       "-OutFile", str(out))
    if completed.returncode != 0 or not out.exists():
        raise RuntimeError(f"snapshot 失败，退出码 {completed.returncode}")
    return json.loads(out.read_text(encoding="utf-8-sig"))


def stop_tree(script: Path, snapshot_file: Path, out: Path) -> dict:
    completed = run_ps(script, "stop", "-SnapshotFile", str(snapshot_file), "-Ports", "8000,15173",
                       "-OutFile", str(out))
    if not out.exists():
        raise RuntimeError(f"stop 失败，退出码 {completed.returncode}")
    return json.loads(out.read_text(encoding="utf-8-sig"))


def start_app(root: Path, log: Path) -> subprocess.Popen:
    env = dict(os.environ)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(name, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    env["FIT_AGENT_FRONTEND_PORT"] = "15173"
    handle = log.open("w", encoding="utf-8")
    process = subprocess.Popen(
        ["cmd.exe", "/c", "npm run dev"], cwd=str(root), env=env,
        stdout=handle, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    process._log_handle = handle
    return process


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    root = Path(args.root)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if root.name != "fit-agent-task5":
        raise RuntimeError("只允许在任务五独占副本中执行")
    process_script = root / "scripts" / "stability-task5" / "process.ps1"
    data = root / "backend" / "data"
    held = root / "backend" / "data-held"
    if held.exists():
        raise RuntimeError("存在残留的 data-held，先人工确认")
    if not (data / "fit-agent.db").exists():
        raise RuntimeError("数据库不存在，无法构造路径故障")

    record: dict = {"started_at": now(), "phases": []}

    def phase(name: str, **values) -> None:
        item = {"phase": name, "at": now(), **values}
        record["phases"].append(item)
        print(json.dumps({"phase": name, **{k: v for k, v in item.items() if k not in {"phases"}}},
                         ensure_ascii=False)[:2000])

    for port in (8000, 15173):
        if listener_count(port):
            raise RuntimeError(f"端口 {port} 已被占用，停止验收")
    phase("preflight", ports_free=True)

    app = None
    try:
        data.rename(held)
        (data).write_text("fault", encoding="utf-8")
        phase("path_fault", data_is_file=True, restored_dir=str(held))

        app = start_app(root, out / "app-db-down.log")
        time.sleep(25.0)
        probes = []
        for _ in range(3):
            started = time.monotonic()
            try:
                with httpx.Client(base_url=BACKEND, headers={"Host": "127.0.0.1:8000"},
                                  timeout=1.5, trust_env=False) as client:
                    response = client.get("/api/sessions")
                probes.append({"elapsed_ms": round((time.monotonic() - started) * 1000),
                               "status": response.status_code})
            except Exception as error:
                probes.append({"elapsed_ms": round((time.monotonic() - started) * 1000),
                               "error": type(error).__name__})
            time.sleep(0.5)
        frontend = []
        for url in (FRONTEND, "http://[::1]:15173"):
            try:
                response = httpx.get(url, timeout=2.0, trust_env=False)
                frontend.append({"url": url, "status": response.status_code})
            except Exception as error:
                frontend.append({"url": url, "error": type(error).__name__})
        snap = snapshot(process_script, app.pid, out / "process-db-down.json")
        phase(
            "startup_attempt",
            root_pid=app.pid,
            http_probes=probes,
            backend_listener=listener_count(8000),
            frontend_probe=frontend,
            process_count=len(snap["processes"]),
            listeners=snap["listeners"],
            app_returncode=app.poll(),
        )

        stop = stop_tree(process_script, out / "process-db-down.json", out / "process-db-down-stop.json")
        phase("stopped", remaining_pids=stop["remaining_pids"], listeners=stop["listeners"])
        assert not stop["remaining_pids"], stop
        assert not stop["listeners"], stop
    finally:
        if app is not None:
            try:
                app.terminate()
                app.wait(timeout=5)
            except Exception:
                app.kill()
            handle = getattr(app, "_log_handle", None)
            if handle is not None:
                handle.close()
        if (data).is_file():
            (data).unlink()
        if held.exists():
            held.rename(data)
        phase("path_restored", db_present=(data / "fit-agent.db").exists(),
               held_present=held.exists())

    if not (data / "fit-agent.db").exists() or held.exists():
        raise RuntimeError("数据目录未恢复到可用状态")
    record["finished_at"] = now()
    (out / "db-down.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"phase": "completed", "result": "PASS"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
