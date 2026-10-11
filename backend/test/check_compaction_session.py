import asyncio
import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from app.agent import compaction as C
from app.agent.compaction_summary import (
    COMPACTION_SUMMARY_PREFIX,
    COMPACTION_SUMMARY_SUFFIX,
)
from app.ai.messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from app.ai.model_capabilities import resolve_model_spec
from app.application.session.service import (
    CREDENTIAL_ERROR_CODE,
    CREDENTIAL_SAFE_MESSAGE,
    SessionProjection,
    SessionService,
)
from app.domain.session.attachments import AttachmentMetadata, attachment_storage_ref
from app.domain.session.errors import SessionConflict
from app.domain.session.models import (
    CompactionAttachmentReference,
    CompactionDetails,
    CompactionEntry,
    SendCommand,
    SendRequest,
    Session,
    SessionMessageEntry,
    SessionRun,
)
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from app.model_config import load_model_config

BACKEND = Path(__file__).resolve().parents[1]
TEMP_ROOT = BACKEND / "temp" / "compaction-session"
EVIDENCE_DIR = BACKEND.parent / "tmp" / "agent-compaction-loop-part-1"

SID = "11111111-1111-4111-8111-111111111111"
BRANCH_SID = "22222222-2222-4222-8222-222222222222"
ATTACH = "33333333-3333-4333-8333-333333333333"
CREDENTIAL_KEY = "sk-test-compaction-session-secret"
RUN = "run-1"

USAGE = Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2)
# 真实摘要提交路径用较小保留区间即可形成合法切点；生产 keep_recent_tokens 保持 20000。
LIVE_SETTINGS = C.CompactionSettings(
    reserve_tokens=C.RESERVE_TOKENS, keep_recent_tokens=400
)

_clock = [1000]


def tick() -> int:
    _clock[0] += 1
    return _clock[0]


def system_message(text: str = "系统指令") -> SystemMessage:
    return SystemMessage(role="system", content=text, timestamp=tick())


def user(text: str) -> UserMessage:
    return UserMessage(role="user", content=text, timestamp=tick())


def assistant(
    text: str = "回答",
    calls: tuple[ToolCall, ...] = (),
    *,
    usage: Usage | None = USAGE,
    stop_reason: str = "stop",
) -> AssistantMessage:
    content = [TextContent(type="text", text=text)] if text else []
    content.extend(calls)
    return AssistantMessage(
        role="assistant",
        content=content,
        api="openai-completions",
        provider="test",
        model="test",
        usage=usage,
        stop_reason=stop_reason,
        timestamp=tick(),
    )


def tool_result(call_id: str, text: str = "结果", name: str = "echo") -> ToolResultMessage:
    return ToolResultMessage(
        role="toolResult",
        tool_call_id=call_id,
        tool_name=name,
        content=[TextContent(type="text", text=text)],
        is_error=False,
        timestamp=tick(),
    )


def attachment(attachment_id: str = ATTACH, file_name: str = "note.md") -> AttachmentMetadata:
    return AttachmentMetadata(
        attachment_id=attachment_id,
        session_id=SID,
        file_name=file_name,
        size_bytes=3,
        storage_ref=attachment_storage_ref(SID, attachment_id, file_name),
        created_at=tick(),
    )


def reference(
    attachment_id: str = ATTACH, file_name: str = "note.md"
) -> CompactionAttachmentReference:
    return CompactionAttachmentReference(
        attachment_id=attachment_id,
        file_name=file_name,
        path=attachment_storage_ref(SID, attachment_id, file_name).removeprefix("tmp/"),
    )


def message_entry(
    session_id: str,
    entry_id: str,
    parent_id: str | None,
    message,
    *,
    run_id: str | None = None,
    created_at: int | None = None,
    attachments: tuple[AttachmentMetadata, ...] = (),
) -> SessionMessageEntry:
    return SessionMessageEntry(
        session_id=session_id,
        id=entry_id,
        parent_id=parent_id,
        run_id=run_id,
        type="message",
        messages=[message],
        created_at=tick() if created_at is None else created_at,
        attachments=list(attachments),
    )


