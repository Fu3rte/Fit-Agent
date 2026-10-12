import asyncio
import json
import sqlite3
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

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
from app.application.business.service import (
    BUSINESS_TIMEZONE,
    BusinessService,
    business_date,
)
from app.application.session.service import SessionService
from app.domain.business.errors import BusinessError
from app.domain.business.models import (
    BusinessContext,
    PlanImportArguments,
    PlanProposalArguments,
    PlanSaveArguments,
    ProfileContent,
    ProfileProposalArguments,
    ProfileSaveArguments,
    SaveConfirmationOperation,
    SaveConfirmationRequest,
    WorkoutProposalArguments,
    WorkoutSaveArguments,
)
from app.domain.session.errors import SessionError
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
from app.infrastructure.persistence.sqlite.database import Database, open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from test.regression_support import temporary_root

EVIDENCE = temporary_root("structured-confirmation")
SYSTEM = SystemMessage(role="system", content="系统提示词", tools_added=[], timestamp=100)
USAGE = Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2)

PROFILE = {
    "goal": "增肌",
    "experience": "新手",
    "environment": "健身房",
    "availability": "每周四次",
    "health_notes": None,
    "movement_restrictions": None,
    "unavailable_equipment": [],
    "forbidden_exercise_ids": [],
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
    "exercises": [
        {
            "exercise_id": None,
            "name": "俯卧撑",
            "load_convention": None,
            "sets": [{"reps": 10, "weight_kg": None, "duration_seconds": None}],
        }
    ],
    "notes": None,
}


def new_id() -> str:
    return str(uuid4())


def now_ms() -> int:
    return time.time_ns() // 1_000_000


async def rejection(awaitable):
    try:
        await awaitable
    except (BusinessError, SessionError) as error:
        return error
    raise AssertionError("expected BusinessError")


async def count_rows(database: Database, table: str) -> int:
    async def read(connection):
        cursor = await connection.execute(f"SELECT COUNT(*) FROM {table}", ())
        row = await cursor.fetchone()
        await cursor.close()
        return int(row[0])

    return await database.read(read)


class Fixture:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.database: Database | None = None
        self.service: SessionService | None = None
        self.repository: SqliteBusinessRepository | None = None
        self.business: BusinessService | None = None
        self.confirmations: ConfirmationService | None = None
        self.catalog: Catalog | None = None
        self.replacements: ReplacementCoordinator | None = None

    async def open(self, *, recovered: bool = False) -> "Fixture":
        self.database = await open_database(self.path)
        self.repository = SqliteBusinessRepository(self.database)
        self.catalog = Catalog.load()
        service = SessionService(SqliteSessionRepository(self.database))
        if recovered:
            await service.recover_interrupted()
        self.replacements = ReplacementCoordinator()
        self.business = BusinessService(
            self.repository, self.catalog, service, self.replacements
        )
        service.attach_snapshots(self.business)
        self.service = service
        self.confirmations = ConfirmationService(self.repository, service, self.business)
        if recovered:
            await self.confirmations.recover_interrupted()
        return self

    async def seeded(self) -> "Fixture":
        await self.open()
        async with self.repository.transaction():
            await self.repository.replace_exercises(self.catalog.all())
        return self

    async def restart(self) -> "Fixture":
        await self.database.close()
        return await self.open(recovered=True)

    async def close(self) -> None:
        await self.database.close()

    async def session(self, title: str = "结构化确认会话") -> str:
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

    async def turn(self, session_id: str, text: str, *, finish: bool = True):
        outcome = await self.service.accept_send(
            SendCommand(operation_id=new_id(), session_id=session_id, request=SendRequest(text=text)),
            system_message=SYSTEM,
        )
        if finish:
            await self.service.finish_run(session_id, outcome.run.id, "completed")
        return outcome.run

    async def append(self, session_id: str, run_id: str, parent_id: str, message) -> str:
        node_id = new_id()
        await self.service.append_entry(
            SessionMessageEntry(
                session_id=session_id, id=node_id, parent_id=parent_id, run_id=run_id,
                type="message", messages=[message], created_at=now_ms(),
            )
        )
        return node_id

    def assistant(self, call_id: str, name: str, arguments: dict) -> AssistantMessage:
        return AssistantMessage(
            role="assistant",
            content=[ToolCall(type="toolCall", id=call_id, name=name, arguments=arguments)],
            api="openai-completions", provider="example", model="model-1",
            usage=USAGE, stop_reason="toolUse", timestamp=now_ms(),
        )

    async def stage(self, session_id: str, kind: str, arguments: dict, tool_name: str) -> dict:
        # 真实准备流程：用户请求 -> 助手工具调用来源 -> 准备工具 -> 结果节点 -> 绑定展示。
        run = await self.turn(session_id, "请整理待确认内容", finish=False)
        request_id = run.request_entry_id
        call_id = "call-" + uuid4().hex[:8]
        source_id = await self.append(
            session_id, run.id, request_id, self.assistant(call_id, tool_name, arguments)
        )
        context = self.context(session_id, request_id, source_id)
        if kind == "profile":
            proposal = await self.business.prepare_profile_update(
                context, ProfileProposalArguments.model_validate(arguments)
            )
        elif kind == "plan_generation":
            proposal = await self.business.prepare_plan(
                context, PlanProposalArguments.model_validate(arguments)
            )
        elif kind == "plan_import":
            proposal = await self.business.prepare_plan_import(
                context, PlanImportArguments.model_validate(arguments)
            )
        else:
            proposal = await self.business.prepare_workout(
                context, WorkoutProposalArguments.model_validate(arguments)
            )
        message = ToolResultMessage(
            role="toolResult",
            tool_call_id=call_id,
            tool_name=tool_name,
            content=[TextContent(type="text", text=proposal.model_dump_json())],
            is_error=False,
            timestamp=now_ms(),
        )
        display_id = await self.append(session_id, run.id, source_id, message)
        if kind == "profile":
            await self.business.bind_display_entry(proposal.proposal_id, display_id)
        elif kind.startswith("plan"):
            await self.business.bind_plan_display_entry(proposal.proposal_id, display_id)
        else:
            await self.business.bind_workout_display_entry(proposal.proposal_id, display_id)
        await self.service.finish_run(session_id, run.id, "completed")
        return {
            "session": session_id, "request": request_id, "source": source_id,
            "display": display_id, "proposal": proposal.proposal_id,
            "performed_on": getattr(proposal, "performed_on", None),
        }

    async def request(self, session_id: str, kind: str, ids: dict, *, operation_id=None, **overrides):
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


