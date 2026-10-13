import asyncio
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from uuid import uuid4

import aiosqlite
from pydantic import TypeAdapter, ValidationError

from app.agent.message_context import AgentMessage
from app.ai.messages import AssistantMessage, Message, SystemMessage, TextContent, Usage
from app.application.session.service import SessionService
from app.domain.session import models as models_module
from app.domain.session.branch import build_session_path
from app.domain.session.errors import EntryNotFound, SessionConflict
from app.domain.session.models import (
    BUSINESS_CONTEXT_TAG,
    BusinessContextDelta,
    BusinessContextEntry,
    BusinessContextMessage,
    BusinessContextSnapshot,
    CustomMessage,
    CustomMessageEntry,
    Model,
    RegenerateCommand,
    RegenerateRequest,
    RemovedPendingProposal,
    RequestContext,
    SendCommand,
    SendRequest,
    SessionEntry,
    SessionMessageEntry,
    build_business_context_entry,
    business_context_content,
    validate_business_context_projection,
)
from app.infrastructure.persistence.sqlite.database import (
    _FOREIGN_KEY_REBUILDS,
    _MIGRATIONS,
    SCHEMA_VERSION,
    apply_schema,
    open_database,
)
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from app.interfaces.http import app, model_visible_entries
from test.regression_support import (
    Server,
    client,
    patch_default_database,
    temporary_root,
)

EVIDENCE = temporary_root("business-context-store")

USAGE = Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2)
BUSINESS_DATE = "2026-10-12"
CREATED_AT = 1786766400000


def uid() -> str:
    return str(uuid4())


def system_message() -> SystemMessage:
    return SystemMessage(
        role="system", content="系统提示词，仅后端可见。", tools_added=[], timestamp=1
    )


def assistant_message(timestamp: int) -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=[TextContent(type="text", text="回答", text_signature="sig")],
        api="openai-completions",
        provider="example",
        model="model-1",
        usage=USAGE,
        stop_reason="stop",
        timestamp=timestamp,
    )


def snapshot_details(request_entry_id: str) -> BusinessContextSnapshot:
    return BusinessContextSnapshot(
        mode="snapshot",
        request=RequestContext(
            timezone="Asia/Shanghai",
            business_date=BUSINESS_DATE,
            request_entry_id=request_entry_id,
        ),
        message_nodes=[
            {
                "entry_id": request_entry_id,
                "role": "user",
                "confirmation": None,
                "proposal": None,
            }
        ],
        pending_proposals=[],
    )


def delta_details(
    tool_entry_id: str, proposal_id: str, request_entry_id: str, source_entry_id: str
) -> BusinessContextDelta:
    return BusinessContextDelta(
        mode="delta",
        request=None,
        upsert_message_nodes=[
            {
                "entry_id": tool_entry_id,
                "role": "toolResult",
                "tool_name": "prepare_profile_update",
                "tool_call_id": "call_prepare_1",
                "proposal": {"business_kind": "profile", "proposal_id": proposal_id},
            }
        ],
        remove_message_node_ids=[],
        upsert_pending_proposals=[
            {
                "business_kind": "profile",
                "proposal_id": proposal_id,
                "status": "pending",
                "request_entry_id": request_entry_id,
                "source_entry_id": source_entry_id,
                "display_entry_id": tool_entry_id,
                "confirmation_entry_id": None,
            }
        ],
        remove_pending_proposals=[],
    )


def rejects(build, fragment: str) -> None:
    try:
        build()
    except ValidationError as error:
        assert fragment in str(error), (fragment, str(error))
        return
    raise AssertionError(f"未拒绝非法输入: {fragment}")


def rejects_value(build, fragment: str) -> None:
    # 投影一致性检查在创建与写入处直接抛出 ValueError，不属于模型字段校验。
    try:
        build()
    except ValueError as error:
        assert fragment in str(error), (fragment, str(error))
        return
    raise AssertionError(f"未拒绝非法输入: {fragment}")


def role_entry_id(entries, role: str, run_id: str | None = None) -> str:
    return next(
        entry.id
        for entry in entries
        if isinstance(entry, SessionMessageEntry)
        and entry.messages[0].role == role
        and (run_id is None or entry.run_id == run_id)
    )


def content_payload(entry: BusinessContextEntry) -> dict:
    body = entry.message.content.split(f"<{BUSINESS_CONTEXT_TAG}>", 1)[1]
    body = body.split(f"</{BUSINESS_CONTEXT_TAG}>", 1)[0]
    return json.loads(body)