def compaction_node(
    session_id: str,
    entry_id: str,
    parent_id: str,
    first_kept_entry_id: str,
    run_id: str,
    *,
    summary: str = "检查点摘要",
    details: CompactionDetails | None = None,
    created_at: int | None = None,
) -> CompactionEntry:
    return CompactionEntry(
        session_id=session_id,
        id=entry_id,
        parent_id=parent_id,
        run_id=run_id,
        type="compaction",
        summary=summary,
        first_kept_entry_id=first_kept_entry_id,
        tokens_before=1234,
        usage=USAGE,
        system_message=system_message(),
        details=details,
        created_at=tick() if created_at is None else created_at,
    )


async def open_store(path: Path):
    database = await open_database(path)
    repository = SqliteSessionRepository(database)
    return database, repository, SessionService(repository)


async def seed(
    repository,
    session_id: str,
    entries: list,
    *,
    run: SessionRun | None = None,
    leaf: str | None = None,
) -> None:
    # 预置确定性输入：父节点先于子节点插入，运行外键延迟到提交校验。
    async with repository.transaction():
        await repository.defer_foreign_keys()
        await repository.insert_session(
            Session(id=session_id, title="压缩会话", active_leaf_id=None, created_at=1, updated_at=1)
        )
        for entry in entries:
            await repository.insert_entry(entry)
        if run is not None:
            await repository.insert_run(run)
        if leaf is not None:
            session = await repository.get_session(session_id)
            await repository.update_session(
                session.model_copy(update={"active_leaf_id": leaf, "updated_at": 1})
            )


def running_run(session_id: str, request_entry_id: str, last_entry_id: str | None) -> SessionRun:
    return SessionRun(
        session_id=session_id,
        id=RUN,
        request_entry_id=request_entry_id,
        last_entry_id=last_entry_id,
        status="running",
        started_at=2,
        finished_at=None,
        error_code=None,
        error_message=None,
    )


def ids(values) -> list[str]:
    return [entry.id for entry in values]


async def scenario_no_checkpoint(root: Path) -> dict:
    database, repository, service = await open_store(root / "no-checkpoint.db")
    try:
        entries = [
            message_entry(SID, "sys", None, system_message()),
            message_entry(SID, "u1", "sys", user("问题一" * 20)),
            message_entry(SID, "a1", "u1", assistant("回答一"), run_id=RUN),
        ]
        await seed(repository, SID, entries, run=running_run(SID, "u1", "a1"), leaf="a1")
        projection = await service.get_projection(SID, "a1")
        assert isinstance(projection, SessionProjection)
        assert ids(projection.path) == ["sys", "u1", "a1"]
        assert [message.role for message in projection.context.messages] == [
            "system",
            "user",
            "assistant",
        ]
        assert projection.context.source_entry_ids == ["sys", "u1", "a1"]
        assert projection.context.messages[2] == entries[2].messages[0]
        context = await service.get_context(SID, "a1")
        assert [message.role for message in context] == ["system", "user", "assistant"]
        return {"entries": ids(projection.path)}
    finally:
        await database.close()