async def check_saves_and_ambiguity(root: Path) -> dict:
    fixture = await Fixture(root / "saves.db").seeded()
    try:
        session = await fixture.session()
        profile_ids = await fixture.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": None, "payload": PROFILE},
            "prepare_profile_update",
        )
        workout_ids = await fixture.stage(
            session, "workout",
            {"performed_on": business_date(now_ms()), "base_workout_id": None,
             "base_workout_version": None, "payload": WORKOUT},
            "prepare_workout",
        )

        # 两份内容待确认：普通“确认。”只产生普通用户节点，不形成授权。
        run = await fixture.turn(session, "确认。")
        before = await fixture.state()
        detail = await rejection(
            fixture.business.save_profile_update(
                fixture.context(session, run.request_entry_id, profile_ids["source"]),
                ProfileSaveArguments(
                    proposal_id=profile_ids["proposal"], display_entry_id=profile_ids["display"],
                    confirmation_entry_id=run.request_entry_id,
                ),
            )
        )
        assert detail.code == "profile_confirmation_invalid", detail.code
        assert await fixture.state() == before
        assert (await fixture.repository.get_snapshot(profile_ids["proposal"])).status == "pending"

        # 明确结构化授权保存画像：只保存画像，训练记录保持待确认。
        profile_op = await fixture.confirmations.confirm(
            session, await fixture.request(session, "profile", profile_ids)
        )
        assert profile_op.status == "saved", profile_op
        assert profile_op.result.version == 1
        assert profile_op.result.content.model_dump() == PROFILE
        assert (await fixture.repository.get_snapshot(profile_ids["proposal"])).status == "saved"
        assert (await fixture.repository.get_workout_snapshot(workout_ids["proposal"])).status == "pending"

        workout_op = await fixture.confirmations.confirm(
            session, await fixture.request(session, "workout", workout_ids)
        )
        assert workout_op.status == "saved"
        assert (await fixture.repository.get_workout_snapshot(workout_ids["proposal"])).status == "saved"

        state = await fixture.state()
        assert state["profile"] == 1 and state["profile_records"] == 1
        assert state["workouts"] == 1 and state["workout_records"] == 1
        assert state["operations"] == 2
        # 确认节点 run_id 为空、无运行、无助手工具调用节点。
        entry = await fixture.service.get_entry(session, profile_op.confirmation_entry_id)
        assert entry.run_id is None and entry.confirmation is not None
        runs = await fixture.service.list_runs(session)
        assert len(runs) == 3  # 两次准备运行 + 普通“确认。”发送运行
        return {
            "plain_text": detail.code,
            "profile": profile_op.result.model_dump(),
            "workout": workout_op.result.model_dump(),
            "state": state,
            "confirmation_run_id": entry.run_id,
        }
    finally:
        await fixture.close()


async def check_plan_and_workout_update(root: Path) -> dict:
    fixture = await Fixture(root / "plan-update.db").seeded()
    try:
        session = await fixture.session()
        profile_ids = await fixture.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": None, "payload": PROFILE},
            "prepare_profile_update",
        )
        assert (await fixture.confirmations.confirm(
            session, await fixture.request(session, "profile", profile_ids)
        )).status == "saved"

        plan_ids = await fixture.stage(
            session, "plan_import",
            {"base_profile_version": 1, "base_plan_id": None, "payload": PLAN},
            "prepare_plan_import",
        )
        plan_op = await fixture.confirmations.confirm(
            session, await fixture.request(session, "plan", plan_ids)
        )
        assert plan_op.status == "saved", plan_op
        assert plan_op.result.content.days[0].kind == "training"

        day = business_date(now_ms())
        workout_ids = await fixture.stage(
            session, "workout",
            {"performed_on": day, "base_workout_id": None, "base_workout_version": None,
             "payload": WORKOUT},
            "prepare_workout",
        )
        assert (await fixture.confirmations.confirm(
            session, await fixture.request(session, "workout", workout_ids)
        )).result.version == 1

        updated_payload = json.loads(json.dumps(WORKOUT))
        updated_payload["notes"] = "肩部尚可"
        record = await fixture.repository.get_workout_by_date(day)
        update_ids = await fixture.stage(
            session, "workout",
            {"performed_on": day, "base_workout_id": record.id,
             "base_workout_version": record.version, "payload": updated_payload},
            "prepare_workout",
        )
        update_op = await fixture.confirmations.confirm(
            session, await fixture.request(session, "workout", update_ids)
        )
        assert update_op.status == "saved", update_op
        assert update_op.result.version == 2 and update_op.result.content.notes == "肩部尚可"
        state = await fixture.state()
        assert state["plans"] == 1 and state["plan_records"] == 1
        assert state["workouts"] == 1 and state["workout_records"] == 2
        return {"plan": plan_op.result.model_dump(), "workout_update": update_op.result.model_dump()}
    finally:
        await fixture.close()


async def check_rejections(root: Path) -> dict:
    fixture = await Fixture(root / "rejections.db").seeded()
    try:
        session = await fixture.session()
        other_session = await fixture.session("另一会话")
        profile_ids = await fixture.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": None, "payload": PROFILE},
            "prepare_profile_update",
        )
        await fixture.stage(
            other_session, "profile",
            {"profile_id": 1, "base_profile_version": None, "payload": PROFILE},
            "prepare_profile_update",
        )
        baseline = await fixture.state()
        results = {}
        results["wrong_kind"] = (await rejection(fixture.confirmations.confirm(
            session, SaveConfirmationRequest(
                operation_id=new_id(), business_kind="plan",
                proposal_id=profile_ids["proposal"], display_entry_id=profile_ids["display"],
            )
        ))).code
        results["wrong_display"] = (await rejection(fixture.confirmations.confirm(
            session, await fixture.request(session, "profile", profile_ids, display=new_id())
        ))).code
        results["wrong_session"] = (await rejection(fixture.confirmations.confirm(
            other_session, await fixture.request(other_session, "profile", profile_ids)
        ))).code
        results["missing"] = (await rejection(fixture.confirmations.confirm(
            session, await fixture.request(session, "profile", profile_ids, proposal=new_id())
        ))).code
        assert await fixture.state() == baseline, await fixture.state()
        kept = await fixture.repository.get_snapshot(profile_ids["proposal"])
        assert kept.status == "pending" and kept.confirmation_entry_id is None

        # 失效快照：同会话同目标新快照令旧快照失效。
        replacement = await fixture.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": None, "payload": PROFILE},
            "prepare_profile_update",
        )
        assert (await fixture.repository.get_snapshot(profile_ids["proposal"])).status == "invalidated"
        results["invalidated"] = (await rejection(fixture.confirmations.confirm(
            session, await fixture.request(session, "profile", profile_ids)
        ))).code
        assert (await fixture.repository.get_snapshot(profile_ids["proposal"])).status == "invalidated"

        # 路径变化：编辑令展示节点离开当前路径，授权核验回滚，不留下确认节点或操作。
        edited = await fixture.service.accept_edit(
            EditCommand(operation_id=new_id(), session_id=session,
                        request=EditRequest(target_entry_id=replacement["request"], text="换个说法")),
        )
        await fixture.service.finish_run(session, edited.run.id, "completed")
        before_path = await fixture.state()
        results["path"] = (await rejection(fixture.confirmations.confirm(
            session, await fixture.request(session, "profile", replacement)
        ))).code
        assert await fixture.state() == before_path
        return {"rejections": results}
    finally:
        await fixture.close()


