import argparse
import os
import re
import shutil
import sys
import time
from pathlib import Path
from uuid import uuid4

SQLITE_TIMEOUT_SECONDS = "30"
RUN_BUDGET_SECONDS = "20"
# 封闭点在运行预算到期 + 10 秒协作等待；诊断写入约在封闭后 3.1 秒放弃。
# 封闭后再持锁 25 秒才释放，保证运行行不会在核对窗口内被迟到写入改写。
RELEASE_AFTER_CLOSE_SECONDS = 25


def instance_counts(snapshot: dict, log_path: Path) -> dict:
    """单实例判据：唯一 spawn worker、其父进程持有 8000 监听、启动日志 PID 一致。

    命令行含 `uvicorn --reload` 的进程有 4 个（cmd 包装、uv.exe、venv 启动器、reloader 本体），
    按命令行计数 reloader 无法唯一化，因此以 socket 归属与父子关系判定。
    """
    workers = [
        (int(proc["pid"]), int(proc["ppid"]))
        for proc in snapshot["processes"]
        if "--multiprocessing-fork" in str(proc.get("command_line") or "")
    ]
    logged = re.findall(r"Started server process \[(\d+)\]",
                        log_path.read_text(encoding="utf-8", errors="replace"))
    listeners_8000 = [item for item in snapshot["listeners"] if item["local_port"] == 8000]
    return {
        "worker_pids": [pid for pid, _ in workers],
        "worker_parent_pids": [ppid for _, ppid in workers],
        "logged_worker_pids": logged,
        "listeners_8000": listeners_8000,
    }


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
        configure_model,
        create_session,
        db_rows,
        http_client,
        model_control,
        port_listeners,
        run_stream,
        set_mode,
        snapshot,
        start_app,
        start_lock_helper,
        start_model_server,
        start_run_thread,
        stop_tree,
        terminate,
        wait_backend,
        wait_for,
        wait_frontend,
        wait_log_marker,
    )

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    evidence = Evidence(out)
    process_script = root / "scripts" / "stability-task5" / "process.ps1"
    db_path = root / "backend" / "data" / "fit-agent.db"
    ports = [8000, args.frontend_port]

    shutil.rmtree(root / "backend" / "data", ignore_errors=True)
    shutil.rmtree(root / "backend" / "temp", ignore_errors=True)
    shutil.rmtree(root / "tmp" / "sessions", ignore_errors=True)
    (root / "backend" / ".env").write_text(
        f"FIT_AGENT_RUN_BUDGET_SECONDS={RUN_BUDGET_SECONDS}\n"
        f"FIT_AGENT_SQLITE_TIMEOUT_SECONDS={SQLITE_TIMEOUT_SECONDS}\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                 "http_proxy", "https_proxy", "all_proxy"):
        env.pop(name, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    env["FIT_AGENT_FRONTEND_PORT"] = str(args.frontend_port)
    env["FIT_AGENT_RUN_BUDGET_SECONDS"] = RUN_BUDGET_SECONDS
    env["FIT_AGENT_SQLITE_TIMEOUT_SECONDS"] = SQLITE_TIMEOUT_SECONDS
    env["PYTHONUNBUFFERED"] = "1"

    model = None
    app = None
    lock = None
    try:
        for port in [8000, args.frontend_port, 18081]:
            if port_listeners(port):
                raise RuntimeError(f"端口 {port} 已被占用，停止验收")
        evidence.phase("preflight", sqlite_timeout_s=SQLITE_TIMEOUT_SECONDS,
                       run_budget_s=RUN_BUDGET_SECONDS, root=str(root), db=str(db_path))

        model = start_model_server(root / "backend", out / "model.ready", out / "model.log")
        wait_for(lambda: (out / "model.ready").exists(), 20)
        app = start_app(root, env, out / "app.log")
        wait_backend()
        wait_frontend(args.frontend_port)
        client = http_client()
        configure_model(client)
        warm_session = create_session(client, "任务五worker_stuck预热")
        warm_holder, warm_events = run_stream(client, warm_session, "预热")
        evidence.phase(
            "app_started", root_pid=app.pid,
            terminal=warm_events[-1] if warm_events else None,
            run_id=warm_holder.get("run_id"),
        )

        fault_session = create_session(client, "任务五worker_stuck")
        set_mode("hold")
        fault_thread, fault_holder, fault_events = start_run_thread(
            client, fault_session, "阻塞收尾"
        )
        wait_for(lambda: model_control("/control/state")["held"] >= 1, 30)
        lock_ready = out / "fault-lock.ready"
        lock_release = out / "fault-lock.release"
        lock = start_lock_helper(root, db_path, "exclusive", lock_ready, lock_release, 180,
                                 out / "fault-lock.log")
        wait_for(lambda: lock_ready.exists(), 20)
        evidence.phase("fault_locked", lock_pid=lock.pid, run_id=fault_holder.get("run_id"))
        model_control("/control/release", {})
        fault_thread.join(120)
        snap = snapshot(process_script, app.pid, ports, out / "process-fault.json")
        evidence.phase(
            "fault_closed", run_id=fault_holder.get("run_id"),
            sse_thread_finished=not fault_thread.is_alive(),
            stream_events=len(fault_events),
            has_terminal=any(item["event"] in {"done", "error"} for item in fault_events),
            hold_after_close_s=RELEASE_AFTER_CLOSE_SECONDS,
            process_count=len(snap["processes"]),
        )

        # journal_mode 为 delete，读也会被排他锁挡住，因此持锁窗口内不读库；
        # 诊断写入每次只等 1 秒，持锁期间被判超时并在约 3.1 秒后放弃，落库值以实际读数为准。
        time.sleep(RELEASE_AFTER_CLOSE_SECONDS)
        lock_release.write_text("release", encoding="utf-8")
        lock.wait(timeout=15)
        rows: list = []
        trajectory: list = []
        for index in range(40):
            rows = db_rows(db_path,
                           "SELECT status, stop_diagnostic FROM session_runs WHERE id = ?",
                           (fault_holder.get("run_id"),))
            item = list(rows[0]) if rows else None
            if not trajectory or trajectory[-1][1] != item:
                trajectory.append([round(index * 0.25, 2), item])
            if item and item[1] == "worker_stuck":
                break
            time.sleep(0.25)
        # 诊断写入受 3 次 1 秒上限约束，持锁期间会被判超时并放弃，落库值按实际读数记录。
        log_text = (out / "app.log").read_text(encoding="utf-8", errors="replace")
        closure_log_hit = f"{fault_holder.get('run_id')}/worker_stuck" in log_text
        runs_before = [list(row) for row in db_rows(
            db_path,
            "SELECT id, status, stop_reason, stop_diagnostic FROM session_runs ORDER BY started_at",
        )]
        evidence.phase(
            "fault_persisted", rows=rows, trajectory=trajectory,
            closure_log_hit=closure_log_hit, runs_before_restart=runs_before,
        )
        assert rows and rows[0][0] == "running", rows
        assert closure_log_hit, "故障封闭原因未见应用日志"

        rejected = client.post(
            "/api/agent/run",
            json={"session_id": warm_session, "operation_id": str(uuid4()), "request": "拒绝"},
        )
        read_only = {}
        for name, path in (
            ("sessions", "/api/sessions"),
            ("stuck_run", f"/api/sessions/{fault_session}/runs/{fault_holder.get('run_id')}"),
            ("history", f"/api/sessions/{fault_session}/history"),
            ("profile", "/api/profile"),
        ):
            try:
                read_only[name] = client.get(path).status_code
            except Exception as error:
                read_only[name] = type(error).__name__
        evidence.phase(
            "fault_admission", status=rejected.status_code,
            code=(rejected.json().get("detail") or {}).get("code"), read_only=read_only,
        )
        assert rejected.status_code == 503, rejected.status_code
        assert all(value == 200 for value in read_only.values()), read_only

        stop = stop_tree(process_script, out / "process-fault.json", ports,
                         out / "process-fault-stop.json")
        evidence.phase("fault_stopped", **stop)
        assert not stop["remaining_pids"], stop
        assert not stop["listeners"], stop
        terminate(app)
        app = None

        restart_log = out / "app-restart.log"
        app = start_app(root, env, restart_log)
        marker_at = wait_log_marker(restart_log, "Waiting for application startup", 120)
        wait_backend()
        restart_snap = snapshot(process_script, app.pid, ports, out / "process-restart.json")
        recovered = client.get(
            f"/api/sessions/{fault_session}/runs/{fault_holder.get('run_id')}"
        ).json()
        runs_after = [list(row) for row in db_rows(
            db_path,
            "SELECT id, status, stop_reason, stop_diagnostic FROM session_runs ORDER BY started_at",
        )]
        evidence.phase(
            "recovery_verified", startup_marker_found=marker_at is not None,
            run=recovered, runs_after_restart=runs_after,
            process_count=len(restart_snap["processes"]),
            listeners=restart_snap["listeners"],
            listeners_during_restart=port_listeners(8000),
        )
        assert recovered["status"] == "interrupted", recovered
        before = {row[0]: row[1] for row in runs_before}
        after = {row[0]: row[1] for row in runs_after}
        assert set(before) == set(after), (before, after)
        assert [key for key in before if before[key] != after[key]] == [fault_holder.get("run_id")], (
            before, after
        )
        assert all(value == "completed" for key, value in after.items()
                   if key != fault_holder.get("run_id")), after

        set_mode("final")
        after_session = create_session(client, "任务五worker_stuck恢复后")
        after_holder, after_events = run_stream(client, after_session, "恢复后回复。")
        instances = instance_counts(restart_snap, out / "app-restart.log")
        evidence.phase(
            "post_recovery_run", session=after_session, run_id=after_holder.get("run_id"),
            terminal=after_events[-1] if after_events else None, instances=instances,
        )
        assert len(instances["worker_pids"]) == 1, instances
        assert [str(instances["worker_pids"][0])] == instances["logged_worker_pids"], instances
        assert len(instances["listeners_8000"]) == 1, instances
        assert (
            instances["listeners_8000"][0]["owning_process"] == instances["worker_parent_pids"][0]
        ), instances
        evidence.phase("completed", result="PASS")
    except Exception as error:
        evidence.phase("failed", error=type(error).__name__, message=str(error))
        raise
    finally:
        for helper in (lock,):
            terminate(helper)
        if app is not None:
            try:
                final_snap = snapshot(process_script, app.pid, ports, out / "process-final.json")
                stop_tree(process_script, out / "process-final.json", ports,
                          out / "process-final-stop.json")
                evidence.phase("final_stop", process_count=len(final_snap["processes"]))
            except Exception as error:
                evidence.phase("final_stop_error", error=type(error).__name__,
                               message=str(error))
            terminate(app)
        terminate(model)
        evidence.write()


if __name__ == "__main__":
    main()