async def scenario_single_checkpoint(root: Path) -> dict:
    database, repository, service = await open_store(root / "single.db")
    try:
        entries = [
            message_entry(SID, "sys", None, system_message()),
            message_entry(SID, "u1", "sys", user("问题一" * 20)),
            message_entry(SID, "a1", "u1", assistant("回答一" * 20), run_id=RUN),
            message_entry(SID, "u2", "a1", user("问题二" * 20), run_id=RUN),
            message_entry(SID, "a2", "u2", assistant("回答二" * 20), run_id=RUN),
            message_entry(SID, "u3", "a2", user("保留原文" * 30), run_id=RUN),
            message_entry(SID, "a3", "u3", assistant("回答三"), run_id=RUN),
        ]
        checkpoint = compaction_node(SID, "c1", "a3", "u3", RUN)
        await seed(
            repository, SID, [*entries, checkpoint],
            run=running_run(SID, "u1", "a3"), leaf="c1",
        )
        projection = await service.get_projection(SID, "c1")
        assert ids(projection.path) == ["sys", "u1", "a1", "u2", "a2", "u3", "a3", "c1"]
        # 压缩头：有效 system_message 与摘要转换 UserMessage 的来源都是检查点节点。
        assert projection.context.source_entry_ids == ["c1", "c1", "u3", "a3"]
        assert [message.role for message in projection.context.messages] == [
            "system",
            "user",
            "user",
            "assistant",
        ]
        assert projection.context.messages[0] == checkpoint.system_message
        summary_text = projection.context.messages[1].content[0].text
        assert summary_text == (
            COMPACTION_SUMMARY_PREFIX + "检查点摘要" + COMPACTION_SUMMARY_SUFFIX
        )
        assert projection.context.messages[2] == entries[5].messages[0]
        assert projection.context.messages[2].content == "保留原文" * 30
        assert projection.context.messages[3] == entries[6].messages[0]
        # 原始路径与业务用户身份保持：get_branch/get_context 仍返回真实消息节点。
        branch = await service.get_branch(SID, "c1")
        assert ids(branch) == ["sys", "u1", "a1", "u2", "a2", "u3", "a3"]
        raw = await service.get_context(SID, "c1")
        assert [message.role for message in raw] == [
            "system", "user", "assistant", "user", "assistant", "user", "assistant",
        ]
        # 摘要转换消息没有真实节点身份：来源映射解析到压缩节点。
        source_entry = next(item for item in projection.path if item.id == "c1")
        assert isinstance(source_entry, CompactionEntry)
        return {"source_entry_ids": projection.context.source_entry_ids}
    finally:
        await database.close()


async def scenario_repeated_checkpoint(root: Path) -> dict:
    database, repository, service = await open_store(root / "repeated.db")
    try:
        entries = [
            message_entry(SID, "sys", None, system_message()),
            message_entry(SID, "u1", "sys", user("历史问题")),
            message_entry(SID, "a1", "u1", assistant("历史回答"), run_id=RUN),
            compaction_node(SID, "c0", "a1", "u1", RUN, summary="旧摘要"),
            message_entry(SID, "u2", "c0", user("问题二" * 20), run_id=RUN),
            message_entry(SID, "a2", "u2", assistant("回答二" * 20), run_id=RUN),
            message_entry(SID, "u3", "a2", user("问题三" * 20), run_id=RUN),
            message_entry(SID, "a3", "u3", assistant("回答三" * 20), run_id=RUN),
            message_entry(SID, "u4", "a3", user("保留原文" * 30), run_id=RUN),
            message_entry(SID, "a4", "u4", assistant("回答四"), run_id=RUN),
        ]
        latest = compaction_node(SID, "c1", "a4", "u4", RUN, summary="最新摘要")
        await seed(
            repository, SID, [*entries, latest],
            run=running_run(SID, "u1", "a4"), leaf="c1",
        )
        projection = await service.get_projection(SID, "c1")
        # 使用当前路径上最新检查点：c1 系统状态、摘要与 u4 起的保留原文及新增消息。
        assert projection.context.source_entry_ids == ["c1", "c1", "u4", "a4"]
        assert "最新摘要" in projection.context.messages[1].content[0].text
        assert projection.context.messages[2].content == "保留原文" * 30
        assert all(
            "旧摘要" not in getattr(message, "content", "")
            for message in [projection.context.messages[1]]
        )
        return {"source_entry_ids": projection.context.source_entry_ids}
    finally:
        await database.close()


