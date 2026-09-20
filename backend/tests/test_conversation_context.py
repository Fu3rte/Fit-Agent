# 阶段 2 测试：同一批持久化数据稳定生成展示投影与模型上下文投影。
# 依据：CHAT_HISTORY_IMPLEMENTATION_PLAN.md §3.3 与阶段 2 测试用例；
#       Pi session-manager.ts:392-470、transform-messages.ts:196-206、
#       test/session-manager/build-context.test.ts。

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from domain.conversations.context import (
    MissingConversationRun,
    build_context_entries,
    build_conversation_display,
    context_messages,
    entry_to_context_messages,
)
from domain.conversations.repo import ConversationRepo
from domain.conversations.schema import (
    ConfirmationAction,
    ConversationEntry,
    ConversationRun,
    MessageRole,
    MessageStatus,
    RunEvent,
    RunStatus,
)
from storage.db import Database

STAMP = "2026-06-01T09:00:00+00:00"
CONVERSATION_ID = "conv-1"


def message_entry(
    entry_id: str,
    sequence: int,
    *,
    role: MessageRole,
    content: str,
    status: MessageStatus = "complete",
    run_id: str = "run-1",
    usage: Mapping[str, Any] | None = None,
    created_at: str = STAMP,
) -> ConversationEntry:
    return ConversationEntry(
        id=entry_id,
        conversation_id=CONVERSATION_ID,
        sequence=sequence,
        entry_type="message",
        payload={
            "role": role,
            "content": content,
            "status": status,
            "run_id": run_id,
            "usage": usage,
            "provider": None,
            "model": None,
        },
        created_at=created_at,
    )


def confirmation_entry(
    entry_id: str,
    sequence: int,
    *,
    text: str,
    action: ConfirmationAction = "plan_confirmed",
    run_id: str = "run-1",
    created_at: str = STAMP,
) -> ConversationEntry:
    payload: dict[str, Any] = {"action": action, "run_id": run_id, "text": text}
    if action == "workout_confirmed":
        payload["workout_session_id"] = 7
    else:
        payload["draft_plan_id"] = 3
    return ConversationEntry(
        id=entry_id,
        conversation_id=CONVERSATION_ID,
        sequence=sequence,
        entry_type="confirmation",
        payload=payload,
        created_at=created_at,
    )


def compaction_entry(
    entry_id: str,
    sequence: int,
    *,
    summary: str,
    first_kept_entry_id: str,
    tokens_before: int = 1000,
    created_at: str = STAMP,
) -> ConversationEntry:
    return ConversationEntry(
        id=entry_id,
        conversation_id=CONVERSATION_ID,
        sequence=sequence,
        entry_type="compaction",
        payload={
            "summary": summary,
            "first_kept_entry_id": first_kept_entry_id,
            "tokens_before": tokens_before,
            "usage": None,
        },
        created_at=created_at,
    )


def conversation_run(
    run_id: str,
    *,
    user_entry_id: str,
    assistant_entry_id: str | None = None,
    status: RunStatus = "completed",
) -> ConversationRun:
    return ConversationRun(
        id=run_id,
        conversation_id=CONVERSATION_ID,
        thread_id=f"thread-{run_id}",
        client_request_id=f"request-{run_id}",
        user_entry_id=user_entry_id,
        assistant_entry_id=assistant_entry_id,
        status=status,
        error_code="graph_error" if status == "failed" else None,
        created_at=STAMP,
        updated_at=STAMP,
    )


def run_event(run_id: str, sequence: int, event_type: str = "message") -> RunEvent:
    return RunEvent(
        id=sequence,
        run_id=run_id,
        sequence=sequence,
        event_type=event_type,
        payload={"text": f"{event_type}-{sequence}"},
        created_at=STAMP,
    )


def roles_and_texts(entries: Sequence[ConversationEntry]) -> list[tuple[str, str]]:
    return [(m.role, m.text) for m in context_messages(entries)]


# ---------- 模型上下文投影 ----------


def test_complete_user_and_assistant_messages_enter_context() -> None:
    """完整 user／assistant 按 sequence 顺序进入模型上下文。"""
    entries = [
        message_entry("1", 1, role="user", content="把计划改成周三"),
        message_entry("2", 2, role="assistant", content="已改成周三"),
        message_entry("3", 3, role="user", content="再加一组硬拉", run_id="run-2"),
    ]

    assert roles_and_texts(build_context_entries(entries)) == [
        ("user", "把计划改成周三"),
        ("assistant", "已改成周三"),
        ("user", "再加一组硬拉"),
    ]


