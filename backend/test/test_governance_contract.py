"""治理基础契约自检：停止原因、截止时间、预算组合扣除与真实 SQLite 持久化。"""

import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Barrier, Lock, Thread

import aiosqlite
import pytest

from app.agent.budget import (
    MAX_MODEL_REQUESTS,
    MAX_RECOVERY_REQUESTS,
    ModelRequestLimitExceeded,
    RecoveryBudgetExhausted,
    RequestBudget,
)
from app.agent.governance import (
    RUN_EXECUTION_BUDGET_SECONDS,
    STOP_TERMINAL,
    RunControl,
    RunDeadline,
    StopDecision,
    stop_terminal,
)
from app.ai.messages import UserMessage
from app.ai.types import ModelSpec
from app.application.session.service import SessionService
from app.domain.session.errors import SessionConflict
from app.domain.session.models import SessionRun
from app.infrastructure.persistence.sqlite.database import (
    _MIGRATIONS,
    SCHEMA_VERSION,
    _enable_foreign_keys,
    _migrate,
    open_database,
)
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository

LEGACY_VERSION = 14
DIAGNOSTIC_VERSION = 15
USER_MESSAGE = json.dumps(
    [{"role": "user", "content": [{"type": "text", "text": "开始训练"}]}],
    ensure_ascii=False,
)

# 本地可控 OpenAI 兼容流式端点：真实 SSE 与真实 usage，不做任何客户端替换。
SUMMARY_TEXT = "## Goal\n完成四日循环计划\n\n## Critical Context\n- 左膝术后，保持每周四练"
SUMMARY_CHUNKS = [
    {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test",
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": SUMMARY_TEXT},
                "finish_reason": None,
            }
        ],
    },
    {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    },
    {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test",
        "choices": [],
        "usage": {
            "prompt_tokens": 15,
            "completion_tokens": 6,
            "total_tokens": 21,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    },
]