async def scenario_tool_batch_and_attachments(root: Path) -> dict:
    database, repository, service = await open_store(root / "batch.db")
    try:
        calls = (
            ToolCall(type="toolCall", id="c1", name="echo", arguments={"n": 1}),
            ToolCall(type="toolCall", id="c2", name="echo", arguments={"n": 2}),
        )
        metadata = attachment()
        entries = [
            message_entry(SID, "sys", None, system_message()),
            message_entry(SID, "u1", "sys", user("批量调用"), attachments=(metadata,)),
            message_entry(SID, "a1", "u1", assistant("", calls), run_id=RUN),
            message_entry(SID, "t1", "a1", tool_result("c1", "结果一"), run_id=RUN),
            message_entry(SID, "t2", "a1", tool_result("c2", "结果二"), run_id=RUN),
            message_entry(SID, "u2", "t2", user("下一轮" * 400), run_id=RUN),
            message_entry(SID, "a2", "u2", assistant("收尾"), run_id=RUN),
        ]
        await seed(repository, SID, entries, run=running_run(SID, "u1", "a2"), leaf="a2")
        async with repository.transaction():
            await repository.insert_attachment(metadata)
            await repository.bind_entry_attachments(SID, "u1", [metadata.attachment_id])
        # 完整工具批次进入总结范围；附件引用来自真实元数据。
        settings = C.CompactionSettings(reserve_tokens=C.RESERVE_TOKENS, keep_recent_tokens=100)
        preparation = C.prepare_compaction(entries, 128000, 16384, settings)
        assert preparation.first_kept_entry_id == "u2"
        assert [
            block.id
            for message in preparation.messages_to_summarize
            if isinstance(message, AssistantMessage)
            for block in message.content
            if isinstance(block, ToolCall)
        ] == ["c1", "c2"]
        assert [item.attachment_id for item in preparation.attachments] == [ATTACH]

        checkpoint = compaction_node(
            SID, "c1", "a2", preparation.first_kept_entry_id, RUN,
            details=CompactionDetails(attachments=[reference()]),
        )
        outcome = await service.commit_compaction(checkpoint, prepared_position="a2")
        assert outcome.created is True
        projection = await service.get_projection(SID, "c1")
        # 保留原文默认包含完整工具批次（切点在 u2）：u2、a2。
        assert projection.context.source_entry_ids == ["c1", "c1", "u2", "a2"]
        assert [message.role for message in projection.context.messages] == [
            "system", "user", "user", "assistant",
        ]
        retained_user = next(entry for entry in projection.path if entry.id == "u1")
        assert retained_user.attachments == [metadata]
        stored = await service.get_entry(SID, "c1")
        assert stored.details.attachments == [reference()]
        return {
            "summarized_call_ids": ["c1", "c2"],
            "attachment_ids": [item.attachment_id for item in stored.details.attachments],
        }
    finally:
        await database.close()


async def scenario_reopen(root: Path) -> dict:
    path = root / "reopen.db"
    entries = [
        message_entry(SID, "sys", None, system_message()),
        message_entry(SID, "u1", "sys", user("问题一" * 20)),
        message_entry(SID, "a1", "u1", assistant("回答一" * 20), run_id=RUN),
        message_entry(SID, "u2", "a1", user("保留原文" * 30), run_id=RUN),
        message_entry(SID, "a2", "u2", assistant("回答二"), run_id=RUN),
        compaction_node(SID, "c1", "a2", "u2", RUN),
    ]
    database, repository, service = await open_store(path)
    try:
        await seed(
            repository, SID, entries, run=running_run(SID, "u1", "a2"), leaf="c1"
        )
    finally:
        await database.close()

    reopened, repository, service = await open_store(path)
    try:
        projection = await service.get_projection(SID, "c1")
        assert projection.context.source_entry_ids == ["c1", "c1", "u2", "a2"]
        assert projection.context.messages[2].content == "保留原文" * 30
        return {"source_entry_ids": projection.context.source_entry_ids}
    finally:
        await reopened.close()