def test_failed_partial_aborted_assistant_are_display_only() -> None:
    """失败／部分／中止的 Assistant 文本可展示，但在模型上下文中缺失。"""
    entries = [
        message_entry("1", 1, role="user", content="提问"),
        message_entry(
            "2",
            2,
            role="assistant",
            content="失败片段",
            status="failed",
            usage={"total_tokens": 900},
        ),
        message_entry("3", 3, role="assistant", content="部分片段", status="partial"),
        message_entry("4", 4, role="assistant", content="中止片段", status="aborted"),
        message_entry("5", 5, role="user", content="重试", run_id="run-2"),
    ]

    assert roles_and_texts(build_context_entries(entries)) == [
        ("user", "提问"),
        ("user", "重试"),
    ]
    display = build_conversation_display(
        entries,
        [
            conversation_run("run-1", user_entry_id="1", status="failed"),
            conversation_run("run-2", user_entry_id="5", status="running"),
        ],
        [run_event("run-1", 1)],
    )
    assert [item["status"] for item in display["rounds"][0]["assistants"]] == [
        "failed",
        "partial",
        "aborted",
    ]
    assert [item["content"] for item in display["rounds"][0]["assistants"]] == [
        "失败片段",
        "部分片段",
        "中止片段",
    ]


def test_confirmation_projects_to_stable_user_text() -> None:
    """确认动作的上下文文本就是 payload.text：与 Action、身份字段无关。"""
    entries = [
        message_entry("1", 1, role="user", content="生成计划"),
        message_entry("2", 2, role="assistant", content="待确认"),
        confirmation_entry("3", 3, text="用户已确认计划 #3"),
        message_entry("4", 4, role="user", content="周三改深蹲", run_id="run-2"),
    ]

    assert entry_to_context_messages(entries[2])[0].text == "用户已确认计划 #3"
    assert roles_and_texts(build_context_entries(entries))[2] == (
        "user",
        "用户已确认计划 #3",
    )


def test_run_events_never_enter_model_context() -> None:
    """Run Event 只在展示投影里出现，模型上下文只读 Entry。"""
    entries = [
        message_entry("1", 1, role="user", content="提问"),
        message_entry("2", 2, role="assistant", content="回答"),
    ]
    run = conversation_run("run-1", user_entry_id="1", assistant_entry_id="2")
    events = [run_event("run-1", 1, "node"), run_event("run-1", 2, "done")]

    display = build_conversation_display(entries, [run], events)
    contexts = context_messages(build_context_entries(entries))

    assert [event["event"] for event in display["rounds"][0]["events"]] == [
        "node",
        "done",
    ]
    assert [event["data"]["text"] for event in display["rounds"][0]["events"]] == [
        "node-1",
        "done-2",
    ]
    assert all("node" not in message.text for message in contexts)
    assert all("done" not in message.text for message in contexts)


# ---------- 最新 Compaction 投影 ----------


def test_context_uses_latest_compaction_boundary() -> None:
    """两次压缩时只应用最新一次：摘要在前，保留区间与之后的新 Entry 跟随其后。"""
    entries = [
        message_entry("1", 1, role="user", content="a"),
        message_entry("2", 2, role="assistant", content="b"),
        compaction_entry("3", 3, summary="第一次摘要", first_kept_entry_id="1"),
        message_entry("4", 4, role="user", content="c", run_id="run-2"),
        message_entry("5", 5, role="assistant", content="d", run_id="run-2"),
        compaction_entry("6", 6, summary="第二次摘要", first_kept_entry_id="4"),
        message_entry("7", 7, role="user", content="e", run_id="run-3"),
    ]

    context_entries = build_context_entries(entries)

    assert [entry.id for entry in context_entries] == ["6", "4", "5", "7"]
    assert roles_and_texts(context_entries)[0] == (
        "user",
        "此前的对话历史已压缩为以下摘要：\n\n<summary>\n第二次摘要\n</summary>",
    )
    assert roles_and_texts(context_entries)[1:] == [
        ("user", "c"),
        ("assistant", "d"),
        ("user", "e"),
    ]


def test_context_keeps_all_entries_when_boundary_is_first_entry() -> None:
    """保留边界落在根 Entry 时，摘要之后仍保留区间内的全部 Entry。"""
    entries = [
        message_entry("1", 1, role="user", content="first"),
        message_entry("2", 2, role="assistant", content="response"),
        compaction_entry("3", 3, summary="空区间摘要", first_kept_entry_id="1"),
        message_entry("4", 4, role="user", content="second", run_id="run-2"),
    ]

    context_entries = build_context_entries(entries)

    assert [entry.id for entry in context_entries] == ["3", "1", "2", "4"]
    assert [text for _role, text in roles_and_texts(context_entries)[1:]] == [
        "first",
        "response",
        "second",
    ]