async def check_idempotency_and_conflict(root: Path) -> dict:
    fixture = await Fixture(root / "idempotency.db").seeded()
    try:
        session = await fixture.session()
        profile_ids = await fixture.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": None, "payload": PROFILE},
            "prepare_profile_update",
        )
        operation_id = new_id()
        request = await fixture.request(session, "profile", profile_ids, operation_id=operation_id)
        first = await fixture.confirmations.confirm(session, request)
        assert first.status == "saved"
        state = await fixture.state()

        # 同键同请求复用原操作。
        repeated = await fixture.confirmations.confirm(session, request)
        assert repeated.operation_id == operation_id and repeated.status == "saved"
        assert repeated.result.model_dump() == first.result.model_dump()
        assert repeated.confirmation_entry_id == first.confirmation_entry_id
        assert await fixture.state() == state

        # 同键异请求冲突。
        conflict = (await rejection(fixture.confirmations.confirm(
            session, SaveConfirmationRequest(
                operation_id=operation_id, business_kind="profile",
                proposal_id=new_id(), display_entry_id=profile_ids["display"],
            )
        ))).code
        assert conflict == "operation_conflict", conflict

        # 已保存快照重复确认：新操作 ID 关联原确认节点与固定结果，不新增消息或业务版本。
        messages_before = len(await fixture.service.list_entries(session))
        again = await fixture.confirmations.confirm(
            session, await fixture.request(session, "profile", profile_ids)
        )
        assert again.status == "saved"
        assert again.result.model_dump() == first.result.model_dump()
        assert again.confirmation_entry_id == first.confirmation_entry_id
        assert len(await fixture.service.list_entries(session)) == messages_before
        assert (await fixture.repository.get_profile()).version == 1

        # 跨入口 ID 冲突：确认操作 ID 与聊天操作 ID 互为占用则拒绝。
        chat_op_id = new_id()
        outcome = await fixture.service.accept_send(
            SendCommand(operation_id=chat_op_id, session_id=session,
                        request=SendRequest(text="普通消息")),
            system_message=SYSTEM,
        )
        await fixture.service.finish_run(session, outcome.run.id, "completed")
        chat_conflict = await rejection(
            fixture.service.accept_send(
                SendCommand(operation_id=again.operation_id, session_id=session,
                            request=SendRequest(text="普通消息")),
                system_message=SYSTEM,
            )
        )
        assert chat_conflict.__class__.__name__ == "OperationConflict", chat_conflict
        confirm_conflict = (await rejection(fixture.confirmations.confirm(
            session, SaveConfirmationRequest(
                operation_id=chat_op_id, business_kind="profile",
                proposal_id=profile_ids["proposal"], display_entry_id=profile_ids["display"],
            )
        ))).code
        assert confirm_conflict == "operation_conflict", confirm_conflict
        return {
            "first": first.model_dump(), "again": again.model_dump(), "conflict": conflict,
            "chat_conflict": chat_conflict.__class__.__name__, "confirm_conflict": confirm_conflict,
        }
    finally:
        await fixture.close()


async def check_failure_and_recovery(root: Path) -> dict:
    fixture = await Fixture(root / "recovery.db").seeded()
    try:
        session = await fixture.session()
        profile_ids = await fixture.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": None, "payload": PROFILE},
            "prepare_profile_update",
        )
        # 版本冲突：准备后由其他路径推进画像版本。
        async with fixture.repository.transaction():
            await fixture.repository.save_profile(ProfileContent.model_validate(PROFILE), 5, now_ms())
        failed_op = await fixture.confirmations.confirm(
            session, await fixture.request(session, "profile", profile_ids)
        )
        assert failed_op.status == "failed", failed_op
        assert failed_op.error.code == "profile_version_conflict"
        assert (await fixture.repository.get_snapshot(profile_ids["proposal"])).status == "conflicted"
        state = await fixture.state()

        # 失败操作重试返回原错误，不重新执行。
        retried = await fixture.confirmations.confirm(
            session, await fixture.request(session, "profile", profile_ids,
                                          operation_id=failed_op.operation_id)
        )
        assert retried.status == "failed" and retried.error.code == "profile_version_conflict"
        assert retried.updated_at == failed_op.updated_at
        assert await fixture.state() == state

        # 处理中记录中断恢复：未提交恢复 failed 并解绑快照。
        pending = await fixture.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": 5, "payload": PROFILE},
            "prepare_profile_update",
        )
        op_id = new_id()
        async with fixture.repository.transaction():
            entry = await fixture.service.append_confirmation_entry(
                session, SaveConfirmation(
                    business_kind="profile", proposal_id=pending["proposal"],
                    display_entry_id=pending["display"],
                )
            )
            await fixture.repository.begin_save(pending["proposal"], entry.id)
            await fixture.repository.insert_confirmation_operation(
                SaveConfirmationOperation(
                    session_id=session, operation_id=op_id, business_kind="profile",
                    proposal_id=pending["proposal"], display_entry_id=pending["display"],
                    confirmation_entry_id=entry.id, status="processing", result=None, error=None,
                    created_at=now_ms(), updated_at=now_ms(),
                )
            )
        before_restart = await fixture.state()
        assert before_restart["operations"] == state["operations"] + 1

        fixture = await fixture.restart()
        snapshot = await fixture.repository.get_snapshot(pending["proposal"])
        assert snapshot.status == "pending" and snapshot.confirmation_entry_id is None
        recovered = await fixture.confirmations.get(session, op_id)
        assert recovered.status == "failed" and recovered.error.code == "confirmation_interrupted"
        return {
            "failed": failed_op.error.model_dump(),
            "retry_same": retried.updated_at == failed_op.updated_at,
            "recovered_status": snapshot.status,
            "recovered_error": recovered.error.code,
        }
    finally:
        await fixture.close()


