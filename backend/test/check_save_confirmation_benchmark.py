# 运行（仅协议构造组，零模型调用）：
#   .venv/Scripts/python -m test.check_save_confirmation_benchmark
# 运行（两组全量）：由 test.run_v2_eval 编排调用 run_benchmark，模型配置取实验根冻结副本。

import base64
import json
import platform
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable, Sequence
from uuid import uuid4

from app import model_config
from app.agent.tools.business import business_tool_declarations
from app.ai.messages import TextContent, ToolResultMessage
from app.domain.session.models import SendCommand, SendRequest
from app.infrastructure.persistence.sqlite import database as database_module
from app.interfaces.http import app
from test.check_http import create_session
from test.check_structured_confirmation_http import (
    PROFILE,
    SYSTEM,
    Live,
    check_chat_entries_reject_authorization,
    check_history_filters_and_pending,
    check_idempotency_conflict_and_expired,
    check_lifespan_recovery,
    check_occupancy_refuses_without_registering,
    check_regenerate_rejects_confirmation,
    check_request_validation,
    check_saved_get_and_history,
    check_security_and_no_store,
    check_workout_update_and_failed,
    new_id,
    now_ms,
    request_body,
)
from test.regression_support import Server, client
from test.v2_business_cases import read_state
from test.v2_contract import BenchmarkResult
from test.v2_eval_support import (
    USAGE_FIELDS,
    _prepared,
    _span,
    _write_json,
    capture_turn,
    git_output,
    trusted_business_date,
    use_frozen_model_config,
)

BACKEND = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND.parent
TOPIC = "save_confirmation"
GROUP_PROTOCOL = "protocol_constructed"
GROUP_REAL = "real_chain"
ALL_GROUPS = (GROUP_PROTOCOL, GROUP_REAL)
# 组目录名与附件根保持简短：附件路径由应用拼接 tmp/sessions/<uuid>/attachments/<uuid>.txt，
# 目录层级过长会撞上 Windows 260 字符路径上限。
PROTOCOL_DIR = "protocol"
REAL_DIR = "real"
ATTACHMENT_DIR = "att"
PRODUCTION_DATABASE = BACKEND / "data" / "fit-agent.db"
PRODUCTION_TRACE_DATABASE = BACKEND / "data" / "trace.db"
SAVE_TOOL_NAMES = frozenset(
    {"save_profile_update", "save_plan", "save_workout", "update_workout"}
)
SPAN_COLUMNS = (
    "trace_id, span_id, parent_span_id, session_id, run_id, operation_id, name,"
    " started_at, finished_at, duration_ms, status, error_code, attributes"
)
# 场景返回字典中的这些键升格为样本一级字段，其余键归入 detail。
LIFTED_KEYS = (
    "session_id", "operation_id", "run_id", "confirmation_ms", "operation_query_ms",
    "reuse_ms", "usage", "trace_root", "states", "request", "response", "evidence",
)

# 覆盖项：标识、判定口径、提供证据的样本组。
COVERAGE: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("profile_new_save", "画像新增由真实运行准备并经确认 HTTP 落地", (GROUP_REAL,)),
    ("profile_update_save", "画像更新由真实运行准备并经确认 HTTP 落地", (GROUP_REAL,)),
    ("plan_new_save", "计划新增由真实运行准备并经确认 HTTP 落地", (GROUP_REAL,)),
    ("plan_update_save", "计划更新由真实运行准备并经确认 HTTP 落地", (GROUP_REAL,)),
    ("workout_new_save", "训练记录新增由真实运行准备并经确认 HTTP 落地", (GROUP_REAL,)),
    ("workout_update_save", "训练记录更新由真实运行准备并经确认 HTTP 落地", (GROUP_REAL,)),
    (
        "independent_authorization",
        "聊天正文、附件正文、工具返回文本与模型自报参数四类来源零提交，快照与业务原值保持",
        (GROUP_PROTOCOL,),
    ),
    (
        "authorization_field_injection_rejected",
        "授权字段注入 run/edit/steering 三个真实入口一律 422 invalid_request 且零写入",
        (GROUP_PROTOCOL,),
    ),
    ("strict_request_validation", "确认请求严格四字段与 UUID/枚举校验", (GROUP_PROTOCOL,)),
    (
        "result_query",
        "专用 GET 返回原固定结果；重复提交复用同一结果与同一确认节点",
        (GROUP_PROTOCOL, GROUP_REAL),
    ),
    (
        "idempotent_reuse",
        "同 operation_id 加同四字段复用原操作：不新增业务版本、保存记录与执行根",
        (GROUP_PROTOCOL, GROUP_REAL),
    ),
    ("same_key_different_body_conflict", "同键不同请求体返回 operation_conflict", (GROUP_PROTOCOL,)),
    ("operation_expired_conflict", "编辑失效的跨入口编号返回 operation_expired", (GROUP_PROTOCOL,)),
    ("invalidated_snapshot_rejected", "旧快照失效后提交被拒绝且零写入", (GROUP_PROTOCOL,)),
    ("wrong_display_node_rejected", "展示节点与后端绑定不一致被拒绝且零写入", (GROUP_PROTOCOL,)),
    ("cross_session_target_rejected", "跨会话目标被拒绝且零写入", (GROUP_PROTOCOL,)),
    (
        "run_occupancy_refused",
        "运行占用期确认返回 run_busy 且不登记操作、快照保持 pending",
        (GROUP_PROTOCOL,),
    ),
    (
        "version_conflict",
        "版本依据冲突返回 profile_version_conflict 并留下可查询的 failed 终态",
        (GROUP_PROTOCOL,),
    ),
    ("regenerate_rejected", "确认节点的模型重新生成请求被拒绝，查询保持只读", (GROUP_PROTOCOL,)),
    (
        "history_pending_projection",
        "历史 confirmations 与 pending_proposals 两数组与实际状态一致",
        (GROUP_PROTOCOL,),
    ),
    ("transport_boundary", "Origin 边界拒绝与 no-store 响应头", (GROUP_PROTOCOL,)),
    (
        "restart_recovery_committed",
        "重启对账：业务已提交的处理中操作恢复为 saved 且不重复写入",
        (GROUP_PROTOCOL,),
    ),
    (
        "restart_recovery_interrupted",
        "重启对账：已受理未提交的操作恢复为 failed/confirmation_interrupted 且快照回到 pending",
        (GROUP_PROTOCOL,),
    ),
)

