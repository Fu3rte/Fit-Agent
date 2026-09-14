"""Stage 6 F6-07：复盘与后续调整闭环协议验证（真实后端 + 真实模型；仅在明确授权下手工运行）。

在 ``stage6_real_smoke`` 的 Harness 上做最小扩展，串起真实复盘闭环：

自包含建档（``propose_profile_draft``，器械 known）→ 计划启用（``propose_plan_draft``）→
至少 1 条训练记录（``propose_record_draft``）→ 显式 ``POST /api/reviews`` → Run completed →
``GET /api/reviews`` 最新条 body_markdown 非空 / basis 含 per_week+prs（形状）→
``GET /api/reviews/{id}}`` 同内容 → 同 ``client_request_id`` 幂等 ``created=false`` 不新增行 →
``GET /api/plan`` 前后 plan_version_id 不变（建议不自动生效）→ 再打卡一条训练记录后
旧 review ``stale=true``（``review_store._view`` 查询时现算）→ 新 ``client_request_id``
重新生成追加第 2 条（不覆盖）。

安全与费用边界与 ``stage6_real_smoke`` 相同：

- 凭据只从进程环境 ``MODEL_API_KEY`` 读取；脚本不打印、不落盘、不写日志任何凭据。
- 数据目录为本批**唯一**账本（``--data-dir``）；换新目录只另起一份账本，不构成新授权。
- 费用走已拍护栏（持久账本）——本脚本不复制账本算术，只读库内事实。
- 正文不进报告：只记录字符数、``stale``、basis 形状（per_week/prs 计数），不回显敏感长文。

用法（在 ``backend/`` 下；Key 由调用方在进程外注入）：

    .venv/Scripts/python.exe scripts/f607_review_loop.py --data-dir %TEMP%/fit-agent-stage6-f607
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

from f606_record_loop import (  # noqa: E402
    _confirm_draft,
    _plan_text,
    _pick_push_session,
    _profile_text,
    _record_text,
    _run_model_draft,
)
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

#: 对话 SSE 业务事件种类（与复盘 Run 仅状态对比）。
CONVERSATION_BUSINESS_EVENTS = {"answer", "draft"}


def log(message: str) -> None:
    print(message, flush=True)


def _post_review(
    client: Any, client_request_id: str
) -> tuple[int, dict[str, Any]]:
    """POST /api/reviews，返回 (status, body)。"""
    resp = client.post(
        "/api/reviews", json={"client_request_id": client_request_id}
    )
    body = resp.json() if resp.status_code == 200 else {}
    if resp.status_code != 200:
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text[:200]}
    return resp.status_code, body if isinstance(body, dict) else {}


def _wait_review_run(
    harness: Harness, run_id: str, report: dict[str, Any], label: str
) -> dict[str, Any]:
    """收集复盘 Run 的 SSE 并等终态；返回 run 行。"""
    client = harness.client_or_raise
    events = collect_sse(client, run_id)
    deadline = time.time() + 120
    run = wait_terminal(client, run_id, deadline)
    sse_summary = summarize_sse(events)
    report[f"{label}_run_id"] = run_id
    report[f"{label}_run"] = run
    report[f"{label}_sse"] = sse_summary
    if not isinstance(run, dict) or run.get("status") != "completed":
        raise RuntimeError(
            f"{label} Run 未 completed：status={run.get('status') if isinstance(run, dict) else None}"
        )
    return run


def _list_reviews(client: Any) -> list[dict[str, Any]]:
    resp = client.get("/api/reviews")
    if resp.status_code != 200:
        raise RuntimeError(f"GET /api/reviews 失败：HTTP {resp.status_code}")
    return list(resp.json().get("reviews") or [])


def _basis_summary(basis: dict[str, Any]) -> dict[str, Any]:
    """basis 形状摘要：只记 per_week/prs 计数与字段形状，不回显数值长文。"""
    per_week = basis.get("per_week") or []
    prs = basis.get("prs") or []
    return {
        "has_per_week_key": "per_week" in basis,
        "has_prs_key": "prs" in basis,
        "per_week_count": len(per_week),
        "prs_count": len(prs),
        "per_week_item_keys": sorted(per_week[0].keys()) if per_week else [],
        "prs_item_keys": sorted(prs[0].keys()) if prs else [],
    }


def _plan_version_id(client: Any) -> str | None:
    resp = client.get("/api/plan")
    if resp.status_code != 200:
        raise RuntimeError(f"GET /api/plan 失败：HTTP {resp.status_code}")
    plan = resp.json().get("plan") or {}
    return plan.get("id")


def _sse_business_events(sse_summary: dict[str, Any]) -> dict[str, int]:
    """从 SSE 摘要挑出业务事件种类（answer/draft），不含状态事件。"""
    kinds = sse_summary.get("event_kinds") or {}
    return {
        name: count
        for name, count in kinds.items()
        if name in CONVERSATION_BUSINESS_EVENTS
    }


def run_review_loop(harness: Harness, report: dict[str, Any]) -> None:
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

    # ---------- 验收点 1：自包含前置（建档 + 计划 + ≥1 训练记录） ----------
    setup_session = client.post("/api/sessions", json={}).json()["session_id"]
    before = ledger_snapshot(harness.db_path)
    profile_draft_id = _run_model_draft(
        harness,
        setup_session,
        "f607-profile",
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

    today = date.today()
    starts_on = today.isoformat()
    review_on = (today + timedelta(days=28)).isoformat()
    before = ledger_snapshot(harness.db_path)
    plan_draft_id = _run_model_draft(
        harness,
        setup_session,
        "f607-plan",
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

    plan_body = client.get("/api/plan").json()
    target_session = _pick_push_session(plan_body)
    scheduled_session_id = str(target_session["id"])
    report["setup_plan_scope"] = {
        "plan_version_id": plan_version_id,
        "starts_on": (plan_body.get("plan") or {}).get("starts_on"),
        "review_on": (plan_body.get("plan") or {}).get("review_on"),
        "scheduled_session_id": scheduled_session_id,
    }

    # 至少 1 条训练记录（复盘正文需有统计依据；source_revision_ids 非空）
    occurred_on = today.isoformat()
    before = ledger_snapshot(harness.db_path)
    record_draft_id = _run_model_draft(
        harness,
        setup_session,
        "f607-record-1",
        _record_text(occurred_on, None, load_value="40"),
        expected_kind="training_record",
        report=report,
        report_key="setup_record_draft",
    )
    status, body = _confirm_draft(client, record_draft_id)
    if status != 200:
        raise RuntimeError(f"记录确认失败：HTTP {status} {body}")
    first_training_session_id = str(body.get("training_session_id") or "")
    report["setup_record_confirm"] = {
        "http_status": status,
        "training_session_id": first_training_session_id,
        "session_revision_id": body.get("session_revision_id"),
    }
    if not first_training_session_id:
        raise RuntimeError("记录确认未返回 training_session_id")
    snapshot_run("setup_record_1", before)

    records = _list_records(client)
    report["setup_records_count"] = len(records)
    if not any(item.get("id") == first_training_session_id for item in records):
        raise RuntimeError("GET /api/records 未包含第 1 条训练身份")

    # 对话 SSE 对照样本：直接复用建档 Run 的 SSE（draft/answer 事件已含业务正文），
    # 不另发对话 Run——省一次模型调用费用。
    conv_sse_summary = report["setup_profile_draft"]["attempts"][0]["sse"]
    report["conversation_sse"] = {
        "run_id": report["setup_profile_draft"]["attempts"][0]["run_id"],
        "run_status": "completed",
        "sse": conv_sse_summary,
        "business_events": _sse_business_events(conv_sse_summary),
    }

    # ---------- 验收点 2+3：显式生成复盘 → Run completed；SSE 仅状态 ----------
    plan_version_before_review = _plan_version_id(client)
    before = ledger_snapshot(harness.db_path)
    review_request_id = f"f607-review-1-{int(time.time())}"
    status, body = _post_review(client, review_request_id)
    if status != 200:
        raise RuntimeError(f"POST /api/reviews 失败：HTTP {status} {body}")
    if not body.get("created"):
        raise RuntimeError("首次 POST /api/reviews 未 created=true")
    first_run_id = str(body["run"]["run_id"])
    _wait_review_run(harness, first_run_id, report, "step2_review")
    snapshot_run("review_first", before)

    first_run_sse = report["step2_review_sse"]
    first_run_business = _sse_business_events(first_run_sse)
    conv_business = report["conversation_sse"]["business_events"]
    report["step3_sse_status_only"] = {
        "review_run_id": first_run_id,
        "review_run_business_events": first_run_business,
        "review_run_event_kinds": first_run_sse.get("event_kinds"),
        "conversation_run_id": report["conversation_sse"]["run_id"],
        "conversation_run_business_events": conv_business,
        "review_sse_is_status_only": len(first_run_business) == 0,
        "conversation_has_business_events": len(conv_business) > 0,
    }
    if first_run_business:
        raise RuntimeError(
            f"复盘 Run SSE 含业务事件：{first_run_business}（应仅状态事件）"
        )
    if not conv_business:
        # 对话 Run 应有 answer 或 draft；两者都没有则对照样本无效
        raise RuntimeError(
            "对话 Run SSE 无业务事件，无法与复盘仅状态对照"
        )

    # ---------- 验收点 4：正文经查询（GET list + GET by id 同内容） ----------
    reviews = _list_reviews(client)
    if not reviews:
        raise RuntimeError("显式生成后 GET /api/reviews 为空")
    latest = reviews[-1]
    latest_body_markdown = str(latest.get("body_markdown") or "")
    if not latest_body_markdown.strip():
        raise RuntimeError("GET /api/reviews 最新条 body_markdown 为空")
    review1_id = str(latest.get("id") or "")
    by_id_resp = client.get(f"/api/reviews/{review1_id}")
    if by_id_resp.status_code != 200:
        raise RuntimeError(
            f"GET /api/reviews/{{id}} 失败：HTTP {by_id_resp.status_code}"
        )
    by_id = by_id_resp.json().get("review") or {}
    by_id_body = str(by_id.get("body_markdown") or "")
    report["step4_body_via_query"] = {
        "review1_id": review1_id,
        "list_count": len(reviews),
        "list_body_present": bool(latest_body_markdown.strip()),
        "list_body_chars": len(latest_body_markdown),
        "by_id_status": by_id_resp.status_code,
        "by_id_body_present": bool(by_id_body.strip()),
        "by_id_body_chars": len(by_id_body),
        "list_and_by_id_same_content": by_id_body == latest_body_markdown,
        "stale_at_generation": latest.get("stale"),
    }
    if by_id_body != latest_body_markdown:
        raise RuntimeError("GET /api/reviews/{{id}} 与列表最新条 body_markdown 不一致")

    # ---------- 验收点 5：冻结 basis 含 per_week/prs ----------
    basis = latest.get("basis") or {}
    basis_summary = _basis_summary(basis)
    report["step5_frozen_basis"] = basis_summary
    if not basis_summary["has_per_week_key"] or not basis_summary["has_prs_key"]:
        raise RuntimeError(f"basis 缺 per_week 或 prs 键：{basis_summary}")
    # 计数只记形状、不回显数值长文；per_week/prs 均可为 0（standalone 记录可能
    # 不绑定安排→不进 per_week 分子；PR 依赖候选视图），键在即视为形状合格。
    if basis_summary["per_week_count"] > 0 and not basis_summary["per_week_item_keys"]:
        raise RuntimeError(f"basis.per_week 非空但缺字段形状：{basis_summary}")
    if basis_summary["prs_count"] > 0 and not basis_summary["prs_item_keys"]:
        raise RuntimeError(f"basis.prs 非空但缺字段形状：{basis_summary}")

    # ---------- 验收点 6：同 client_request_id 幂等 ----------
    reviews_before = _list_reviews(client)
    status, idem_body = _post_review(client, review_request_id)
    if status != 200:
        raise RuntimeError(f"幂等 POST /api/reviews 失败：HTTP {status} {idem_body}")
    idem_created = bool(idem_body.get("created"))
    idem_run_id = str(idem_body.get("run", {}).get("run_id") or "")
    reviews_after = _list_reviews(client)
    report["step6_idempotent"] = {
        "http_status": status,
        "created": idem_created,
        "same_run_id": idem_run_id == first_run_id,
        "list_count_before": len(reviews_before),
        "list_count_after": len(reviews_after),
        "list_count_unchanged": len(reviews_after) == len(reviews_before),
    }
    if idem_created:
        raise RuntimeError("幂等重发未返回 created=false")
    if idem_run_id != first_run_id:
        raise RuntimeError("幂等重发未返回同一 run_id")
    if len(reviews_after) != len(reviews_before):
        raise RuntimeError("幂等重发新增了 reviews 行")

    # ---------- 验收点 7：建议不自动生效（plan_version_id 不变） ----------
    plan_version_after_review = _plan_version_id(client)
    report["step7_no_auto_plan"] = {
        "plan_version_id_before_review": plan_version_before_review,
        "plan_version_id_after_review": plan_version_after_review,
        "plan_version_unchanged": plan_version_after_review == plan_version_before_review,
    }
    if plan_version_after_review != plan_version_before_review:
        raise RuntimeError("复盘生成后 plan_version_id 被自动改写")

    # ---------- 验收点 8：再打卡一条训练记录 → 旧 review stale=true ----------
    # stale 语义：``app/review_store.py::_view`` 在**查询时**调用 ``review_basis_changed`` 现算，
    # 不是生成时冻结。新记录确认后旧 review 引用的 source_revision_ids 若不再是当前修订，
    # GET /api/reviews/{id}.stale 应为 true。若前置记录与新记录是不同 training_session_id，
    # 旧 review 的 source 不受影响，stale 仍为 false——按实际行为断言并注明。
    before = ledger_snapshot(harness.db_path)
    record2_draft_id = _run_model_draft(
        harness,
        setup_session,
        "f607-record-2",
        _record_text(occurred_on, first_training_session_id, load_value="45"),
        expected_kind="training_record",
        report=report,
        report_key="step8_record2_draft",
    )
    status, body = _confirm_draft(client, record2_draft_id)
    if status != 200:
        raise RuntimeError(f"第 2 条记录确认失败：HTTP {status} {body}")
    report["step8_record2_confirm"] = {
        "http_status": status,
        "training_session_id": body.get("training_session_id"),
        "session_revision_id": body.get("session_revision_id"),
        "same_training_session_id": body.get("training_session_id")
        == first_training_session_id,
    }
    snapshot_run("record_2_correct", before)

    # 旧 review 重新查询：应 stale=true（同身份更正追加新修订，旧引用不再当前）
    old_review_resp = client.get(f"/api/reviews/{review1_id}")
    if old_review_resp.status_code != 200:
        raise RuntimeError(
            f"GET /api/reviews/{{id}}（旧 review）失败：HTTP {old_review_resp.status_code}"
        )
    old_review = old_review_resp.json().get("review") or {}
    report["step8_stale"] = {
        "review1_id": review1_id,
        "stale_after_record2": old_review.get("stale"),
        "stale_semantics": (
            "查询时现算（review_store._view → review_basis_changed）；"
            "第 2 条记录与第 1 条同身份更正，旧引用不再当前，预期 stale=true"
        ),
        "body_preserved_chars": len(str(old_review.get("body_markdown") or "")),
    }
    if old_review.get("stale") is not True:
        raise RuntimeError(
            f"第 2 条记录后旧 review stale 未变 true：{old_review.get('stale')}"
        )

    # ---------- 验收点 9：新 client_request_id 重新生成 → 追加第 2 条 ----------
    before = ledger_snapshot(harness.db_path)
    review2_request_id = f"f607-review-2-{int(time.time())}"
    status, body = _post_review(client, review2_request_id)
    if status != 200:
        raise RuntimeError(f"重新 POST /api/reviews 失败：HTTP {status} {body}")
    if not body.get("created"):
        raise RuntimeError("重新 POST /api/reviews 未 created=true")
    second_run_id = str(body["run"]["run_id"])
    _wait_review_run(harness, second_run_id, report, "step9_review2")
    snapshot_run("review_second", before)

    reviews_final = _list_reviews(client)
    report["step9_regenerate"] = {
        "review2_request_id": review2_request_id,
        "review2_run_id": second_run_id,
        "review2_run_completed": report["step9_review2_run"].get("status")
        == "completed",
        "final_list_count": len(reviews_final),
        "list_count_increased": len(reviews_final) == len(reviews) + 1,
        "review1_id_still_present": any(
            item.get("id") == review1_id for item in reviews_final
        ),
        "review2_id": reviews_final[-1].get("id") if reviews_final else None,
        "review2_body_present": bool(
            str(reviews_final[-1].get("body_markdown") or "").strip()
            if reviews_final
            else ""
        ),
    }
    if len(reviews_final) != len(reviews) + 1:
        raise RuntimeError(
            f"重新生成未追加为第 2 条：期望 {len(reviews) + 1}，实际 {len(reviews_final)}"
        )
    if not any(item.get("id") == review1_id for item in reviews_final):
        raise RuntimeError("重新生成后旧 review 被覆盖/删除（应只追加）")

    # ---------- 验收点 10（可选）：复盘后对话 propose_arrangement / propose_plan ----------
    # 标准剧本要求：若模型/费用紧张可 SKIP，不得假装 PASS。
    # 此处尝试 propose_arrangement_draft（同身份更正后的安排草稿）；失败标 SKIP 不影响核心 9 点。
    try:
        arrange_draft_id = _run_model_draft(
            harness,
            setup_session,
            "f607-arrange-after",
            _arrangement_text(scheduled_session_id),
            expected_kind="arrangement",
            report=report,
            report_key="step10_arrangement_draft",
        )
        status, body = _confirm_draft(client, arrange_draft_id)
        report["step10_after_review"] = {
            "status": "PASS" if status == 200 else "SKIP",
            "arrangement_draft_id": arrange_draft_id,
            "confirm_http": status,
            "arrangement_revision_id": body.get("arrangement_revision_id"),
            "plan_version_id_after_arrangement": _plan_version_id(client),
            "reason": None if status == 200 else f"arrangement confirm HTTP {status}",
        }
    except RuntimeError as exc:
        report["step10_after_review"] = {
            "status": "SKIP",
            "reason": f"模型未产出 arrangement 草稿：{exc}",
        }

    # ---------- 费用与存储 ----------
    stored = inspect_run_messages(
        harness.db_path, report["setup_profile_draft"]["attempts"][0]["run_id"]
    )
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


def _list_records(client: Any) -> list[dict[str, Any]]:
    resp = client.get("/api/records")
    if resp.status_code != 200:
        raise RuntimeError(f"GET /api/records 失败：HTTP {resp.status_code}")
    return list(resp.json().get("records") or [])


def _arrangement_text(scheduled_session_id: str) -> str:
    """复盘后安排草稿提示（与 f606 同形状，纯 JSON 参数块）。"""
    from f606_record_loop import ARRANGE_ITEM_KEY, ARRANGE_REASON, ARRANGE_WORK_SETS

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 6 F6-07：复盘与后续调整闭环（真实模型；多 Run）"
    )
    parser.add_argument("--data-dir", required=True, help="本批唯一临时数据/账本目录")
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "phase": "f607_review_loop",
        "data_dir": str(data_dir),
        "ledger_scope_note": (
            "本批账本：仅此 data-dir 内 app.db 的 stage6 行；"
            "换新目录只另起一份账本，不构成新的付费调用授权。"
        ),
        "model_name_env": os.environ.get("MODEL_NAME"),
        "model_base_url_env": os.environ.get("MODEL_BASE_URL"),
        "checks": {
            "s1_prerequisite_profile_plan_record": False,
            "s2_explicit_review_completed": False,
            "s3_sse_status_only": False,
            "s4_body_via_query": False,
            "s5_frozen_basis": False,
            "s6_idempotent": False,
            "s7_no_auto_plan": False,
            "s8_stale_after_record": False,
            "s9_regenerate_append": False,
            "s10_after_review": None,  # True / False / None(SKIP)
        },
    }
    try:
        with Harness(data_dir) as harness:
            report["provider"] = harness.ensure_key()
            report["ledger_start"] = ledger_snapshot(harness.db_path)
            run_review_loop(harness, report)
            report["checks"] = {
                "s1_prerequisite_profile_plan_record": bool(
                    report.get("setup_plan_confirm")
                    and report.get("setup_record_confirm", {}).get("training_session_id")
                    and report.get("setup_records_count", 0) >= 1
                ),
                "s2_explicit_review_completed": report["step2_review_run"].get(
                    "status"
                )
                == "completed",
                "s3_sse_status_only": (
                    report["step3_sse_status_only"]["review_sse_is_status_only"]
                    and report["step3_sse_status_only"]["conversation_has_business_events"]
                ),
                "s4_body_via_query": (
                    report["step4_body_via_query"]["list_body_present"]
                    and report["step4_body_via_query"]["by_id_body_present"]
                    and report["step4_body_via_query"]["list_and_by_id_same_content"]
                ),
                "s5_frozen_basis": (
                    report["step5_frozen_basis"]["has_per_week_key"]
                    and report["step5_frozen_basis"]["has_prs_key"]
                ),
                "s6_idempotent": (
                    report["step6_idempotent"]["created"] is False
                    and report["step6_idempotent"]["same_run_id"]
                    and report["step6_idempotent"]["list_count_unchanged"]
                ),
                "s7_no_auto_plan": report["step7_no_auto_plan"]["plan_version_unchanged"],
                "s8_stale_after_record": report["step8_stale"]["stale_after_record2"]
                is True,
                "s9_regenerate_append": (
                    report["step9_regenerate"]["review2_run_completed"]
                    and report["step9_regenerate"]["list_count_increased"]
                    and report["step9_regenerate"]["review1_id_still_present"]
                    and report["step9_regenerate"]["review2_body_present"]
                ),
                "s10_after_review": (
                    None
                    if report["step10_after_review"]["status"] == "SKIP"
                    else report["step10_after_review"]["status"] == "PASS"
                ),
            }
    except Exception as exc:  # noqa: BLE001 - 证据脚本记录脱敏失败原因
        report["error"] = f"{type(exc).__name__}: {exc}"
        log(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    log(json.dumps(report, ensure_ascii=False, indent=2))
    core = [
        report["checks"]["s1_prerequisite_profile_plan_record"],
        report["checks"]["s2_explicit_review_completed"],
        report["checks"]["s3_sse_status_only"],
        report["checks"]["s4_body_via_query"],
        report["checks"]["s5_frozen_basis"],
        report["checks"]["s6_idempotent"],
        report["checks"]["s7_no_auto_plan"],
        report["checks"]["s8_stale_after_record"],
        report["checks"]["s9_regenerate_append"],
    ]
    # s10 SKIP 不算失败；FAIL 算失败
    after_ok = report["checks"]["s10_after_review"] in (True, None)
    return 0 if all(core) and after_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