async def check_edit_and_delete(root: Path) -> dict:
    fixture = await Fixture(root / "edit-delete.db").seeded()
    try:
        session = await fixture.session()
        profile_ids = await fixture.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": None, "payload": PROFILE},
            "prepare_profile_update",
        )
        op = await fixture.confirmations.confirm(
            session, await fixture.request(session, "profile", profile_ids)
        )
        assert op.status == "saved"

        # 编辑确认用户节点：普通新节点不继承授权，已保存操作与固定结果保留。
        edited = await fixture.service.accept_edit(
            EditCommand(operation_id=new_id(), session_id=session,
                        request=EditRequest(target_entry_id=op.confirmation_entry_id,
                                            text="改成普通消息")),
        )
        await fixture.service.finish_run(session, edited.run.id, "completed")
        assert (await fixture.confirmations.get(session, op.operation_id)).status == "saved"
        assert await fixture.repository.get_save_record(profile_ids["proposal"]) is not None

        # 编辑之后新准备的待确认快照，用编辑产生的普通节点确认应被拒绝。
        pending = await fixture.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": 1, "payload": PROFILE},
            "prepare_profile_update",
        )
        before = await fixture.state()
        detail = await rejection(
            fixture.business.save_profile_update(
                fixture.context(session, edited.run.request_entry_id, pending["source"]),
                ProfileSaveArguments(
                    proposal_id=pending["proposal"], display_entry_id=pending["display"],
                    confirmation_entry_id=edited.run.request_entry_id,
                ),
            )
        )
        assert detail.code == "profile_confirmation_invalid", detail.code
        assert await fixture.state() == before
        assert (await fixture.repository.get_snapshot(pending["proposal"])).status == "pending"

        # 会话删除：清除未完成操作，保留已保存操作与结果。
        await fixture.service.delete_session(session)
        remaining = await fixture.repository.list_confirmation_operations(session)
        assert [item.operation_id for item in remaining] == [op.operation_id]
        assert remaining[0].status == "saved"
        ghost = (await rejection(fixture.confirmations.get(session, op.operation_id))).code
        return {
            "saved_after_edit": True, "plain_new_message": detail.code,
            "remaining_saved": len(remaining), "ghost": ghost,
        }
    finally:
        await fixture.close()


async def check_storage_constraints(root: Path) -> dict:
    fixture = await Fixture(root / "storage.db").seeded()
    try:
        session = await fixture.session()

        async def raw(sql: str, parameters: tuple = ()):
            async def run(connection):
                cursor = await connection.execute(sql, parameters)
                await cursor.close()

            await fixture.database.read(run)

        op = SaveConfirmationOperation(
            session_id=session, operation_id=new_id(), business_kind="profile",
            proposal_id=new_id(), display_entry_id=new_id(), confirmation_entry_id=new_id(),
            status="processing", result=None, error=None,
            created_at=now_ms(), updated_at=now_ms(),
        )
        async with fixture.repository.transaction():
            await fixture.repository.insert_confirmation_operation(op)
        assert await fixture.repository.get_confirmation_operation(op.operation_id) == op

        columns = ("operation_id, session_id, business_kind, proposal_id, display_entry_id, "
                   "confirmation_entry_id, status, result, error, created_at, updated_at")
        pairing = {}
        # 状态与 result / error 的配对由数据库约束直接拒绝。
        try:
            await raw(
                f"INSERT INTO save_confirmation_operations ({columns}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (new_id(), session, "profile", new_id(), new_id(), new_id(),
                 "saved", None, None, now_ms(), now_ms()),
            )
        except sqlite3.IntegrityError as error:
            pairing["saved_missing_result"] = type(error).__name__
        try:
            await raw(
                f"INSERT INTO save_confirmation_operations ({columns}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (new_id(), session, "profile", new_id(), new_id(), new_id(),
                 "processing", json.dumps({"proposal_id": new_id()}), None, now_ms(), now_ms()),
            )
        except sqlite3.IntegrityError as error:
            pairing["processing_with_result"] = type(error).__name__
        assert pairing == {"saved_missing_result": "IntegrityError", "processing_with_result": "IntegrityError"}

        # 四字段请求与目标关联创建后不可改换。
        try:
            await raw(
                "UPDATE save_confirmation_operations SET proposal_id = ? WHERE operation_id = ?",
                (new_id(), op.operation_id),
            )
        except sqlite3.IntegrityError as error:
            immutable = type(error).__name__
        assert immutable == "IntegrityError"
        kept = await fixture.repository.get_confirmation_operation(op.operation_id)
        assert kept.proposal_id == op.proposal_id

        # 迁移 12 → 13：保留已有业务数据，重建确认操作表。
        async with fixture.repository.transaction():
            await fixture.repository.save_profile(ProfileContent.model_validate(PROFILE), 3, now_ms())
        async with fixture.database.transaction_scope():
            await fixture.database.connection.execute("DROP TABLE save_confirmation_operations")
            await fixture.database.connection.execute("PRAGMA user_version = 12")
        await fixture.database.close()
        fixture = await Fixture(root / "storage.db").open()
        assert (await fixture.repository.get_profile()).version == 3
        assert await fixture.repository.get_confirmation_operation(op.operation_id) is None
        return {"pairing": pairing, "immutable": immutable, "upgraded_preserved_version": 3}
    finally:
        await fixture.close()


async def check_readonly_history(root: Path) -> dict:
    fixture = await Fixture(root / "history.db").seeded()
    try:
        session = await fixture.session()
        profile_ids = await fixture.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": None, "payload": PROFILE},
            "prepare_profile_update",
        )
        op = await fixture.confirmations.confirm(
            session, await fixture.request(session, "profile", profile_ids)
        )
        before = await fixture.state()
        history = await fixture.confirmations.history(session)
        assert [item.operation_id for item in history.confirmations] == [op.operation_id]
        assert history.pending_proposals == []
        assert await fixture.state() == before
        fetched = await fixture.confirmations.get(session, op.operation_id)
        assert fetched.model_dump() == op.model_dump()
        assert await fixture.state() == before
        missing = (await rejection(fixture.confirmations.get(session, new_id()))).code
        assert missing == "confirmation_not_found"

        # 新待确认快照出现在 pending_proposals，且只读查询无副作用。
        pending = await fixture.stage(
            session, "workout",
            {"performed_on": business_date(now_ms()), "base_workout_id": None,
             "base_workout_version": None, "payload": WORKOUT},
            "prepare_workout",
        )
        snapshot = await fixture.state()
        history2 = await fixture.confirmations.history(session)
        assert [item.proposal_id for item in history2.pending_proposals] == [pending["proposal"]]
        assert history2.pending_proposals[0].business_kind == "workout"
        assert await fixture.state() == snapshot
        return {"status": fetched.status, "pending": len(history.pending_proposals),
                "pending_ref": history2.pending_proposals[0].business_kind, "missing": missing}
    finally:
        await fixture.close()


