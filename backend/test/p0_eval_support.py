import asyncio
import hashlib
import json
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app import model_config
from app.agent.usage import summarize_usage
from app.ai.messages import AssistantMessage, ToolResultMessage
from app.application.business.catalog import DEFAULT_ACTIONS_PATH
from app.application.business.service import BUSINESS_TIMEZONE, business_date
from app.domain.business.models import BusinessContext, WorkoutListArguments
from app.domain.session.models import CompactionEntry, SessionMessageEntry
from app.interfaces.http import app
from app.infrastructure.persistence.sqlite import database as database_module
from test.check_http import create_session, events, validate_events, wait_idle
from test.p0_contract import (
    CASE_CATEGORIES,
    CASE_ID_PREFIXES,
    CATEGORY_QUOTAS,
    ROUNDS,
    CaseRoundObservation,
    CheckResult,
    ExperimentEnv,
    P0Case,
    PreconditionEvidence,
    TurnObservation,
)
from test.regression_support import Server, client

BACKEND = Path(__file__).resolve().parents[1]
# 冻结前记录默认数据根：主仓库运行时即真实模型配置所在目录。
DEFAULT_DATA_ROOT = model_config.DATA_ROOT
USAGE_FIELDS = ("input", "output", "cache_read", "cache_write", "total_tokens")


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def trusted_business_date() -> str:
    return business_date(now_ms())


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2),
        encoding="utf-8",
    )


def _call(coro):
    return asyncio.run_coroutine_threadsafe(coro, app.state.loop).result()


def load_cases(path) -> list[P0Case]:
    # 用例文件支持顶层数组与 {"cases": [...]} 两种形状，逐项按冻结契约校验。
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    items = payload["cases"] if isinstance(payload, dict) else payload
    return [P0Case.model_validate(item) for item in items]


def validate_cases(cases: list[P0Case]) -> list[str]:
    errors: list[str] = []
    counts = {category: 0 for category in CASE_CATEGORIES}
    seen: set[str] = set()
    for case in cases:
        category = case.category
        if category not in CATEGORY_QUOTAS:
            errors.append(f"{case.case_id}: 未知类别 {category}")
            continue
        counts[category] += 1
        if case.case_id in seen:
            errors.append(f"{case.case_id}: 用例 ID 重复")
        seen.add(case.case_id)
        prefix = CASE_ID_PREFIXES[category]
        if not case.case_id.startswith(prefix):
            errors.append(f"{case.case_id}: ID 前缀与类别 {category} 不符（应为 {prefix}）")
        if not case.turns:
            errors.append(f"{case.case_id}: 缺少用户回合")
        if not case.checks:
            errors.append(f"{case.case_id}: 缺少检查项")
        check_ids = [check.check_id for check in case.checks]
        duplicated = {item for item in check_ids if check_ids.count(item) > 1}
        if duplicated:
            errors.append(f"{case.case_id}: 检查项 ID 重复 {sorted(duplicated)}")
        for review in case.manual_review:
            if not review.review_id.strip():
                errors.append(f"{case.case_id}: 审阅项缺少 review_id")
            if not review.standard.strip():
                errors.append(f"{case.case_id}: 审阅项 {review.review_id} 缺少审阅标准")
            for reference in review.evidence_refs:
                if not isinstance(reference, str) or not reference.strip():
                    errors.append(f"{case.case_id}: 审阅项 {review.review_id} 存在空证据引用")
    for category, quota in CATEGORY_QUOTAS.items():
        if counts[category] != quota:
            errors.append(f"类别 {category} 数量 {counts[category]} 与配额 {quota} 不符")
    return errors


def check_model_identity(identity: dict) -> None:
    if "api_key" in identity:
        raise ValueError("模型身份不得包含 api_key")
    for field in ("provider", "api", "model", "base_url"):
        if not identity.get(field):
            raise ValueError(f"模型身份缺少 {field}")


def freeze_model_config(source_root, experiment_dir) -> dict:
    # 从源目录读取真实配置，写入实验隔离目录并保持 DATA_ROOT 指向冻结快照。
    model_config.DATA_ROOT = Path(source_root)
    loaded = model_config.load_model_config()
    model_config.DATA_ROOT = Path(experiment_dir) / "model-config"
    model_config.save_provider(
        api=loaded.api,
        base_url=loaded.base_url,
        model=loaded.model,
        provider=loaded.provider,
        api_key=loaded.api_key,
    )
    identity = {
        "provider": loaded.provider,
        "api": loaded.api,
        "model": loaded.model,
        "base_url": loaded.base_url,
    }
    check_model_identity(identity)
    return identity


