# 运行：在 backend 执行 python -m test.check_trace_confirmation
import asyncio
import json
import re
import shutil
import sqlite3
import threading
import time
from math import isfinite
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from uuid import uuid4

import httpx

from app.agent import telemetry as telemetry_module
from app.agent.telemetry import open_trace_store
from app.ai.messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
)
from app.application.business import confirmation as confirmation_module
from app.application.business.catalog import Catalog
from app.application.business.confirmation import ConfirmationService
from app.application.business.coordination import ReplacementCoordinator
from app.application.business.profile import (
    BUSINESS_TIMEZONE,
    business_date,
)
from app.application.business.service import BusinessService
from app.application.session.credentials import CredentialFilter
from app.application.session.service import SessionService
from app.domain.business.errors import BusinessError
from app.domain.business.models import (
    BusinessContext,
    PlanImportArguments,
    ProfileContent,
    ProfileProposalArguments,
    SaveConfirmationRequest,
    WorkoutProposalArguments,
)
from app.domain.session.errors import SessionError
from app.domain.session.models import (
    SendCommand,
    SendRequest,
    SessionMessageEntry,
)
from app.infrastructure.persistence.sqlite.business_repository import (
    SqliteBusinessRepository,
)
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from app.interfaces.http import active, app
from test.regression_support import (
    Server,
    client,
    patch_default_database,
    patch_trace_database,
    temporary_root,
)

BACKEND = Path(__file__).resolve().parents[1]
ROOT = temporary_root("trace-confirmation")
EVIDENCE = ROOT / "evidence.json"
RESULTS: dict[str, object] = {}
TRACE_PATH: Path = ROOT / "unset.db"
SYSTEM = SystemMessage(role="system", content="系统提示词", tools_added=[], timestamp=100)
USAGE = Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2)
OPERATION_FIELDS = {
    "session_id", "operation_id", "business_kind", "proposal_id", "display_entry_id",
    "confirmation_entry_id", "status", "result", "error", "created_at", "updated_at",
}
PROFILE = {
    "goal": "增肌", "experience": "新手", "environment": "健身房", "availability": "每周四次",
    "health_notes": None, "movement_restrictions": None,
    "unavailable_equipment": [], "forbidden_exercise_ids": [],
}
PLAN = {
    "repeat": True,
    "days": [
        {"kind": "training", "focus": "推", "exercises": [], "notes": None},
        {"kind": "rest", "focus": None, "exercises": [], "notes": None},
    ],
    "notes": None,
    "suggested_fields": [],
}
WORKOUT = {
    "exercises": [{
        "exercise_id": None, "name": "俯卧撑", "load_convention": None,
        "sets": [{"reps": 10, "weight_kg": None, "duration_seconds": None}],
    }],
    "notes": None,
}
SPAN_COLUMNS = (
    "trace_id, span_id, parent_span_id, session_id, run_id, operation_id, name,"
    " started_at, finished_at, duration_ms, status, error_code, attributes"
)


def new_id() -> str:
    return str(uuid4())


def now_ms() -> int:
    return time_ns() // 1_000_000


async def rejection(awaitable):
    try:
        await awaitable
    except (BusinessError, SessionError) as error:
        return error
    raise AssertionError("expected BusinessError")


class RunTraceSpy:
    # 观测 hook：只记录真实 RunTrace 实例以核对释放边界，不替换任何业务实现。
    def __init__(self) -> None:
        self.created: list = []
        self._original = confirmation_module.RunTrace

    def __enter__(self) -> "RunTraceSpy":
        original = self._original
        created = self.created

        def factory(*args, **kwargs):
            trace = original(*args, **kwargs)
            created.append(trace)
            return trace

        confirmation_module.RunTrace = factory
        return self

    def __exit__(self, *_error) -> None:
        confirmation_module.RunTrace = self._original


