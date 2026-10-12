import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from uuid import uuid4

import httpx

from app.ai.messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
)
from app.application.business.catalog import Catalog
from app.application.business.confirmation import ConfirmationService
from app.application.business.coordination import ReplacementCoordinator
from app.application.business.service import BusinessService, business_date
from app.application.session.service import SessionService
from app.domain.business.models import (
    BusinessContext,
    ProfileContent,
    ProfileProposalArguments,
    SaveConfirmationOperation,
)
from app.domain.session.models import (
    EditCommand,
    EditRequest,
    SaveConfirmation,
    SendCommand,
    SendRequest,
    SessionMessageEntry,
)
from app.infrastructure.persistence.sqlite.business_repository import (
    SqliteBusinessRepository,
)
from app.infrastructure.persistence.sqlite.database import (
    open_database,
)
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from app.interfaces.http import app
from test.regression_support import (
    Server,
    client,
    install_test_model_config,
    patch_default_database,
    temporary_root,
)

EVIDENCE = temporary_root("structured-confirmation-http")
SYSTEM = SystemMessage(role="system", content="系统提示词", tools_added=[], timestamp=100)
USAGE = Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2)
OPERATION_FIELDS = {
    "session_id", "operation_id", "business_kind", "proposal_id", "display_entry_id",
    "confirmation_entry_id", "status", "result", "error", "created_at", "updated_at",
}
PENDING_FIELDS = {
    "business_kind", "proposal_id", "status", "request_entry_id", "source_entry_id",
    "display_entry_id", "confirmation_entry_id",
}

PROFILE = {
    "goal": "增肌", "experience": "新手", "environment": "健身房", "availability": "每周四次",
    "health_notes": None, "movement_restrictions": None,
    "unavailable_equipment": [], "forbidden_exercise_ids": [],
}
WORKOUT = {
    "exercises": [{
        "exercise_id": None, "name": "俯卧撑", "load_convention": None,
        "sets": [{"reps": 10, "weight_kg": None, "duration_seconds": None}],
    }],
    "notes": None,
}
PLAN = {
    "repeat": True, "days": [
        {"kind": "training", "focus": "推", "exercises": [], "notes": None},
        {"kind": "rest", "focus": None, "exercises": [], "notes": None},
    ],
    "notes": None, "suggested_fields": [],
}


def new_id() -> str:
    return str(uuid4())


def now_ms() -> int:
    return time_ns() // 1_000_000


def request_body(session_unused, kind, ids, operation_id=None, **overrides):
    return {
        "operation_id": operation_id or new_id(),
        "business_kind": kind,
        "proposal_id": overrides.get("proposal", ids["proposal"]),
        "display_entry_id": overrides.get("display", ids["display"]),
    }


