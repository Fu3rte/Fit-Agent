# 运行：在 backend 执行 python -m test.v2_trace_reconcile --experiment-id <id>
# 逐槽位把该槽隔离 Trace 库与评测器记录的业务观测对齐，输出机器可核对的链路完整性证据。
import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from app.agent.telemetry import ROOT_SPAN_NAMES, SpanName, SpanRecord, SpanStatus
from app.agent.trace_report import _distribution, _readonly_uri
from test.v2_contract import CASE_ID_PREFIXES, RoundObservation

REPO_ROOT = Path(__file__).resolve().parents[1].parent
USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
CLIENT_FIELDS = ("input", "output", "cache_read", "cache_write")
CALL_KINDS = ("normal", "history_summary", "turn_prefix_summary", None)
CONFIRM_POST_KINDS = ("confirm", "confirm_repeat", "confirm_negative")
# detail 中只有这三类前置会跑真实 Agent：saved_operations 在本槽会话内执行，故会话字段取 None。
PRECONDITION_RUN_KEYS = (("saved_operations", None), ("cross_session_targets", "session_id"), ("session_seed_runs", "session"))
EXPECTED_BASIS = {"scenario": "本槽 round.json 中 kind=user 回合的 run_id",
                  "precondition": "本槽 precondition.json detail 中真实前置运行的 run_id",
                  "agent_endpoint": "确认观测里派发到 /api/agent/* 且响应回传 run_id 的运行（edit 与授权注入路径）"}
DEFINITIONS = {
    "slot_complete": "结构违规、身份关联错误、期望根缺失、无法归因的根、确认根数量偏差全部为 0 时判定链路完整；耗时与用量缺样单列，不参与该判定。",
    "agent_run.groups": "expected 为该组推导出的期望根数，actual 为在库中命中数，group.extra 与 agent_run.unattributed 同一口径（无法归因到任何期望组的根）。",
    "confirmation.expected_execution_roots": "按受理/执行边界推导，分 scenario 与 precondition 两组：scenario 取本槽确认步骤中 POST 会话确认端点、status_code 为 200 且该 operation_id 首次受理执行的操作；precondition 取 precondition.detail.saved_operations 内真实前置保存链受理执行的 operation_id。同键复用、受理事务前拒绝、GET 查询、edit 期望零新根。",
    "trace_completeness_rate": "分母取 claims/*.json 记录数（缺失时退回发现的槽位数），分子取链路完整槽位数，与业务通过率分列上报。",
    "trace_db_missing": "槽位只有 error.json 且 Trace 库不存在时记为缺证据并继续汇总；已交付 round.json 的槽位缺库直接失败退出。",
}


def _issue(kind: str, trace_id: str, span_id, detail: str) -> dict:
    return {"kind": kind, "trace_id": trace_id, "span_id": span_id, "detail": detail}


def _rate(numerator: int, denominator: int):
    return (numerator / denominator) if denominator else None


async def _read_spans(path: Path) -> list[SpanRecord]:
    # 逐槽位显式打开该槽 Trace 库：collect_metrics 只读默认路径，此处沿用同一只读连接方式。
    connection = await aiosqlite.connect(_readonly_uri(path.resolve()), uri=True, isolation_level=None)
    connection.row_factory = aiosqlite.Row
    try:
        cursor = await connection.execute(
            "SELECT trace_id, span_id, parent_span_id, session_id, run_id, operation_id, name, started_at,"
            " finished_at, duration_ms, status, error_code, attributes FROM spans ORDER BY trace_id, span_id"
        )
        try:
            rows = await cursor.fetchall()
        finally:
            await cursor.close()
    finally:
        await connection.close()
    return [SpanRecord.model_validate({**dict(row), "attributes": json.loads(row["attributes"])}) for row in rows]