async def spans_for(store, operation_id: str) -> list[dict]:
    cursor = await store._connection.execute(
        f"SELECT {SPAN_COLUMNS} FROM spans WHERE operation_id = ? ORDER BY span_id",
        (operation_id,),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [{**dict(row), "attributes": json.loads(row["attributes"])} for row in rows]


def assert_root(
    spans: list[dict], *, operation_id: str, session_id: str, kind: str,
    status: str, error_code: str | None = None,
) -> dict:
    assert len(spans) == 1, spans
    root = spans[0]
    assert root["name"] == "confirmation", root
    assert root["span_id"] == 1 and root["parent_span_id"] is None, root
    assert root["run_id"] is None, root
    assert root["session_id"] == session_id and root["operation_id"] == operation_id, root
    assert root["attributes"] == {"business_kind": kind}, root
    assert root["status"] == status and root["error_code"] == error_code, root
    assert re.fullmatch(r"[0-9a-f]{32}", root["trace_id"]), root
    assert int(root["trace_id"][12], 16) == 4, root
    assert isfinite(root["duration_ms"]) and root["duration_ms"] >= 0, root
    assert root["started_at"] <= root["finished_at"], root
    return root


def read_trace_file(operation_id: str) -> list[dict]:
    connection = sqlite3.connect(TRACE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            f"SELECT {SPAN_COLUMNS} FROM spans WHERE operation_id = ? ORDER BY span_id",
            (operation_id,),
        ).fetchall()
    finally:
        connection.close()
    return [{**dict(row), "attributes": json.loads(row["attributes"])} for row in rows]


async def count_rows(database, table: str) -> int:
    async def read(connection):
        cursor = await connection.execute(f"SELECT COUNT(*) FROM {table}", ())
        row = await cursor.fetchone()
        await cursor.close()
        return int(row[0])

    return await database.read(read)


class Fixture:
    # 真实隔离业务库 + 真实隔离观测库：确认根由 _execute 实际建立并写入。
    def __init__(self, root: Path, name: str) -> None:
        self.path = root / f"{name}.db"
        self.trace_path = root / f"{name}-trace.db"
        self.database = None
        self.store = None
        self.repository: SqliteBusinessRepository | None = None
        self.catalog: Catalog | None = None
        self.service: SessionService | None = None
        self.business: BusinessService | None = None
        self.confirmations: ConfirmationService | None = None

    async def open(self) -> "Fixture":
        self.database = await open_database(self.path)
        self.repository = SqliteBusinessRepository(self.database)
        self.catalog = Catalog.load()
        service = SessionService(SqliteSessionRepository(self.database))
        replacements = ReplacementCoordinator()
        self.business = BusinessService(self.repository, self.catalog, service.queries, replacements)
        service.attach_snapshots(self.business.proposals)
        self.service = service
        self.confirmations = ConfirmationService(
            self.repository, service.queries, service.operations, service.entries, self.business
        )
        self.store = await open_trace_store(self.trace_path)
        async with self.repository.transaction():
            await self.repository.replace_exercises(self.catalog.all())
        return self

    async def close(self) -> None:
        if self.store is not None:
            await self.store.close()
        await self.database.close()

    async def session(self, title: str = "Trace 确认会话") -> str:
        session_id = new_id()
        created, _ = await self.service.create_session_result(session_id, title)
        assert created
        return session_id

    def context(self, session_id: str, request: str, source: str) -> BusinessContext:
        return BusinessContext(
            timezone=BUSINESS_TIMEZONE,
            business_date=business_date(now_ms()),
            session_id=session_id,
            run_id="",
            request_entry_id=request,
            source_entry_id=source,
        )

    async def turn(self, session_id: str, text: str) -> str:
        outcome = await self.service.accept_send(
            SendCommand(operation_id=new_id(), session_id=session_id, request=SendRequest(text=text)),
            system_message=SYSTEM,
        )
        return outcome.run

    async def append(self, session_id: str, run_id: str, parent_id: str, message) -> str:
        node_id = new_id()
        await self.service.entries.append_entry(SessionMessageEntry(
            session_id=session_id, id=node_id, parent_id=parent_id, run_id=run_id,
            type="message", messages=[message], created_at=now_ms(),
        ))
        return node_id

    def assistant(self, call_id: str, name: str, arguments: dict) -> AssistantMessage:
        return AssistantMessage(
            role="assistant",
            content=[ToolCall(type="toolCall", id=call_id, name=name, arguments=arguments)],
            api="openai-completions", provider="example", model="model-1",
            usage=USAGE, stop_reason="toolUse", timestamp=now_ms(),
        )

    async def stage(self, session_id: str, kind: str, arguments: dict, tool_name: str) -> dict:
        run = await self.turn(session_id, "请整理待确认内容")
        call_id = "call-" + uuid4().hex[:8]
        source = await self.append(
            session_id, run.id, run.request_entry_id, self.assistant(call_id, tool_name, arguments)
        )
        context = self.context(session_id, run.request_entry_id, source)
        if kind == "profile":
            proposal = await self.business.profile.prepare_profile_update(
                context, ProfileProposalArguments.model_validate(arguments))
        elif kind == "plan_import":
            proposal = await self.business.plans.prepare_plan_import(
                context, PlanImportArguments.model_validate(arguments))
        else:
            proposal = await self.business.workouts.prepare_workout(
                context, WorkoutProposalArguments.model_validate(arguments))
        message = ToolResultMessage(
            role="toolResult", tool_call_id=call_id, tool_name=tool_name,
            content=[TextContent(type="text", text=proposal.model_dump_json())],
            is_error=False, timestamp=now_ms(),
        )
        display = await self.append(session_id, run.id, source, message)
        if kind == "profile":
            await self.business.profile.bind_display_entry(proposal.proposal_id, display)
        elif kind == "plan_import":
            await self.business.plans.bind_plan_display_entry(proposal.proposal_id, display)
        else:
            await self.business.workouts.bind_workout_display_entry(proposal.proposal_id, display)
        await self.service.runs.finish_run(session_id, run.id, "completed")
        return {
            "session": session_id, "request": run.request_entry_id, "source": source,
            "display": display, "proposal": proposal.proposal_id,
        }

    def request(self, session_id: str, kind: str, ids: dict, *, operation_id=None, **overrides):
        return SaveConfirmationRequest(
            operation_id=operation_id or new_id(),
            business_kind=kind,
            proposal_id=overrides.get("proposal", ids["proposal"]),
            display_entry_id=overrides.get("display", ids["display"]),
        )

    async def state(self) -> dict:
        return {
            "profile": await count_rows(self.database, "profile"),
            "profile_records": await count_rows(self.database, "profile_save_records"),
            "plans": await count_rows(self.database, "plans"),
            "plan_records": await count_rows(self.database, "plan_save_records"),
            "workouts": await count_rows(self.database, "workouts"),
            "workout_records": await count_rows(self.database, "workout_save_records"),
            "operations": await count_rows(self.database, "save_confirmation_operations"),
        }


async def check_kinds(root: Path) -> dict:
    f = await Fixture(root, "kinds").open()
    try:
        session = await f.session()
        # 画像新建
        profile_ids = await f.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": None, "payload": PROFILE},
            "prepare_profile_update")
        profile_op = await f.confirmations.confirm(
            session, f.request(session, "profile", profile_ids), trace_store=f.store)
        assert profile_op.status == "saved" and profile_op.result.version == 1
        assert_root(
            await spans_for(f.store, profile_op.operation_id),
            operation_id=profile_op.operation_id, session_id=session, kind="profile",
            status="completed")

        # 计划保存
        plan_ids = await f.stage(
            session, "plan_import",
            {"base_profile_version": 1, "base_plan_id": None, "payload": PLAN},
            "prepare_plan_import")
        plan_op = await f.confirmations.confirm(
            session, f.request(session, "plan", plan_ids), trace_store=f.store)
        assert plan_op.status == "saved"
        assert_root(
            await spans_for(f.store, plan_op.operation_id),
            operation_id=plan_op.operation_id, session_id=session, kind="plan",
            status="completed")

        # 训练记录新建
        day = business_date(now_ms())
        workout_ids = await f.stage(
            session, "workout",
            {"performed_on": day, "base_workout_id": None, "base_workout_version": None,
             "payload": WORKOUT},
            "prepare_workout")
        workout_op = await f.confirmations.confirm(
            session, f.request(session, "workout", workout_ids), trace_store=f.store)
        assert workout_op.status == "saved" and workout_op.result.version == 1
        assert_root(
            await spans_for(f.store, workout_op.operation_id),
            operation_id=workout_op.operation_id, session_id=session, kind="workout",
            status="completed")

        # 训练记录修正
        record = await f.repository.get_workout_by_date(day)
        updated = json.loads(json.dumps(WORKOUT))
        updated["notes"] = "肩部尚可"
        update_ids = await f.stage(
            session, "workout",
            {"performed_on": day, "base_workout_id": record.id,
             "base_workout_version": record.version, "payload": updated},
            "prepare_workout")
        update_op = await f.confirmations.confirm(
            session, f.request(session, "workout", update_ids), trace_store=f.store)
        assert update_op.status == "saved" and update_op.result.version == 2
        assert update_op.result.content.notes == "肩部尚可"
        assert_root(
            await spans_for(f.store, update_op.operation_id),
            operation_id=update_op.operation_id, session_id=session, kind="workout",
            status="completed")

        # 每次已受理保存恰好一条根记录；训练记录新增与修正同为 workout 类型。
        assert await f.store.trace_count() == 4
        kinds = [
            (await spans_for(f.store, op.operation_id))[0]["attributes"]["business_kind"]
            for op in (profile_op, plan_op, workout_op, update_op)
        ]
        assert kinds == ["profile", "plan", "workout", "workout"], kinds
        return {"kinds": kinds, "traces": await f.store.trace_count()}
    finally:
        await f.close()


