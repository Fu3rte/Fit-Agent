# 阶段 3 测试：token 估算、阈值、切点与压缩摘要。
# 依据：CHAT_HISTORY_IMPLEMENTATION_PLAN.md §3.4 与阶段 3 测试用例；
#       Pi compaction/compaction.ts:147-253、:323-478、:762-1024 与 test/compaction.test.ts。

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from app.domain.conversations.compaction import (
    DEFAULT_COMPACTION_SETTINGS,
    SUMMARIZATION_SYSTEM_PROMPT,
    SUMMARY_PROMPT,
    CompactionSettings,
    EmptyCompactionSummary,
    build_summary_prompt,
    calculate_context_tokens,
    compact,
    estimate_context_tokens,
    estimate_entry_tokens,
    estimate_tokens,
    find_cut_point,
    prepare_compaction,
    should_compact,
)
from app.domain.conversations.context import (
    ContextMessage,
    build_context_entries,
    context_messages,
    entry_to_context_messages,
)
from app.domain.conversations.schema import (
    ConversationEntry,
    MessageRole,
    MessageStatus,
)
from app.infrastructure.database.connection import Database
from app.infrastructure.database.repositories.conversations_repository import (
    ConversationRepo,
)

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
        },
        created_at=STAMP,
    )


def compaction_entry(
    entry_id: str,
    sequence: int,
    *,
    summary: str,
    first_kept_entry_id: str,
    tokens_before: int = 1000,
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
        },
        created_at=STAMP,
    )


def rounds(count: int, *, size: int = 40) -> list[ConversationEntry]:
    """``count`` 轮完整问答（默认每条 10 token），序号按追加顺序递增。"""
    entries: list[ConversationEntry] = []
    for index in range(count):
        user_id = str(index * 2 + 1)
        assistant_id = str(index * 2 + 2)
        entries.append(
            message_entry(
                user_id,
                index * 2 + 1,
                role="user",
                content="u" * size,
                run_id=f"run-{index}",
            )
        )
        entries.append(
            message_entry(
                assistant_id,
                index * 2 + 2,
                role="assistant",
                content="a" * size,
                run_id=f"run-{index}",
            )
        )
    return entries


# ---------- token 估算 ----------


def test_calculate_context_tokens_prefers_total_then_components() -> None:
    """``total_tokens`` 优先；缺失时累加四个组件键；读不到即 0。"""
    assert calculate_context_tokens({"total_tokens": 1800, "input": 1, "output": 2}) == 1800
    assert calculate_context_tokens({"input": 100, "output": 50, "cache_read": 30}) == 180
    assert calculate_context_tokens({"input": 7}) == 7
    assert calculate_context_tokens({"total_tokens": 0, "input": 9}) == 9
    assert calculate_context_tokens({}) == 0
    assert calculate_context_tokens(None) == 0
    assert calculate_context_tokens({"total_tokens": "1800", "input": 3}) == 3


def test_estimate_tokens_is_chars_over_four_and_deterministic() -> None:
    """单一集中估算函数：字符数／4 向上取整，同一输入恒等。"""
    assert estimate_tokens(ContextMessage(role="user", text="")) == 0
    assert estimate_tokens(ContextMessage(role="user", text="abcd")) == 1
    assert estimate_tokens(ContextMessage(role="user", text="abcde")) == 2
    message = ContextMessage(role="assistant", text="x" * 401)
    assert estimate_tokens(message) == estimate_tokens(message) == 101


def test_estimate_context_tokens_anchors_on_last_valid_assistant_usage() -> None:
    """锚点取最后一条带有效 usage 的完整 Assistant，其后逐条字符估算。"""
    entries = [
        message_entry("1", 1, role="user", content="hello"),
        message_entry(
            "2", 2, role="assistant", content="hi", usage={"total_tokens": 150}
        ),
        message_entry("3", 3, role="user", content="continue", run_id="run-2"),
        message_entry(
            "4",
            4,
            role="assistant",
            content="partial thinking",
            status="partial",
            run_id="run-2",
            usage={"total_tokens": 900},
        ),
    ]

    assert estimate_context_tokens(build_context_entries(entries)) == 150 + estimate_tokens(
        ContextMessage(role="user", text="continue")
    )