def check_types() -> dict:
    adapter = TypeAdapter(SessionEntry)
    session_id, node_id, parent_id, run_id = uid(), uid(), uid(), uid()
    details = snapshot_details(uid())
    entry = build_business_context_entry(
        session_id=session_id,
        id=node_id,
        parent_id=parent_id,
        run_id=run_id,
        details=details,
        created_at=CREATED_AT,
    )
    assert entry.type == "custom_message"
    assert entry.run_id == run_id
    assert entry.message.role == "custom"
    assert entry.message.custom_type == "business_context"
    assert entry.message.display is False
    assert entry.message.timestamp == entry.created_at == CREATED_AT
    assert entry.message.content == business_context_content(details)
    assert content_payload(entry) == details.model_dump()
    round_tripped = adapter.validate_python(
        entry.model_dump(exclude_unset=True), strict=True
    )
    assert isinstance(round_tripped, BusinessContextEntry)
    assert round_tripped == entry

    delta = delta_details(uid(), uid(), uid(), uid())
    delta_entry = build_business_context_entry(
        session_id=session_id,
        id=uid(),
        parent_id=node_id,
        run_id=run_id,
        details=delta,
        created_at=CREATED_AT + 1,
    )
    assert content_payload(delta_entry) == delta.model_dump()
    assert (
        adapter.validate_python(delta_entry.model_dump(exclude_unset=True), strict=True)
        == delta_entry
    )

    # 通用泛型基类按 TData / TMessage 参数化：自定义资料类型可直接复用信封与节点。
    class SampleData(Model):
        value: int

    class SampleMessage(CustomMessage[SampleData]):
        custom_type: Literal["sample"]

    class SampleEntry(CustomMessageEntry[SampleMessage]):
        pass

    sample = SampleEntry(
        session_id=session_id,
        id=uid(),
        parent_id=None,
        run_id=None,
        type="custom_message",
        message=SampleMessage(
            role="custom",
            custom_type="sample",
            content="内部文本",
            display=True,
            details=SampleData(value=1),
            timestamp=7,
        ),
        created_at=7,
    )
    sample_adapter = TypeAdapter(SampleEntry)
    assert (
        sample_adapter.validate_python(sample.model_dump(exclude_unset=True), strict=True)
        == sample
    )
    rejects(
        lambda: SampleMessage(role="custom", custom_type="other", content="x", display=True, timestamp=7),
        "custom_type",
    )
    rejects(
        lambda: BusinessContextEntry(
            session_id=session_id,
            id=uid(),
            parent_id=None,
            run_id=None,
            type="custom_message",
            message=BusinessContextMessage(
                role="custom",
                custom_type="business_context",
                content=business_context_content(details),
                display=False,
                details=details,
                timestamp=7,
            ),
            created_at=7,
        ),
        "run_id",
    )
    # 投影一致性只在创建与写入处核对，模型本身不按当前模板重新计算 content。
    forged = BusinessContextMessage(
        role="custom",
        custom_type="business_context",
        content="伪造文本",
        display=False,
        details=details,
        timestamp=CREATED_AT,
    )
    rejects_value(
        lambda: validate_business_context_projection(forged),
        "content 必须为 details 的固定序列化投影",
    )
    assert validate_business_context_projection(entry.message) is None
    # 消息层联合登记内部资料消息，服务商协议 Message 联合保持原样。
    agent_adapter = TypeAdapter(AgentMessage)
    agent_message = agent_adapter.validate_python(
        entry.message.model_dump(exclude_unset=True), strict=True
    )
    assert isinstance(agent_message, BusinessContextMessage)
    assert agent_message == entry.message
    try:
        TypeAdapter(Message).validate_python(
            entry.message.model_dump(exclude_unset=True), strict=True
        )
    except ValidationError:
        pass
    else:
        raise AssertionError("服务商 Message 联合接受了内部资料消息")
    rejects(
        lambda: BusinessContextEntry(
            session_id=session_id,
            id=uid(),
            parent_id=None,
            run_id=run_id,
            type="custom_message",
            message=BusinessContextMessage(
                role="custom",
                custom_type="business_context",
                content=business_context_content(details),
                display=False,
                details=details,
                timestamp=CREATED_AT,
            ),
            created_at=CREATED_AT + 5,
        ),
        "消息时间必须与节点时间一致",
    )
    rejects(
        lambda: BusinessContextMessage(
            role="custom",
            custom_type="business_context",
            content=business_context_content(details),
            display=True,
            details=details,
            timestamp=CREATED_AT,
        ),
        "display",
    )
    rejects(
        lambda: BusinessContextSnapshot(
            mode="snapshot",
            request=RequestContext(
                timezone="Asia/Shanghai",
                business_date=BUSINESS_DATE,
                request_entry_id=uid(),
            ),
            message_nodes=[],
            pending_proposals=[],
            extra=1,
        ),
        "extra",
    )
    return {
        "content": entry.message.content,
        "public_keys": sorted(
            {
                "type",
                "entry_id",
                "parent_id",
                "run_id",
                "created_at",
            }
        ),
    }


