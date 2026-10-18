import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from uuid import uuid4

from app.ai.messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
)
from app.application.business.profile import (
    BUSINESS_TIMEZONE,
    business_date,
)
from app.application.business.service import BusinessService
from app.application.session.service import SessionService
from app.domain.business.models import (
    BusinessContext,
    PlanImportArguments,
    ProfileContent,
    ProfileProposalArguments,
    SaveConfirmationOperation,
    SaveConfirmationRequest,
)
from app.domain.session.models import SendCommand, SendRequest, SessionMessageEntry
from app.infrastructure.persistence.sqlite.business_repository import (
    SqliteBusinessRepository,
)
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from app.interfaces.http import app
from test.regression_support import (
    Server,
    client,
    patch_default_database,
    temporary_root,
)

EVIDENCE = temporary_root("confirmation-serialization")
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


def new_id() -> str:
    return str(uuid4())


def now_ms() -> int:
    return time_ns() // 1_000_000


def plan_import_payload(exercise=None) -> dict:
    exercises = []
    if exercise is not None:
        exercises = [{
            "exercise_id": exercise.id, "name": exercise.name, "sets": 3, "reps": 8,
            "duration_seconds": None, "weight_kg": None, "load_convention": None,
            "rest_seconds": None,
        }]
    return {
        "repeat": True,
        "days": [{"kind": "training", "focus": "推", "exercises": exercises, "notes": None}],
        "notes": None,
        "suggested_fields": [],
    }


class Live:
    # 通过真实 Server 的主事件循环驱动服务；确认 HTTP 本身不调用模型。
    def __init__(self, server: Server) -> None:
        self.http = client(server.base_url)
        self.loop = app.state.loop
        self.service: SessionService = app.state.session_service
        self.business: BusinessService = app.state.business
        self.confirmations = app.state.confirmations
        self.repository = app.state.business_repository

    def call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def reset(self) -> None:
        # 隔离各段：把上一段遗留的 running 运行标记中断，解除全局执行占用。
        self.call(self.service.runs.recover_interrupted())

    def session(self) -> str:
        async def create():
            created, session = await self.service.create_session_result(new_id(), "序列化会话")
            assert created
            return session.id

        return self.call(create())

    def context(self, session_id, request, source) -> BusinessContext:
        return BusinessContext(
            timezone=BUSINESS_TIMEZONE, business_date=business_date(now_ms()),
            session_id=session_id, run_id="", request_entry_id=request, source_entry_id=source,
        )

    async def turn_async(self, session_id, text, *, finish=True):
        outcome = await self.service.accept_send(
            SendCommand(operation_id=new_id(), session_id=session_id, request=SendRequest(text=text)),
            system_message=SYSTEM,
        )
        if finish:
            await self.service.runs.finish_run(session_id, outcome.run.id, "completed")
        return outcome.run

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
        source = new_id()
        await self.service.entries.append_entry(SessionMessageEntry(
            session_id=session_id, id=source, parent_id=run.request_entry_id, run_id=run.id,
            type="message", messages=[self.assistant(call_id, tool_name, arguments)],
            created_at=now_ms(),
        ))
        context = self.context(session_id, run.request_entry_id, source)
        proposal = await prepare(context, _proposal_args(kind, arguments))
        display = new_id()
        await self.service.entries.append_entry(SessionMessageEntry(
            session_id=session_id, id=display, parent_id=source, run_id=run.id,
            type="message", messages=[ToolResultMessage(
                role="toolResult", tool_call_id=call_id, tool_name=tool_name,
                content=[TextContent(type="text", text=proposal.model_dump_json())],
                is_error=False, timestamp=now_ms(),
            )], created_at=now_ms(),
        ))
        await bind(proposal.proposal_id, display)
        await self.service.runs.finish_run(session_id, run.id, "completed")
        return {
            "session": session_id, "request": run.request_entry_id, "source": source,
            "display": display, "proposal": proposal.proposal_id,
        }

    def stage(self, session_id, kind, arguments, prepare, bind, tool_name) -> dict:
        return self.call(self.stage_async(session_id, kind, arguments, prepare, bind, tool_name))

    def stage_profile(self, session_id) -> dict:
        base = self.http.get("/api/profile").json()["version"]
        return self.stage(
            session_id, "profile",
            {"profile_id": 1, "base_profile_version": base, "payload": PROFILE},
            self.business.profile.prepare_profile_update, self.business.profile.bind_display_entry,
            "prepare_profile_update",
        )

    def stage_plan_import(self, session_id, base_version, payload, *, base_plan_id=None) -> dict:
        return self.stage(
            session_id, "plan",
            {"base_profile_version": base_version, "base_plan_id": base_plan_id, "payload": payload},
            self.business.plans.prepare_plan_import, self.business.plans.bind_plan_display_entry,
            "prepare_plan_import",
        )

    def confirm(self, session_id, kind, ids, *, operation_id=None, proposal=None, display=None):
        body = {
            "operation_id": operation_id or new_id(),
            "business_kind": kind,
            "proposal_id": proposal or ids["proposal"],
            "display_entry_id": display or ids["display"],
        }
        return self.http.post(f"/api/sessions/{session_id}/confirmations", json=body), body

    def get_op(self, session_id, operation_id):
        return self.http.get(f"/api/sessions/{session_id}/confirmations/{operation_id}")

    def history(self, session_id):
        return self.http.get(f"/api/sessions/{session_id}/history")


