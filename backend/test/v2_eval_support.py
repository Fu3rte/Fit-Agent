"""v2 评测执行器：加载/校验用例、冻结模型配置、执行一个场景轮次、聚合结果。"""

import asyncio
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import partial, wraps
from pathlib import Path
from uuid import uuid4

import aiosqlite

from app import model_config
from app.application.business.catalog import DEFAULT_ACTIONS_PATH
from app.application.business.service import BUSINESS_TIMEZONE, business_date
from app.domain.business.models import BusinessContext, WorkoutListArguments
from app.infrastructure.persistence.sqlite import database as database_module
from app.interfaces.http import app
from test.check_http import create_session, events, wait_idle
from test.p0_eval_support import summarize_run_usage
from test.regression_support import Server, client
from test.v2_attempts import (
    budget_path,
    claim_slot,
    finish_slot,
    slot_attachment_root,
    validate_unique_rounds,
)
from test.v2_budget import BudgetExhausted, BudgetLedger, StopTriggered
from test.v2_contract import (
    CASE_CATEGORIES,
    CASE_ID_PREFIXES,
    CATEGORY_QUOTAS,
    NEGATIVE_BREAK_FIELDS,
    NEGATIVE_ENTRIES,
    PREPARE_TOOLS,
    ROUNDS,
    CheckResult,
    ConfirmationObservation,
    ExperimentEnv,
    PreconditionEvidence,
    RoundObservation,
    ToolCallRecord,
    TurnObservation,
    V2Case,
)

BACKEND = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = model_config.DATA_ROOT
USAGE_FIELDS = ("input", "output", "cache_read", "cache_write", "total_tokens")

# 用量来源标签：只有供应商原始字段实际存在才算 reported。
SOURCE_REPORTED = "reported"
SOURCE_UNREPORTED_DEFAULTED = "unreported_defaulted"
SOURCE_UNKNOWN = "unknown"

# 缓存命中率共同样本要求的字段：三项输入均真实已知才计入。
CACHE_COMMON_FIELDS = ("input", "cache_read", "cache_write")


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def trusted_business_date() -> str:
    return business_date(now_ms())


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8"
    )


def trigger_stop(ledger, category, *, owner, trigger, slot=None, operation_id=None,
                 evidence_refs=None, detail=None) -> dict:
    # 真实触发源就地置位共享停止；首次触发即定稿，停止保持置位不自动解除。
    return ledger.request_stop(
        category, owner=owner, trigger=trigger, slot=slot, operation_id=operation_id,
        evidence_refs=evidence_refs, detail=detail,
    )


def write_stop_report(experiment_dir, stop: dict) -> Path:
    # 持久化停止报告：主控输出与落盘报告共同暴露触发原因与证据引用。
    target = Path(experiment_dir) / "stop-report.json"
    _write_json(target, stop)
    return target


def _call(coro):
    return asyncio.run_coroutine_threadsafe(coro, app.state.loop).result()


def load_cases(path) -> list[V2Case]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    items = payload["cases"] if isinstance(payload, dict) else payload
    return [V2Case.model_validate(item) for item in items]


def validate_cases(cases: list[V2Case]) -> list[str]:
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
        if not case.case_id.startswith(CASE_ID_PREFIXES[category]):
            errors.append(f"{case.case_id}: ID 前缀与类别 {category} 不符")
        if not case.steps:
            errors.append(f"{case.case_id}: 缺少步骤")
        if not case.checks:
            errors.append(f"{case.case_id}: 缺少检查项")
        check_ids = [check.check_id for check in case.checks]
        duplicated = {item for item in check_ids if check_ids.count(item) > 1}
        if duplicated:
            errors.append(f"{case.case_id}: 检查项 ID 重复 {sorted(duplicated)}")
        step_check_counts = {index: 0 for index in range(1, len(case.steps) + 1)}
        for check in case.checks:
            match = re.match(r"^[A-Z]\d+-S(\d+)-\d+$", check.check_id)
            if match is None:
                errors.append(f"{case.case_id}: 检查项 ID 格式非法 {check.check_id}")
                continue
            index = int(match.group(1))
            if index not in step_check_counts:
                errors.append(f"{case.case_id}: 检查项 {check.check_id} 指向不存在的步骤 {index}")
                continue
            step_check_counts[index] += 1
        for index, count in step_check_counts.items():
            if count == 0:
                errors.append(f"{case.case_id}: 步骤 {index} 缺少检查项")
        for step in case.steps:
            if step.kind == "user" and not step.text:
                errors.append(f"{case.case_id}: user 步骤缺少文本")
            if step.kind in {"confirm", "confirm_repeat", "confirm_negative"} and not step.operation:
                errors.append(f"{case.case_id}: 确认步骤缺少 operation 幂等键")
            if step.kind == "confirm_negative":
                if not step.break_field:
                    errors.append(f"{case.case_id}: 负向步骤缺少 break_field（唯一破坏条件）")
                elif step.break_field not in NEGATIVE_BREAK_FIELDS:
                    errors.append(f"{case.case_id}: 未登记的负向条件 {step.break_field}")
                # 入口在运行期直接决定派发路径，Literal 之外的构造与改写同样要就地拦下。
                if (step.entry or "confirmation") not in NEGATIVE_ENTRIES:
                    errors.append(f"{case.case_id}: 未知派发入口 {step.entry}")
                if step.break_field == "authorization_injection" and step.entry not in {"run", "edit", "steering"}:
                    errors.append(f"{case.case_id}: 授权注入必须由 run/edit/steering 入口承载，确认端点内不构成该条件")
                if step.entry in {"edit", "steering"} and not any(
                    item.kind == "user" for item in case.steps[: case.steps.index(step)]
                ):
                    errors.append(f"{case.case_id}: {step.entry} 入口的负向步骤前缺少 user 步骤提供真实锚点")
                if not step.expect_code:
                    errors.append(f"{case.case_id}: 负向步骤缺少 expect_code（受控错误码）")
                if step.entry == "confirmation" and step.overrides.get("path_session"):
                    errors.append(f"{case.case_id}: 跨会话目标应使用 cross: 引用保持单条件破坏")
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


EMBEDDING_BASIS = "官方单条输入上限与单次条数上限登记于 tmp/research/；本轮以返回的真实 usage 单项入账"


def _append_budget_error(ledger, payload: dict) -> None:
    path = Path(ledger.path).with_name("budget-errors.jsonl")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")


def install_embedding_recorder(ledger, *, experiment_id: str, process_index: int) -> None:
    # 评测侧包装真实编码请求：请求前登记并读取暂停状态，响应用真实 usage 结算，
    # 取消与失败保持用量未知；记账自身的异常记录后让位给业务异常。
    # 同一执行单元只安装一次：重复安装复用已安装包装与其进程归属，避免嵌套。
    from app.application.business import retrieval_embedding

    client_class = retrieval_embedding.EmbeddingClient
    if getattr(client_class, "_v2_recorder_owner", None) == experiment_id:
        return
    original = getattr(client_class, "_v2_original_embed", client_class._embed)
    client_class._v2_original_embed = original
    ledger = BudgetLedger(ledger.path, ledger.limits, experiment_id=experiment_id)

    async def recorded(self, texts):
        model = self._config.model
        # 共享停止置位后拒绝新增 Embedding 请求；在途请求按原有结算语义收尾。
        ledger.check_dispatch()
        attempt_id = str(uuid4())
        ledger.record_attempt(
            model,
            attempt_id,
            inputs=len(texts),
            basis=EMBEDDING_BASIS,
            context={"experiment_id": experiment_id, "process_index": process_index},
        )
        started = time.perf_counter()
        try:
            response = await original(self, texts)
        except BaseException as error:
            try:
                ledger.settle(
                    model, attempt_id, None,
                    reason=f"{type(error).__name__}: {error}",
                    elapsed_ms=(time.perf_counter() - started) * 1000.0,
                )
            except Exception as settle_error:
                _append_budget_error(
                    ledger,
                    {"attempt_id": attempt_id, "model": model,
                     "error": f"{type(settle_error).__name__}: {settle_error}",
                     "business_error": f"{type(error).__name__}: {error}"},
                )
            raise
        usage = getattr(response, "usage", None)
        tokens = getattr(usage, "total_tokens", None)
        if isinstance(tokens, bool) or not isinstance(tokens, int):
            tokens = None
        ledger.settle(
            model, attempt_id, tokens,
            reason=None if tokens is not None else "usage 缺失或非数值",
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            inputs=len(texts),
        )
        return response

    client_class._embed = recorded
    client_class._v2_recorder_installed = True
    client_class._v2_recorder_owner = experiment_id


def exception_type(error: BaseException) -> str:
    return f"{type(error).__module__}.{type(error).__qualname__}"


@contextmanager
def execution_guards(ledger, evidence_dir, *, experiment_id=None, execution_unit=None,
                     attempt_id=None, expected_trace_failure=False):
    # 执行入口持有完整生命周期：安装时固定实验、执行单元与证据目录；异常与取消路径同样卸载。
    from app.agent import telemetry, tool
    from app.ai import stream as model_stream
    from app.application.business import retrieval_index
    from app.application.business.retrieval_index import MilvusConnection

    if getattr(telemetry, "_v2_execution_owner", None) is not None:
        raise RuntimeError("评测执行包装器已有 owner")
    owner = experiment_id or ledger.experiment_id
    if owner is None:
        raise ValueError("执行包装器需要实验作用域")
    # 全局包装期间禁止混入其他实验：归属不一致即拒绝安装。
    if ledger.experiment_id is not None and ledger.experiment_id != owner:
        raise ValueError("执行包装器与账本归属不一致")
    ledger.attribution_guard()
    telemetry._v2_execution_owner = owner
    originals = (telemetry.TraceStore.save, telemetry.open_trace_store,
                 model_stream._validate_request, tool._execute_with_cancel,
                 MilvusConnection.__aenter__, MilvusConnection.call,
                 retrieval_index.published_collection)

    def observed_failure(category, boundary, error, **identity):
        payload = {"experiment_id": owner, "execution_unit": execution_unit,
                   "attempt_id": attempt_id, "boundary": boundary,
                   "error_type": exception_type(error), "error": str(error),
                   "at": now_ms(), **identity}
        path = Path(evidence_dir) / f"{category}-{uuid4().hex}.json"
        _write_json(path, payload)
        # 意外 Trace 写失败在发现处置位停止；专项明确声明的预期观测故障豁免置位并保留证据。
        if category == "trace_write_failure" and expected_trace_failure:
            return payload
        ledger.request_stop(category, owner=owner, trigger=boundary,
                            operation_id=identity.get("operation_id"),
                            evidence_refs=[str(path)], detail=payload)
        return payload

    @wraps(originals[0])
    async def save(store, records, **kwargs):
        try:
            return await originals[0](store, records, **kwargs)
        except Exception as error:
            observed_failure("trace_write_failure", "TraceStore.save", error,
                             operation_id=records[0].operation_id if records else None)
            raise

    @wraps(originals[1])
    async def open_store(*args, **kwargs):
        try:
            return await originals[1](*args, **kwargs)
        except Exception as error:
            observed_failure("trace_write_failure", "open_trace_store", error)
            raise

    def dispatch(signal=None):
        try:
            ledger.check_dispatch()
        except (StopTriggered, BudgetExhausted):
            if signal is not None:
                signal.set()
            raise asyncio.CancelledError

    @wraps(originals[2])
    def validate_request(context, options):
        dispatch(options.get("signal"))
        return originals[2](context, options)

    @wraps(originals[3])
    async def execute_tool(tool_call_id, tool, arguments, signal, on_update):
        dispatch(signal)
        return await originals[3](tool_call_id, tool, arguments, signal, on_update)

    @wraps(originals[4])
    async def connect(connection):
        dispatch()
        try:
            return await originals[4](connection)
        except Exception as error:
            observed_failure("milvus_unavailable", "MilvusConnection.__aenter__", error)
            raise

    @wraps(originals[5])
    async def call(connection, method, *args, **kwargs):
        dispatch()
        try:
            return await originals[5](connection, method, *args, **kwargs)
        except Exception as error:
            observed_failure("milvus_unavailable", f"MilvusConnection.call:{method}", error)
            raise

    @wraps(originals[6])
    async def published(connection, *args, **kwargs):
        # Milvus 实际使用点同样纳入探测：发布集合身份读取失败即就地置位停止。
        dispatch()
        try:
            return await originals[6](connection, *args, **kwargs)
        except Exception as error:
            observed_failure("milvus_unavailable", "published_collection", error)
            raise

    telemetry.TraceStore.save = save
    telemetry.open_trace_store = open_store
    model_stream._validate_request = validate_request
    tool._execute_with_cancel = execute_tool
    MilvusConnection.__aenter__ = connect
    MilvusConnection.call = call
    retrieval_index.published_collection = published
    try:
        yield
    finally:
        (telemetry.TraceStore.save, telemetry.open_trace_store,
         model_stream._validate_request, tool._execute_with_cancel,
         MilvusConnection.__aenter__, MilvusConnection.call,
         retrieval_index.published_collection) = originals
        telemetry._v2_execution_owner = None