def check_payload_rules() -> None:
    request_entry_id, source_entry_id, tool_entry_id = uid(), uid(), uid()
    proposal_id, other_proposal_id = uid(), uid()

    rejects(
        lambda: BusinessContextDelta(
            mode="delta",
            request=None,
            upsert_message_nodes=[],
            remove_message_node_ids=[],
            upsert_pending_proposals=[],
            remove_pending_proposals=[],
        ),
        "空增量拒绝入库",
    )
    rejects(
        lambda: BusinessContextDelta(
            mode="delta",
            request=None,
            upsert_message_nodes=[
                {
                    "entry_id": tool_entry_id,
                    "role": "assistant",
                    "tool_call_ids": [],
                },
                {
                    "entry_id": tool_entry_id,
                    "role": "assistant",
                    "tool_call_ids": [],
                },
            ],
            remove_message_node_ids=[],
            upsert_pending_proposals=[],
            remove_pending_proposals=[],
        ),
        "重复 entry_id",
    )
    rejects(
        lambda: BusinessContextDelta(
            mode="delta",
            request=None,
            upsert_message_nodes=[
                {
                    "entry_id": tool_entry_id,
                    "role": "assistant",
                    "tool_call_ids": [],
                }
            ],
            remove_message_node_ids=[tool_entry_id, tool_entry_id],
            upsert_pending_proposals=[],
            remove_pending_proposals=[],
        ),
        "重复 entry_id",
    )
    rejects(
        lambda: BusinessContextDelta(
            mode="delta",
            request=None,
            upsert_message_nodes=[
                {
                    "entry_id": tool_entry_id,
                    "role": "assistant",
                    "tool_call_ids": [],
                }
            ],
            remove_message_node_ids=[tool_entry_id],
            upsert_pending_proposals=[],
            remove_pending_proposals=[],
        ),
        "同时出现在新增与移除集合",
    )
    rejects(
        lambda: BusinessContextDelta(
            mode="delta",
            request=None,
            upsert_message_nodes=[],
            remove_message_node_ids=[],
            upsert_pending_proposals=[
                {
                    "business_kind": "profile",
                    "proposal_id": proposal_id,
                    "status": "pending",
                    "request_entry_id": request_entry_id,
                    "source_entry_id": source_entry_id,
                    "display_entry_id": tool_entry_id,
                    "confirmation_entry_id": None,
                },
                {
                    "business_kind": "profile",
                    "proposal_id": proposal_id,
                    "status": "pending",
                    "request_entry_id": request_entry_id,
                    "source_entry_id": source_entry_id,
                    "display_entry_id": tool_entry_id,
                    "confirmation_entry_id": None,
                },
            ],
            remove_pending_proposals=[],
        ),
        "重复键",
    )
    rejects(
        lambda: BusinessContextDelta(
            mode="delta",
            request=None,
            upsert_message_nodes=[],
            remove_message_node_ids=[],
            upsert_pending_proposals=[
                {
                    "business_kind": "profile",
                    "proposal_id": proposal_id,
                    "status": "pending",
                    "request_entry_id": request_entry_id,
                    "source_entry_id": source_entry_id,
                    "display_entry_id": tool_entry_id,
                    "confirmation_entry_id": None,
                }
            ],
            remove_pending_proposals=[
                {
                    "business_kind": "profile",
                    "proposal_id": proposal_id,
                    "reason": "invalidated",
                }
            ],
        ),
        "同时出现在新增与移除集合",
    )
    # 不同业务类型的相同 proposal_id 是两个独立键，不构成重复。
    mixed = BusinessContextDelta(
        mode="delta",
        request=None,
        upsert_message_nodes=[],
        remove_message_node_ids=[],
        upsert_pending_proposals=[
            {
                "business_kind": "profile",
                "proposal_id": proposal_id,
                "status": "pending",
                "request_entry_id": request_entry_id,
                "source_entry_id": source_entry_id,
                "display_entry_id": tool_entry_id,
                "confirmation_entry_id": None,
            },
            {
                "business_kind": "plan",
                "proposal_id": proposal_id,
                "status": "pending",
                "request_entry_id": request_entry_id,
                "source_entry_id": source_entry_id,
                "display_entry_id": tool_entry_id,
                "confirmation_entry_id": None,
            },
        ],
        remove_pending_proposals=[
            {
                "business_kind": "workout",
                "proposal_id": other_proposal_id,
                "reason": "saved",
            }
        ],
    )
    assert len(mixed.upsert_pending_proposals) == 2
    rejects(
        lambda: RemovedPendingProposal(
            business_kind="profile", proposal_id=proposal_id, reason="unknown"
        ),
        "reason",
    )
    rejects(
        lambda: BusinessContextDelta(
            mode="delta",
            request=None,
            upsert_message_nodes=[],
            remove_message_node_ids=[],
            upsert_pending_proposals=[
                {
                    "business_kind": "profile",
                    "proposal_id": proposal_id,
                    "status": "processing",
                    "request_entry_id": request_entry_id,
                    "source_entry_id": source_entry_id,
                    "display_entry_id": None,
                    "confirmation_entry_id": None,
                }
            ],
            remove_pending_proposals=[],
        ),
        "processing 提案必须同时绑定展示与确认节点",
    )
    rejects(
        lambda: BusinessContextDelta(
            mode="delta",
            request=None,
            upsert_message_nodes=[],
            remove_message_node_ids=[],
            upsert_pending_proposals=[
                {
                    "business_kind": "profile",
                    "proposal_id": proposal_id,
                    "status": "pending",
                    "request_entry_id": request_entry_id,
                    "source_entry_id": source_entry_id,
                    "display_entry_id": None,
                    "confirmation_entry_id": uid(),
                }
            ],
            remove_pending_proposals=[],
        ),
        "确认节点有值时展示节点必须有值",
    )
    # processing 且展示、确认均已绑定通过校验。
    assert (
        BusinessContextDelta(
            mode="delta",
            request=None,
            upsert_message_nodes=[],
            remove_message_node_ids=[],
            upsert_pending_proposals=[
                {
                    "business_kind": "workout",
                    "proposal_id": proposal_id,
                    "status": "processing",
                    "request_entry_id": request_entry_id,
                    "source_entry_id": source_entry_id,
                    "display_entry_id": tool_entry_id,
                    "confirmation_entry_id": uid(),
                }
            ],
            remove_pending_proposals=[],
        ).upsert_pending_proposals[0].status
        == "processing"
    )
    # processing 但展示已绑定、确认为 null：必须拒绝。
    rejects(
        lambda: BusinessContextDelta(
            mode="delta",
            request=None,
            upsert_message_nodes=[],
            remove_message_node_ids=[],
            upsert_pending_proposals=[
                {
                    "business_kind": "profile",
                    "proposal_id": proposal_id,
                    "status": "processing",
                    "request_entry_id": request_entry_id,
                    "source_entry_id": source_entry_id,
                    "display_entry_id": tool_entry_id,
                    "confirmation_entry_id": None,
                }
            ],
            remove_pending_proposals=[],
        ),
        "processing 提案必须同时绑定展示与确认节点",
    )
    # 完整基线同样拒绝重复键：节点按 entry_id，提案按 (business_kind, proposal_id)。
    rejects(
        lambda: BusinessContextSnapshot(
            mode="snapshot",
            request=RequestContext(
                timezone="Asia/Shanghai",
                business_date=BUSINESS_DATE,
                request_entry_id=request_entry_id,
            ),
            message_nodes=[
                {"entry_id": tool_entry_id, "role": "assistant", "tool_call_ids": []},
                {"entry_id": tool_entry_id, "role": "assistant", "tool_call_ids": []},
            ],
            pending_proposals=[],
        ),
        "基线节点记录存在重复 entry_id",
    )
    duplicate_proposal = {
        "business_kind": "profile",
        "proposal_id": proposal_id,
        "status": "pending",
        "request_entry_id": request_entry_id,
        "source_entry_id": source_entry_id,
        "display_entry_id": tool_entry_id,
        "confirmation_entry_id": None,
    }
    rejects(
        lambda: BusinessContextSnapshot(
            mode="snapshot",
            request=RequestContext(
                timezone="Asia/Shanghai",
                business_date=BUSINESS_DATE,
                request_entry_id=request_entry_id,
            ),
            message_nodes=[],
            pending_proposals=[duplicate_proposal, duplicate_proposal],
        ),
        "基线提案记录存在重复键",
    )
    # 不同业务类型的相同 proposal_id 在基线中也是两个独立键。
    baseline = BusinessContextSnapshot(
        mode="snapshot",
        request=RequestContext(
            timezone="Asia/Shanghai",
            business_date=BUSINESS_DATE,
            request_entry_id=request_entry_id,
        ),
        message_nodes=[
            {"entry_id": request_entry_id, "role": "user", "confirmation": None, "proposal": None}
        ],
        pending_proposals=[
            duplicate_proposal,
            {**duplicate_proposal, "business_kind": "plan"},
        ],
    )
    assert len(baseline.pending_proposals) == 2
    rejects(
        lambda: BusinessContextSnapshot(
            mode="snapshot",
            request=RequestContext(
                timezone="Asia/Shanghai",
                business_date="2026-02-30",
                request_entry_id=request_entry_id,
            ),
            message_nodes=[],
            pending_proposals=[],
        ),
        "business_date",
    )
    rejects(
        lambda: RequestContext(
            timezone="Asia/Shanghai",
            business_date="2026/10/12",
            request_entry_id=request_entry_id,
        ),
        "business_date",
    )
    rejects(
        lambda: RequestContext(
            timezone="UTC",
            business_date=BUSINESS_DATE,
            request_entry_id=request_entry_id,
        ),
        "timezone",
    )
    rejects(
        lambda: BusinessContextSnapshot(
            mode="snapshot",
            request=None,
            message_nodes=[],
            pending_proposals=[],
        ),
        "request",
    )
    rejects(
        lambda: BusinessContextSnapshot(
            mode="snapshot",
            request=RequestContext(
                timezone="Asia/Shanghai",
                business_date=BUSINESS_DATE,
                request_entry_id="not-a-uuid",
            ),
            message_nodes=[],
            pending_proposals=[],
        ),
        "operation_id",
    )
    rejects(
        lambda: BusinessContextSnapshot(
            mode="snapshot",
            request=RequestContext(
                timezone="Asia/Shanghai",
                business_date=BUSINESS_DATE,
                request_entry_id=request_entry_id,
            ),
            message_nodes=[{"entry_id": tool_entry_id, "role": "system"}],
            pending_proposals=[],
        ),
        "role",
    )
    rejects(
        lambda: BusinessContextSnapshot(
            mode="unknown",
            request=None,
            message_nodes=[],
            pending_proposals=[],
        ),
        "mode",
    )