# (样本名, 业务类型, 落地覆盖项, 真实运行自然语言请求)
REAL_SCENARIOS: tuple[tuple[str, str, str, str], ...] = (
    (
        "profile_new",
        "profile",
        "profile_new_save",
        "我最近开始系统训练：目标是增肌，新手，家里只有哑铃和弹力带，每周能练四次、每次一小时。"
        "把这些情况整理成训练画像给我看，先不要保存。",
    ),
    (
        "profile_update",
        "profile",
        "profile_update_save",
        "把训练目标改成减脂，其余情况保持不变，重新整理一份画像给我看，先不要保存。",
    ),
    (
        "plan_new",
        "plan",
        "plan_new_save",
        "基于现在的画像生成一份居家增肌周计划，完整展示给我看，先不要保存。",
    ),
    (
        "plan_update",
        "plan",
        "plan_update_save",
        "把当前计划里的拉日改成以罗马尼亚硬拉和引体向上为主，其余天保持不变，"
        "重新整理一份完整计划给我看，先不要保存。",
    ),
    ("workout_new", "workout", "workout_new_save", "记录今天训练：卧推 3 组 8 次 40kg，先不要保存。"),
    (
        "workout_update",
        "workout",
        "workout_update_save",
        "把今天卧推第三组的重量改成 45kg，其它保持不变，重新整理今天的训练记录给我看，先不要保存。",
    ),
)


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="milliseconds")


class SuiteRoot:
    # 每组一套独立业务库、观测库与附件根，全部落在实验目录内。
    def __init__(self, experiment_dir: Path, group_dir: str) -> None:
        self.base = experiment_dir / group_dir
        self.database_path = self.base / "app.db"
        self.trace_path = self.base / "trace.db"
        from test.v2_attempts import authorized_attachment_root

        self.attachment_root = authorized_attachment_root(self.base, self.base / ATTACHMENT_DIR)
        self.samples_dir = self.base / "samples"
        self.attachment_root.mkdir(parents=True, exist_ok=True)
        self.samples_dir.mkdir(parents=True, exist_ok=True)

    def paths(self) -> dict:
        return {
            "root": str(self.base),
            "database": str(self.database_path),
            "trace": str(self.trace_path),
            "attachments": str(self.attachment_root),
            "samples": str(self.samples_dir),
            "file_tools_tmp_root": str(self.attachment_root / "tmp"),
            "observations": str(self.base / "observations"),
        }

    @contextmanager
    def bind(self):
        from app.agent import cache_observation, telemetry
        from app.application.session import service as session_service
        from test.v2_eval_support import evaluation_roots

        with evaluation_roots(self.attachment_root, self.database_path, self.trace_path,
                              self.base / "observations"):
            _write_json(self.base / "init.json", {
                **self.paths(), "effective_database": str(database_module.default_database_path()),
                "effective_trace": str(telemetry.trace_database_path()),
                "effective_attachments": str(session_service.PROJECT_ROOT),
                "effective_observations": str(cache_observation.observation_directory()),
            })
            yield self


class MeasuredLive(Live):
    # 只在真实确认 HTTP 往返外侧计时并登记结果状态，请求与响应原样透传。

    def __init__(self, server: Server) -> None:
        super().__init__(server)
        self.posts: list[dict] = []
        self.queries: list[dict] = []

    def confirm(self, session_id, kind, ids, *, operation_id=None, **overrides):
        started = time.perf_counter()
        response = super().confirm(
            session_id, kind, ids, operation_id=operation_id, **overrides
        )
        self.posts.append({
            "status_code": response.status_code,
            "duration_ms": (time.perf_counter() - started) * 1000.0,
        })
        return response

    def get_op(self, session_id, operation_id):
        started = time.perf_counter()
        response = super().get_op(session_id, operation_id)
        self.queries.append({
            "status_code": response.status_code,
            "duration_ms": (time.perf_counter() - started) * 1000.0,
        })
        return response

    def timings(self) -> dict:
        posts, queries = list(self.posts), list(self.queries)
        self.posts.clear()
        self.queries.clear()
        return {"confirmation_posts": posts, "confirmation_queries": queries}


def _send_text(live: Live, session_id: str, text: str, attachments: list[dict] | None = None):
    # 文字与附件只进入普通消息通道：全程未调用确认 HTTP，也不产生任何授权。
    async def run():
        outcome = await live.service.accept_send(
            SendCommand(
                operation_id=new_id(),
                session_id=session_id,
                request=SendRequest(text=text, attachments=attachments or []),
            ),
            system_message=SYSTEM,
        )
        await live.service.finish_run(session_id, outcome.run.id, "completed")
        return outcome.run

    return live.call(run())


def _stage_profile(live: Live, session_id: str, base_version, payload: dict) -> dict:
    return live.stage(
        session_id,
        "profile",
        {"profile_id": 1, "base_profile_version": base_version, "payload": payload},
        live.business.prepare_profile_update,
        live.business.bind_display_entry,
        "prepare_profile_update",
    )