def use_frozen_model_config(experiment_dir) -> dict:
    # 分片子进程复用父进程已冻结的配置快照，避免多进程并发写同一配置目录。
    model_config.DATA_ROOT = Path(experiment_dir) / "model-config"
    loaded = model_config.load_model_config()
    identity = {
        "provider": loaded.provider,
        "api": loaded.api,
        "model": loaded.model,
        "base_url": loaded.base_url,
    }
    check_model_identity(identity)
    return identity


def experiment_env(
    experiment_id: str,
    case: P0Case,
    round_index: int,
    experiment_dir,
    model_identity: dict,
    business_date_value: str,
) -> ExperimentEnv:
    root = Path(experiment_dir)
    return ExperimentEnv(
        experiment_id=experiment_id,
        case_id=case.case_id,
        round_index=round_index,
        database_path=str(root / "dbs" / f"{case.case_id}-r{round_index}.db"),
        evidence_dir=str(root / "cases" / case.case_id / f"round-{round_index}"),
        business_date=business_date_value,
        model_identity=model_identity,
    )


def business_state() -> dict:
    business = app.state.business
    profile = _call(business.get_profile())
    plan = _call(business.get_current_plan())
    workouts = _call(business.list_workouts(WorkoutListArguments(page_size=100)))
    return {
        "profile": profile.model_dump(),
        "current_plan": plan.model_dump(),
        "workouts": {
            "total": workouts.total,
            "items": [item.model_dump() for item in workouts.items],
        },
    }


def summarize_run_usage(entries, run_id: str) -> tuple[dict | None, list[dict]]:
    # 按本次 run 汇总真实用量：assistant/toolResult 消息加压缩检查点 usage。
    # 存在 usage 缺失的助手消息（aborted 等）时不汇总，如实返回缺测条目。
    messages = []
    compaction_usage = []
    missing: list[dict] = []
    for entry in entries:
        if entry.run_id != run_id:
            continue
        if isinstance(entry, SessionMessageEntry):
            message = entry.messages[0]
            if isinstance(message, AssistantMessage):
                if message.stop_reason == "pending" or message.usage is None:
                    missing.append(
                        {
                            "entry_id": entry.id,
                            "stop_reason": message.stop_reason,
                            "usage": None,
                            "reason": "助手消息缺少真实 usage",
                        }
                    )
                    continue
                messages.append(message)
            elif isinstance(message, ToolResultMessage):
                messages.append(message)
        elif isinstance(entry, CompactionEntry):
            compaction_usage.append(entry.usage)
    if missing:
        return None, missing
    if not messages and not compaction_usage:
        return None, [
            {
                "entry_id": None,
                "stop_reason": None,
                "usage": None,
                "reason": "本次 run 无持久化条目",
            }
        ]
    totals = summarize_usage(messages, compaction_usage=compaction_usage)
    return {field: getattr(totals, field) for field in USAGE_FIELDS}, []


def _tool_calls(events_list: list[dict]) -> list[dict]:
    starts: dict[str, dict] = {}
    endings: dict[str, dict] = {}
    results: dict[str, dict] = {}
    for event in events_list:
        kind = event["event"]
        data = event["data"]
        call_id = data.get("tool_call_id")
        if call_id is None:
            continue
        if kind == "tool_start":
            starts[call_id] = data
        elif kind == "tool_execution_end":
            endings[call_id] = data
        elif kind == "tool_result":
            results[call_id] = data
    return [
        {
            "tool_call_id": call_id,
            "name": start["name"],
            "arguments": start["arguments"],
            "is_error": endings.get(call_id, {}).get("is_error"),
            "content": endings.get(call_id, {}).get("content"),
            "entry_id": results.get(call_id, {}).get("entry_id"),
        }
        for call_id, start in starts.items()
    ]