class Live:
    # 通过真实 Server 的主事件循环驱动服务；确认接口本身不调用模型。
    def __init__(self, server: Server) -> None:
        self.http = client(server.base_url)
        self.loop = app.state.loop
        self.service: SessionService = app.state.session_service
        self.business: BusinessService = app.state.business
        self.confirmations: ConfirmationService = app.state.confirmations
        self.repository = self.business._repository

    def call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def reset(self) -> None:
        # 将上一段遗留的 running 运行标记中断，解除全局运行占用。
        self.call(self.service.recover_interrupted())

    def session(self) -> str:
        async def create():
            created, session = await self.service.create_session_result(new_id(), "确认 HTTP 会话")
            assert created
            return session.id

        return self.call(create())

    def context(self, session_id, request, source):
        return BusinessContext(
            timezone="Asia/Shanghai", business_date=business_date(now_ms()),
            session_id=session_id, run_id="", request_entry_id=request, source_entry_id=source,
        )

    async def turn_async(self, session_id, text, *, finish=True):
        outcome = await self.service.accept_send(
            SendCommand(operation_id=new_id(), session_id=session_id, request=SendRequest(text=text)),
            system_message=SYSTEM,
        )
        if finish:
            await self.service.finish_run(session_id, outcome.run.id, "completed")
        return outcome.run

    def turn(self, session_id, text, *, finish=True):
        return self.call(self.turn_async(session_id, text, finish=finish))

    async def append_async(self, session_id, run_id, parent_id, message) -> str:
        node_id = new_id()
        await self.service.append_entry(SessionMessageEntry(
            session_id=session_id, id=node_id, parent_id=parent_id, run_id=run_id,
            type="message", messages=[message], created_at=now_ms(),
        ))
        return node_id

    def append(self, session_id, run_id, parent_id, message) -> str:
        return self.call(self.append_async(session_id, run_id, parent_id, message))

    def assistant(self, call_id, name, arguments):
        return AssistantMessage(
            role="assistant",
            content=[ToolCall(type="toolCall", id=call_id, name=name, arguments=arguments)],
            api="openai-completions", provider="example", model="model-1",
            usage=USAGE, stop_reason="toolUse", timestamp=now_ms(),
        )

    async def stage_async(self, session_id, kind, arguments, prepare, bind, tool_name) -> dict:
        run = await self.turn_async(session_id, "请整理待确认内容", finish=False)
        call_id = "call-" + uuid4().hex[:8]
        source = await self.append_async(
            session_id, run.id, run.request_entry_id,
            self.assistant(call_id, tool_name, arguments),
        )
        context = self.context(session_id, run.request_entry_id, source)
        proposal = await prepare(context, _proposal_args(kind, arguments))
        display = await self.append_async(session_id, run.id, source, ToolResultMessage(
            role="toolResult", tool_call_id=call_id, tool_name=tool_name,
            content=[TextContent(type="text", text=proposal.model_dump_json())],
            is_error=False, timestamp=now_ms(),
        ))
        await bind(proposal.proposal_id, display)
        await self.service.finish_run(session_id, run.id, "completed")
        return {
            "session": session_id, "request": run.request_entry_id, "source": source,
            "display": display, "proposal": proposal.proposal_id,
        }

    def stage(self, session_id, kind, arguments, prepare, bind, tool_name) -> dict:
        return self.call(
            self.stage_async(session_id, kind, arguments, prepare, bind, tool_name)
        )

    def stage_profile(self, session_id, base_version=None):
        if base_version is None:
            base_version = self.http.get("/api/profile").json()["version"]
        return self.stage(
            session_id, "profile",
            {"profile_id": 1, "base_profile_version": base_version, "payload": PROFILE},
            self.business.prepare_profile_update, self.business.bind_display_entry,
            "prepare_profile_update",
        )

    def stage_workout(self, session_id, day, base_id=None, base_version=None, payload=None):
        return self.stage(
            session_id, "workout",
            {"performed_on": day, "base_workout_id": base_id, "base_workout_version": base_version,
             "payload": payload or WORKOUT},
            self.business.prepare_workout, self.business.bind_workout_display_entry,
            "prepare_workout",
        )

    def stage_plan(self, session_id, base_version=1):
        return self.stage(
            session_id, "plan",
            {"base_profile_version": base_version, "base_plan_id": None, "payload": PLAN},
            self.business.prepare_plan_import, self.business.bind_plan_display_entry,
            "prepare_plan_import",
        )

    def confirm(self, session_id, kind, ids, *, operation_id=None, **overrides):
        body = request_body(session_id, kind, ids, operation_id, **overrides)
        return self.http.post(f"/api/sessions/{session_id}/confirmations", json=body)

    def get_op(self, session_id, operation_id):
        return self.http.get(f"/api/sessions/{session_id}/confirmations/{operation_id}")

    def count(self, table) -> int:
        async def read():
            async def operation(connection):
                cursor = await connection.execute(f"SELECT COUNT(*) FROM {table}", ())
                row = await cursor.fetchone()
                await cursor.close()
                return int(row[0])

            return await app.state.database.read(operation)

        return self.call(read())


def _proposal_args(kind, arguments):
    if kind == "profile":
        return ProfileProposalArguments.model_validate(arguments)
    if kind == "workout":
        from app.domain.business.models import WorkoutProposalArguments

        return WorkoutProposalArguments.model_validate(arguments)
    from app.domain.business.models import PlanImportArguments

    return PlanImportArguments.model_validate(arguments)