def test_estimate_context_tokens_falls_back_to_character_estimate() -> None:
    """没有可用 usage 锚点时整段字符估算。"""
    entries = rounds(2, size=400)

    assert estimate_context_tokens(build_context_entries(entries)) == 4 * 100


def test_estimate_context_tokens_counts_compaction_summary_without_wrapper() -> None:
    """compaction Entry 只按 ``payload['summary']`` 估算：渲染边界标记不计入上下文规模。"""
    entries = [
        message_entry("1", 1, role="user", content="u" * 400),
        compaction_entry("2", 2, summary="摘要摘要", first_kept_entry_id="1"),
    ]
    rendered = entry_to_context_messages(entries[1])[0]

    assert estimate_entry_tokens(entries[1]) == 1
    assert estimate_tokens(rendered) == 11
    assert estimate_context_tokens(entries) == 100 + 1


def test_estimate_context_tokens_trailing_ignores_compaction_wrapper() -> None:
    """锚点之后的 compaction Entry 同样只计摘要文本：尾部估算不因边界标记胀大。"""
    entries = [
        message_entry("1", 1, role="user", content="u" * 400),
        message_entry(
            "2", 2, role="assistant", content="a" * 400, usage={"total_tokens": 150}
        ),
        compaction_entry("3", 3, summary="摘要摘要", first_kept_entry_id="1"),
    ]

    assert estimate_context_tokens(entries) == 151


# ---------- 阈值 ----------


def test_should_compact_only_above_window_minus_reserve() -> None:
    """触发条件严格为 ``context > window - reserve``。"""
    settings = CompactionSettings(reserve_tokens=10000, keep_recent_tokens=20000)

    assert should_compact(90001, 100000, settings) is True
    assert should_compact(90000, 100000, settings) is False


# ---------- 切点 ----------


def test_find_cut_point_keeps_recent_complete_rounds() -> None:
    """预算只覆盖最后一轮时，切点落在该轮的 user 消息上，保留区间以完整轮次开头。"""
    entries = rounds(6)

    cut = find_cut_point(entries, 0, len(entries), 30)

    assert cut == 10
    assert entries[cut].payload["role"] == "user"
    assert [entry.id for entry in entries[cut:]] == ["11", "12"]


def test_find_cut_point_keeps_everything_when_budget_covers_all() -> None:
    """预算足够时不切分：切点就是起始下标。"""
    entries = rounds(6)

    assert find_cut_point(entries, 0, len(entries), 50000) == 0


def test_find_cut_point_returns_start_when_range_has_no_user_entry() -> None:
    """范围内没有 user 语义 Entry 时返回起始下标：不自造切点、不越界。"""
    entries = [
        message_entry("1", None, role="user", content="提问"),
        message_entry("2", "1", role="assistant", content="回答"),
    ]

    assert find_cut_point(entries, 1, len(entries), 1000) == 1


def test_find_cut_point_ignores_compaction_wrapper_text() -> None:
    """切点预算按摘要文本累计：若把边界标记也计入，切点会被提前到后一轮。"""
    entries = [
        message_entry("1", None, role="user", content="u" * 400),
        message_entry("2", "1", role="assistant", content="a" * 400),
        message_entry("3", "2", role="user", content="m" * 40, run_id="run-2"),
        message_entry("4", "3", role="assistant", content="n" * 12, run_id="run-2"),
        compaction_entry("5", "4", summary="摘要摘要", first_kept_entry_id="1"),
        message_entry("6", "5", role="user", content="t" * 40, run_id="run-3"),
        message_entry("7", "6", role="assistant", content="s" * 40, run_id="run-3"),
    ]

    # 预算 25：只计摘要时累计在下标 2 越过预算；把 11 token 的边界标记算进去则在下标 4 就越过。
    cut = find_cut_point(entries, 0, len(entries), 25)

    assert cut == 2
    assert entries[cut].id == "3"
    # 预算 31 = 边界标记版本的累计量（11+10+10）：计入标记时切点会落到下标 5（Entry 6）。
    assert find_cut_point(entries, 0, len(entries), 31) == 2