def _proposal_args(kind, arguments):
    if kind == "profile":
        return ProfileProposalArguments.model_validate(arguments)
    return PlanImportArguments.model_validate(arguments)


def assert_error_shape(error: dict) -> None:
    # 无错误明细时省略 errors；有明细时为严格 path/message 数组（§7.3）。
    assert set(error) >= {"code", "message"}, error
    assert set(error) <= {"code", "message", "errors"}, error
    if "errors" in error:
        assert isinstance(error["errors"], list) and error["errors"], error["errors"]
        for item in error["errors"]:
            assert set(item) == {"path", "message"}, item
            assert isinstance(item["path"], str) and isinstance(item["message"], str), item


def check_saved_and_required_nulls(live: Live) -> dict:
    session = live.session()
    profile = live.stage_profile(session)
    response, body = live.confirm(session, "profile", profile)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    operation = response.json()
    # saved：全部必填字段存在，result 为完整固定结果，error 显式 null。
    assert set(operation) == OPERATION_FIELDS, operation
    assert operation["status"] == "saved"
    assert operation["error"] is None
    assert operation["result"] is not None
    # 业务结果中的合法 null 必须保留，不能被统一 exclude_none 剔除。
    assert operation["result"]["content"]["health_notes"] is None
    assert "health_notes" in operation["result"]["content"]
    assert operation["result"]["content"]["movement_restrictions"] is None

    fetched = live.get_op(session, operation["operation_id"])
    assert fetched.status_code == 200 and fetched.json() == operation, fetched.text
    assert fetched.headers["cache-control"] == "no-store"

    history = live.history(session).json()
    listed = next(item for item in history["confirmations"] if item["operation_id"] == operation["operation_id"])
    assert listed == operation, listed
    return {"saved_keys": sorted(operation), "result_nulls": True}


def check_version_conflict_omits_errors(live: Live) -> dict:
    session = live.session()
    basis = live.http.get("/api/profile").json()["version"]
    profile = live.stage_profile(session)

    async def bump():
        async with live.repository.transaction():
            await live.repository.save_profile(
                ProfileContent.model_validate(PROFILE), (basis or 0) + 5, now_ms(),
            )

    live.call(bump())
    response, body = live.confirm(session, "profile", profile)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    # POST 错误结构保持原契约：非内容校验失败只携带 code / message。
    assert set(detail) == {"code", "message"}, detail
    assert detail["code"] == "profile_version_conflict"

    # 失败操作已持久化：GET 公开对象保留 result=null 且省略 error.errors。
    fetched = live.get_op(session, body["operation_id"])
    assert fetched.status_code == 200, fetched.text
    operation = fetched.json()
    assert set(operation) == OPERATION_FIELDS, operation
    assert operation["status"] == "failed"
    assert operation["result"] is None
    assert_error_shape(operation["error"])
    assert "errors" not in operation["error"], operation["error"]

    # 既有 errors=null 的失败记录：history 同样省略该字段。
    listed = next(
        item for item in live.history(session).json()["confirmations"]
        if item["operation_id"] == body["operation_id"]
    )
    assert listed == operation, listed
    return {"detail": detail, "error": operation["error"]}


def check_field_validation_details(live: Live) -> dict:
    session = live.session()
    version = live.http.get("/api/profile").json()["version"]
    assert version is not None, "字段校验失败前置需要已保存画像"
    exercise = live.business._catalog.all()[0]
    plan = live.stage_plan_import(session, version, plan_import_payload(exercise))

    # 保存提交前该快照依据的画像内容变化（同版本、新增禁用动作）：提交时重新校验命中字段错误。
    async def forbid():
        async with live.repository.transaction():
            await live.repository.save_profile(
                ProfileContent.model_validate({**PROFILE, "forbidden_exercise_ids": [exercise.id]}),
                version, now_ms(),
            )

    live.call(forbid())
    response, body = live.confirm(session, "plan", plan)
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_business_payload", detail
    assert set(detail) == {"code", "message", "errors"}, detail
    assert detail["errors"] == [{
        "path": "/payload/days/0/exercises/0/exercise_id",
        "message": "动作不符合已保存画像的动作或器械限制。",
    }], detail

    fetched = live.get_op(session, body["operation_id"])
    assert fetched.status_code == 200, fetched.text
    operation = fetched.json()
    assert operation["status"] == "failed"
    assert operation["result"] is None
    assert_error_shape(operation["error"])
    assert operation["error"]["errors"] == detail["errors"], operation["error"]
    listed = next(
        item for item in live.history(session).json()["confirmations"]
        if item["operation_id"] == body["operation_id"]
    )
    assert listed["error"] == operation["error"], listed
    return {"errors": operation["error"]["errors"]}