def check_saved_get_and_history(live: Live) -> dict:
    session = live.session()
    result = {}

    def confirm_and_assert(kind, ids):
        response = live.confirm(session, kind, ids)
        assert response.status_code == 200, (kind, response.text)
        assert response.headers["cache-control"] == "no-store"
        operation = response.json()
        assert set(operation) == OPERATION_FIELDS, operation
        assert operation["status"] == "saved"
        assert operation["error"] is None and operation["result"] is not None
        assert operation["proposal_id"] == ids["proposal"]
        fetched = live.get_op(session, operation["operation_id"])
        assert fetched.status_code == 200 and fetched.json() == operation, fetched.text
        entry = live.call(live.service.get_entry(session, operation["confirmation_entry_id"]))
        assert entry.run_id is None and entry.confirmation is not None
        result[kind] = operation["operation_id"]

    # 画像先确认建档（版本 1），计划再以版本 1 为依据准备并确认；训练记录独立。
    profile = live.stage_profile(session)
    confirm_and_assert("profile", profile)
    saved_version = live.http.get("/api/profile").json()["version"]
    plan = live.stage_plan(session, base_version=saved_version)
    confirm_and_assert("plan", plan)
    workout = live.stage_workout(session, business_date(now_ms()))
    confirm_and_assert("workout", workout)

    history = live.http.get(f"/api/sessions/{session}/history")
    assert history.status_code == 200, history.text
    body = history.json()
    assert set(body) == {
        "session", "entries", "runs", "steering", "confirmations", "pending_proposals",
    }, set(body)
    assert len(body["confirmations"]) == 3
    assert body["pending_proposals"] == []
    assert {item["business_kind"] for item in body["confirmations"]} == {"profile", "plan", "workout"}
    return {"saved": sorted(result)}


def check_workout_update_and_failed(live: Live) -> dict:
    session = live.session()
    day = "2020-01-01"
    first = live.stage_workout(session, day)
    saved_first = live.confirm(session, "workout", first)
    assert saved_first.status_code == 200 and saved_first.json()["result"]["version"] == 1

    record = live.call(live.repository.get_workout_by_date(day))
    updated = json.loads(json.dumps(WORKOUT))
    updated["notes"] = "肩部尚可"
    second = live.stage_workout(
        session, day, base_id=record.id, base_version=record.version, payload=updated,
    )
    saved_second = live.confirm(session, "workout", second)
    assert saved_second.status_code == 200, saved_second.text
    assert saved_second.json()["result"]["version"] == 2
    assert saved_second.json()["result"]["content"]["notes"] == "肩部尚可"

    # 失败：以当前画像版本为基准准备，随后由其他路径推进版本，确认时命中版本冲突。
    basis = live.http.get("/api/profile").json()["version"] or 0
    profile = live.stage_profile(session, base_version=basis)

    async def bump():
        async with live.repository.transaction():
            await live.repository.save_profile(
                ProfileContent.model_validate(PROFILE), basis + 5, now_ms(),
            )

    live.call(bump())
    failure = live.confirm(session, "profile", profile)
    assert failure.status_code == 409, failure.text
    detail = failure.json()["detail"]
    assert detail["code"] == "profile_version_conflict", detail
    assert "errors" not in detail

    # 另一新键提交同一无效目标仍返回同一业务错误。
    retry = live.confirm(session, "profile", profile)
    assert retry.status_code == 409 and retry.json()["detail"]["code"] == "profile_version_conflict"
    return {"versions": [1, 2], "failed": detail["code"]}


def check_request_validation(live: Live) -> dict:
    session = live.session()
    profile = live.stage_profile(session)
    base = request_body(session, "profile", profile)

    def post(body):
        return live.http.post(f"/api/sessions/{session}/confirmations", json=body)

    extra = post({**base, "confirmation": {"text": "确认"}})
    assert extra.status_code == 422 and extra.json()["detail"]["code"] == "invalid_request", extra.text
    assert post({**base, "operation_id": "not-a-uuid"}).status_code == 422
    assert post({**base, "business_kind": "goal"}).status_code == 422
    missing = dict(base)
    del missing["proposal_id"]
    assert post(missing).status_code == 422
    query = live.http.get(f"/api/sessions/{session}/confirmations/{base['operation_id']}?extra=1")
    assert query.status_code == 422 and query.json()["detail"]["code"] == "invalid_request"
    return {"rejected": ["extra", "uuid", "enum", "missing", "query"]}