async def check_failure_and_reuse(root: Path) -> dict:
    f = await Fixture(root, "failure").open()
    try:
        session = await f.session()
        ids = await f.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": None, "payload": PROFILE},
            "prepare_profile_update")
        # 受理后由其他路径推进版本：已受理保存失败以真实结局显式记 failed。
        async with f.repository.transaction():
            await f.repository.save_profile(ProfileContent.model_validate(PROFILE), 5, now_ms())
        filed_id = new_id()
        request = f.request(session, "profile", ids, operation_id=filed_id)
        failed = await f.confirmations.confirm(session, request, trace_store=f.store)
        assert failed.status == "failed" and failed.error.code == "profile_version_conflict"
        assert_root(
            await spans_for(f.store, filed_id), operation_id=filed_id, session_id=session,
            kind="profile", status="failed", error_code="profile_version_conflict")
        committed = await f.store.trace_count()

        # 失败重复：返回原失败对象、同一根记录，无新增执行与记录。
        replay = await f.confirmations.confirm(session, request, trace_store=f.store)
        assert replay.model_dump() == failed.model_dump()
        assert await f.store.trace_count() == committed
        assert len(await spans_for(f.store, filed_id)) == 1

        # 处理中重复：同一键请求复用原处理中对象，不启动保存、不建根。
        second_ids = await f.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": 5, "payload": {**PROFILE, "goal": "耐力"}},
            "prepare_profile_update")
        second_request = f.request(session, "profile", second_ids)
        operation, first = await f.confirmations._accept(session, second_request, now_ms())
        assert first
        during = await f.confirmations.confirm(session, second_request, trace_store=f.store)
        assert during.status == "processing" and during.model_dump() == operation.model_dump()
        assert await spans_for(f.store, second_request.operation_id) == []
        assert await f.store.trace_count() == committed

        # 原执行负责人完成后：同键请求返回固定结果，无新增记录。
        done = await f.confirmations._execute(
            operation, second_request, None, trace_store=f.store)
        assert done.status == "saved" and done.result.version == 6
        assert_root(
            await spans_for(f.store, second_request.operation_id),
            operation_id=second_request.operation_id, session_id=session, kind="profile",
            status="completed")
        completed = await f.store.trace_count()
        again = await f.confirmations.confirm(session, second_request, trace_store=f.store)
        assert again.status == "saved" and again.result.model_dump() == done.result.model_dump()
        assert await f.store.trace_count() == completed
        return {
            "failed": failed.error.code, "failed_traces": committed,
            "processing_reuse": during.status, "success_reuse": again.status,
            "traces": completed,
        }
    finally:
        await f.close()