_SAVE_ARGUMENTS = {
    "profile": ProfileSaveArguments,
    "plan": PlanSaveArguments,
    "workout": WorkoutSaveArguments,
}


def save_arguments(kind: str, ids: dict, confirmation: str):
    return _SAVE_ARGUMENTS[kind](
        proposal_id=ids["proposal"], display_entry_id=ids["display"],
        confirmation_entry_id=confirmation,
    )


def business_save(f: Fixture, kind: str, confirmation: str, ids: dict, session: str,
                  *, updating: bool = False):
    save = {
        "profile": f.business.save_profile_update,
        "plan": f.business.save_plan,
        "workout": f.business.update_workout if updating else f.business.save_workout,
    }[kind]
    return save(
        f.context(session, confirmation, ids["source"]),
        save_arguments(kind, ids, confirmation),
    )


async def interrupt_after_accept(f: Fixture, session: str, kind: str, ids: dict,
                                 *, updating: bool = False) -> SaveConfirmationOperation:
    # 受理并让业务进入处理中但不提交，等价于进程在业务落地前被中断。
    operation, accepted = await f.confirmations._accept(
        session, await f.request(session, kind, ids), now_ms()
    )
    assert accepted, operation.operation_id
    confirmation = operation.confirmation_entry_id
    context = f.context(session, confirmation, ids["source"])
    arguments = save_arguments(kind, ids, confirmation)
    async with f.repository.transaction():
        if kind == "profile":
            await f.business._begin_save(context, arguments)
        elif kind == "plan":
            await f.business._begin_plan_save(context, arguments)
        else:
            await f.business._begin_workout_save(context, arguments, updating=updating)
    return operation


async def expire_and_recheck(f: Fixture, session: str, kind: str, ids: dict,
                             *, updating: bool = False) -> dict:
    # 中断恢复后的失败事件不再授权写入；新的按钮确认继续正常保存同一快照。
    interrupted = await interrupt_after_accept(f, session, kind, ids, updating=updating)
    await f.restart()
    failed = await f.confirmations.get(session, interrupted.operation_id)
    assert failed.status == "failed" and failed.error.code == "confirmation_interrupted"
    baseline = await f.state()
    nodes = len(await f.service.list_entries(session))
    detail = await rejection(
        business_save(f, kind, failed.confirmation_entry_id, ids, session, updating=updating)
    )
    assert detail.code == f"{kind}_confirmation_invalid", detail.code
    assert await f.state() == baseline
    assert len(await f.service.list_entries(session)) == nodes
    kept = await f.confirmations.get(session, interrupted.operation_id)
    assert kept.model_dump() == failed.model_dump()
    fresh = await f.confirmations.confirm(session, await f.request(session, kind, ids))
    assert fresh.status == "saved", fresh
    replay = await f.confirmations.confirm(session, await f.request(session, kind, ids))
    assert replay.status == "saved"
    assert replay.result.model_dump() == fresh.result.model_dump()
    assert replay.confirmation_entry_id == fresh.confirmation_entry_id
    return {"rejected": detail.code, "reconfirmed": fresh.status,
            "idempotent_replay": replay.result.model_dump()}


def without_ledger(state: dict) -> dict:
    return {key: value for key, value in state.items() if key != "operations"}


