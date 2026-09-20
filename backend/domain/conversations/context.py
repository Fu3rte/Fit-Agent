# Entry 的两种投影：展示投影（含失败／部分／中止内容与 Run Event）与模型上下文投影。
# 依据：Pi session-manager.ts:392-470（sessionEntryToContextMessages／buildContextEntries）、
#       transform-messages.ts:196-206（失败 Assistant 过滤）、build-context.test.ts。

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from domain.conversations.schema import (
    ConversationEntry,
    ConversationRun,
    MessageRole,
    RunEvent,
)

#: 压缩摘要消息的固定边界标记（Pi messages.ts:11-17 的 COMPACTION_SUMMARY_PREFIX／SUFFIX）。
COMPACTION_SUMMARY_PREFIX = "此前的对话历史已压缩为以下摘要：\n\n<summary>\n"
COMPACTION_SUMMARY_SUFFIX = "\n</summary>"


class MissingConversationRun(LookupError):
    """Entry 或 Event 引用的 Run 不在给定集合内：展示投影无法归属到轮次。"""


@dataclass(frozen=True, slots=True)
class ContextMessage:
    """模型上下文投影的一条消息：只有角色与稳定文本，不携带状态、usage 与展示信息。"""

    role: MessageRole
    text: str


def build_context_entries(
    entries: Sequence[ConversationEntry],
) -> tuple[ConversationEntry, ...]:
    """应用会话内最新 Compaction 的重建（Pi buildContextEntries:429-470）。

    输入是会话按 ``sequence`` 升序的全部 Entry。输出 = compaction Entry + 从
    ``first_kept_entry_id`` 起的保留 Entry + compaction 之后的 Entry；更早的已摘要 Entry 被省略，
    原始 Entry 本身不被改动。
    """
    compaction_index = -1
    for index, entry in enumerate(entries):
        if entry.entry_type == "compaction":
            compaction_index = index
    if compaction_index < 0:
        return tuple(entries)
    compaction = entries[compaction_index]
    first_kept_entry_id = compaction.payload["first_kept_entry_id"]
    context_entries: list[ConversationEntry] = [compaction]
    reached_first_kept = False
    for entry in entries[:compaction_index]:
        if entry.id == first_kept_entry_id:
            reached_first_kept = True
        if reached_first_kept:
            context_entries.append(entry)
    context_entries.extend(entries[compaction_index + 1 :])
    return tuple(context_entries)


def entry_to_context_messages(entry: ConversationEntry) -> tuple[ContextMessage, ...]:
    """Entry → 模型上下文消息（Pi sessionEntryToContextMessages:392-421）。

    - 完整 message 投影为对应角色的一条消息。
    - ``partial``／``failed``／``aborted`` message 不进入模型上下文（transform-messages.ts:196-206）。
    - ``confirmation`` 投影为 user 角色的稳定语义文本。
    - ``compaction`` 投影为带边界标记的 user 角色摘要消息。
    """
    if entry.entry_type == "confirmation":
        return (ContextMessage(role="user", text=entry.payload["text"]),)
    if entry.entry_type == "compaction":
        return (
            ContextMessage(
                role="user",
                text=(
                    f"{COMPACTION_SUMMARY_PREFIX}"
                    f"{entry.payload['summary']}"
                    f"{COMPACTION_SUMMARY_SUFFIX}"
                ),
            ),
        )
    if entry.payload["status"] != "complete":
        return ()
    return (
        ContextMessage(role=entry.payload["role"], text=entry.payload["content"]),
    )


