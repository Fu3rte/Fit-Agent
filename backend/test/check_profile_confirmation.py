import asyncio
import json
import sqlite3
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from app.agent.tool import run_tool_call
from app.agent.tools.business import bind_business_tools, business_tool_declarations
from app.ai.messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
)
from app.application.business.catalog import Catalog
from app.application.business.coordination import ReplacementCoordinator
from app.application.business.service import BusinessService, business_date
from app.application.session.service import SessionService
from app.domain.business.errors import BusinessError
from app.domain.business.models import (
    BusinessContext,
    ProfileContent,
    ProfileProposalArguments,
    ProfileSaveArguments,
    ProfileSaveRecord,
    ProfileSaveResult,
)
from app.domain.session.models import (
    EditCommand,
    EditRequest,
    RegenerateCommand,
    RegenerateRequest,
    SendCommand,
    SendRequest,
    SessionMessageEntry,
)
from app.infrastructure.persistence.sqlite.business_repository import (
    SqliteBusinessRepository,
)
from app.infrastructure.persistence.sqlite.database import Database, open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from app.interfaces.http import app
from test.regression_support import (
    Server,
    client,
    patch_default_database,
    temporary_root,
)

EVIDENCE = temporary_root("profile-confirmation")
USAGE = Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2)
SYSTEM = SystemMessage(role="system", content="系统提示词", tools_added=[], timestamp=100)

GET_PROFILE = "get_profile"
SEARCH_EXERCISES = "search_exercises"
PREPARE = "prepare_profile_update"
SAVE = "save_profile_update"
STATUS = "get_profile_update_status"
# 声明与实例同源：harness 用同一份声明校验参数与执行注册表。
DECLARED = {item.name: item for item in business_tool_declarations()}

FULL_PROFILE = {
    "goal": "增肌",
    "experience": "新手",
    "environment": "健身房",
    "availability": "每周四次",
    "health_notes": None,
    "movement_restrictions": "避免肩部过顶",
    "unavailable_equipment": [],
    "forbidden_exercise_ids": [],
}

PREPARE_FIELDS = {"proposal_id", "profile_id", "base_profile_version", "payload"}
SAVE_FIELDS = {"proposal_id", "profile_id", "version", "content", "saved_at"}
STATUS_FIELDS = {"proposal_id", "status", "result"}


def payload(**overrides) -> dict:
    result = dict(FULL_PROFILE)
    result.update(overrides)
    return result


def new_id() -> str:
    return str(uuid4())


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def message_text(message) -> str:
    return "".join(block.text for block in message.content)


def envelope(message) -> dict:
    # 工具结果为单个 text 内容块，块内是完整可解析 JSON。
    assert len(message.content) == 1, message.content
    block = message.content[0]
    assert isinstance(block, TextContent) and block.type == "text", block
    body = json.loads(block.text)
    assert isinstance(body, dict), body
    return body


def prepare_arguments(base, data: dict) -> dict:
    return {
        "profile_id": 1,
        "base_profile_version": base,
        "payload": data,
    }


def proposal_model(base, data: dict) -> ProfileProposalArguments:
    return ProfileProposalArguments.model_validate(prepare_arguments(base, data))


async def count_rows(database: Database, table: str) -> int:
    async def read(connection):
        cursor = await connection.execute(f"SELECT COUNT(*) FROM {table}", ())
        row = await cursor.fetchone()
        await cursor.close()
        return int(row[0])

    return await database.read(read)


async def rejection(awaitable) -> BusinessError:
    try:
        await awaitable
    except BusinessError as error:
        return error
    raise AssertionError("expected BusinessError")


async def assert_rejected(label: str, awaitable, code: str) -> dict:
    error = await rejection(awaitable)
    assert error.code == code, (label, error.code, error.detail())
    return error.detail()


