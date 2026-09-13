"""S4-06b：活跃 prompt 估算、旧历史安全压缩与请求投影（stage4.md S4-06；08「容量、估算与溢出」）。

三条硬边界：

1. **估算覆盖整个真实请求输入**：当前事实（系统投影）、工具定义、投影后的历史与本次用户
   消息一起算。token 估算取 ``max(ceil(0.6 × 字符数), 最近一次真实 usage.input_tokens +
   ceil(0.6 × 新增字符))``（08 3.3A）；每次实际请求前重算（`BudgetedModel` 的容量闸），
   压缩决策也基于同一估算。锚点只跟随本进程内真实请求（本片不新增用量持久化），跨 Run 的
   首个请求退化为官方字符上界。
2. **压缩只动旧历史**：达到派生触发点（有效输入上限 × 80%）时只摘要**最老的完整交互**，
   保留最近完整交互到派生 10% 目标、以及当前 Run 的全部内容。交互边界即 Run 边界，因此
   不拆工具调用与结果、不实现 split-turn（08「压缩 A 与失败 B」）。摘要请求自身放不下时
   有界收缩待摘要区间；缩到无可摘要即放弃，同一上下文状态只尝试一次。
3. **提交成功才启用**：摘要经 :class:`~storage.summary_repo.SummaryRepo` 条件提交（Run 仍
   ``running``）后才替换投影；取消先发生则不启用。可恢复的生成失败保留旧上下文继续（放不下
   时由普通容量闸以 ``context_budget_exceeded`` 结束）；存储失败与取消不降级继续。
   摘要只是辅助历史，不冒充最新档案、安全限制、计划或草稿状态——当前事实仍由每次请求的
   唯一系统事实投影给出（``runtime.context``）。

摘要请求与普通请求共用同一个 Run 预算对象：请求计数、暂时故障重试池、取消检查、单次请求
时限、Run 总时限与费用接缝都由 :class:`~runtime.budget.BudgetedModel` 放行，摘要输出上限取
``EffectiveHarness.summary_output_tokens``（默认 6,144）。
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition

from runtime.context import HistoryInteraction
from runtime.run_task import ExecutionFailure
from storage.summary_repo import SummaryRepo

if (
    TYPE_CHECKING
):  # 仅类型检查期：budget 反向 import 本模块（估算与容量判断），运行期不成环
    from config import EffectiveHarness
    from runtime.budget import RunBudget

#: 官方口径 token 估算上界：``ceil(0.6 × 字符数)``（08 3.3A，首版不引入离线 tokenizer）。
TOKEN_ESTIMATE_RATIO = 0.6

#: 摘要投影标题：摘要只是辅助历史，不得被当成当前事实或已发生操作。
SUMMARY_PROJECTION_HEADER = (
    "[上下文摘要] 以下是较早会话历史的整理摘要（辅助历史）：它不是当前事实，不代表任何"
    "操作已发生或已生效；最新档案、安全限制、计划与草稿状态一律以本次系统事实与工具查询"
    "结果为准。早先未完成的请求仍是不完整的，不得当作已回答或已发生。"
)

#: 摘要模型的系统指令：只整理给定内容，不补造事实，排除未完成回答，保留中断语境。
SUMMARY_SYSTEM_INSTRUCTION = """你是会话上下文整理器：把给定的较早会话历史整理成一段摘要，供后续对话参考。

要求：
- 只依据给定内容整理，不得补充、猜测或编造任何事实、操作结果或用户偏好，也不得复述任何凭据或密钥。
- 保留：用户提出过什么请求；已完成回答的结论；失败或已取消的用户请求（标为未完成）；
  已核实到的业务查询结果与已发生的操作；系统中断标注。