# ---------- 业务状态 ----------


def experiment_env(
    experiment_id: str,
    case: V2Case,
    round_index: int,
    slot: int,
    process_index: int,
    experiment_dir,
    model_identity: dict,
    business_date_value: str,
    *,
    attempt_id: str | None = None,
    experiment_scope: str | None = None,
    expected_trace_failure: bool = False,
) -> ExperimentEnv:
    root = Path(experiment_dir)
    return ExperimentEnv(
        experiment_id=experiment_id,
        experiment_scope=experiment_scope,
        attempt_id=attempt_id,
        expected_trace_failure=expected_trace_failure,
        case_id=case.case_id,
        round_index=round_index,
        slot=slot,
        process_index=process_index,
        database_path=str(root / "dbs" / f"{case.case_id}-r{round_index}.db"),
        trace_path=str(root / "traces" / f"{case.case_id}-r{round_index}.db"),
        attachment_root=str(root / "attachments" / f"{case.case_id}-r{round_index}"),
        evidence_dir=str(root / "cases" / case.case_id / f"round-{round_index}"),
        execution_root=str(root),
        budget_ledger_path=str(budget_path(root)),
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
        "workouts": {"total": workouts.total, "items": [item.model_dump() for item in workouts.items]},
    }


def _tool_calls(events_list: list[dict]) -> list[ToolCallRecord]:
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
    records: list[ToolCallRecord] = []
    for call_id, start in starts.items():
        records.append(
            ToolCallRecord(
                tool_call_id=call_id,
                name=start["name"],
                arguments=start.get("arguments"),
                is_error=endings.get(call_id, {}).get("is_error"),
                content=endings.get(call_id, {}).get("content"),
                entry_id=results.get(call_id, {}).get("entry_id"),
            )
        )
    return records


def first_non_empty_body_ms(events: list[dict]) -> float | None:
    # 首个非空公开正文：空文本事件、思考内容与工具事件均不计入。
    for event in events:
        if event["event"] != "message_update" or event["data"].get("update_type") != "text_delta":
            continue
        text = "".join(block.get("text", "") for block in event["data"].get("content", []))
        if text.strip():
            return event["t_ms"]
    return None


def _usage_sources(entries, run_id: str) -> dict[str, str] | None:
    # 逐字段来源标签：openai-completions 适配器把缺省的 cache_write_tokens 物化为 0，
    # 无法证明 known_zero，故该字段一律标为未上报默认值；其余字段由上报值派生。
    from app.ai.messages import AssistantMessage

    apis: set[str] = set()
    found = False
    for entry in entries:
        if getattr(entry, "run_id", None) != run_id:
            continue
        messages = getattr(entry, "messages", None)
        if not messages:
            continue
        message = messages[0]
        if not isinstance(message, AssistantMessage) or message.usage is None:
            continue
        found = True
        apis.add(message.api)
    if not found:
        return None
    sources = {field: SOURCE_REPORTED for field in USAGE_FIELDS}
    if "openai-completions" in apis:
        sources["cache_write"] = SOURCE_UNREPORTED_DEFAULTED
    return sources


def capture_turn(http, session_id, *, case_id, round_index, step_index, request_text, attachments):
    operation_id = str(uuid4())
    before_state = business_state()
    started = time.perf_counter()
    terminal = "unknown"
    status = "unknown"
    first_body_ms = None
    events_list: list[dict] = []
    payload = {"session_id": session_id, "operation_id": operation_id, "request": request_text}
    if attachments:
        payload["attachments"] = attachments
    run_id = None
    request_entry_id = None
    error_type = None
    error_text = None
    try:
        with http.stream("POST", "/api/agent/run", json=payload) as response:
            if response.status_code != 200:
                response.read()
                raise RuntimeError(f"运行请求失败: {response.status_code} {response.text}")
            run_id = response.headers["X-Run-ID"]
            request_entry_id = response.headers["X-Request-Entry-ID"]
            for event in events(response):
                relative = (time.perf_counter() - started) * 1000.0
                events_list.append({"t_ms": relative, "event": event["event"], "data": event["data"]})
                if event["event"] in {"done", "error"}:
                    terminal = event["event"]
                    break
        wait_idle()
        first_body_ms = first_non_empty_body_ms(events_list)
        status = "done" if terminal == "done" else ("failed" if terminal == "error" else "unknown")
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        # 取消、中断与进程退出保留原控制流语义：不记为普通失败，向上传播并保留已完成事件。
        raise
    except BaseException as error:
        terminal = "error"
        status = "failed"
        error_type = exception_type(error)
        error_text = str(error)
        events_list.append({"t_ms": None, "event": "exception", "data": {
            "error": error_text, "error_type": error_type}})
    duration_ms = (time.perf_counter() - started) * 1000.0
    after_state = business_state()
    usage = None
    usage_sources = None
    if run_id:
        history = _call(app.state.session_service.get_session_history(session_id))
        usage, _missing = summarize_run_usage(history.entries, run_id)
        usage_sources = _usage_sources(history.entries, run_id)
    return TurnObservation(
        case_id=case_id,
        round_index=round_index,
        step_index=step_index,
        kind="user",
        request_text=request_text,
        session_id=session_id,
        operation_id=operation_id,
        run_id=run_id,
        request_entry_id=request_entry_id,
        terminal=terminal,
        status=status,
        events=events_list,
        tool_calls=_tool_calls(events_list),
        before_state=before_state,
        after_state=after_state,
        usage=usage,
        usage_sources=usage_sources,
        client_first_body_ms=first_body_ms,
        duration_ms=duration_ms,
        error=error_text,
        error_type=error_type,
    )


def _prepared(events_list: list[dict]) -> list[dict]:
    names = {e["data"]["tool_call_id"]: e["data"]["name"] for e in events_list if e["event"] == "tool_start"}
    prepared: list[dict] = []
    for event in events_list:
        if event["event"] != "tool_result":
            continue
        call_id = event["data"]["tool_call_id"]
        name = names.get(call_id)
        if name not in PREPARE_TOOLS or event["data"].get("is_error"):
            continue
        try:
            payload = json.loads(event["data"]["content"])
        except ValueError:
            continue
        proposal_id = payload.get("proposal_id")
        if not proposal_id:
            continue
        prepared.append(
            {
                "business_kind": PREPARE_TOOLS[name],
                "proposal_id": proposal_id,
                "display_entry_id": event["data"]["entry_id"],
                "tool": name,
            }
        )
    return prepared


CONTROLLED_REJECTION_CODES = frozenset({
    "invalid_request",
    "operation_conflict",
    "confirmation_not_found",
    "session_not_found",
    "run_busy",
    "proposal_already_saved",
    "proposal_cancellation_invalid",
    "profile_confirmation_invalid",
    "plan_confirmation_invalid",
    "workout_confirmation_invalid",
    "profile_proposal_invalidated",
    "plan_proposal_invalidated",
    "workout_proposal_invalidated",
    "profile_access_denied",
    "plan_access_denied",
    "workout_access_denied",
})


def _select_target(prepared, cross_targets, business_kind, target):
    candidates = [item for item in prepared if item["business_kind"] == business_kind]
    if target in (None, "pending"):
        if len(candidates) != 1:
            return None, f"待确认目标数量为 {len(candidates)}（要求唯一）"
        return candidates[0], None
    if target == "synthetic":
        # 注入类负向步骤使用合成身份：断言的是端点拒绝从请求正文派生授权。
        return {
            "business_kind": business_kind or "profile",
            "proposal_id": str(uuid4()),
            "display_entry_id": str(uuid4()),
            "tool": "synthetic",
        }, None
    if target.startswith("prepared:"):
        index = int(target.split(":", 1)[1])
        if index < 1 or index > len(candidates):
            return None, f"prepared:{index} 超出本轮 {len(candidates)} 个候选"
        return candidates[index - 1], None
    if target.startswith("cross:"):
        name = target.split(":", 1)[1]
        matches = [item for item in cross_targets if item.get("name") == name]
        if len(matches) != 1:
            return None, f"跨会话目标 {name} 数量为 {len(matches)}（要求唯一）"
        return matches[0], None
    return None, f"未知目标 {target}"


def _resolve_reference(prepared, cross_targets, value, field):
    # overrides 中的 prepared:<i> / cross:<name> 引用解析为真实绑定身份。
    if not isinstance(value, str):
        return value
    if value.startswith("prepared:"):
        index = int(value.split(":", 1)[1])
        if 1 <= index <= len(prepared):
            return prepared[index - 1][field]
        raise ValueError(f"overrides 引用超出候选：{value}")
    if value.startswith("cross:"):
        name = value.split(":", 1)[1]
        matches = [item for item in cross_targets if item.get("name") == name]
        if len(matches) != 1:
            raise ValueError(f"overrides 跨会话引用不唯一：{value}")
        return matches[0][field]
    return value