def capture_turn(
    http,
    session_id: str,
    *,
    request_text: str,
    case_id: str,
    round_index: int,
    turn_index: int,
) -> tuple[TurnObservation, list[dict]]:
    # 单个真实 HTTP/SSE 回合：请求身份取自响应头，终态事件定稿，用量按 run_id 汇总。
    operation_id = str(uuid4())
    before_state = business_state()
    started = time.perf_counter()
    terminal = "unknown"
    first_text_delta_ms = None
    events_list: list[dict] = []
    with http.stream(
        "POST",
        "/api/agent/run",
        json={"session_id": session_id, "operation_id": operation_id, "request": request_text},
    ) as response:
        if response.status_code != 200:
            raise RuntimeError(f"运行请求失败: {response.status_code} {response.text}")
        session_header = response.headers["X-Session-ID"]
        operation_header = response.headers["X-Operation-ID"]
        run_id = response.headers["X-Run-ID"]
        request_entry_id = response.headers["X-Request-Entry-ID"]
        for event in events(response):
            events_list.append(event)
            if (
                first_text_delta_ms is None
                and event["event"] == "message_update"
                and event["data"].get("update_type") == "text_delta"
            ):
                first_text_delta_ms = (time.perf_counter() - started) * 1000.0
            if event["event"] in {"done", "error"}:
                terminal = event["event"]
                break
    duration_ms = (time.perf_counter() - started) * 1000.0
    wait_idle()
    if terminal != "unknown":
        # 复用真实设施校验业务流 SSE 结构完整性；压缩事件结构由压缩专项单独测量。
        validate_events(
            [event for event in events_list if not event["event"].startswith("compaction_")]
        )
    after_state = business_state()
    history = _call(app.state.session_service.get_session_history(session_id))
    usage, missing = summarize_run_usage(history.entries, run_id)
    observation = TurnObservation(
        case_id=case_id,
        round_index=round_index,
        turn_index=turn_index,
        request_text=request_text,
        session_id=session_header,
        operation_id=operation_header,
        run_id=run_id,
        request_entry_id=request_entry_id,
        terminal=terminal,
        events=events_list,
        tool_calls=_tool_calls(events_list),
        before_state=before_state,
        after_state=after_state,
        usage=usage,
        first_text_delta_ms=first_text_delta_ms,
        duration_ms=duration_ms,
    )
    return observation, missing


def _sum_turns_usage(turns: list[TurnObservation]) -> dict | None:
    present = [turn.usage for turn in turns if turn.usage is not None]
    if not present:
        return None
    return {field: sum(item[field] for item in present) for field in USAGE_FIELDS}


def _round_status(turns: list[TurnObservation], checks: list[CheckResult]) -> str:
    if any(turn.terminal in {"error", "unknown"} for turn in turns):
        return "error"
    if any(check.status == "fail" for check in checks):
        return "fail"
    if any(check.status == "pending_review" for check in checks):
        return "pending_review"
    return "pass"