def build_conversation_display(
    entries: Sequence[ConversationEntry],
    runs: Sequence[ConversationRun],
    events: Sequence[RunEvent],
) -> dict[str, Any]:
    """Entry／Run／Event → 响应就绪的展示形状（Pi interactive-mode.ts:3584-3770 的显示态范围）。

    ``entries`` 是本次展示的 Entry 集合（调用方传入会话全部 Entry）：轮次顺序与压缩顺序都取
    ``entries`` 给出的 sequence 顺序，同一 Run 的事件按 ``sequence`` 升序；``entries`` 未覆盖的
    Run 不进入结果，Entry 与 Event 引用的 Run 必须位于 ``runs`` 且其用户 Entry 在 ``entries`` 内，
    否则抛 :class:`MissingConversationRun`。Run Event 只出现在展示投影里。

    返回值直接就是传输层轮次与压缩分隔（``rounds``／``compactions``），不再经中间数据类重映射。
    """
    run_by_id: dict[str, ConversationRun] = {}
    for run in runs:
        if run.id in run_by_id:
            raise MissingConversationRun(f"Run id 重复：{run.id!r}")
        run_by_id[run.id] = run
    events_by_run: dict[str, list[RunEvent]] = {}
    for event in events:
        if event.run_id not in run_by_id:
            raise MissingConversationRun(f"Event 引用的 Run 不存在：{event.run_id!r}")
        events_by_run.setdefault(event.run_id, []).append(event)

    rounds: list[dict[str, Any]] = []
    round_by_run: dict[str, dict[str, Any]] = {}
    compactions: list[dict[str, Any]] = []

    for entry in entries:
        if entry.entry_type == "compaction":
            compactions.append(
                {
                    "entry_id": entry.id,
                    "summary": entry.payload["summary"],
                    "first_kept_entry_id": entry.payload["first_kept_entry_id"],
                }
            )
            continue
        run_id = entry.payload["run_id"]
        run = run_by_id.get(run_id)
        if run is None:
            raise MissingConversationRun(f"Entry {entry.id!r} 引用的 Run 不存在：{run_id!r}")
        round_dto = round_by_run.get(run_id)
        if entry.entry_type == "confirmation":
            if round_dto is None:
                raise MissingConversationRun(
                    f"确认 Entry {entry.id!r} 的用户 Entry 不在展示范围内：{run.user_entry_id!r}"
                )
            round_dto["confirmations"].append(
                {
                    "entry_id": entry.id,
                    "action": entry.payload["action"],
                    "text": entry.payload["text"],
                }
            )
            continue
        if entry.payload["role"] == "user":
            if round_dto is not None:
                raise MissingConversationRun(f"Run {run_id!r} 对应多个用户 Entry")
            if entry.id != run.user_entry_id:
                raise MissingConversationRun(
                    f"Run {run_id!r} 的用户 Entry 不匹配：{entry.id!r}"
                )
            round_dto = {
                "run_id": run_id,
                "conversation_id": run.thread_id,
                "status": run.status,
                "request": entry.payload["content"],
                "assistants": [],
                "confirmations": [],
                "events": [
                    {
                        "sequence": event.sequence,
                        "event": event.event_type,
                        "data": event.payload,
                    }
                    for event in sorted(
                        events_by_run.get(run_id, ()), key=lambda event: event.sequence
                    )
                ],
            }
            round_by_run[run_id] = round_dto
            rounds.append(round_dto)
            continue
        if round_dto is None:
            raise MissingConversationRun(
                f"Assistant Entry {entry.id!r} 的用户 Entry 不在展示范围内：{run.user_entry_id!r}"
            )
        round_dto["assistants"].append(
            {
                "entry_id": entry.id,
                "content": entry.payload["content"],
                "status": entry.payload["status"],
            }
        )

    return {"rounds": rounds, "compactions": compactions}


def context_messages(
    context_entries: Sequence[ConversationEntry],
) -> tuple[ContextMessage, ...]:
    """上下文 Entry 序列 → 模型消息序列（跳过没有消息投影的 Entry）。"""
    return tuple(
        message
        for entry in context_entries
        for message in entry_to_context_messages(entry)
    )


def history_payload(messages: Sequence[ContextMessage]) -> dict[str, Any]:
    """模型载荷里的对话历史字段：为空即空字典，展开后 payload 不出现该键。"""
    if not messages:
        return {}
    return {
        "conversation_messages": [
            {"role": message.role, "text": message.text} for message in messages
        ]
    }