def _merge_overrides(body, overrides, prepared, cross_targets):
    for key, value in (overrides or {}).items():
        if key == "path_session":
            continue
        body[key] = _resolve_reference(prepared, cross_targets or [], value, key)
    return body


CONFIRM_IDENTITY_FIELDS = ("business_kind", "proposal_id", "display_entry_id")


def _expected_endpoint(step, session_id, *, last_run_id=None) -> str:
    # 预期端点由步骤声明与该会话真实 run_id 独立推导，和派发器实际请求的路径分别取值。
    if step.entry in (None, "confirmation"):
        return f"/api/sessions/{session_id}/confirmations"
    if step.entry == "run":
        return "/api/agent/run"
    if step.entry == "edit":
        return "/api/agent/edit"
    if step.entry == "steering":
        if not last_run_id:
            raise ValueError("steering 负向入口需要本会话真实 run_id")
        return f"/api/agent/runs/{last_run_id}/steering"
    raise ValueError(f"未知负向入口：{step.entry}")


def _canonical_state(state: dict) -> dict:
    # 固定排序：枚举型列表按规范 JSON 排序后比较；业务 payload 内容顺序保持原样。
    def sort_list(items):
        return sorted(
            items,
            key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False, default=str),
        )

    ordered = {}
    for key, value in state.items():
        if isinstance(value, list):
            ordered[key] = sort_list(value)
        elif isinstance(value, dict):
            ordered[key] = {
                sub_key: sort_list(sub_value) if isinstance(sub_value, list) else sub_value
                for sub_key, sub_value in value.items()
            }
        else:
            ordered[key] = value
    return ordered


def _protected_state(database_path, session_id) -> dict:
    # 逐请求比较的保护面：受保护记录的完整内容、身份、归属、状态与绑定；声明保护表缺失即拒绝声称保护。
    from test.v2_business_cases import read_state

    state = read_state(str(database_path), session_id)
    missing = (state.get("protected") or {}).get("missing_tables") or []
    if missing:
        raise RuntimeError(f"受保护记录缺少声明保护表，无法声称零写入：{missing}")
    return _canonical_state(state)


def _digest(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _state_diff(before: dict, after: dict) -> list[str]:
    return sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))


def _condition_broken(step, *, body, snapshots, session_id, endpoint):
    """按库内真实快照身份核验声明的破坏条件确实成立；每个负向步骤只允许一个条件不匹配。"""
    snapshot = snapshots.get(body.get("proposal_id"))
    if step.break_field == "authorization_injection":
        details = {
            "injected_fields_present": all(field in body for field in CONFIRM_IDENTITY_FIELDS),
            "entry_is_agent_endpoint": endpoint != f"/api/sessions/{session_id}/confirmations",
            "injected_proposal_unknown": snapshot is None,
        }
        return all(details.values()), details
    if snapshot is None:
        return False, {"error": "请求的 proposal_id 在该库内没有对应快照", "endpoint": endpoint}
    same_session_nodes = {
        item["display_entry_id"]
        for item in snapshots.values()
        if item["session_id"] == session_id and item["display_entry_id"]
    }
    if step.break_field == "display_entry_id":
        details = {
            "target_snapshot_pending": snapshot["status"] == "pending",
            "target_snapshot_in_path_session": snapshot["session_id"] == session_id,
            "display_binding_mismatched": body.get("display_entry_id") != snapshot["display_entry_id"],
            "display_binding_is_real_node": body.get("display_entry_id") in same_session_nodes,
        }
        return all(details.values()), details
    if step.break_field == "cross_session_target":
        details = {
            "target_snapshot_foreign": bool(snapshot["session_id"]) and snapshot["session_id"] != session_id,
            "target_snapshot_pending": snapshot["status"] == "pending",
            "binding_matches_foreign_snapshot": body.get("display_entry_id") == snapshot["display_entry_id"],
        }
        return all(details.values()), details
    if step.break_field == "expired_snapshot":
        details = {
            "target_snapshot_not_pending": snapshot["status"] != "pending",
            "binding_matches_original_snapshot": body.get("display_entry_id") == snapshot["display_entry_id"],
        }
        return all(details.values()), details
    raise ValueError(f"未登记的负向条件：{step.break_field}")


def _negative_assertions(step, *, observed_endpoint, expected_endpoint, code, status_code,
                         condition_ok, condition_details, before_digest, after_digest,
                         changed_keys, business_kind):
    controlled = step.expect_code or ""
    return {
        "endpoint_hit": bool(observed_endpoint) and observed_endpoint == expected_endpoint,
        "expected_endpoint": expected_endpoint,
        "observed_endpoint": observed_endpoint,
        "condition": step.break_field,
        "condition_broken": bool(step.break_field) and condition_ok,
        "condition_details": condition_details,
        "controlled_error": bool(code) and (
            code == controlled if controlled else code in CONTROLLED_REJECTION_CODES
        ),
        "observed_code": code,
        "expected_code": controlled or "任一受控拒绝码",
        "status_code": status_code,
        "zero_write": bool(before_digest) and before_digest == after_digest,
        "protected_state_before": before_digest,
        "protected_state_after": after_digest,
        "state_changed_keys": changed_keys,
        "business_kind": business_kind,
    }


def _unresolved(case_id, round_index, step_index, step, reason, *, endpoint=None):
    return ConfirmationObservation(
        case_id=case_id,
        round_index=round_index,
        step_index=step_index,
        kind=step.kind,
        business_kind=step.business_kind,
        operation_label=step.operation,
        operation_id=str(uuid4()),
        target=step.target,
        request={},
        rejected=True,
        rejection_code="target_unresolved",
        endpoint=endpoint,
        entry=step.entry,
        break_field=step.break_field,
        expect_code=step.expect_code,
        error=reason,
    )


def _send(http, session_id, *, endpoint, body, step, case_id, round_index, step_index, business_kind,
          database_path=None, last_run_id=None):
    negative = step.kind == "confirm_negative"
    if negative and not database_path:
        raise ValueError("负向步骤需要隔离库路径以核验受保护状态")
    before = _protected_state(database_path, session_id) if negative else None
    before_digest = _digest(before) if negative else None
    before_state = before if negative else business_state()
    started = time.perf_counter()
    response = http.post(endpoint, json=body)
    duration_ms = (time.perf_counter() - started) * 1000.0
    after = _protected_state(database_path, session_id) if negative else None
    after_digest = _digest(after) if negative else None
    after_state = after if negative else business_state()
    try:
        payload = response.json()
    except ValueError:
        payload = None
    code = None
    if isinstance(payload, dict) and isinstance(payload.get("detail"), dict):
        code = payload["detail"].get("code")
    rejected = response.status_code != 200
    assertions = {}
    if negative:
        expected_endpoint = _expected_endpoint(step, session_id, last_run_id=last_run_id)
        condition_ok, condition_details = _condition_broken(
            step, body=body, snapshots=before["snapshots_by_proposal"],
            session_id=session_id, endpoint=endpoint,
        )
        assertions = _negative_assertions(
            step, observed_endpoint=response.url.path, expected_endpoint=expected_endpoint,
            code=code, status_code=response.status_code, condition_ok=condition_ok,
            condition_details=condition_details, before_digest=before_digest,
            after_digest=after_digest,
            changed_keys=_state_diff(before, after), business_kind=business_kind,
        )
    return ConfirmationObservation(
        case_id=case_id,
        round_index=round_index,
        step_index=step_index,
        kind=step.kind,
        business_kind=business_kind,
        operation_label=step.operation,
        operation_id=body["operation_id"],
        target=step.target,
        request=body,
        status_code=response.status_code,
        response=payload,
        rejected=rejected,
        rejection_code=code,
        before_state=before_state,
        after_state=after_state,
        duration_ms=duration_ms,
        endpoint=endpoint,
        entry=step.entry,
        break_field=step.break_field,
        expect_code=step.expect_code,
        path_session_id=session_id,
        assertions=assertions,
    )


def _run_injection(http, session_id, *, case_id, round_index, step_index, step, selected,
                   last_run_id, last_entry_id, database_path):
    # 授权字段注入：确认字段进入 run/edit/steering 三个真实入口，期望请求字段校验就地拒绝且零写入。
    operation_id = str(uuid4())
    injected = {
        "business_kind": selected["business_kind"],
        "proposal_id": selected["proposal_id"],
        "display_entry_id": selected["display_entry_id"],
    }
    if step.entry == "run":
        endpoint = "/api/agent/run"
        body = {"session_id": session_id, "operation_id": operation_id,
                "request": step.text or "确认保存", **injected}
    elif step.entry == "edit":
        # 编辑入口的真实会话锚点来自上一回合，注入的确认字段是该步骤唯一改变的校验条件。
        if not last_entry_id:
            raise ValueError("edit 负向入口需要本会话真实展示节点编号")
        endpoint = "/api/agent/edit"
        body = {"session_id": session_id, "operation_id": operation_id,
                "target_entry_id": last_entry_id, "request": step.text or "确认保存", **injected}
    else:
        if not last_run_id:
            raise ValueError("steering 负向入口需要本会话真实 run_id")
        endpoint = f"/api/agent/runs/{last_run_id}/steering"
        body = {"session_id": session_id, "operation_id": operation_id,
                "message": step.text or "确认保存", **injected}
    body.update(step.overrides)
    return _send(http, session_id, endpoint=endpoint, body=body, step=step, case_id=case_id,
                 round_index=round_index, step_index=step_index, business_kind=step.business_kind,
                 database_path=database_path, last_run_id=last_run_id)


def _run_confirm(
    http, session_id, *, case_id, round_index, step_index, step, prepared, operations, negative,
    cross_targets=None, last_run_id=None, last_entry_id=None, database_path=None,
):
    selected, reason = _select_target(prepared, cross_targets or [], step.business_kind, step.target)
    if selected is None:
        return _unresolved(case_id, round_index, step_index, step, reason)
    if step.entry in {"run", "edit", "steering"}:
        return _run_injection(
            http, session_id, case_id=case_id, round_index=round_index, step_index=step_index,
            step=step, selected=selected, last_run_id=last_run_id, last_entry_id=last_entry_id,
            database_path=database_path,
        )
    if step.kind == "confirm_repeat" and step.operation in operations:
        record = operations[step.operation]
    else:
        record = {
            "operation_id": str(uuid4()),
            "business_kind": step.business_kind,
            "proposal_id": selected["proposal_id"],
            "display_entry_id": selected["display_entry_id"],
        }
        operations[step.operation] = record
    body = {
        "operation_id": record["operation_id"],
        "business_kind": record["business_kind"],
        "proposal_id": record["proposal_id"],
        "display_entry_id": record["display_entry_id"],
    }
    _merge_overrides(body, step.overrides, prepared, cross_targets or [])
    endpoint = f"/api/sessions/{session_id}/confirmations"
    return _send(http, session_id, endpoint=endpoint, body=body, step=step, case_id=case_id,
                 round_index=round_index, step_index=step_index, business_kind=record["business_kind"],
                 database_path=database_path, last_run_id=last_run_id)


