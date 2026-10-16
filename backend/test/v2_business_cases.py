"""v2 前置构造、真实状态读取与检查项判定。

复用 p0_business_cases 的纯判定工具（规则、路径解析、状态读取），并扩展：
- 经真实准备→展示→确认保存链建立“已保存操作”前置；
- 经真实运行在指定会话上传附件（跨会话隔离前置）；
- 读取 save_confirmation_operations 账本与检索预算检查。
"""

import asyncio
import json
import sqlite3
from uuid import uuid4

from app.application.business.catalog import Catalog
from app.domain.business.models import BusinessContext
from test.p0_business_cases import (
    _MISSING,
    _resolve_expectation,
    _resolve_value,
)
from test.p0_business_cases import (
    _apply_rule as _p0_apply_rule,
)
from test.p0_business_cases import (
    build_precondition as _p0_build_precondition,
)
from test.p0_business_cases import (
    read_state as _p0_read_state,
)
from test.v2_contract import CheckResult, PreconditionEvidence, V2Case

SNAPSHOT_TABLES = (
    ("profile", "profile_snapshots"),
    ("plan", "plan_snapshots"),
    ("workout", "workout_snapshots"),
)

# 逐请求受保护表：完整内容、身份、归属、状态与绑定全部纳入零写入比较，禁止只比对行数或 ID。
PROTECTED_TABLES = (
    "profile", "profile_snapshots", "profile_save_records",
    "plans", "plan_snapshots", "plan_save_records",
    "workouts", "workout_snapshots", "workout_save_records",
    "save_confirmation_operations",
)


def _protected_records(connection) -> dict:
    # 逐表读取全部列与全部行（PRAGMA 取 schema，避免硬编码漏列），固定排序由 _canonical_state 保证。
    present = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    tables: dict[str, dict] = {}
    missing: list[str] = []
    for name in PROTECTED_TABLES:
        if name not in present:
            missing.append(name)
            tables[name] = {"columns": [], "rows": [], "missing": True}
            continue
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({name})")]
        rows = sorted(
            (dict(row) for row in connection.execute(f"SELECT {', '.join(columns)} FROM {name}")),
            key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False, default=str),
        )
        tables[name] = {"columns": columns, "rows": rows}
    return {"tables": tables, "declared": list(PROTECTED_TABLES), "missing_tables": missing}


def _call(coro):
    from app.interfaces.http import app

    return asyncio.run_coroutine_threadsafe(coro, app.state.loop).result()