def _validate_chains(records: list[SpanRecord]) -> dict:
    traces: dict[str, list[SpanRecord]] = {}
    for record in records:
        traces.setdefault(record.trace_id, []).append(record)
    violations, roots = [], []
    for trace_id, spans in sorted(traces.items()):
        by_id: dict[int, SpanRecord] = {}
        for span in spans:
            if span.span_id in by_id:
                violations.append(_issue("duplicate_span_id", trace_id, span.span_id, "同 Trace 内 span_id 重复"))
            by_id[span.span_id] = span
        trace_roots = [span for span in spans if span.parent_span_id is None]
        if len(trace_roots) != 1:
            violations.append(_issue("root_count", trace_id, None, f"根 Span 数量 {len(trace_roots)}，要求恰好 1"))
        for span in trace_roots:
            if span.name.value not in ROOT_SPAN_NAMES:
                violations.append(_issue("root_kind", trace_id, span.span_id, f"非法根类型 {span.name.value}"))
            elif (span.name is SpanName.agent_run) != (span.run_id is not None):
                violations.append(_issue("root_identity", trace_id, span.span_id, "根类型与 run_id 携带规则不符"))
            roots.append(span)
        for span in spans:
            if span.parent_span_id is None:
                continue
            parent = by_id.get(span.parent_span_id)
            if parent is None:
                violations.append(_issue("missing_parent", trace_id, span.span_id, f"父 Span {span.parent_span_id} 不在同 Trace"))
            elif (span.session_id, span.run_id, span.operation_id) != (parent.session_id, parent.run_id, parent.operation_id):
                violations.append(_issue("identity_mismatch", trace_id, span.span_id, "子 Span 业务身份与父 Span 不一致"))
            # 逐级上溯判定成环：同一 Span 二次入链或链长超过本 Trace Span 数量即视为环。
            chain, current = {span.span_id}, span
            while current.parent_span_id is not None and (current := by_id.get(current.parent_span_id)) is not None:
                if current.span_id in chain or len(chain) > len(spans):
                    violations.append(_issue("cycle", trace_id, span.span_id, f"父链成环：{sorted(chain)}"))
                    break
                chain.add(current.span_id)
    return {"traces": len(traces), "spans": len(records), "roots": roots, "violations": violations}


def _expected_agent_runs(observation: RoundObservation, detail: dict) -> dict[str, list[dict]]:
    scenario = [
        {"run_id": turn.run_id, "session_id": turn.session_id, "operation_id": turn.operation_id,
         "source": f"turns[{index}] step {turn.step_index}"}
        for index, turn in enumerate(observation.turns) if turn.kind == "user" and turn.run_id
    ]
    precondition = []
    for key, session_field in PRECONDITION_RUN_KEYS:
        items = detail.get(key) or []
        if not isinstance(items, list):
            raise ValueError(f"precondition.detail.{key} 结构非法")
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f"precondition.detail.{key}[{index}] 结构非法")
            if item.get("run_id"):
                session_id = observation.session_id if session_field is None else str(item.get(session_field) or "")
                precondition.append({"run_id": str(item["run_id"]), "session_id": session_id, "operation_id": None,
                                     "source": f"precondition.detail.{key}[{index}]"})
    derived, claimed = [], {item["run_id"] for item in scenario + precondition}
    for step in observation.confirmations:
        endpoint, response = step.endpoint or "", (step.response if isinstance(step.response, dict) else {})
        accepted = endpoint.startswith("/api/agent") and step.status_code in (200, 201) and response.get("run_id")
        run_id = str(response["run_id"]) if accepted else None
        if run_id is None or run_id in claimed:
            continue
        claimed.add(run_id)
        derived.append({"run_id": run_id, "session_id": observation.session_id, "operation_id": None,
                        "source": f"confirmations[{step.step_index}] {step.kind} -> {endpoint}"})
    return {"scenario": scenario, "precondition": precondition, "agent_endpoint": derived}


