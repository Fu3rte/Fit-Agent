"""Stage 6 F6-06b：训练、安排与更正闭环协议验证（真实后端 + 真实模型）。

在 ``stage6_real_smoke`` 的 Harness 基础上做**最小扩展**，串起完整训练闭环：

自包含建档（``DRAFT_TEXT`` 变体）→ 计划启用 → 查看当次安排 → ``propose_arrangement_draft``
→ confirm → ``propose_record_draft``（新增）→ confirm → 统计刷新 → 同身份更正 → confirm →
作废（``POST /void``）→ 终态后再更正被拒（fail-closed）。

安全与费用边界与 ``stage6_real_smoke`` 相同：

- 凭据只从进程环境 ``MODEL_API_KEY`` 读取；脚本不打印、不落盘、不写日志任何凭据。
- 数据目录为本批**唯一**账本（``--data-dir``）；换新目录只另起一份账本。
- 费用走已拍护栏（持久账本）——本脚本不复制账本算术，只读库内事实。

用法（在 ``backend/`` 下；Key 由调用方在进程外注入）：

    .venv/Scripts/python.exe scripts/f606_record_loop.py --data-dir %TEMP%/fit-agent-stage6-f606
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(BACKEND / "scripts") not in sys.path:
    sys.path.insert(0, str(BACKEND / "scripts"))

from stage6_real_smoke import (  # noqa: E402
    Harness,
    _model_name,
    _run_ledger_delta,
    collect_sse,
    expected_fees,
    inspect_run_messages,
    ledger_snapshot,
    summarize_sse,
    wait_terminal,
)

#: 模型草稿失败时的最大重试次数（首次 + 最多 2 次重试）。
MAX_DRAFT_RETRIES = 2

#: 记录闭环使用的动作（目录种子内、外加负重次数型）。
RECORD_EXERCISE_ID = "barbell-bench-press"
RECORD_LOAD_NOTATION = "barbell_includes_bar_total"

#: 安排调整条目（PPL 推日第 1 项：杠铃平板卧推，计划 4 组）。
ARRANGE_ITEM_KEY = "push-01"
ARRANGE_WORK_SETS = 2
ARRANGE_REASON = "用户报告睡眠不足，今天把 push-01 的工作组从 4 组减到 2 组"


def log(message: str) -> None:
    print(message, flush=True)


def _profile_text() -> str:
    """首次建档显式指令：器械必须 known（计划生成需要），其余按 f604 词表。"""
    return (
        "请调用 propose_profile_draft 工具，直接提出一条待确认档案草稿，不要向我追问。"
        'proposed 参数是八字段对象，每个字段只能是 {"state": ..., "value": ...} 形状，'
        'state 只能是 "unknown"、"denied"、"known" 三种之一，非 known 时 value 必须为 null。'
        '请严格按以下内容传入：training_goal={"state":"known","value":"增肌"}；'
        'training_experience={"state":"known","value":"新手"}；'
        'weekly_frequency={"state":"known","value":3}；'
        'session_duration_minutes={"state":"known","value":60}；'
        'body_weight_kg={"state":"known","value":70}；'
        'available_equipment={"state":"known","value":["杠铃","哑铃","绳索","引体架"]}；'
        'action_restrictions={"state":"denied","value":null}；'
        'body_conditions={"state":"denied","value":null}。'
    )


def _plan_text(starts_on: str, review_on: str) -> str:
    return (
        "请调用 propose_plan_draft 工具，直接提出一条待确认计划草稿，不要向我追问。"
        f'starts_on="{starts_on}"，review_on="{review_on}"。'
        "这是首次建档后的第一个计划：不要传 proposed_profile，不要传 adjustments，"
        "不要传 long_term_adjustment。只传 starts_on 与 review_on 即可。"
    )


def _arrangement_text(scheduled_session_id: str) -> str:
    # 纯 JSON 参数块：避免模型把 adjustments 数组序列化成字符串（qwen 已复现的校验失败）。
    args = (
        "{"
        f'"scheduled_session_id": "{scheduled_session_id}", '
        f'"adjustments": [{{"item_key": "{ARRANGE_ITEM_KEY}", '
        f'"work_sets": {ARRANGE_WORK_SETS}}}], '
        f'"adjustment_reason": "{ARRANGE_REASON}"'
        "}"
    )
    return (
        "请调用 propose_arrangement_draft 工具，直接提出一条待确认当次安排草稿，不要向我追问。"
        "把下面这份 JSON **原样**作为工具调用参数（adjustments 必须是数组，不能是字符串）："
        f"{args}"
    )


def _arrangement_text_minimal(scheduled_session_id: str) -> str:
    """重试降级：只传 scheduled_session_id（接受原计划目标），避免 adjustments 数组参数。"""
    return (
        "请调用 propose_arrangement_draft 工具，直接提出一条待确认当次安排草稿，不要向我追问。"
        f'只传一个参数：scheduled_session_id="{scheduled_session_id}"。'
        "不要传 adjustments，不要传 adjustment_reason。"
    )


def _record_text(
    occurred_on: str, training_session_id: str | None, *, load_value: str
) -> str:
    session_part = (
        '"training_session_id": null'
        if training_session_id is None
        else f'"training_session_id": "{training_session_id}"'
    )
    return (
        "请调用 propose_record_draft 工具，直接提出一条待确认训练记录草稿，不要向我追问。"
        "record 参数必须是 JSON 对象（禁止写成字符串），严格按以下形状传入"
        "（不要改字段名、不要省略 sets）："
        "{"
        f"{session_part}, "
        f'"occurred_on": "{occurred_on}", '
        '"completion_declared": true, '
        '"exercises": [{'
        '"position": 1, '
        f'"exercise_id": "{RECORD_EXERCISE_ID}", '
        '"record_type": "reps_weight", '
        f'"load_notation": "{RECORD_LOAD_NOTATION}", '
        '"sets": [{"set_no": 1, "set_type": "work", '
        f'"load": {{"value_text": "{load_value}", "unit": "kg"}}, "reps": 8}}]'
        "}]"
        "}"
    )


def _run_model_draft(
    harness: Harness,
    session_id: str,
    request_prefix: str,
    text: str | list[str],
    *,
    expected_kind: str,
    report: dict[str, Any],
    report_key: str,
) -> str:
    """一次模型 Run 产出指定 kind 草稿；失败时最多重试 2 次。

    ``text`` 为列表时按 attempt 索引取对应指令（越靠后越简化），用于绕开复杂参数校验失败。
    """
    client = harness.client_or_raise
    prompts = [text] if isinstance(text, str) else list(text)
    attempts: list[dict[str, Any]] = []
    last_error = ""
    for attempt in range(1 + MAX_DRAFT_RETRIES):
        prompt = prompts[min(attempt, len(prompts) - 1)]
        run_id = harness.start_text_run(
            session_id, f"{request_prefix}-{attempt}-{int(time.time())}", prompt
        )
        events = collect_sse(client, run_id)
        draft_event = None
        for name, data in events:
            if name == "draft":
                draft_event = json.loads(data)
                break
        deadline = time.time() + 120
        run = wait_terminal(client, run_id, deadline)
        status = run.get("status") if isinstance(run, dict) else None
        draft_id = str((draft_event or {}).get("draft_id") or "")
        row = harness.draft_row(draft_id) if draft_id else None
        ok = (
            status == "completed"
            and row is not None
            and row.get("kind") == expected_kind
            and row.get("status") == "pending"
        )
        attempts.append(
            {
                "attempt": attempt + 1,
                "run_id": run_id,
                "run_status": status,
                "draft_id": draft_id or None,
                "draft_kind": None if row is None else row.get("kind"),
                "draft_status": None if row is None else row.get("status"),
                "sse": summarize_sse(events),
            }
        )
        if ok:
            report[report_key] = {"attempts": attempts, "selected": draft_id}
            return draft_id
        last_error = (
            f"attempt {attempt + 1}: run={status} draft={row} expected_kind={expected_kind}"
        )
        log(f"  [retry] {report_key}: {last_error}")
    raise RuntimeError(f"模型未产出 {expected_kind} 草稿（已重试 {MAX_DRAFT_RETRIES} 次）：{last_error}")


def _confirm_draft(
    client: Any, draft_id: str, *, expect_status: int = 200
) -> tuple[int, dict[str, Any]]:
    draft_view = client.get(f"/api/drafts/{draft_id}")
    if draft_view.status_code != 200:
        raise RuntimeError(
            f"GET /api/drafts/{{id}} 失败：HTTP {draft_view.status_code}"
        )
    revision = int(draft_view.json().get("revision") or 0)
    resp = client.post(
        f"/api/drafts/{draft_id}/confirm", json={"revision": revision}
    )
    body = resp.json() if resp.status_code == expect_status else {}
    if resp.status_code != expect_status:
        # 读取错误体以便报告；调用方决定是否 raise
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text[:200]}
    return resp.status_code, body if isinstance(body, dict) else {}


def _pick_push_session(plan_body: dict[str, Any]) -> dict[str, Any]:
    """从 GET /api/plan 选一条未取消的 push 日程（优先未锁定）。"""
    plan = plan_body.get("plan") or {}
    schedules = plan.get("schedules") or []
    candidates = [
        entry
        for entry in schedules
        if entry.get("plan_workout_key") == "push" and not entry.get("cancelled")
    ]
    if not candidates:
        raise RuntimeError("计划里没有未取消的 push 日程，无法绑定安排")
    unlocked = [entry for entry in candidates if not (entry.get("lock") or {}).get("effective")]
    return (unlocked or candidates)[0]


def run_loop(harness: Harness, report: dict[str, Any]) -> None:
    client = harness.client_or_raise
    ledger_runs: list[dict[str, Any]] = []

    def snapshot_run(label: str, before: dict[str, Any] | None) -> None:
        after = ledger_snapshot(harness.db_path)
        ledger_runs.append(
            {
                "label": label,
                "before": before,
                "after": after,
                "delta": _run_ledger_delta(before, after),
            }
        )

    # ---------- 建档（真实模型） ----------
    setup_session = client.post("/api/sessions", json={}).json()["session_id"]
    before = ledger_snapshot(harness.db_path)
    profile_draft_id = _run_model_draft(
        harness,
        setup_session,
        "f606-profile",
        _profile_text(),
        expected_kind="profile_update",
        report=report,
        report_key="setup_profile_draft",
    )
    status, body = _confirm_draft(client, profile_draft_id)
    if status != 200:
        raise RuntimeError(f"档案确认失败：HTTP {status} {body}")
    report["setup_profile_confirm"] = body
    snapshot_run("setup_profile", before)

    # ---------- 计划启用（真实模型） ----------
    today = date.today()
    starts_on = today.isoformat()
    review_on = (today + timedelta(days=28)).isoformat()
    before = ledger_snapshot(harness.db_path)
    plan_draft_id = _run_model_draft(
        harness,
        setup_session,
        "f606-plan",
        _plan_text(starts_on, review_on),
        expected_kind="plan",
        report=report,
        report_key="setup_plan_draft",
    )
    status, body = _confirm_draft(client, plan_draft_id)
    if status != 200:
        raise RuntimeError(f"计划确认失败：HTTP {status} {body}")
    report["setup_plan_confirm"] = body
    plan_version_id = body.get("plan_version_id")
    snapshot_run("setup_plan", before)
    if not plan_version_id:
        raise RuntimeError("计划确认未返回 plan_version_id")

    # ---------- 验收点 1：查看当次安排 ----------
    plan_resp = client.get("/api/plan")
    if plan_resp.status_code != 200:
        raise RuntimeError(f"GET /api/plan 失败：HTTP {plan_resp.status_code}")
    plan_body = plan_resp.json()
    target_session = _pick_push_session(plan_body)
    scheduled_session_id = str(target_session["id"])
    report["step1_view_arrangement"] = {
        "plan_version_id": plan_version_id,
        "plan_starts_on": (plan_body.get("plan") or {}).get("starts_on"),
        "plan_review_on": (plan_body.get("plan") or {}).get("review_on"),
        "scheduled_session_id": scheduled_session_id,
        "scheduled_on": target_session.get("scheduled_on"),
        "plan_workout_key": target_session.get("plan_workout_key"),
        "status": target_session.get("status"),
        "schedule_count": len((plan_body.get("plan") or {}).get("schedules") or []),
    }

    # ---------- 验收点 2：对话提出安排调整并确认 ----------
    conv_session = client.post("/api/sessions", json={}).json()["session_id"]
    before = ledger_snapshot(harness.db_path)
    arrange_draft_id = _run_model_draft(
        harness,
        conv_session,
        "f606-arrange",
        [
            _arrangement_text(scheduled_session_id),
            _arrangement_text(scheduled_session_id),
            _arrangement_text_minimal(scheduled_session_id),
        ],
        expected_kind="arrangement",
        report=report,
        report_key="step2_arrangement_draft",
    )
    status, body = _confirm_draft(client, arrange_draft_id)
    if status != 200:
        raise RuntimeError(f"安排确认失败：HTTP {status} {body}")
    arrangement_revision_id = body.get("arrangement_revision_id")
    report["step2_arrangement_confirm"] = {
        "http_status": status,
        "arrangement_revision_id": arrangement_revision_id,
        "arrangement_revision_no": body.get("arrangement_revision_no"),
        "scheduled_session_id": body.get("scheduled_session_id"),
        "accepted_at": body.get("accepted_at"),
        "committed_business_version": body.get("committed_business_version"),
    }
    if not arrangement_revision_id or body.get("scheduled_session_id") != scheduled_session_id:
        raise RuntimeError(f"安排确认凭据不符合预期：{body}")
    snapshot_run("arrangement", before)

    # 计划长期版本不应因当次安排改变
    plan_after_arr = client.get("/api/plan").json()
    if (plan_after_arr.get("plan") or {}).get("id") != plan_version_id:
        raise RuntimeError("当次安排确认后正式计划版本被改写")

    # ---------- 验收点 3：新增打卡记录并确认 ----------
    occurred_on = today.isoformat()
    before = ledger_snapshot(harness.db_path)
    pr_before = client.get(
        "/api/stats/pr",
        params={
            "exercise_id": RECORD_EXERCISE_ID,
            "load_notation": RECORD_LOAD_NOTATION,
        },
    ).json()
    pr_before_load = (pr_before.get("pr") or {}).get("max_load_kg_key")

    record_draft_id = _run_model_draft(
        harness,
        conv_session,
        "f606-record-new",
        _record_text(occurred_on, None, load_value="40"),
        expected_kind="training_record",
        report=report,
        report_key="step3_record_new_draft",
    )
    status, body = _confirm_draft(client, record_draft_id)
    if status != 200:
        raise RuntimeError(f"记录确认失败：HTTP {status} {body}")
    training_session_id = str(body.get("training_session_id") or "")
    report["step3_record_new_confirm"] = {
        "http_status": status,
        "training_session_id": training_session_id,
        "session_revision_id": body.get("session_revision_id"),
        "revision_no": body.get("revision_no"),
        "revision_status": body.get("revision_status"),
        "committed_business_version": body.get("committed_business_version"),
    }
    if not training_session_id:
        raise RuntimeError("记录确认未返回 training_session_id")

    records_resp = client.get("/api/records")
    records = records_resp.json().get("records") or [] if records_resp.status_code == 200 else []
    pr_after = client.get(
        "/api/stats/pr",
        params={
            "exercise_id": RECORD_EXERCISE_ID,
            "load_notation": RECORD_LOAD_NOTATION,
        },
    ).json()
    pr_after_load = (pr_after.get("pr") or {}).get("max_load_kg_key")
    report["step3_records_after"] = {
        "records_http": records_resp.status_code,
        "records_count": len(records),
        "session_in_records": any(
            item.get("id") == training_session_id for item in records
        ),
        "pr_before_max_load_kg_key": pr_before_load,
        "pr_after_max_load_kg_key": pr_after_load,
        "pr_updated": pr_after_load is not None
        and pr_after_load != pr_before_load,
    }
    if not report["step3_records_after"]["session_in_records"]:
        raise RuntimeError("确认后 GET /api/records 未包含本次训练身份")
    if not report["step3_records_after"]["pr_updated"]:
        raise RuntimeError("确认后 GET /api/stats/pr 未反映本次记录（不伪造）")
    snapshot_run("record_new", before)

    # ---------- 验收点 4：同身份更正（不新增训练身份） ----------
    before = ledger_snapshot(harness.db_path)
    correct_draft_id = _run_model_draft(
        harness,
        conv_session,
        "f606-record-correct",
        _record_text(occurred_on, training_session_id, load_value="45"),
        expected_kind="training_record",
        report=report,
        report_key="step4_record_correct_draft",
    )
    status, body = _confirm_draft(client, correct_draft_id)
    if status != 200:
        raise RuntimeError(f"更正确认失败：HTTP {status} {body}")
    report["step4_record_correct_confirm"] = {
        "http_status": status,
        "training_session_id": body.get("training_session_id"),
        "session_revision_id": body.get("session_revision_id"),
        "revision_no": body.get("revision_no"),
        "revision_status": body.get("revision_status"),
        "same_training_session_id": body.get("training_session_id")
        == training_session_id,
    }
    if body.get("training_session_id") != training_session_id:
        raise RuntimeError("更正未绑定原训练身份")
    if int(body.get("revision_no") or 0) < 2:
        raise RuntimeError("更正未追加修订号")
    records_after_correct = client.get(
        f"/api/records/{training_session_id}"
    ).json()
    current_rev = (records_after_correct.get("record") or {}).get("revision") or {}
    report["step4_history"] = {
        "record_http": 200,
        "current_revision_no": current_rev.get("revision_no"),
        "current_status": current_rev.get("status"),
        "revision_no_traceable": current_rev.get("revision_no")
        == body.get("revision_no"),
    }
    snapshot_run("record_correct", before)

    # ---------- 验收点 5：作废（记录草稿路径 POST /void） ----------
    before = ledger_snapshot(harness.db_path)
    void_draft_id = _run_model_draft(
        harness,
        conv_session,
        "f606-record-void",
        _record_text(occurred_on, training_session_id, load_value="45"),
        expected_kind="training_record",
        report=report,
        report_key="step5_record_void_draft",
    )
    draft_view = client.get(f"/api/drafts/{void_draft_id}").json()
    void_revision = int(draft_view.get("revision") or 0)
    void_resp = client.post(
        f"/api/drafts/{void_draft_id}/void", json={"revision": void_revision}
    )
    void_body = void_resp.json() if void_resp.status_code == 200 else {}
    report["step5_void"] = {
        "http_status": void_resp.status_code,
        "training_session_id": void_body.get("training_session_id"),
        "session_revision_id": void_body.get("session_revision_id"),
        "revision_no": void_body.get("revision_no"),
        "revision_status": void_body.get("revision_status"),
        "same_training_session_id": void_body.get("training_session_id")
        == training_session_id,
    }
    if void_resp.status_code != 200 or void_body.get("revision_status") != "voided":
        raise RuntimeError(f"作废失败：HTTP {void_resp.status_code} {void_body}")
    record_voided = client.get(f"/api/records/{training_session_id}").json()
    voided_rev = (record_voided.get("record") or {}).get("revision") or {}
    report["step5_records_status"] = {
        "current_status": voided_rev.get("status"),
        "current_revision_no": voided_rev.get("revision_no"),
    }
    if voided_rev.get("status") != "voided":
        raise RuntimeError(f"GET /api/records 当前修订未呈 voided：{voided_rev}")
    snapshot_run("record_void", before)

    # ---------- 验收点 6（硬）：作废后再更正被拒 ----------
    before = ledger_snapshot(harness.db_path)
    re_draft_id = _run_model_draft(
        harness,
        conv_session,
        "f606-record-recorrect",
        _record_text(occurred_on, training_session_id, load_value="50"),
        expected_kind="training_record",
        report=report,
        report_key="step6_recorrect_draft",
    )
    status, body = _confirm_draft(client, re_draft_id, expect_status=422)
    report["step6_recorrect_rejected"] = {
        "http_status": status,
        "error_code": body.get("error_code"),
        "message_present": bool(body.get("message") or body.get("detail")),
    }
    if status != 422 or body.get("error_code") != "invalid_request":
        # 硬验收点失败：立即停下，不静默通过
        raise RuntimeError(
            f"作废终态未被 fail-closed 拦截：期望 422 invalid_request，"
            f"实际 HTTP {status} code={body.get('error_code')}"
        )
    # 身份仍 voided、无新修订
    record_still = client.get(f"/api/records/{training_session_id}").json()
    still_rev = (record_still.get("record") or {}).get("revision") or {}
    report["step6_after_rejection"] = {
        "current_status": still_rev.get("status"),
        "current_revision_no": still_rev.get("revision_no"),
        "unchanged_after_reject": still_rev.get("status") == "voided"
        and still_rev.get("revision_no") == void_body.get("revision_no"),
    }
    snapshot_run("record_recorrect_rejected", before)

    # ---------- 验收点 7：三类数据身份区分 ----------
    report["step7_identity_separation"] = {
        "original_plan": {
            "plan_version_id": plan_version_id,
            "starts_on": report["step1_view_arrangement"]["plan_starts_on"],
            "review_on": report["step1_view_arrangement"]["plan_review_on"],
        },
        "accepted_arrangement": {
            "arrangement_revision_id": arrangement_revision_id,
            "scheduled_session_id": scheduled_session_id,
            "accepted_at": report["step2_arrangement_confirm"]["accepted_at"],
        },
        "actual_record": {
            "training_session_id": training_session_id,
            "first_session_revision_id": report["step3_record_new_confirm"][
                "session_revision_id"
            ],
            "correct_session_revision_id": report["step4_record_correct_confirm"][
                "session_revision_id"
            ],
            "void_session_revision_id": report["step5_void"]["session_revision_id"],
            "final_status": still_rev.get("status"),
        },
        "all_ids_distinct": len(
            {
                plan_version_id,
                arrangement_revision_id,
                scheduled_session_id,
                training_session_id,
            }
        )
        == 4,
    }
    if not report["step7_identity_separation"]["all_ids_distinct"]:
        raise RuntimeError("计划版本／安排修订／日程／训练身份 id 未区分")

    # ---------- 费用与存储 ----------
    stored = inspect_run_messages(harness.db_path, report["setup_profile_draft"]["attempts"][0]["run_id"])
    after = ledger_snapshot(harness.db_path)
    report["stored_first_run"] = stored
    report["ledger_before"] = report.get("ledger_start")
    report["ledger_after"] = after
    report["ledger_run_steps"] = ledger_runs
    report["ledger_total_delta"] = _run_ledger_delta(
        report.get("ledger_start"), after
    )
    report["expected_fees"] = expected_fees(
        _model_name(report), harness.data_dir, stored["usage"]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 6 F6-06b：训练、安排与更正闭环（真实模型；多 Run）"
    )
    parser.add_argument("--data-dir", required=True, help="本批唯一临时数据/账本目录")
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "phase": "f606_record_loop",
        "data_dir": str(data_dir),
        "ledger_scope_note": (
            "本批账本：仅此 data-dir 内 app.db 的 stage6 行；"
            "换新目录只另起一份账本，不构成新的付费调用授权。"
        ),
        "model_name_env": os.environ.get("MODEL_NAME"),
        "model_base_url_env": os.environ.get("MODEL_BASE_URL"),
        "checks": {
            "s1_view_arrangement": False,
            "s2_arrangement_confirm": False,
            "s3_record_new_and_stats": False,
            "s4_record_correct_same_identity": False,
            "s5_void_terminal": False,
            "s6_recorrect_after_void_rejected": False,
            "s7_plan_arrangement_record_distinct": False,
        },
    }
    try:
        with Harness(data_dir) as harness:
            report["provider"] = harness.ensure_key()
            report["ledger_start"] = ledger_snapshot(harness.db_path)
            run_loop(harness, report)
            report["checks"] = {
                "s1_view_arrangement": bool(
                    report["step1_view_arrangement"]["scheduled_session_id"]
                ),
                "s2_arrangement_confirm": bool(
                    report["step2_arrangement_confirm"]["arrangement_revision_id"]
                ),
                "s3_record_new_and_stats": bool(
                    report["step3_records_after"]["session_in_records"]
                    and report["step3_records_after"]["pr_updated"]
                ),
                "s4_record_correct_same_identity": bool(
                    report["step4_record_correct_confirm"]["same_training_session_id"]
                    and int(
                        report["step4_record_correct_confirm"]["revision_no"] or 0
                    )
                    >= 2
                ),
                "s5_void_terminal": report["step5_records_status"]["current_status"]
                == "voided",
                "s6_recorrect_after_void_rejected": (
                    report["step6_recorrect_rejected"]["http_status"] == 422
                    and report["step6_recorrect_rejected"]["error_code"]
                    == "invalid_request"
                    and report["step6_after_rejection"]["unchanged_after_reject"]
                ),
                "s7_plan_arrangement_record_distinct": report[
                    "step7_identity_separation"
                ]["all_ids_distinct"],
            }
    except Exception as exc:  # noqa: BLE001 - 证据脚本记录脱敏失败原因
        report["error"] = f"{type(exc).__name__}: {exc}"
        log(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    log(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(report["checks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