async def scenario_illegal_first_kept_read(root: Path) -> dict:
    database, repository, service = await open_store(root / "illegal-read.db")
    try:
        # 保留起点是同会话但不在当前路径上的兄弟节点。
        off_path = [
            message_entry(SID, "sys", None, system_message()),
            message_entry(SID, "u1", "sys", user("问题一")),
            message_entry(SID, "a1", "u1", assistant("回答一")),
            message_entry(SID, "u2", "sys", user("兄弟分支")),
            compaction_node(SID, "c1", "a1", "u2", RUN),
        ]
        await seed(
            repository, SID, off_path,
            run=running_run(SID, "u1", None), leaf="c1",
        )
        with pytest.raises(ValueError, match="不在当前路径"):
            await service.get_projection(SID, "c1")
    finally:
        await database.close()

    database, repository, service = await open_store(root / "illegal-node.db")
    try:
        # 保留起点指向压缩结构节点，不是真实消息节点。
        structural = [
            message_entry(BRANCH_SID, "sys", None, system_message()),
            message_entry(BRANCH_SID, "u1", "sys", user("问题一")),
            message_entry(BRANCH_SID, "a1", "u1", assistant("回答一"), run_id=RUN),
            compaction_node(BRANCH_SID, "c0", "a1", "u1", RUN),
            compaction_node(BRANCH_SID, "c1", "c0", "c0", RUN),
        ]
        await seed(
            repository, BRANCH_SID, structural,
            run=running_run(BRANCH_SID, "u1", None), leaf="c1",
        )
        with pytest.raises(ValueError, match="不是真实消息节点"):
            await service.get_projection(BRANCH_SID, "c1")
        return {"off_path": "不在当前路径", "structural": "不是真实消息节点"}
    finally:
        await database.close()


async def scenario_commit_success(root: Path) -> dict:
    database, repository, service = await open_store(root / "commit.db")
    try:
        entries = [
            message_entry(SID, "sys", None, system_message()),
            message_entry(SID, "u1", "sys", user("问题一")),
            message_entry(SID, "a1", "u1", assistant("回答一"), run_id=RUN),
        ]
        await seed(repository, SID, entries, run=running_run(SID, "u1", "a1"), leaf="a1")
        metadata = attachment()
        async with repository.transaction():
            await repository.insert_attachment(metadata)
            await repository.bind_entry_attachments(SID, "u1", [metadata.attachment_id])

        checkpoint = compaction_node(
            SID, "c1", "a1", "u1", RUN,
            details=CompactionDetails(attachments=[reference()]),
        )
        outcome = await service.commit_compaction(checkpoint, prepared_position="a1")
        assert outcome.created is True and outcome.credential_detected is False
        assert outcome.entry.id == "c1" and outcome.entry.parent_id == "a1"
        assert outcome.run.last_entry_id == "c1"

        stored = await service.get_entry(SID, "c1")
        assert isinstance(stored, CompactionEntry) and stored.model_dump() == checkpoint.model_dump()
        assert stored.details.attachments == [reference()]
        session = await service.get_session(SID)
        assert session.active_leaf_id == "c1"
        run = await service.get_run(SID, RUN)
        assert run.last_entry_id == "c1" and run.request_entry_id == "u1"

        projection = await service.get_projection(SID, "c1")
        assert projection.context.source_entry_ids == ["c1", "c1", "u1", "a1"]
        assert [message.role for message in projection.context.messages] == [
            "system", "user", "user", "assistant",
        ]
        assert next(entry for entry in projection.path if entry.id == "u1").attachments == [metadata]
        assert ids(await service.get_branch(SID, "c1")) == ["sys", "u1", "a1"]
        return {"last_entry_id": run.last_entry_id, "active_leaf_id": session.active_leaf_id}
    finally:
        await database.close()