async def build_v13(path: Path) -> None:
    connection = await aiosqlite.connect(path, isolation_level=None)
    try:
        await connection.execute("PRAGMA foreign_keys = ON")
        for target in range(1, 14):
            rebuild = target in _FOREIGN_KEY_REBUILDS
            if rebuild:
                await connection.execute("PRAGMA foreign_keys = OFF")
                await connection.execute("PRAGMA legacy_alter_table = ON")
            try:
                await apply_schema(
                    connection,
                    _MIGRATIONS[target].read_text(encoding="utf-8"),
                    target,
                )
            finally:
                if rebuild:
                    await connection.execute("PRAGMA legacy_alter_table = OFF")
                    await connection.execute("PRAGMA foreign_keys = ON")
        await connection.execute(
            "INSERT INTO sessions (id, title, active_leaf_id, created_at, updated_at) "
            "VALUES ('old-s', '旧会话', NULL, 1, 1)"
        )
        await connection.execute(
            "INSERT INTO session_entries "
            "(session_id, id, parent_id, run_id, type, messages, created_at) "
            "VALUES ('old-s', 'old-u', NULL, NULL, 'message', ?, 10)",
            (
                json.dumps(
                    [
                        {
                            "role": "user",
                            "content": "旧用户消息",
                            "timestamp": 10,
                        }
                    ],
                    ensure_ascii=False,
                ),
            ),
        )
        await connection.execute(
            "INSERT INTO session_runs "
            "(session_id, id, request_entry_id, last_entry_id, status, started_at, "
            "finished_at, error_code, error_message) "
            "VALUES ('old-s', 'old-r', 'old-u', NULL, 'completed', 10, 20, NULL, NULL)"
        )
        await connection.execute(
            "INSERT INTO session_entries "
            "(session_id, id, parent_id, run_id, type, messages, summary, "
            "first_kept_entry_id, tokens_before, usage, system_message, details, created_at) "
            "VALUES ('old-s', 'old-c', 'old-u', 'old-r', 'compaction', NULL, '旧摘要', "
            "'old-u', 5, ?, ?, NULL, 30)",
            (
                json.dumps(
                    {
                        "input": 1,
                        "output": 1,
                        "cache_read": 0,
                        "cache_write": 0,
                        "total_tokens": 2,
                    }
                ),
                json.dumps({"role": "system", "content": "旧系统", "timestamp": 1}),
            ),
        )
        await connection.execute(
            "UPDATE session_runs SET last_entry_id = 'old-c' WHERE id = 'old-r'"
        )
        await connection.execute(
            "UPDATE sessions SET active_leaf_id = 'old-c' WHERE id = 'old-s'"
        )
    finally:
        await connection.close()