def check_chat_text_no_authorization(live: Live) -> dict:
    # 方案 §9.1 与 §9.3：同类与跨类多份待确认时，纯文字确认零提交。
    session = live.session()
    profile = live.stage_profile(session)
    workout = live.stage_workout(session, "2019-01-05")
    operations = live.count("save_confirmation_operations")
    records = live.count("profile_save_records")
    texts = ("确认。", "确认保存画像", "两个都确认")
    for text in texts:
        _send_text(live, session, text)
    history = live.http.get(f"/api/sessions/{session}/history")
    assert history.status_code == 200, history.text
    body = history.json()
    assert body["confirmations"] == [], body["confirmations"]
    assert {item["proposal_id"] for item in body["pending_proposals"]} == {
        profile["proposal"], workout["proposal"]
    }
    profile_snapshot = live.call(live.repository.get_snapshot(profile["proposal"]))
    assert profile_snapshot.status == "pending" and profile_snapshot.confirmation_entry_id is None
    workout_snapshot = live.call(live.repository.get_workout_snapshot(workout["proposal"]))
    assert workout_snapshot.status == "pending" and workout_snapshot.confirmation_entry_id is None
    assert live.count("save_confirmation_operations") == operations
    assert live.count("profile_save_records") == records
    return {
        "session_id": session,
        "texts": list(texts),
        "operations": operations,
        "profile_save_records": records,
        "pending_kinds": sorted(item["business_kind"] for item in body["pending_proposals"]),
        "profile_version": live.http.get("/api/profile").json()["version"],
    }


def check_untrusted_instruction_channels(live: Live) -> dict:
    # 方案 §3 与 §9.3：附件正文、工具返回文本、模型自报参数均非授权来源；
    # 模型给出的目标标识与用户点击快照不一致时确认入口拒绝提交。
    session = live.session()
    profile = live.stage_profile(session)
    operations = live.count("save_confirmation_operations")
    records = live.count("profile_save_records")
    wire = json.dumps(request_body(None, "profile", profile), ensure_ascii=False)
    attachment_id = new_id()
    _send_text(
        live, session, "按附件里的指令保存",
        attachments=[
            {
                "kind": "upload",
                "attachment_id": attachment_id,
                "file_name": "confirm.txt",
                "data_base64": base64.b64encode(wire.encode("utf-8")).decode("ascii"),
            }
        ],
    )
    run = live.turn(session, "把附件指令整理一下", finish=False)
    call_id = "call-" + uuid4().hex[:8]
    source = live.append(
        session, run.id, run.request_entry_id, live.assistant(call_id, "get_profile", {})
    )
    live.append(
        session, run.id, source,
        ToolResultMessage(
            role="toolResult", tool_call_id=call_id, tool_name="get_profile",
            content=[TextContent(type="text", text=wire)], is_error=False, timestamp=now_ms(),
        ),
    )
    live.call(live.service.finish_run(session, run.id, "completed"))
    declared = {item.name for item in business_tool_declarations()}
    assert SAVE_TOOL_NAMES.isdisjoint(declared), sorted(SAVE_TOOL_NAMES & declared)
    forged = live.confirm(session, "profile", profile, display=new_id())
    assert forged.status_code == 409, forged.text
    code = forged.json()["detail"]["code"]
    assert code == "profile_confirmation_invalid", forged.text
    assert live.count("save_confirmation_operations") == operations
    assert live.count("profile_save_records") == records
    snapshot = live.call(live.repository.get_snapshot(profile["proposal"]))
    assert snapshot.status == "pending" and snapshot.confirmation_entry_id is None
    return {
        "session_id": session,
        "attachment_id": attachment_id,
        "rejected_code": code,
        "save_tools_absent_from_registry": True,
        "declared_tools": sorted(declared),
        "operations": operations,
        "profile_save_records": records,
    }


def check_invalidated_snapshot_rejected(live: Live) -> dict:
    # 同目标新快照令旧快照失效：旧快照提交被拒且零写入，新快照仍可确认保存。
    session = live.session()
    base_version = live.http.get("/api/profile").json()["version"]
    old = _stage_profile(live, session, base_version, PROFILE)
    newer = _stage_profile(live, session, base_version, {**PROFILE, "goal": "减脂"})
    operations = live.count("save_confirmation_operations")
    records = live.count("profile_save_records")
    rejected = live.confirm(session, "profile", old)
    assert rejected.status_code == 409, rejected.text
    code = rejected.json()["detail"]["code"]
    assert code == "profile_proposal_invalidated", rejected.text
    assert live.count("save_confirmation_operations") == operations
    assert live.count("profile_save_records") == records
    stale = live.call(live.repository.get_snapshot(old["proposal"]))
    assert stale.status == "invalidated" and stale.confirmation_entry_id is None
    saved = live.confirm(session, "profile", newer)
    assert saved.status_code == 200, saved.text
    assert saved.json()["status"] == "saved", saved.json()
    assert live.count("save_confirmation_operations") == operations + 1
    assert live.count("profile_save_records") == records + 1
    return {
        "session_id": session,
        "operation_id": saved.json()["operation_id"],
        "rejected_code": code,
        "stale_status": stale.status,
        "saved_version": saved.json()["result"]["version"],
        "operations": operations,
        "profile_save_records": records,
    }