- 排除：未完成的回答片段不得写成已发生的事实或已给出的结论；不确定的内容明确标为不确定。
- 摘要只是历史辅助，不是当前档案、安全限制、计划或草稿状态的事实来源。
- 用简洁的书面中文、按时间顺序分段输出。"""

#: 摘要请求的用户指令（与系统指令一起构成摘要请求的提示词余量）。
SUMMARY_USER_INSTRUCTION = (
    "请整理以上较早会话历史，输出更新后的完整摘要（只包含给定内容）。"
)


def character_bound_tokens(characters: int) -> int:
    """字符上界估算：``ceil(0.6 × 字符数)``（08 3.3A）。"""
    return math.ceil(TOKEN_ESTIMATE_RATIO * characters)


def request_json(messages: Sequence[ModelMessage]) -> str:
    """一次请求消息的原生 JSON（计数与「同一上下文状态」判据共用同一序列化）。"""
    return ModelMessagesTypeAdapter.dump_json(list(messages)).decode("utf-8")


def request_characters(
    messages: Sequence[ModelMessage], *, tool_characters: int = 0
) -> int:
    """一次请求实际输入的字符数：框架原生消息 JSON + 已序列化的工具定义。"""
    return len(request_json(messages)) + tool_characters


def tool_definition_characters(definitions: Iterable[ToolDefinition]) -> int:
    """工具定义的字符数（Provider 实际收到的 name／description／parameters 同源）。"""
    return sum(
        len(
            json.dumps(
                {
                    "name": definition.name,
                    "description": definition.description or "",
                    "parameters_json_schema": definition.parameters_json_schema,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        for definition in definitions
    )


def agent_tool_characters(agent: Agent[None, str]) -> int:
    """装配完成的 Agent 每次请求都会带上的工具定义字符数。

    与 ``model_request_parameters.function_tools`` 同源（都是注册时的 ``ToolDefinition``）：
    Agent 自己注册的工具在 ``agent.toolsets`` 里，输出工具（结构化输出）不发给 Provider。
    """
    definitions: list[ToolDefinition] = []
    for toolset in agent.toolsets:
        tools = getattr(toolset, "tools", None)
        if not isinstance(tools, dict):
            continue
        for tool in tools.values():
            definition = getattr(tool, "tool_def", None)
            if isinstance(definition, ToolDefinition):
                definitions.append(definition)
    return tool_definition_characters(definitions)


def ordinary_request_fits(estimate_tokens: int, harness: EffectiveHarness) -> bool:
    """普通请求容量方程（08「容量、估算与溢出」）。

    估算输入 ≤ 有效输入上限，且「估算输入 + 输出预留（单次输出上限 8,192）+ 安全余量
    （max(4,096, 10% × 估算)）」≤ 模型窗口；不成立即结束，不发送、不做 provider 溢出应急重试。
    """
    if estimate_tokens > harness.effective_input_tokens:
        return False
    return (
        estimate_tokens
        + harness.output_reserve_tokens
        + harness.safety_margin_tokens(estimate_tokens)
        <= harness.context_window
    )


def summary_request_fits(estimate_tokens: int, harness: EffectiveHarness) -> bool:
    """摘要请求容量方程：估算输入 + 摘要输出上限 ≤ 有效输入上限（与启动交叉校验同一公式）。"""
    return (
        estimate_tokens + harness.summary_output_tokens
        <= harness.effective_input_tokens
    )


class PromptEstimator:
    """活跃 prompt 估算：字符上界与最近一次真实 usage 锚点取较大值（08 3.3A）。

    锚点 = 最近一次真实成功请求的 ``usage.input_tokens`` 与那次请求的字符数；后续请求按
    ``锚点 tokens + ceil(0.6 × 新增字符)`` 增长。锚点只在当前进程内跟随实际请求——本片不新增
    用量持久化，因此跨 Run 的首个请求用官方字符上界估算（保守上界，不低报）。

    上下文被压缩重写后锚点失效（当前请求不再是那次请求的延伸）：由压缩流程显式 ``clear()``。
    """

    def __init__(self) -> None:
        self._anchor_characters: int | None = None
        self._anchor_input_tokens: int | None = None

    @property
    def anchored(self) -> bool:
        """是否存在可用锚点（证据与测试可见）。"""
        return self._anchor_characters is not None

    def record(self, characters: int, input_tokens: int | None) -> None:
        """记录一次真实请求的输入字符数与 Provider 回报的输入 token。"""
        if input_tokens is None or input_tokens <= 0:
            return  # 未知 usage（0／None）不作为锚点，不低报
        self._anchor_characters = characters
        self._anchor_input_tokens = input_tokens

    def clear(self) -> None:
        """上下文被重写（压缩）后锚点失效。"""
        self._anchor_characters = None
        self._anchor_input_tokens = None

    def estimate(self, characters: int) -> int:
        """当前字符数下的活跃 prompt 估算（tokens）。"""
        bound = character_bound_tokens(characters)
        if self._anchor_characters is None or self._anchor_input_tokens is None:
            return bound
        added = max(0, characters - self._anchor_characters)
        return max(bound, self._anchor_input_tokens + character_bound_tokens(added))


@dataclass(frozen=True, slots=True)
class ActiveSummary:
    """当前有效摘要：覆盖区间、来源行身份与内容（投影与连续压缩的输入）。"""

    content: str
    covered_from_seq: int
    covered_to_seq: int
    source_message_ids: tuple[int, ...]


async def load_active_summary(
    summaries: SummaryRepo, conversation_id: str
) -> ActiveSummary | None:
    """读取当前有效摘要（无则 ``None``）；来源行身份一并取出用于连续压缩的来源并集。"""
    row = await summaries.get_active_summary(conversation_id)
    if row is None:
        return None
    return ActiveSummary(
        content=str(row["content"]),
        covered_from_seq=int(row["covered_from_seq"]),
        covered_to_seq=int(row["covered_to_seq"]),
        source_message_ids=tuple(
            await summaries.list_source_message_ids(str(row["id"]))
        ),
    )


@dataclass(frozen=True, slots=True)
class RequestProjection:
    """一次请求的最终投影：交给 ``agent.run`` 的历史（含摘要与保留尾段）与估算输入。

    ``message_history`` 不含本次用户消息——它由 ``agent.run(user_prompt=...)`` 注入一次
    （07 7.4：用户请求在模型上下文里只出现一次）；估算按「消息 + 工具定义 + 本次用户消息」
    的真实请求形状计算。
    """

    message_history: tuple[ModelMessage, ...]
    estimated_input_tokens: int
    compressed: bool


@dataclass(frozen=True, slots=True)
class SummaryPlan:
    """一次压缩区间计划：待摘要交互、保留交互、覆盖区间与来源并集。"""

    summed: tuple[HistoryInteraction, ...]
    remaining: tuple[HistoryInteraction, ...]
    covered_from_seq: int
    covered_to_seq: int
    source_message_ids: tuple[int, ...]
    estimated_input_tokens: int


def summary_projection_request(content: str) -> ModelRequest:
    """摘要 → 模型可见投影：明确标注为辅助历史（不是当前事实）。"""
    return ModelRequest(
        parts=[UserPromptPart(content=f"{SUMMARY_PROJECTION_HEADER}\n{content}")]
    )


def projected_message_history(
    *,
    facts_request: ModelRequest,
    interactions: Sequence[HistoryInteraction],
    summary_content: str | None,
) -> list[ModelMessage]:
    """当前事实 + （可选）旧摘要 + 投影后的历史交互；用户消息由调用方另注入一次。"""
    messages: list[ModelMessage] = [facts_request]
    if summary_content:
        messages.append(summary_projection_request(summary_content))
    for interaction in interactions:
        messages.extend(interaction.messages)
    return messages


def summary_request_messages(
    history: Sequence[ModelMessage], previous: ActiveSummary | None
) -> list[ModelMessage]:
    """摘要模型请求：系统指令（含已有摘要）+ 待整理历史 + 输出指令。"""
    system = SUMMARY_SYSTEM_INSTRUCTION
    if previous is not None:
        system += f"\n\n【此前摘要】\n{previous.content}"
    return [
        ModelRequest(parts=[SystemPromptPart(content=system)]),
        *history,
        ModelRequest(parts=[UserPromptPart(content=SUMMARY_USER_INSTRUCTION)]),
    ]


def summary_input_estimate(
    summed: Sequence[HistoryInteraction], previous: ActiveSummary | None
) -> int:
    """一次摘要请求的估算输入（tokens）：字符上界（摘要请求不走 usage 锚点）。"""
    history = [message for interaction in summed for message in interaction.messages]
    return character_bound_tokens(
        request_characters(summary_request_messages(history, previous))
    )


def retained_suffix_start(
    interactions: Sequence[HistoryInteraction], retained_tokens: int
) -> int:
    """保留尾段起点：从最新完整交互往回累计到派生 10% 目标，至少保留最新一条交互。

    完整交互边界优先于精确数量（08「压缩 A 与失败 B」）；返回的起点之后即为保留尾段，
    起点之前是「最老的、可摘要的」区间。
    """
    total = 0
    start = len(interactions)
    for index in range(len(interactions) - 1, -1, -1):
        size = character_bound_tokens(request_characters(interactions[index].messages))
        if start < len(interactions) and total + size > retained_tokens:
            break
        total += size
        start = index
    return start


def plan_summary_range(
    *,
    interactions: Sequence[HistoryInteraction],
    previous: ActiveSummary | None,
    harness: EffectiveHarness,
) -> SummaryPlan | None:
    """选择待摘要区间：只摘要最老的完整交互，摘要请求放不下时有界收缩。

    返回 ``None`` 表示放弃压缩（没有可摘要的更老交互，或缩到最老一条仍放不下）：
    保留旧上下文，由普通容量闸决定是否还能继续。
    """
    start = retained_suffix_start(interactions, harness.retained_history_tokens)
    if start <= 0:
        return None  # 全部交互都在保留目标内：没有安全切点
    for cut in range(start, 0, -1):
        summed = tuple(interactions[:cut])
        estimated = summary_input_estimate(summed, previous)
        if not summary_request_fits(estimated, harness):
            continue  # 放不下：收缩待摘要区间（更老的、放得下的部分优先）
        return SummaryPlan(
            summed=summed,
            remaining=tuple(interactions[cut:]),
            covered_from_seq=(
                previous.covered_from_seq
                if previous is not None
                else summed[0].first_seq
            ),
            covered_to_seq=summed[-1].last_seq,
            source_message_ids=_source_union(previous, summed),
            estimated_input_tokens=estimated,
        )
    return None


def _source_union(
    previous: ActiveSummary | None, summed: Sequence[HistoryInteraction]
) -> tuple[int, ...]:
    """来源并集：已有摘要的来源行 + 本次新纳入的交互行（连续摘要仍可追溯到原消息）。"""
    ids = set(previous.source_message_ids) if previous is not None else set()
    for interaction in summed:
        ids.update(interaction.source_message_ids)
    return tuple(sorted(ids))


class SummaryModel(Protocol):
    """摘要生成模型接缝：与普通请求共用 Run 预算的模型包装。"""

    async def request_summary(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None = None,
    ) -> ModelResponse: ...


class ContextCompressor:
    """一次 Run 的旧历史压缩编排（stage4.md S4-06）。

    同一上下文状态最多尝试一次压缩：放弃（无安全切点／缩到无可摘要／可恢复生成失败）后，
    相同投影再次进入不重复尝试，等上下文变化再试。
    """

    def __init__(
        self,
        *,
        summaries: SummaryRepo,
        harness: EffectiveHarness,
        budget: RunBudget,
        model: SummaryModel,
        estimator: PromptEstimator,
        conversation_id: str,
        run_id: str,
        tool_characters: int = 0,
        on_compression: Callable[[str], None] | None = None,
    ) -> None:
        self._summaries = summaries
        self._harness = harness
        self._budget = budget
        self._model = model
        self._estimator = estimator
        self._conversation_id = conversation_id
        self._run_id = run_id
        self._tool_characters = tool_characters
        self._on_compression = on_compression
        self._attempted_signature: str | None = None

    def _notify_compression(self, state: str) -> None:
        """压缩状态通知（S4-07 传输观察者）：只告知展示层，不改变压缩决策与提交。"""
        if self._on_compression is not None:
            self._on_compression(state)

    @property
    def attempted(self) -> bool:
        """本 Run 是否已对某个上下文状态尝试过压缩（证据与测试可见）。"""
        return self._attempted_signature is not None

    async def project(
        self,
        *,
        facts_request: ModelRequest,
        interactions: Sequence[HistoryInteraction],
        summary: ActiveSummary | None,
        user_text: str,
    ) -> RequestProjection:
        """构造本次请求的投影；达到触发点则先尝试压缩旧历史。

        ``message_history`` 返回给 ``agent.run``（用户消息另行注入一次）；估算按
        「消息 + 工具定义 + 本次用户消息」的真实请求形状计算，并在每次实际请求前由
        :class:`~runtime.budget.BudgetedModel` 的容量闸重算。
        """
        interactions = tuple(interactions)
        history = projected_message_history(
            facts_request=facts_request,
            interactions=interactions,
            summary_content=None if summary is None else summary.content,
        )
        estimate = self._estimate(history, user_text)
        if estimate < self._harness.compression_trigger_tokens:
            return RequestProjection(tuple(history), estimate, compressed=False)
        signature = _state_signature(facts_request, interactions, summary, user_text)
        if signature == self._attempted_signature:
            # 同一上下文状态不重复尝试（放弃过的不再原样重试）。
            return RequestProjection(tuple(history), estimate, compressed=False)
        self._attempted_signature = signature
        compressed = await self._compress(
            facts_request=facts_request,
            interactions=interactions,
            summary=summary,
            user_text=user_text,
        )
        if compressed is None:
            return RequestProjection(tuple(history), estimate, compressed=False)
        return compressed

    async def _compress(
        self,
        *,
        facts_request: ModelRequest,
        interactions: tuple[HistoryInteraction, ...],
        summary: ActiveSummary | None,
        user_text: str,
    ) -> RequestProjection | None:
        """尝试一次压缩；返回新投影，或 ``None`` 表示保留旧上下文。"""
        plan = plan_summary_range(
            interactions=interactions, previous=summary, harness=self._harness
        )
        if plan is None:
            return None
        # 压缩状态事件（S4-07）：只在真要开始整理时告知展示层，结束必发（含放弃后保留旧上下文）。
        self._notify_compression("started")
        try:
            try:
                content = await self._generate(plan, summary)
                if content is None:
                    return None  # 空摘要不算成功：保留旧上下文
                # 取消先发生则不提交（存储层还会在事务内再次条件校验 Run 状态）。
                self._budget.require_running()
                await self._summaries.commit_summary(
                    run_id=self._run_id,
                    content=content,
                    covered_from_seq=plan.covered_from_seq,
                    covered_to_seq=plan.covered_to_seq,
                    source_message_ids=list(plan.source_message_ids),
                )
            except ExecutionFailure:
                # 可恢复的生成／容量失败：保留旧上下文继续；放不下时由普通容量闸结束。
                return None
            # 提交成功才启用：新摘要 + 保留尾段的投影，锚点随上下文重写失效。
            self._estimator.clear()
            history = projected_message_history(
                facts_request=facts_request,
                interactions=plan.remaining,
                summary_content=content,
            )
            return RequestProjection(
                tuple(history), self._estimate(history, user_text), compressed=True
            )
        finally:
            self._notify_compression("finished")

    async def _generate(
        self, plan: SummaryPlan, previous: ActiveSummary | None
    ) -> str | None:
        """生成摘要正文；模型请求走 Run 预算（计数／重试池／取消／墙钟／费用接缝）。"""
        history = [
            message for interaction in plan.summed for message in interaction.messages
        ]
        response = await self._model.request_summary(
            summary_request_messages(history, previous),
            ModelSettings(max_tokens=self._harness.summary_output_tokens),
        )
        text = "".join(
            part.content for part in response.parts if isinstance(part, TextPart)
        ).strip()
        return text or None

    def _estimate(self, history: Sequence[ModelMessage], user_text: str) -> int:
        """真实请求形状（消息 + 工具定义 + 本次用户消息）的估算 tokens。"""
        characters = request_characters(
            [*history, _user_request(user_text)], tool_characters=self._tool_characters
        )
        return self._estimator.estimate(characters)


def _user_request(user_text: str) -> ModelRequest:
    """``agent.run(user_prompt=...)`` 实际追加的请求消息（估算与状态判据按同一形状）。"""
    return ModelRequest(parts=[UserPromptPart(content=user_text)])


def _state_signature(
    facts_request: ModelRequest,
    interactions: Sequence[HistoryInteraction],
    summary: ActiveSummary | None,
    user_text: str,
) -> str:
    """「同一上下文状态」判据：按内容（不含消息时间戳等元数据）比较。"""
    return json.dumps(
        {
            "facts": [
                str(getattr(part, "content", part)) for part in facts_request.parts
            ],
            "interactions": [
                (interaction.run_id, interaction.first_seq, interaction.last_seq)
                for interaction in interactions
            ],
            "summary": None
            if summary is None
            else (summary.covered_to_seq, summary.content),
            "user": user_text,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