def check_processing_required_nulls(live: Live) -> dict:
    session = live.session()
    profile = live.stage_profile(session)
    # 受理后不执行保存：留下真实处理中操作，核对 GET / history 的必填 null。
    operation, accepted = live.call(
        live.confirmations._accept(
            session,
            SaveConfirmationRequest(
                operation_id=new_id(), business_kind="profile",
                proposal_id=profile["proposal"], display_entry_id=profile["display"],
            ),
            now_ms(),
        )
    )
    assert accepted
    fetched = live.get_op(session, operation.operation_id)
    assert fetched.status_code == 200, fetched.text
    payload = fetched.json()
    assert set(payload) == OPERATION_FIELDS, payload
    assert payload["status"] == "processing"
    assert payload["result"] is None and payload["error"] is None
    listed = next(
        item for item in live.history(session).json()["confirmations"]
        if item["operation_id"] == operation.operation_id
    )
    assert listed == payload, listed
    return {"processing_nulls": True}


def seed_recovery_database(path: Path) -> tuple[str, str]:
    # 独立库预置真实会话、确认节点与未提交的处理中操作，供 lifespan 对账。
    async def main():
        database = await open_database(path)
        service = SessionService(SqliteSessionRepository(database))
        repository = SqliteBusinessRepository(database)
        try:
            created, session = await service.create_session_result(new_id(), "恢复会话")
            assert created
            session_id = session.id
            outcome = await service.accept_send(
                SendCommand(operation_id=new_id(), session_id=session_id,
                            request=SendRequest(text="确认保存画像")),
                system_message=SYSTEM,
            )
            run = outcome.run
            await service.runs.finish_run(session_id, run.id, "completed")
            # 请求用户节点即当前路径中的确认节点，供 history 过滤核对。
            entry_id = run.request_entry_id
            operation_id = new_id()
            async with repository.transaction():
                await repository.insert_confirmation_operation(SaveConfirmationOperation(
                    session_id=session_id, operation_id=operation_id, business_kind="profile",
                    proposal_id=new_id(), display_entry_id=new_id(),
                    confirmation_entry_id=entry_id, status="processing",
                    result=None, error=None, created_at=now_ms(), updated_at=now_ms(),
                ))
            return session_id, operation_id
        finally:
            await database.close()

    return asyncio.run(main())


def check_interrupted_recovery_omits_errors(root: Path) -> dict:
    path = root / "recovery-live.db"
    session_id, operation_id = seed_recovery_database(path)
    import app.infrastructure.persistence.sqlite.database as db_module

    original = db_module.default_database_path
    db_module.default_database_path = lambda: path
    try:
        with Server(app) as server:
            http = client(server.base_url)
            fetched = http.get(f"/api/sessions/{session_id}/confirmations/{operation_id}")
            assert fetched.status_code == 200, fetched.text
            assert fetched.headers["cache-control"] == "no-store"
            operation = fetched.json()
            assert set(operation) == OPERATION_FIELDS, operation
            assert operation["status"] == "failed", operation
            assert operation["result"] is None
            assert_error_shape(operation["error"])
            assert operation["error"]["code"] == "confirmation_interrupted"
            assert "errors" not in operation["error"], operation["error"]
            listed = next(
                item for item in http.get(f"/api/sessions/{session_id}/history").json()["confirmations"]
                if item["operation_id"] == operation_id
            )
            assert listed == operation, listed
            return {"status": operation["status"], "error": operation["error"]}
    finally:
        db_module.default_database_path = original


def main() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    evidence: dict = {}
    with TemporaryDirectory(dir=EVIDENCE, ignore_cleanup_errors=True) as directory:
        root = Path(directory)
        patch_default_database("confirmation-serialization-live")
        with Server(app) as server:
            live = Live(server)
            for key, check in (
                ("saved_required_nulls", check_saved_and_required_nulls),
                ("version_conflict_omits_errors", check_version_conflict_omits_errors),
                ("field_validation_details", check_field_validation_details),
                ("processing_required_nulls", check_processing_required_nulls),
            ):
                live.reset()
                evidence[key] = check(live)
        evidence["interrupted_recovery_omits_errors"] = check_interrupted_recovery_omits_errors(root)
    (EVIDENCE / "confirmation-serialization.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
    )
    print(
        "PASS: 确认操作公开投影 —— saved/processing/failed 必填 null 保留、业务结果合法 null 保留、"
        "版本冲突与中断恢复省略 error.errors、字段校验失败输出严格 path/message、"
        "GET 与 history 一致、POST 错误结构不变"
    )


if __name__ == "__main__":
    main()