def _agent_runs(groups: dict[str, list[dict]], agent_roots: list[SpanRecord]) -> dict:
    by_run, duplicates = {}, []
    for root in agent_roots:
        if root.run_id in by_run:
            duplicates.append(root.run_id)
        else:
            by_run[root.run_id] = root
    attributed = {entry["run_id"] for entries in groups.values() for entry in entries}
    unattributed = [
        {"run_id": root.run_id, "trace_id": root.trace_id, "root_span_id": root.span_id, "session_id": root.session_id}
        for root in agent_roots if root.run_id not in attributed
    ]
    reports = {}
    for name, entries in groups.items():
        found, missing, association = 0, [], []
        for entry in entries:
            root = by_run.get(entry["run_id"])
            if root is None:
                missing.append(entry)
                continue
            found += 1
            errors = []
            if entry["session_id"] and root.session_id != entry["session_id"]:
                errors.append(f"会话身份不符：期望 {entry['session_id']} 实际 {root.session_id}")
            if entry.get("operation_id") and root.operation_id != entry["operation_id"]:
                errors.append(f"操作身份不符：期望 {entry['operation_id']} 实际 {root.operation_id}")
            if errors:
                association.append({"run_id": entry["run_id"], "trace_id": root.trace_id,
                                    "root_span_id": root.span_id, "source": entry["source"], "errors": errors})
        reports[name] = {"expected_basis": EXPECTED_BASIS[name], "expected": len(entries), "actual": found,
                         "missing": missing, "association_errors": association, "extra": unattributed}
    return {"groups": reports, "unattributed": unattributed, "duplicate_run_ids": duplicates}


def _scenario_confirmation_roots(observation: RoundObservation) -> tuple[dict, dict]:
    accepted, posts = {}, {}
    zero = {"reused_operation": 0, "rejected": 0, "unresolved": 0, "operation_query": 0, "edit": 0}
    for step in sorted(observation.confirmations, key=lambda item: item.step_index):
        endpoint = step.endpoint or ""
        if step.kind not in CONFIRM_POST_KINDS:
            zero["operation_query" if step.kind == "operation_query" else "edit"] += 1
            continue
        # 派发到确认端点：显式 endpoint 命中，或早期证据未落 endpoint 时按请求体身份推断。
        if (endpoint and not endpoint.endswith("/confirmations")) or (not endpoint and not step.request.get("proposal_id")):
            continue
        if step.rejected or step.status_code != 200:
            zero["unresolved" if step.rejection_code == "target_unresolved" else "rejected"] += 1
            continue
        posts[step.operation_id] = posts.get(step.operation_id, 0) + 1
        if step.operation_id in accepted:
            zero["reused_operation"] += 1
            continue
        accepted[step.operation_id] = {"operation_id": step.operation_id, "business_kind": step.business_kind,
                                       "session_id": observation.session_id, "step_index": step.step_index,
                                       "kind": step.kind, "source": f"confirmations[{step.step_index}]"}
    return accepted, {"zero_root_steps": zero, "posts_per_operation": posts}


def _precondition_confirmation_roots(detail: dict, session_id: str) -> dict:
    # 真实前置保存链在本槽会话内受理执行，产生的 confirmation 根属于独立期望分组。
    entries: dict[str, dict] = {}
    for index, item in enumerate((detail or {}).get("saved_operations") or []):
        if not isinstance(item, dict):
            raise ValueError(f"precondition.detail.saved_operations[{index}] 结构非法")
        operation_id = item.get("operation_id")
        if not operation_id:
            continue
        entries[str(operation_id)] = {
            "operation_id": str(operation_id),
            "business_kind": item.get("business_kind"),
            "session_id": str(item.get("session_id") or session_id or ""),
            "proposal_id": item.get("proposal_id"),
            "source": f"precondition.detail.saved_operations[{index}]",
        }
    return entries


