"""Stage 6 F6-04b：建档闭环协议验证（真实后端 + 真实模型；仅在明确授权下手工运行）。

在 ``stage6_real_smoke`` 的 Harness 基础上做**最小扩展**，串起完整建档闭环：

空库 ``GET /api/profile`` null → 对话工具 Run 产生 ``profile_update`` Pending 草稿 →
确认前 profile 仍 null → ``POST /api/drafts/{id}/revise`` 改 ``weekly_frequency`` →
错 revision confirm → 409 ``draft_modified`` → 正确 confirm → profile 非 null 且含
已确认事实、denied/unknown 三态不伪造 → 再 confirm 同 revision 幂等同凭据、profile 不变。

安全与费用边界与 ``stage6_real_smoke`` 相同：

- 凭据只从进程环境 ``MODEL_API_KEY`` 读取（由调用方在**启动验证进程前**注入其环境）；
  脚本不打印、不落盘、不写日志任何凭据；报告只含存在性与脱敏事实。
- 数据目录为本批**唯一**账本（``--data-dir``）；换新目录只另起一份账本，不构成新授权。

用法（在 ``backend/`` 下；Key 由调用方在进程外注入）：

    .venv/Scripts/python.exe scripts/f604_profile_loop.py --data-dir %TEMP%/fit-agent-stage6-f604b
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(BACKEND / "scripts") not in sys.path:
    sys.path.insert(0, str(BACKEND / "scripts"))

from stage6_real_smoke import (  # noqa: E402
    DRAFT_TEXT,
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

#: 纠错改字段：相对 DRAFT_TEXT 产生的 known 值 3，改为 4（验收点 4）。
REVISED_WEEKLY_FREQUENCY = 4


def log(message: str) -> None:
    print(message, flush=True)


def profile_summary(body: dict[str, Any]) -> dict[str, Any]:
    """``GET /api/profile`` 响应的脱敏摘要：字段级 state/value，不回显模型正文。"""
    profile = body.get("profile")
    facts: dict[str, Any] | None = None
    if isinstance(profile, dict):
        facts = {}
        for name, fact in sorted(profile.items()):
            if not isinstance(fact, dict):
                facts[name] = fact
                continue
            state = fact.get("state")
            value = fact.get("value")
            if state == "known" and isinstance(value, (list, dict)):
                facts[name] = {
                    "state": state,
                    "value_shape": type(value).__name__,
                    "value_len": len(value),
                }
            else:
                facts[name] = {"state": state, "value": value}
    return {
        "context_version": body.get("context_version"),
        "profile_null": profile is None,
        "facts": facts,
    }


def run_profile_loop(harness: Harness, report: dict[str, Any]) -> None:
    client = harness.client
    assert client is not None

    # 验收点 1：空库 profile 为 null
    empty_profile = client.get("/api/profile")
    if empty_profile.status_code != 200:
        raise RuntimeError(f"GET /api/profile 失败：HTTP {empty_profile.status_code}")
    empty_body = empty_profile.json()
    report["step1_empty_profile"] = profile_summary(empty_body)
    if empty_body.get("profile") is not None:
        raise RuntimeError("空库 GET /api/profile 非 null，预期未建档")

    # 对话 + 工具 Run 产生 profile_update 草稿
    session_id = client.post("/api/sessions", json={}).json()["session_id"]
    before = ledger_snapshot(harness.db_path)
    run_id = harness.start_text_run(
        session_id, f"stage6-f604b-draft-{int(time.time())}", DRAFT_TEXT
    )
    events = collect_sse(client, run_id)
    draft_event = None
    for name, data in events:
        if name == "draft":
            draft_event = json.loads(data)
            break
    deadline = time.time() + 120
    run = wait_terminal(client, run_id, deadline)
    if not isinstance(run, dict) or run.get("status") != "completed":
        raise RuntimeError(f"Run 未 completed：{run.get('status')}")
    draft_id = str((draft_event or {}).get("draft_id") or "")
    if not draft_id:
        raise RuntimeError("SSE 未收到 draft 事件，无法继续闭环")
    row = harness.draft_row(draft_id)
    report["session_id"] = session_id
    report["run_id"] = run_id
    report["run"] = run
    report["sse"] = summarize_sse(events)
    report["draft_id"] = draft_id
    report["draft_row_after_terminal"] = row

    # 验收点 2：Pending + kind=profile_update
    if (
        row is None
        or row.get("status") != "pending"
        or row.get("kind") != "profile_update"
    ):
        raise RuntimeError(f"草稿行不符合 Pending profile_update：{row}")

    # 验收点 3：确认前 profile 仍 null
    pre_confirm = client.get("/api/profile")
    if pre_confirm.status_code != 200 or pre_confirm.json().get("profile") is not None:
        raise RuntimeError("确认前 GET /api/profile 非 null")
    report["step3_pre_confirm_profile"] = profile_summary(pre_confirm.json())

    # 读取草稿当前 payload 与 revision
    draft_view = client.get(f"/api/drafts/{draft_id}")
    if draft_view.status_code != 200:
        raise RuntimeError(
            f"GET /api/drafts/{{id}} 失败：HTTP {draft_view.status_code}"
        )
    draft_body = draft_view.json()
    draft_payload = draft_body.get("payload") or {}
    draft_profile = draft_payload.get("profile") or {}
    base_revision = int(draft_body.get("revision") or 0)
    report["draft_view_before_revise"] = {
        "revision": base_revision,
        "status": draft_body.get("status"),
        "kind": draft_body.get("kind"),
        "fact_states": {
            name: (fact or {}).get("state")
            for name, fact in sorted(draft_profile.items())
        },
        "weekly_frequency_before": draft_profile.get("weekly_frequency"),
    }

    # 验收点 4：revise 改 weekly_frequency
    revised_profile = copy.deepcopy(draft_profile)
    revised_profile["weekly_frequency"] = {
        "state": "known",
        "value": REVISED_WEEKLY_FREQUENCY,
    }
    revise_resp = client.post(
        f"/api/drafts/{draft_id}/revise",
        json={"revision": base_revision, "payload": {"profile": revised_profile}},
    )
    if revise_resp.status_code != 200:
        raise RuntimeError(
            f"POST revise 失败：HTTP {revise_resp.status_code} {revise_resp.text[:200]}"
        )
    revised_view = revise_resp.json().get("draft") or {}
    revised_revision = int(revised_view.get("revision") or 0)
    report["step4_revise"] = {
        "http_status": revise_resp.status_code,
        "revision_after": revised_revision,
        "status_after": revised_view.get("status"),
        "weekly_frequency_after": (revised_view.get("payload") or {})
        .get("profile", {})
        .get("weekly_frequency"),
    }

    # 验收点 7：错 revision confirm → 409 draft_modified（在正确 confirm 之前）
    wrong_resp = client.post(
        f"/api/drafts/{draft_id}/confirm", json={"revision": base_revision}
    )
    wrong_body = wrong_resp.json() if wrong_resp.status_code == 409 else {}
    report["step7_wrong_revision_confirm"] = {
        "http_status": wrong_resp.status_code,
        "error_code": wrong_body.get("error_code"),
        "message_present": bool(wrong_body.get("message")),
    }
    if (
        wrong_resp.status_code != 409
        or wrong_body.get("error_code") != "draft_modified"
    ):
        raise RuntimeError(
            f"错 revision confirm 期望 409 draft_modified，实际 "
            f"HTTP {wrong_resp.status_code} code={wrong_body.get('error_code')}"
        )

    # 验收点 5：正确 confirm → 提交凭据；profile 非 null 且含已确认事实
    confirm_resp = client.post(
        f"/api/drafts/{draft_id}/confirm", json={"revision": revised_revision}
    )
    if confirm_resp.status_code != 200:
        raise RuntimeError(
            f"POST confirm 失败：HTTP {confirm_resp.status_code} {confirm_resp.text[:200]}"
        )
    confirm_body = confirm_resp.json()
    profile_after = client.get("/api/profile")
    if profile_after.status_code != 200:
        raise RuntimeError(
            f"确认后 GET /api/profile 失败：HTTP {profile_after.status_code}"
        )
    profile_body = profile_after.json()
    summary_after = profile_summary(profile_body)
    report["step5_confirm"] = {
        "http_status": confirm_resp.status_code,
        "committed_revision": confirm_body.get("committed_revision"),
        "committed_business_version": confirm_body.get("committed_business_version"),
        "status": confirm_body.get("status"),
    }
    report["step5_profile_after_confirm"] = summary_after
    if profile_body.get("profile") is None:
        raise RuntimeError("确认后 GET /api/profile 仍为 null")

    # 验收点 8：denied/unknown 不编造（对照草稿 payload 与正式档案）
    formal_facts = profile_body.get("profile") or {}
    triad_check: dict[str, Any] = {}
    for name, draft_fact in sorted(draft_profile.items()):
        draft_state = (draft_fact or {}).get("state")
        formal_fact = formal_facts.get(name) or {}
        formal_state = formal_fact.get("state")
        formal_value = formal_fact.get("value")
        # 除被 revise 的字段外，state 应与草稿一致；denied/unknown 不得变成 known 带假值
        if name == "weekly_frequency":
            preserved = (
                formal_state == "known" and formal_value == REVISED_WEEKLY_FREQUENCY
            )
            if not preserved:
                raise RuntimeError(f"weekly_frequency 未写入纠错值：{formal_fact}")
        else:
            preserved = formal_state == draft_state
            if draft_state in ("denied", "unknown"):
                if formal_state != draft_state or formal_value is not None:
                    raise RuntimeError(
                        f"字段 {name} 三态被改写：draft={draft_state} formal={formal_fact}"
                    )
        triad_check[name] = {
            "draft_state": draft_state,
            "formal_state": formal_state,
            "formal_value_if_not_known": None
            if formal_state == "known"
            else formal_value,
            "state_preserved": preserved,
        }
    report["step8_triad_check"] = triad_check
    if not all(item.get("state_preserved") for item in triad_check.values()):
        raise RuntimeError("存在字段三态未按草稿保留")

    # 验收点 6：幂等再 confirm 同 revision → 同凭据，profile 不变
    reconfirm = client.post(
        f"/api/drafts/{draft_id}/confirm", json={"revision": revised_revision}
    )
    reconfirm_body = reconfirm.json() if reconfirm.status_code == 200 else {}
    profile_reconfirm = client.get("/api/profile")
    profile_reconfirm_body = (
        profile_reconfirm.json() if profile_reconfirm.status_code == 200 else {}
    )
    report["step6_idempotent_reconfirm"] = {
        "http_status": reconfirm.status_code,
        "same_committed_revision": reconfirm_body.get("committed_revision")
        == confirm_body.get("committed_revision"),
        "same_committed_business_version": reconfirm_body.get(
            "committed_business_version"
        )
        == confirm_body.get("committed_business_version"),
        "profile_context_version_unchanged": profile_reconfirm_body.get(
            "context_version"
        )
        == profile_body.get("context_version"),
        "profile_facts_unchanged": profile_summary(profile_reconfirm_body).get("facts")
        == summary_after.get("facts"),
    }
    idem = report["step6_idempotent_reconfirm"]
    if reconfirm.status_code != 200 or not all(
        [
            idem["same_committed_revision"],
            idem["same_committed_business_version"],
            idem["profile_context_version_unchanged"],
            idem["profile_facts_unchanged"],
        ]
    ):
        raise RuntimeError(f"幂等再确认不符合预期：{idem}")

    # 费用与存储摘要（验收点 9：无 Key；usage/费用只读）
    stored = inspect_run_messages(harness.db_path, run_id)
    after = ledger_snapshot(harness.db_path)
    report["stored"] = stored
    report["ledger_before"] = before
    report["ledger_after"] = after
    report["ledger_run_delta"] = _run_ledger_delta(before, after)
    report["expected_fees"] = expected_fees(
        _model_name(report), harness.data_dir, stored["usage"]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 6 F6-04b：建档闭环协议验证（真实模型；一次 Run）"
    )
    parser.add_argument("--data-dir", required=True, help="本批唯一临时数据/账本目录")
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "phase": "f604_profile_loop",
        "data_dir": str(data_dir),
        "ledger_scope_note": (
            "本批账本：仅此 data-dir 内 app.db 的 stage6 行；"
            "换新目录只另起一份账本，不构成新的付费调用授权。"
        ),
        "model_name_env": os.environ.get("MODEL_NAME"),
        "model_base_url_env": os.environ.get("MODEL_BASE_URL"),
        "checks": {
            "s1_empty_profile_null": False,
            "s2_pending_profile_draft": False,
            "s3_pre_confirm_profile_null": False,
            "s4_revise_weekly_frequency": False,
            "s5_confirm_profile_non_null": False,
            "s6_idempotent_reconfirm": False,
            "s7_wrong_revision_409": False,
            "s8_triad_preserved": False,
        },
    }
    try:
        with Harness(data_dir) as harness:
            report["provider"] = harness.ensure_key()
            report["ledger_start"] = ledger_snapshot(harness.db_path)
            run_profile_loop(harness, report)
            report["checks"] = {
                "s1_empty_profile_null": True,
                "s2_pending_profile_draft": True,
                "s3_pre_confirm_profile_null": True,
                "s4_revise_weekly_frequency": (
                    report["step4_revise"]["weekly_frequency_after"]
                    == {
                        "state": "known",
                        "value": REVISED_WEEKLY_FREQUENCY,
                    }
                ),
                "s5_confirm_profile_non_null": not report[
                    "step5_profile_after_confirm"
                ]["profile_null"],
                "s6_idempotent_reconfirm": all(
                    report["step6_idempotent_reconfirm"][k]
                    for k in (
                        "same_committed_revision",
                        "same_committed_business_version",
                        "profile_context_version_unchanged",
                        "profile_facts_unchanged",
                    )
                ),
                "s7_wrong_revision_409": report["step7_wrong_revision_confirm"][
                    "error_code"
                ]
                == "draft_modified",
                "s8_triad_preserved": all(
                    item.get("state_preserved")
                    for item in report["step8_triad_check"].values()
                ),
            }
    except Exception as exc:  # noqa: BLE001 - 证据脚本记录脱敏失败原因
        report["error"] = f"{type(exc).__name__}: {exc}"
        log(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    log(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(report["checks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