def check_chat_entries_reject_authorization(live: Live) -> dict:
    session = live.session()
    run = live.http.post("/api/agent/run", json={
        "session_id": session, "operation_id": new_id(), "request": "确认保存画像",
        "confirmation": {"business_kind": "profile", "proposal_id": new_id(), "display_entry_id": new_id()},
    })
    assert run.status_code == 422 and run.json()["detail"]["code"] == "invalid_request", run.text
    edit = live.http.post("/api/agent/edit", json={
        "session_id": session, "operation_id": new_id(), "target_entry_id": new_id(),
        "request": "改写", "business_kind": "profile",
    })
    assert edit.status_code == 422, edit.text
    steering = live.http.post(f"/api/agent/runs/{new_id()}/steering", json={
        "session_id": session, "operation_id": new_id(), "message": "确认", "proposal_id": new_id(),
    })
    assert steering.status_code == 422, steering.text
    return {"run": run.status_code, "edit": edit.status_code, "steering": steering.status_code}


def check_occupancy_refuses_without_registering(live: Live) -> dict:
    session = live.session()
    profile = live.stage_profile(session)
    busy = live.turn(session, "占用运行中", finish=False)
    before = live.count("save_confirmation_operations")
    response = live.confirm(session, "profile", profile)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "run_busy"
    assert live.count("save_confirmation_operations") == before
    snapshot = live.call(live.repository.get_snapshot(profile["proposal"]))
    assert snapshot.status == "pending" and snapshot.confirmation_entry_id is None
    live.call(live.service.finish_run(session, busy.id, "completed"))
    after = live.confirm(session, "profile", profile)
    assert after.status_code == 200, after.text
    assert live.count("save_confirmation_operations") == before + 1
    return {"busy": "run_busy", "registered_after": True}


def check_idempotency_conflict_and_expired(live: Live) -> dict:
    session = live.session()
    profile = live.stage_profile(session)
    operation_id = new_id()
    first = live.confirm(session, "profile", profile, operation_id=operation_id)
    assert first.status_code == 200
    saved = first.json()
    again = live.confirm(session, "profile", profile, operation_id=operation_id)
    assert again.status_code == 200 and again.json()["operation_id"] == operation_id
    assert again.json()["result"] == saved["result"]
    diff = live.confirm(session, "profile", profile, operation_id=operation_id, proposal=new_id())
    assert diff.status_code == 409, diff.text
    detail = diff.json()["detail"]
    assert detail["code"] == "operation_conflict" and "reason" not in detail

    # 跨入口失效编号：聊天发送占用编号后编辑令其失效，用同编号提交确认 → operation_expired。
    chat_operation_id = new_id()
    sent = live.call(live.service.accept_send(
        SendCommand(operation_id=chat_operation_id, session_id=session, request=SendRequest(text="旧操作")),
        system_message=SYSTEM,
    ))
    live.call(live.service.finish_run(session, sent.run.id, "completed"))
    live.call(live.service.accept_edit(
        EditCommand(operation_id=new_id(), session_id=session,
                    request=EditRequest(target_entry_id=sent.run.request_entry_id, text="改写旧操作")),
    ))
    expired = live.confirm(session, "profile", profile, operation_id=chat_operation_id)
    assert expired.status_code == 409, expired.text
    expired_detail = expired.json()["detail"]
    assert expired_detail["code"] == "operation_conflict"
    assert expired_detail["reason"] == "operation_expired", expired_detail
    return {"reuse": True, "conflict": detail["code"], "expired": expired_detail["reason"]}