def test_find_cut_point_never_lands_on_assistant_or_compaction() -> None:
    """切点只落在 user 语义的 Entry：assistant 与 compaction 都不作切点。"""
    entries = rounds(5) + [
        compaction_entry("11", 11, summary="摘要", first_kept_entry_id="1")
    ]

    for budget in range(1, 200):
        cut = find_cut_point(entries, 0, len(entries), budget)
        assert entries[cut].entry_type != "compaction"
        assert entry_to_context_messages(entries[cut])[0].role == "user"


def test_find_cut_point_keeps_confirmation_with_its_following_result() -> None:
    """确认动作与它后面的等待／结果语义不被切开：切点落在确认 Entry 本身。"""
    entries = [
        message_entry("1", 1, role="user", content="u" * 400),
        message_entry("2", 2, role="assistant", content="a" * 400),
        message_entry("3", 3, role="user", content="生成计划", run_id="run-2"),
        message_entry("4", 4, role="assistant", content="计划草稿待确认", run_id="run-2"),
        ConversationEntry(
            id="5",
            conversation_id=CONVERSATION_ID,
            sequence=5,
            entry_type="confirmation",
            payload={
                "action": "plan_confirmed",
                "run_id": "run-2",
                "draft_plan_id": 3,
                "text": "用户已确认计划 #3",
            },
            created_at=STAMP,
        ),
        message_entry("6", 6, role="assistant", content="计划已生效", run_id="run-2"),
        message_entry("7", 7, role="user", content="再加一组硬拉", run_id="run-3"),
        message_entry("8", 8, role="assistant", content="已加到计划", run_id="run-3"),
    ]
    # 预算 9 token 恰好等于确认 Entry、其结果与下一轮的估算之和：切点只能落在确认本身。
    settings = CompactionSettings(keep_recent_tokens=9)

    cut = find_cut_point(entries, 0, len(entries), settings.keep_recent_tokens)
    preparation = prepare_compaction(entries, settings)

    assert cut == 4
    assert entries[cut].entry_type == "confirmation"
    assert [entry.id for entry in entries[cut:]] == ["5", "6", "7", "8"]
    assert preparation is not None
    assert preparation.first_kept_entry_id == "5"
    summarized = "".join(message.text for message in preparation.messages_to_summarize)
    assert "用户已确认计划 #3" not in summarized
    assert "计划已生效" not in summarized
    assert "生成计划" in summarized


def test_find_cut_point_absorbs_adjacent_display_only_entries() -> None:
    """切点紧邻的不可见 Entry（失败 Assistant）留在保留区间内，展示不出现空档。"""
    entries = [
        message_entry("1", 1, role="user", content="提问"),
        message_entry("2", 2, role="assistant", content="失败片段", status="failed"),
        message_entry("3", 3, role="user", content="重试", run_id="run-2"),
        message_entry("4", 4, role="assistant", content="回答", run_id="run-2"),
    ]

    cut = find_cut_point(entries, 0, len(entries), 1)

    assert cut == 1
    assert entry_to_context_messages(entries[1]) == ()


# ---------- 压缩准备 ----------


def test_prepare_compaction_skips_when_nothing_to_summarize() -> None:
    """保留区间覆盖全部消息时不需要压缩。"""
    entries = rounds(2, size=8)

    assert prepare_compaction(entries, DEFAULT_COMPACTION_SETTINGS) is None


def test_prepare_compaction_skips_when_last_entry_is_compaction() -> None:
    """末尾已是 compaction Entry：同一批消息不重复生成压缩。"""
    entries = rounds(2, size=400) + [
        compaction_entry("5", 5, summary="摘要", first_kept_entry_id="1")
    ]

    assert prepare_compaction(entries, CompactionSettings(keep_recent_tokens=10)) is None


def test_prepare_compaction_skips_when_kept_window_still_fits() -> None:
    """已有 compaction 且保留区间仍在预算内：不生成新压缩，旧边界继续沿用。"""
    entries = rounds(2, size=400) + [
        compaction_entry("5", 5, summary="第一次摘要", first_kept_entry_id="3"),
        message_entry("6", 6, role="user", content="u" * 20, run_id="run-2"),
        message_entry("7", 7, role="assistant", content="a" * 20, run_id="run-2"),
    ]

    assert (
        prepare_compaction(entries, CompactionSettings(keep_recent_tokens=50000))
        is None
    )