async def check_saved_snapshot_reuse(root: Path) -> dict:
    f = await Fixture(root, "snapshot-reuse").open()
    try:
        session = await f.session()
        ids = await f.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": None, "payload": PROFILE},
            "prepare_profile_update")
        first = await f.confirmations.confirm(
            session, f.request(session, "profile", ids), trace_store=f.store)
        assert first.status == "saved"
        traces = await f.store.trace_count()
        nodes = len(await f.service.queries.list_entries(session))

        # 已保存快照复用：新操作 ID 关联原确认节点与固定结果，不新增节点、版本或根记录。
        again = await f.confirmations.confirm(
            session, f.request(session, "profile", ids), trace_store=f.store)
        assert again.status == "saved" and again.operation_id != first.operation_id
        assert again.result.model_dump() == first.result.model_dump()
        assert again.confirmation_entry_id == first.confirmation_entry_id
        assert await spans_for(f.store, again.operation_id) == []
        assert await f.store.trace_count() == traces
        assert len(await f.service.queries.list_entries(session)) == nodes
        assert (await f.repository.get_profile()).version == 1
        return {
            "same_result": again.result.model_dump() == first.result.model_dump(),
            "same_node": again.confirmation_entry_id == first.confirmation_entry_id,
            "traces": traces, "nodes": nodes,
        }
    finally:
        await f.close()