async def check_display_snapshot_binding(root: Path) -> dict:
    f = await Fixture(root / "display-binding.db").seeded()
    try:
        session = await f.session()
        first_args = {"profile_id": 1, "base_profile_version": None, "payload": PROFILE}
        second_args = {**first_args, "payload": {**PROFILE, "goal": "增力"}}
        run = await f.turn(session, "整理两份画像", finish=False)
        assistant = f.assistant("call-a", "prepare_profile_update", first_args)
        assistant.content.append(ToolCall(
            type="toolCall", id="call-b", name="prepare_profile_update", arguments=second_args,
        ))
        source = await f.append(session, run.id, run.request_entry_id, assistant)
        context = f.context(session, run.request_entry_id, source)
        first = await f.business.prepare_profile_update(
            context, ProfileProposalArguments.model_validate(first_args))
        second = await f.business.prepare_profile_update(
            context, ProfileProposalArguments.model_validate(second_args))
        first_display = await f.append(session, run.id, source, ToolResultMessage(
            role="toolResult", tool_call_id="call-a", tool_name="prepare_profile_update",
            content=[TextContent(type="text", text=first.model_dump_json())],
            is_error=False, timestamp=now_ms()))
        second_display = await f.append(session, run.id, first_display, ToolResultMessage(
            role="toolResult", tool_call_id="call-b", tool_name="prepare_profile_update",
            content=[TextContent(type="text", text=second.model_dump_json())],
            is_error=False, timestamp=now_ms()))
        await f.service.finish_run(session, run.id, "completed")
        baseline = await f.state()
        nodes = len(await f.service.list_entries(session))

        # 同批次错绑：第二份快照绑定第一份展示节点，绑定入口按完整内容核验拒绝。
        crossed = await rejection(
            f.business.bind_display_entry(second.proposal_id, first_display)
        )
        assert crossed.code == "profile_confirmation_invalid", crossed.code
        assert (await f.repository.get_snapshot(second.proposal_id)).display_entry_id is None
        assert await f.state() == baseline
        assert len(await f.service.list_entries(session)) == nodes

        # 正确绑定后确认保存：展示内容与保存结果一致。
        await f.business.bind_display_entry(first.proposal_id, first_display)
        await f.business.bind_display_entry(second.proposal_id, second_display)
        saved = await f.confirmations.confirm(session, await f.request(session, "profile", {
            "proposal": second.proposal_id, "display": second_display, "source": source}))
        assert saved.status == "saved", saved
        assert saved.result.content.goal == "增力" == second.payload.goal

        # 数据库层强制错绑的快照：保存授权核验拒绝，零提交且账本无残留。
        state = await f.state()
        after_nodes = len(await f.service.list_entries(session))
        mismatched = {}
        for label, updates in (
            ("proposal_id", {"proposal_id": new_id()}),
            ("base_profile_version", {"proposal_id": new_id(), "base_profile_version": 1}),
            ("payload", {"proposal_id": new_id(),
                         "payload": ProfileContent.model_validate({**PROFILE, "goal": "耐力"})}),
        ):
            snapshot = (await f.repository.get_snapshot(second.proposal_id)).model_copy(
                update={**updates, "status": "pending", "display_entry_id": None,
                        "confirmation_entry_id": None, "created_at": now_ms()}
            )
            async with f.repository.transaction():
                await f.repository.insert_snapshot(snapshot)
                await f.repository.bind_display_entry(snapshot.proposal_id, second_display)
            detail = await rejection(f.confirmations.confirm(
                session, SaveConfirmationRequest(
                    operation_id=new_id(), business_kind="profile",
                    proposal_id=snapshot.proposal_id, display_entry_id=second_display,
                )
            ))
            mismatched[label] = detail.code
            assert detail.code == "profile_confirmation_invalid", (label, detail.code)
            assert await f.state() == state
            assert len(await f.service.list_entries(session)) == after_nodes
        assert (await f.repository.get_snapshot(second.proposal_id)).confirmation_entry_id == (
            saved.confirmation_entry_id
        )

        # 计划与训练记录的既有展示核验继续生效：错绑同样被拒绝。
        plan_ids = await f.stage(
            session, "plan_import",
            {"base_profile_version": 1, "base_plan_id": None, "payload": PLAN},
            "prepare_plan_import",
        )
        plan_run = await f.turn(session, "再整理一份计划", finish=False)
        plan_call = "call-" + uuid4().hex[:8]
        plan_source = await f.append(session, plan_run.id, plan_run.request_entry_id, f.assistant(
            plan_call, "prepare_plan_import",
            {"base_profile_version": 1, "base_plan_id": None, "payload": PLAN},
        ))
        plan_context = f.context(session, plan_run.request_entry_id, plan_source)
        other_plan = await f.business.prepare_plan_import(
            plan_context, PlanImportArguments.model_validate(
                {"base_profile_version": 1, "base_plan_id": None,
                 "payload": {**PLAN, "notes": "另一份计划"}}))
        other_display = await f.append(session, plan_run.id, plan_source, ToolResultMessage(
            role="toolResult", tool_call_id=plan_call, tool_name="prepare_plan_import",
            content=[TextContent(type="text", text=other_plan.model_dump_json())],
            is_error=False, timestamp=now_ms()))
        await f.service.finish_run(session, plan_run.id, "completed")
        plan_crossed = await rejection(
            f.business.bind_plan_display_entry(other_plan.proposal_id, plan_ids["display"])
        )
        assert plan_crossed.code == "plan_confirmation_invalid", plan_crossed.code
        await f.business.bind_plan_display_entry(other_plan.proposal_id, other_display)
        plan_saved = await f.confirmations.confirm(
            session, await f.request(session, "plan", {"proposal": other_plan.proposal_id,
                                                       "display": other_display,
                                                       "source": plan_source}))
        assert plan_saved.status == "saved", plan_saved
        assert plan_saved.result.content.notes == "另一份计划"
        return {
            "crossed_binding": crossed.code, "displayed_goal": second.payload.goal,
            "saved_goal": saved.result.content.goal, "snapshot_mismatch": mismatched,
            "plan_crossed_binding": plan_crossed.code, "saved_plan": plan_saved.result.id,
        }
    finally:
        await f.close()


async def check_expired_confirmation_events(root: Path) -> dict:
    f = await Fixture(root / "expired-events.db").seeded()
    try:
        session = await f.session()
        day = business_date(now_ms())
        profile_ids = await f.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": None, "payload": PROFILE},
            "prepare_profile_update",
        )
        results = {"profile": await expire_and_recheck(f, session, "profile", profile_ids)}

        await f.restart()
        plan_ids = await f.stage(
            session, "plan_import",
            {"base_profile_version": 1, "base_plan_id": None, "payload": PLAN},
            "prepare_plan_import",
        )
        results["plan"] = await expire_and_recheck(f, session, "plan", plan_ids)

        await f.restart()
        workout_ids = await f.stage(
            session, "workout",
            {"performed_on": day, "base_workout_id": None, "base_workout_version": None,
             "payload": WORKOUT},
            "prepare_workout",
        )
        results["workout"] = await expire_and_recheck(f, session, "workout", workout_ids)

        await f.restart()
        record = await f.repository.get_workout_by_date(day)
        update_ids = await f.stage(
            session, "workout",
            {"performed_on": day, "base_workout_id": record.id,
             "base_workout_version": record.version,
             "payload": {**WORKOUT, "notes": "肩部尚可"}},
            "prepare_workout",
        )
        results["workout_update"] = await expire_and_recheck(
            f, session, "workout", update_ids, updating=True
        )

        # 重启后账本与业务事实一致：saved 操作必有同内容的固定保存记录。
        for operation in await f.repository.list_confirmation_operations(session):
            record = await f.confirmations._save_record(
                operation.business_kind, operation.proposal_id
            )
            if operation.status == "saved":
                assert record is not None, operation.operation_id
                assert record.result.model_dump() == operation.result.model_dump()
            elif operation.status == "failed":
                assert operation.result is None and operation.error is not None
            else:
                raise AssertionError(f"重启后仍有处理中操作: {operation.operation_id}")
        return results
    finally:
        await f.close()