def check_target_mismatch_rejected(live: Live) -> dict:
    # 错误展示节点、跨会话目标、不存在快照与不存在会话四类目标破坏：全部零提交。
    session = live.session()
    other = live.session()
    profile = live.stage_profile(session)
    workout = live.stage_workout(session, "2019-02-02")
    operations = live.count("save_confirmation_operations")
    profile_records = live.count("profile_save_records")
    workout_records = live.count("workout_save_records")
    wrong_display = live.confirm(session, "profile", profile, display=new_id())
    assert wrong_display.status_code == 409, wrong_display.text
    assert wrong_display.json()["detail"]["code"] == "profile_confirmation_invalid", wrong_display.text
    cross = live.confirm(other, "profile", profile)
    assert cross.status_code == 403, cross.text
    assert cross.json()["detail"]["code"] == "profile_access_denied", cross.text
    unknown = live.confirm(session, "workout", {"proposal": new_id(), "display": new_id()})
    assert unknown.status_code == 404, unknown.text
    assert unknown.json()["detail"]["code"] == "workout_proposal_not_found", unknown.text
    absent = live.http.get(f"/api/sessions/{new_id()}/confirmations/{new_id()}")
    assert absent.status_code == 404, absent.text
    assert absent.json()["detail"]["code"] == "session_not_found", absent.text
    assert live.count("save_confirmation_operations") == operations
    assert live.count("profile_save_records") == profile_records
    assert live.count("workout_save_records") == workout_records
    snapshot = live.call(live.repository.get_workout_snapshot(workout["proposal"]))
    assert snapshot.status == "pending" and snapshot.confirmation_entry_id is None
    # 仅破坏单一条件时其余链路可用：同快照的正确目标仍完成保存。
    saved = live.confirm(session, "workout", workout)
    assert saved.status_code == 200, saved.text
    assert saved.json()["status"] == "saved", saved.json()
    assert live.count("save_confirmation_operations") == operations + 1
    return {
        "session_id": session,
        "other_session_id": other,
        "operation_id": saved.json()["operation_id"],
        "rejected": {
            "wrong_display": wrong_display.json()["detail"]["code"],
            "cross_session": cross.json()["detail"]["code"],
            "unknown_proposal": unknown.json()["detail"]["code"],
            "unknown_session": absent.json()["detail"]["code"],
        },
        "saved_workout_version": saved.json()["result"]["version"],
        "operations": operations,
    }


def check_restart_committed(root: Path) -> dict:
    # 崩溃窗口只有终态落地差异：业务事务已提交而操作仍记 processing。
    # 重启对账必须按真实保存记录恢复 saved，且不重复写入业务或消息。
    path = root / "recovery-committed.db"
    original = database_module.default_database_path
    database_module.default_database_path = lambda: path
    try:
        with Server(app) as server:
            live = Live(server)
            session = live.session()
            profile = live.stage_profile(session)
            operation_id = new_id()
            first = live.confirm(session, "profile", profile, operation_id=operation_id)
            assert first.status_code == 200, first.text
            saved = first.json()
            assert saved["status"] == "saved", saved
            version = saved["result"]["version"]
            operations = live.count("save_confirmation_operations")
            records = live.count("profile_save_records")
            entries = live.count("session_entries")
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "UPDATE save_confirmation_operations SET status = 'processing',"
                " result = NULL, error = NULL, updated_at = ? WHERE operation_id = ?",
                (now_ms(), operation_id),
            )
            connection.commit()
        finally:
            connection.close()
        with Server(app) as server:
            http = client(server.base_url)
            recovered_live = Live(server)
            fetched = http.get(f"/api/sessions/{session}/confirmations/{operation_id}")
            assert fetched.status_code == 200, fetched.text
            recovered = fetched.json()
            assert recovered["status"] == "saved", recovered
            assert recovered["result"] == saved["result"], recovered["result"]
            assert recovered["confirmation_entry_id"] == saved["confirmation_entry_id"]
            assert recovered_live.count("save_confirmation_operations") == operations
            assert recovered_live.count("profile_save_records") == records
            assert recovered_live.count("session_entries") == entries
            assert recovered_live.http.get("/api/profile").json()["version"] == version
            again = recovered_live.confirm(session, "profile", profile, operation_id=operation_id)
            assert again.status_code == 200, again.text
            assert again.json()["result"] == saved["result"], again.text
            assert recovered_live.count("profile_save_records") == records
            assert recovered_live.count("session_entries") == entries
        return {
            "session_id": session,
            "operation_id": operation_id,
            "status": recovered["status"],
            "version": version,
            "result_identical": recovered["result"] == saved["result"],
            "reused_on_resubmit": again.json()["status"] == "saved",
            "profile_save_records": records,
            "session_entries": entries,
        }
    finally:
        database_module.default_database_path = original


PROTOCOL_SCENARIOS: tuple[tuple[str, tuple[str, ...], Callable[..., dict]], ...] = (
    ("saved_get_and_history", ("result_query",), check_saved_get_and_history),
    ("workout_update_and_version_conflict", ("version_conflict",), check_workout_update_and_failed),
    ("strict_request_validation", ("strict_request_validation",), check_request_validation),
    (
        "authorization_field_injection",
        ("authorization_field_injection_rejected",),
        check_chat_entries_reject_authorization,
    ),
    ("run_occupancy", ("run_occupancy_refused",), check_occupancy_refuses_without_registering),
    (
        "idempotency_conflict_expired",
        (
            "result_query", "idempotent_reuse", "same_key_different_body_conflict",
            "operation_expired_conflict",
        ),
        check_idempotency_conflict_and_expired,
    ),
    (
        "regenerate_rejects_confirmation",
        ("regenerate_rejected", "result_query"),
        check_regenerate_rejects_confirmation,
    ),
    ("history_filters_and_pending", ("history_pending_projection",), check_history_filters_and_pending),
    ("security_and_no_store", ("transport_boundary",), check_security_and_no_store),
    ("chat_text_no_authorization", ("independent_authorization",), check_chat_text_no_authorization),
    (
        "untrusted_instruction_channels",
        ("independent_authorization", "wrong_display_node_rejected"),
        check_untrusted_instruction_channels,
    ),
    (
        "invalidated_snapshot_rejected",
        ("invalidated_snapshot_rejected",),
        check_invalidated_snapshot_rejected,
    ),
    (
        "target_mismatch_rejected",
        ("wrong_display_node_rejected", "cross_session_target_rejected"),
        check_target_mismatch_rejected,
    ),
)