async def check_authorization_zero_commit(root: Path) -> dict:
    f = await Fixture(root, "auth").open()
    try:
        session = await f.session()
        other = await f.session("另一会话")
        ids = await f.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": None, "payload": PROFILE},
            "prepare_profile_update")
        baseline = await f.state()
        traces = await f.store.trace_count()
        results = {}
        results["wrong_display"] = (await rejection(f.confirmations.confirm(
            session, f.request(session, "profile", ids, display=new_id()),
            trace_store=f.store))).code
        results["wrong_session"] = (await rejection(f.confirmations.confirm(
            other, f.request(other, "profile", ids), trace_store=f.store))).code
        chat_id = new_id()
        sent = await f.service.accept_send(
            SendCommand(operation_id=chat_id, session_id=session, request=SendRequest(text="普通消息")),
            system_message=SYSTEM)
        await f.service.runs.finish_run(session, sent.run.id, "completed")
        results["cross_entry"] = (await rejection(f.confirmations.confirm(
            session, f.request(session, "profile", ids, operation_id=chat_id),
            trace_store=f.store))).code
        # 失效快照：同会话同目标新快照令旧快照失效，授权核验拒绝。
        await f.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": None, "payload": {**PROFILE, "goal": "耐力"}},
            "prepare_profile_update")
        results["invalidated"] = (await rejection(f.confirmations.confirm(
            session, f.request(session, "profile", ids), trace_store=f.store))).code

        # 全部拒绝路径业务零提交，且不留下任何确认根记录。
        assert await f.state() == baseline, (await f.state(), baseline)
        assert await f.store.trace_count() == traces
        kept = await f.repository.get_snapshot(ids["proposal"])
        assert kept.status == "invalidated" and kept.confirmation_entry_id is None
        return results
    finally:
        await f.close()


def _drop_spans(trace_path: Path):
    async def drop():
        store = await open_trace_store(trace_path)
        cursor = await store._connection.execute("DROP TABLE spans")
        await cursor.close()
        return store

    return drop()


async def check_observation_and_business_faults(root: Path) -> dict:
    f = await Fixture(root, "faults").open()
    try:
        session = await f.session()
        ids = await f.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": None, "payload": PROFILE},
            "prepare_profile_update")
        results = {}

        # 无观测依赖：无记录语义，业务照常保存且不建立记录器。
        with RunTraceSpy() as spy:
            via_none = await f.confirmations.confirm(
                session, f.request(session, "profile", ids), trace_store=None)
        assert via_none.status == "saved" and spy.created == []
        results["none"] = via_none.status

        # 观测写入故障隔离：已提交业务结果与响应保持原样，记录器仍释放。
        second_ids = await f.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": 1, "payload": {**PROFILE, "goal": "耐力"}},
            "prepare_profile_update")
        broken = await _drop_spans(root / "faults-broken.db")
        with RunTraceSpy() as spy2:
            via_broken = await f.confirmations.confirm(
                session, f.request(session, "profile", second_ids), trace_store=broken)
        assert via_broken.status == "saved" and via_broken.result.version == 2
        assert spy2.created and spy2.created[0].released is True
        fetched = await f.confirmations.get(session, via_broken.operation_id)
        assert fetched.model_dump() == via_broken.model_dump()
        await broken.close()
        results["observation_fault"] = via_broken.status

        # 业务失败 + 观测失败：保留原始业务结果（failed 操作与受控错误码）。
        third_ids = await f.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": 2, "payload": {**PROFILE, "goal": "力量"}},
            "prepare_profile_update")
        async with f.repository.transaction():
            await f.repository.save_profile(ProfileContent.model_validate(PROFILE), 9, now_ms())
        broken2 = await _drop_spans(root / "faults-broken2.db")
        failed = await f.confirmations.confirm(
            session, f.request(session, "profile", third_ids), trace_store=broken2)
        assert failed.status == "failed" and failed.error.code == "profile_version_conflict"
        await broken2.close()
        results["double_business"] = failed.error.code

        # 意外业务异常 + 观测失败：原始异常对象原样传播。
        fourth_ids = await f.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": 9, "payload": {**PROFILE, "goal": "减脂"}},
            "prepare_profile_update")
        broken3 = await _drop_spans(root / "faults-broken3.db")
        original_save = f.business.profile.save_profile_update
        error = RuntimeError("业务原始异常正文")
        spy3 = RunTraceSpy()
        spy3.__enter__()

        async def boom(context, arguments, signal=None, *, on_committed=None):
            raise error

        f.business.profile.save_profile_update = boom
        raised = None
        try:
            await f.confirmations.confirm(
                session, f.request(session, "profile", fourth_ids), trace_store=broken3)
        except RuntimeError as caught:
            raised = caught
        finally:
            f.business.profile.save_profile_update = original_save
            spy3.__exit__(None, None, None)
            await broken3.close()
        assert raised is error
        assert spy3.created and spy3.created[-1].released is True
        results["double_unexpected"] = type(raised).__name__
        return results
    finally:
        await f.close()