async def check_migration(root: Path) -> dict:
    fresh_path = root / "fresh.db"
    fresh = await open_database(fresh_path)
    try:
        cursor = await fresh.connection.execute("PRAGMA user_version")
        assert (await cursor.fetchone())[0] == SCHEMA_VERSION
        await cursor.close()
        cursor = await fresh.connection.execute(
            "SELECT count(*) FROM session_entries WHERE type = 'custom_message'"
        )
        assert (await cursor.fetchone())[0] == 0
        await cursor.close()
    finally:
        await fresh.close()

    old_path = root / "old.db"
    await build_v13(old_path)
    upgraded = await open_database(old_path)
    try:
        connection = upgraded.connection
        cursor = await connection.execute("PRAGMA user_version")
        assert (await cursor.fetchone())[0] == SCHEMA_VERSION
        await cursor.close()
        cursor = await connection.execute(
            "SELECT type, message, messages FROM session_entries WHERE id = 'old-u'"
        )
        old_user = await cursor.fetchone()
        await cursor.close()
        assert old_user["type"] == "message"
        assert old_user["message"] is None
        assert json.loads(old_user["messages"])[0]["content"] == "旧用户消息"
        cursor = await connection.execute(
            "SELECT type, summary, first_kept_entry_id, tokens_before "
            "FROM session_entries WHERE id = 'old-c'"
        )
        old_compaction = await cursor.fetchone()
        await cursor.close()
        assert old_compaction["type"] == "compaction"
        assert old_compaction["summary"] == "旧摘要"
        assert old_compaction["first_kept_entry_id"] == "old-u"
        assert old_compaction["tokens_before"] == 5
        cursor = await connection.execute(
            "SELECT count(*) FROM session_entries WHERE type = 'custom_message'"
        )
        assert (await cursor.fetchone())[0] == 0
        await cursor.close()
        cursor = await connection.execute("PRAGMA foreign_key_check")
        assert await cursor.fetchall() == []
        await cursor.close()
    finally:
        await upgraded.close()
    return {"schema_version": SCHEMA_VERSION}


async def check_write_and_restore(root: Path) -> dict:
    path = root / "write.db"
    session_id = uid()
    database = await open_database(path)
    service = SessionService(SqliteSessionRepository(database))
    await service.create_session(session_id, "资料节点写入")
    send = await service.accept_send(
        SendCommand(
            operation_id=uid(), session_id=session_id, request=SendRequest(text="你好")
        ),
        system_message=system_message(),
    )
    run = send.run
    await service.append_entry(
        SessionMessageEntry(
            session_id=session_id,
            id=uid(),
            parent_id=run.request_entry_id,
            run_id=run.id,
            type="message",
            messages=[assistant_message(2)],
            created_at=2,
        )
    )
    entries = await service.list_entries(session_id)
    system_id = role_entry_id(entries, "system")
    assistant_id = role_entry_id(entries, "assistant")

    details = snapshot_details(run.request_entry_id)
    node = build_business_context_entry(
        session_id=session_id,
        id=uid(),
        parent_id=assistant_id,
        run_id=run.id,
        details=details,
        created_at=CREATED_AT,
    )
    outcome = await service.append_business_context(node)
    assert outcome.created and not outcome.credential_detected
    assert outcome.run.last_entry_id == node.id
    session = await service.get_session(session_id)
    assert session.active_leaf_id == node.id

    repeated = await service.append_business_context(node)
    assert not repeated.created and repeated.entry == node

    conflicting = build_business_context_entry(
        session_id=session_id,
        id=node.id,
        parent_id=assistant_id,
        run_id=run.id,
        details=details,
        created_at=CREATED_AT + 1,
    )
    try:
        await service.append_business_context(conflicting)
    except SessionConflict as error:
        assert node.id in str(error)
    else:
        raise AssertionError("同 ID 不同载荷未报冲突")

    # 写入处核对固定投影：伪造文本的节点在写入时被拒绝，不落库、不改动运行与分支指针。
    forged = BusinessContextEntry(
        session_id=session_id,
        id=uid(),
        parent_id=node.id,
        run_id=run.id,
        type="custom_message",
        message=BusinessContextMessage(
            role="custom",
            custom_type="business_context",
            content="伪造文本",
            display=False,
            details=details,
            timestamp=CREATED_AT + 2,
        ),
        created_at=CREATED_AT + 2,
    )
    try:
        await service.append_business_context(forged)
    except ValueError as error:
        assert "content 必须为 details 的固定序列化投影" in str(error)
    else:
        raise AssertionError("写入处未核对固定投影")
    stored_ids = [entry.id for entry in await service.list_entries(session_id)]
    assert forged.id not in stored_ids
    assert (await service.get_session(session_id)).active_leaf_id == node.id
    assert (await service.get_run(session_id, run.id)).last_entry_id == node.id

    history = await service.get_session_history(session_id)
    assert [entry.id for entry in history.entries] == [
        system_id,
        run.request_entry_id,
        assistant_id,
        node.id,
    ]
    stored = [entry for entry in history.entries if entry.type == "custom_message"]
    assert len(stored) == 1 and stored[0] == node
    assert history.runs[-1].last_entry_id == node.id

    # 历史内容不可变：节点落库后禁止任何更新。
    try:
        await database.connection.execute(
            "UPDATE session_entries SET message = '{}' WHERE session_id = ? AND id = ?",
            (session_id, node.id),
        )
    except sqlite3.IntegrityError as error:
        assert "不可变" in str(error)
    else:
        raise AssertionError("资料节点允许被更新")
    await database.close()

    reopened = await open_database(path)
    try:
        reloaded = await SqliteSessionRepository(reopened).list_entries(session_id)
        restored = [entry for entry in reloaded if entry.type == "custom_message"]
        assert len(restored) == 1
        assert restored[0] == node
        assert restored[0].message.content == node.message.content
        assert restored[0].message.details == node.message.details
        assert restored[0].parent_id == assistant_id
        assert restored[0].run_id == run.id
        assert restored[0].created_at == CREATED_AT
        assert [entry.id for entry in build_session_path(session_id, reloaded, node.id)] == [
            system_id,
            run.request_entry_id,
            assistant_id,
            node.id,
        ]
    finally:
        await reopened.close()
    return {"entry_id": node.id, "run_id": run.id}