def _confirmations(observation: RoundObservation, confirm_roots: list[SpanRecord], detail: dict) -> dict:
    scenario, stats = _scenario_confirmation_roots(observation)
    precondition = _precondition_confirmation_roots(detail, observation.session_id)
    overlap = sorted(set(scenario) & set(precondition))
    if overlap:
        raise ValueError(f"前置保存操作与场景确认步骤身份重叠：{overlap}")
    expected = {**precondition, **scenario}
    by_operation: dict[str, list[SpanRecord]] = {}
    for root in confirm_roots:
        by_operation.setdefault(root.operation_id, []).append(root)
    missing = [entry for key, entry in expected.items() if key not in by_operation]
    extra = [{"operation_id": key, "trace_id": root.trace_id, "root_span_id": root.span_id}
             for key, items in by_operation.items() if key not in expected for root in items]
    kind_mismatches = [{"operation_id": key, "expected": entry["business_kind"], "actual": root.attributes.get("business_kind"),
                        "trace_id": root.trace_id, "root_span_id": root.span_id}
                       for key, entry in expected.items() for root in by_operation.get(key, [])
                       if entry["business_kind"] and root.attributes.get("business_kind") != entry["business_kind"]]
    session_mismatches = [{"operation_id": key, "expected_session": entry["session_id"], "actual_session": root.session_id,
                           "trace_id": root.trace_id, "root_span_id": root.span_id}
                          for key, entry in expected.items() for root in by_operation.get(key, [])
                          if entry["session_id"] and root.session_id != entry["session_id"]]
    multiple = {key: count for key, count in stats["posts_per_operation"].items() if count >= 2}

    def _group(keys) -> dict:
        return {
            "expected": len(keys),
            "actual": sum(1 for key in keys if key in by_operation),
            "missing": [entry for key, entry in expected.items() if key not in by_operation and key in keys],
        }

    # 每个受理成功的操作编号都必须恰好对应一个 confirmation 根：0 个是缺证据，多于 1 个是幂等破防。
    violations = sorted(key for key in set(multiple) | set(expected) if len(by_operation.get(key, [])) != 1)
    return {
        "expected_execution_roots": len(expected), "actual_roots": len(confirm_roots),
        "groups": {"scenario": _group(set(scenario)), "precondition": _group(set(precondition))},
        "missing": missing, "extra": extra, "business_kind_mismatches": kind_mismatches,
        "session_mismatches": session_mismatches, "zero_root_steps": stats["zero_root_steps"],
        "idempotency_check": {"note": "同一 operation_id 的多次受理成功 POST 只允许产生一个 confirmation 根。",
                              "operation_ids_with_multiple_accepted_posts": multiple,
                              "roots_per_operation": {key: len(items) for key, items in by_operation.items()},
                              "violations": violations, "passed": not violations},
    }


def _stat(boundary: str, expected_samples: int, values: list[float]) -> dict:
    return {"boundary": boundary, "expected_samples": expected_samples,
            "missing": max(0, expected_samples - len(values)), **_distribution(values)}


def _first_text(pool: list[SpanRecord]) -> list[float]:
    return [record.attributes["first_text_ms"] for record in pool
            if record.status is SpanStatus.completed and record.attributes.get("first_text_ms") is not None]


def _timing(records: list[SpanRecord], observation: RoundObservation, roots: list[SpanRecord]) -> dict:
    agent_roots = [root for root in roots if root.name is SpanName.agent_run]
    models = [record for record in records if record.name is SpanName.model]
    user_turns = [turn for turn in observation.turns if turn.kind == "user"]
    client_values = [turn.client_first_body_ms for turn in user_turns if turn.client_first_body_ms is not None]
    return {
        "agent_run_first_text_ms": _stat("受理起点 → 首个非空公开正文", len(agent_roots), _first_text(agent_roots)),
        "model_first_text_ms": _stat("模型请求起点 → 该请求首次非空 text_delta", len(models), _first_text(models)),
        "client_first_body_ms": _stat("客户端发起 run 请求 → 首个非空公开正文 SSE 事件", len(user_turns), client_values),
    }


def _field_summary(pool: list[SpanRecord], field: str) -> dict:
    values = [record.attributes[field] for record in pool if record.attributes.get(field) is not None]
    return {"total": sum(values) if values else None, "samples": len(values), "missing": len(pool) - len(values)}


def _field_totals(pool: list[SpanRecord]) -> dict:
    return {field: _field_summary(pool, field)["total"] for field in USAGE_FIELDS}


def _usage(records: list[SpanRecord], observation: RoundObservation, groups: dict) -> dict:
    models = [record for record in records if record.name is SpanName.model]
    by_kind = {}
    for kind in CALL_KINDS:
        pool = [record for record in models if record.attributes.get("call_kind") is None] if kind is None else [
            record for record in models if record.attributes.get("call_kind") == kind
        ]
        by_kind[kind or "unknown"] = {"spans": len(pool), "fields": {field: _field_summary(pool, field) for field in USAGE_FIELDS}}
    scenario_ids = {entry["run_id"] for entry in groups["scenario"]}
    trace_scenario = _field_totals([record for record in models if record.run_id in scenario_ids])
    client_total, mapping = observation.usage or {}, dict(zip(USAGE_FIELDS, CLIENT_FIELDS))
    difference = {field: None if trace_scenario[field] is None or client_total.get(mapping[field]) is None
                  else trace_scenario[field] - client_total[mapping[field]] for field in USAGE_FIELDS}
    return {
        "by_call_kind": by_kind, "model_spans": len(models), "trace_all_model_spans": _field_totals(models),
        "trace_scenario_runs_only": trace_scenario, "round_json_usage": {field: client_total.get(mapping[field]) for field in USAGE_FIELDS},
        "round_json_total_tokens": client_total.get("total_tokens"), "difference_scenario_vs_round_json": difference,
        "note": "round.json usage 只覆盖场景 user 回合；trace_all 含前置运行、agent 端点派生运行与摘要调用，两列分别对比。",
    }