def _run_operation_query(http, session_id, *, case_id, round_index, step_index, step, database_path):
    # 取该类最近一次已保存确认操作的编号（含前置链建立的操作），通过专用 GET 查询原结果。
    from test.v2_business_cases import read_state

    state = read_state(database_path)
    saved = [
        item
        for item in state["confirmations"]["items"]
        if item["status"] == "saved" and (step.business_kind is None or item["business_kind"] == step.business_kind)
    ]
    if not saved:
        return ConfirmationObservation(
            case_id=case_id, round_index=round_index, step_index=step_index, kind=step.kind,
            business_kind=step.business_kind, operation_id=str(uuid4()), request={},
            rejected=True, rejection_code="no_operation",
        )
    operation_id = saved[-1]["operation_id"]
    response = http.get(f"/api/sessions/{session_id}/confirmations/{operation_id}")
    payload = response.json() if response.status_code == 200 else None
    return ConfirmationObservation(
        case_id=case_id,
        round_index=round_index,
        step_index=step_index,
        kind=step.kind,
        business_kind=step.business_kind,
        operation_label=step.operation,
        operation_id=operation_id,
        request={},
        status_code=response.status_code,
        response=payload,
        rejected=response.status_code != 200,
    )


def _run_edit(http, session_id, *, case_id, round_index, step_index, step, operations, confirmations):
    target_entry_id = None
    if step.target and step.target.startswith("confirmation:"):
        label = step.target.split(":", 1)[1]
        record = operations.get(label)
        if record:
            target_entry_id = record.get("confirmation_entry_id")
        if target_entry_id is None:
            for item in confirmations:
                if item.operation_label == label and isinstance(item.response, dict):
                    target_entry_id = item.response.get("confirmation_entry_id")
    before_state = business_state()
    response = http.post(
        "/api/agent/edit",
        json={
            "session_id": session_id,
            "operation_id": str(uuid4()),
            "target_entry_id": target_entry_id or str(uuid4()),
            "request": step.text or "",
        },
    )
    return ConfirmationObservation(
        case_id=case_id,
        round_index=round_index,
        step_index=step_index,
        kind=step.kind,
        operation_label=None,
        operation_id=str(uuid4()),
        target=step.target,
        request={"target_entry_id": target_entry_id},
        status_code=response.status_code,
        response=response.json() if response.headers.get("content-type", "").startswith("application/json") else None,
        rejected=response.status_code not in {200, 201},
        before_state=before_state,
        after_state=business_state(),
    )


def _integrity_error(integrity: dict) -> str | None:
    problems: list[str] = []
    if integrity.get("missing_check_ids"):
        problems.append(f"缺少必需检查 {integrity['missing_check_ids']}")
    if integrity.get("duplicate_check_ids"):
        problems.append(f"检查项重复 {integrity['duplicate_check_ids']}")
    if integrity.get("unexpected_check_ids"):
        problems.append(f"计划外检查 {integrity['unexpected_check_ids']}")
    if integrity.get("identity_errors"):
        problems.append(f"检查身份错误 {integrity['identity_errors']}")
    if integrity.get("contract_errors"):
        problems.append(f"检查契约错误 {integrity['contract_errors']}")
    return "; ".join(problems) if problems else None


def _round_status(turns, confirmations, checks, integrity=None) -> str:
    if any(turn.terminal in {"error", "unknown"} for turn in turns if turn.kind == "user"):
        return "error"
    # 检查覆盖不完整属于证据缺口，任何空列表或缺项都不得落入 pass。
    if integrity is not None and not integrity.get("complete", False):
        return "error"
    if any(check.status == "fail" for check in checks):
        return "fail"
    if any(check.status == "pending_review" for check in checks):
        return "pending_review"
    return "pass"


# 观测与派发边界就地置位的停止类别：在业务步骤循环内即时发现，不等最终对账。
OBSERVATION_STOP_CATEGORIES = ("trace_write_failure", "milvus_unavailable")


def execution_unit_of(env: ExperimentEnv, slot: int) -> str:
    # 稳定执行单元身份：追加运行按 attempt 结算，普通槽按 slot 结算，同属父实验作用域。
    if env.attempt_id:
        return f"attempt:{env.attempt_id}"
    return f"slot:{slot}"


def run_case_round(case: V2Case, round_index: int, slot: int, env: ExperimentEnv, model_identity: dict) -> RoundObservation:
    # 执行入口持有包装器完整生命周期：安装时固定实验、执行单元与证据目录；追加沿用父实验作用域。
    scope = env.experiment_scope or env.experiment_id
    ledger = BudgetLedger(env.budget_ledger_path, experiment_id=scope)
    with execution_guards(ledger, env.evidence_dir, experiment_id=scope,
                          execution_unit=execution_unit_of(env, slot),
                          attempt_id=env.attempt_id,
                          expected_trace_failure=env.expected_trace_failure):
        with slot_file_roots(env):
            return _run_case_round(case, round_index, slot, env, model_identity)


@contextmanager
def evaluation_roots(directory, database_path, trace_path, observation_root):
    from app.agent import cache_observation, telemetry
    from test import p0_business_cases

    originals = (database_module.default_database_path, telemetry.trace_database_path,
                 cache_observation.observation_directory, p0_business_cases.TMP_ROOT)
    database_module.default_database_path = lambda: Path(database_path)
    telemetry.trace_database_path = lambda: Path(trace_path)
    cache_observation.observation_directory = lambda: Path(observation_root)
    p0_business_cases.TMP_ROOT = directory / "tmp"
    try:
        with file_roots(directory):
            yield directory
    finally:
        (database_module.default_database_path, telemetry.trace_database_path,
         cache_observation.observation_directory, p0_business_cases.TMP_ROOT) = originals


@contextmanager
def slot_file_roots(env: ExperimentEnv):
    with evaluation_roots(slot_attachment_root(env), env.database_path, env.trace_path,
                          Path(env.execution_root) / "observations") as directory:
        yield directory


@contextmanager
def file_roots(directory):
    from app.application.session import service as session_service
    from app.interfaces import http as http_interface

    directory = Path(directory)
    original_root = session_service.PROJECT_ROOT
    original_factory = http_interface.create_file_tools
    session_service.PROJECT_ROOT = directory
    http_interface.create_file_tools = partial(original_factory, tmp_root=directory / "tmp")
    try:
        yield directory
    finally:
        http_interface.create_file_tools = original_factory
        session_service.PROJECT_ROOT = original_root