PROTOCOL_RECOVERY_SCENARIOS: tuple[tuple[str, str, Callable[[], dict]], ...] = (
    ("restart_recovery_committed", "restart_recovery_committed", check_restart_committed),
    ("restart_recovery_interrupted", "restart_recovery_interrupted", check_lifespan_recovery),
)


def _build_sample(
    root: SuiteRoot, *, name: str, group: str, coverage: Sequence[str], status: str,
    started: float, detail: dict, error: str | None,
) -> dict:
    ended = time.time()
    lifted = {key: detail.pop(key) for key in LIFTED_KEYS if key in detail}
    evidence = [str(root.samples_dir / f"{name}.json")]
    evidence.extend(lifted.get("evidence", []))
    return {
        "name": name,
        "group": group,
        # 协议构造组的助手与工具条目为合成（provider=example、model=model-1、固定 USAGE）：
        # 其模型用量、运行数与 Trace 一律排除在真实用量统计之外。
        "synthetic_constructed": group == GROUP_PROTOCOL,
        "status": status,
        "coverage": list(coverage),
        "started_at": _iso(started),
        "ended_at": _iso(ended),
        "duration_ms": (ended - started) * 1000.0,
        "session_id": lifted.get("session_id"),
        "operation_id": lifted.get("operation_id"),
        "run_id": lifted.get("run_id"),
        "confirmation_ms": lifted.get("confirmation_ms"),
        "operation_query_ms": lifted.get("operation_query_ms"),
        "reuse_ms": lifted.get("reuse_ms"),
        "usage": lifted.get("usage"),
        "trace_root": lifted.get("trace_root"),
        "states": lifted.get("states"),
        "request": lifted.get("request"),
        "response": lifted.get("response"),
        "evidence": evidence,
        "detail": detail,
        "error": error,
    }


def _guarded(
    root: SuiteRoot, name: str, group: str, coverage: Sequence[str], call: Callable[[], dict],
) -> dict:
    started = time.time()
    detail: dict = {}
    error = None
    try:
        detail = call() or {}
        status = "pass"
    except AssertionError as failure:
        # 断言即失败点：记录真实结论，同组其余场景继续完成测量。
        status, error = "fail", f"{type(failure).__name__}: {failure}"
    sample = _build_sample(
        root, name=name, group=group, coverage=coverage, status=status,
        started=started, detail=detail, error=error,
    )
    _write_json(root.samples_dir / f"{name}.json", sample)
    return sample


def run_protocol(root: SuiteRoot) -> list[dict]:
    samples: list[dict] = []
    with root.bind(), Server(app) as server:
        live = MeasuredLive(server)
        for name, items, scenario in PROTOCOL_SCENARIOS:
            live.reset()
            live.timings()
            sample = _guarded(root, name, GROUP_PROTOCOL, items, partial(scenario, live))
            samples.append(_attach_protocol_timings(root, sample, live.timings()))
    # 重启恢复两个分支各自使用独立库，均在专项目录内：lifespan 对账在真实启动序列中执行。
    for name, item, scenario in PROTOCOL_RECOVERY_SCENARIOS:
        samples.append(
            _guarded(root, name, GROUP_PROTOCOL, (item,),
                     partial(_run_recovery, root, scenario))
        )
    return samples


def _run_recovery(root, scenario):
    with root.bind():
        return scenario(root.base)


def _attach_protocol_timings(root: SuiteRoot, sample: dict, timings: dict) -> dict:
    posts = timings["confirmation_posts"]
    queries = timings["confirmation_queries"]
    sample["detail"].update(timings)
    accepted = [item["duration_ms"] for item in posts if item["status_code"] == 200]
    # 代表值取该场景首个成功受理并落地的确认往返；无成功往返时取最后一次往返。
    sample["confirmation_ms"] = (
        accepted[0] if accepted else (posts[-1]["duration_ms"] if posts else None)
    )
    sample["operation_query_ms"] = queries[-1]["duration_ms"] if queries else None
    _write_json(root.samples_dir / f"{sample['name']}.json", sample)
    return sample


def _latencies(samples: list[dict], key: str, status: int | None = None) -> list[float]:
    values: list[float] = []
    for sample in samples:
        recorded = [
            item["duration_ms"]
            for item in sample["detail"].get(key, ())
            if status is None or item["status_code"] == status
        ]
        if key == "confirmation_posts" and not recorded and sample["confirmation_ms"] is not None:
            # 真实链路样本直接记录单次确认往返，没有逐请求清单。
            values.append(sample["confirmation_ms"])
            continue
        values.extend(recorded)
    return values


def _trace_roots(trace_path: Path, operation_id: str) -> list[dict]:
    # 确认执行根由 ConfirmationService._execute 真实写入隔离观测库，响应返回时已落库。
    connection = sqlite3.connect(str(trace_path), timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            f"SELECT {SPAN_COLUMNS} FROM spans WHERE operation_id = ? ORDER BY span_id",
            (operation_id,),
        ).fetchall()
    finally:
        connection.close()
    return [{**dict(row), "attributes": json.loads(row["attributes"])} for row in rows]