def read_state(database_path, session_id: str | None = None) -> dict:
    state = _p0_read_state(database_path)
    connection = sqlite3.connect(str(database_path))
    connection.row_factory = sqlite3.Row
    try:
        operations = [
            {
                "operation_id": row["operation_id"],
                "business_kind": row["business_kind"],
                "proposal_id": row["proposal_id"],
                "display_entry_id": row["display_entry_id"],
                "confirmation_entry_id": row["confirmation_entry_id"],
                "status": row["status"],
                "result": json.loads(row["result"]) if row["result"] else None,
                "error": json.loads(row["error"]) if row["error"] else None,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in connection.execute(
                "SELECT operation_id, business_kind, proposal_id, display_entry_id, "
                "confirmation_entry_id, status, result, error, created_at, updated_at "
                "FROM save_confirmation_operations ORDER BY created_at ASC, operation_id ASC"
            )
        ]
        snapshot_index = {
            row["proposal_id"]: {
                "business_kind": kind,
                "session_id": row["session_id"],
                "display_entry_id": row["display_entry_id"],
                "confirmation_entry_id": row["confirmation_entry_id"],
                "status": row["status"],
            }
            for kind, table in SNAPSHOT_TABLES
            for row in connection.execute(
                f"SELECT proposal_id, session_id, display_entry_id, confirmation_entry_id, status "
                f"FROM {table}"
            )
        }
        protected = _protected_records(connection)
    finally:
        connection.close()
    statuses = {"count": len(operations)}
    for item in operations:
        statuses[f"{item['status']}_count"] = statuses.get(f"{item['status']}_count", 0) + 1
    statuses["items"] = operations
    statuses["last"] = operations[-1] if operations else None
    state["confirmations"] = statuses
    saved = [item for item in operations if item["status"] == "saved"]
    state["saved_operations"] = {
        "count": len(saved),
        "last": saved[-1] if saved else None,
    }
    if session_id:
        # 快照归属按会话区分：跨会话目标用例需要本会话与其他会话的待确认数分别核对。
        for kind, _table in SNAPSHOT_TABLES:
            pending = [
                item for item in snapshot_index.values()
                if item["business_kind"] == kind and item["status"] == "pending"
            ]
            state[f"{kind}_snapshots"]["current_pending_count"] = sum(
                1 for item in pending if item["session_id"] == session_id
            )
            state[f"{kind}_snapshots"]["foreign_pending_count"] = sum(
                1 for item in pending if item["session_id"] != session_id
            )
    state["snapshots_by_proposal"] = snapshot_index
    # 逐请求受保护范围：全库内容级记录，跨会话请求同时保护请求会话与目标归属会话（所有会话均在范围内）。
    sessions = sorted({
        str(row[column])
        for table in protected["tables"].values()
        for row in table["rows"]
        for column in row
        if column == "session_id" and row[column]
    })
    state["protected"] = protected
    state["protected_scope"] = {
        "request_session": session_id,
        "sessions_present": sessions,
        "scope": "全库内容级：请求会话与全部目标归属会话一并纳入零写入比较",
    }
    return state


# ---------- 前置构造 ----------


def _real_turn(http, session_id, *, label, text, attachments=None):
    # 前置构造走真实 Agent 链路：真实 /api/agent/run + SSE，捕获回合用量与准备工具结果。
    from test.v2_eval_support import _prepared, capture_turn

    turn = capture_turn(
        http,
        session_id,
        case_id=f"precondition:{label}",
        round_index=-1,
        step_index=0,
        request_text=text,
        attachments=attachments or [],
    )
    if turn.status != "done":
        raise RuntimeError(f"前置真实回合失败：{label} terminal={turn.terminal} {turn.error or ''}")
    return turn, _prepared(turn.events)


def _attachment_payloads(items):
    import base64

    return [
        {
            "kind": "upload",
            "attachment_id": str(uuid4()),
            "file_name": item["file_name"],
            "data_base64": base64.b64encode(item["text"].encode("utf-8")).decode("ascii"),
        }
        for item in items
    ]


def _confirm_saved(http, session_id, kind, target):
    operation_id = str(uuid4())
    body = {
        "operation_id": operation_id,
        "business_kind": kind,
        "proposal_id": target["proposal_id"],
        "display_entry_id": target["display_entry_id"],
    }
    response = http.post(f"/api/sessions/{session_id}/confirmations", json=body)
    if response.status_code != 200:
        raise RuntimeError(f"前置保存链失败：{kind} {response.status_code} {response.text}")
    return response.json()


def _unique_prepared(prepared, kind, label):
    candidates = [item for item in prepared if item["business_kind"] == kind]
    if len(candidates) != 1:
        raise RuntimeError(f"前置 {label} 未产生唯一 {kind} 快照：{len(candidates)} 个")
    return candidates[0]


def _establish_saved(http, session_id, spec, business_date) -> dict:
    # 真实准备→展示→结构化确认三步链建立已保存操作，逐次登记请求、耗时与真实用量。
    kind = spec["business_kind"]
    label = spec.get("name", kind)
    turn, prepared = _real_turn(http, session_id, label=label, text=spec["prompt"])
    target = _unique_prepared(prepared, kind, label)
    operation = _confirm_saved(http, session_id, kind, target)
    return {
        "business_kind": kind,
        "name": label,
        "operation_id": operation["operation_id"],
        "proposal_id": operation["proposal_id"],
        "display_entry_id": operation["display_entry_id"],
        "confirmation_entry_id": operation["confirmation_entry_id"],
        "status": operation["status"],
        "prepare_tool": target["tool"],
        "run_id": turn.run_id,
        "usage": turn.usage,
        "duration_ms": turn.duration_ms,
    }


def _stage_pending(http, session_id, spec) -> dict:
    # 只准备并完整展示快照，停在等待确认状态，供跨会话目标用例引用真实绑定。
    kind = spec["business_kind"]
    label = spec.get("name", kind)
    turn, prepared = _real_turn(http, session_id, label=label, text=spec["prompt"])
    target = _unique_prepared(prepared, kind, label)
    return {
        "name": label,
        "business_kind": kind,
        "session_id": session_id,
        "proposal_id": target["proposal_id"],
        "display_entry_id": target["display_entry_id"],
        "run_id": turn.run_id,
        "usage": turn.usage,
        "duration_ms": turn.duration_ms,
    }


def _seed_session_run(http, session_ids, spec) -> dict:
    target = session_ids.get(spec["session"])
    if target is None:
        raise RuntimeError(f"前置会话不存在：{spec['session']}")
    attachments = _attachment_payloads(spec.get("attachments", []))
    turn, _prepared = _real_turn(
        http, target, label=spec.get("name", spec["session"]), text=spec["text"], attachments=attachments
    )
    return {
        "session": target,
        "run_id": turn.run_id,
        "usage": turn.usage,
        "duration_ms": turn.duration_ms,
        "attachment_ids": [item["attachment_id"] for item in attachments],
    }


def _precondition_usage(detail) -> dict:
    present = [
        item["usage"]
        for key in ("saved_operations", "cross_session_targets", "session_seed_runs")
        for item in detail.get(key, [])
        if isinstance(item, dict) and item.get("usage")
    ]
    totals = {field: sum(item[field] for item in present) for field in
              ("input", "output", "cache_read", "cache_write", "total_tokens")}
    totals["requests"] = len(present)
    return totals


def build_precondition(
    case: V2Case, *, http, session_id: str, business_context: BusinessContext, live
) -> PreconditionEvidence:
    # 基本业务状态（画像/计划/训练记录/会话）沿用 p0 的隔离库直接构造。
    base = _p0_build_precondition(
        _as_p0_case(case), service=None, business_context=business_context, session_id=session_id
    )
    detail = dict(base.detail)
    declaration = case.precondition or {}
    # 已保存操作前置：真实准备→展示→确认保存链。
    if declaration.get("saved_operations"):
        detail["saved_operations"] = [
            _establish_saved(http, session_id, spec, business_context.business_date)
            for spec in declaration["saved_operations"]
        ]
    # 跨会话目标前置：另一会话内真实准备并完整展示的 pending 快照。
    if declaration.get("cross_session_pending"):
        detail["cross_session_targets"] = [
            _stage_pending(http, detail["sessions"][spec["session"]], spec)
            for spec in declaration["cross_session_pending"]
        ]
    # 跨会话附件：在指定会话的真实运行中上传附件。
    if declaration.get("session_seed_runs"):
        detail["session_seed_runs"] = [
            _seed_session_run(http, detail.get("sessions", {}), spec)
            for spec in declaration["session_seed_runs"]
        ]
    detail["usage"] = _precondition_usage(detail)
    detail["case_id"] = case.case_id
    return PreconditionEvidence(case_id=case.case_id, built=True, detail=detail)


def _as_p0_case(case: V2Case):
    from test.p0_contract import P0Case

    return P0Case(
        case_id=case.case_id,
        category=case.category,
        precondition=case.precondition,
        turns=[],
        checks=[],
        product_basis=case.product_basis,
    )


# ---------- 检索预算与负向确认 ----------


def _tool_records(observation) -> list[dict]:
    records: list[dict] = []
    for turn in observation.turns:
        for call in turn.tool_calls:
            records.append(call.model_dump() if hasattr(call, "model_dump") else dict(call))
    return records


def _observation_text(observation) -> str:
    parts: list[str] = []
    for turn in observation.turns:
        for call in turn.tool_calls:
            parts.append(json.dumps(call.model_dump() if hasattr(call, "model_dump") else call, ensure_ascii=False, default=str))
        for event in turn.events:
            parts.append(json.dumps(event, ensure_ascii=False, default=str))
    return "\n".join(parts)


def _search_budget_ok(observation, expectation) -> tuple[bool, str]:
    from app.application.business.retrieval_response import SEARCH_BUDGET, SEARCH_LIMIT

    details: list[str] = []
    ok = True
    seen = 0
    for call in _tool_records(observation):
        if call.get("name") != "search_exercises" or call.get("is_error"):
            continue
        seen += 1
        text = call.get("content") or ""
        if len(text) > SEARCH_BUDGET:
            ok = False
            details.append(f"字符 {len(text)}>{SEARCH_BUDGET}")
        try:
            payload = json.loads(text)
        except ValueError:
            ok = False
            details.append("非法 JSON")
            continue
        if not isinstance(payload, dict) or "items" not in payload:
            ok = False
            details.append("结构不合法")
            continue
        if len(payload["items"]) > SEARCH_LIMIT:
            ok = False
            details.append(f"候选 {len(payload['items'])}>{SEARCH_LIMIT}")
    if seen == 0:
        return (False, "未观察到成功的 search_exercises 结果")
    return (ok, "; ".join(details) if details else f"{seen} 次检索均合规")


def _details_budget_ok(observation, expectation) -> tuple[bool, str]:
    from app.application.business.retrieval_response import (
        DETAILS_BUDGET,
        DETAILS_LIMIT,
    )

    details: list[str] = []
    ok = True
    seen = 0
    for call in _tool_records(observation):
        if call.get("name") != "get_exercise_details" or call.get("is_error"):
            continue
        seen += 1
        text = call.get("content") or ""
        if len(text) > DETAILS_BUDGET:
            ok = False
            details.append(f"字符 {len(text)}>{DETAILS_BUDGET}")
        try:
            payload = json.loads(text)
        except ValueError:
            ok = False
            details.append("非法 JSON")
            continue
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            ok = False
            details.append("结构不合法")
            continue
        if len(items) > DETAILS_LIMIT:
            ok = False
            details.append(f"条目 {len(items)}>{DETAILS_LIMIT}")
        for item in items:
            identifier = item.get("id") if isinstance(item, dict) else None
            if not isinstance(identifier, str) or not identifier.isdigit():
                ok = False
                details.append(f"动作 ID 非法 {identifier!r}")
    if seen == 0:
        return (False, "未观察到成功的 get_exercise_details 结果")
    return (ok, "; ".join(details) if details else f"{seen} 次详情均合规")


def _last_confirm_rejected(observation, expectation) -> tuple[bool, str]:
    confirmations = observation.confirmations
    if not confirmations:
        return (False, "本轮无确认步骤")
    last = confirmations[-1]
    if last.rejected:
        return (True, f"拒绝 {last.rejection_code}")
    return (False, f"未拒绝：status={last.status_code}")


def _step_index_of(expectation):
    if isinstance(expectation, dict):
        return int(expectation.get("step_index", 0))
    return int(expectation)


def _negative_boundary_ok(observation, expectation) -> tuple[bool, str]:
    # 负向步骤四项断言齐备：端点命中、单条件破坏、受控错误码、零业务写入。
    wanted = _step_index_of(expectation)
    steps = [item for item in observation.confirmations if item.step_index == wanted]
    if not steps:
        return (False, f"步骤 {wanted} 无负向观测记录")
    item = steps[-1]
    if item.rejection_code == "target_unresolved":
        return (False, f"步骤 {wanted} 目标未解析：{item.error}")
    missing = [key for key in ("endpoint_hit", "condition_broken", "controlled_error", "zero_write")
               if not item.assertions.get(key)]
    if missing:
        return (False, f"步骤 {wanted} 断言缺失 {missing}，观测 {item.assertions}")
    return (True, f"步骤 {wanted} 受控拒绝 {item.assertions.get('observed_code')} 且零写入")


def _details_match_candidates(observation, expectation) -> tuple[bool, str]:
    # 详情请求的 ID 必须来自本轮检索返回的候选集合。
    searched: list[str] = []
    requested: list[str] = []
    for call in _tool_records(observation):
        text = call.get("content") or ""
        if call.get("is_error"):
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            continue
        if call.get("name") == "search_exercises" and isinstance(payload, dict):
            searched.extend(str(item.get("id")) for item in payload.get("items", []) if isinstance(item, dict))
        if call.get("name") == "get_exercise_details" and isinstance(payload, dict):
            requested.extend(str(item.get("id")) for item in payload.get("items", []) if isinstance(item, dict))
    if not searched:
        return (False, "本轮无成功检索候选")
    if not requested:
        return (False, "本轮无成功详情结果")
    outside = [item for item in requested if item not in searched]
    if outside:
        return (False, f"详情 ID 超出检索候选：{outside}")
    return (True, f"{len(requested)} 个详情 ID 均来自检索候选")


_OBSERVATION_RULES = {
    "search_budget_ok": _search_budget_ok,
    "details_budget_ok": _details_budget_ok,
    "last_confirm_rejected": _last_confirm_rejected,
    "negative_boundary_ok": _negative_boundary_ok,
    "details_match_candidates": _details_match_candidates,
}

_NULL_TARGET_RULES = frozenset(_OBSERVATION_RULES)


def _needs_observation(rule: str) -> bool:
    return rule in _NULL_TARGET_RULES or rule in {
        "tool_called_successfully",
        "tool_not_called_successfully",
        "output_excludes",
    }


def _apply(rule, value, expectation, observation, catalog, check_target=None):
    if rule in _OBSERVATION_RULES:
        return _OBSERVATION_RULES[rule](observation, expectation)
    if rule == "tool_called_successfully":
        names = expectation if isinstance(expectation, list) else [expectation]
        succeeded = {c.get("name") for c in _tool_records(observation) if not c.get("is_error")}
        missing = [name for name in names if name not in succeeded]
        return (not missing, f"未成功调用 {missing}" if missing else "已成功调用")
    if rule == "tool_not_called_successfully":
        names = expectation if isinstance(expectation, list) else [expectation]
        succeeded = {c.get("name") for c in _tool_records(observation) if not c.get("is_error")}
        hit = [name for name in names if name in succeeded]
        return (not hit, f"出现成功调用 {hit}" if hit else "未出现成功调用")
    if rule == "output_excludes":
        needles = expectation if isinstance(expectation, list) else [expectation]
        text = _observation_text(observation)
        hit = [needle for needle in needles if needle in text]
        return (not hit, f"命中敏感片段 {hit}" if hit else "未出现敏感片段")
    return _p0_apply_rule(_RuleShim(check_target, rule), value, expectation, observation, catalog)


class _RuleShim:
    def __init__(self, target, rule):
        self.target = target
        self.rule = rule


def _step_index(check_id: str) -> int:
    for part in check_id.split("-"):
        if part.startswith("S") and part[1:].isdigit():
            return int(part[1:])
    return 1


def evaluate(case: V2Case, *, observation, service, database_path, business_context) -> list[CheckResult]:
    state = read_state(database_path, observation.session_id)
    catalog = Catalog.load()
    business_date = business_context.business_date
    results: list[CheckResult] = []
    for check in case.checks:
        if _step_index(check.check_id) != observation.step_index:
            continue
        if check.kind == "manual":
            results.append(
                CheckResult(
                    case_id=case.case_id,
                    round_index=observation.round_index,
                    check_id=check.check_id,
                    kind="manual",
                    status="pending_review",
                    reason=check.rule,
                    evidence_refs=[check.target] if check.target else [],
                )
            )
            continue
        value = None
        if not _needs_observation(check.rule):
            value = _resolve_value(check.target, state, business_date)
            if value is _MISSING:
                results.append(
                    CheckResult(
                        case_id=case.case_id,
                        round_index=observation.round_index,
                        check_id=check.check_id,
                        kind="deterministic",
                        status="fail",
                        reason=f"状态路径无法解析：{check.target}",
                        evidence_refs=[check.target or "", f"database={database_path}"],
                    )
                )
                continue
        expectation = _resolve_expectation(check.expectation, business_date)
        try:
            passed, detail = _apply(
                check.rule, value, expectation, observation, catalog, check.target
            )
        except ValueError as error:
            passed, detail = False, str(error)
        results.append(
            CheckResult(
                case_id=case.case_id,
                round_index=observation.round_index,
                check_id=check.check_id,
                kind="deterministic",
                status="pass" if passed else "fail",
                reason=f"{check.rule}: {detail}",
                evidence_refs=[check.target or "observation", f"database={database_path}"],
            )
        )
    return results