async def check_cancellation_and_recovery(root: Path) -> dict:
    f = await Fixture(root, "cancel").open()
    try:
        session = await f.session()
        ids = await f.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": None, "payload": PROFILE},
            "prepare_profile_update")
        request = f.request(session, "profile", ids)
        entered = asyncio.Event()
        unblock = asyncio.Event()
        original_save = f.business.profile.save_profile_update

        async def slow(context, arguments, signal=None, *, on_committed=None):
            entered.set()
            await unblock.wait()
            return await original_save(context, arguments, signal, on_committed=on_committed)

        f.business.profile.save_profile_update = slow
        spy = RunTraceSpy()
        spy.__enter__()
        task = asyncio.create_task(
            f.confirmations.confirm(session, request, trace_store=f.store))
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel()
        cancelled = False
        try:
            await task
        except asyncio.CancelledError:
            cancelled = True
        finally:
            spy.__exit__(None, None, None)
            f.business.profile.save_profile_update = original_save
            unblock.set()
        assert cancelled

        # 真实取消：根按真实结局记 cancelled，记录器释放。
        assert_root(
            await spans_for(f.store, request.operation_id), operation_id=request.operation_id,
            session_id=session, kind="profile", status="cancelled")
        assert spy.created and spy.created[-1].released is True
        traces = await f.store.trace_count()

        # 业务控制流保持：操作仍为处理中，启动对账按当前机制恢复中断失败，不补造 Trace。
        processing = await f.confirmations.get(session, request.operation_id)
        assert processing.status == "processing"
        recovered = await f.confirmations.recover_interrupted()
        assert recovered >= 1
        failed = await f.confirmations.get(session, request.operation_id)
        assert failed.status == "failed" and failed.error.code == "confirmation_interrupted"
        assert await f.store.trace_count() == traces
        snapshot = await f.repository.get_snapshot(ids["proposal"])
        assert snapshot.status == "pending" and snapshot.confirmation_entry_id is None
        return {
            "cancelled": cancelled, "root_status": "cancelled", "traces": traces,
            "recovered": failed.error.code,
        }
    finally:
        await f.close()


async def check_credential_boundary(root: Path) -> dict:
    f = await Fixture(root, "credential").open()
    try:
        session = await f.session()
        ids = await f.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": None, "payload": PROFILE},
            "prepare_profile_update")
        # 复用现有凭据检查：命中会话身份的记录连同子树被拒绝，业务仍提交。
        rejected = await f.confirmations.confirm(
            session, f.request(session, "profile", ids), trace_store=f.store,
            contains=CredentialFilter((session,)).contains)
        assert rejected.status == "saved"
        assert await spans_for(f.store, rejected.operation_id) == []

        # 属性白名单：确认根只记录 business_kind，快照、对话、伤病信息与凭据不进入 Trace。
        second_ids = await f.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": 1, "payload": {**PROFILE, "goal": "耐力"}},
            "prepare_profile_update")
        saved = await f.confirmations.confirm(
            session, f.request(session, "profile", second_ids), trace_store=f.store)
        record = assert_root(
            await spans_for(f.store, saved.operation_id), operation_id=saved.operation_id,
            session_id=session, kind="profile", status="completed")
        assert set(record["attributes"]) == {"business_kind"}
        return {"rejected_spans": 0, "attributes": record["attributes"]}
    finally:
        await f.close()