def test_prepare_compaction_reports_boundary_and_tokens_before() -> None:
    """准备结果给出保留边界、待摘要消息与压缩前的上下文规模。"""
    entries = rounds(4, size=400)
    settings = CompactionSettings(keep_recent_tokens=110)

    preparation = prepare_compaction(entries, settings)

    assert preparation is not None
    assert preparation.first_kept_entry_id == "7"
    assert preparation.previous_summary is None
    assert [entry.id for entry in entries[:6]] == ["1", "2", "3", "4", "5", "6"]
    assert len(preparation.messages_to_summarize) == 6
    assert preparation.messages_to_summarize[-1].text == "a" * 400
    assert preparation.tokens_before == estimate_context_tokens(
        build_context_entries(entries)
    )


def test_prepare_compaction_continues_from_previous_boundary() -> None:
    """二次压缩从上一次的保留边界继续，并携带上一次摘要做增量更新。"""
    entries = rounds(3, size=400) + [
        compaction_entry("7", 7, summary="第一次摘要", first_kept_entry_id="3")
    ]
    entries.extend(
        [
            message_entry("8", 8, role="user", content="u" * 400, run_id="run-3"),
            message_entry("9", 9, role="assistant", content="a" * 400, run_id="run-3"),
        ]
    )
    settings = CompactionSettings(keep_recent_tokens=110)

    preparation = prepare_compaction(entries, settings)

    assert preparation is not None
    assert preparation.previous_summary == "第一次摘要"
    assert preparation.first_kept_entry_id == "8"
    summarized = "".join(message.text for message in preparation.messages_to_summarize)
    assert summarized == ("u" * 400 + "a" * 400) * 2
    assert "第一次摘要" not in summarized


# ---------- 摘要提示与模型调用 ----------


def test_build_summary_prompt_is_stable_and_carries_previous_summary() -> None:
    """提示形状固定：``<conversation>`` 加初始规则；有上一次摘要时附 ``<previous-summary>``。"""
    entries = rounds(2, size=400)
    preparation = prepare_compaction(entries, CompactionSettings(keep_recent_tokens=10))
    assert preparation is not None

    prompt = build_summary_prompt(preparation)

    assert prompt == build_summary_prompt(preparation)
    assert prompt.startswith("<conversation>\n")
    assert "[User]: " in prompt and "[Assistant]: " in prompt
    assert prompt.endswith(SUMMARY_PROMPT)
    assert "<previous-summary>" not in prompt

    updated = prepare_compaction(
        entries
        + [compaction_entry("5", 5, summary="上一次摘要", first_kept_entry_id="3")]
        + [message_entry("6", 6, role="user", content="u" * 400, run_id="run-3")],
        CompactionSettings(keep_recent_tokens=10),
    )
    assert updated is not None
    prompt_with_previous = build_summary_prompt(updated)

    assert updated.previous_summary == "上一次摘要"
    assert "<previous-summary>\n上一次摘要\n</previous-summary>" in prompt_with_previous
    assert not prompt_with_previous.endswith(SUMMARY_PROMPT)


async def test_compact_calls_injected_model_call_with_stable_prompt() -> None:
    """摘要由注入的模型调用产生；system prompt 与 user 提示都来自本层。"""
    entries = rounds(2, size=400)
    preparation = prepare_compaction(entries, CompactionSettings(keep_recent_tokens=10))
    assert preparation is not None
    calls: list[tuple[str, str]] = []

    async def model_call(system_prompt: str, user_payload: str) -> str:
        calls.append((system_prompt, user_payload))
        return "  ## 用户目标\n 改成周三  \n"

    result = await compact(preparation, model_call)

    assert calls == [
        (SUMMARIZATION_SYSTEM_PROMPT, build_summary_prompt(preparation))
    ]
    assert result.summary == "## 用户目标\n 改成周三"
    assert result.first_kept_entry_id == preparation.first_kept_entry_id
    assert result.tokens_before == preparation.tokens_before
    assert result.to_payload() == {
        "summary": "## 用户目标\n 改成周三",
        "first_kept_entry_id": preparation.first_kept_entry_id,
        "tokens_before": preparation.tokens_before,
    }