def _run_case_round(case: V2Case, round_index: int, slot: int, env: ExperimentEnv, model_identity: dict) -> RoundObservation:
    from test.check_structured_confirmation_http import Live
    from test.v2_business_cases import build_precondition, evaluate

    check_model_identity(model_identity)
    if (env.case_id, env.round_index, env.slot, env.model_identity) != (case.case_id, round_index, slot, model_identity):
        raise ValueError("执行参数与槽位身份不匹配")
    claim = claim_slot(env)
    database_path = Path(env.database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path = Path(env.trace_path)
    from app.agent import telemetry

    attachment_root = slot_attachment_root(env)
    attachment_root.mkdir(parents=True, exist_ok=False)
    telemetry.trace_database_path = lambda: trace_path
    database_module.default_database_path = lambda: database_path
    # 逐模型调用观测同样隔离到本次实验目录：应用默认写 backend/data/cache-observation，
    # 不重定向会让评测自写生产目录并在收尾被判为 business_data 漂移。
    from app.agent import cache_observation

    observation_root = Path(env.execution_root) / "observations"
    observation_root.mkdir(parents=True, exist_ok=True)
    cache_observation.observation_directory = lambda: observation_root
    evidence_dir = Path(env.evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    session_id = str(uuid4())
    turns: list[TurnObservation] = []
    confirmations: list[ConfirmationObservation] = []
    checks: list[CheckResult] = []
    operations: dict[str, dict] = {}
    prepared: list[dict] = []
    startup_ms = None
    precondition_ms = None
    started = None
    round_started = time.perf_counter()
    ledger = BudgetLedger(Path(env.budget_ledger_path), experiment_id=env.experiment_scope or env.experiment_id)
    business_ms_total = 0.0
    assertion_ms = 0.0
    serialize_ms = 0.0
    artifact_write_ms = 0.0
    step_timings: list[dict] = []
    pending_stop: dict | None = None
    try:
        server_started = time.perf_counter()
        with Server(app) as server, client(server.base_url, timeout=900) as http:
            ledger.check_dispatch()
            create_session(http, session_id, title=case.case_id)
            context = BusinessContext(
                timezone=BUSINESS_TIMEZONE,
                business_date=env.business_date,
                session_id=session_id,
                run_id="",
                request_entry_id="",
                source_entry_id="",
            )
            live = Live(server)
            startup_ms = (time.perf_counter() - server_started) * 1000.0
            ledger.check_dispatch()
            precondition_started = time.perf_counter()
            precondition: PreconditionEvidence = build_precondition(
                case, http=http, session_id=session_id, business_context=context, live=live
            )
            precondition_ms = (time.perf_counter() - precondition_started) * 1000.0
            _write_json(
                evidence_dir / "precondition.json",
                {
                    **precondition.model_dump(),
                    "startup_ms": startup_ms,
                    "precondition_ms": precondition_ms,
                },
            )
            cross_targets = precondition.detail.get("cross_session_targets", [])
            # 场景业务耗时只覆盖业务步骤区间；评测开销在业务终态之后单独计量。
            started = time.perf_counter()
            for index, step in enumerate(case.steps):
                step_index = index + 1
                # 已领取槽在停止置位后不再启动新业务步骤。
                ledger.check_dispatch()
                step_started = time.perf_counter()
                if step.kind == "user":
                    attachments = []
                    if step.attachments:
                        import base64

                        for item in step.attachments:
                            attachments.append(
                                {
                                    "kind": "upload",
                                    "attachment_id": str(uuid4()),
                                    "file_name": item.file_name,
                                    "data_base64": base64.b64encode(item.text.encode("utf-8")).decode("ascii"),
                                }
                            )
                    turn = capture_turn(
                        http,
                        session_id,
                        case_id=case.case_id,
                        round_index=round_index,
                        step_index=step_index,
                        request_text=step.text,
                        attachments=attachments,
                    )
                    turns.append(turn)
                    prepared.extend(_prepared(turn.events))
                elif step.kind in {"confirm", "confirm_repeat", "confirm_negative"}:
                    observation = _run_confirm(
                        http,
                        session_id,
                        case_id=case.case_id,
                        round_index=round_index,
                        step_index=step_index,
                        step=step,
                        prepared=prepared,
                        operations=operations,
                        negative=step.kind == "confirm_negative",
                        cross_targets=cross_targets,
                        last_run_id=turns[-1].run_id if turns else None,
                        last_entry_id=turns[-1].request_entry_id if turns else None,
                        database_path=str(database_path),
                    )
                    confirmations.append(observation)
                    if isinstance(observation.response, dict) and observation.response.get("confirmation_entry_id"):
                        operations[step.operation] = {
                            **operations.get(step.operation, {}),
                            "confirmation_entry_id": observation.response["confirmation_entry_id"],
                            "operation_id": observation.operation_id,
                        }
                    if step.kind == "confirm_negative":
                        assertion = observation.assertions or {}
                        if assertion.get("zero_write") is False or observation.rejected is False:
                            category = ("unauthorized_write"
                                        if step.break_field == "authorization_injection"
                                        else "wrong_target_write")
                            pending_stop = trigger_stop(
                                ledger, category, owner=env.experiment_id,
                                trigger="run_case_round:negative", slot=slot,
                                operation_id=observation.operation_id,
                                evidence_refs=[f"cases/{case.case_id}/round-{round_index}/round.json"],
                                detail={"break_field": step.break_field, "assertions": assertion},
                            )
                elif step.kind == "operation_query":
                    confirmations.append(
                        _run_operation_query(
                            http, session_id, case_id=case.case_id, round_index=round_index,
                            step_index=step_index, step=step, database_path=str(database_path),
                        )
                    )
                elif step.kind == "edit":
                    confirmations.append(
                        _run_edit(
                            http, session_id, case_id=case.case_id, round_index=round_index,
                            step_index=step_index, step=step, operations=operations,
                            confirmations=confirmations,
                        )
                    )
                business_terminal_at = time.perf_counter()
                business_ms_total += (business_terminal_at - step_started) * 1000.0
                step_timings.append({
                    "step_index": step_index, "kind": step.kind,
                    "business_ms": (business_terminal_at - step_started) * 1000.0,
                })
                round_view = RoundObservation(
                    case_id=case.case_id,
                    round_index=round_index,
                    slot=slot,
                    step_index=step_index,
                    session_id=session_id,
                    process_index=env.process_index,
                    turns=list(turns),
                    confirmations=list(confirmations),
                    checks=[],
                    status="pass",
                )
                assertion_started = time.perf_counter()
                step_checks = evaluate(
                    case,
                    observation=round_view,
                    service=app.state.business,
                    database_path=str(database_path),
                    business_context=context,
                )
                assertion_ms += (time.perf_counter() - assertion_started) * 1000.0
                checks.extend(step_checks)
                # 观测/派发边界就地置位的停止在槽位循环内立即发现：不等待最终对账，保留业务与观测各自结果。
                observed_stop = ledger.stop_info()
                if observed_stop is not None and observed_stop.get("reason_category") in OBSERVATION_STOP_CATEGORIES:
                    _write_json(evidence_dir / f"step-{step_index}.json", round_view.model_dump())
                    raise StopTriggered(observed_stop)
                serialize_started = time.perf_counter()
                payload = round_view.model_dump()
                serialize_ms += (time.perf_counter() - serialize_started) * 1000.0
                artifact_started = time.perf_counter()
                _write_json(evidence_dir / f"step-{step_index}.json", payload)
                artifact_write_ms += (time.perf_counter() - artifact_started) * 1000.0
                # 泄密：公开输出命中敏感片段即置位共享停止；证据先落盘再中断。
                leaked = [item for item in step_checks
                          if item.status == "fail" and item.reason.startswith("output_excludes")]
                if leaked:
                    pending_stop = trigger_stop(
                        ledger, "leak", owner=env.experiment_id,
                        trigger="run_case_round:output_excludes", slot=slot,
                        evidence_refs=[f"cases/{case.case_id}/round-{round_index}/round.json"],
                        detail={"checks": [item.check_id for item in leaked]},
                    )
                if pending_stop is not None:
                    raise StopTriggered(pending_stop)
            duration_ms = business_ms_total
            total_wall_ms = (time.perf_counter() - round_started) * 1000.0
            from test.v2_business_cases import check_coverage

            integrity = check_coverage(case, checks, round_index=round_index)
            integrity_error = _integrity_error(integrity)
            observation = RoundObservation(
                case_id=case.case_id,
                round_index=round_index,
                slot=slot,
                session_id=session_id,
                process_index=env.process_index,
                turns=turns,
                confirmations=confirmations,
                checks=checks,
                check_integrity=integrity,
                usage=_sum_turns_usage(turns),
                duration_ms=duration_ms,
                startup_ms=startup_ms,
                precondition_ms=precondition_ms,
                step_timings=step_timings,
                eval_overhead_ms={
                    "assertion_ms": assertion_ms,
                    "serialize_ms": serialize_ms,
                    "artifact_write_ms": artifact_write_ms,
                },
                total_wall_ms=total_wall_ms,
                status=_round_status(turns, confirmations, checks, integrity),
                error=integrity_error,
                error_type=(
                    next((turn.error_type for turn in turns if turn.status == "failed" and turn.error_type), None)
                    or ("CheckCoverageError" if integrity_error else None)
                ),
            )
    except BaseException as error:
        _write_json(
            evidence_dir / "error.json",
            {
                "case_id": case.case_id,
                "round_index": round_index,
                "slot": slot,
                "error": str(error),
                "error_type": exception_type(error),
                "startup_ms": startup_ms,
                "precondition_ms": precondition_ms,
                # 未完成场景记录截止时间与已完成步骤数，供已知终态判定使用。
                "cutoff_ms": None if started is None else (time.perf_counter() - started) * 1000.0,
                "completed_steps": len(turns) + len(confirmations),
                "completed_turns": [turn.model_dump() for turn in turns],
            },
        )
        from test.v2_business_cases import check_coverage

        observation = RoundObservation(
            case_id=case.case_id, round_index=round_index, slot=slot,
            session_id=session_id, process_index=env.process_index,
            turns=turns, confirmations=confirmations, checks=checks,
            check_integrity=check_coverage(case, checks, round_index=round_index),
            usage=_sum_turns_usage(turns), status="error", error=str(error), error_type=exception_type(error),
            startup_ms=startup_ms, precondition_ms=precondition_ms,
            total_wall_ms=(time.perf_counter() - round_started) * 1000.0,
        )
        _write_json(evidence_dir / "round.json", observation.model_dump())
        finish_slot(claim, observation)
        raise
    _write_json(evidence_dir / "round.json", observation.model_dump())
    finish_slot(claim, observation)
    return observation


def _sum_turns_usage(turns: list[TurnObservation]):
    present = [turn.usage for turn in turns if turn.usage is not None]
    if not present:
        return None
    return {field: sum(item[field] for item in present) for field in USAGE_FIELDS}


# ---------- 聚合 ----------


def _median(values: list[float]):
    ordered = sorted(values)
    count = len(ordered)
    if count == 0:
        return None
    middle = count // 2
    return ordered[middle] if count % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _quantile(values: list[float], q: float):
    import statistics

    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[int(round(q * 100)) - 1]


def _span(values: list[float]) -> dict:
    clean = [value for value in values if value is not None]
    if not clean:
        return {"samples": 0, "mean": None, "p50": None, "p95": None, "max": None}
    return {
        "samples": len(clean),
        "mean": sum(clean) / len(clean),
        "p50": _median(clean),
        "p95": _quantile(clean, 0.95),
        "max": max(clean),
    }


def _category_of(case_id: str) -> str:
    for category, prefix in CASE_ID_PREFIXES.items():
        if case_id.startswith(prefix):
            return category
    return "unknown"


def _usage_totals(rounds: list[RoundObservation]) -> dict:
    seen: dict[str, tuple[dict, dict | None]] = {}
    duplicates: list[str] = []
    missing = 0
    calls = {"done": 0, "error": 0, "unknown": 0}
    turns_total = 0
    for observation in rounds:
        for turn in observation.turns:
            turns_total += 1
            calls[turn.terminal] = calls.get(turn.terminal, 0) + 1
            if turn.usage is None:
                missing += 1
                continue
            if turn.run_id in seen:
                duplicates.append(turn.run_id)
                continue
            seen[turn.run_id] = (turn.usage, turn.usage_sources)
    usages = [item[0] for item in seen.values()]
    # 顶层字段和沿用既有口径：缺失键就地失败，禁止把未上报字段补零后宣称完整总量。
    totals: dict = {field: sum(item[field] for item in usages) for field in USAGE_FIELDS}
    totals["fields"] = {
        field: {
            "total": (
                sum(item[field] for item in usages if item.get(field) is not None)
                if any(item.get(field) is not None for item in usages)
                else None
            ),
            "samples": sum(1 for item in usages if item.get(field) is not None),
            "missing": sum(1 for item in usages if item.get(field) is None),
        }
        for field in USAGE_FIELDS
    }
    provenance: dict[str, str] = {}
    for field in USAGE_FIELDS:
        labels = {src.get(field, SOURCE_UNKNOWN) for _usage, src in seen.values() if src}
        provenance[field] = labels.pop() if len(labels) == 1 else ("mixed" if labels else SOURCE_UNKNOWN)
    totals["provenance"] = provenance
    # 缓存命中率只使用三项输入均真实已知的共同样本；默认零不进入共同样本。
    common = [
        usage
        for usage, src in seen.values()
        if src is not None and all(src.get(field) == SOURCE_REPORTED for field in CACHE_COMMON_FIELDS)
    ]
    denominator = sum(item["input"] + item["cache_read"] + item["cache_write"] for item in common)
    numerator = sum(item["cache_read"] for item in common)
    totals["cache_samples"] = len(common)
    totals["cache_read_sum"] = numerator
    totals["cache_input_sum"] = denominator
    totals["cache_hit_rate"] = (numerator / denominator) if denominator else None
    # 非权威对照：把默认零机械套用旧口径的参考值，不参与结论。
    mechanical_num = sum(item["cache_read"] for item in usages)
    mechanical_den = sum(item["input"] + item["cache_read"] + item["cache_write"] for item in usages)
    totals["mechanical_cache_read_sum"] = mechanical_num
    totals["mechanical_cache_input_sum"] = mechanical_den
    totals["mechanical_cache_hit_rate"] = (mechanical_num / mechanical_den) if mechanical_den else None
    totals["runs"] = len(seen)
    totals["duplicate_runs"] = duplicates
    totals["missing_turns"] = missing
    totals["calls"] = {"turns": turns_total, "by_terminal": calls}
    return totals


def _duration_totals(rounds: list[RoundObservation]) -> dict:
    return {
        "round_ms": _span([r.duration_ms for r in rounds]),
        "client_first_body_ms": _span(
            [t.client_first_body_ms for r in rounds for t in r.turns if t.client_first_body_ms is not None]
        ),
        "run_ms": _span([t.duration_ms for r in rounds for t in r.turns if t.kind == "user" and t.duration_ms is not None]),
        "confirmation_ms": _span(
            [c.duration_ms for r in rounds for c in r.confirmations if c.duration_ms is not None]
        ),
    }


def annotate_check_integrity(rounds: list[RoundObservation], cases: list[V2Case]) -> list[RoundObservation]:
    # 读取既有证据时按冻结用例派生覆盖结论：原始 status/checks 原样保留，只附加派生标记。
    from test.v2_business_cases import check_coverage

    by_case = {case.case_id: case for case in cases}
    annotated: list[RoundObservation] = []
    for observation in rounds:
        case = by_case.get(observation.case_id)
        if case is None:
            integrity = {
                "complete": False,
                "required_check_ids": [],
                "observed_check_ids": [item.check_id for item in observation.checks],
                "missing_check_ids": [],
                "duplicate_check_ids": [],
                "unexpected_check_ids": [item.check_id for item in observation.checks],
                "identity_errors": [],
                "contract_errors": [f"未知用例 {observation.case_id}"],
            }
        else:
            integrity = check_coverage(case, observation.checks, round_index=observation.round_index)
        annotated.append(observation.model_copy(update={"check_integrity": integrity}))
    return annotated


def _effective_status(observation: RoundObservation) -> str:
    # 已有 fail/error 保留业务终态；缺证据只覆盖 pass/pending，避免掩盖真实失败。
    if observation.status in {"fail", "error"}:
        return observation.status
    integrity = observation.check_integrity
    if integrity is not None and not integrity.get("complete", False):
        return "insufficient_evidence"
    return observation.status


def aggregate(rounds: list[RoundObservation], planned_case_ids=None, repeats: int = ROUNDS) -> dict:
    validate_unique_rounds(rounds)
    ordered = sorted(rounds, key=lambda item: (item.case_id, item.round_index))
    case_ids = sorted(set(planned_case_ids) if planned_case_ids is not None else {item.case_id for item in ordered})
    if any(item.case_id not in case_ids for item in ordered):
        raise ValueError("观测包含计划外正式用例")
    rounds_by_case: dict[str, dict[int, RoundObservation]] = {case_id: {} for case_id in case_ids}
    for observation in ordered:
        rounds_by_case[observation.case_id][observation.round_index] = observation

    per_round = []
    for index in range(repeats):
        observed = [r for r in ordered if r.round_index == index]
        passed = [r for r in observed if _effective_status(r) == "pass"]
        missing = [r for r in observed if _effective_status(r) == "insufficient_evidence"]
        per_round.append(
            {
                "round_index": index,
                "observed": len(observed),
                "passed": len(passed),
                "insufficient_evidence": len(missing),
                "rate": (len(passed) / len(case_ids)) if case_ids else None,
            }
        )

    # 正式分母固定为计划用例 × 轮次；缺证据单列，不改变分母。
    executions = len(case_ids) * repeats if planned_case_ids is not None else len(ordered)
    passed_executions = sum(1 for r in ordered if _effective_status(r) == "pass")
    distribution = {f"{count}/{repeats}": 0 for count in range(repeats + 1)}
    stable = 0
    for case_id in case_ids:
        bucket = rounds_by_case[case_id]
        passed_rounds = sum(
            1 for index in range(repeats)
            if bucket.get(index) is not None and _effective_status(bucket[index]) == "pass"
        )
        distribution[f"{passed_rounds}/{repeats}"] += 1
        if passed_rounds == repeats:
            stable += 1

    categories: dict[str, dict] = {}
    for case_id in case_ids:
        category = _category_of(case_id)
        bucket = categories.setdefault(
            category,
            {"cases": 0, "executions": 0, "passed_executions": 0, "stable_cases": 0, "pending_review_rounds": 0},
        )
        bucket["cases"] += 1
        observed = [rounds_by_case[case_id][i] for i in range(repeats) if rounds_by_case[case_id].get(i)]
        bucket["executions"] += repeats if planned_case_ids is not None else len(observed)
        bucket["passed_executions"] += sum(1 for r in observed if _effective_status(r) == "pass")
        bucket["missing_evidence_executions"] = bucket.get("missing_evidence_executions", 0) + sum(
            1 for r in observed if _effective_status(r) == "insufficient_evidence"
        )
        if len(observed) == repeats and all(_effective_status(r) == "pass" for r in observed):
            bucket["stable_cases"] += 1
        bucket["pending_review_rounds"] += sum(
            1 for r in observed if _effective_status(r) == "pending_review"
        )
    for bucket in categories.values():
        bucket["execution_pass_rate"] = bucket["passed_executions"] / bucket["executions"] if bucket["executions"] else None
        bucket["stable_pass_rate"] = bucket["stable_cases"] / bucket["cases"] if bucket["cases"] else None

    failures = [
        {"case_id": r.case_id, "round_index": r.round_index, "slot": r.slot, "status": _effective_status(r)}
        for r in ordered
        if _effective_status(r) not in {"pass", "pending_review", "insufficient_evidence"}
    ]
    missing_evidence = [
        {
            "case_id": r.case_id,
            "round_index": r.round_index,
            "slot": r.slot,
            "missing_check_ids": (r.check_integrity or {}).get("missing_check_ids", []),
            "duplicate_check_ids": (r.check_integrity or {}).get("duplicate_check_ids", []),
            "unexpected_check_ids": (r.check_integrity or {}).get("unexpected_check_ids", []),
            "identity_errors": (r.check_integrity or {}).get("identity_errors", []),
            "contract_errors": (r.check_integrity or {}).get("contract_errors", []),
        }
        for r in ordered
        if r.check_integrity is not None and not r.check_integrity.get("complete", False)
    ]
    pending_review = [
        {"case_id": r.case_id, "round_index": r.round_index, "check_id": c.check_id, "reason": c.reason}
        for r in ordered
        for c in r.checks
        if c.status == "pending_review"
    ]
    per_case = {}
    for case_id in case_ids:
        bucket = rounds_by_case[case_id]
        per_case[case_id] = {
            "category": _category_of(case_id),
            "rounds": {str(i): (_effective_status(bucket[i]) if bucket.get(i) else None) for i in range(repeats)},
            "passed_rounds": sum(
                1 for i in range(repeats) if bucket.get(i) and _effective_status(bucket[i]) == "pass"
            ),
        }
    return {
        "independent_cases": len(case_ids),
        "expected_cases": len(case_ids),
        "repeats_per_case": repeats,
        "executions": executions,
        "observed_executions": len(ordered),
        "expected_executions": len(case_ids) * repeats,
        "passed_executions": passed_executions,
        "missing_evidence_executions": len(missing_evidence),
        "execution_pass_rate": (passed_executions / executions) if executions else None,
        "stable_cases": stable,
        "stable_pass_rate": (stable / len(case_ids)) if case_ids else None,
        "per_round": per_round,
        "stability_distribution": distribution,
        "categories": categories,
        "per_case": per_case,
        "usage": _usage_totals(ordered),
        "durations": _duration_totals(ordered),
        "failures": failures,
        "missing_evidence": missing_evidence,
        "pending_review": pending_review,
    }


def _public_text(turn: TurnObservation) -> str:
    parts: list[str] = []
    for event in turn.events:
        if event["event"] == "message_update" and event["data"].get("update_type") == "text_delta":
            parts.append("".join(block.get("text", "") for block in event["data"].get("content", [])))
    return "".join(parts)


def _final_state(observation) -> tuple[dict | None, str | None, int | None]:
    # 最终状态取最后业务步骤之后的真实持久化结果：在受理成功确认、user 回合与已受理 edit 中
    # 按步骤序取最后发生者，避免一律优先最后确认而忽略其后的实际变化；无候选时明确未知。
    candidates: list[tuple[int, dict, str]] = []
    for item in observation.confirmations:
        if (item.kind in {"confirm", "confirm_repeat"} and not item.rejected
                and item.status_code == 200 and isinstance(item.after_state, dict)):
            candidates.append((item.step_index, item.after_state, "confirmation_after_state"))
        elif item.kind == "edit" and not item.rejected and isinstance(item.after_state, dict):
            candidates.append((item.step_index, item.after_state, "edit_after_state"))
    for turn in observation.turns:
        if isinstance(turn.after_state, dict):
            candidates.append((turn.step_index, turn.after_state, "last_turn_prepare_state"))
    if not candidates:
        return None, None, None
    step_index, state, source = max(candidates, key=lambda item: item[0])
    return state, source, step_index


def build_review_packet(rounds: list[RoundObservation], cases: list[V2Case]) -> list[dict]:
    # 语义审阅包：每项携带公开正文、工具调用、确认请求与前后状态等可核对证据引用。
    by_case = {case.case_id: case for case in cases}
    packet: list[dict] = []
    for observation in rounds:
        case = by_case.get(observation.case_id)
        if case is None:
            continue
        for check in observation.checks:
            if check.status != "pending_review":
                continue
            standards = {
                item.review_id: item.standard
                for item in case.manual_review
            }
            final_state, final_state_source, final_state_step = _final_state(observation)
            packet.append(
                {
                    "case_id": observation.case_id,
                    "round_index": observation.round_index,
                    "slot": observation.slot,
                    "check_id": check.check_id,
                    "standard": standards.get(check.check_id, check.reason),
                    "product_basis": case.product_basis,
                    "session_id": observation.session_id,
                    "run_ids": [turn.run_id for turn in observation.turns if turn.run_id],
                    "evidence_refs": list(check.evidence_refs),
                    "assistant_texts": [_public_text(turn) for turn in observation.turns],
                    "tool_calls": [
                        {"name": call.name, "arguments": call.arguments, "is_error": call.is_error}
                        for turn in observation.turns
                        for call in turn.tool_calls
                    ],
                    "confirmations": [
                        {
                            "step_index": item.step_index,
                            "kind": item.kind,
                            "request": item.request,
                            "status_code": item.status_code,
                            "rejection_code": item.rejection_code,
                            "assertions": item.assertions,
                        }
                        for item in observation.confirmations
                    ],
                    "final_state": final_state,
                    "final_state_source": final_state_source,
                    "final_state_step": final_state_step,
                    "final_state_evidence": (
                        [f"cases/{observation.case_id}/round-{observation.round_index}/round.json"]
                        if final_state is not None
                        else []
                    ),
                }
            )
    return packet


def load_v2_reviews(path) -> list[dict]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    items = payload["reviews"] if isinstance(payload, dict) else payload
    index = {(item["case_id"], item["round_index"], item["check_id"]) for item in items}
    if len(index) != len(items):
        raise ValueError("审阅结论存在重复身份")
    return items


def apply_v2_review(
    rounds: list[RoundObservation], reviews: list[dict]
) -> list[RoundObservation]:
    # 审阅结论只作用于 pending_review 检查项，并按新结论重算每轮状态。
    index = {(item["case_id"], item["round_index"], item["check_id"]): item for item in reviews}
    unknown = [key for key in index if not any(
        r.case_id == key[0] and r.round_index == key[1]
        and any(c.check_id == key[2] and c.status == "pending_review" for c in r.checks)
        for r in rounds
    )]
    if unknown:
        raise ValueError(f"审阅结论指向未知或已判定检查项：{unknown[:5]}")
    updated: list[RoundObservation] = []
    for observation in rounds:
        checks: list[CheckResult] = []
        for check in observation.checks:
            if check.status != "pending_review":
                checks.append(check)
                continue
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
                        "reviewer": verdict.get("reviewer"),
                        "reviewed_by_agent": True,
                    }
                )
            )
        updated.append(
            observation.model_copy(
                update={
                    "checks": checks,
                    "status": _round_status(
                        observation.turns, observation.confirmations, checks,
                        observation.check_integrity,
                    ),
                }
            )
        )
    return updated


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    digest.update(Path(path).read_bytes())
    return digest.hexdigest()