class SummaryHttpServer:
    """本地可控真实 HTTP 端点：按真实 SSE 顺序返回摘要与用量，仅统计请求数。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self.requests = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                with outer._lock:
                    outer.requests += 1
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in SUMMARY_CHUNKS:
                    self.wfile.write(
                        f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
                    )
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

            def log_message(self, *_args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def __enter__(self) -> "SummaryHttpServer":
        self._thread.start()
        return self

    def __exit__(self, *_error) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


async def _one(connection, sql: str, parameters=()):
    cursor = await connection.execute(sql, parameters)
    row = await cursor.fetchone()
    await cursor.close()
    return row


async def _all(connection, sql: str, parameters=()):
    cursor = await connection.execute(sql, parameters)
    rows = await cursor.fetchall()
    await cursor.close()
    return rows


async def _seed_session_and_entry(connection, session_id="s-1", entry_id="e-1") -> None:
    await connection.execute(
        "INSERT INTO sessions (id, title, active_leaf_id, created_at, updated_at) "
        "VALUES (?, ?, NULL, ?, ?)",
        (session_id, "会话", 1, 1),
    )
    await connection.execute(
        "INSERT INTO session_entries "
        "(session_id, id, parent_id, run_id, type, messages, created_at) "
        "VALUES (?, ?, NULL, NULL, 'message', ?, ?)",
        (session_id, entry_id, USER_MESSAGE, 1),
    )


async def _seed_running_run(connection, session_id="s-1", entry_id="e-1", run_id="r-1") -> None:
    await _seed_session_and_entry(connection, session_id, entry_id)
    # 不显式写入 stop_reason：兼容 v14 旧库（该列尚不存在），新库按可空默认回落。
    await connection.execute(
        "INSERT INTO session_runs "
        "(session_id, id, request_entry_id, last_entry_id, status, started_at, "
        "finished_at, error_code, error_message) "
        "VALUES (?, ?, ?, NULL, 'running', 1, NULL, NULL, NULL)",
        (session_id, run_id, entry_id),
    )


def test_stop_decision_keeps_first_reason_only() -> None:
    decision = StopDecision()
    assert decision.reason is None
    assert decision.request("run_timeout") is True
    assert decision.request("cancelled") is False
    assert decision.reason == "run_timeout"


def test_stop_decision_under_concurrency_accepts_single_reason() -> None:
    decision = StopDecision()
    reasons = sorted(STOP_TERMINAL)
    barrier = Barrier(len(reasons))
    accepted: list[str] = []
    guard = Lock()

    def attempt(reason) -> None:
        barrier.wait()
        if decision.request(reason):
            with guard:
                accepted.append(reason)

    threads = [Thread(target=attempt, args=(reason,)) for reason in reasons]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(accepted) == 1
    assert decision.reason == accepted[0]


def test_stop_terminal_mapping_matches_contract() -> None:
    assert stop_terminal("model_timeout") == ("failed", "model_timeout")
    assert stop_terminal("tool_timeout") == ("failed", "tool_timeout")
    assert stop_terminal("run_timeout") == ("failed", "run_timeout")
    assert stop_terminal("cancelled") == ("cancelled", "cancelled")
    assert stop_terminal("slow_consumer") == ("cancelled", "slow_consumer")
    assert stop_terminal("output_too_large") == ("failed", "output_too_large")
    assert set(STOP_TERMINAL) == {
        "model_timeout",
        "tool_timeout",
        "run_timeout",
        "cancelled",
        "slow_consumer",
        "output_too_large",
    }


def test_run_deadline_uses_injected_monotonic_clock() -> None:
    state = {"now": 100.0}

    def clock() -> float:
        return state["now"]

    assert RunDeadline().seconds == RUN_EXECUTION_BUDGET_SECONDS
    deadline = RunDeadline(30.0, clock=clock)
    assert deadline.started_at == 100.0
    assert deadline.expires_at == 130.0
    assert deadline.remaining() == 30.0
    assert deadline.effective(5.0) == 5.0
    assert deadline.effective(40.0) == 30.0

    state["now"] = 120.0
    assert deadline.remaining() == 10.0
    assert deadline.expired() is False

    state["now"] = 130.0
    assert deadline.remaining() == 0.0
    assert deadline.expired() is True
    assert deadline.effective(7.0) == 0.0

    with pytest.raises(ValueError):
        RunDeadline(0)


def test_run_control_records_first_reason_before_setting_signal() -> None:
    control = RunControl(deadline=RunDeadline(60.0))
    assert control.cancelled is False
    assert 0.0 < control.remaining() <= 60.0
    assert control.request_cancel("slow_consumer") is True
    assert control.cancelled is True
    assert control.stop.reason == "slow_consumer"
    # 后续停止信号不覆盖首个原因。
    assert control.request_cancel("cancelled") is False
    assert control.stop.reason == "slow_consumer"


def test_request_budget_boundary_and_defaults() -> None:
    budget = RequestBudget()
    assert budget.limit == MAX_MODEL_REQUESTS == 64
    assert budget.recovery_limit == MAX_RECOVERY_REQUESTS == 4
    assert budget.used == 0 and budget.remaining == 64

    small = RequestBudget(limit=2)
    assert small.can_reserve() is True
    assert small.reserve() == 1
    assert small.reserve() == 2
    assert small.can_reserve() is False
    with pytest.raises(ModelRequestLimitExceeded):
        small.reserve()
    assert small.used == 2 and small.remaining == 0


def test_request_budget_recovery_consumes_total_and_recovery_once() -> None:
    budget = RequestBudget(limit=3, recovery_limit=2)
    assert budget.can_reserve(recovery=True) is True

    budget.reserve(recovery=True)
    assert (budget.used, budget.recovery_used, budget.recovery_remaining) == (1, 1, 1)

    budget.reserve()
    assert (budget.used, budget.recovery_used) == (2, 1)

    budget.reserve(recovery=True)
    assert (budget.used, budget.recovery_used) == (3, 2)
    assert budget.can_reserve() is False
    assert budget.can_reserve(recovery=True) is False
    # 总预算耗尽优先，保留既有超限异常。
    with pytest.raises(ModelRequestLimitExceeded):
        budget.reserve(recovery=True)


def test_request_budget_recovery_exhaustion_keeps_total_and_deducts_nothing() -> None:
    budget = RequestBudget(limit=5, recovery_limit=1)
    budget.reserve(recovery=True)
    assert budget.can_reserve(recovery=True) is False
    assert budget.can_reserve() is True
    with pytest.raises(RecoveryBudgetExhausted):
        budget.reserve(recovery=True)
    assert (budget.used, budget.recovery_used) == (1, 1)


def test_request_budget_concurrent_reserve_never_overdraws() -> None:
    budget = RequestBudget(limit=10, recovery_limit=4)
    attempts = 50
    barrier = Barrier(attempts)
    succeeded: list[int] = []
    rejected: list[int] = []
    guard = Lock()

    def attempt(index: int) -> None:
        barrier.wait()
        try:
            budget.reserve()
        except ModelRequestLimitExceeded:
            with guard:
                rejected.append(index)
            return
        with guard:
            succeeded.append(index)

    threads = [Thread(target=attempt, args=(index,)) for index in range(attempts)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(succeeded) == 10
    assert len(rejected) == attempts - 10
    assert budget.used == 10 and budget.remaining == 0


def test_terminal_decision_prefers_stop_reason_but_keeps_credential_priority() -> None:
    from app.agent.tool import CredentialDetectedError
    from app.interfaces.http import terminal_decision

    assert terminal_decision(None, False) == ("completed", None, None)
    assert terminal_decision(None, True) == ("cancelled", "cancelled", "执行已取消。")
    assert terminal_decision(None, False, "run_timeout") == (
        "failed",
        "run_timeout",
        "本次运行已超时结束。",
    )
    assert terminal_decision(None, True, "run_timeout")[:2] == ("failed", "run_timeout")
    assert terminal_decision(None, False, "slow_consumer")[:2] == (
        "cancelled",
        "slow_consumer",
    )
    assert terminal_decision(None, False, "output_too_large")[:2] == (
        "failed",
        "output_too_large",
    )
    credential = terminal_decision(CredentialDetectedError(), True, "run_timeout")
    assert credential[:2] == ("failed", "credential_detected")


async def test_stop_reason_roundtrip_through_real_sqlite(tmp_path: Path) -> None:
    database = await open_database(tmp_path / "roundtrip.db")
    try:
        await _seed_session_and_entry(database.connection)
        repository = SqliteSessionRepository(database)
        await repository.insert_run(
            SessionRun(
                session_id="s-1",
                id="r-1",
                request_entry_id="e-1",
                last_entry_id=None,
                status="failed",
                started_at=1,
                finished_at=2,
                error_code="tool_timeout",
                error_message="工具执行超时",
                stop_reason="tool_timeout",
            )
        )
        stored = await repository.get_run("s-1", "r-1")
        assert stored is not None
        assert stored.stop_reason == "tool_timeout"
        assert stored.error_code == "tool_timeout"
        # 未记录停止原因的历史形状读回 None。
        await repository.insert_run(
            SessionRun(
                session_id="s-1",
                id="r-2",
                request_entry_id="e-1",
                last_entry_id=None,
                status="completed",
                started_at=3,
                finished_at=4,
                error_code=None,
                error_message=None,
            )
        )
        legacy = await repository.get_run("s-1", "r-2")
        assert legacy is not None and legacy.stop_reason is None
    finally:
        await database.close()


async def test_finish_run_persists_first_stop_reason_and_keeps_terminal(tmp_path: Path) -> None:
    database = await open_database(tmp_path / "finish.db")
    try:
        await _seed_running_run(database.connection)
        service = SessionService(SqliteSessionRepository(database))

        outcome = await service.runs.finish_run(
            "s-1",
            "r-1",
            "failed",
            error_code="model_timeout",
            error_message="模型响应超时，请重新发起请求。",
            stop_reason="model_timeout",
        )
        assert outcome.changed is True
        assert outcome.run.stop_reason == "model_timeout"

        stored = await service.queries.get_run("s-1", "r-1")
        assert (stored.status, stored.error_code, stored.stop_reason) == (
            "failed",
            "model_timeout",
            "model_timeout",
        )

        # 已提交终态保持原值：不同终态被拒绝，同终态幂等返回且不改写停止原因。
        with pytest.raises(SessionConflict):
            await service.runs.finish_run(
                "s-1",
                "r-1",
                "cancelled",
                error_code="slow_consumer",
                stop_reason="slow_consumer",
            )
        replay = await service.runs.finish_run(
            "s-1", "r-1", "failed", error_code="tool_timeout", stop_reason="tool_timeout"
        )
        assert replay.changed is False
        final = await service.queries.get_run("s-1", "r-1")
        assert (final.status, final.error_code, final.stop_reason) == (
            "failed",
            "model_timeout",
            "model_timeout",
        )
    finally:
        await database.close()


async def test_migration_v14_upgrade_adds_stop_reason_and_keeps_old_rows(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    legacy = await aiosqlite.connect(path, isolation_level=None)
    legacy.row_factory = sqlite3.Row
    try:
        await _enable_foreign_keys(legacy)
        await _migrate(legacy, 1, LEGACY_VERSION)
        assert (await _one(legacy, "PRAGMA user_version"))[0] == LEGACY_VERSION
        await _seed_running_run(legacy)
        columns = {row[1] for row in await _all(legacy, "PRAGMA table_info(session_runs)")}
        assert "stop_reason" not in columns
        assert "stop_diagnostic" not in columns
        frozen = await _one(
            legacy, "SELECT status, error_code FROM session_runs WHERE id = 'r-1'"
        )
        assert frozen["status"] == "running" and frozen["error_code"] is None
    finally:
        await legacy.close()

    migrated = await open_database(path)
    try:
        assert (await _one(migrated.connection, "PRAGMA user_version"))[0] == SCHEMA_VERSION
        columns = {
            row[1]
            for row in await _all(migrated.connection, "PRAGMA table_info(session_runs)")
        }
        assert "stop_reason" in columns and "stop_diagnostic" in columns
        upgraded = await _one(
            migrated.connection,
            "SELECT status, error_code, error_message, stop_reason, stop_diagnostic "
            "FROM session_runs WHERE id = 'r-1'",
        )
        assert upgraded["status"] == "running"
        assert upgraded["error_code"] is None
        assert upgraded["error_message"] is None
        assert upgraded["stop_reason"] is None
        assert upgraded["stop_diagnostic"] is None
        # 升级记录可参与首次终态提交并写入停止原因。
        service = SessionService(SqliteSessionRepository(migrated))
        await service.runs.finish_run(
            "s-1",
            "r-1",
            "cancelled",
            error_code="slow_consumer",
            stop_reason="slow_consumer",
        )
        finished = await service.queries.get_run("s-1", "r-1")
        assert (finished.status, finished.stop_reason) == ("cancelled", "slow_consumer")
    finally:
        await migrated.close()


def test_migration_registry_exposes_run_governance_versions() -> None:
    assert SCHEMA_VERSION == 16
    assert _MIGRATIONS[15].name == "015_run_stop_reason.sql"
    assert _MIGRATIONS[16].name == "016_run_stop_diagnostic.sql"


async def test_migration_v15_upgrade_keeps_stop_reason_and_adds_diagnostic(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v15.db"
    legacy = await aiosqlite.connect(path, isolation_level=None)
    legacy.row_factory = sqlite3.Row
    try:
        await _enable_foreign_keys(legacy)
        await _migrate(legacy, 1, DIAGNOSTIC_VERSION)
        assert (await _one(legacy, "PRAGMA user_version"))[0] == DIAGNOSTIC_VERSION
        await _seed_session_and_entry(legacy)
        await legacy.execute(
            "INSERT INTO session_runs "
            "(session_id, id, request_entry_id, last_entry_id, status, started_at, "
            "finished_at, error_code, error_message, stop_reason) "
            "VALUES ('s-1', 'r-1', 'e-1', NULL, 'cancelled', 1, 2, 'slow_consumer', "
            "'客户端读取过慢', 'slow_consumer')"
        )
    finally:
        await legacy.close()

    migrated = await open_database(path)
    try:
        assert (await _one(migrated.connection, "PRAGMA user_version"))[0] == SCHEMA_VERSION
        upgraded = await _one(
            migrated.connection,
            "SELECT status, error_code, stop_reason, stop_diagnostic FROM session_runs "
            "WHERE id = 'r-1'",
        )
        assert upgraded["status"] == "cancelled"
        assert upgraded["error_code"] == "slow_consumer"
        assert upgraded["stop_reason"] == "slow_consumer"
        assert upgraded["stop_diagnostic"] is None
    finally:
        await migrated.close()


async def test_run_diagnostic_roundtrip_survives_real_reopen(tmp_path: Path) -> None:
    path = tmp_path / "diagnostic.db"
    database = await open_database(path)
    try:
        await _seed_running_run(database.connection)
        service = SessionService(SqliteSessionRepository(database))
        recorded = await service.runs.record_run_diagnostic("s-1", "r-1", "worker_stuck")
        assert recorded.status == "running"
        assert recorded.stop_diagnostic == "worker_stuck"
        # 诊断槽位保留首个已记录证据。
        again = await service.runs.record_run_diagnostic("s-1", "r-1", "terminal_persist_failed")
        assert again.stop_diagnostic == "worker_stuck"
    finally:
        await database.close()

    reopened = await open_database(path)
    try:
        service = SessionService(SqliteSessionRepository(reopened))
        stored = await service.queries.get_run("s-1", "r-1")
        assert stored.status == "running"
        assert stored.stop_diagnostic == "worker_stuck"
        assert stored.stop_reason is None
        with pytest.raises(ValueError):
            await service.runs.record_run_diagnostic("s-1", "r-1", "not_registered")
        assert (await service.queries.get_run("s-1", "r-1")).stop_diagnostic == "worker_stuck"
    finally:
        await reopened.close()


async def test_summary_request_uses_local_endpoint_and_same_run_deadline(monkeypatch) -> None:
    # 真实摘要链路：本地可控 HTTP 端点 + 真实适配器、真实摘要与真实用量。
    # 截止时间当前尚无消费方（执行接入归任务二），故在真实 complete 外挂一层只读转发记录器：
    # 真实实现继续执行，真实请求仍发往本地端点（server.requests == 1 为证）。
    from app.agent import compaction as compaction_module

    deadline = RunDeadline(120.0)
    captured: list[dict] = []
    real_complete = compaction_module.complete

    async def recording_complete(model, context, options):
        captured.append(options)
        return await real_complete(model, context, options)

    monkeypatch.setattr(compaction_module, "complete", recording_complete)
    preparation = compaction_module.CompactionPreparation(
        first_kept_entry_id="kept-1",
        messages_to_summarize=[UserMessage(role="user", content="历史" * 100, timestamp=1)],
        turn_prefix_messages=[],
        is_split_turn=False,
        tokens_before=0,
        previous_summary=None,
        attachments=[],
    )
    budget = RequestBudget()
    with SummaryHttpServer() as server:
        result = await compaction_module.generate_compaction_summary(
            preparation,
            ModelSpec(
                api="openai-completions",
                provider="test",
                id="test",
                base_url=server.base_url,
                context_window=128000,
                max_tokens=16384,
            ),
            api_key="test-key",
            budget=budget,
            deadline=deadline,
        )
        assert server.requests == 1

    assert result.summary == SUMMARY_TEXT
    assert result.usage.total_tokens == 21
    assert budget.used == 1
    assert len(captured) == 1
    assert captured[0]["deadline"] is deadline