async def check_processing_retry(root: Path) -> dict:
    f = await Fixture(root / "processing-retry.db").seeded()
    try:
        session = await f.session()
        ids = await f.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": None, "payload": PROFILE},
            "prepare_profile_update",
        )
        operation_id = new_id()
        request = await f.request(session, "profile", ids, operation_id=operation_id)
        nodes = len(await f.service.list_entries(session))

        # 受理后、执行前到达：两个真实并发任务共用同一受理事务边界，仅一方执行保存。
        concurrent = await asyncio.gather(
            asyncio.create_task(f.confirmations.confirm(session, request)),
            asyncio.create_task(f.confirmations.confirm(session, request)),
        )
        statuses = sorted(item.status for item in concurrent)
        assert statuses == ["processing", "saved"], statuses
        assert all(item.operation_id == operation_id for item in concurrent)
        assert len({item.confirmation_entry_id for item in concurrent}) == 1
        assert len(await f.service.list_entries(session)) == nodes + 1
        state = await f.state()
        assert state["profile"] == 1 and state["profile_records"] == 1
        assert state["operations"] == 1
        processing = next(item for item in concurrent if item.status == "processing")
        assert processing.result is None and processing.error is None

        # 受理后、执行前到达：同键请求复用原处理中对象，不执行保存也不记录失败。
        second_ids = await f.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": 1, "payload": {**PROFILE, "goal": "耐力"}},
            "prepare_profile_update",
        )
        second_request = await f.request(session, "profile", second_ids)
        operation, is_first = await f.confirmations._accept(session, second_request, now_ms())
        assert is_first
        before = await f.state()
        nodes_before = len(await f.service.list_entries(session))
        retry = await f.confirmations.confirm(session, second_request)
        assert retry.model_dump() == operation.model_dump() and retry.status == "processing"
        assert await f.state() == before
        assert len(await f.service.list_entries(session)) == nodes_before

        # 原执行负责人完成后：同键请求返回固定 saved 结果，无新增版本。
        done = await f.confirmations._execute(operation, second_request, None)
        assert done.status == "saved" and done.result.version == 2
        committed = await f.state()
        after = await f.confirmations.confirm(session, second_request)
        assert after.result.model_dump() == done.result.model_dump()
        assert after.updated_at == done.updated_at
        assert await f.state() == committed
        assert (await f.repository.get_profile()).version == 2

        # 业务处理中到达：快照被原执行占用时，同键请求仍只返回原对象，不改写终态。
        third_ids = await f.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": 2, "payload": {**PROFILE, "goal": "力量"}},
            "prepare_profile_update",
        )
        third_request = await f.request(session, "profile", third_ids)
        third, third_first = await f.confirmations._accept(session, third_request, now_ms())
        assert third_first
        async with f.repository.transaction():
            await f.business._begin_save(
                f.context(session, third.confirmation_entry_id, third_ids["source"]),
                save_arguments("profile", third_ids, third.confirmation_entry_id),
            )
        occupied = await f.state()
        during = await f.confirmations.confirm(session, third_request)
        assert during.model_dump() == third.model_dump() and during.status == "processing"
        assert await f.state() == occupied
        assert (await f.repository.get_snapshot(third_ids["proposal"])).status == "processing"
        await f.restart()
        released = await f.confirmations.get(session, third.operation_id)
        assert released.status == "failed" and released.error.code == "confirmation_interrupted"
        assert (await f.repository.get_snapshot(third_ids["proposal"])).status == "pending"

        # 原执行失败后：同键请求返回原失败对象，不重新执行。
        conflict_ids = await f.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": 2, "payload": {**PROFILE, "goal": "减脂"}},
            "prepare_profile_update",
        )
        async with f.repository.transaction():
            await f.repository.save_profile(
                ProfileContent.model_validate(PROFILE), 9, now_ms())
        conflict_request = await f.request(session, "profile", conflict_ids)
        failed = await f.confirmations.confirm(session, conflict_request)
        assert failed.status == "failed" and failed.error.code == "profile_version_conflict"
        state_after_failure = await f.state()
        replay = await f.confirmations.confirm(session, conflict_request)
        assert replay.model_dump() == failed.model_dump()
        assert await f.state() == state_after_failure

        # 同键异请求始终冲突，处理中的操作不被改写。
        wrong = await rejection(f.confirmations.confirm(
            session, second_request.model_copy(update={"display_entry_id": new_id()})
        ))
        assert wrong.code == "operation_conflict", wrong.code
        assert (await f.confirmations.get(session, second_request.operation_id)).status == "saved"
        return {
            "concurrent": statuses, "processing_replay": retry.status,
            "saved_replay": after.result.model_dump(), "failed_replay": replay.error.code,
            "different_request": wrong.code,
        }
    finally:
        await f.close()


async def check_operation_id_namespace(root: Path) -> dict:
    f = await Fixture(root / "operation-id.db").seeded()
    try:
        session = await f.session()
        other = await f.session("另一会话")
        results = {}

        # 聊天请求被编辑后原编号只留失效标记，确认入口不得复用。
        chat_id = new_id()
        turn = await f.service.accept_send(
            SendCommand(operation_id=chat_id, session_id=session,
                        request=SendRequest(text="旧请求")), system_message=SYSTEM)
        await f.service.finish_run(session, turn.run.id, "completed")
        edited = await f.service.accept_edit(
            EditCommand(operation_id=new_id(), session_id=session,
                        request=EditRequest(target_entry_id=turn.run.request_entry_id,
                                            text="新请求")))
        await f.service.finish_run(session, edited.run.id, "completed")
        assert await f.service.get_operation(chat_id) is None
        assert await f.service.get_operation_invalidation(chat_id) == session
        ids = await f.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": None, "payload": PROFILE},
            "prepare_profile_update",
        )
        baseline = await f.state()
        nodes = len(await f.service.list_entries(session))
        expired = await rejection(f.confirmations.confirm(
            session, await f.request(session, "profile", ids, operation_id=chat_id)))
        assert expired.code == "operation_conflict", expired.code
        assert expired.detail().get("reason") == "operation_expired", expired.detail()
        assert await f.state() == baseline
        assert len(await f.service.list_entries(session)) == nodes
        assert (await f.repository.get_snapshot(ids["proposal"])).status == "pending"
        results["expired_chat_id"] = expired.detail()

        # 已登记的确认编号跨入口与跨会话均不可复用。
        confirm_id = new_id()
        saved = await f.confirmations.confirm(
            session, await f.request(session, "profile", ids, operation_id=confirm_id))
        assert saved.status == "saved"
        chat_reuse = await rejection(f.service.accept_send(
            SendCommand(operation_id=confirm_id, session_id=session,
                        request=SendRequest(text="复用确认编号")), system_message=SYSTEM))
        assert chat_reuse.__class__.__name__ == "OperationConflict", chat_reuse
        committed = await f.state()
        cross = await rejection(f.confirmations.confirm(
            other, await f.request(other, "profile", ids, operation_id=confirm_id)))
        assert cross.code == "operation_conflict", cross.code
        assert "reason" not in cross.detail(), cross.detail()
        assert await f.state() == committed
        assert len(await f.service.list_entries(other)) == 0
        results["cross_session"] = cross.code

        # 编辑未保存的确认节点：原操作连同编号失效，编号不可重新授权或复用。
        pending_ids = await f.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": 1, "payload": {**PROFILE, "goal": "耐力"}},
            "prepare_profile_update",
        )
        pending_id = new_id()
        operation, is_first = await f.confirmations._accept(
            session, await f.request(session, "profile", pending_ids, operation_id=pending_id),
            now_ms())
        assert is_first and operation.status == "processing"
        moved = await f.service.accept_edit(
            EditCommand(operation_id=new_id(), session_id=session,
                        request=EditRequest(target_entry_id=operation.confirmation_entry_id,
                                            text="改成普通消息")))
        await f.service.finish_run(session, moved.run.id, "completed")
        assert await f.repository.get_confirmation_operation(pending_id) is None
        assert await f.service.get_operation_invalidation(pending_id) == session
        before = await f.state()
        reuse = await rejection(f.confirmations.confirm(
            session, await f.request(session, "profile", pending_ids, operation_id=pending_id)))
        assert reuse.code == "operation_conflict" and reuse.detail().get("reason") == "operation_expired"
        stale = await rejection(business_save(
            f, "profile", operation.confirmation_entry_id, pending_ids, session))
        assert stale.code == "profile_confirmation_invalid", stale.code
        assert await f.state() == before
        snapshot = await f.repository.get_snapshot(pending_ids["proposal"])
        assert snapshot.status == "pending" and snapshot.confirmation_entry_id is None
        chat_expired = await rejection(f.service.accept_send(
            SendCommand(operation_id=pending_id, session_id=session,
                        request=SendRequest(text="复用确认编号")), system_message=SYSTEM))
        assert chat_expired.__class__.__name__ == "OperationExpired", chat_expired
        # 失效编号仍可通过新确认正常保存同一快照。
        fresh = await f.confirmations.confirm(
            session, await f.request(session, "profile", pending_ids))
        assert fresh.status == "saved" and fresh.result.version == 2
        results["edited_confirmation"] = reuse.detail()
        return results
    finally:
        await f.close()


