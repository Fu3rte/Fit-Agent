# 上下文压缩：token 估算、切点选择、压缩准备与摘要生成。
# 依据：Pi compaction/compaction.ts:147-253（估算与阈值）、:323-478（切点与最近轮次）、
#       :762-1024（压缩准备与摘要）；摘要模型调用由调用方注入，本层不构造 Provider。

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from math import ceil
from typing import Any

from domain.conversations.context import (
    ContextMessage,
    build_context_entries,
    entry_to_context_messages,
)
from domain.conversations.schema import ConversationEntry

#: 摘要模型调用：与 ``graph.model.ModelCall`` 同形（``system_prompt, user_payload``）→ 文本。
#: Provider 构造留在 ``graph.model``，本层只接受注入。
SummaryModelCall = Callable[[str, str], Awaitable[str]]

#: usage 组件键：对应 Pi Usage 的 input／output／cacheRead／cacheWrite，payload 用 snake_case。
_USAGE_COMPONENT_KEYS = ("input", "output", "cache_read", "cache_write")


class EmptyCompactionSummary(ValueError):
    """摘要模型返回空白文本：不生成 Compaction Entry，本轮就地失败。"""


@dataclass(frozen=True, slots=True)
class CompactionSettings:
    """两项 token 预算；默认值取 Pi compaction.ts:145-150。"""

    reserve_tokens: int = 16384
    keep_recent_tokens: int = 20000


DEFAULT_COMPACTION_SETTINGS = CompactionSettings()


def calculate_context_tokens(usage: Mapping[str, Any] | None) -> int:
    """usage → 上下文 token 总量（Pi calculateContextTokens:157-159）。

    优先 ``total_tokens``（Pi ``totalTokens``），缺失时累加四个组件键；两者都读不到即 0。
    """
    if not isinstance(usage, Mapping):
        return 0
    total = usage.get("total_tokens")
    if isinstance(total, int) and not isinstance(total, bool) and total > 0:
        return total
    return sum(
        value
        for key in _USAGE_COMPONENT_KEYS
        if isinstance(value := usage.get(key), int) and not isinstance(value, bool)
    )


def estimate_tokens(message: ContextMessage) -> int:
    """单条消息的 token 估算：字符数／4 向上取整（Pi estimateTokens:283-330，保守高估）。"""
    return _text_tokens(message.text)


def estimate_entry_tokens(entry: ConversationEntry) -> int:
    """Entry 的 token 估算：唯一入口，两种 Estimation 都经它汇总。

    compaction Entry 只计 ``payload['summary']``：渲染用的边界标记不属上下文内容
    （Pi estimateTokens:314-317 的 compactionSummary 分支）。其余 Entry 按消息投影估算。
    """
    if entry.entry_type == "compaction":
        return _text_tokens(entry.payload["summary"])
    return sum(estimate_tokens(message) for message in entry_to_context_messages(entry))


def _text_tokens(text: str) -> int:
    return ceil(len(text) / 4)


def estimate_context_tokens(
    context_entries: Sequence[ConversationEntry],
) -> int:
    """估算上下文规模：最后一条带有效 usage 的 Assistant 消息作锚点，其后逐条 Entry 估算。

    锚点缺失时整段 Entry 估算（Pi estimateContextTokens:222-253 与 getAssistantUsage:170-185）。
    """
    projected = [
        (entry, message)
        for entry in context_entries
        for message in entry_to_context_messages(entry)
    ]
    anchor: int | None = None
    for index, (entry, message) in enumerate(projected):
        if message.role != "assistant":
            continue
        if calculate_context_tokens(entry.payload.get("usage")) > 0:
            anchor = index
    if anchor is None:
        return sum(estimate_entry_tokens(entry) for entry, _message in projected)
    usage_tokens = calculate_context_tokens(projected[anchor][0].payload.get("usage"))
    return usage_tokens + sum(
        estimate_entry_tokens(entry) for entry, _message in projected[anchor + 1 :]
    )