def check_regenerate_rejects_confirmation(live: Live) -> dict:
    session = live.session()
    profile = live.stage_profile(session)
    saved = live.confirm(session, "profile", profile).json()
    # 结构化确认节点的模型重新生成请求被拒绝；查询只读不触发保存。
    regenerate = live.http.post("/api/agent/regenerate", json={
        "session_id": session, "operation_id": new_id(),
        "target_entry_id": saved["confirmation_entry_id"],
    })
    assert regenerate.status_code == 409, regenerate.text
    assert regenerate.json()["detail"]["code"] == "invalid_target_entry"
    # 确认结果查询保持原对象。
    fetched = live.get_op(session, saved["operation_id"])
    assert fetched.status_code == 200 and fetched.json()["status"] == "saved"
    return {"regenerate": regenerate.json()["detail"]["code"]}


def check_history_filters_and_pending(live: Live) -> dict:
    session = live.session()
    profile = live.stage_profile(session)
    workout = live.stage_workout(session, "2020-02-02")
    saved = live.confirm(session, "profile", profile).json()
    history = live.http.get(f"/api/sessions/{session}/history").json()
    assert [item["operation_id"] for item in history["confirmations"]] == [saved["operation_id"]]
    assert [item["proposal_id"] for item in history["pending_proposals"]] == [workout["proposal"]]
    pending = history["pending_proposals"][0]
    assert set(pending) == PENDING_FIELDS, pending
    assert pending["status"] == "pending" and pending["business_kind"] == "workout"
    entry = next(e for e in history["entries"] if e["entry_id"] == saved["confirmation_entry_id"])
    assert entry["run_id"] is None
    return {"confirmations": 1, "pending": pending["business_kind"]}


def check_security_and_no_store(live: Live) -> dict:
    session = live.session()
    profile = live.stage_profile(session)
    base_url = live.http.base_url
    forbidden = httpx.post(
        f"{base_url}/api/sessions/{session}/confirmations",
        json=request_body(session, "profile", profile),
        headers={"Host": "127.0.0.1:8000", "Origin": "http://evil.example.com"},
        timeout=30, trust_env=False,
    )
    assert forbidden.status_code == 403, forbidden.text
    assert forbidden.json()["detail"]["code"] == "origin_forbidden"
    ok = live.confirm(session, "profile", profile)
    assert ok.status_code == 200 and ok.headers["cache-control"] == "no-store"
    fetched = live.get_op(session, ok.json()["operation_id"])
    assert fetched.headers["cache-control"] == "no-store"
    return {"origin_forbidden": 403, "no_store": ok.headers["cache-control"]}


def _seed_processing_operation(path: Path) -> tuple[str, str, str]:
    # 独立库预置会话、待确认快照及未提交的处理中操作，供 lifespan 对账验证。
    async def main():
        database = await open_database(path)
        repository = SqliteBusinessRepository(database)
        service = SessionService(SqliteSessionRepository(database))
        business = BusinessService(repository, Catalog.load(), service, ReplacementCoordinator())
        service.attach_snapshots(business)
        async with repository.transaction():
            await repository.replace_exercises(Catalog.load().all())
        created, session = await service.create_session_result(new_id(), "恢复会话")
        assert created
        session_id = session.id
        outcome = await service.accept_send(
            SendCommand(operation_id=new_id(), session_id=session_id, request=SendRequest(text="整理画像")),
            system_message=SYSTEM,
        )
        run = outcome.run
        call_id = "call-seed"
        source_id = new_id()
        await service.append_entry(SessionMessageEntry(
            session_id=session_id, id=source_id, parent_id=run.request_entry_id, run_id=run.id,
            type="message", messages=[AssistantMessage(
                role="assistant",
                content=[ToolCall(type="toolCall", id=call_id, name="prepare_profile_update",
                                  arguments={"profile_id": 1, "base_profile_version": None, "payload": PROFILE})],
                api="openai-completions", provider="e", model="m", usage=USAGE,
                stop_reason="toolUse", timestamp=now_ms(),
            )], created_at=now_ms(),
        ))
        context = BusinessContext(
            timezone="Asia/Shanghai", business_date=business_date(now_ms()),
            session_id=session_id, run_id="", request_entry_id=run.request_entry_id,
            source_entry_id=source_id,
        )
        proposal = await business.prepare_profile_update(
            context, ProfileProposalArguments.model_validate(
                {"profile_id": 1, "base_profile_version": None, "payload": PROFILE}
            ),
        )
        display_id = new_id()
        await service.append_entry(SessionMessageEntry(
            session_id=session_id, id=display_id, parent_id=source_id, run_id=run.id,
            type="message", messages=[ToolResultMessage(
                role="toolResult", tool_call_id=call_id, tool_name="prepare_profile_update",
                content=[TextContent(type="text", text=proposal.model_dump_json())],
                is_error=False, timestamp=now_ms(),
            )], created_at=now_ms(),
        ))
        await business.bind_display_entry(proposal.proposal_id, display_id)
        await service.finish_run(session_id, run.id, "completed")
        operation_id = new_id()
        async with repository.transaction():
            entry = await service.append_confirmation_entry(session_id, SaveConfirmation(
                business_kind="profile", proposal_id=proposal.proposal_id, display_entry_id=display_id,
            ))
            await repository.begin_save(proposal.proposal_id, entry.id)
            await repository.insert_confirmation_operation(SaveConfirmationOperation(
                session_id=session_id, operation_id=operation_id, business_kind="profile",
                proposal_id=proposal.proposal_id, display_entry_id=display_id,
                confirmation_entry_id=entry.id, status="processing", result=None, error=None,
                created_at=now_ms(), updated_at=now_ms(),
            ))
        await database.close()
        return session_id, operation_id, proposal.proposal_id

    return asyncio.run(main())