def _incomplete_reasons(agent: dict, confirmation: dict, violations: list[dict]) -> list[str]:
    reasons = [f"agent_run.{name}.{field}={len(report[field])}" for name, report in agent["groups"].items()
               for field in ("missing", "association_errors") if report[field]]
    for key, count in (("agent_run.unattributed", len(agent["unattributed"])),
                       ("agent_run.duplicate_run_ids", len(agent["duplicate_run_ids"])),
                       ("confirmation.missing", len(confirmation["missing"])),
                       ("confirmation.extra", len(confirmation["extra"])),
                       ("confirmation.business_kind_mismatches", len(confirmation["business_kind_mismatches"])),
                       ("confirmation.session_mismatches", len(confirmation["session_mismatches"])),
                       ("trace.structural_violations", len(violations))):
        if count:
            reasons.append(f"{key}={count}")
    return reasons


def _reconcile_slot(records, observation: RoundObservation, detail: dict, paths: dict, interrupted: bool) -> dict:
    chains = _validate_chains(records)
    agent_roots = [root for root in chains["roots"] if root.name is SpanName.agent_run]
    confirm_roots = [root for root in chains["roots"] if root.name is SpanName.confirmation]
    groups = _expected_agent_runs(observation, detail)
    agent = _agent_runs(groups, agent_roots)
    confirmation = _confirmations(observation, confirm_roots, detail)
    compactions = [record for record in records if record.name is SpanName.compaction]
    compaction_reasons: dict[str, int] = {}
    for record in compactions:
        key = str(record.attributes.get("reason"))
        compaction_reasons[key] = compaction_reasons.get(key, 0) + 1
    reasons = _incomplete_reasons(agent, confirmation, chains["violations"])
    missing = [{"type": f"agent_run_root/{name}", "identity": item["run_id"], "source": item["source"]}
               for name, report in agent["groups"].items() for item in report["missing"]]
    missing += [
        {"type": "confirmation_root", "identity": item["operation_id"], "source": item["source"],
         "business_kind": item["business_kind"], "session_id": item["session_id"],
         **({"step_index": item["step_index"]} if "step_index" in item else {})}
        for item in confirmation["missing"]
    ]
    return {
        "case_id": observation.case_id, "round_index": observation.round_index, "slot": observation.slot,
        "process_index": observation.process_index, "session_id": observation.session_id, "interrupted": interrupted,
        "business_status": observation.status, "evidence": paths,
        "trace": {"traces": chains["traces"], "spans": chains["spans"], "structural_violations": chains["violations"],
                  "roots": {"agent_run": len(agent_roots), "confirmation": len(confirm_roots),
                            "other": len(chains["roots"]) - len(agent_roots) - len(confirm_roots)}},
        "agent_run": agent, "confirmation": confirmation, "timing": _timing(records, observation, chains["roots"]),
        "usage": _usage(records, observation, groups),
        "compaction": {"count": len(compactions), "total_duration_ms": sum(record.duration_ms for record in compactions),
                       "reasons": compaction_reasons},
        "missing": missing, "complete": not reasons, "incomplete_reasons": reasons,
    }