class Live:
    def __init__(self, server: Server) -> None:
        self.http = client(server.base_url)
        self.loop = app.state.loop
        self.service: SessionService = app.state.session_service
        self.business: BusinessService = app.state.business
        self.repository = app.state.business_repository

    def call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def session(self) -> str:
        async def create():
            created, session = await self.service.create_session_result(new_id(), "Trace 确认 HTTP")
            assert created
            return session.id

        return self.call(create())

    async def stage_async(self, session_id: str, base_profile_version) -> dict:
        arguments = {
            "profile_id": 1, "base_profile_version": base_profile_version, "payload": PROFILE,
        }
        outcome = await self.service.accept_send(
            SendCommand(operation_id=new_id(), session_id=session_id,
                        request=SendRequest(text="请整理待确认内容")),
            system_message=SYSTEM)
        run = outcome.run
        call_id = "call-" + uuid4().hex[:8]
        source = new_id()
        await self.service.entries.append_entry(SessionMessageEntry(
            session_id=session_id, id=source, parent_id=run.request_entry_id, run_id=run.id,
            type="message", messages=[AssistantMessage(
                role="assistant",
                content=[ToolCall(type="toolCall", id=call_id, name="prepare_profile_update",
                                  arguments=arguments)],
                api="openai-completions", provider="example", model="model-1", usage=USAGE,
                stop_reason="toolUse", timestamp=now_ms())], created_at=now_ms()))
        context = BusinessContext(
            timezone=BUSINESS_TIMEZONE, business_date=business_date(now_ms()),
            session_id=session_id, run_id="", request_entry_id=run.request_entry_id,
            source_entry_id=source)
        proposal = await self.business.profile.prepare_profile_update(
            context, ProfileProposalArguments.model_validate(arguments))
        display = new_id()
        await self.service.entries.append_entry(SessionMessageEntry(
            session_id=session_id, id=display, parent_id=source, run_id=run.id,
            type="message", messages=[ToolResultMessage(
                role="toolResult", tool_call_id=call_id, tool_name="prepare_profile_update",
                content=[TextContent(type="text", text=proposal.model_dump_json())],
                is_error=False, timestamp=now_ms())], created_at=now_ms()))
        await self.business.profile.bind_display_entry(proposal.proposal_id, display)
        await self.service.runs.finish_run(session_id, run.id, "completed")
        return {"proposal": proposal.proposal_id, "display": display, "source": source}

    def stage_profile(self, session_id: str) -> dict:
        # 画像为全局单例：基础版本取当前真实版本，首次建档为 null。
        base_version = self.http.get("/api/profile").json()["version"]
        return self.call(self.stage_async(session_id, base_version))

    def confirm(self, session_id: str, ids: dict, operation_id: str):
        return self.http.post(
            f"/api/sessions/{session_id}/confirmations",
            json={"operation_id": operation_id, "business_kind": "profile",
                  "proposal_id": ids["proposal"], "display_entry_id": ids["display"]})

    def get_op(self, session_id: str, operation_id: str):
        return self.http.get(f"/api/sessions/{session_id}/confirmations/{operation_id}")


def check_http_response_and_trace(live: Live) -> dict:
    session = live.session()
    ids = live.stage_profile(session)
    operation_id = new_id()
    response = live.confirm(session, ids, operation_id)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    operation = response.json()
    assert set(operation) == OPERATION_FIELDS, operation
    assert operation["operation_id"] == operation_id and operation["status"] == "saved"
    assert operation["error"] is None and operation["result"] is not None

    # 响应返回时对应 Trace 已完成写入尝试：立即同步读取隔离观测库。
    record = assert_root(
        read_trace_file(operation_id), operation_id=operation_id, session_id=session,
        kind="profile", status="completed")
    # 业务结果与公开字段保持原样：GET 查询与响应一致。
    fetched = live.get_op(session, operation_id)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json() == operation, fetched.text
    assert fetched.headers["cache-control"] == "no-store"
    return {
        "status": operation["status"], "traces": len(read_trace_file(operation_id)),
        "trace_id": record["trace_id"], "public_fields": sorted(operation),
    }