async def check_branch_isolation(root: Path) -> dict:
    path = root / "branch.db"
    session_id = uid()
    database = await open_database(path)
    service = SessionService(SqliteSessionRepository(database))
    await service.create_session(session_id, "分支隔离")
    first = await service.accept_send(
        SendCommand(
            operation_id=uid(), session_id=session_id, request=SendRequest(text="第一")
        ),
        system_message=system_message(),
    )
    run_one = first.run
    user_id = run_one.request_entry_id
    await service.append_entry(
        SessionMessageEntry(
            session_id=session_id,
            id=uid(),
            parent_id=user_id,
            run_id=run_one.id,
            type="message",
            messages=[assistant_message(2)],
            created_at=2,
        )
    )
    entries = await service.list_entries(session_id)
    system_id = role_entry_id(entries, "system")
    assistant_one = role_entry_id(entries, "assistant")
    old_details = snapshot_details(user_id)
    old_node = build_business_context_entry(
        session_id=session_id,
        id=uid(),
        parent_id=assistant_one,
        run_id=run_one.id,
        details=old_details,
        created_at=CREATED_AT,
    )
    await service.append_business_context(old_node)
    await service.finish_run(session_id, run_one.id, "completed")

    regenerated = await service.accept_regenerate(
        RegenerateCommand(
            operation_id=uid(), session_id=session_id, request=RegenerateRequest(target_entry_id=user_id)
        )
    )
    run_two = regenerated.run
    assert run_two.request_entry_id == user_id
    try:
        await service.get_entry(session_id, old_node.id)
    except EntryNotFound:
        pass
    else:
        raise AssertionError("被移除分支的资料节点仍可读取")

    await service.append_entry(
        SessionMessageEntry(
            session_id=session_id,
            id=uid(),
            parent_id=user_id,
            run_id=run_two.id,
            type="message",
            messages=[assistant_message(3)],
            created_at=3,
        )
    )
    entries = await service.list_entries(session_id)
    assistant_two = role_entry_id(entries, "assistant", run_two.id)
    new_node = build_business_context_entry(
        session_id=session_id,
        id=uid(),
        parent_id=assistant_two,
        run_id=run_two.id,
        details=delta_details(assistant_two, uid(), user_id, assistant_two),
        created_at=CREATED_AT + 10,
    )
    await service.append_business_context(new_node)

    history = await service.get_session_history(session_id)
    ids = [entry.id for entry in history.entries]
    assert ids == [system_id, user_id, assistant_two, new_node.id]
    assert old_node.id not in ids
    assert history.session.active_leaf_id == new_node.id
    custom = [entry for entry in history.entries if entry.type == "custom_message"]
    assert [entry.id for entry in custom] == [new_node.id]
    assert custom[0].parent_id == assistant_two
    assert custom[0].run_id == run_two.id
    assert custom[0].message.details.mode == "delta"

    # 祖先链读取：以被移除节点为叶子的路径必须失败，资料节点不跨分支泄漏。
    all_entries = await service.list_entries(session_id)
    path = build_session_path(session_id, all_entries, new_node.id)
    assert [entry.id for entry in path] == [system_id, user_id, assistant_two, new_node.id]
    try:
        build_session_path(session_id, all_entries, old_node.id)
    except ValueError as error:
        assert "未知 leaf" in str(error)
    else:
        raise AssertionError("已移除分支仍可构建路径")

    # 既有消息、压缩与确认路径不受新节点类型影响：当前分支读取仍只返回真实消息节点。
    branch = await service.get_current_branch(session_id)
    assert [entry.id for entry in branch] == [system_id, user_id, assistant_two]
    await database.close()
    return {"kept": new_node.id, "removed": old_node.id}