# ---------- 展示投影 ----------


def test_display_round_groups_entries_events_and_compaction() -> None:
    """轮次按 Run 聚合：请求、Assistant 投影、确认投影、有序事件，压缩单独成列。"""
    entries = [
        message_entry(
            "1",
            1,
            role="user",
            content="生成计划",
            created_at="2026-06-01T09:00:00+00:00",
        ),
        message_entry(
            "2",
            2,
            role="assistant",
            content="计划草稿",
            created_at="2026-06-01T09:00:01+00:00",
        ),
        confirmation_entry(
            "3",
            3,
            text="用户已确认计划 #3",
            created_at="2026-06-01T09:00:02+00:00",
        ),
        compaction_entry(
            "4",
            4,
            summary="摘要",
            first_kept_entry_id="1",
            created_at="2026-06-01T09:00:03+00:00",
        ),
    ]
    run = conversation_run("run-1", user_entry_id="1", assistant_entry_id="2")

    display = build_conversation_display(
        entries, [run], [run_event("run-1", 2), run_event("run-1", 1)]
    )

    assert len(display["rounds"]) == 1
    round_ = display["rounds"][0]
    assert round_["run_id"] == "run-1"
    assert round_["conversation_id"] == "thread-run-1"
    assert round_["status"] == "completed"
    assert round_["request"] == "生成计划"
    assert [item["content"] for item in round_["assistants"]] == ["计划草稿"]
    assert [item["text"] for item in round_["confirmations"]] == ["用户已确认计划 #3"]
    assert [event["sequence"] for event in round_["events"]] == [1, 2]
    assert [item["entry_id"] for item in display["compactions"]] == ["4"]


def test_display_ordering_is_deterministic() -> None:
    """轮次顺序取 entries 给出的 sequence 顺序，事件顺序由 ``sequence`` 固定：打乱入参不换结果。"""
    entries = [
        message_entry(
            "1",
            1,
            role="user",
            content="第一轮",
            created_at="2026-06-01T09:00:00+00:00",
        ),
        message_entry(
            "2",
            2,
            role="assistant",
            content="第一答",
            created_at="2026-06-01T09:00:01+00:00",
        ),
        message_entry(
            "3",
            3,
            role="user",
            content="第二轮",
            run_id="run-2",
            created_at="2026-06-01T09:00:02+00:00",
        ),
        message_entry(
            "4",
            4,
            role="assistant",
            content="第二答",
            run_id="run-2",
            created_at="2026-06-01T09:00:03+00:00",
        ),
    ]
    runs = [
        conversation_run("run-1", user_entry_id="1", assistant_entry_id="2"),
        conversation_run("run-2", user_entry_id="3", assistant_entry_id="4"),
    ]
    events = [run_event("run-2", 2), run_event("run-1", 1), run_event("run-2", 1)]

    first = build_conversation_display(entries, runs, events)
    shuffled = build_conversation_display(
        entries, list(reversed(runs)), list(reversed(events))
    )

    assert first == shuffled
    assert [round_["request"] for round_ in first["rounds"]] == ["第一轮", "第二轮"]
    assert [len(round_["events"]) for round_ in first["rounds"]] == [1, 2]


def test_display_keeps_sequence_order_when_timestamps_tie_and_ids_are_reverse_lexical() -> None:
    """同一时间戳且 id 字典序与追加顺序相反时，轮次仍按 sequence 分组，不按 id 排序。"""
    entries = [
        message_entry("b", 1, role="user", content="第一轮"),
        message_entry("a", 2, role="assistant", content="第一答"),
        message_entry("d", 3, role="user", content="第二轮", run_id="run-2"),
        message_entry("c", 4, role="assistant", content="第二答", run_id="run-2"),
    ]
    runs = [
        conversation_run("run-1", user_entry_id="b", assistant_entry_id="a"),
        conversation_run("run-2", user_entry_id="d", assistant_entry_id="c"),
    ]

    display = build_conversation_display(entries, runs, [])

    assert [round_["request"] for round_ in display["rounds"]] == ["第一轮", "第二轮"]
    assert [
        [item["content"] for item in round_["assistants"]]
        for round_ in display["rounds"]
    ] == [["第一答"], ["第二答"]]


