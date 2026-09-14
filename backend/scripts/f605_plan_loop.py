"""Stage 6 F6-05：计划启用闭环协议验证（真实后端 + 真实模型；仅在明确授权下手工运行）。

在 ``stage6_real_smoke`` 的 Harness 基础上做最小扩展，串起完整计划启用闭环：

空库 ``GET /api/plan`` null → 建档（``profile_update`` Pending → confirm → profile 非 null）→
对话提出计划（``propose_plan_draft``，首次建档不传 ``long_term_adjustment``）→ 草稿
kind=plan Pending → 确认前 plan 仍 null → confirm 计划草稿 → ``GET /api/plan`` 非 null
（含处方与日程）→ ``GET /api/plan/guidance`` 非 null → 记录生效范围摘要。

可选（默认尝试；失败标 SKIP 不假装 PASS）：第二次长期调整
``long_term_adjustment=true`` 路径（生效日=业务日期次日、保留原复核节点、真实条目改动）。

安全与费用边界与 ``stage6_real_smoke`` / ``f604_profile_loop`` 相同：

- 凭据只从进程环境 ``MODEL_API_KEY`` 读取（由调用方在**启动验证进程前**注入其环境）；
  脚本不打印、不落盘、不写日志任何凭据；报告只含存在性与脱敏事实。
- 数据目录为本批**唯一**账本（``--data-dir``）；换新目录只另起一份账本，不构成新授权。
- 处方正文不进报告：只记录条目数、训练日集合、``starts_on``/``review_on`` 与安全字段。

用法（在 ``backend/`` 下；Key 由调用方在进程外注入）：

    .venv/Scripts/python.exe scripts/f605_plan_loop.py --data-dir %TEMP%/fit-agent-stage6-f605
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

#: 建档草稿文本：与 DRAFT_TEXT 同形，但器械改为 known（计划生成 fail-closed：
#: denied/unknown 器械 → 无可用动作 → unschedulable，无法走通 F6-05）。
PROFILE_TEXT = (
    "请调用 propose_profile_draft 工具，直接提出一条待确认档案草稿，不要向我追问。"
    'proposed 参数是八字段对象，每个字段只能是 {"state": ..., "value": ...} 形状，'
    'state 只能是 "unknown"、"denied"、"known" 三种之一，非 known 时 value 必须为 null。'
    "请严格按以下内容传入："
    'training_goal={"state":"known","value":"增肌"}；'
    'training_experience={"state":"known","value":"新手"}；'
    'weekly_frequency={"state":"known","value":3}；'
    'session_duration_minutes={"state":"known","value":60}；'
    'body_weight_kg={"state":"known","value":70}；'
    'available_equipment={"state":"known","value":["杠铃","哑铃","绳索","引体架"]}；'
    'action_restrictions={"state":"denied","value":null}；'
    'body_conditions={"state":"denied","value":null}。'
)

#: 首次计划的模型提示变体：业务日期由脚本算出注入（模型不取系统时钟）。
#: 工具首次实测若未产生草稿，按措辞 2/3 重试（最多 2 次不同措变体）。
def first_plan_texts(starts_on: str, review_on: str) -> list[str]:
    return [
        (
            "请调用 propose_plan_draft 工具，直接提出一条待确认训练计划草稿，不要向我追问，"
            "也不要先建档案（档案已就绪）。"
            f'starts_on 传 "{starts_on}"，review_on 传 "{review_on}"（都是 YYYY-MM-DD）。'
            "这是首次建档后的第一个计划：不要传 long_term_adjustment、proposed_profile、"
            "adjustments、anchor_date。"
        ),
        (
            "用户已明确要求启用训练计划。请立即调用工具 propose_plan_draft 创建待确认计划草稿，"
            "不要输出其他内容、不要追问。参数必须是："
            f'starts_on="{starts_on}"（字符串日期），'
            f'review_on="{review_on}"（字符串日期），'
            "long_term_adjustment 省略或 false，adjustments 与 proposed_profile 省略。"
        ),
        (
            "工具调用指令（只执行一次，不要解释）：propose_plan_draft("
            f'starts_on="{starts_on}", review_on="{review_on}")。'
            "这是首次计划（当前无正式计划）：不需要 long_term_adjustment / adjustments / "
            "proposed_profile。请立刻发起该工具调用来生成 Pending 计划草稿。"
        ),
    ]


def adjustment_text(starts_on: str, review_on: str, item_key: str) -> str:
    """长期调整提示：生效日=业务日期次日、保留原复核节点、真实条目改动（减载）。"""
    return (
        "用户已明确要求调整长期训练计划。请调用 propose_plan_draft 工具创建待确认计划草稿，"
        "不要向我追问。参数必须是："
        f'starts_on="{starts_on}"（业务日期次日，YYYY-MM-DD）；'
        f'review_on="{review_on}"（必须与当前计划复核日完全相同）；'
        "long_term_adjustment=true；"
        f'adjustments 传 [{{"item_key":"{item_key}","disposition":"deload","work_sets":2}}]'
        "（把该动作减载到 2 组）；不要传 proposed_profile、anchor_date。"
        "请立刻发起该工具调用。"
    )


def log(message: str) -> None:
    print(message, flush=True)


def draft_row_event(
    events: list[tuple[str, str]],
) -> tuple[str | None, dict[str, Any] | None]:
    for name, data in events:
        if name == "draft":
            payload = json.loads(data)
            return str(payload.get("draft_id") or "") or None, payload
    return None, None


def run_to_draft(
    harness: Harness,
    session_id: str,
    request_prefix: str,
    text: str,
    report: dict[str, Any],
    label: str,
) -> tuple[str, str, list[tuple[str, str]], dict[str, Any]]:
    """提交一次对话 Run，等到终态；返回 run_id、draft_id、SSE 与 run 行。"""
    client = harness.client_or_raise
    run_id = harness.start_text_run(
        session_id, f"{request_prefix}-{int(time.time())}", text
    )
    events = collect_sse(client, run_id)
    draft_id, draft_event = draft_row_event(events)
    deadline = time.time() + 180
    run = wait_terminal(client, run_id, deadline)
    if not isinstance(run, dict) or run.get("status") != "completed":
        raise RuntimeError(f"{label} Run 未 completed：{run.get('status')}")
    if not draft_id:
        raise RuntimeError(f"{label}：SSE 未收到 draft 事件，无法继续闭环")
    report[f"{label}_run_id"] = run_id
    report[f"{label}_sse"] = summarize_sse(events)
    report[f"{label}_draft_id"] = draft_id
    report[f"{label}_draft_row"] = harness.draft_row(draft_id)
    return run_id, draft_id, events, run


def confirm_draft(harness: Harness, draft_id: str, report: dict[str, Any], label: str) -> dict[str, Any]:
    client = harness.client_or_raise
    view = client.get(f"/api/drafts/{draft_id}")
    if view.status_code != 200:
        raise RuntimeError(f"{label}: GET /api/drafts/{{id}} HTTP {view.status_code}")
    body = view.json()
    revision = int(body.get("revision") or 0)
    confirm = client.post(
        f"/api/drafts/{draft_id}/confirm", json={"revision": revision}
    )
    if confirm.status_code != 200:
        raise RuntimeError(
            f"{label}: POST confirm HTTP {confirm.status_code} {confirm.text[:200]}"
        )
    confirm_body = confirm.json()
    report[f"{label}_confirm"] = {
        "http_status": confirm.status_code,
        "committed_revision": confirm_body.get("committed_revision"),
        "committed_business_version": confirm_body.get("committed_business_version"),
        "status": confirm_body.get("status"),
        "plan_version_id": confirm_body.get("plan_version_id")
        or (confirm_body.get("plan") or {}).get("id"),
    }
    return confirm_body


def plan_scope_summary(plan: dict[str, Any]) -> dict[str, Any]:
    """生效范围摘要：不打印完整处方正文，只给结构化事实。"""
    payload = plan.get("plan") or {}
    workouts = payload.get("plan_workouts") or []
    schedules = plan.get("schedules") or []
    workout_keys = [w.get("workout_key") for w in workouts]
    item_counts = {w.get("workout_key"): len(w.get("exercises") or []) for w in workouts}
    return {
        "id": plan.get("id"),
        "version": plan.get("version"),
        "mode": plan.get("mode"),
        "is_current": plan.get("is_current"),
        "starts_on": plan.get("starts_on"),
        "review_on": plan.get("review_on"),
        "template_key": plan.get("template_key") or payload.get("template_key"),
        "workout_keys": workout_keys,
        "item_counts_by_workout": item_counts,
        "total_exercise_items": sum(item_counts.values()),
        "schedule_entry_count": len(schedules),
        "training_days": sorted({s.get("scheduled_on") for s in schedules if s.get("scheduled_on")}),
        "schedule_statuses": {
            status: sum(1 for s in schedules if s.get("status") == status)
            for status in sorted({str(s.get("status")) for s in schedules})
        },
    }


def guidance_safety_summary(guidance: dict[str, Any]) -> dict[str, Any]:
    """安全字段按响应形状记录：bool / 稳定 code / 冲突计数；不回显 reasons 原文。"""
    safety = guidance.get("safety") or {}
    conflicts = safety.get("conflicts") or []
    return {
        "usable": safety.get("usable"),
        "red_flag_blocked": safety.get("red_flag_blocked"),
        "conflict_count": len(conflicts),
        "action_unavailable": safety.get("action_unavailable"),
        "block_code": safety.get("block_code"),
        "unknown_exercise_id_count": len(safety.get("unknown_exercise_ids") or []),
        "reason_count": len(safety.get("reasons") or []),
        "clarification_count": len(safety.get("clarifications") or []),
        "context_version": safety.get("context_version"),
    }


def run_plan_loop(harness: Harness, report: dict[str, Any], *, skip_adjustment: bool) -> None:
    client = harness.client_or_raise

    # 验收点 1：空库 plan / guidance 均为 null
    empty_plan = client.get("/api/plan")
    empty_guidance = client.get("/api/plan/guidance")
    if empty_plan.status_code != 200 or empty_plan.json().get("plan") is not None:
        raise RuntimeError("空库 GET /api/plan 非 null，预期无正式计划")
    if (
        empty_guidance.status_code != 200
        or empty_guidance.json().get("guidance") is not None
    ):
        raise RuntimeError("空库 GET /api/plan/guidance 非 null")
    report["step1_empty"] = {
        "plan_null": True,
        "guidance_null": True,
    }

    # ---- 建档（复用 f604 路径的最小子集：草稿 → confirm → profile 非 null）----
    session_id = client.post("/api/sessions", json={}).json()["session_id"]
    report["profile_session_id"] = session_id
    run_to_draft(
        harness,
        session_id,
        "stage6-f605-profile",
        PROFILE_TEXT,
        report,
        "profile_draft",
    )
    profile_draft_id = report["profile_draft_draft_id"]
    profile_row = report["profile_draft_draft_row"] or {}
    if profile_row.get("kind") != "profile_update" or profile_row.get("status") != "pending":
        raise RuntimeError(f"建档草稿不符合 Pending profile_update：{profile_row}")
    confirm_draft(harness, profile_draft_id, report, "profile")
    profile_after = client.get("/api/profile")
    if profile_after.status_code != 200 or profile_after.json().get("profile") is None:
        raise RuntimeError("建档确认后 GET /api/plan 前 profile 仍为 null")
    report["profile_non_null"] = True

    # 验收点 2：对话提出计划（首次；多次措辞重试）
    business_today = date.today()
    starts_on = (business_today + timedelta(days=1)).isoformat()
    review_on = (business_today + timedelta(days=29)).isoformat()
    plan_session_id = client.post("/api/sessions", json={}).json()["session_id"]
    report["plan_session_id"] = plan_session_id
    report["plan_dates"] = {
        "business_today": business_today.isoformat(),
        "starts_on": starts_on,
        "review_on": review_on,
    }
    attempt_labels: list[str] = []
    plan_draft_id: str | None = None
    last_error = ""
    texts = first_plan_texts(starts_on, review_on)
    for index, text in enumerate(texts):
        label = f"plan_draft_try{index + 1}"
        attempt_labels.append(label)
        session_for_try = (
            plan_session_id
            if index == 0
            else client.post("/api/sessions", json={}).json()["session_id"]
        )
        try:
            run_to_draft(
                harness,
                session_for_try,
                f"stage6-f605-plan-{index + 1}",
                text,
                report,
                label,
            )
            plan_draft_id = report[f"{label}_draft_id"]
            row = report[f"{label}_draft_row"] or {}
            if row.get("kind") == "plan" and row.get("status") == "pending":
                break
            last_error = f"草稿行不是 Pending plan：{row}"
            plan_draft_id = None
        except RuntimeError as exc:
            last_error = str(exc)
            plan_draft_id = None
            # 模型未按工具调用产生 plan 草稿：换措辞重试（最多 2 次）
            continue
    report["plan_draft_attempts"] = {
        "labels": attempt_labels,
        "draft_id": plan_draft_id,
        "last_error": last_error if plan_draft_id is None else None,
    }
    if not plan_draft_id:
        raise RuntimeError(
            f"三次措辞均未产生 Pending plan 草稿；最后一次错误：{last_error}"
        )

    # 验收点 3：确认前 plan 仍 null
    pre_confirm = client.get("/api/plan")
    if pre_confirm.status_code != 200 or pre_confirm.json().get("plan") is not None:
        raise RuntimeError("确认计划草稿前 GET /api/plan 非 null")
    report["step3_pre_confirm_plan_null"] = True

    # 草稿生效范围（只读形状，不打印处方正文）
    draft_view = client.get(f"/api/drafts/{plan_draft_id}").json()
    draft_payload = draft_view.get("payload") or {}
    report["plan_draft_scope"] = {
        "revision": draft_view.get("revision"),
        "kind": draft_view.get("kind"),
        "starts_on": draft_payload.get("starts_on"),
        "review_on": draft_payload.get("review_on"),
        "workout_keys": [
            w.get("workout_key")
            for w in (draft_payload.get("plan") or {}).get("plan_workouts") or []
        ],
        "schedule_preview_count": len(draft_payload.get("schedules") or []),
    }

    # 验收点 4：confirm 计划草稿 → plan 非 null
    confirm_body = confirm_draft(harness, plan_draft_id, report, "plan")
    plan_after = client.get("/api/plan")
    if plan_after.status_code != 200:
        raise RuntimeError(f"确认后 GET /api/plan HTTP {plan_after.status_code}")
    plan_obj = plan_after.json().get("plan")
    if plan_obj is None:
        raise RuntimeError("确认后 GET /api/plan 仍为 null")
    scope = plan_scope_summary(plan_obj)
    report["step4_plan_after_confirm"] = scope
    if not scope["workout_keys"] or scope["schedule_entry_count"] <= 0:
        raise RuntimeError(f"确认后计划缺少处方或日程：{scope}")
    if not scope["is_current"]:
        raise RuntimeError(f"确认后计划 is_current 非 true：{scope}")

    # 验收点 5：guidance 非 null + 安全字段
    guidance_resp = client.get("/api/plan/guidance")
    if guidance_resp.status_code != 200:
        raise RuntimeError(f"GET /api/plan/guidance HTTP {guidance_resp.status_code}")
    guidance = guidance_resp.json().get("guidance")
    if guidance is None:
        raise RuntimeError("有正式计划时 GET /api/plan/guidance 仍为 null")
    safety = guidance_safety_summary(guidance)
    report["step5_guidance"] = {
        "guidance_non_null": True,
        "safety": safety,
        "plan_scope": plan_scope_summary(guidance.get("plan") or {}),
    }
    if safety.get("usable") is not True:
        raise RuntimeError(f"guidance.safety.usable 非 true：{safety}")

    # 验收点 6：生效范围（与 step4 同源；显式落报告键）
    report["step6_effective_scope"] = {
        "starts_on": scope["starts_on"],
        "review_on": scope["review_on"],
        "schedule_entry_count": scope["schedule_entry_count"],
        "training_day_count": len(scope["training_days"]),
        "workout_keys": scope["workout_keys"],
    }

    # 验收点 7（可选）：第二次长期调整
    if skip_adjustment:
        report["step7_long_term_adjustment"] = {
            "status": "SKIP",
            "reason": "调用方通过 --skip-adjustment 显式跳过",
        }
    else:
        run_optional_adjustment(harness, report, starts_on_from_plan=scope, business_today=business_today)


def run_optional_adjustment(
    harness: Harness,
    report: dict[str, Any],
    *,
    starts_on_from_plan: dict[str, Any],
    business_today: date,
) -> None:
    """可选路径：long_term_adjustment=true；失败标 SKIP/FAIL 细节，不假装 PASS。"""
    client = harness.client_or_raise
    adj_starts = (business_today + timedelta(days=1)).isoformat()
    adj_review = str(starts_on_from_plan.get("review_on") or "")
    # 取当前计划首个 item_key 作为真实改动锚点
    plan_obj = client.get("/api/plan").json().get("plan") or {}
    workouts = (plan_obj.get("plan") or {}).get("plan_workouts") or []
    item_key = ""
    for workout in workouts:
        exercises = workout.get("exercises") or []
        if exercises:
            item_key = str(exercises[0].get("item_key") or "")
            break
    if not item_key:
        report["step7_long_term_adjustment"] = {
            "status": "SKIP",
            "reason": "当前计划无可读 item_key，无法构造真实条目改动",
        }
        return

    adj_session = client.post("/api/sessions", json={}).json()["session_id"]
    before_ledger = ledger_snapshot(harness.db_path)
    run_id = harness.start_text_run(
        adj_session,
        f"stage6-f605-adj-{int(time.time())}",
        adjustment_text(adj_starts, adj_review, item_key),
    )
    events = collect_sse(client, run_id)
    draft_id, _ = draft_row_event(events)
    deadline = time.time() + 180
    run = wait_terminal(client, run_id, deadline)
    stored = inspect_run_messages(harness.db_path, run_id)
    after_ledger = ledger_snapshot(harness.db_path)
    report["step7_run"] = {
        "run_id": run_id,
        "status": (run or {}).get("status"),
        "sse": summarize_sse(events),
        "stored": stored,
        "ledger_run_delta": _run_ledger_delta(before_ledger, after_ledger),
    }
    if (run or {}).get("status") != "completed":
        # 可选路径：基础设施/模型请求失败不假装 PASS，标 SKIP（核心验收 1–6 已独立成立）
        report["step7_long_term_adjustment"] = {
            "status": "SKIP",
            "reason": (
                f"调整 Run 未 completed：status={(run or {}).get('status')} "
                f"error_code={run.get('error_code') if isinstance(run, dict) else None}"
            ),
        }
        return
    if not draft_id:
        report["step7_long_term_adjustment"] = {
            "status": "SKIP",
            "reason": "调整 Run 完成但未收到 plan 草稿事件（模型未按工具调用）",
        }
        return
    row = harness.draft_row(draft_id) or {}
    if row.get("kind") != "plan" or row.get("status") != "pending":
        report["step7_long_term_adjustment"] = {
            "status": "SKIP",
            "reason": f"调整草稿不是 Pending plan：{row}",
            "draft_id": draft_id,
        }
        return
    confirm_draft(harness, draft_id, report, "adjustment")
    plan_after = client.get("/api/plan")
    plan_obj2 = plan_after.json().get("plan") or {}
    scope2 = plan_scope_summary(plan_obj2)
    report["step7_long_term_adjustment"] = {
        "status": "PASS",
        "draft_id": draft_id,
        "adjustment_item_key": item_key,
        "starts_on": scope2.get("starts_on"),
        "review_on": scope2.get("review_on"),
        "version": scope2.get("version"),
        "schedule_entry_count": scope2.get("schedule_entry_count"),
        "review_on_preserved": scope2.get("review_on") == adj_review,
        "starts_on_is_business_plus_one": scope2.get("starts_on") == adj_starts,
    }
    if not report["step7_long_term_adjustment"]["review_on_preserved"]:
        report["step7_long_term_adjustment"]["status"] = "FAIL"
        report["step7_long_term_adjustment"]["reason"] = "复核节点未保留"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 6 F6-05：计划启用闭环协议验证（真实模型）"
    )
    parser.add_argument("--data-dir", required=True, help="本批唯一临时数据/账本目录")
    parser.add_argument(
        "--skip-adjustment",
        action="store_true",
        help="跳过可选的第二次长期调整（报告标 SKIP）",
    )
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "phase": "f605_plan_loop",
        "data_dir": str(data_dir),
        "ledger_scope_note": (
            "本批账本：仅此 data-dir 内 app.db 的 stage6 行；"
            "换新目录只另起一份账本，不构成新的付费调用授权。"
        ),
        "model_name_env": os.environ.get("MODEL_NAME"),
        "model_base_url_env": os.environ.get("MODEL_BASE_URL"),
        "checks": {
            "s1_empty_plan_null": False,
            "s2_pending_plan_draft": False,
            "s3_pre_confirm_plan_null": False,
            "s4_confirm_plan_non_null": False,
            "s5_guidance_non_null": False,
            "s6_effective_scope": False,
            "s7_long_term_adjustment": None,  # True / False / None(SKIP)
        },
    }
    try:
        with Harness(data_dir) as harness:
            report["provider"] = harness.ensure_key()
            report["ledger_start"] = ledger_snapshot(harness.db_path)
            run_plan_loop(harness, report, skip_adjustment=args.skip_adjustment)
            scope = report["step4_plan_after_confirm"]
            adj = report.get("step7_long_term_adjustment") or {}
            adj_status = adj.get("status")
            report["checks"] = {
                "s1_empty_plan_null": True,
                "s2_pending_plan_draft": True,
                "s3_pre_confirm_plan_null": True,
                "s4_confirm_plan_non_null": bool(
                    scope.get("workout_keys") and scope.get("schedule_entry_count")
                ),
                "s5_guidance_non_null": report["step5_guidance"]["guidance_non_null"]
                and report["step5_guidance"]["safety"].get("usable") is True,
                "s6_effective_scope": bool(
                    scope.get("starts_on")
                    and scope.get("review_on")
                    and scope.get("schedule_entry_count")
                ),
                "s7_long_term_adjustment": (
                    None if adj_status == "SKIP" else adj_status == "PASS"
                ),
            }
            # 费用摘要（只读账本；不复制账本算术）
            report["ledger_end"] = ledger_snapshot(harness.db_path)
            if report.get("ledger_start") and report.get("ledger_end"):
                report["ledger_total_delta"] = _run_ledger_delta(
                    report["ledger_start"], report["ledger_end"]
                )
    except Exception as exc:  # noqa: BLE001 - 证据脚本记录脱敏失败原因
        report["error"] = f"{type(exc).__name__}: {exc}"
        log(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    log(json.dumps(report, ensure_ascii=False, indent=2))
    core = [
        report["checks"]["s1_empty_plan_null"],
        report["checks"]["s2_pending_plan_draft"],
        report["checks"]["s3_pre_confirm_plan_null"],
        report["checks"]["s4_confirm_plan_non_null"],
        report["checks"]["s5_guidance_non_null"],
        report["checks"]["s6_effective_scope"],
    ]
    # s7 SKIP 不算失败；FAIL 算失败
    adj_ok = report["checks"]["s7_long_term_adjustment"] in (True, None)
    return 0 if all(core) and adj_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