async def test_compact_propagates_summary_failure() -> None:
    """摘要调用失败与空白摘要都原地报错：不生成任何压缩结果。"""
    entries = rounds(2, size=400)
    preparation = prepare_compaction(entries, CompactionSettings(keep_recent_tokens=10))
    assert preparation is not None

    async def failing_model_call(system_prompt: str, user_payload: str) -> str:
        raise RuntimeError("模型调用失败")

    with pytest.raises(RuntimeError, match="模型调用失败"):
        await compact(preparation, failing_model_call)

    async def blank_model_call(system_prompt: str, user_payload: str) -> str:
        return "   \n"

    with pytest.raises(EmptyCompactionSummary, match="空白文本"):
        await compact(preparation, blank_model_call)


# ---------- 真实库上的压缩闭环 ----------


@asynccontextmanager
async def _harness(tmp_path: Path) -> AsyncIterator[tuple[Database, ConversationRepo]]:
    db = Database(tmp_path / "fit_agent.db")
    await db.open()
    try:
        await db.migrate()
        yield db, ConversationRepo(db)
    finally:
        await db.close()


async def _append_round(
    repo: ConversationRepo,
    db: Database,
    conversation_id: str,
    index: int,
    *,
    content_size: int,
) -> None:
    """真实写入一轮问答：用户 Entry＋pending Run，再补完整 Assistant Entry。"""
    async with db.transaction() as conn:
        _run, _user_entry = await repo.begin_run_in_transaction(
            conn,
            conversation_id=conversation_id,
            run_id=f"run-{index}",
            thread_id=f"thread-{index}",
            client_request_id=f"request-{index}",
            entry_id=f"u{index}",
            content="u" * content_size,
            created_at=STAMP,
        )
        await repo.update_run_status_in_transaction(
            conn, f"run-{index}", status="running", updated_at=STAMP
        )
        await repo.complete_run_in_transaction(
            conn,
            f"run-{index}",
            entry_id=f"a{index}",
            content="a" * content_size,
            created_at=STAMP,
            usage={"total_tokens": (index + 1) * 1000},
        )


async def _append_compaction(
    repo: ConversationRepo,
    db: Database,
    conversation_id: str,
    entry_id: str,
    *,
    summary: str,
    first_kept_entry_id: str,
    tokens_before: int,
) -> ConversationEntry:
    async with db.transaction() as conn:
        return await repo.append_entry_in_transaction(
            conn,
            conversation_id=conversation_id,
            entry_id=entry_id,
            entry_type="compaction",
            payload={
                "summary": summary,
                "first_kept_entry_id": first_kept_entry_id,
                "tokens_before": tokens_before,
            },
            created_at=STAMP,
        )


async def test_compaction_keeps_original_entries_and_rebuilds_context(
    tmp_path: Path,
) -> None:
    """超阈值时只生成一条 Compaction Entry：原始 Entry 不减，上下文变为摘要＋保留区间。"""
    async with _harness(tmp_path) as (db, repo):
        await repo.create_conversation(
            conversation_id=CONVERSATION_ID, title="压缩", created_at=STAMP
        )
        for index in range(4):
            await _append_round(repo, db, CONVERSATION_ID, index, content_size=400)
        settings = CompactionSettings(reserve_tokens=100, keep_recent_tokens=110)
        before = await repo.list_entries(CONVERSATION_ID)
        entries = await repo.list_entries(CONVERSATION_ID)
        estimate = estimate_context_tokens(build_context_entries(entries))

        assert should_compact(estimate, 1000, settings) is True

        preparation = prepare_compaction(entries, settings)
        assert preparation is not None
        result = await compact(preparation, _stub_model_call("## 用户目标\n减脂"))

        await _append_compaction(
            repo,
            db,
            CONVERSATION_ID,
            "c1",
            summary=result.summary,
            first_kept_entry_id=result.first_kept_entry_id,
            tokens_before=result.tokens_before,
        )

        after = await repo.list_entries(CONVERSATION_ID)
        assert {entry.id for entry in after} == {entry.id for entry in before} | {"c1"}
        assert [entry.entry_type for entry in after].count("compaction") == 1

        rebuilt = await repo.list_entries(CONVERSATION_ID)
        context_entries = build_context_entries(rebuilt)
        first_kept_index = [entry.id for entry in rebuilt].index(
            result.first_kept_entry_id
        )
        assert [entry.id for entry in context_entries] == ["c1"] + [
            entry.id for entry in rebuilt[first_kept_index:-1]
        ]
        assert context_messages(context_entries)[0].text.startswith(
            "此前的对话历史已压缩为以下摘要："
        )
        assert (
            prepare_compaction(rebuilt, settings) is None
        ), "同一批消息只生成一条 Compaction Entry"