async def scenario_commit_rejections(root: Path) -> dict:
    database, repository, service = await open_store(root / "reject.db")
    try:
        entries = [
            message_entry(SID, "sys", None, system_message()),
            message_entry(SID, "u1", "sys", user("问题一")),
            message_entry(SID, "a1", "u1", assistant("回答一"), run_id=RUN),
            message_entry(SID, "u2", "sys", user("兄弟分支")),
        ]
        await seed(repository, SID, entries, run=running_run(SID, "u1", "a1"), leaf="a1")

        # entry.parent_id 与摘要准备位置不一致。
        with pytest.raises(SessionConflict, match="父节点与摘要准备位置不一致"):
            await service.commit_compaction(
                compaction_node(SID, "cx", "a1", "u1", RUN, created_at=100),
                prepared_position="u1",
            )

        # 非法保留起点（同会话但不在当前路径）拒绝，且不产生部分写入。
        before = {
            "entries": ids(await service.list_entries(SID)),
            "run": (await service.get_run(SID, RUN)).last_entry_id,
            "leaf": (await service.get_session(SID)).active_leaf_id,
        }
        with pytest.raises(ValueError, match="不在当前路径"):
            await service.commit_compaction(
                compaction_node(SID, "cy", "a1", "u2", RUN, created_at=101),
                prepared_position="a1",
            )
        assert ids(await service.list_entries(SID)) == before["entries"]
        assert (await service.get_run(SID, RUN)).last_entry_id == before["run"]
        assert (await service.get_session(SID)).active_leaf_id == before["leaf"]

        # 分支指针与运行进度不一致。
        session = await service.get_session(SID)
        async with repository.transaction():
            await repository.update_session(
                session.model_copy(update={"active_leaf_id": "u1", "updated_at": 5})
            )
        with pytest.raises(SessionConflict, match="当前分支指针与运行进度不一致"):
            await service.commit_compaction(
                compaction_node(SID, "cz", "a1", "u1", RUN, created_at=102),
                prepared_position="a1",
            )
        async with repository.transaction():
            await repository.update_session(
                session.model_copy(update={"active_leaf_id": "a1", "updated_at": 6})
            )

        # 摘要准备后运行位置已推进：不自动改父节点，也不提交基于旧位置的摘要。
        await service.append_entry(
            SessionMessageEntry(
                session_id=SID, id="a2", parent_id="a1", run_id=RUN, type="message",
                messages=[assistant("后续回答")], created_at=7,
            )
        )
        with pytest.raises(SessionConflict, match="摘要准备位置与运行当前进度不一致"):
            await service.commit_compaction(
                compaction_node(SID, "cw", "a1", "u1", RUN, created_at=103),
                prepared_position="a1",
            )

        # 终态运行拒绝提交。
        await service.finish_run(SID, RUN, "completed")
        with pytest.raises(SessionConflict, match="运行未在运行"):
            await service.commit_compaction(
                compaction_node(SID, "cv", "a2", "u1", RUN, created_at=104),
                prepared_position="a2",
            )
        return {"rejected": ["parent", "first_kept", "leaf", "position", "terminal"]}
    finally:
        await database.close()


async def scenario_commit_credential_and_identity(root: Path) -> dict:
    database, repository, service = await open_store(root / "identity.db")
    try:
        entries = [
            message_entry(SID, "sys", None, system_message()),
            message_entry(SID, "u1", "sys", user("问题一")),
            message_entry(SID, "a1", "u1", assistant("回答一"), run_id=RUN),
        ]
        await seed(repository, SID, entries, run=running_run(SID, "u1", "a1"), leaf="a1")

        # 凭据命中：不产生检查点，运行以 credential_detected 失败并保留安全文案。
        credential = compaction_node(
            SID, "cred", "a1", "u1", RUN,
            summary=f"摘要内命中 {CREDENTIAL_KEY}", created_at=200,
        )
        outcome = await service.commit_compaction(
            credential, prepared_position="a1", credentials=(CREDENTIAL_KEY,)
        )
        assert outcome.created is False and outcome.credential_detected is True
        assert outcome.entry is None
        assert outcome.run.status == "failed"
        assert outcome.run.error_code == CREDENTIAL_ERROR_CODE
        assert outcome.run.error_message == CREDENTIAL_SAFE_MESSAGE
        assert await repository.get_entry(SID, "cred") is None
        return {"credential": outcome.run.error_code}
    finally:
        await database.close()