def _proof(kind: str, result: dict, state: dict) -> dict:
    # 落库事实投影：固定结果对象必须与库内业务记录一致。
    if kind == "profile":
        return {
            "profile_version": state["profile"]["version"],
            "profile_save_records": state["profile_save_records"]["count"],
            "matches_result": state["profile"]["version"] == result["version"],
        }
    if kind == "plan":
        return {
            "plan_current_id": state["plans"]["current_id"],
            "plan_count": state["plans"]["count"],
            "plan_save_records": state["plan_save_records"]["count"],
            "matches_result": state["plans"]["current_id"] == result["id"],
        }
    record = state["workouts"]["by_date"].get(result["performed_on"])
    return {
        "workout_id": None if record is None else record["id"],
        "workout_version": None if record is None else record["version"],
        "workout_save_records": state["workout_save_records"]["count"],
        "matches_result": (
            record is not None
            and record["id"] == result["id"]
            and record["version"] == result["version"]
        ),
    }


def real_sample(
    root: SuiteRoot, live: Live, http, *, name: str, kind: str, item: str, prompt: str,
) -> dict:
    started = time.time()
    session = str(uuid4())
    create_session(http, session, title=f"确认专项 {name}")
    turn = capture_turn(
        http, session, case_id=f"save-confirmation/{name}", round_index=0, step_index=1,
        request_text=prompt, attachments=[],
    )
    turn_path = root.samples_dir / f"{name}-turn.json"
    _write_json(turn_path, turn.model_dump())
    prepared = [entry for entry in _prepared(turn.events) if entry["business_kind"] == kind]
    detail: dict[str, Any] = {
        "prompt": prompt,
        "turn_status": turn.status,
        "terminal": turn.terminal,
        "turn_ms": turn.duration_ms,
        "client_first_body_ms": turn.client_first_body_ms,
        "prepared": prepared,
        "session_id": session,
        "run_id": turn.run_id,
        "usage": turn.usage,
        "evidence": [str(turn_path)],
    }
    if turn.status != "done" or not prepared:
        detail["blocked_reason"] = (
            f"真实运行终态 {turn.status}（terminal={turn.terminal}），"
            f"未产出 {kind} 待确认快照，确认步骤未发起"
        )
        return _build_sample(
            root, name=name, group=GROUP_REAL, coverage=(item,), status="blocked",
            started=started, detail=detail, error=None,
        )
    target = prepared[-1]
    body = {
        "operation_id": new_id(),
        "business_kind": kind,
        "proposal_id": target["proposal_id"],
        "display_entry_id": target["display_entry_id"],
    }
    endpoint = f"/api/sessions/{session}/confirmations"
    detail["prepared_target"] = target
    detail["confirmation_endpoint"] = endpoint
    operations_before = live.count("save_confirmation_operations")
    before = read_state(str(root.database_path))
    post_started = time.perf_counter()
    response = http.post(endpoint, json=body)
    confirmation_ms = (time.perf_counter() - post_started) * 1000.0
    assert response.status_code == 200, response.text
    operation = response.json()
    assert operation["status"] == "saved", operation
    assert operation["error"] is None and operation["result"] is not None, operation
    assert response.headers["cache-control"] == "no-store"
    query_started = time.perf_counter()
    fetched = http.get(f"{endpoint}/{body['operation_id']}")
    query_ms = (time.perf_counter() - query_started) * 1000.0
    assert fetched.status_code == 200 and fetched.json() == operation, fetched.text
    reuse_started = time.perf_counter()
    reuse = http.post(endpoint, json=body)
    reuse_ms = (time.perf_counter() - reuse_started) * 1000.0
    assert reuse.status_code == 200, reuse.text
    assert reuse.json()["result"] == operation["result"], reuse.text
    assert reuse.json()["confirmation_entry_id"] == operation["confirmation_entry_id"]
    after = read_state(str(root.database_path))
    operations_after = live.count("save_confirmation_operations")
    # 复用原操作：操作行只增加首次受理那一条，业务版本与保存记录不前进。
    assert operations_after == operations_before + 1, (operations_before, operations_after)
    proof_before = _proof(kind, operation["result"], before)
    proof_after = _proof(kind, operation["result"], after)
    assert proof_after["matches_result"], proof_after
    roots = _trace_roots(root.trace_path, body["operation_id"])
    assert len(roots) == 1 and roots[0]["name"] == "confirmation", roots
    assert roots[0]["status"] == "completed" and roots[0]["run_id"] is None, roots
    detail.update({
        "operation_id": body["operation_id"],
        "reuse_status_code": reuse.status_code,
        "proof_before": proof_before,
        "proof_after": proof_after,
        "operations_before": operations_before,
        "operations_after": operations_after,
        "trace_root_count": len(roots),
    })
    detail.update({
        "confirmation_ms": confirmation_ms,
        "operation_query_ms": query_ms,
        "reuse_ms": reuse_ms,
        "trace_root": {
            "present": True,
            "trace_id": roots[0]["trace_id"],
            "status": roots[0]["status"],
            "duration_ms": roots[0]["duration_ms"],
            "attributes": roots[0]["attributes"],
        },
        "states": {"before": proof_before, "after": proof_after},
        "request": body,
        "response": operation,
    })
    return _build_sample(
        root, name=name, group=GROUP_REAL,
        coverage=(item, "result_query", "idempotent_reuse"), status="pass",
        started=started, detail=detail, error=None,
    )