def test_display_fails_when_entry_or_event_run_is_absent() -> None:
    """Entry 与 Event 引用的 Run 必须在给定集合内，且用户 Entry 必须在展示范围内。"""
    entries = [
        message_entry("1", 1, role="user", content="提问"),
        message_entry("2", 2, role="assistant", content="回答"),
    ]

    with pytest.raises(MissingConversationRun, match="引用的 Run 不存在"):
        build_conversation_display(entries, [], [])
    with pytest.raises(MissingConversationRun, match="Event 引用的 Run 不存在"):
        build_conversation_display(
            entries,
            [conversation_run("run-1", user_entry_id="1")],
            [run_event("run-2", 1)],
        )
    with pytest.raises(MissingConversationRun, match="用户 Entry 不在展示范围内"):
        build_conversation_display(
            [entries[1]],
            [conversation_run("run-1", user_entry_id="1")],
            [],
        )


# ---------- 真实库上的两层投影 ----------


@asynccontextmanager
async def _harness(tmp_path: Path) -> AsyncIterator[tuple[Database, ConversationRepo]]:
    db = Database(tmp_path / "fit_agent.db")
    await db.open()
    try:
        await db.migrate()
        yield db, ConversationRepo(db)
    finally:
        await db.close()


async def test_persisted_entries_project_to_both_views(tmp_path: Path) -> None:
    """落库数据一次读取：展示投影含失败文本，模型上下文只含完整消息与事件之外的内容。"""
    async with _harness(tmp_path) as (db, repo):
        await repo.create_conversation(
            conversation_id=CONVERSATION_ID, title="两层投影", created_at=STAMP
        )
        async with db.transaction() as conn:
            _run, user_entry = await repo.begin_run_in_transaction(
                conn,
                conversation_id=CONVERSATION_ID,
                run_id="run-1",
                thread_id="thread-1",
                client_request_id="request-1",
                entry_id="1",
                content="生成计划",
                created_at=STAMP,
            )
            await repo.update_run_status_in_transaction(
                conn, "run-1", status="running", updated_at=STAMP
            )
            _run, assistant_entry = await repo.complete_run_in_transaction(
                conn,
                "run-1",
                entry_id="2",
                content="计划草稿",
                created_at=STAMP,
                usage={"total_tokens": 120},
            )
            await repo.append_event_in_transaction(
                conn,
                run_id="run-1",
                sequence=1,
                event_type="done",
                payload={"intent": "plan"},
                created_at=STAMP,
            )
            await repo.begin_run_in_transaction(
                conn,
                conversation_id=CONVERSATION_ID,
                run_id="run-2",
                thread_id="thread-2",
                client_request_id="request-2",
                entry_id="3",
                content="再改一次",
                created_at=STAMP,
            )
            await repo.update_run_status_in_transaction(
                conn, "run-2", status="running", updated_at=STAMP
            )
            failed_entry = await repo.append_entry_in_transaction(
                conn,
                conversation_id=CONVERSATION_ID,
                entry_id="4",
                entry_type="message",
                payload={
                    "role": "assistant",
                    "content": "中断的半句",
                    "status": "failed",
                    "run_id": "run-2",
                },
                created_at=STAMP,
            )
            await repo.update_run_status_in_transaction(
                conn,
                "run-2",
                status="failed",
                updated_at=STAMP,
                error_code="graph_error",
            )

        assert user_entry.payload["content"] == "生成计划"
        assert user_entry.sequence == 1
        assert assistant_entry.payload["status"] == "complete"
        assert failed_entry.sequence == 4

        entries = await repo.list_entries(CONVERSATION_ID)
        planned_run = await repo.read_run("run-1")
        failed_run = await repo.read_run("run-2")
        assert planned_run is not None and failed_run is not None
        events = await repo.list_run_events("run-1")

        assert roles_and_texts(build_context_entries(entries)) == [
            ("user", "生成计划"),
            ("assistant", "计划草稿"),
            ("user", "再改一次"),
        ]
        display = build_conversation_display(entries, [planned_run, failed_run], events)
        assert [round_["request"] for round_ in display["rounds"]] == [
            "生成计划",
            "再改一次",
        ]
        assert [
            assistant["status"]
            for round_ in display["rounds"]
            for assistant in round_["assistants"]
        ] == ["complete", "failed"]
        assert [
            assistant["content"]
            for round_ in display["rounds"]
            for assistant in round_["assistants"]
        ] == ["计划草稿", "中断的半句"]
        assert [event["event"] for event in display["rounds"][0]["events"]] == ["done"]
        assert [entry.id for entry in entries] == ["1", "2", "3", "4"]
