import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from app.agent import compaction as C
from app.ai.messages import (
    AssistantMessage,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from app.application.session.service import SessionService
from app.domain.session.branch import build_session_path
from app.domain.session.models import (
    CompactionDetails,
    SendCommand,
    SendRequest,
    SessionMessageEntry,
)
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from test import check_compaction_core as T

TEMP_ROOT = Path(__file__).resolve().parents[2] / "tmp" / "agent-compaction-fix-a"
EXTERNAL_SESSION = "99999999-9999-4999-8999-999999999999"


def summarized_call_ids(messages: list) -> list[str]:
    return [
        block.id
        for message in messages
        if isinstance(message, AssistantMessage)
        for block in message.content
        if isinstance(block, ToolCall)
    ]


def test_retained_messages_before_checkpoint_remain_visible() -> None:
    path = T.build_path_entries()
    checkpoint = T.compaction_entry("c1", path[-1].id, "u3")
    context = C.build_effective_context([*path, checkpoint])
    assert context.source_entry_ids == ["c1", "c1", "u3", "a3"]
    assert len(context.messages) == len(context.source_entry_ids)
    assert context.messages[0].role == "system"
    assert context.messages[1].role == "user"
    assert context.messages[2] == path[5].messages[0]
    assert context.messages[3] == path[6].messages[0]
    assert context.messages[2].content == "问题三" * 400


def test_next_compaction_summarizes_previous_retained_messages() -> None:
    path = T.build_path_entries()
    checkpoint = T.compaction_entry("c1", path[-1].id, "u3")
    new_user = T.entry("u4", "c1", T.user("新的用户请求" * 500))
    result = C.prepare_compaction([*path, checkpoint, new_user], 128000, 16384, T.SMALL)
    assert result.first_kept_entry_id == "u4"
    assert result.previous_summary == "旧摘要"
    retained = [message for message in result.messages_to_summarize if isinstance(message, UserMessage)]
    assert any("问题三" in message.content for message in retained)
    assert not any("问题一" in message.content or "问题二" in message.content for message in retained)
    assert result.is_split_turn is False


def test_multiple_compactions_use_latest_checkpoint() -> None:
    path = [
        T.entry("sys", None, T.system_message()),
        T.entry("u1", "sys", T.user("问题一" * 20)),
        T.entry("a1", "u1", T.assistant("回答一" * 20)),
        T.compaction_entry("c1", "a1", "u1", summary="摘要一"),
        T.entry("u2", "c1", T.user("问题二" * 20)),
        T.entry("a2", "u2", T.assistant("回答二" * 20)),
        T.compaction_entry("c2", "a2", "u2", summary="摘要二"),
        T.entry("u3", "c2", T.user("问题三" * 400)),
        T.entry("a3", "u3", T.assistant("回答三")),
    ]
    context = C.build_effective_context(path)
    assert context.source_entry_ids == ["c2", "c2", "u2", "a2", "u3", "a3"]
    preparation = C.prepare_compaction(path, 128000, 16384, T.SMALL)
    assert preparation.previous_summary == "摘要二"
    assert preparation.first_kept_entry_id == "u3"
    texts = [getattr(message, "content", "") for message in preparation.messages_to_summarize]
    assert any("问题二" in str(text) for text in texts)
    assert not any("问题一" in str(text) for text in texts)
    assert not any("问题三" in str(text) for text in texts)


def test_tool_batch_summarized_whole_when_cut_at_later_user() -> None:
    calls = (
        ToolCall(type="toolCall", id="c1", name="echo", arguments={"n": 1}),
        ToolCall(type="toolCall", id="c2", name="echo", arguments={"n": 2}),
    )
    path = [
        T.entry("sys", None, T.system_message()),
        T.entry("u1", "sys", T.user("批量调用")),
        T.entry("a1", "u1", T.assistant("", calls)),
        T.entry("t1", "a1", T.tool_result("c1", "结果一")),
        T.entry("t2", "a1", T.tool_result("c2", "结果二")),
        T.entry("u2", "t2", T.user("下一轮" * 400)),
        T.entry("a2", "u2", T.assistant("收尾")),
    ]
    preparation = C.prepare_compaction(path, 128000, 16384, T.SMALL)
    assert preparation.first_kept_entry_id == "u2"
    assert summarized_call_ids(preparation.messages_to_summarize) == ["c1", "c2"]
    result_ids = [
        message.tool_call_id
        for message in preparation.messages_to_summarize
        if isinstance(message, ToolResultMessage)
    ]
    assert result_ids == ["c1", "c2"]


def test_tool_batch_kept_whole_when_cut_at_assistant_call() -> None:
    calls = (
        ToolCall(type="toolCall", id="c1", name="echo", arguments={"payload": "x" * 4000}),
        ToolCall(type="toolCall", id="c2", name="echo", arguments={"payload": "x" * 4000}),
    )
    path = [
        T.entry("sys", None, T.system_message()),
        T.entry("u1", "sys", T.user("批量调用")),
        T.entry("a1", "u1", T.assistant("", calls)),
        T.entry("t1", "a1", T.tool_result("c1", "结果一")),
        T.entry("t2", "a1", T.tool_result("c2", "结果二")),
        T.entry("u2", "t2", T.user("收尾")),
    ]
    preparation = C.prepare_compaction(path, 128000, 16384, T.SMALL)
    assert preparation.first_kept_entry_id == "a1"
    assert preparation.is_split_turn is True
    assert summarized_call_ids(preparation.messages_to_summarize) == []
    assert summarized_call_ids(preparation.turn_prefix_messages) == []
    assert [message.content for message in preparation.turn_prefix_messages] == ["批量调用"]

    for keep in range(1, 4000, 137):
        cut = C.find_cut_point(path, 0, len(path), keep)
        assert path[cut.first_kept_entry_index].messages[0].role in {"user", "assistant"}


def test_attachment_accumulation_dedup_and_relative_paths() -> None:
    previous = T.compaction_entry(
        "c1", "a1", "a1", details=CompactionDetails(attachments=[T.reference(T.ATT_A)])
    )
    path = [
        T.entry("sys", None, T.system_message()),
        T.entry("u1", "sys", T.user("问题一" * 20), attachments=[T.attachment(T.ATT_A)]),
        T.entry("a1", "u1", T.assistant("回答一" * 20)),
        previous,
        T.entry(
            "u2",
            "c1",
            T.user("问题二" * 20),
            attachments=[T.attachment(T.ATT_B, "plan.txt"), T.attachment(T.ATT_A)],
        ),
        T.entry("a2", "u2", T.assistant("回答二" * 20)),
        T.entry("u3", "a2", T.user("问题三" * 400)),
        T.entry("a3", "u3", T.assistant("回答三")),
    ]
    preparation = C.prepare_compaction(path, 128000, 16384, T.SMALL)
    assert preparation.first_kept_entry_id == "u3"
    assert [item.attachment_id for item in preparation.attachments] == [T.ATT_A, T.ATT_B]
    assert T.reference(T.ATT_A) in preparation.attachments
    assert T.reference(T.ATT_B, "plan.txt") in preparation.attachments
    assert all(item.path.startswith("sessions/") for item in preparation.attachments)
    assert all("tmp/" not in item.path for item in preparation.attachments)


def illegal_paths() -> list[tuple[list, str]]:
    external = SessionMessageEntry(
        session_id=EXTERNAL_SESSION,
        id="ux",
        parent_id=None,
        run_id=None,
        type="message",
        messages=[T.user("外部会话")],
        created_at=T.tick(),
    )
    return [
        (
            [
                T.entry("sys", None, T.system_message()),
                T.entry("u1", "sys", T.user("问题")),
                T.entry("a1", "u1", T.assistant("回答")),
                T.compaction_entry("c1", "a1", "ghost"),
                T.entry("u2", "c1", T.user("后续")),
            ],
            "不在当前路径",
        ),
        (
            [
                T.entry("sys", None, T.system_message()),
                T.entry("u1", "sys", T.user("问题")),
                T.entry("a1", "u1", T.assistant("回答")),
                T.compaction_entry("c1", "a1", "u1"),
                T.compaction_entry("c2", "c1", "c1"),
                T.entry("u2", "c2", T.user("后续")),
            ],
            "不是真实消息节点",
        ),
        (
            [
                external,
                T.compaction_entry("c1", "ux", "ux"),
                T.entry("u2", "c1", T.user("后续")),
            ],
            "不属于会话",
        ),
        (
            [
                T.entry("sys", None, T.system_message()),
                T.entry("u1", "sys", T.user("问题")),
                T.entry("a1", "u1", T.assistant("回答")),
                T.compaction_entry("c1", "a1", "u2"),
                T.entry("u2", "c1", T.user("后续")),
                T.entry("a2", "u2", T.assistant("收尾")),
            ],
            "不在当前路径",
        ),
    ]


@pytest.mark.parametrize("path,message", illegal_paths())
def test_invalid_first_kept_entry_id_reports_in_place(path: list, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        C.build_effective_context(path)
    with pytest.raises(ValueError, match=message):
        C.prepare_compaction(path, 128000, 16384, T.SMALL)


async def test_real_sqlite_path_retains_and_rolls_over() -> None:
    root = TEMP_ROOT / uuid4().hex
    root.mkdir(parents=True)
    database = await open_database(root / "database.sqlite")
    repository = SqliteSessionRepository(database)
    service = SessionService(repository)
    session_id = T.SESSION
    try:
        await service.create_session(session_id, "投影")
        send = await service.accept_send(
            SendCommand(
                operation_id=str(uuid4()),
                session_id=session_id,
                request=SendRequest(text="开始训练计划" * 30),
            ),
            system_message=T.system_message(),
        )
        run = send.run
        request_entry = await service.get_entry(session_id, run.request_entry_id)
        await service.append_entry(
            SessionMessageEntry(
                session_id=session_id,
                id="a1",
                parent_id=request_entry.id,
                run_id=run.id,
                type="message",
                messages=[T.assistant("计划已生成")],
                created_at=T.tick(),
            )
        )
        compaction = T.compaction_entry("c1", "a1", request_entry.id).model_copy(
            update={"run_id": run.id}
        )
        new_user = SessionMessageEntry(
            session_id=session_id,
            id="u4",
            parent_id="c1",
            run_id=run.id,
            type="message",
            messages=[T.user("追加请求" * 500)],
            created_at=T.tick(),
        )
        async with repository.transaction():
            await repository.insert_entry(compaction)
            await repository.insert_entry(new_user)
        entries = await service.list_entries(session_id)
        path = build_session_path(session_id, entries, "u4")
        context = C.build_effective_context(path)
        assert context.source_entry_ids == ["c1", "c1", request_entry.id, "a1", "u4"]
        assert len(context.messages) == len(context.source_entry_ids)
        preparation = C.prepare_compaction(
            path,
            128000,
            16384,
            C.CompactionSettings(reserve_tokens=16384, keep_recent_tokens=100),
        )
        assert preparation.first_kept_entry_id == "u4"
        assert preparation.previous_summary == "旧摘要"
        assert any(
            isinstance(message, UserMessage) and "开始训练计划" in message.content
            for message in preparation.messages_to_summarize
        )
    finally:
        await database.close()


def check() -> None:
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    test_retained_messages_before_checkpoint_remain_visible()
    test_next_compaction_summarizes_previous_retained_messages()
    test_multiple_compactions_use_latest_checkpoint()
    test_tool_batch_summarized_whole_when_cut_at_later_user()
    test_tool_batch_kept_whole_when_cut_at_assistant_call()
    test_attachment_accumulation_dedup_and_relative_paths()
    for path, message in illegal_paths():
        test_invalid_first_kept_entry_id_reports_in_place(path, message)
    asyncio.run(test_real_sqlite_path_retains_and_rolls_over())
    print("压缩投影：保留区间、重复压缩、多次检查点、工具批次、非法起点与附件累计通过")


if __name__ == "__main__":
    check()