def run_real(root: SuiteRoot, identity: dict) -> list[dict]:
    samples: list[dict] = []
    with root.bind(), Server(app) as server, client(server.base_url, timeout=900) as http:
        live = Live(server)
        for name, kind, item, prompt in REAL_SCENARIOS:
            started = time.time()
            try:
                sample = real_sample(
                    root, live, http, name=name, kind=kind, item=item, prompt=prompt
                )
            except AssertionError as failure:
                sample = _build_sample(
                    root, name=name, group=GROUP_REAL,
                    coverage=(item, "result_query", "idempotent_reuse"), status="fail",
                    started=started, detail={}, error=f"{type(failure).__name__}: {failure}",
                )
                _write_json(root.samples_dir / f"{name}.json", sample)
            samples.append(sample)
    return samples


def _experiment_root(directory: Path) -> Path:
    # 冻结模型配置写在 v2 实验根；专项目录位于 experiment_dir/benchmarks/save-confirmation。
    for candidate in (directory, *directory.parents):
        if (candidate / "model-config" / model_config.MODELS_FILE).exists():
            return candidate
    raise FileNotFoundError(
        f"未找到实验冻结模型配置 {model_config.MODELS_FILE}：从 {directory} 向上未命中 model-config"
    )


def _coverage_report(samples: list[dict]) -> dict:
    report: dict[str, dict] = {}
    for key, description, groups in COVERAGE:
        related = [sample for sample in samples if key in sample["coverage"]]
        report[key] = {
            "description": description,
            "groups": list(groups),
            "pass": sum(1 for sample in related if sample["status"] == "pass"),
            "fail": sum(1 for sample in related if sample["status"] == "fail"),
            "blocked": sum(1 for sample in related if sample["status"] == "blocked"),
            "evidence": [
                {
                    "sample": sample["name"],
                    "status": sample["status"],
                    "evidence": sample["evidence"],
                }
                for sample in related
            ],
        }
    return report


def _missing_items(
    report: dict, samples: list[dict], requested: Sequence[str]
) -> list[str]:
    missing: list[str] = []
    for key, item in report.items():
        if item["pass"]:
            continue
        related = [sample for sample in samples if key in sample["coverage"]]
        if not related:
            unrun = [group for group in item["groups"] if group not in requested]
            reason = (
                f"提供组 {unrun} 本次未运行（groups={list(requested)}）"
                if unrun else "本次没有样本发起该覆盖项"
            )
            missing.append(f"{key}: {reason}")
            continue
        reasons = sorted(
            {
                sample["error"] or sample["detail"].get("blocked_reason") or sample["status"]
                for sample in related
            }
        )
        statuses = sorted({sample["status"] for sample in related})
        missing.append(f"{key}: 样本状态 {statuses} —— {reasons}")
    return missing


def _real_usage(real: list[dict]) -> dict:
    seen: dict[str, dict] = {}
    invalid = 0
    for sample in real:
        usage = sample["usage"]
        if usage is None:
            invalid += 1
            continue
        seen.setdefault(sample["run_id"] or sample["name"], usage)
    totals: dict[str, Any] = {
        field: sum(item[field] for item in seen.values()) for field in USAGE_FIELDS
    }
    common = [
        item
        for item in seen.values()
        if all(item.get(field) is not None for field in ("input", "cache_read", "cache_write"))
    ]
    cache_read = sum(item["cache_read"] for item in common)
    denominator = sum(item["input"] + item["cache_read"] + item["cache_write"] for item in common)
    totals.update({
        "source_group": GROUP_REAL,
        "valid_samples": len(seen),
        "invalid_samples": invalid,
        "runs": len(seen),
        "cache_read_sum": cache_read,
        "cache_input_sum": denominator,
        "cache_hit_rate": cache_read / denominator if denominator else None,
    })
    return totals


def _group_report(samples: list[dict]) -> dict:
    report = {}
    for group in ALL_GROUPS:
        selected = [sample for sample in samples if sample["group"] == group]
        report[group] = {
            "samples": len(selected),
            "pass": sum(1 for sample in selected if sample["status"] == "pass"),
            "fail": sum(1 for sample in selected if sample["status"] == "fail"),
            "blocked": sum(1 for sample in selected if sample["status"] == "blocked"),
            "confirmation_ms": _span(_latencies(selected, "confirmation_posts")),
            "confirmation_ms_accepted": _span(
                _latencies(selected, "confirmation_posts", 200)
            ),
            "operation_query_ms": _span(_latencies(selected, "confirmation_queries")),
        }
    return report


