"""Stage 6 F6-09b：重启恢复与查询恢复真实进程验证（无真实模型、无真实 Key）。

在**真实 uvicorn 进程**上复现 08 8.4 启动恢复：lifespan 启动时把遗留 ``pending``/
``running`` Run 标 ``failed`` + ``interrupted_by_restart``（``api/app.py``）。本脚本
不在 HTTP 侧造 running Run（提交请求会走真实模型路径）——按任务授权，用 **sqlite 夹具
插入**一条 ``status='running'`` 假 Run（非业务端点，报告中标注「夹具插入」）。

流程：

1. 临时 ``data_dir``；spawn uvicorn ``--host 127.0.0.1``（子进程环境剥离 ``MODEL_*``）。
2. 经 ``POST /api/sessions`` 建会话；sqlite **只读**确认库路径后，夹具插入 running Run。
3. kill uvicorn 进程。
4. **同一 data_dir** 再 spawn uvicorn。
5. ``GET /api/runs/{id}`` → ``status=failed`` 且 ``error_code=interrupted_by_restart``。
6. ``GET /api/sessions/{id}`` → 会话可读；runs 列表含该 Run 与 interrupted 码。
7. 无 SSE 重放：查询恢复不订阅 events 即可（步骤 5/6）；另 **只读一次** events 确认
   无业务事件轰炸（终态 Run 应只发一帧 status 后立即结束流）。
8. 账本/档案/草稿：只读探测，缺失或空则记 ``N/A``。
9. 两次 spawn 的 stdout/stderr：无假 Key、无 ``MODEL_API_KEY`` 值。

安全边界：仅 127.0.0.1；无真实模型调用（不提交业务请求、不 PUT Key）；子进程环境
剔除 ``MODEL_*``/``DEEPSEEK_*``；报告不打印任何凭据值。

用法（在 ``backend/`` 下）：

    .venv/Scripts/python.exe scripts/f609_restart_recovery.py
    .venv/Scripts/python.exe scripts/f609_restart_recovery.py --data-dir %TEMP%/fit-agent-f609

退出码：全检查通过 = 0；任一 FAIL = 1。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

#: 夹具遗留 Run 的稳定身份（仅存在于临时 data_dir，不与真实业务冲突）。
FIXTURE_RUN_ID = "f609-leftover-run"
FIXTURE_REQUEST_ID = "f609-fixture-request"
INTERRUPTED_CODE = "interrupted_by_restart"


def log(message: str) -> None:
    print(message, flush=True)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def child_env(data_dir: Path) -> dict[str, str]:
    """子进程环境：剥离凭据环境变量；仅绑定临时数据目录。"""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("MODEL_", "DEEPSEEK_"))
    }
    env["FIT_AGENT_DATA_DIR"] = str(data_dir)
    env["PYTHONPATH"] = str(BACKEND)
    return env


class UvicornProcess:
    """一次性的真实 uvicorn 进程：spawn、healthz 等待、硬终止并回收日志。"""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "api.app:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--log-level",
                "warning",
            ],
            cwd=BACKEND,
            env=child_env(self.data_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        client = httpx2.Client(base_url=self.base, timeout=5.0)
        deadline = time.time() + 20
        try:
            while time.time() < deadline:
                if self.proc.poll() is not None:
                    raise RuntimeError(
                        f"uvicorn 启动即退出（exit={self.proc.returncode}）"
                    )
                try:
                    if client.get("/healthz").status_code == 200:
                        return
                except httpx2.HTTPError:
                    time.sleep(0.1)
        finally:
            client.close()
        raise RuntimeError("uvicorn 未在 20 秒内就绪（healthz 超时）")

    def kill(self) -> str:
        """硬终止进程（模拟崩溃；Windows terminate 即强制结束），返回已捕获日志。"""
        if self.proc is None:
            return ""
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        stdout = self.proc.stdout
        data = stdout.read() if stdout is not None else b""
        return data.decode("utf-8", errors="replace")


def confirm_db_readonly(db_path: Path) -> dict[str, Any]:
    """只读确认库路径存在且含 runs/conversations 表（不写任何内容）。"""
    if not db_path.exists():
        return {"exists": False, "runs_table": False}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name IN ('runs', 'conversations', 'run_events')"
        ).fetchall()
    finally:
        con.close()
    names = {str(row[0]) for row in rows}
    return {
        "exists": True,
        "runs_table": "runs" in names,
        "conversations_table": "conversations" in names,
        "run_events_table": "run_events" in names,
    }


def fixture_insert_running_run(db_path: Path, conversation_id: str) -> None:
    """夹具插入：最小合法 running Run 行（仅测试夹具，非业务端点）。

    列约束来自 001 迁移 + 015（``kind DEFAULT 'chat'``）：id/conversation_id/
    client_request_id/status/created_at/updated_at 非空；error_code/retry_of 可空。
    """
    now = datetime.now(UTC).isoformat()
    con = sqlite3.connect(db_path, timeout=10)
    try:
        con.execute("PRAGMA busy_timeout = 10000")
        con.execute(
            "INSERT INTO runs (id, conversation_id, client_request_id, status,"
            " error_code, retry_of_run_id, kind, created_at, updated_at)"
            " VALUES (?, ?, ?, 'running', NULL, NULL, 'chat', ?, ?)",
            (FIXTURE_RUN_ID, conversation_id, FIXTURE_REQUEST_ID, now, now),
        )
        con.commit()
    finally:
        con.close()


def read_fixture_run_readonly(db_path: Path) -> dict[str, Any] | None:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        row = con.execute(
            "SELECT id, status, error_code FROM runs WHERE id = ?",
            (FIXTURE_RUN_ID,),
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return None
    return {"id": row[0], "status": row[1], "error_code": row[2]}


def read_interrupted_event_count(db_path: Path) -> int | None:
    """只读：该 Run 的 failed 轨迹事件中 payload 含 interrupted 码的条数。"""
    if not db_path.exists():
        return None
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        row = con.execute(
            "SELECT COUNT(*) FROM run_events"
            " WHERE run_id = ? AND event_type = 'failed' AND payload_json LIKE ?",
            (FIXTURE_RUN_ID, f"%{INTERRUPTED_CODE}%"),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()
    return int(row[0]) if row is not None else None


def probe_ledger_drafts(db_path: Path) -> dict[str, Any]:
    """账本/档案/草稿只读探测：表缺失或空行记 N/A。"""
    result: dict[str, Any] = {}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        for label, table in (
            ("fee_ledger", "fee_ledger"),
            ("business_drafts", "business_drafts"),
            ("profile_baseline", "profile_baselines"),
        ):
            try:
                row = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608 - 表名为脚本内常量
                count = int(row[0]) if row else 0
            except sqlite3.OperationalError:
                result[label] = {"status": "N/A", "reason": "table_missing"}
                continue
            result[label] = (
                {"status": "N/A", "reason": "empty", "row_count": 0}
                if count == 0
                else {"status": "present", "row_count": count}
            )
    finally:
        con.close()
    return result


def collect_events_once(client: httpx2.Client, run_id: str) -> dict[str, Any]:
    """只读一次 SSE：终态 Run 应只发 status 帧后立即结束，无业务事件轰炸。"""
    frames: list[tuple[str, str]] = []
    name = ""
    started = time.time()
    with client.stream("GET", f"/api/runs/{run_id}/events", timeout=20.0) as stream:
        for line in stream.iter_lines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                frames.append((name, line[len("data: ") :]))
            if len(frames) > 50:
                break
    elapsed = round(time.time() - started, 3)
    kinds: dict[str, int] = {}
    for kind, _data in frames:
        kinds[kind] = kinds.get(kind, 0) + 1
    return {
        "frame_count": len(frames),
        "event_kinds": kinds,
        "elapsed_seconds": elapsed,
        "frames": frames,
    }


def scan_logs_for_secrets(log_text: str) -> dict[str, Any]:
    """日志秘密扫描：无 MODEL_API_KEY 值；父进程 Key 值（若有）不得出现在日志。"""
    parent_key = os.environ.get("MODEL_API_KEY", "")
    findings = {
        "model_api_key_name_in_logs": "MODEL_API_KEY" in log_text,
        "parent_key_value_in_logs": bool(parent_key.strip())
        and parent_key.strip() in log_text,
        "sk_style_token_in_logs": "sk-" in log_text,
    }
    findings["clean"] = not any(findings.values())
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="F6-09b 重启恢复真实进程验证")
    parser.add_argument(
        "--data-dir", default=None, help="可选临时数据目录（缺省自建并清理）"
    )
    args = parser.parse_args(argv)

    owned_tmp: str | None = None
    if args.data_dir:
        data_dir = Path(args.data_dir).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
    else:
        owned_tmp = tempfile.mkdtemp(prefix="fit-agent-f609-")
        data_dir = Path(owned_tmp)
    db_path = data_dir / "app.db"

    checklist: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "task": "F6-09b",
        "data_dir": str(data_dir),
        "fixture_insert": True,
        "fixture_run_id": FIXTURE_RUN_ID,
        "model_calls": False,
        "api_keys_used": False,
    }
    log_parts: list[str] = []

    def check(name: str, ok: bool, detail: Any = None) -> bool:
        entry: dict[str, Any] = {"check": name, "result": "PASS" if ok else "FAIL"}
        if detail is not None:
            entry["detail"] = detail
        checklist.append(entry)
        return ok

    try:
        # ---------- 阶段 1：首次 spawn + 建会话 + 夹具插入 ----------
        first = UvicornProcess(data_dir)
        first.start()
        client = httpx2.Client(base_url=first.base, timeout=15.0)
        all_ok = True

        created = client.post("/api/sessions", json={})
        session_id = (
            created.json().get("session_id") if created.status_code == 200 else None
        )
        all_ok &= check(
            "session_created_via_http",
            created.status_code == 200 and bool(session_id),
            {"http_status": created.status_code, "session_id": session_id},
        )

        readonly = confirm_db_readonly(db_path)
        all_ok &= check(
            "db_path_readonly_confirmed",
            readonly.get("exists") and readonly.get("runs_table"),
            {"db_path": str(db_path), **readonly},
        )

        if session_id:
            fixture_insert_running_run(db_path, str(session_id))
            pre_kill = read_fixture_run_readonly(db_path)
            all_ok &= check(
                "fixture_running_run_inserted",
                bool(pre_kill)
                and pre_kill.get("status") == "running"
                and pre_kill.get("error_code") is None,
                {"note": "夹具插入（非业务端点）", "row": pre_kill},
            )
        else:
            all_ok &= check(
                "fixture_running_run_inserted",
                False,
                {"note": "无 session_id，跳过夹具插入"},
            )

        client.close()
        log_parts.append(first.kill())
        all_ok &= check("uvicorn_first_killed", first.proc is not None and first.proc.returncode is not None)

        # ---------- 阶段 2：同一 data_dir 再 spawn + 查询恢复 ----------
        second = UvicornProcess(data_dir)
        second.start()
        client2 = httpx2.Client(base_url=second.base, timeout=15.0)

        run_resp = client2.get(f"/api/runs/{FIXTURE_RUN_ID}")
        run_body = (
            run_resp.json().get("run") if run_resp.status_code == 200 else None
        )
        all_ok &= check(
            "run_failed_interrupted_after_restart",
            run_resp.status_code == 200
            and isinstance(run_body, dict)
            and run_body.get("status") == "failed"
            and run_body.get("error_code") == INTERRUPTED_CODE,
            {"http_status": run_resp.status_code, "run": run_body},
        )

        sess_resp = client2.get(f"/api/sessions/{session_id}")
        sess_body = sess_resp.json() if sess_resp.status_code == 200 else {}
        runs = sess_body.get("runs") if isinstance(sess_body, dict) else None
        target = None
        if isinstance(runs, list):
            for item in runs:
                if isinstance(item, dict) and item.get("run_id") == FIXTURE_RUN_ID:
                    target = item
                    break
        all_ok &= check(
            "session_readable_run_listed_interrupted",
            sess_resp.status_code == 200
            and sess_body.get("session_id") == session_id
            and target is not None
            and target.get("status") == "failed"
            and target.get("error_code") == INTERRUPTED_CODE,
            {"http_status": sess_resp.status_code, "target_run": target},
        )

        # 无 SSE 重放：以上查询恢复不依赖 events；再只读一次 events 确认无轰炸。
        events = collect_events_once(client2, FIXTURE_RUN_ID)
        frames = events.pop("frames")
        status_payload = None
        for kind, data in frames:
            if kind == "status":
                try:
                    status_payload = json.loads(data)
                except ValueError:
                    status_payload = None
        all_ok &= check(
            "no_sse_replay_single_terminal_status_frame",
            events["frame_count"] == 1
            and events.get("event_kinds") == {"status": 1}
            and events["elapsed_seconds"] < 15.0
            and isinstance(status_payload, dict)
            and status_payload.get("status") == "failed"
            and status_payload.get("error_code") == INTERRUPTED_CODE,
            {**events, "status_payload": status_payload},
        )

        event_rows = read_interrupted_event_count(db_path)
        all_ok &= check(
            "db_failed_event_appended",
            event_rows == 1,
            {"interrupted_failed_events": event_rows},
        )

        report["ledger_profile_drafts"] = probe_ledger_drafts(db_path)

        client2.close()
        log_parts.append(second.kill())
        all_ok &= check(
            "uvicorn_second_killed",
            second.proc is not None and second.proc.returncode is not None,
        )

        log_text = "\n".join(log_parts)
        secrets = scan_logs_for_secrets(log_text)
        all_ok &= check("logs_free_of_keys", secrets["clean"], secrets)

        report["checklist"] = checklist
        report["all_passed"] = bool(all_ok)
        log(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if all_ok else 1
    except Exception as exc:  # noqa: BLE001 - 证据脚本记录脱敏失败原因
        report["checklist"] = checklist
        report["all_passed"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
        log(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    finally:
        if owned_tmp is not None:
            shutil.rmtree(owned_tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