def _discover_slots(experiment_dir: Path, only: str | None) -> list[dict]:
    cases = experiment_dir / "cases"
    if not cases.is_dir():
        raise FileNotFoundError(f"实验目录缺少用例证据目录: {cases}")
    slots = []
    for case_dir in sorted(path for path in cases.iterdir() if path.is_dir()):
        if only and case_dir.name != only:
            continue
        rounds = sorted((int(path.name.split("-", 1)[1]), path) for path in case_dir.iterdir()
                        if path.is_dir() and path.name.startswith("round-"))
        for round_index, directory in rounds:
            for interrupted, name in ((False, "round.json"), (True, "error.json")):
                if (directory / name).exists():
                    slots.append({"case_id": case_dir.name, "round_index": round_index,
                                  "directory": directory, "interrupted": interrupted})
                    break
    return slots


def _observation_for(slot: dict) -> RoundObservation:
    directory = slot["directory"]
    if not slot["interrupted"]:
        return RoundObservation.model_validate(json.loads((directory / "round.json").read_text(encoding="utf-8")))
    data = json.loads((directory / "error.json").read_text(encoding="utf-8"))
    turns = data.get("completed_turns") or []
    return RoundObservation.model_validate({
        "case_id": data["case_id"], "round_index": data["round_index"], "slot": data["slot"],
        "session_id": str(turns[0].get("session_id") or "") if turns else "",
        "process_index": int(data.get("process_index", -1)), "turns": turns, "status": "error",
    })


def _invalid_slot(observation: RoundObservation, paths: dict, reason: str) -> dict:
    return {"case_id": observation.case_id, "round_index": observation.round_index, "slot": observation.slot,
            "process_index": observation.process_index, "session_id": observation.session_id, "interrupted": True,
            "business_status": observation.status, "evidence": paths, "evidence_invalid": True, "reason": reason,
            "missing": [], "complete": False, "incomplete_reasons": ["证据不可读"]}


async def _reconcile_all(experiment_dir: Path, slots: list[dict]) -> tuple[list[dict], list[dict]]:
    entries, missing = [], []
    for slot in slots:
        case_id, round_index, directory = slot["case_id"], slot["round_index"], slot["directory"]
        trace_path = experiment_dir / "traces" / f"{case_id}-r{round_index}.db"
        precondition_path = directory / "precondition.json"
        paths = {
            "round_json": (directory / "round.json").resolve().as_posix(),
            "error_json": (directory / "error.json").resolve().as_posix() if slot["interrupted"] else None,
            "precondition_json": precondition_path.resolve().as_posix() if precondition_path.exists() else None,
            "trace_db": trace_path.resolve().as_posix(),
            "business_db": (experiment_dir / "dbs" / f"{case_id}-r{round_index}.db").resolve().as_posix(),
        }
        observation = _observation_for(slot)
        detail = json.loads(precondition_path.read_text(encoding="utf-8")).get("detail") or {} if precondition_path.exists() else {}
        if not trace_path.exists():
            if not slot["interrupted"]:
                raise FileNotFoundError(f"槽位 {case_id}/round-{round_index} 已交付 round.json 但缺少 Trace 库：{paths['trace_db']}")
            entries.append(_invalid_slot(observation, paths, f"Trace 库缺失：{paths['trace_db']}"))
            missing.append({"type": "trace_db", "case_id": case_id, "round_index": round_index, "slot": observation.slot, "detail": paths["trace_db"]})
            continue
        try:
            records = await _read_spans(trace_path)
        except Exception as error:
            reason = f"Trace 库不可读：{type(error).__name__}: {error}"
            entries.append(_invalid_slot(observation, paths, reason))
            missing.append({"type": "trace_db_unreadable", "case_id": case_id, "round_index": round_index, "slot": observation.slot, "detail": reason})
            continue
        entry = _reconcile_slot(records, observation, detail, paths, slot["interrupted"])
        entries.append(entry)
        missing.extend({**item, "case_id": case_id, "round_index": round_index, "slot": observation.slot} for item in entry["missing"])
    return entries, missing