def should_compact(
    context_tokens: int, context_window: int, settings: CompactionSettings
) -> bool:
    """阈值规则：``context_tokens > context_window - reserve_tokens``（Pi shouldCompact:258-261）。"""
    return context_tokens > context_window - settings.reserve_tokens


def find_cut_point(
    entries: Sequence[ConversationEntry],
    start_index: int,
    end_index: int,
    keep_recent_tokens: int,
) -> int:
    """返回最近完整轮次的起点下标（Pi findCutPoint:389-462）。

    合法切点是投影为 user 消息的 Entry：完整用户消息与确认 Entry。切点因此总落在轮次开头，
    确认动作不会与它后面的等待／结果语义被切开；assistant 消息、失败内容与 compaction Entry
    不作切点。预算按 :func:`estimate_entry_tokens` 累计：compaction Entry 只计
    ``payload['summary']``，渲染边界标记不计入。范围内没有合法切点时返回 ``start_index``。
    """
    cut_points = [
        index
        for index in range(start_index, end_index)
        if _is_cut_point(entries[index])
    ]
    if not cut_points:
        return start_index
    accumulated_tokens = 0
    cut_index = cut_points[0]
    for index in range(end_index - 1, start_index - 1, -1):
        entry_tokens = estimate_entry_tokens(entries[index])
        if entry_tokens == 0:
            continue
        accumulated_tokens += entry_tokens
        if accumulated_tokens >= keep_recent_tokens:
            cut_index = next(
                (
                    candidate
                    for candidate in cut_points
                    if candidate >= index
                ),
                cut_points[-1],
            )
            break
    while cut_index > start_index:
        previous = entries[cut_index - 1]
        if previous.entry_type == "compaction" or entry_to_context_messages(previous):
            break
        cut_index -= 1
    return cut_index


def _is_cut_point(entry: ConversationEntry) -> bool:
    if entry.entry_type == "compaction":
        return False
    return any(message.role == "user" for message in entry_to_context_messages(entry))


@dataclass(frozen=True, slots=True)
class CompactionPreparation:
    """一次压缩的输入：保留边界、待摘要消息、压缩前规模与此前摘要。"""

    first_kept_entry_id: str
    messages_to_summarize: tuple[ContextMessage, ...]
    tokens_before: int
    previous_summary: str | None


@dataclass(frozen=True, slots=True)
class CompactionResult:
    """一次压缩的输出：摘要文本与写 Entry 所需的边界、压缩前规模。"""

    summary: str
    first_kept_entry_id: str
    tokens_before: int

    def to_payload(self) -> dict[str, Any]:
        """→ ``compaction`` Entry 的 payload；``usage`` 由调用方按需补写。"""
        return {
            "summary": self.summary,
            "first_kept_entry_id": self.first_kept_entry_id,
            "tokens_before": self.tokens_before,
        }


def prepare_compaction(
    path_entries: Sequence[ConversationEntry], settings: CompactionSettings
) -> CompactionPreparation | None:
    """计算压缩输入；无需压缩时返回 ``None``（Pi prepareCompaction:762-858）。

    - 末尾已是 compaction Entry 即 ``None``：同一批消息不重复生成 Compaction Entry。
    - 已有 compaction 时从上一次的保留边界之后继续，摘要带上一次的内容做增量更新。
    """
    if not path_entries:
        return None
    if path_entries[-1].entry_type == "compaction":
        return None
    previous_index = -1
    for index in range(len(path_entries) - 1, -1, -1):
        if path_entries[index].entry_type == "compaction":
            previous_index = index
            break
    previous_summary: str | None = None
    boundary_start = 0
    if previous_index >= 0:
        previous_compaction = path_entries[previous_index]
        previous_summary = previous_compaction.payload["summary"]
        first_kept_index = next(
            (
                index
                for index, entry in enumerate(path_entries)
                if entry.id == previous_compaction.payload["first_kept_entry_id"]
            ),
            -1,
        )
        boundary_start = (
            first_kept_index if first_kept_index >= 0 else previous_index + 1
        )
    cut_index = find_cut_point(
        path_entries, boundary_start, len(path_entries), settings.keep_recent_tokens
    )
    messages_to_summarize = tuple(
        message
        for entry in path_entries[boundary_start:cut_index]
        if entry.entry_type != "compaction"
        for message in entry_to_context_messages(entry)
    )
    if not messages_to_summarize:
        return None
    return CompactionPreparation(
        first_kept_entry_id=path_entries[cut_index].id,
        messages_to_summarize=messages_to_summarize,
        tokens_before=estimate_context_tokens(build_context_entries(path_entries)),
        previous_summary=previous_summary,
    )