async def check_message_nodes_boundary(root: Path) -> None:
    # 资料节点参与祖先链读取时，其身份不进入 message_nodes 白名单。
    path = root / "message-nodes.db"
    session_id = uid()
    database = await open_database(path)
    service = SessionService(SqliteSessionRepository(database))
    try:
        await service.create_session(session_id, "资料节点白名单")
        send = await service.accept_send(
            SendCommand(
                operation_id=uid(), session_id=session_id, request=SendRequest(text="白名单")
            ),
            system_message=system_message(),
        )
        run = send.run
        await service.append_entry(
            SessionMessageEntry(
                session_id=session_id,
                id=uid(),
                parent_id=run.request_entry_id,
                run_id=run.id,
                type="message",
                messages=[assistant_message(2)],
                created_at=2,
            )
        )
        entries = await service.list_entries(session_id)
        assistant_id = role_entry_id(entries, "assistant")
        node = build_business_context_entry(
            session_id=session_id,
            id=uid(),
            parent_id=assistant_id,
            run_id=run.id,
            details=snapshot_details(run.request_entry_id),
            created_at=CREATED_AT,
        )
        await service.append_business_context(node)

        # 资料节点是当前分支的叶子，真实参与祖先链读取，并在模型投影中按原位置贡献 user 消息。
        projection = await service.get_projection(session_id, node.id)
        assert [entry.id for entry in projection.path][-1] == node.id
        assert isinstance(projection.path[-1], BusinessContextEntry)
        assert len(projection.context.messages) == len(projection.context.source_entry_ids)
        # 模型消息来源与消息一一对应：资料消息映射到真实资料节点身份。
        visible = set(projection.context.source_entry_ids)
        assert node.id in visible
        # message_nodes 白名单继续只接纳真实消息节点，资料节点身份不进入其中。
        selected = model_visible_entries(projection.path, visible)
        selected_ids = [entry.id for entry in selected]
        assert node.id not in selected_ids
        assert all(isinstance(entry, SessionMessageEntry) for entry in selected)
        # 正向对照：真实消息节点全部保留，资料节点没有挤掉任何资料来源。
        assert selected_ids == [
            entry.id for entry in projection.path if isinstance(entry, SessionMessageEntry)
        ]
        assert set(selected_ids) <= visible
    finally:
        await database.close()


async def check_template_update(root: Path) -> None:
    # 模板更新只影响新事件：旧资料节点按已保存的 content 恢复，不重新计算文本。
    path = root / "template.db"
    session_id = uid()
    database = await open_database(path)
    service = SessionService(SqliteSessionRepository(database))
    await service.create_session(session_id, "模板更新")
    send = await service.accept_send(
        SendCommand(
            operation_id=uid(), session_id=session_id, request=SendRequest(text="模板")
        ),
        system_message=system_message(),
    )
    run = send.run
    await service.append_entry(
        SessionMessageEntry(
            session_id=session_id,
            id=uid(),
            parent_id=run.request_entry_id,
            run_id=run.id,
            type="message",
            messages=[assistant_message(2)],
            created_at=2,
        )
    )
    entries = await service.list_entries(session_id)
    assistant_id = role_entry_id(entries, "assistant")
    details = snapshot_details(run.request_entry_id)
    node = build_business_context_entry(
        session_id=session_id,
        id=uid(),
        parent_id=assistant_id,
        run_id=run.id,
        details=details,
        created_at=CREATED_AT,
    )
    await service.append_business_context(node)
    original = node.message.content
    await database.close()

    original_notice = models_module.BUSINESS_CONTEXT_SOURCE_NOTICE
    models_module.BUSINESS_CONTEXT_SOURCE_NOTICE = "更新后的来源声明。"
    try:
        # 模板已变化：同一 details 现在会生成不同文本。
        assert business_context_content(details) != original
        reopened = await open_database(path)
        try:
            reloaded = await SqliteSessionRepository(reopened).list_entries(session_id)
            restored = [entry for entry in reloaded if entry.type == "custom_message"]
            assert len(restored) == 1
            assert restored[0] == node
            assert restored[0].message.content == original
            assert restored[0].message.details == details
            # 恢复不依赖当前模板：旧文本与新模板投影不同也照常读取。
            assert restored[0].message.content != business_context_content(
                restored[0].message.details
            )
        finally:
            await reopened.close()
    finally:
        models_module.BUSINESS_CONTEXT_SOURCE_NOTICE = original_notice