async def test_second_compaction_continues_from_previous_boundary(tmp_path: Path) -> None:
    """二次压缩从上次边界继续：新摘要覆盖上一批保留消息，原摘要不被重复摘要。"""
    async with _harness(tmp_path) as (db, repo):
        await repo.create_conversation(
            conversation_id=CONVERSATION_ID, title="二次压缩", created_at=STAMP
        )
        for index in range(4):
            await _append_round(repo, db, CONVERSATION_ID, index, content_size=400)
        settings = CompactionSettings(reserve_tokens=100, keep_recent_tokens=110)

        first_preparation = prepare_compaction(
            await repo.list_entries(CONVERSATION_ID), settings
        )
        assert first_preparation is not None
        first_result = await compact(
            first_preparation, _stub_model_call("第一次摘要")
        )
        await _append_compaction(
            repo,
            db,
            CONVERSATION_ID,
            "c1",
            summary=first_result.summary,
            first_kept_entry_id=first_result.first_kept_entry_id,
            tokens_before=first_result.tokens_before,
        )

        for index in range(4, 7):
            await _append_round(repo, db, CONVERSATION_ID, index, content_size=400)

        second_preparation = prepare_compaction(
            await repo.list_entries(CONVERSATION_ID), settings
        )
        assert second_preparation is not None
        assert second_preparation.previous_summary == "第一次摘要"
        assert second_preparation.first_kept_entry_id != first_result.first_kept_entry_id
        summarized = "".join(
            message.text for message in second_preparation.messages_to_summarize
        )
        assert "第一次摘要" not in summarized
        assert "u" * 400 in summarized

        second_result = await compact(second_preparation, _stub_model_call("第二次摘要"))
        await _append_compaction(
            repo,
            db,
            CONVERSATION_ID,
            "c2",
            summary=second_result.summary,
            first_kept_entry_id=second_result.first_kept_entry_id,
            tokens_before=second_result.tokens_before,
        )

        rebuilt = build_context_entries(await repo.list_entries(CONVERSATION_ID))
        assert [entry.id for entry in rebuilt][0] == "c2"
        assert "c1" not in {entry.id for entry in rebuilt}
        conversation = await repo.read_conversation(CONVERSATION_ID)
        assert conversation is not None
        assert conversation.updated_at == STAMP
        assert (await repo.list_entries(CONVERSATION_ID))[-1].id == "c2"


async def test_summary_failure_leaves_history_usable(tmp_path: Path) -> None:
    """摘要失败时原历史仍是完整可用的上下文，且不落任何 Compaction Entry。"""
    async with _harness(tmp_path) as (db, repo):
        await repo.create_conversation(
            conversation_id=CONVERSATION_ID, title="摘要失败", created_at=STAMP
        )
        for index in range(2):
            await _append_round(repo, db, CONVERSATION_ID, index, content_size=400)
        settings = CompactionSettings(reserve_tokens=100, keep_recent_tokens=10)
        entries = await repo.list_entries(CONVERSATION_ID)
        preparation = prepare_compaction(entries, settings)
        assert preparation is not None

        async def failing_model_call(system_prompt: str, user_payload: str) -> str:
            raise RuntimeError("provider 不可用")

        with pytest.raises(RuntimeError, match="provider 不可用"):
            await compact(preparation, failing_model_call)

        rebuilt = await repo.list_entries(CONVERSATION_ID)
        assert [entry.id for entry in rebuilt] == [entry.id for entry in entries]
        assert await repo.read_latest_compaction(CONVERSATION_ID) is None
        assert [
            message.role
            for message in context_messages(build_context_entries(rebuilt))
        ] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]


def _stub_model_call(summary: str):
    async def model_call(system_prompt: str, user_payload: str) -> str:
        return summary

    return model_call