async def scenario_commit_idempotent_and_conflict(root: Path) -> dict:
    database, repository, service = await open_store(root / "idem.db")
    try:
        entries = [
            message_entry(SID, "sys", None, system_message()),
            message_entry(SID, "u1", "sys", user("问题一")),
            message_entry(SID, "a1", "u1", assistant("回答一"), run_id=RUN),
        ]
        await seed(repository, SID, entries, run=running_run(SID, "u1", "a1"), leaf="a1")

        checkpoint = compaction_node(SID, "c1", "a1", "u1", RUN)
        first = await service.commit_compaction(checkpoint, prepared_position="a1")
        assert first.created is True
        second = await service.commit_compaction(
            checkpoint.model_copy(update={"created_at": 999}),
            prepared_position="c1",
        )
        assert second.created is False and second.entry.id == "c1"
        assert len([entry for entry in await service.list_entries(SID) if entry.id == "c1"]) == 1

        # 节点 ID 与既有消息节点冲突：载荷不同按节点身份冲突拒绝。
        with pytest.raises(SessionConflict, match="节点 ID 冲突"):
            await service.commit_compaction(
                compaction_node(SID, "a1", "c1", "u1", RUN, created_at=300),
                prepared_position="c1",
            )
        return {"idempotent_created": [first.created, second.created]}
    finally:
        await database.close()


async def scenario_portal_commit_and_projection(root: Path) -> dict:
    database, repository, service = await open_store(root / "portal.db")
    try:
        # 无检查点时必须仍然返回真实原始投影。
        await service.create_session(SID, "入口")
        send = await service.accept_send(
            SendCommand(
                operation_id=str(uuid4()),
                session_id=SID,
                request=SendRequest(text="开始训练计划" * 5),
            ),
            system_message=system_message(),
        )
        run = send.run
        request_entry = await service.get_entry(SID, run.request_entry_id)
        await service.append_entry(
            SessionMessageEntry(
                session_id=SID, id="pa1", parent_id=request_entry.id, run_id=run.id,
                type="message", messages=[assistant("计划已生成")], created_at=tick(),
            )
        )
        projection = await service.get_projection(SID, "pa1")
        assert projection.context.source_entry_ids == [request_entry.parent_id, request_entry.id, "pa1"]
        assert projection.context.messages[0].role == "system"

        checkpoint = compaction_node(SID, "pc1", "pa1", request_entry.id, run.id)
        outcome = await service.commit_compaction(checkpoint, prepared_position="pa1")
        assert outcome.created is True
        refreshed = await service.get_projection(SID, "pc1")
        assert refreshed.context.source_entry_ids == ["pc1", "pc1", request_entry.id, "pa1"]
        return {"projection": refreshed.context.source_entry_ids}
    finally:
        await database.close()