class Fixture:
    """真实 SQLite、真实服务与真实工具 harness 的测试夹具。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.database: Database | None = None
        self.service: SessionService | None = None
        self.repository: SqliteBusinessRepository | None = None
        self.business: BusinessService | None = None
        self.catalog: Catalog | None = None
        self.replacements: ReplacementCoordinator | None = None
        self.loop = None

    async def open(self, *, recovered: bool = False) -> "Fixture":
        self.database = await open_database(self.path)
        self.repository = SqliteBusinessRepository(self.database)
        self.catalog = Catalog.load()
        self.replacements = ReplacementCoordinator()
        service = SessionService(SqliteSessionRepository(self.database))
        if recovered:
            # 与 lifespan 等价：先恢复中断的运行，再恢复中断的保存。
            await service.recover_interrupted()
            await self.repository.recover_interrupted_saves()
        self.business = BusinessService(
            self.repository, self.catalog, service, self.replacements
        )
        service.attach_snapshots(self.business)
        self.service = service
        self.loop = asyncio.get_running_loop()
        return self

    async def seeded(self) -> "Fixture":
        await self.open()
        async with self.repository.transaction():
            await self.repository.replace_exercises(self.catalog.all())
        return self

    async def restart(self) -> "Fixture":
        # 关闭连接后重开同一文件并执行恢复，等价于服务重启。
        await self.database.close()
        return await self.open(recovered=True)

    async def close(self) -> None:
        await self.database.close()

    def call(self, coroutine):
        # 工具在工作线程执行，业务协程投递回持有数据库的主事件循环。
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result()

    def context(
        self, session_id: str, request_entry_id: str, source_entry_id: str
    ) -> BusinessContext:
        return BusinessContext(
            timezone="Asia/Shanghai",
            business_date=business_date(now_ms()),
            session_id=session_id,
            run_id=new_id(),
            request_entry_id=request_entry_id,
            source_entry_id=source_entry_id,
        )

    def save_arguments(self, ids: dict, **overrides) -> ProfileSaveArguments:
        return ProfileSaveArguments(
            proposal_id=overrides.get("proposal", ids["proposal"]),
            display_entry_id=overrides.get("display", ids["display"]),
            confirmation_entry_id=overrides.get("confirmation", ids["confirmation"]),
        )

    def save_context(self, ids: dict, **overrides) -> BusinessContext:
        return self.context(
            overrides.get("session", ids["session"]),
            overrides.get("request", ids["request"]),
            overrides.get("source", ids["source"]),
        )

    async def session(self, title: str = "画像确认会话") -> str:
        session_id = new_id()
        created, _ = await self.service.create_session_result(session_id, title)
        assert created
        return session_id

    async def user_turn(self, session_id: str, text: str) -> str:
        outcome = await self.service.accept_send(
            SendCommand(
                operation_id=new_id(),
                session_id=session_id,
                request=SendRequest(text=text),
            ),
            system_message=SYSTEM,
        )
        run = outcome.run
        assert run is not None
        await self.service.finish_run(session_id, run.id, "completed")
        return run.request_entry_id

    async def append(
        self, session_id: str, run_id: str, parent_id: str, message
    ) -> str:
        node_id = new_id()
        await self.service.append_entry(
            SessionMessageEntry(
                session_id=session_id,
                id=node_id,
                parent_id=parent_id,
                run_id=run_id,
                type="message",
                messages=[message],
                created_at=now_ms(),
            )
        )
        return node_id

    async def invoke(
        self,
        name: str,
        arguments: dict,
        context: BusinessContext,
        prepared: dict[str, str] | None = None,
        *,
        call_id: str | None = None,
    ) -> ToolResultMessage:
        tools = bind_business_tools(
            self.business, context, self.call, {} if prepared is None else prepared
        )
        return await run_tool_call(
            ToolCall(
                type="toolCall",
                id=call_id or ("call-" + uuid4().hex[:8]),
                name=name,
                arguments=arguments,
            ),
            tools={name: tools[name]},
            declared={name: DECLARED[name]},
        )

    async def tool_save(self, ids: dict, **overrides) -> ToolResultMessage:
        return await self.invoke(
            SAVE,
            self.save_arguments(ids, **overrides).model_dump(),
            self.save_context(ids, **overrides),
        )

    async def tool_status(
        self, ids: dict, proposal_id: str | None = None, **overrides
    ) -> ToolResultMessage:
        return await self.invoke(
            STATUS,
            {"proposal_id": proposal_id or ids["proposal"]},
            self.save_context(ids, **overrides),
        )

    async def service_status(
        self, ids: dict, proposal_id: str | None = None, **overrides
    ):
        return await self.business.get_profile_update_status(
            self.save_context(ids, **overrides), proposal_id or ids["proposal"]
        )

    async def service_save(self, ids: dict, **overrides):
        return await self.business.save_profile_update(
            self.save_context(ids, **overrides),
            self.save_arguments(ids, **overrides),
        )

    async def snapshot(self, proposal_id: str):
        return await self.repository.get_snapshot(proposal_id)

    async def status_of(self, proposal_id: str) -> str:
        snapshot = await self.repository.get_snapshot(proposal_id)
        assert snapshot is not None, proposal_id
        return snapshot.status

    async def profile_state(self) -> dict:
        profile = await self.business.get_profile()
        return {
            "version": profile.version,
            "profile_rows": await count_rows(self.database, "profile"),
            "record_rows": await count_rows(self.database, "profile_save_records"),
        }


def assistant_prepare_call(
    call_id: str, arguments: dict, timestamp: int
) -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=[ToolCall(type="toolCall", id=call_id, name=PREPARE, arguments=arguments)],
        api="openai-completions",
        provider="example",
        model="model-1",
        usage=USAGE,
        stop_reason="toolUse",
        timestamp=timestamp,
    )


async def propose(
    fixture: Fixture,
    session_id: str,
    arguments: dict,
    *,
    bind: bool = True,
    confirmation_text: str | None = "确认保存这份画像",
) -> dict:
    # 完整回合：用户请求节点 -> 助手工具调用节点 -> 真实执行准备工具 -> 结果节点持久化
    # -> 后端绑定展示节点 -> 用户确认节点。
    outcome = await fixture.service.accept_send(
        SendCommand(
            operation_id=new_id(),
            session_id=session_id,
            request=SendRequest(text="请整理我的画像"),
        ),
        system_message=SYSTEM,
    )
    run = outcome.run
    assert run is not None
    request_id = run.request_entry_id
    call_id = "call-" + uuid4().hex[:8]
    source_id = await fixture.append(
        session_id,
        run.id,
        request_id,
        assistant_prepare_call(call_id, arguments, now_ms()),
    )
    prepared: dict[str, str] = {}
    message = await fixture.invoke(
        PREPARE,
        arguments,
        fixture.context(session_id, request_id, source_id),
        prepared,
        call_id=call_id,
    )
    assert message.is_error is False, message_text(message)
    result = envelope(message)
    # prepared 由准备工具按 tool_call_id 记录快照标识，供结果节点持久化后绑定展示。
    assert prepared == {call_id: result["proposal_id"]}, prepared
    display_id = await fixture.append(session_id, run.id, source_id, message)
    if bind:
        await fixture.business.bind_display_entry(result["proposal_id"], display_id)
    await fixture.service.finish_run(session_id, run.id, "completed")
    confirmation = (
        None
        if confirmation_text is None
        else await fixture.user_turn(session_id, confirmation_text)
    )
    return {
        "session": session_id,
        "run": run.id,
        "request": request_id,
        "source": source_id,
        "call": call_id,
        "display": display_id,
        "confirmation": confirmation,
        "proposal": result["proposal_id"],
        "proposal_body": result,
        "result_message": message,
    }


async def check_first_build(root: Path) -> dict:
    fixture = await Fixture(root / "first-build.db").seeded()
    try:
        session_id = await fixture.session()

        # 声明协议：五个业务工具，准备输入含 profile_id 且无旧卡片字段。
        tools = bind_business_tools(
            fixture.business,
            fixture.context(session_id, session_id, session_id),
            fixture.call,
            {},
        )
        assert set(tools) == {
            GET_PROFILE,
            SEARCH_EXERCISES,
            PREPARE,
            SAVE,
            STATUS,
        }
        assert {name: tool.definition() for name, tool in tools.items()} == DECLARED
        assert set(DECLARED[PREPARE].parameters["properties"]) == {
            "profile_id",
            "base_profile_version",
            "payload",
        }
        assert set(DECLARED[PREPARE].parameters["required"]) == {
            "profile_id",
            "base_profile_version",
            "payload",
        }
        assert set(DECLARED[SAVE].parameters["properties"]) == {
            "proposal_id",
            "display_entry_id",
            "confirmation_entry_id",
        }
        assert set(DECLARED[STATUS].parameters["properties"]) == {"proposal_id"}

        empty = await fixture.invoke(
            GET_PROFILE, {}, fixture.context(session_id, session_id, session_id)
        )
        assert empty.is_error is False
        assert envelope(empty) == {"version": None, "content": None}

        # 首次建档：准备结果只含四个字段，profile_id 为整数 1。
        ids = await propose(
            fixture, session_id, prepare_arguments(None, payload())
        )
        body = ids["proposal_body"]
        assert set(body) == PREPARE_FIELDS, body
        assert body["profile_id"] == 1 and type(body["profile_id"]) is int
        assert body["base_profile_version"] is None
        assert body["payload"] == payload()

        # 结果节点即展示节点：角色保持 toolResult，与助手工具调用配对。
        display = await fixture.service.get_entry(session_id, ids["display"])
        stored = display.messages[0]
        assert isinstance(stored, ToolResultMessage) and stored.role == "toolResult"
        assert stored.tool_name == PREPARE and stored.tool_call_id == ids["call"]
        assert stored.is_error is False
        assert display.parent_id == ids["source"]
        snapshot = await fixture.snapshot(ids["proposal"])
        assert snapshot.display_entry_id == ids["display"]
        assert snapshot.confirmation_entry_id is None
        assert snapshot.status == "pending"

        status = await fixture.service_status(ids)
        assert set(status.model_dump()) == STATUS_FIELDS
        assert status.status == "pending" and status.result is None
        pending_tool = await fixture.tool_status(ids)
        assert pending_tool.is_error is False
        assert pending_tool.role == "toolResult"
        assert set(envelope(pending_tool)) == STATUS_FIELDS
        assert envelope(pending_tool) == {
            "proposal_id": ids["proposal"],
            "status": "pending",
            "result": None,
        }

        saved = envelope(await fixture.tool_save(ids))
        assert set(saved) == SAVE_FIELDS, saved
        assert saved["proposal_id"] == ids["proposal"]
        assert saved["profile_id"] == 1 and type(saved["profile_id"]) is int
        assert saved["version"] == 1
        assert saved["content"] == payload()
        assert type(saved["saved_at"]) is int and saved["saved_at"] > 0

        profile = await fixture.business.get_profile()
        assert profile.version == 1
        assert profile.content.model_dump() == payload()
        assert await fixture.status_of(ids["proposal"]) == "saved"
        after = await fixture.service_status(ids)
        assert after.status == "saved" and after.result.model_dump() == saved
        record = await fixture.repository.get_save_record(ids["proposal"])
        assert record.result.model_dump() == saved
        assert record.session_id == session_id
        assert record.display_entry_id == ids["display"]
        assert record.confirmation_entry_id == ids["confirmation"]

        # 更新流程：以版本 1 为依据整理新画像并保存为版本 2。
        second = await propose(
            fixture, session_id, prepare_arguments(1, payload(goal="减脂"))
        )
        assert second["proposal_body"]["base_profile_version"] == 1
        second_saved = envelope(await fixture.tool_save(second))
        assert second_saved["version"] == 2
        assert second_saved["content"] == payload(goal="减脂")
        assert (await fixture.business.get_profile()).version == 2

        # 失效链：同会话同目标画像的新快照使旧待确认快照失效。
        third = await propose(
            fixture,
            session_id,
            prepare_arguments(2, payload(goal="力量")),
            confirmation_text=None,
        )
        fourth = await propose(
            fixture,
            session_id,
            prepare_arguments(2, payload(goal="耐力")),
            confirmation_text=None,
        )
        assert await fixture.status_of(third["proposal"]) == "invalidated"
        assert await fixture.status_of(fourth["proposal"]) == "pending"
        detail = await assert_rejected(
            "失效快照保存",
            fixture.service_save({**third, "confirmation": second["confirmation"]}),
            "profile_proposal_invalidated",
        )
        assert "errors" not in detail
        assert (await fixture.business.get_profile()).version == 2
        return {
            "prepare": body,
            "first_saved": saved,
            "second_saved": second_saved,
            "invalidated_chain": [third["proposal"], fourth["proposal"]],
            "tool_names": sorted(tools),
        }
    finally:
        await fixture.close()


async def check_display_binding(root: Path) -> dict:
    fixture = await Fixture(root / "display-binding.db").seeded()
    try:
        session_id = await fixture.session()
        other_session = await fixture.session("另一会话")
        other = await propose(
            fixture, other_session, prepare_arguments(None, payload(goal="其他会话"))
        )

        # 展示未绑定即保存：结果节点已持久化，但后端绑定缺失。
        unbound = await propose(
            fixture, session_id, prepare_arguments(None, payload()), bind=False
        )
        assert (await fixture.snapshot(unbound["proposal"])).display_entry_id is None
        detail = await assert_rejected(
            "未绑定展示即保存",
            fixture.service_save(unbound),
            "profile_confirmation_invalid",
        )
        assert "展示节点与后端绑定不一致" in detail["message"]
        assert await fixture.status_of(unbound["proposal"]) == "pending"

        # 展示绑定只能指向同会话工具结果节点：助手节点、用户节点、跨会话节点均拒绝。
        rejected_bindings = []
        for label, target in (
            ("助手节点", unbound["source"]),
            ("用户确认节点", unbound["confirmation"]),
            ("另一会话工具结果节点", other["display"]),
        ):
            try:
                await fixture.business.bind_display_entry(unbound["proposal"], target)
            except sqlite3.IntegrityError as error:
                rejected_bindings.append(f"{label}:{type(error).__name__}")
            else:
                raise AssertionError(f"{label} 展示绑定未被拒绝")
        assert all("IntegrityError" in item for item in rejected_bindings)
        assert (await fixture.snapshot(unbound["proposal"])).display_entry_id is None

        # 已绑定快照：保存使用其他会话的展示节点同样拒绝。
        bound = await propose(fixture, session_id, prepare_arguments(None, payload()))
        detail = await assert_rejected(
            "展示节点不匹配",
            fixture.service_save(bound, display=other["display"]),
            "profile_confirmation_invalid",
        )
        assert "展示节点与后端绑定不一致" in detail["message"]
        assert await fixture.status_of(bound["proposal"]) == "pending"
        return {
            "unbound_rejected": True,
            "binding_trigger_rejections": rejected_bindings,
            "display_mismatch": detail,
        }
    finally:
        await fixture.close()


async def check_confirmation_binding(root: Path) -> dict:
    fixture = await Fixture(root / "confirmation-binding.db").seeded()
    try:
        session_id = await fixture.session()
        probe = await propose(fixture, session_id, prepare_arguments(None, payload()))

        # 确认节点早于展示节点。
        detail = await assert_rejected(
            "确认早于展示",
            fixture.service_save(probe, confirmation=probe["request"]),
            "profile_confirmation_invalid",
        )
        assert "确认消息必须晚于" in detail["message"]

        # 确认节点角色为助手。
        detail = await assert_rejected(
            "确认节点为助手",
            fixture.service_save(probe, confirmation=probe["source"]),
            "profile_confirmation_invalid",
        )
        assert "确认节点必须是用户消息" in detail["message"]

        saved = envelope(await fixture.tool_save(probe))
        assert saved["version"] == 1

        # 重复保存同一快照：返回完全相同的固定结果，版本不再递增。
        state = await fixture.profile_state()
        repeated = envelope(await fixture.tool_save(probe))
        assert repeated == saved, (repeated, saved)
        assert repeated["saved_at"] == saved["saved_at"]
        assert await fixture.profile_state() == state

        # 同一确认消息绑定第二个快照：拒绝且原绑定保留。
        second = await propose(
            fixture, session_id, prepare_arguments(1, payload(goal="塑形"))
        )
        detail = await assert_rejected(
            "确认消息重复绑定",
            fixture.service_save(second, confirmation=probe["confirmation"]),
            "profile_confirmation_invalid",
        )
        assert "该确认消息已绑定其他快照" in detail["message"]
        kept = await fixture.repository.find_snapshot_by_confirmation(
            session_id, probe["confirmation"]
        )
        assert kept is not None and kept.proposal_id == probe["proposal"]
        assert kept.status == "saved"
        assert await fixture.status_of(second["proposal"]) == "pending"
        assert await fixture.repository.get_save_record(second["proposal"]) is None
        assert await fixture.profile_state() == state
        duplicate_message = detail["message"]

        # 编辑确认消息：原确认节点离开当前消息路径，快照保留待确认。
        edited = await fixture.service.accept_edit(
            EditCommand(
                operation_id=new_id(),
                session_id=session_id,
                request=EditRequest(
                    target_entry_id=second["confirmation"], text="改成另一句确认"
                ),
            )
        )
        await fixture.service.finish_run(session_id, edited.run.id, "completed")
        assert await fixture.status_of(second["proposal"]) == "pending"
        detail = await assert_rejected(
            "确认节点不在当前路径",
            fixture.service_save(second),
            "profile_confirmation_invalid",
        )
        assert "当前消息路径" in detail["message"]
        # 编辑产生的新确认节点晚于展示节点，可完成保存。
        replacement = {**second, "confirmation": edited.run.request_entry_id}
        edited_saved = envelope(await fixture.tool_save(replacement))
        assert edited_saved["version"] == 2

        # 跨会话归属校验。
        other_session = await fixture.session("跨会话")
        other = await propose(
            fixture, other_session, prepare_arguments(2, payload(goal="其他"))
        )
        detail = await assert_rejected(
            "跨会话保存",
            fixture.service_save(other, proposal=probe["proposal"]),
            "profile_access_denied",
        )
        assert "errors" not in detail
        message = await fixture.tool_status(other, probe["proposal"])
        assert message.is_error is True
        assert envelope(message)["code"] == "profile_access_denied"
        assert (await fixture.business.get_profile()).version == 2
        assert await fixture.status_of(probe["proposal"]) == "saved"

        # 会话不存在。
        ghost = fixture.context(new_id(), new_id(), new_id())
        detail = await assert_rejected(
            "缺失会话查询",
            fixture.business.get_profile_update_status(ghost, probe["proposal"]),
            "session_not_found",
        )
        assert "errors" not in detail
        await assert_rejected(
            "缺失会话准备",
            fixture.business.prepare_profile_update(
                ghost, proposal_model(None, payload())
            ),
            "session_not_found",
        )
        await assert_rejected(
            "缺失会话保存",
            fixture.business.save_profile_update(
                ghost, fixture.save_arguments(probe)
            ),
            "session_not_found",
        )
        return {
            "repeat": repeated,
            "duplicate_confirmation": duplicate_message,
            "edited_saved": edited_saved,
            "cross_session": "profile_access_denied",
            "ghost_session": "session_not_found",
        }
    finally:
        await fixture.close()


async def check_status_branches(root: Path) -> dict:
    fixture = await Fixture(root / "status-branches.db").seeded()
    try:
        session_id = await fixture.session()

        # processing：确认关联已提交、保存事务未落地。
        processing = await propose(
            fixture, session_id, prepare_arguments(None, payload())
        )
        async with fixture.repository.transaction():
            await fixture.repository.begin_save(
                processing["proposal"], processing["confirmation"]
            )
        assert await fixture.status_of(processing["proposal"]) == "processing"
        detail = await assert_rejected(
            "处理中保存",
            fixture.service_save(processing),
            "profile_update_processing",
        )
        assert "errors" not in detail
        checking = await fixture.tool_status(processing)
        assert checking.is_error is False
        assert envelope(checking) == {
            "proposal_id": processing["proposal"],
            "status": "processing",
            "result": None,
        }
        baseline = await fixture.profile_state()
        assert baseline == {"version": None, "profile_rows": 0, "record_rows": 0}

        # invalidated：手工置位。
        invalidated = await propose(
            fixture, session_id, prepare_arguments(None, payload(goal="力量"))
        )
        async with fixture.repository.transaction():
            await fixture.repository.set_snapshot_status(
                invalidated["proposal"], "invalidated"
            )
        await assert_rejected(
            "失效保存",
            fixture.service_save(invalidated),
            "profile_proposal_invalidated",
        )

        # conflicted：手工置位。
        conflicted = await propose(
            fixture, session_id, prepare_arguments(None, payload(goal="耐力"))
        )
        async with fixture.repository.transaction():
            await fixture.repository.set_snapshot_status(
                conflicted["proposal"], "conflicted"
            )
        await assert_rejected(
            "冲突保存", fixture.service_save(conflicted), "profile_version_conflict"
        )

        # 快照不存在。
        await assert_rejected(
            "缺失快照保存",
            fixture.service_save(processing, proposal=new_id()),
            "profile_proposal_not_found",
        )
        await assert_rejected(
            "缺失快照查询",
            fixture.business.get_profile_update_status(
                fixture.save_context(processing), new_id()
            ),
            "profile_proposal_not_found",
        )
        rejected = {
            "processing": "profile_update_processing",
            "invalidated": "profile_proposal_invalidated",
            "conflicted": "profile_version_conflict",
            "not_found": "profile_proposal_not_found",
        }
        assert await fixture.profile_state() == baseline

        # 版本冲突：准备之后画像版本被其他路径推进。
        stale = await propose(
            fixture, session_id, prepare_arguments(None, payload(goal="维持"))
        )
        async with fixture.repository.transaction():
            await fixture.repository.save_profile(
                ProfileContent.model_validate(payload(goal="他人写入")), 1, now_ms()
            )
        detail = await assert_rejected(
            "提交时版本冲突",
            fixture.service_save(stale),
            "profile_version_conflict",
        )
        assert "errors" not in detail
        assert await fixture.status_of(stale["proposal"]) == "conflicted"
        profile = await fixture.business.get_profile()
        assert profile.version == 1 and profile.content.goal == "他人写入"
        assert await fixture.repository.get_save_record(stale["proposal"]) is None
        assert await count_rows(fixture.database, "profile_save_records") == 0
        assert await count_rows(fixture.database, "profile") == 1
        assert (await fixture.snapshot(stale["proposal"])).display_entry_id == stale["display"]

        # 准备阶段的依据版本同样拒绝。
        await assert_rejected(
            "准备阶段版本冲突",
            fixture.business.prepare_profile_update(
                fixture.save_context(stale), proposal_model(2, payload())
            ),
            "profile_version_conflict",
        )
        assert (await fixture.business.get_profile()).version == 1
        return {"rejected": rejected, "state": baseline, "conflicted": detail}
    finally:
        await fixture.close()


async def check_interrupted_save(root: Path) -> dict:
    fixture = await Fixture(root / "interrupted.db").seeded()
    try:
        session_id = await fixture.session()
        ids = await propose(fixture, session_id, prepare_arguments(None, payload()))
        proposal_id = ids["proposal"]
        saved_at = now_ms()
        content = ProfileContent.model_validate(payload())
        record = ProfileSaveRecord(
            proposal_id=proposal_id,
            session_id=session_id,
            profile_id=1,
            display_entry_id=ids["display"],
            confirmation_entry_id=ids["confirmation"],
            result=ProfileSaveResult(
                proposal_id=proposal_id,
                profile_id=1,
                version=1,
                content=content,
                saved_at=saved_at,
            ),
            saved_at=saved_at,
        )

        # 确认关联先落地，保存事务在提交前中断。
        async with fixture.repository.transaction():
            await fixture.repository.begin_save(proposal_id, ids["confirmation"])

        async def broken_save() -> None:
            async with fixture.repository.transaction():
                await fixture.repository.begin_save(proposal_id, ids["confirmation"])
                await fixture.repository.save_profile(content, 1, saved_at)
                await fixture.repository.complete_save(record)
                raise RuntimeError("模拟保存事务提交前中断")

        interrupted = None
        try:
            await broken_save()
        except RuntimeError as error:
            interrupted = type(error).__name__
        assert interrupted == "RuntimeError"
        assert (await fixture.business.get_profile()).version is None
        assert await fixture.repository.get_save_record(proposal_id) is None
        assert await fixture.status_of(proposal_id) == "processing"

        # 重启：lifespan 等价恢复把未提交的保存退回待确认并保留确认绑定。
        fixture = await fixture.restart()
        recovered = await fixture.snapshot(proposal_id)
        assert recovered.status == "pending"
        assert recovered.confirmation_entry_id == ids["confirmation"]
        assert recovered.display_entry_id == ids["display"]

        # 按原快照与原确认绑定重试保存成功。
        retried = envelope(await fixture.tool_save(ids))
        assert retried["proposal_id"] == proposal_id
        assert retried["version"] == 1
        assert retried["content"] == payload()
        assert retried["saved_at"] >= saved_at
        assert (await fixture.business.get_profile()).version == 1
        assert await fixture.status_of(proposal_id) == "saved"
        stored = await fixture.repository.get_save_record(proposal_id)
        assert stored is not None and stored.result.model_dump() == retried
        assert await fixture.repository.recover_interrupted_saves() == 0
        return {
            "interrupted": interrupted,
            "recovered": [recovered.status, recovered.confirmation_entry_id],
            "retry": retried,
        }
    finally:
        await fixture.close()


async def check_regenerate_and_removal(root: Path) -> dict:
    fixture = await Fixture(root / "regenerate-removal.db").seeded()
    try:
        session_id = await fixture.session()
        kept = await propose(fixture, session_id, prepare_arguments(None, payload()))
        first = envelope(await fixture.tool_save(kept))
        assert first["version"] == 1
        assert await fixture.business.list_display_bindings(session_id) == {
            kept["display"]: kept["proposal"]
        }

        # 重新生成保存回复：确认用户节点保留，幂等记录仍可定位原快照。
        reply = await fixture.user_turn(session_id, "刚才的画像已经生效")
        regenerated = await fixture.service.accept_regenerate(
            RegenerateCommand(
                operation_id=new_id(),
                session_id=session_id,
                request=RegenerateRequest(target_entry_id=reply),
            )
        )
        await fixture.service.finish_run(session_id, regenerated.run.id, "completed")
        record = await fixture.repository.find_save_record_by_confirmation(
            session_id, kept["confirmation"]
        )
        assert record is not None and record.proposal_id == kept["proposal"]
        status = await fixture.service_status(kept)
        assert status.status == "saved"
        assert status.result.model_dump() == first
        assert await fixture.status_of(kept["proposal"]) == "saved"
        # 运行上下文按此映射在确认节点上投影 proposal_id。
        assert await fixture.business.list_confirmation_bindings(session_id) == {
            kept["confirmation"]: kept["proposal"]
        }

        # 新快照保存成功前，已保存画像保持不变。
        second = await propose(
            fixture, session_id, prepare_arguments(1, payload(goal="力量"))
        )
        state = await fixture.profile_state()
        assert state == {"version": 1, "profile_rows": 1, "record_rows": 1}
        detail = await assert_rejected(
            "新快照沿用旧确认消息",
            fixture.service_save(second, confirmation=kept["confirmation"]),
            "profile_confirmation_invalid",
        )
        assert "errors" not in detail
        assert await fixture.profile_state() == state
        second_saved = envelope(await fixture.tool_save(second))
        assert second_saved["version"] == 2
        assert (await fixture.business.get_profile()).version == 2

        # 会话删除：快照全部清理，画像与幂等记录保留。
        pending = await propose(
            fixture,
            session_id,
            prepare_arguments(2, payload(goal="待清理")),
            confirmation_text=None,
        )
        assert len(await fixture.repository.list_snapshots(session_id)) == 3
        await fixture.service.delete_session(session_id)
        assert await fixture.repository.list_snapshots(session_id) == []
        assert (await fixture.business.get_profile()).version == 2
        assert await count_rows(fixture.database, "profile") == 1
        assert await count_rows(fixture.database, "profile_save_records") == 2
        survived = await fixture.repository.get_save_record(kept["proposal"])
        assert survived.result.model_dump() == first
        located = await fixture.repository.find_save_record_by_confirmation(
            session_id, kept["confirmation"]
        )
        assert located is not None and located.proposal_id == kept["proposal"]

        # 删除后调用原会话工具：按会话不存在拒绝，快照缺失不掩盖会话缺失。
        detail = await assert_rejected(
            "删除后查询",
            fixture.business.get_profile_update_status(
                fixture.save_context(kept), kept["proposal"]
            ),
            "session_not_found",
        )
        assert "errors" not in detail
        await assert_rejected(
            "删除后保存",
            fixture.business.save_profile_update(
                fixture.save_context(kept), fixture.save_arguments(kept)
            ),
            "session_not_found",
        )
        await assert_rejected(
            "删除后查询未保存快照",
            fixture.business.get_profile_update_status(
                fixture.save_context(pending), pending["proposal"]
            ),
            "session_not_found",
        )
        return {
            "regenerated_status": status.status,
            "kept_version_before_second_save": 1,
            "second_saved": second_saved,
            "after_delete": {"snapshots": 0, "profile_version": 2, "records": 2},
            "codes": ["session_not_found", "session_not_found", "session_not_found"],
        }
    finally:
        await fixture.close()


async def check_tool_envelopes(root: Path) -> dict:
    fixture = await Fixture(root / "tool-envelope.db").seeded()
    try:
        session_id = await fixture.session()
        ids = await propose(fixture, session_id, prepare_arguments(None, payload()))
        envelope(await fixture.tool_save(ids))
        context = fixture.save_context(ids)

        failures: dict[str, dict] = {}

        async def failing(name: str, arguments: dict) -> dict:
            message = await fixture.invoke(name, arguments, context)
            assert message.is_error is True, message_text(message)
            assert message.role == "toolResult"
            body = envelope(message)
            assert set(body) == {"code", "message"}, body
            assert isinstance(body["code"], str) and isinstance(body["message"], str)
            return body

        failures["prepare"] = await failing(
            PREPARE, prepare_arguments(99, payload(goal="冲突"))
        )
        assert failures["prepare"]["code"] == "profile_version_conflict"
        failures["save"] = await failing(
            SAVE,
            {
                "proposal_id": new_id(),
                "display_entry_id": ids["display"],
                "confirmation_entry_id": ids["confirmation"],
            },
        )
        assert failures["save"]["code"] == "profile_proposal_not_found"
        failures["status"] = await failing(STATUS, {"proposal_id": new_id()})
        assert failures["status"]["code"] == "profile_proposal_not_found"

        # 画像内容非法：目录外器械，返回字段级错误。
        message = await fixture.invoke(
            PREPARE,
            prepare_arguments(1, payload(unavailable_equipment=["不存在的器械"])),
            context,
        )
        assert message.is_error is True
        body = envelope(message)
        assert set(body) == {"code", "message", "errors"}, body
        assert body["code"] == "invalid_business_payload"
        assert len(body["errors"]) == 1
        assert set(body["errors"][0]) == {"path", "message"}
        assert body["errors"][0]["path"] == "/payload/unavailable_equipment/0"

        # 目录外动作 ID 同样落到字段路径。
        error = await rejection(
            fixture.business.prepare_profile_update(
                context, proposal_model(1, payload(forbidden_exercise_ids=["9999"]))
            )
        )
        assert error.code == "invalid_business_payload"
        assert error.detail()["errors"][0]["path"] == "/payload/forbidden_exercise_ids/0"

        # 空白文本字段由工具参数 schema 拒绝，结果仍是 is_error 单块消息。
        blank = await fixture.invoke(
            PREPARE, prepare_arguments(1, payload(goal="   ")), context
        )
        assert blank.is_error is True
        assert len(blank.content) == 1

        # 成功协议：目录检索与画像读取保持完整结果。
        searched = await fixture.invoke(
            SEARCH_EXERCISES, {"name": "卧推", "equipment": "barbell"}, context
        )
        assert searched.is_error is False
        exercises = json.loads(message_text(searched))
        assert exercises and set(exercises[0]) == {
            "id",
            "name",
            "body_part",
            "equipment",
            "target",
            "muscle_group",
            "secondary_muscles",
            "load_convention",
            "steps",
        }, exercises[0]
        profile = await fixture.invoke(GET_PROFILE, {}, context)
        assert profile.is_error is False
        assert profile.role == "toolResult"
        assert envelope(profile)["version"] == 1
        return {"failures": failures, "payload_errors": body["errors"]}
    finally:
        await fixture.close()


def check_legacy_http() -> dict:
    patch_default_database("profile-confirmation")
    with Server(app) as server, client(server.base_url) as http:
        loop = app.state.loop
        service: SessionService = app.state.session_service
        business: BusinessService = app.state.business

        def run(coro):
            return asyncio.run_coroutine_threadsafe(coro, loop).result()

        session_id = new_id()
        run(service.create_session_result(session_id, "画像 HTTP 会话"))
        outcome = run(
            service.accept_send(
                SendCommand(
                    operation_id=new_id(),
                    session_id=session_id,
                    request=SendRequest(text="请整理我的画像"),
                ),
                system_message=SYSTEM,
            )
        )
        run_id = outcome.run.id
        request_id = outcome.run.request_entry_id
        call_id = "call-" + uuid4().hex[:8]
        arguments = prepare_arguments(None, payload())
        source_id = new_id()
        run(
            service.append_entry(
                SessionMessageEntry(
                    session_id=session_id,
                    id=source_id,
                    parent_id=request_id,
                    run_id=run_id,
                    type="message",
                    messages=[assistant_prepare_call(call_id, arguments, now_ms())],
                    created_at=now_ms(),
                )
            )
        )
        context = BusinessContext(
            timezone="Asia/Shanghai",
            business_date=business_date(now_ms()),
            session_id=session_id,
            run_id=run_id,
            request_entry_id=request_id,
            source_entry_id=source_id,
        )
        prepared: dict[str, str] = {}
        tools = bind_business_tools(business, context, run, prepared)
        message = asyncio.run(
            run_tool_call(
                ToolCall(type="toolCall", id=call_id, name=PREPARE, arguments=arguments),
                tools={PREPARE: tools[PREPARE]},
                declared={PREPARE: DECLARED[PREPARE]},
            )
        )
        assert message.is_error is False, message_text(message)
        assert prepared == {call_id: envelope(message)["proposal_id"]}
        display_id = new_id()
        run(
            service.append_entry(
                SessionMessageEntry(
                    session_id=session_id,
                    id=display_id,
                    parent_id=source_id,
                    run_id=run_id,
                    type="message",
                    messages=[message],
                    created_at=now_ms(),
                )
            )
        )
        proposal = envelope(message)["proposal_id"]
        run(business.bind_display_entry(proposal, display_id))
        run(service.finish_run(session_id, run_id, "completed"))
        confirm = run(
            service.accept_send(
                SendCommand(
                    operation_id=new_id(),
                    session_id=session_id,
                    request=SendRequest(text="确认保存这份画像"),
                ),
                system_message=SYSTEM,
            )
        )
        run(service.finish_run(session_id, confirm.run.id, "completed"))
        saved = run(
            business.save_profile_update(
                context,
                ProfileSaveArguments(
                    proposal_id=proposal,
                    display_entry_id=display_id,
                    confirmation_entry_id=confirm.run.request_entry_id,
                ),
            )
        )
        assert saved.version == 1 and saved.content.model_dump() == payload()

        legacy_get = http.get(f"/api/confirmations/{new_id()}")
        legacy_post = http.post(f"/api/confirmations/{new_id()}/commit")
        assert legacy_get.status_code == 404, legacy_get.text
        assert legacy_post.status_code == 404, legacy_post.text

        history = http.get(f"/api/sessions/{session_id}/history")
        assert history.status_code == 200, history.text
        body = history.json()
        assert set(body) == {"session", "entries", "runs", "steering"}, set(body)
        assert "confirmation" not in json.dumps(body, ensure_ascii=False).lower()

        profile = http.get("/api/profile")
        assert profile.status_code == 200
        assert profile.json() == {"version": 1, "content": payload()}
        return {
            "legacy_get": {"status": legacy_get.status_code, "body": legacy_get.json()},
            "legacy_post": {
                "status": legacy_post.status_code,
                "body": legacy_post.json(),
            },
            "history_keys": sorted(body),
            "profile": profile.json(),
            "proposal": proposal,
        }


def check() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    assert set(DECLARED) == {
        GET_PROFILE,
        SEARCH_EXERCISES,
        PREPARE,
        SAVE,
        STATUS,
    }
    evidence: dict = {}
    with TemporaryDirectory(dir=EVIDENCE, ignore_cleanup_errors=True) as directory:
        root = Path(directory)
        evidence["first_build"] = asyncio.run(check_first_build(root))
        evidence["display_binding"] = asyncio.run(check_display_binding(root))
        evidence["confirmation_binding"] = asyncio.run(check_confirmation_binding(root))
        evidence["status_branches"] = asyncio.run(check_status_branches(root))
        evidence["interrupted_save"] = asyncio.run(check_interrupted_save(root))
        evidence["regenerate_removal"] = asyncio.run(check_regenerate_and_removal(root))
        evidence["tool_envelopes"] = asyncio.run(check_tool_envelopes(root))
    evidence["legacy_http"] = check_legacy_http()
    (EVIDENCE / "profile-confirmation.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(
        "PASS: 首次建档与更新快照流程、展示与确认绑定校验、时序与角色拒绝、确认消息唯一绑定与"
        "重复保存幂等、跨会话与缺失会话拒绝、状态分支拒绝、版本冲突、保存中断与重启恢复、"
        "重新生成保留幂等记录、会话删除保留画像、旧卡片接口移除、工具错误信封与字段级错误"
    )


if __name__ == "__main__":
    check()