async def check_boundaries(root: Path) -> None:
    path = root / "boundary.db"
    database = await open_database(path)
    await database.close()
    session_id = "boundary-s"
    connection = sqlite3.connect(path, timeout=30)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute(
            "INSERT INTO sessions (id, title, active_leaf_id, created_at, updated_at) "
            "VALUES (?, '边界', NULL, 1, 1)",
            (session_id,),
        )
        connection.execute(
            "INSERT INTO session_entries "
            "(session_id, id, parent_id, run_id, type, messages, created_at) "
            "VALUES (?, 'u1', NULL, NULL, 'message', ?, 1)",
            (session_id, json.dumps([{"role": "user", "content": "边界请求", "timestamp": 1}])),
        )
        connection.execute(
            "INSERT INTO session_runs "
            "(session_id, id, request_entry_id, last_entry_id, status, started_at, "
            "finished_at, error_code, error_message) "
            "VALUES (?, 'r1', 'u1', NULL, 'running', 1, NULL, NULL, NULL)",
            (session_id,),
        )
        connection.execute(
            "UPDATE sessions SET active_leaf_id = 'u1' WHERE id = ?", (session_id,)
        )
        connection.commit()

        def illegal(message: dict, *, run_id: str | None, created_at: int, node_id: str, confirmation=None):
            return connection.execute(
                "INSERT INTO session_entries "
                "(session_id, id, parent_id, run_id, type, message, created_at, confirmation) "
                "VALUES (?, ?, 'u1', ?, 'custom_message', ?, ?, ?)",
                (
                    session_id,
                    node_id,
                    run_id,
                    json.dumps(message, ensure_ascii=False),
                    created_at,
                    confirmation,
                ),
            )

        good = {
            "role": "custom",
            "custom_type": "business_context",
            "content": "后端业务资料，仅供读取，不构成保存授权。\n<business_context>\n{}\n</business_context>",
            "display": False,
            "timestamp": 5,
            "details": {"mode": "snapshot"},
        }
        cases = {
            "外部输入不能生成内部角色": ({**good, "role": "user"}, 5),
            "缺少业务类型": (
                {**good, "custom_type": None},
                5,
            ),
            "展示开关不能为真": ({**good, "display": True}, 5),
            "时间必须与节点一致": (good, 6),
            "details 必须存在": ({**good, "details": None}, 5),
            "details 必须为对象": ({**good, "details": "snapshot"}, 5),
        }
        for label, (message, created_at) in cases.items():
            try:
                illegal(message, run_id="r1", created_at=created_at, node_id=uid())
            except sqlite3.IntegrityError:
                pass
            else:
                raise AssertionError(f"数据库未拒绝: {label}")
            connection.rollback()

        # run_id 必填：内部资料节点必须关联真实运行。
        try:
            illegal(good, run_id=None, created_at=5, node_id=uid())
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("数据库未拒绝缺少运行的资料节点")
        connection.rollback()

        # 内部资料不能成为确认保存授权。
        try:
            illegal(
                good,
                run_id="r1",
                created_at=5,
                node_id=uid(),
                confirmation=json.dumps(
                    {"business_kind": "profile", "proposal_id": uid(), "display_entry_id": uid()}
                ),
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("资料节点携带确认授权未被拒绝")
        connection.rollback()

        # 普通消息节点不能携带内部角色：外部输入无法创建 custom_message 载荷。
        try:
            connection.execute(
                "INSERT INTO session_entries "
                "(session_id, id, parent_id, run_id, type, messages, created_at) "
                "VALUES (?, ?, NULL, NULL, 'message', ?, 2)",
                (
                    session_id,
                    uid(),
                    json.dumps(
                        [{"role": "custom", "custom_type": "business_context", "content": "x"}]
                    ),
                ),
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("普通消息节点接受了内部角色")
        connection.rollback()

        # 合法资料节点可入库，且运行外键必须指向真实运行。
        illegal(good, run_id="r1", created_at=5, node_id=uid())
        connection.commit()
        try:
            illegal(good, run_id="missing-run", created_at=5, node_id=uid())
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("资料节点接受了不存在的运行")
        connection.rollback()
    finally:
        connection.close()

    rejects(
        lambda: SessionMessageEntry(
            session_id=session_id,
            id=uid(),
            parent_id=None,
            run_id=None,
            type="message",
            messages=[
                {
                    "role": "custom",
                    "custom_type": "business_context",
                    "content": "外部文本",
                    "display": False,
                    "timestamp": 1,
                }
            ],
            created_at=1,
        ),
        "role",
    )


async def seed_http_history(path: Path) -> dict:
    session_id = uid()
    database = await open_database(path)
    service = SessionService(SqliteSessionRepository(database))
    try:
        await service.create_session(session_id, "公开历史")
        send = await service.accept_send(
            SendCommand(
                operation_id=uid(), session_id=session_id, request=SendRequest(text="公开请求")
            ),
            system_message=system_message(),
        )
        run = send.run
        await service.append_entry(
            SessionMessageEntry(
                session_id=session_id,
                id=uid(),
                parent_id=run.request_entry_id,
                run_id=run.id,
                type="message",
                messages=[assistant_message(2)],
                created_at=2,
            )
        )
        entries = await service.list_entries(session_id)
        assistant_id = role_entry_id(entries, "assistant")
        details = snapshot_details(run.request_entry_id)
        node = build_business_context_entry(
            session_id=session_id,
            id=uid(),
            parent_id=assistant_id,
            run_id=run.id,
            details=details,
            created_at=CREATED_AT,
        )
        await service.append_business_context(node)
        return {
            "session_id": session_id,
            "entry_id": node.id,
            "parent_id": assistant_id,
            "run_id": run.id,
            "content": node.message.content,
            "business_date": details.request.business_date,
        }
    finally:
        await database.close()


def http_checks(evidence: dict) -> None:
    path = patch_default_database("business-context-store-http")
    server = Server(app)
    with server, client(server.base_url) as http:
        seed = asyncio.run(seed_http_history(path))
        response = http.get(f"/api/sessions/{seed['session_id']}/history")
        assert response.status_code == 200
        body = response.json()
        entry = next(
            item for item in body["entries"] if item["entry_id"] == seed["entry_id"]
        )
        assert set(entry) == {
            "type",
            "entry_id",
            "parent_id",
            "run_id",
            "created_at",
        }
        assert entry["type"] == "custom_message"
        assert entry["parent_id"] == seed["parent_id"]
        assert entry["run_id"] == seed["run_id"]
        assert isinstance(entry["created_at"], int)
        assert [item["entry_id"] for item in body["entries"]][-1] == seed["entry_id"]
        assert body["session"]["active_leaf_id"] == seed["entry_id"]
        assert body["runs"][-1]["last_entry_id"] == seed["entry_id"]
        # 业务资料、模型文本与内部字段不得进入公开响应。
        assert seed["content"] not in response.text
        assert "<business_context>" not in response.text
        assert seed["business_date"] not in response.text
        assert "details" not in response.text
        assert "display" not in response.text
        evidence["http_history_entry"] = entry


def check() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=EVIDENCE, ignore_cleanup_errors=True) as directory:
        root = Path(directory)
        evidence = {
            "types": check_types(),
            "migration": asyncio.run(check_migration(root)),
            "write": asyncio.run(check_write_and_restore(root)),
            "branch": asyncio.run(check_branch_isolation(root)),
        }
        check_payload_rules()
        asyncio.run(check_message_nodes_boundary(root))
        asyncio.run(check_template_update(root))
        asyncio.run(check_boundaries(root))
        http_checks(evidence)
    (EVIDENCE / "business-context-store.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "PASS: 泛型基类与业务资料校验、序列化往返、非法字段组合拒绝、"
        "真实 SQLite 新库迁移与旧库升级、内部节点写入与重启恢复、"
        "分支隔离与祖先链读取、公开历史五字段白名单、外部输入与确认授权边界、"
        "资料节点不进入 message_nodes 白名单、模板更新后旧资料文本保持原值"
    )


if __name__ == "__main__":
    check()
