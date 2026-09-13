"""Stage 6 真实联调最小验证（S4-09 Subtask D 证据脚本；仅在明确授权下手工运行）。

用途：在**产品真实路径**上做最小真实模型验证——真实 uvicorn 进程 + 临时数据目录，
Key 经 ``PUT /api/provider/api-key`` 进入 SettingRepo（产品运行时按 10.3 从同库读 Key，
不经环境变量），对话 Run 走生产的 Provider／profile／费用账本（``runtime/fees``）路径。

三个阶段（**操作员协议，脚本不强制次数上限**：每阶段一条命令／至多一次 Run、smoke 失败即停
属运行纪律；脚本每次调用都会新建 Run，硬边界只有 Harness 容量上限与数据目录内 USD 50
账本）：

- ``smoke``：一条极小非敏感文本 Run；采集 SSE 事件种类、可见文本有无、原始 SSE 无
  隐藏推理字段、模型目录 id、模型响应行数（＝发送请求数代理；Run DTO 不含计数）、
  usage token 与账本花费增量。
- ``draft``：一条草稿工具路径 Run；Run 终态后按 ``draft`` 事件的 id 只读查库证明该行已
  落盘（本脚本在 SSE 流读到终态后才查库，**不证明通知瞬间已落盘**；即时顺序由离线用例
  ``tests/test_stage4_sse_events.py::test_draft_notification_follows_persistence`` 固定），
  随后用 ``GET /api/sessions/{id}`` 验证完成后的会话查询恢复。
- ``review``：``POST /api/reviews`` → Run 终态 → ``GET /api/reviews`` 取正文。

安全与成本边界（脚本自身强制）：

- 凭据只从环境变量 ``MODEL_API_KEY`` 读取（由调用方在**启动验证进程前**注入其环境；
  **不得在命令里 ``source`` 凭据文件**——``.env`` 的注释／行文本会被 shell 回显进运行
  产物，2026-09-13 已发生一次该事件并完成两轮产物脱敏）；脚本不打印、不落盘、不写日志
  任何凭据；报告只含存在性与脱敏事实。
- 子进程环境剔除 ``MODEL_*``／``DEEPSEEK_*``：产品运行时只经 SettingRepo 取 Key。
- 费用走已拍护栏（持久账本、预留／真实结算）——本脚本不复制账本算术，只读库内事实。
- 不落存思考原文：报告只给部件种类、字符数与布尔值，不打印模型输出正文。

用法（在 ``backend/`` 下；Key 由调用方在进程外注入，命令行不出现凭据、不 ``source`` 文件）：

    .venv/bin/python scripts/stage6_real_smoke.py --data-dir /tmp/fit-agent-stage6-d --phase smoke

数据目录协议：``/tmp/fit-agent-stage6-d`` 是本次授权批的**唯一**账本／数据目录；账本按目录
内 ``app.db`` 的 ``stage6`` 行持久，**换新 ``--data-dir`` 只会另起一份 $50 账本，不构成新的
付费调用授权**——额度是授权批口径，只有 owner 明确授权才可再发起付费调用。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx2

BACKEND = Path(__file__).resolve().parents[1]
#: 脚本用于对账的生产费用组件与 Harness 事实（只读；账本写入仍由产品路径完成）。
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
PHASES = ("smoke", "draft", "review")
TERMINAL = {"completed", "failed", "cancelled"}
REASONING_MARKERS = ("reasoning", "thinking", "thinkingpart", "enable_thinking")
#: 非敏感、极小提示：只证明真实流式连通，不携带业务与个人信息。
SMOKE_TEXT = "Reply with exactly one word: ok"
#: 草稿路径的显式指令：模型只需按工具形状透传一条最小档案草稿（协议验证用）。
#: 首次实测（工具描述当时未给词表）模型连续 4 次用非法 state 后放弃——那是普通工具结果后的
#: 模型自行再调（重复模型调用），不是框架纠错重试；当时重跑只在测试输入里补写词表。
#: 2026-09-13 跟进：该词表已写进 ``propose_profile_draft`` 工具描述（校验未改）。
DRAFT_TEXT = (
    "请调用 propose_profile_draft 工具，直接提出一条待确认档案草稿，不要向我追问。"
    'proposed 参数是八字段对象，每个字段只能是 {"state": ..., "value": ...} 形状，'
    'state 只能是 "unknown"、"denied"、"known" 三种之一，非 known 时 value 必须为 null。'
    '请严格按以下内容传入：training_goal={"state":"known","value":"增肌"}；'
    'training_experience={"state":"known","value":"新手"}；'
    'weekly_frequency={"state":"known","value":3}；'
    'session_duration_minutes={"state":"known","value":60}；'
    'body_weight_kg={"state":"known","value":70}；'
    'available_equipment={"state":"denied","value":null}；'
    'action_restrictions={"state":"denied","value":null}；'
    'body_conditions={"state":"denied","value":null}。'
)


def log(message: str) -> None:
    print(message, flush=True)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def child_env(data_dir: Path) -> dict[str, str]:
    """子进程环境：剔除凭据环境变量（产品运行时只经 SettingRepo 取 Key）。"""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("MODEL_", "DEEPSEEK_"))
    }
    env["FIT_AGENT_DATA_DIR"] = str(data_dir)
    env["PYTHONPATH"] = str(BACKEND)
    return env


def ledger_snapshot(db_path: Path) -> dict[str, Any] | None:
    """只读查询持久账本 Stage 6 行（金额事实，不含任何凭据）。"""
    if not db_path.exists():
        return None
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        row = con.execute(
            "SELECT limit_usd, spent_usd, reserved_usd, updated_at"
            " FROM fee_ledger WHERE stage = 'stage6'"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()
    if row is None:
        return None
    limit, spent, reserved = float(row[0]), float(row[1]), float(row[2])
    return {
        "limit_usd": limit,
        "spent_usd": spent,
        "reserved_usd": reserved,
        "available_usd": limit - spent - reserved,
        "updated_at": str(row[3]),
    }


def inspect_run_messages(db_path: Path, run_id: str) -> dict[str, Any]:
    """只读检查 Run 已落盘的框架消息：部件种类、可见文本字符数、usage（不含正文）。"""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        rows = con.execute(
            "SELECT role, kind, payload_json FROM messages WHERE run_id = ? ORDER BY seq",
            (run_id,),
        ).fetchall()
    finally:
        con.close()
    response_rows = 0
    framework_rows = 0
    part_kinds: list[str] = []
    visible_text_chars = 0
    input_tokens = output_tokens = cache_read_tokens = reasoning_tokens = 0
    model_name = provider_name = finish_reason = None
    for _role, kind, payload_json in rows:
        if kind != "framework":
            continue
        framework_rows += 1
        try:
            payload = json.loads(payload_json)
        except ValueError:
            continue
        # 每条框架消息的负载是 ``ModelMessagesTypeAdapter`` 的单消息 JSON 列表。
        messages = payload if isinstance(payload, list) else [payload]
        for message in messages:
            if not isinstance(message, dict) or message.get("kind") != "response":
                continue
            response_rows += 1
            model_name = model_name or message.get("model_name")
            provider_name = provider_name or message.get("provider_name")
            finish_reason = finish_reason or message.get("finish_reason")
            for part in message.get("parts", []):
                part_kind = str(part.get("part_kind"))
                if part_kind not in part_kinds:
                    part_kinds.append(part_kind)
                if part_kind == "text" and isinstance(part.get("content"), str):
                    visible_text_chars += len(part["content"])
            usage = message.get("usage") or {}
            input_tokens += int(usage.get("input_tokens") or 0)
            output_tokens += int(usage.get("output_tokens") or 0)
            cache_read_tokens += int(usage.get("cache_read_tokens") or 0)
            reasoning_tokens += int(usage.get("output_reasoning_tokens") or 0)
    return {
        "framework_message_rows": framework_rows,
        "model_response_rows": response_rows,
        "provider_name_reported": provider_name,
        "part_kinds": part_kinds,
        "thinking_part_present": "thinking" in part_kinds,
        "visible_text_chars": visible_text_chars,
        "model_name_reported": model_name,
        "finish_reason": finish_reason,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "output_reasoning_tokens": reasoning_tokens,
        },
    }


def collect_sse(client: httpx2.Client, run_id: str) -> list[tuple[str, str]]:
    """收集一个 Run 的 SSE 帧（event 名 + data 文本），终态即返回；不落盘。"""
    events: list[tuple[str, str]] = []
    name = ""
    with client.stream("GET", f"/api/runs/{run_id}/events", timeout=180.0) as stream:
        for line in stream.iter_lines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                events.append((name, line[len("data: ") :]))
            if len(events) > 5000:
                break
    return events


def summarize_sse(events: list[tuple[str, str]]) -> dict[str, Any]:
    kinds: dict[str, int] = {}
    answer_chars = 0
    for name, data in events:
        kinds[name] = kinds.get(name, 0) + 1
        if name == "answer":
            try:
                answer_chars += len(str(json.loads(data).get("text") or ""))
            except ValueError:
                pass
    raw = "\n".join(f"{name} {data}" for name, data in events)
    lowered = raw.lower()
    return {
        "frame_count": len(events),
        "event_kinds": kinds,
        "answer_text_chars": answer_chars,
        "reasoning_marker_in_raw_sse": [
            marker for marker in REASONING_MARKERS if marker in lowered
        ],
    }


def wait_terminal(
    client: httpx2.Client, run_id: str, deadline: float
) -> dict[str, Any]:
    run: dict[str, Any] = {}
    while time.time() < deadline:
        fetched = client.get(f"/api/runs/{run_id}").json().get("run")
        if isinstance(fetched, dict):
            run = fetched
        if run.get("status") in TERMINAL:
            return run
        time.sleep(0.25)
    return run


class Harness:
    """一个阶段一次性的真实后端进程与回环客户端（数据目录跨阶段复用）。"""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.db_path = data_dir / "app.db"
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen[bytes] | None = None
        self.client: httpx2.Client | None = None

    def __enter__(self) -> "Harness":
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
        client = httpx2.Client(base_url=self.base, timeout=30.0)
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                if client.get("/healthz").status_code == 200:
                    break
            except httpx2.HTTPError:
                time.sleep(0.1)
        else:
            raise RuntimeError("真实后端未在 20 秒内就绪（healthz 超时）")
        self.client = client
        return self

    def __exit__(self, *exc: object) -> None:
        if self.client is not None:
            self.client.close()
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    @property
    def client_or_raise(self) -> httpx2.Client:
        if self.client is None:
            raise RuntimeError("Harness 未进入上下文，客户端不可用")
        return self.client

    def ensure_key(self) -> dict[str, Any]:
        """Key 只从验证进程环境读入，经产品 API 写入 SettingRepo；报告只含存在性。"""
        client = self.client_or_raise
        projection = client.get("/api/provider").json()
        if not projection.get("has_api_key"):
            key = os.environ.get("MODEL_API_KEY", "")
            if not key.strip():
                raise RuntimeError(
                    "验证进程环境没有 MODEL_API_KEY，且库内未配置："
                    "先把 owner .env 载入本进程（不打印值）"
                )
            response = client.put("/api/provider/api-key", json={"api_key": key})
            if response.status_code != 200 or not response.json().get("has_api_key"):
                raise RuntimeError(
                    f"PUT /api/provider/api-key 失败：HTTP {response.status_code}"
                )
        return {
            "has_api_key": bool(client.get("/api/provider").json().get("has_api_key")),
            "key_from_validation_process_env": bool(
                os.environ.get("MODEL_API_KEY", "").strip()
            ),
            "model": projection.get("model"),
            "base_url": projection.get("base_url"),
            "provider": projection.get("provider"),
        }

    def start_text_run(self, session_id: str, request_id: str, text: str) -> str:
        response = self.client_or_raise.post(
            f"/api/sessions/{session_id}/requests",
            json={"client_request_id": request_id, "text": text},
        )
        if response.status_code != 200:
            raise RuntimeError(f"提交请求失败：HTTP {response.status_code}")
        return str(response.json()["run"]["run_id"])

    def draft_row(self, draft_id: str) -> dict[str, Any] | None:
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10)
        try:
            row = con.execute(
                "SELECT id, kind, status, revision FROM business_drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        return {"id": row[0], "kind": row[1], "status": row[2], "revision": int(row[3])}


def expected_fees(
    model_id: str, data_dir: Path, usage: dict[str, int]
) -> dict[str, Any]:
    """生产费用组件的期望值（只为对账；账本本身仍由产品路径写入）。"""
    try:
        from config import effective_harness_config, load_harness_config
        from runtime.fees import reservation_usd, usage_usd
    except ImportError as exc:
        return {"available": False, "import_error": type(exc).__name__}
    harness = effective_harness_config(load_harness_config(data_dir))
    result: dict[str, Any] = {
        "available": True,
        "effective_input_tokens": harness.effective_input_tokens,
        "expected_reservation_per_request_usd": round(
            reservation_usd(model_id, harness.effective_input_tokens), 8
        ),
    }
    if usage["input_tokens"] or usage["output_tokens"]:
        result["expected_actual_usd"] = round(
            usage_usd(
                model_id,
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
            ),
            8,
        )
    else:
        result["expected_actual_usd"] = None
    return result


def _run_ledger_delta(
    before: dict[str, Any] | None, after: dict[str, Any] | None
) -> dict[str, Any]:
    if before is None or after is None:
        return {}
    return {
        "spent_delta_usd": round(after["spent_usd"] - before["spent_usd"], 8),
        "reserved_after_usd": after["reserved_usd"],
    }


def _model_name(report: dict[str, Any]) -> str:
    model = (report.get("provider") or {}).get("model") or {}
    return str(model.get("name") or report.get("model_name_env") or "")


def phase_smoke(harness: Harness, report: dict[str, Any]) -> None:
    client = harness.client
    assert client is not None
    session_id = client.post("/api/sessions", json={}).json()["session_id"]
    before = ledger_snapshot(harness.db_path)
    run_id = harness.start_text_run(
        session_id, f"stage6-d-smoke-{int(time.time())}", SMOKE_TEXT
    )
    events = collect_sse(client, run_id)
    deadline = time.time() + 120
    run = wait_terminal(client, run_id, deadline)
    stored = inspect_run_messages(harness.db_path, run_id)
    after = ledger_snapshot(harness.db_path)
    report["session_id"] = session_id
    report["run"] = run
    report["sse"] = summarize_sse(events)
    report["stored"] = stored
    report["ledger_before"] = before
    report["ledger_after"] = after
    report["ledger_run_delta"] = _run_ledger_delta(before, after)
    report["expected_fees"] = expected_fees(
        _model_name(report), harness.data_dir, stored["usage"]
    )


def phase_draft(harness: Harness, report: dict[str, Any]) -> None:
    client = harness.client
    assert client is not None
    session_id = client.post("/api/sessions", json={}).json()["session_id"]
    before = ledger_snapshot(harness.db_path)
    run_id = harness.start_text_run(
        session_id, f"stage6-d-draft-{int(time.time())}", DRAFT_TEXT
    )
    events = collect_sse(client, run_id)
    draft_event = None
    draft_row_after_terminal = None
    for name, data in events:
        if name == "draft":
            draft_event = json.loads(data)
            draft_row_after_terminal = harness.draft_row(
                str(draft_event.get("draft_id"))
            )
            break
    deadline = time.time() + 120
    run = wait_terminal(client, run_id, deadline)
    stored = inspect_run_messages(harness.db_path, run_id)
    after = ledger_snapshot(harness.db_path)
    session = client.get(f"/api/sessions/{session_id}").json()
    user_texts = [
        m.get("text") for m in session.get("messages", []) if m.get("role") == "user"
    ]
    answers = [
        m.get("text")
        for m in session.get("messages", [])
        if m.get("role") == "assistant" and m.get("kind") == "answer"
    ]
    report["session_id"] = session_id
    report["run"] = run
    report["sse"] = summarize_sse(events)
    report["draft_event"] = draft_event
    # 命名按可证事实：events 由 collect_sse 收完整流后返回，这里的查询发生在终态之后；
    # 即时顺序由 tests/test_stage4_sse_events.py::test_draft_notification_follows_persistence 固定
    report["draft_row_present_after_terminal"] = draft_row_after_terminal is not None
    report["draft_row_after_terminal"] = draft_row_after_terminal
    report["stored"] = stored
    report["ledger_before"] = before
    report["ledger_after"] = after
    report["ledger_run_delta"] = _run_ledger_delta(before, after)
    report["expected_fees"] = expected_fees(
        _model_name(report), harness.data_dir, stored["usage"]
    )
    report["session_recovery"] = {
        "user_text_recovered": DRAFT_TEXT in user_texts,
        "answer_recovered": bool(answers),
        "run_recovered": any(
            item.get("run_id") == run_id for item in session.get("runs", [])
        ),
        "run_terminal_status": run.get("status") if run else None,
    }


def phase_review(harness: Harness, report: dict[str, Any]) -> None:
    client = harness.client
    assert client is not None
    before = ledger_snapshot(harness.db_path)
    response = client.post(
        "/api/reviews",
        json={"client_request_id": f"stage6-d-review-{int(time.time())}"},
    )
    if response.status_code != 200:
        raise RuntimeError(f"POST /api/reviews 失败：HTTP {response.status_code}")
    run_id = str(response.json()["run"]["run_id"])
    events = collect_sse(client, run_id)
    deadline = time.time() + 120
    run = wait_terminal(client, run_id, deadline)
    stored = inspect_run_messages(harness.db_path, run_id)
    after = ledger_snapshot(harness.db_path)
    reviews = client.get("/api/reviews")
    review_list = (
        reviews.json().get("reviews", []) if reviews.status_code == 200 else []
    )
    latest = review_list[-1] if review_list else None
    body = str(latest.get("body_markdown") or "") if latest else ""
    report["run"] = run
    report["sse"] = summarize_sse(events)
    report["stored"] = stored
    report["ledger_before"] = before
    report["ledger_after"] = after
    report["ledger_run_delta"] = _run_ledger_delta(before, after)
    report["expected_fees"] = expected_fees(
        _model_name(report), harness.data_dir, stored["usage"]
    )
    report["reviews_get"] = {
        "http_status": reviews.status_code,
        "count": len(review_list),
        "latest_id": None if latest is None else latest.get("id"),
        "latest_body_chars": len(body),
        "latest_body_present": bool(body),
        "latest_stale": None if latest is None else latest.get("stale"),
    }


PHASE_RUNNERS = {
    "smoke": phase_smoke,
    "draft": phase_draft,
    "review": phase_review,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 6 真实模型最小验证（一次一个阶段）"
    )
    parser.add_argument("--data-dir", required=True, help="跨阶段复用的临时数据目录")
    parser.add_argument("--phase", choices=PHASES, required=True)
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "phase": args.phase,
        "data_dir": str(data_dir),
        "model_name_env": os.environ.get("MODEL_NAME"),
        "model_base_url_env": os.environ.get("MODEL_BASE_URL"),
    }
    try:
        with Harness(data_dir) as harness:
            report["provider"] = harness.ensure_key()
            report["ledger_start"] = ledger_snapshot(harness.db_path)
            PHASE_RUNNERS[args.phase](harness, report)
    except Exception as exc:  # noqa: BLE001 - 证据脚本记录脱敏失败原因
        report["error"] = f"{type(exc).__name__}: {exc}"
        log(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    log(json.dumps(report, ensure_ascii=False, indent=2))
    run = report.get("run") or {}
    if args.phase == "smoke":
        return 0 if run.get("status") == "completed" else 1
    return 0 if run.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