def _aggregate(entries: list[dict], slots_claimed: int, denominator_source: str) -> dict:
    readable = [item for item in entries if not item.get("evidence_invalid")]
    complete = [item for item in readable if item["complete"]]
    by_category: dict[str, dict] = {}
    for item in entries:
        prefix = next((value for value in CASE_ID_PREFIXES.values() if item["case_id"].startswith(value)), "?")
        bucket = by_category.setdefault(prefix, {"category": next((key for key, value in CASE_ID_PREFIXES.items()
                                                                   if value == prefix), "unknown"),
                                                "slots": 0, "evidence_readable": 0, "complete": 0})
        bucket["slots"] += 1
        bucket["evidence_readable"] += 0 if item.get("evidence_invalid") else 1
        bucket["complete"] += 1 if item["complete"] else 0
    for bucket in by_category.values():
        bucket["completeness_rate"] = _rate(bucket["complete"], bucket["slots"])
    return {
        "slots_claimed": slots_claimed, "slots_claimed_source": denominator_source, "slots_discovered": len(entries),
        "slots_evidence_readable": len(readable), "slots_complete": len(complete),
        "trace_completeness_rate": _rate(len(complete), slots_claimed),
        "trace_completeness_rate_over_readable": _rate(len(complete), len(readable)),
        "by_category": by_category,
        "agent_run_expected": sum(report["expected"] for item in readable for report in item["agent_run"]["groups"].values()),
        "agent_run_actual": sum(report["actual"] for item in readable for report in item["agent_run"]["groups"].values()),
        "confirmation_expected": sum(item["confirmation"]["expected_execution_roots"] for item in readable),
        "confirmation_actual": sum(item["confirmation"]["actual_roots"] for item in readable),
        "structural_violations": sum(len(item["trace"]["structural_violations"]) for item in readable),
    }


def build_report(experiment_dir: Path, experiment_id: str, only: str | None) -> dict:
    slots = _discover_slots(experiment_dir, only)
    if only and not slots:
        raise FileNotFoundError(f"实验 {experiment_id} 内没有用例 {only} 的轮次证据")
    claims = list((experiment_dir / "claims").glob("*.json"))
    entries, missing = asyncio.run(_reconcile_all(experiment_dir, slots))
    aggregates = _aggregate(entries, len(claims) if claims else len(slots), "claims/*.json" if claims else "cases/*/round-*")
    delivered = [item for item in entries if not item["interrupted"]]
    passed = sum(1 for item in delivered if item["business_status"] == "pass")
    statuses = sorted({item["business_status"] for item in delivered})
    return {
        "schema": "v2-trace-reconciliation/1", "experiment_id": experiment_id,
        "experiment_dir": experiment_dir.resolve().as_posix(),
        "generated_at": datetime.now(timezone.utc).isoformat(), "only_case_id": only, "definitions": DEFINITIONS,
        "slots": entries, "aggregates": aggregates,
        "business_vs_trace": {
            "statement": "Trace 链路完整率只度量观测链与业务身份的对应关系；业务通过率只度量评测检查项判定结果。"
            "两者判定口径与分母相互独立，分别上报，任何一项变化都不蕴含另一项变化。",
            "trace_completeness": {"numerator": aggregates["slots_complete"], "denominator": aggregates["slots_discovered"],
                                   "rate": _rate(aggregates["slots_complete"], aggregates["slots_discovered"])},
            "business_pass_rate": {"numerator": passed, "denominator": len(delivered), "rate": _rate(passed, len(delivered)),
                                   "status_counts": {status: sum(1 for item in delivered if item["business_status"] == status)
                                                     for status in statuses}},
        },
        "missing": missing,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m test.v2_trace_reconcile", description="v2 实验槽位 Trace 与业务观测对账")
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--experiment-root", default=str(REPO_ROOT / "tmp" / "v2-evaluation"))
    parser.add_argument("--out", default="trace-reconciliation.json")
    parser.add_argument("--only", default=None, help="仅对账单个 case_id")
    args = parser.parse_args(argv)
    experiment_dir = Path(args.experiment_root) / args.experiment_id
    if not experiment_dir.is_dir():
        print(f"trace_reconcile: 实验目录不存在: {experiment_dir}", file=sys.stderr)
        return 2
    try:
        report = build_report(experiment_dir, args.experiment_id, args.only)
    except (FileNotFoundError, ValueError) as error:
        print(f"trace_reconcile: {type(error).__name__}: {error}", file=sys.stderr)
        return 3
    target = experiment_dir / args.out
    target.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    aggregates = report["aggregates"]
    print(f"trace_reconcile: slots={aggregates['slots_discovered']} complete={aggregates['slots_complete']} "
          f"rate={aggregates['trace_completeness_rate']} missing={len(report['missing'])} -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