async def check_replacement_and_migration(root: Path) -> dict:
    path = root / "replacement-migration.db"
    f = await Fixture(path).seeded()
    try:
        session = await f.session()
        ids = await f.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": None, "payload": PROFILE},
            "prepare_profile_update",
        )
        assert (await f.confirmations.confirm(
            session, await f.request(session, "profile", ids))).status == "saved"
        second_ids = await f.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": 1, "payload": {**PROFILE, "goal": "耐力"}},
            "prepare_profile_update",
        )
        second_request = await f.request(session, "profile", second_ids)
        operation, is_first = await f.confirmations._accept(session, second_request, now_ms())
        assert is_first

        # 内容替换意图与业务提交交错：首次提交被否决，撤回关联后重新核验并提交。
        held = asyncio.Event()
        release = asyncio.Event()

        async def hold_replacement() -> None:
            async with f.replacements.register(session):
                held.set()
                await release.wait()

        holder = asyncio.create_task(hold_replacement())
        await held.wait()
        executor = asyncio.create_task(
            f.confirmations._execute(operation, second_request, None))
        during = await f.confirmations.confirm(session, second_request)
        assert during.status == "processing", during.status
        assert (await f.repository.get_profile()).version == 1
        release.set()
        await holder
        done = await executor
        assert done.status == "saved" and done.result.version == 2
        assert (await f.repository.get_profile()).version == 2
        assert (await f.repository.get_save_record(second_ids["proposal"])).result.model_dump() == (
            done.result.model_dump())

        await f.restart()
        recovered = await f.confirmations.get(session, second_request.operation_id)
        assert recovered.model_dump() == done.model_dump()
        nodes = len(await f.service.list_entries(session))
        records = without_ledger(await f.state())

        # 迁移 12→13 保真：已有业务数据、消息节点与固定结果全部保留。
        async with f.database.transaction_scope():
            await f.database.connection.execute("DROP TABLE save_confirmation_operations")
            await f.database.connection.execute("PRAGMA user_version = 12")
        await f.database.close()
        f = await Fixture(path).open()
        assert (await f.repository.get_profile()).version == 2
        assert without_ledger(await f.state()) == records
        assert len(await f.service.list_entries(session)) == nodes
        replay = await f.confirmations.confirm(session, second_request)
        assert replay.status == "saved" and replay.result.model_dump() == done.result.model_dump()
        assert (await f.repository.get_profile()).version == 2

        # 删除与已提交结果交错：未完成操作被清除，已保存业务数据与固定结果保持完整。
        third_ids = await f.stage(
            session, "profile",
            {"profile_id": 1, "base_profile_version": 2, "payload": {**PROFILE, "goal": "力量"}},
            "prepare_profile_update",
        )
        third_request = await f.request(session, "profile", third_ids)
        _, third_first = await f.confirmations._accept(session, third_request, now_ms())
        assert third_first
        saved_before = [
            item.operation_id
            for item in await f.repository.list_confirmation_operations(session)
            if item.status == "saved"
        ]
        await f.service.delete_session(session)
        remaining = await f.repository.list_confirmation_operations(session)
        assert sorted(item.operation_id for item in remaining) == sorted(saved_before)
        assert all(item.status == "saved" for item in remaining)
        assert (await f.repository.get_profile()).version == 2
        assert await count_rows(f.database, "profile_save_records") == 2
        return {
            "veto_then_commit": done.result.model_dump(),
            "processing_during_replacement": during.status,
            "restart_ledger": recovered.model_dump()["status"],
            "migration_preserved": records,
            "replay_after_migration": replay.result.version,
            "delete_keeps_saved_operations": len(remaining),
        }
    finally:
        await f.close()


def check() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    evidence: dict = {}
    with TemporaryDirectory(dir=EVIDENCE, ignore_cleanup_errors=True) as directory:
        root = Path(directory)
        evidence["saves_and_ambiguity"] = asyncio.run(check_saves_and_ambiguity(root))
        evidence["plan_and_workout_update"] = asyncio.run(check_plan_and_workout_update(root))
        evidence["rejections"] = asyncio.run(check_rejections(root))
        evidence["idempotency_and_conflict"] = asyncio.run(check_idempotency_and_conflict(root))
        evidence["failure_and_recovery"] = asyncio.run(check_failure_and_recovery(root))
        evidence["edit_and_delete"] = asyncio.run(check_edit_and_delete(root))
        evidence["storage_constraints"] = asyncio.run(check_storage_constraints(root))
        evidence["readonly_history"] = asyncio.run(check_readonly_history(root))
        evidence["display_snapshot_binding"] = asyncio.run(check_display_snapshot_binding(root))
        evidence["expired_confirmation_events"] = asyncio.run(check_expired_confirmation_events(root))
        evidence["processing_retry"] = asyncio.run(check_processing_retry(root))
        evidence["operation_id_namespace"] = asyncio.run(check_operation_id_namespace(root))
        evidence["replacement_and_migration"] = asyncio.run(check_replacement_and_migration(root))
    (EVIDENCE / "structured-confirmation.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
    )
    print("PASS: 结构化确认 —— 普通文字零授权、三类业务保存、计划与训练更新、错误目标零提交、"
          "幂等与跨入口冲突、失败重试与中断恢复、编辑删除交错、只读历史无副作用、"
          "展示与快照一致、失效事件零写入、处理中重试零重复执行、跨入口编号防冲突、"
          "提交否决交错与迁移保真")


if __name__ == "__main__":
    check()
