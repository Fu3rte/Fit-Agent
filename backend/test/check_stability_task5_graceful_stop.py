import argparse
import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def attach_console() -> dict:
    """无控制台宿主里显式分配控制台，使 Ctrl+Break 事件能送达子进程组。"""
    kernel32 = ctypes.windll.kernel32
    before = kernel32.GetConsoleWindow()
    kernel32.FreeConsole()
    kernel32.AllocConsole()
    after = kernel32.GetConsoleWindow()
    if after:
        ctypes.windll.user32.ShowWindow(after, 0)
    return {"console_before": int(before or 0), "console_after": int(after or 0)}


def alive_set(pids: list[int]) -> set[int]:
    """一次 CIM 查询取回全部存活 PID，避免逐 PID 派生 PowerShell。"""
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", "(Get-CimInstance Win32_Process).ProcessId"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    existing = {int(line) for line in completed.stdout.split() if line.strip().isdigit()}
    return {pid for pid in pids if pid in existing}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--frontend-port", type=int, default=15173)
    args = parser.parse_args()

    root = Path(args.root)
    if root.name != "fit-agent-task5":
        raise RuntimeError("只允许在任务五独占副本中执行")
    sys.path.insert(0, str(root / "backend"))

    from test.check_stability_task5_windows import (  # noqa: E402
        Evidence,
        port_listeners,
        snapshot,
        start_app,
        stop_tree,
        terminate,
        wait_backend,
        wait_frontend,
    )

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    evidence = Evidence(out)
    process_script = root / "scripts" / "stability-task5" / "process.ps1"
    ports = [8000, args.frontend_port]

    env = dict(os.environ)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                 "http_proxy", "https_proxy", "all_proxy"):
        env.pop(name, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    env["FIT_AGENT_FRONTEND_PORT"] = str(args.frontend_port)
    env["PYTHONUNBUFFERED"] = "1"

    app = None
    try:
        for port in ports:
            if port_listeners(port):
                raise RuntimeError(f"端口 {port} 已被占用，停止验收")
        console = attach_console()
        evidence.phase("console", **console)
        if not console["console_after"]:
            raise RuntimeError("AllocConsole 未取得控制台，Ctrl+Break 无送达通道")
        app = start_app(root, env, out / "app-graceful.log")
        wait_backend()
        wait_frontend(args.frontend_port)
        snap = snapshot(process_script, app.pid, ports, out / "process-graceful.json")
        recorded = [int(proc["pid"]) for proc in snap["processes"]]
        evidence.phase(
            "app_started", root_pid=app.pid, process_count=len(recorded),
            processes=snap["processes"], listeners=snap["listeners"],
        )

        # CREATE_NEW_PROCESS_GROUP 下用 CTRL_BREAK_EVENT 触发 uvicorn 的 SIGBREAK 收尾路径。
        os.kill(app.pid, signal.CTRL_BREAK_EVENT)
        started = time.monotonic()
        exit_times: dict = {}
        while time.monotonic() - started < 90.0:
            living = alive_set(recorded)
            for pid in recorded:
                if pid not in living and pid not in exit_times:
                    exit_times[pid] = round(time.monotonic() - started, 2)
            if len(exit_times) == len(recorded):
                break
            time.sleep(0.5)
        survivors = [pid for pid in recorded if pid not in exit_times]
        evidence.phase(
            "graceful_stop", ctrl_break_sent=True, elapsed_s=round(time.monotonic() - started, 2),
            exit_times_s=exit_times, survivors=survivors,
            listeners_after=port_listeners(8000) + port_listeners(args.frontend_port),
        )
        assert not survivors, survivors
        evidence.phase("root_exit", returncode=app.wait(timeout=30))
        evidence.phase("completed", result="PASS")
    except Exception as error:
        evidence.phase("failed", error=type(error).__name__, message=str(error))
        raise
    finally:
        # 优雅退出未收敛时按快照逐 PID 核验后终止，属清理责任而非通过判据。
        if app is not None and app.poll() is None:
            stop = stop_tree(process_script, out / "process-graceful.json", ports,
                             out / "process-graceful-force-stop.json")
            evidence.phase("force_cleanup", **stop)
            terminate(app)
        evidence.write()


if __name__ == "__main__":
    main()