def git_output(root, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def load_schedule(experiment_dir) -> dict:
    # 每进程独立 JSONL，按 (case_id, round_index) 取最后一条记录。
    merged: dict[tuple[str, int], dict] = {}
    for path in sorted((Path(experiment_dir) / "logs").glob("schedule-p*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            merged[(record["case_id"], record["round_index"])] = record
    return merged


def _directory_digest(root: Path) -> dict:
    files = {}
    if not root.exists():
        return {"exists": False, "files": files}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            stat = path.stat()
            files[str(path.relative_to(root))] = {
                "sha256": sha256_file(path),
                "bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
    return {"exists": True, "files": files}


def _sidecar_stat(path: Path) -> dict:
    # SQLite sidecar（-wal/-shm）的状态一并采集：只读探针不 checkpoint、不删除。
    sidecars: dict[str, dict | None] = {}
    for suffix in ("-wal", "-shm"):
        target = Path(str(path) + suffix)
        if target.exists():
            stat = target.stat()
            sidecars[suffix] = {"bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        else:
            sidecars[suffix] = None
    return sidecars


def _file_identity(path: Path) -> dict:
    stat = path.stat()
    return {"sha256": sha256_file(path), "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _content_identity(path: Path) -> dict:
    # 副本只比较内容与大小：复制不保留源 mtime，时间戳不参与副本一致性判定。
    stat = path.stat()
    return {"sha256": sha256_file(path), "bytes": stat.st_size}


def _trace_source_files(path: Path) -> dict[str, Path]:
    # 源文件集合：库本体与既有 sidecar；采集不得新增、删除或改写其中任何一项。
    names = {path.name: path}
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            names[sidecar.name] = sidecar
    return names


def _consistency_error(detail: str, *, before: dict, after: dict) -> dict:
    # 无法取得一致副本时返回失败结论：不得给出 read_only=true 的成功结果。
    return {
        "exists": True,
        "read_only": False,
        "consistent": False,
        "error": detail,
        "source_before": before,
        "source_after": after,
    }


# 受保护比较纳入的稳定身份字段：源文件集合、内容、大小与 mtime 逐项参与判定。
# 本次采集的工件元数据（副本目录、副本开库路径、采集时刻）只留作审计证据，
# 不参与保护比较：副本目录每次采集都新建，路径不可能在两次采集之间保持相等。
PROTECTED_TRACE_FIELDS = (
    "exists",
    "read_only",
    "source_files",
    "source_identity",
    "source_unchanged",
    "consistency",
    "bytes",
    "mtime_ns",
    "sidecars",
    "user_version",
    "spans",
    "schema_columns",
    "schema_indexes",
)

# 采集工件元数据：留证但不参与稳定身份比较。
TRACE_ARTIFACT_FIELDS = ("copy_dir", "opened_path", "wal_bytes_before_open", "wal_bytes_copied",
                          "copied_files", "taken_at")


def _trace_protected_identity(stat: dict) -> dict:
    # 稳定受保护身份：只取与源库及副本内容身份有关的字段，剔除每次采集必然变化的副本路径。
    if not isinstance(stat, dict):
        return stat
    return {key: stat[key] for key in PROTECTED_TRACE_FIELDS if key in stat}


def _trace_artifact_metadata(stat: dict) -> dict:
    if not isinstance(stat, dict):
        return {}
    return {key: stat[key] for key in TRACE_ARTIFACT_FIELDS if key in stat}


def _source_signature(source: dict[str, Path]) -> dict:
    # 一致快照判定签名：源数据文件（库本体与全部既有 sidecar，含 -wal/-shm/-journal）
    # 的集合 + 内容/大小/mtime。
    #
    # 采集器全程不打开源库（SQLite 只在副本内开库），`-shm` 因此不再由探针自身连接
    # 派生，可完整纳入零写入判定，不需要从稳定性比较中排除。
    signature = {}
    for name, item in sorted(source.items()):
        try:
            signature[name] = _file_identity(item)
        except (FileNotFoundError, PermissionError, OSError):
            try:
                stat = item.stat()
                signature[name] = {"locked": True, "bytes": stat.st_size}
            except OSError:
                signature[name] = {"unreadable": True}
    return signature


def _signature_stable(before: dict, after: dict) -> bool:
    # 窗口稳定性判定：源数据文件集合必须一致，逐文件严格比较。
    # 一侧可读、另一侧被锁定时，以大小与 mtime 比对，避免"读取方式差异"被误判为改写。
    if set(before) != set(after):
        return False
    for name in before:
        left, right = before[name], after[name]
        if "sha256" in left and "sha256" in right:
            if left != right:
                return False
            continue
        if ("sha256" in left) != ("sha256" in right):
            readable, locked = (left, right) if "sha256" in left else (right, left)
            if (locked.get("bytes") != readable.get("bytes")
                    or locked.get("mtime_ns") != readable.get("mtime_ns")):
                return False
            continue
        if left != right:
            return False
    return True


# 一致快照取得机制：文件级采样，全过程不打开源库。
FILE_SNAPSHOT_MECHANISM = "file_level_snapshot_no_source_open"


def _copy_with_source_check(source: dict[str, Path], copy_dir: Path, *, path: Path) -> dict:
    """在**不打开源库**的前提下取得一致文件副本。

    一致性判定：复制前后对源数据文件集合（库本体与全部既有 sidecar，含 -shm）取
    内容/大小/mtime 签名并逐项比对。签名一致即证明复制期间源未发生可观察写入；
    签名偏离即判失败，禁止把新状态当作基线。SQLite 只在副本内开库（见
    `_trace_store_stat`），源库在整个采集过程中不建立任何连接，也不取得任何锁。
    """
    before = _source_signature(_trace_source_files(path))
    for name, item in source.items():
        if item.exists():
            shutil.copyfile(item, copy_dir / name)
    # 源文件集合可能在采样与复制之间新增 sidecar：以复制后的集合为准补齐。
    for name, item in _trace_source_files(path).items():
        if not (copy_dir / name).exists() and item.exists():
            shutil.copyfile(item, copy_dir / name)
    after = _source_signature(_trace_source_files(path))
    if not _signature_stable(before, after):
        for copied in copy_dir.glob("*"):
            copied.unlink()
        return {"acquired": False,
                "mechanism": FILE_SNAPSHOT_MECHANISM,
                "reason": "一致快照取得失败：复制前后源文件签名不一致（复制期间源被写入）",
                "blocked": True,
                "signature": before, "post_copy_signature": after}
    return {"acquired": True,
            "mechanism": FILE_SNAPSHOT_MECHANISM,
            "samples": 2,
            "signature": before,
            "post_copy_signature": after,
            "proof": "复制前后源文件集合与内容/大小/mtime 签名一致；全过程未打开源库"}


def _trace_store_stat(path: Path, *, snapshot_root=None) -> dict:
    # 源文件零写入：不对源库创建任何 SQLite 连接，仅做标准文件复制。
    # 一致副本 = 库本体与既有 sidecar 的完整文件副本，SQLite 只打开本次独立副本；
    # 副本内的 WAL 恢复由 SQLite 处理，不手写 WAL parser、不 checkpoint 源库。
    from app.agent import telemetry

    if not path.exists():
        return {"exists": False}
    root = Path(snapshot_root) if snapshot_root else Path(
        os.environ.get("V2_TRACE_SNAPSHOT_ROOT") or (BACKEND / "temp" / "v2-repair" / "trace-snapshots")
    )
    source = _trace_source_files(path)
    copy_dir = root / f"{path.stem}-{uuid4().hex}"
    copy_dir.mkdir(parents=True, exist_ok=True)
    # 一致性取得机制：文件级采样覆盖复制前后；签名一致才认定副本在一致状态内取得。
    window = _copy_with_source_check(source, copy_dir, path=path)
    if not window["acquired"]:
        return _consistency_error(
            "一致快照未取得：" + window["reason"],
            before=window.get("signature") or {},
            after=window.get("post_copy_signature")
            or _source_signature(_trace_source_files(path)),
        )
    before = window["signature"]
    # 复制后的复核：以"复制前签名 vs 复制后签名"的数据文件内容比对为准，
    # 内容未变即证明复制期间源未被写入。
    after = _source_signature(_trace_source_files(path))
    if not _signature_stable(before, after):
        return _consistency_error("采集窗口内源文件被改写，副本不保证一致", before=before, after=after)
    copied = {name: copy_dir / name for name in after if (copy_dir / name).exists()}
    copied_identity = {name: _content_identity(item) for name, item in copied.items()}
    # 副本一致性核对全部数据文件（库本体与既有 sidecar，含 -shm）：采集不打开源库，
    # -shm 的副本内容应与源一致。
    data_names = sorted(name for name, item in before.items() if "sha256" in item)
    source_content = {name: {"sha256": before[name]["sha256"], "bytes": before[name]["bytes"]}
                      for name in data_names}
    copied_content = {name: copied_identity[name] for name in data_names if name in copied_identity}
    if copied_content != source_content:
        return _consistency_error("副本内容与源文件不一致", before=source_content,
                                  after=copied_content)
    # 副本文件集合必须覆盖源集合中的全部数据文件与既有 sidecar。
    missing_copies = sorted(set(before) - set(copied_identity))
    if missing_copies:
        return _consistency_error("副本缺少源文件集合条目", before=sorted(before),
                                  after=sorted(copied_identity))
    copied_files_at_capture = sorted(copied_identity)

    result: dict = {
        "exists": True,
        "bytes": before[path.name].get("bytes"),
        "mtime_ns": before[path.name].get("mtime_ns"),
        "sidecars": _sidecar_stat(path),
        "source_files": sorted(before),
        # 源文件集合与逐文件内容/大小/mtime 的完整快照：受保护身份的核心证据。
        "source_identity": {name: dict(item) for name, item in sorted(before.items())},
        "source_unchanged": True,
        "consistency": "文件级采样取得的 db+sidecar 完整文件副本；源文件集合/内容/大小/mtime 逐项一致（未打开源库）",
        "acquisition": {key: value for key, value in window.items() if key != "signature"},
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "copy_dir": str(copy_dir),
        # 复制完成时副本内的完整文件集合：db 本体与全部既有 sidecar。
        "copied_files": copied_files_at_capture,
        "wal_bytes_before_open": copied_identity.get(f"{path.name}-wal", {}).get("bytes", 0),
        "wal_bytes_copied": copied_identity.get(f"{path.name}-wal", {}).get("bytes", 0),
    }

    async def probe() -> dict:
        # 只打开本次独立副本：副本开库与 WAL 恢复都在副本目录内完成。
        connection = await aiosqlite.connect(str(copy_dir / path.name), isolation_level=None)
        connection.row_factory = aiosqlite.Row
        try:
            cursor = await connection.execute("PRAGMA user_version")
            row = await cursor.fetchone()
            await cursor.close()
            version = None if row is None else int(row[0])
            if version != telemetry.SCHEMA_VERSION:
                raise RuntimeError(
                    f"观测库 user_version={version} 与探针期望 {telemetry.SCHEMA_VERSION} 不符"
                )
            cursor = await connection.execute("PRAGMA table_info(spans)")
            columns = [item[1] for item in await cursor.fetchall()]
            await cursor.close()
            if columns != list(telemetry._COLUMNS):
                raise RuntimeError(f"观测库 spans 列不兼容：{columns}")
            cursor = await connection.execute(
                "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            names = [item[0] for item in await cursor.fetchall()]
            await cursor.close()
            missing = telemetry._INDEXES - set(names)
            if missing:
                raise RuntimeError(f"观测库结构缺失：{sorted(missing)}")
            cursor = await connection.execute("SELECT count(*) FROM spans")
            row = await cursor.fetchone()
            await cursor.close()
            return {"user_version": version, "spans": int(row[0]),
                    "schema_columns": columns, "schema_indexes": sorted(names),
                    "opened_path": str(copy_dir / path.name)}
        finally:
            await connection.close()

    try:
        result.update(asyncio.run(probe()))
        result["read_only"] = True
    except BaseException as error:
        result["read_only"] = False
        result["error"] = f"{type(error).__name__}: {error}"
    # 采集结束后的终态复核：比较源数据文件签名（库本体与全部既有 sidecar，含 -shm）。
    final = _source_signature(_trace_source_files(path))
    if not _signature_stable(before, final):
        return _consistency_error("采集结束后源文件发生变化", before=before, after=final)
    return result


def _milvus_identity() -> dict:
    # 只读探针：正式 alias 指向的物理集合与清单身份，禁写禁切换。
    from app.application.business.retrieval_index import (
        PUBLISHED_ALIAS,
        MilvusConnection,
        published_collection,
        read_manifest,
    )

    async def probe():
        async with MilvusConnection.from_env() as db:
            aliases = (await db.call("list_aliases"))["aliases"]
            collections = list(await db.call("list_collections"))
            target = await published_collection(db)
            manifest = await read_manifest(db, target)
            return {
                "alias": PUBLISHED_ALIAS,
                "collection": target,
                "manifest": manifest,
                "aliases": sorted(aliases),
                "collections": sorted(collections),
            }

    return asyncio.run(probe())


# 本项目明确保护的卷范围：只比较归属本项目的 named volume，不枚举宿主全部卷。
PROTECTED_VOLUME_PREFIXES = ("fit-agent-milvus",)

# 受保护基线的比较键：逐项全量相等，禁止以空清单或缺失字段表示通过。
BASELINE_KEYS = (
    "business_data",
    "production_trace",
    "attachment_store",
    "embedding_config",
    "milvus",
    "volumes",
)

# 稳定受保护身份字段与采集工件元数据字段：比较只看前者，后者留作审计证据。
PROTECTED_IDENTITY_FIELDS = ("protected_identity", "protected_artifacts")


def _split_protected(key: str, value):
    # production_trace 与其它受保护键的区别：Trace 采集会额外产生本次运行的副本工件。
    # 返回 (稳定受保护身份, 采集工件元数据)：比较只使用前者，后者随证据留盘。
    if key != "production_trace" or not isinstance(value, dict):
        return value, None
    return _trace_protected_identity(value), _trace_artifact_metadata(value)


def _volume_identity() -> dict:
    # 记录受保护卷的名称、可取得的身份字段、实际保护范围与采集错误。
    listed = subprocess.run(
        ["docker", "volume", "ls", "--format", "{{.Name}}"], capture_output=True, text=True
    )
    if listed.returncode != 0:
        return {
            "scope": list(PROTECTED_VOLUME_PREFIXES),
            "volumes": {},
            "error": listed.stderr.strip(),
        }
    names = [
        name
        for name in listed.stdout.split()
        if any(name.startswith(prefix) for prefix in PROTECTED_VOLUME_PREFIXES)
    ]
    volumes: dict[str, dict] = {}
    errors: list[str] = []
    for name in sorted(names):
        inspected = subprocess.run(
            ["docker", "volume", "inspect", name], capture_output=True, text=True
        )
        if inspected.returncode != 0:
            errors.append(f"{name}: {inspected.stderr.strip()}")
            volumes[name] = {"error": inspected.stderr.strip()}
            continue
        try:
            payload = json.loads(inspected.stdout)[0]
        except (ValueError, IndexError, TypeError) as error:
            errors.append(f"{name}: inspect 解析失败 {error}")
            volumes[name] = {"error": f"inspect 解析失败：{error}"}
            continue
        volumes[name] = {
            "driver": payload.get("Driver"),
            "created_at": payload.get("CreatedAt"),
            "labels": payload.get("Labels") or {},
        }
    result: dict = {"scope": list(PROTECTED_VOLUME_PREFIXES), "volumes": volumes}
    if errors:
        result["errors"] = errors
    return result


def environment_preflight(evidence_dir, expected=None):
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    observed = {}
    for key, probe in (("milvus", _milvus_identity), ("volumes", _volume_identity)):
        try:
            observed[key] = probe()
        except Exception as error:
            observed[key] = {"error": f"{exception_type(error)}: {error}"}
    blockers = []
    for key, value in observed.items():
        reason = _baseline_unverified(key, value)
        if reason:
            blockers.append(f"{key}:unverified({reason})")
        if expected is not None and expected.get(key) != value:
            blockers.append(f"{key}:冻结环境身份不匹配")
        if key == "volumes" and value.get("errors"):
            blockers.append("volumes:inventory读取失败")
    _write_json(evidence_dir / f"environment-{uuid4().hex}.json",
                {"at": datetime.now(timezone.utc).isoformat(), "observed": observed, "blockers": blockers})
    if blockers:
        raise RuntimeError("只读环境预检失败：" + "; ".join(blockers))
    return observed


def protected_baseline(root, *, snapshot_root=None) -> dict:
    """受保护数据的只读基线：正式库、生产 Trace、附件目录、Embedding 配置、Milvus 身份与受保护卷。"""
    backend = Path(root) / "backend"
    data = backend / "data"
    attachments = backend / "tmp" / "sessions"
    try:
        from app.application.business.retrieval_config import load_embedding_config

        configured = load_embedding_config()
        embedding = {"model": configured.model, "dimensions": configured.dimensions}
    except Exception as error:
        embedding = {"error": f"{type(error).__name__}: {error}"}
    try:
        milvus = _milvus_identity()
    except Exception as error:
        milvus = {"error": f"{type(error).__name__}: {error}"}
    try:
        volumes = _volume_identity()
    except OSError as error:
        volumes = {"error": f"{exception_type(error)}: {error}"}
    return {
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "business_data": _directory_digest(data),
        "production_trace": _trace_store_stat(data / "trace.db", snapshot_root=snapshot_root),
        "attachment_store": {
            "exists": attachments.exists(),
            "entries": sorted(path.name for path in attachments.glob("*")) if attachments.exists() else [],
        },
        "embedding_config": embedding,
        "milvus": milvus,
        "volumes": volumes,
    }


def _baseline_split(baseline: dict) -> dict:
    # 受保护基线的稳定身份 / 采集工件分离视图：写入 protection.json 供逐项核对。
    identity = baseline_identity(baseline)
    return {
        "protected_identity": identity,
        "protected_identity_sha256": _digest(identity),
        "protected_artifacts": baseline_artifacts(baseline),
    }


def _baseline_unverified(key: str, value) -> str | None:
    # 空清单、缺失字段与采集错误都不能表示检查通过，据此判为未核实。
    if value is None:
        return "字段缺失"
    if not isinstance(value, dict):
        return None
    if value.get("error"):
        return "采集错误"
    if key == "volumes" and not value.get("volumes"):
        return "受保护卷清单为空"
    if key == "milvus" and not (
        value.get("alias") and value.get("collection") and value.get("manifest")
    ):
        return "Milvus 身份不完整"
    if key == "production_trace" and value.get("exists") and value.get("read_only") is not True:
        return "Trace 一致副本采集未确认只读"
    if key == "production_trace" and value.get("exists") and value.get("acquisition") is not None:
        if not value["acquisition"].get("acquired"):
            return "Trace 采集未取得可验证一致快照"
    return None


def compare_baseline(before: dict, after: dict) -> list[str]:
    # 稳定受保护身份与采集工件元数据分开：副本路径等每次采集必然变化的工件不参与比较。
    # 工件元数据仍随基线落盘留证；只有稳定身份差异才登记为漂移。
    drift: list[str] = []
    for key in BASELINE_KEYS:
        if key not in before or key not in after:
            drift.append(f"{key}:unverified(字段缺失)")
            continue
        before_identity, _before_artifacts = _split_protected(key, before[key])
        after_identity, _after_artifacts = _split_protected(key, after[key])
        if before_identity != after_identity:
            drift.append(key)
            continue
        reason = _baseline_unverified(key, after_identity)
        if reason:
            drift.append(f"{key}:unverified({reason})")
    return drift


def baseline_identity(value: dict) -> dict:
    # 供报告与证据使用的稳定身份视图：与 compare_baseline 的比较输入逐项一致。
    return {key: _split_protected(key, value[key])[0] for key in BASELINE_KEYS if key in value}


def baseline_artifacts(value: dict) -> dict:
    # 采集工件元数据视图：副本路径等留证字段，不参与稳定性判定。
    artifacts = {}
    for key in BASELINE_KEYS:
        if key not in value:
            continue
        _identity, artifact = _split_protected(key, value[key])
        if artifact:
            artifacts[key] = artifact
    return artifacts


def build_manifest(root, experiment_id, cases_path, model_identity, business_date_value, concurrency, smoke=False) -> dict:
    from app.application.business import retrieval_frozen

    return {
        "experiment_id": experiment_id,
        "smoke": smoke,
        "retrieval_frozen": retrieval_frozen.frozen_values(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "commit": git_output(root, "rev-parse", "HEAD"),
        "status_porcelain": git_output(root, "status", "--porcelain"),
        "cases_path": str(cases_path),
        "cases_sha256": sha256_file(cases_path),
        "catalog_path": str(DEFAULT_ACTIONS_PATH),
        "catalog_sha256": sha256_file(DEFAULT_ACTIONS_PATH),
        "model_identity": model_identity,
        "business_date": business_date_value,
        "timezone": BUSINESS_TIMEZONE,
        "rounds": 1 if smoke else ROUNDS,
        "concurrency": concurrency,
        "platform": platform.platform(),
        "python": sys.version.split()[0],
    }


def slot_plan(cases: list[V2Case], smoke: bool = False) -> list[dict]:
    # 冒烟每例一轮、正式每例 ROUNDS 轮；槽位身份写入 manifest 后由调度、账本与放行共用。
    rounds = 1 if smoke else ROUNDS
    return [
        {
            "slot": case_index * rounds + round_index,
            "case_id": case.case_id,
            "category": case.category,
            "round_index": round_index,
        }
        for case_index, case in enumerate(cases)
        for round_index in range(rounds)
    ]