async def scenario_live_summary_commit(root: Path) -> dict:
    # 用实际测试模型生成摘要与真实 usage，再经提交入口写入真实检查点。
    config = load_model_config()
    model = resolve_model_spec(config)
    database, repository, service = await open_store(root / "live.db")
    try:
        entries = [
            message_entry(SID, "sys", None, system_message("你是个人力量训练助手。")),
            message_entry(SID, "u1", "sys", user(
                "我左膝半月板术后三个月，不能负重深跳；家里只有可调哑铃和弹力带；"
                "每周只能练四天。请给我一个四日循环。" * 6
            )),
            message_entry(SID, "a1", "u1", assistant(
                "已记录限制：左膝术后、无杠铃、每周四练。" * 6
            ), run_id=RUN),
            message_entry(SID, "u2", "a1", user("我特别想提高频率，直接改成一周六练可以吗？" * 6), run_id=RUN),
            message_entry(SID, "a2", "u2", assistant("需要先确认恢复情况与器械条件再决定频率。" * 6), run_id=RUN),
            message_entry(SID, "u3", "a2", user("撤回六练，保持每周四练，膝盖优先。保留原文。" * 120), run_id=RUN),
            message_entry(SID, "a3", "u3", assistant("已按每周四练继续，膝盖保护放在首位。"), run_id=RUN),
        ]
        await seed(repository, SID, entries, run=running_run(SID, "u1", "a3"), leaf="a3")

        preparation = C.prepare_compaction(entries, model.context_window, model.max_tokens, LIVE_SETTINGS)
        started = time.perf_counter()
        result = await C.generate_compaction_summary(
            preparation, model, api_key=config.api_key
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        assert result.summary.strip()
        assert result.usage.total_tokens > 0

        system_state = entries[0].messages[0]
        checkpoint = CompactionEntry(
            session_id=SID,
            id="lc1",
            parent_id="a3",
            run_id=RUN,
            type="compaction",
            summary=result.summary,
            first_kept_entry_id=preparation.first_kept_entry_id,
            tokens_before=preparation.tokens_before,
            usage=result.usage,
            system_message=system_state,
            details=CompactionDetails(attachments=preparation.attachments) if preparation.attachments else None,
            created_at=tick(),
        )
        outcome = await service.commit_compaction(checkpoint, prepared_position="a3")
        assert outcome.created is True and outcome.run.last_entry_id == "lc1"
        stored = await service.get_entry(SID, "lc1")
        assert stored.tokens_before == preparation.tokens_before
        assert stored.usage == result.usage
        projection = await service.get_projection(SID, "lc1")
        assert projection.context.source_entry_ids[0] == "lc1"
        assert projection.context.messages[0] == system_state
        assert result.summary in projection.context.messages[1].content[0].text
        lowered = result.summary.lower()
        return {
            "model": {
                "api": config.api,
                "provider": config.provider,
                "id": config.model,
                "base_url": config.base_url,
                "context_window": model.context_window,
                "max_tokens": model.max_tokens,
            },
            "settings": {
                "reserve_tokens": LIVE_SETTINGS.reserve_tokens,
                "test_keep_recent_tokens": LIVE_SETTINGS.keep_recent_tokens,
                "production_keep_recent_tokens": C.KEEP_RECENT_TOKENS,
            },
            "first_kept_entry_id": preparation.first_kept_entry_id,
            "tokens_before": preparation.tokens_before,
            "tokens_before_basis": (
                "夹具助手 usage 为占位值，tokens_before 反映该基准；"
                "摘要 usage 为真实服务返回"
            ),
            "usage": result.usage.model_dump(),
            "latency_ms": latency_ms,
            "summary_chars": len(result.summary),
            "section_goal_present": "goal" in lowered,
            "committed": outcome.entry.id,
        }
    finally:
        await database.close()


async def run_all() -> dict:
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    evidence: dict = {}
    with TemporaryDirectory(dir=TEMP_ROOT, ignore_cleanup_errors=True) as directory:
        root = Path(directory)
        evidence["no_checkpoint"] = await scenario_no_checkpoint(root)
        evidence["single_checkpoint"] = await scenario_single_checkpoint(root)
        evidence["repeated_checkpoint"] = await scenario_repeated_checkpoint(root)
        evidence["tool_batch_attachments"] = await scenario_tool_batch_and_attachments(root)
        evidence["reopen"] = await scenario_reopen(root)
        evidence["illegal_first_kept_read"] = await scenario_illegal_first_kept_read(root)
        evidence["commit_success"] = await scenario_commit_success(root)
        evidence["commit_rejections"] = await scenario_commit_rejections(root)
        evidence["commit_credential"] = await scenario_commit_credential_and_identity(root)
        evidence["commit_idempotent"] = await scenario_commit_idempotent_and_conflict(root)
        evidence["portal"] = await scenario_portal_commit_and_projection(root)
        evidence["live"] = await scenario_live_summary_commit(root)
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / "session-evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return evidence


def check() -> None:
    evidence = asyncio.run(run_all())
    live = evidence["live"]
    print(
        "压缩会话：无/单/重复检查点投影、真实来源、保留原文、重开读取、"
        "原始路径与业务身份保持、完整工具批次与附件引用、非法起点、位置冲突、"
        "终态与凭据拒绝、三项事务原子提交与幂等通过"
    )
    print(
        f"  真实摘要提交：model={live['model']['id']} "
        f"first_kept={live['first_kept_entry_id']} "
        f"tokens_before={live['tokens_before']} "
        f"usage={live['usage']} latency={live['latency_ms']}ms "
        f"summary_chars={live['summary_chars']}"
    )


if __name__ == "__main__":
    check()