def check_lifespan_recovery(root: Path) -> dict:
    path = root / "recovery-live.db"
    session_id, operation_id, proposal_id = _seed_processing_operation(path)
    import app.infrastructure.persistence.sqlite.database as db_module

    original = db_module.default_database_path
    db_module.default_database_path = lambda: path
    try:
        with Server(app) as server:
            http = client(server.base_url)
            fetched = http.get(f"/api/sessions/{session_id}/confirmations/{operation_id}")
            assert fetched.status_code == 200, fetched.text
            operation = fetched.json()
            assert operation["status"] == "failed", operation
            assert operation["error"]["code"] == "confirmation_interrupted", operation["error"]

            async def snapshot_status():
                database = await open_database(path)
                repository = SqliteBusinessRepository(database)
                snapshot = await repository.get_snapshot(proposal_id)
                await database.close()
                return snapshot.status if snapshot else None

            assert asyncio.run(snapshot_status()) == "pending"
        return {"recovered": operation["status"], "error": operation["error"]["code"]}
    finally:
        db_module.default_database_path = original


def main() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    evidence: dict = {}
    with TemporaryDirectory(dir=EVIDENCE, ignore_cleanup_errors=True) as directory:
        root = Path(directory)
        patch_default_database("structured-confirmation-http-live")
        # 确认接口不读取模型配置；仅让 run/edit/regenerate 通过 load_model_config 检查。
        try:
            install_test_model_config()
        except RuntimeError:
            pass
        with Server(app) as server:
            live = Live(server)
            # 全局单一运行占用跨场景共享：每段前把上一段遗留的 running 运行标记中断，保持隔离。
            for key, check in (
                ("saved_get_history", check_saved_get_and_history),
                ("workout_update_failed", check_workout_update_and_failed),
                ("request_validation", check_request_validation),
                ("chat_rejects_authorization", check_chat_entries_reject_authorization),
                ("occupancy", check_occupancy_refuses_without_registering),
                ("idempotency_conflict_expired", check_idempotency_conflict_and_expired),
                ("regenerate_rejects_confirmation", check_regenerate_rejects_confirmation),
                ("history_filter_pending", check_history_filters_and_pending),
                ("security_no_store", check_security_and_no_store),
            ):
                live.reset()
                evidence[key] = check(live)
        evidence["lifespan_recovery"] = check_lifespan_recovery(root)
    (EVIDENCE / "structured-confirmation-http.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
    )
    print("PASS: 结构化确认 HTTP —— 三类保存与查询、严格四字段与拒绝、聊天入口注入被拒、"
          "占用零登记、幂等冲突与失效、重新生成拒绝确认节点、历史两数组与无 run、安全头、lifespan 对账")


if __name__ == "__main__":
    main()