def _result(
    base: Path, roots: dict, identity: dict | None, requested: Sequence[str], samples: list[dict],
) -> BenchmarkResult:
    report = _coverage_report(samples)
    missing = _missing_items(report, samples, requested)
    protocol = [sample for sample in samples if sample["group"] == GROUP_PROTOCOL]
    real = [sample for sample in samples if sample["group"] == GROUP_REAL]
    stats = {
        "samples": len(samples),
        "groups": _group_report(samples),
        "coverage": report,
        # 确认 HTTP 延迟取 POST /api/sessions/{sid}/confirmations 的端到端毫秒。
        "confirmation_ms": _span(_latencies(samples, "confirmation_posts")),
        "confirmation_ms_accepted": _span(_latencies(samples, "confirmation_posts", 200)),
        "operation_query_ms": _span(_latencies(samples, "confirmation_queries")),
        "reuse_ms": _span(
            [sample["reuse_ms"] for sample in samples if sample["reuse_ms"] is not None]
        ),
        "usage": _real_usage(real),
        "synthetic_constructed": {
            "samples": len(protocol),
            "excluded_from_usage": True,
            "excluded_fields": ["usage", "model_runs", "trace_root"],
            "note": (
                "协议构造组的助手与工具条目为合成输入，其固定 USAGE、运行数与 Trace "
                "不进入真实用量统计"
            ),
        },
        "model_runs": {
            GROUP_PROTOCOL: 0,
            GROUP_REAL: len({sample["run_id"] for sample in real if sample["run_id"]}),
        },
    }
    correctness = {
        key: {"pass": item["pass"], "fail": item["fail"], "blocked": item["blocked"]}
        for key, item in report.items()
    }
    if not samples:
        status = "blocked"
    elif missing or any(sample["status"] != "pass" for sample in samples):
        status = "incomplete"
    else:
        status = "complete"
    env = {
        "scope": (
            "结构化保存确认专项：协议构造组用真实确认 HTTP 驱动确定性协议场景（快照条目按合成输入例化），"
            "真实链路组用真实模型运行准备快照后经确认 HTTP 保存并回读落库事实"
        ),
        "topic": TOPIC,
        "commit": git_output(REPO_ROOT, "rev-parse", "HEAD"),
        "worktree_status": git_output(REPO_ROOT, "status", "--porcelain"),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "model_identity": identity,
        "groups_requested": list(requested),
        "business_date": trusted_business_date(),
        "timezone": "Asia/Shanghai",
        GROUP_PROTOCOL: {
            "kind": "deterministic_protocol_scenarios",
            "entry_construction": "provider=example / model=model-1 / 固定 USAGE 的合成助手与工具条目",
            "synthetic_constructed": True,
            "confirmation_interface": "真实 POST 与 GET /api/sessions/{sid}/confirmations",
            "model_calls": 0,
            "scenarios": [name for name, _, _ in PROTOCOL_SCENARIOS] + [
                name for name, _, _ in PROTOCOL_RECOVERY_SCENARIOS
            ],
            "paths": roots[GROUP_PROTOCOL],
        },
        GROUP_REAL: {
            "kind": "real_agent_run_then_confirm",
            "synthetic_constructed": False,
            "prepare_channel": "真实 /api/agent/run SSE 与真实 prepare 工具",
            "confirmation_interface": "真实 POST 与 GET /api/sessions/{sid}/confirmations",
            "state_proof": "test.v2_business_cases.read_state 落库前后对比",
            "samples": [name for name, _, _, _ in REAL_SCENARIOS],
            "paths": roots[GROUP_REAL],
        },
        "isolation": {
            "suite_root": str(base),
            "production_database_untouched": str(PRODUCTION_DATABASE),
            "production_trace_database_untouched": str(PRODUCTION_TRACE_DATABASE),
            "milvus": "本专项不构建或修改任何索引，published alias 与集合保持原样",
        },
        "statistics": (
            "延迟 samples/mean/p50/p95/max：p50 取 statistics.median，"
            "p95 取 statistics.quantiles(n=100, method='inclusive')；"
            "confirmation_ms 覆盖每一次确认 POST 往返（含受控拒绝），"
            "confirmation_ms_accepted 只覆盖 HTTP 200 的受理与复用往返；"
            "真实用量仅由 real_chain 样本结算"
        ),
    }
    review = [
        {
            "review_id": "synthetic_usage_exclusion",
            "standard": (
                "协议构造组的模型身份与 USAGE 为合成输入，只用于协议判定；"
                "真实用量、运行数与执行根统计只取 real_chain 样本"
            ),
            "evidence_refs": [
                "stats.synthetic_constructed",
                "stats.usage",
                "samples[].synthetic_constructed",
            ],
        },
        {
            "review_id": "confirmation_latency_semantics",
            "standard": (
                "协议构造组确认延迟反映零模型的纯后端确认链路；真实链路组确认延迟反映"
                "真实快照准备后的确认提交，两组分别报告，不合并解读"
            ),
            "evidence_refs": [
                "stats.groups.protocol_constructed.confirmation_ms",
                "stats.groups.real_chain.confirmation_ms",
                "samples[].confirmation_ms",
            ],
        },
    ]
    return BenchmarkResult(
        topic=TOPIC, env=env, samples=samples, stats=stats, correctness=correctness,
        missing=missing, review=review, status=status,
    )


def run_benchmark(experiment_dir: str, *, groups: Sequence[str] = ALL_GROUPS) -> BenchmarkResult:
    requested = tuple(groups)
    unknown = [item for item in requested if item not in ALL_GROUPS]
    if unknown:
        raise ValueError(f"未知样本组：{unknown}，可选 {list(ALL_GROUPS)}")
    base = Path(experiment_dir)
    base.mkdir(parents=True, exist_ok=True)
    identity = (
        use_frozen_model_config(_experiment_root(base)) if GROUP_REAL in requested else None
    )
    protocol_root = SuiteRoot(base, PROTOCOL_DIR)
    real_root = SuiteRoot(base, REAL_DIR)
    roots = {GROUP_PROTOCOL: protocol_root.paths(), GROUP_REAL: real_root.paths()}
    samples: list[dict] = []
    if GROUP_PROTOCOL in requested:
        samples.extend(run_protocol(protocol_root))
    if GROUP_REAL in requested:
        samples.extend(run_real(real_root, identity))
    _write_json(base / "samples.json", samples)
    return _result(base, roots, identity, requested, samples)


def main() -> None:
    result = run_benchmark(
        str(BACKEND / "temp" / "save-confirmation-benchmark"), groups=(GROUP_PROTOCOL,)
    )
    print(
        json.dumps(
            {
                "status": result.status,
                "samples": result.stats["samples"],
                "confirmation_ms": result.stats["confirmation_ms"],
                "missing": result.missing,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