def serialize_conversation(messages: Sequence[ContextMessage]) -> str:
    """上下文消息 → 摘要输入纯文本（Pi compaction/utils.ts:109-149 的两态拼装）。"""
    labels = {"user": "[User]", "assistant": "[Assistant]"}
    return "\n\n".join(
        f"{labels[message.role]}: {message.text}" for message in messages if message.text
    )


SUMMARIZATION_SYSTEM_PROMPT = (
    "你是对话历史压缩助手。你的任务是阅读用户与助手之间的对话，"
    "按指定结构输出摘要。\n\n"
    "不要继续这场对话，不要回答对话中的任何问题，只输出结构化摘要。"
)

_SUMMARY_FORMAT = """严格使用以下格式：

## 用户目标
[用户想达成什么？跨多个任务时逐条列出]

## 偏好与约束
- [用户提到的偏好、约束或要求；没有则写“（无）”]

## 已讨论与已接受的方案
- [已经讨论过并且被接受的方案；没有则写“（无）”]

## 已拒绝的内容
- [被明确拒绝或否定的内容；没有则写“（无）”]

## 当前指代对象
- [“这个”“刚才那个”等指代当前对应的对象；没有则写“（无）”]

## 未解决的问题
- [还没有结论的问题；没有则写“（无）”]

## 需要继续遵守的对话约束
- [后续回答必须遵守的约定；没有则写“（无）”]

每节保持简短，保留原文中的关键数值、动作名称与标识。"""

SUMMARY_PROMPT = f"""以上消息是一段需要压缩的对话。请生成一份结构化上下文摘要，供另一个模型继续对话时使用。

{_SUMMARY_FORMAT}"""

UPDATE_SUMMARY_PROMPT = f"""以上消息是需要并入现有摘要的新对话消息，现有摘要见 <previous-summary> 标签。

更新规则：
- 保留现有摘要中的全部信息
- 补充新消息中的目标、偏好、决策与上下文
- 被接受的方案与被拒绝的内容发生变化时就地更新
- 已经解决的问题从“未解决的问题”中移除

{_SUMMARY_FORMAT}"""


def build_summary_prompt(preparation: CompactionPreparation) -> str:
    """构造固定形状的摘要提示：``<conversation>`` 之后按需附上一次摘要与更新规则。"""
    parts = [
        f"<conversation>\n{serialize_conversation(preparation.messages_to_summarize)}\n"
        "</conversation>\n\n"
    ]
    if preparation.previous_summary is not None:
        parts.append(
            f"<previous-summary>\n{preparation.previous_summary}\n</previous-summary>\n\n"
        )
        parts.append(UPDATE_SUMMARY_PROMPT)
    else:
        parts.append(SUMMARY_PROMPT)
    return "".join(parts)


async def compact(
    preparation: CompactionPreparation, model_call: SummaryModelCall
) -> CompactionResult:
    """调用注入的模型调用生成摘要（Pi compact:868-990 的历史摘要分支）。"""
    prompt = build_summary_prompt(preparation)
    summary = (await model_call(SUMMARIZATION_SYSTEM_PROMPT, prompt)).strip()
    if not summary:
        raise EmptyCompactionSummary("摘要模型返回空白文本：本次压缩不落库")
    return CompactionResult(
        summary=summary,
        first_kept_entry_id=preparation.first_kept_entry_id,
        tokens_before=preparation.tokens_before,
    )