def run_case_round(
    case: P0Case,
    round_index: int,
    env: ExperimentEnv,
    model_identity: dict,
) -> CaseRoundObservation:
    from test.p0_business_cases import build_precondition, evaluate

    check_model_identity(model_identity)
    database_path = Path(env.database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    # 每次场景执行必须从同一干净前置条件开始：清除同路径残留库及其卷/日志文件。
    for suffix in ("", "-wal", "-shm", "-journal"):
        stale = database_path.with_name(database_path.name + suffix)
        if stale.exists():
            stale.unlink()
    database_module.default_database_path = lambda: database_path
    evidence_dir = Path(env.evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    session_id = str(uuid4())
    started = time.perf_counter()
    turns: list[TurnObservation] = []
    try:
        with Server(app) as server, client(server.base_url, timeout=600) as http:
            create_session(http, session_id, title=case.case_id)
            context = BusinessContext(
                timezone=BUSINESS_TIMEZONE,
                business_date=env.business_date,
                session_id=session_id,
                run_id="",
                request_entry_id="",
                source_entry_id="",
            )
            precondition: PreconditionEvidence = build_precondition(
                case,
                service=app.state.business,
                business_context=context,
                session_id=session_id,
            )
            _write_json(evidence_dir / "precondition.json", precondition.model_dump())
            checks: list[CheckResult] = []
            for index, case_turn in enumerate(case.turns):
                observation, missing = capture_turn(
                    http,
                    session_id,
                    request_text=case_turn.text,
                    case_id=case.case_id,
                    round_index=round_index,
                    turn_index=index,
                )
                turns.append(observation)
                _write_json(
                    evidence_dir / f"turn-{index}.json",
                    {"observation": observation.model_dump(), "usage_missing": missing},
                )
                # check_id 的 -R<n>- 指用户回合（1 起）。每回合结束后立即按该时刻的真实
                # 持久化状态判定本回合检查项，避免多回合用例把前置状态与终态混淆。
                turn_view = CaseRoundObservation(
                    case_id=case.case_id,
                    round_index=index + 1,
                    session_id=session_id,
                    turns=[observation],
                    checks=[],
                    usage=observation.usage,
                    duration_ms=observation.duration_ms,
                    status="pass",
                )
                checks.extend(
                    evaluate(
                        case,
                        observation=turn_view,
                        service=app.state.business,
                        database_path=str(database_path),
                        business_context=context,
                    )
                )
            duration_ms = (time.perf_counter() - started) * 1000.0
            observation = CaseRoundObservation(
                case_id=case.case_id,
                round_index=round_index,
                session_id=session_id,
                turns=turns,
                checks=checks,
                usage=_sum_turns_usage(turns),
                duration_ms=duration_ms,
                status=_round_status(turns, checks),
            )
    except BaseException as error:
        _write_json(
            evidence_dir / "error.json",
            {
                "case_id": case.case_id,
                "round_index": round_index,
                "error": f"{type(error).__name__}: {error}",
                "completed_turns": [turn.model_dump() for turn in turns],
            },
        )
        raise
    _write_json(evidence_dir / "round.json", observation.model_dump())
    return observation


def _median(values: list[float]) -> float | None:
    ordered = sorted(values)
    count = len(ordered)
    if count == 0:
        return None
    middle = count // 2
    if count % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _span(values: list[float]) -> dict:
    return {"n": len(values), "median": _median(values), "min": min(values) if values else None,
            "max": max(values) if values else None}


def _category_of(case_id: str) -> str:
    for category, prefix in CASE_ID_PREFIXES.items():
        if case_id.startswith(prefix):
            return category
    return "unknown"


def _usage_totals(rounds: list[CaseRoundObservation]) -> dict:
    seen: dict[str, dict] = {}
    duplicates: list[str] = []
    missing_turns = 0
    for observation in rounds:
        for turn in observation.turns:
            if turn.usage is None:
                missing_turns += 1
                continue
            if turn.run_id in seen:
                duplicates.append(turn.run_id)
                continue
            seen[turn.run_id] = turn.usage
    totals = {field: sum(item[field] for item in seen.values()) for field in USAGE_FIELDS}
    prompt_tokens = totals["cache_read"] + totals["input"]
    totals["cache_hit_rate"] = (
        totals["cache_read"] / prompt_tokens if prompt_tokens > 0 else None
    )
    totals["runs"] = len(seen)
    totals["duplicate_runs"] = duplicates
    totals["missing_turns"] = missing_turns
    return totals


def _duration_totals(rounds: list[CaseRoundObservation]) -> dict:
    round_ms = [r.duration_ms for r in rounds if r.duration_ms is not None]
    first_ms = [
        turn.first_text_delta_ms
        for r in rounds
        for turn in r.turns
        if turn.first_text_delta_ms is not None
    ]
    return {"round_ms": _span(round_ms), "first_text_delta_ms": _span(first_ms)}


def _category_breakdown(case_ids: list[str], rounds_by_case: dict) -> dict:
    result: dict[str, dict] = {}
    for case_id in case_ids:
        category = _category_of(case_id)
        bucket = result.setdefault(
            category,
            {
                "cases": 0,
                "executions": 0,
                "passed_executions": 0,
                "stable_cases": 0,
                "pending_review_rounds": 0,
            },
        )
        bucket["cases"] += 1
        observed = [
            rounds_by_case[case_id][index]
            for index in range(ROUNDS)
            if rounds_by_case[case_id].get(index) is not None
        ]
        bucket["executions"] += len(observed)
        bucket["passed_executions"] += sum(1 for r in observed if r.status == "pass")
        if len(observed) == ROUNDS and all(r.status == "pass" for r in observed):
            bucket["stable_cases"] += 1
        bucket["pending_review_rounds"] += sum(
            1 for r in observed if r.status == "pending_review"
        )
    for bucket in result.values():
        bucket["execution_pass_rate"] = (
            bucket["passed_executions"] / bucket["executions"] if bucket["executions"] else None
        )
        bucket["stable_pass_rate"] = (
            bucket["stable_cases"] / bucket["cases"] if bucket["cases"] else None
        )
    return result


def aggregate(rounds: list[CaseRoundObservation]) -> dict:
    ordered = sorted(rounds, key=lambda item: (item.case_id, item.round_index))
    case_ids = sorted({item.case_id for item in ordered})
    rounds_by_case: dict[str, dict[int, CaseRoundObservation]] = {
        case_id: {} for case_id in case_ids
    }
    for observation in ordered:
        rounds_by_case[observation.case_id][observation.round_index] = observation

    per_round = []
    for index in range(ROUNDS):
        observed = [r for r in ordered if r.round_index == index]
        passed = [r for r in observed if r.status == "pass"]
        per_round.append(
            {
                "round_index": index,
                "observed": len(observed),
                "passed": len(passed),
                "rate": (len(passed) / len(observed)) if observed else None,
            }
        )

    executions = len(ordered)
    passed_executions = sum(1 for r in ordered if r.status == "pass")
    distribution = {f"{count}/{ROUNDS}": 0 for count in range(ROUNDS + 1)}
    stable_cases = 0
    for case_id in case_ids:
        bucket = rounds_by_case[case_id]
        passed_rounds = sum(
            1
            for index in range(ROUNDS)
            if bucket.get(index) is not None and bucket[index].status == "pass"
        )
        distribution[f"{passed_rounds}/{ROUNDS}"] += 1
        if passed_rounds == ROUNDS:
            stable_cases += 1

    failures = [
        {"case_id": r.case_id, "round_index": r.round_index, "status": r.status}
        for r in ordered
        if r.status not in {"pass", "pending_review"}
    ]
    pending_review = [
        {
            "case_id": r.case_id,
            "round_index": r.round_index,
            "check_id": check.check_id,
            "reason": check.reason,
        }
        for r in ordered
        for check in r.checks
        if check.status == "pending_review"
    ]
    missing_rounds = [
        {
            "case_id": case_id,
            "missing_rounds": [
                index for index in range(ROUNDS) if rounds_by_case[case_id].get(index) is None
            ],
        }
        for case_id in case_ids
        if any(rounds_by_case[case_id].get(index) is None for index in range(ROUNDS))
    ]
    missing_usage = [
        {
            "case_id": r.case_id,
            "round_index": r.round_index,
            "turn_index": turn.turn_index,
            "run_id": turn.run_id,
        }
        for r in ordered
        for turn in r.turns
        if turn.usage is None
    ]
    return {
        "independent_cases": len(case_ids),
        "expected_cases": sum(CATEGORY_QUOTAS.values()),
        "executions": executions,
        "expected_executions": len(case_ids) * ROUNDS,
        "failure_denominator": executions,
        "passed_executions": passed_executions,
        "execution_pass_rate": (passed_executions / executions) if executions else None,
        "stable_cases": stable_cases,
        "stable_pass_rate": (stable_cases / len(case_ids)) if case_ids else None,
        "per_round": per_round,
        "stability_distribution": distribution,
        "categories": _category_breakdown(case_ids, rounds_by_case),
        "usage": _usage_totals(ordered),
        "durations": _duration_totals(ordered),
        "failures": failures,
        "pending_review": pending_review,
        "missing_rounds": missing_rounds,
        "missing_usage": missing_usage,
    }


def load_reviews(path) -> list[dict]:
    # 人工审阅结论文件：顶层数组或 {"reviews": [...]}；每条键为 case_id/round_index/check_id。
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    items = payload["reviews"] if isinstance(payload, dict) else payload
    return list(items)


def apply_review(
    rounds: list[CaseRoundObservation], reviews: list[dict]
) -> list[CaseRoundObservation]:
    # 将人工审阅结论写回 pending_review 检查项，并按新结论重算每轮状态。
    index = {(item["case_id"], item["round_index"], item["check_id"]): item for item in reviews}
    updated: list[CaseRoundObservation] = []
    for observation in rounds:
        checks: list[CheckResult] = []
        for check in observation.checks:
            if check.status == "pending_review":
                verdict = index.get((observation.case_id, observation.round_index, check.check_id))
                if verdict is None:
                    checks.append(check)
                    continue
                status = verdict["status"]
                if status not in {"pass", "fail", "pending_review"}:
                    raise ValueError(f"审阅结论状态非法: {status}")
                checks.append(
                    check.model_copy(
                        update={
                            "status": status,
                            "reason": verdict.get("reason", check.reason),
                            "evidence_refs": verdict.get("evidence_refs", check.evidence_refs),
                        }
                    )
                )
            else:
                checks.append(check)
        updated.append(
            observation.model_copy(
                update={"checks": checks, "status": _round_status(observation.turns, checks)}
            )
        )
    return updated


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    digest.update(Path(path).read_bytes())
    return digest.hexdigest()


def git_output(root, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def build_manifest(
    root, experiment_id: str, cases_path, model_identity: dict
) -> dict:
    return {
        "experiment_id": experiment_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "commit": git_output(root, "rev-parse", "HEAD"),
        "status_porcelain": git_output(root, "status", "--porcelain"),
        "cases_path": str(cases_path),
        "cases_sha256": sha256_file(cases_path),
        "catalog_path": str(DEFAULT_ACTIONS_PATH),
        "catalog_sha256": sha256_file(DEFAULT_ACTIONS_PATH),
        "model_identity": model_identity,
        "platform": platform.platform(),
        "python": sys.version.split()[0],
    }