def check_http_disconnect(live: Live) -> dict:
    session = live.session()
    ids = live.stage_profile(session)
    operation_id = new_id()
    unblock = threading.Event()
    original_save = live.business.profile.save_profile_update

    async def slow_save(context, arguments, signal=None, *, on_committed=None):
        await asyncio.get_running_loop().run_in_executor(None, unblock.wait, 30)
        return await original_save(context, arguments, signal, on_committed=on_committed)

    live.business.profile.save_profile_update = slow_save
    aborted = False
    try:
        short = httpx.Client(
            base_url=live.http.base_url,
            headers={"Host": "127.0.0.1:8000", "Origin": "http://localhost:5173"},
            timeout=1.0, trust_env=False)
        try:
            short.post(
                f"/api/sessions/{session}/confirmations",
                json={"operation_id": operation_id, "business_kind": "profile",
                      "proposal_id": ids["proposal"], "display_entry_id": ids["display"]})
        except httpx.TimeoutException:
            aborted = True
        finally:
            short.close()
        assert aborted

        # 断连只关闭响应：后台保存与观测收尾继续完成，占用恰好释放一次。
        unblock.set()
        deadline = time.monotonic() + 30
        operation = None
        while time.monotonic() < deadline:
            fetched = live.get_op(session, operation_id)
            if fetched.status_code == 200 and fetched.json()["status"] in {"saved", "failed"}:
                operation = fetched.json()
                break
            time.sleep(0.05)
        assert operation is not None and operation["status"] == "saved", operation
        assert not active.locked()
        record = assert_root(
            read_trace_file(operation_id), operation_id=operation_id, session_id=session,
            kind="profile", status="completed")
        return {
            "aborted": aborted, "status": operation["status"],
            "active_free": not active.locked(), "span": record["name"],
        }
    finally:
        live.business.profile.save_profile_update = original_save
        unblock.set()


def check() -> None:
    if ROOT.exists():
        shutil.rmtree(ROOT)
    ROOT.mkdir(parents=True, exist_ok=True)
    production = telemetry_module.trace_database_path()
    production_existed = production.exists()
    production_mtime = production.stat().st_mtime_ns if production_existed else None

    global TRACE_PATH
    with TemporaryDirectory(dir=ROOT, ignore_cleanup_errors=True) as directory:
        root = Path(directory)
        RESULTS["kinds"] = asyncio.run(check_kinds(root))
        RESULTS["failure_and_reuse"] = asyncio.run(check_failure_and_reuse(root))
        RESULTS["snapshot_reuse"] = asyncio.run(check_saved_snapshot_reuse(root))
        RESULTS["authorization_zero_commit"] = asyncio.run(check_authorization_zero_commit(root))
        RESULTS["faults"] = asyncio.run(check_observation_and_business_faults(root))
        RESULTS["cancellation"] = asyncio.run(check_cancellation_and_recovery(root))
        RESULTS["credential_boundary"] = asyncio.run(check_credential_boundary(root))

        patch_default_database("trace-confirmation")
        TRACE_PATH = patch_trace_database("trace-confirmation")
        with Server(app) as server:
            live = Live(server)
            RESULTS["http_response"] = check_http_response_and_trace(live)
            RESULTS["http_disconnect"] = check_http_disconnect(live)

    # 观测库生产默认路径零改动：本次运行只写入隔离观测库。
    assert production == BACKEND / "data" / "trace.db"
    assert production.exists() == production_existed
    if production_mtime is not None:
        assert production.stat().st_mtime_ns == production_mtime

    EVIDENCE.write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(
        "PASS: 确认保存 Trace —— 画像/计划/训练新增与修正的独立根、真实受理身份、"
        "run_id=null、business_kind 白名单、有限非负耗时与释放、失败与重复复用零新增、"
        "已保存快照复用不新增节点与版本、授权类拒绝业务零提交零 Trace、"
        "观测与业务故障隔离与双重失败、真实取消与对账恢复、凭据与属性白名单、"
        "HTTP 响应返回即完成写入尝试、断连后台收尾与 active 释放、生产路径零改动"
    )


if __name__ == "__main__":
    check()
