"""PydanticAI Agent 装配与一次 Run 的执行入口（stage4.md S4-04；08 8.5/8.6 的参数与预算归 S4-05）。

装配只做四件事：注册 :class:`~runtime.tools.BusinessTools` 的业务工具、把框架 ``retries`` 固定在
纠错池上限（框架不得自行决定额外尝试）、用 :class:`~runtime.budget.BudgetedModel` 包住模型
（Run 级请求计数、墙钟与分类的唯一权威）、把执行封成 S4-03 执行驱动要的 ``work``
（``ActiveExecution`` → 新框架消息）。

一次 Run 的执行顺序（每 Run 重做，不用上一次的投影）：

1. 读本 Run 的用户请求应用事实（与 pending Run 同事务保存的那一条）；
2. 从应用层重读当前业务事实（档案／限制／计划指导），作为**唯一**一条系统事实部件注入；
3. 原生反序列化已保存历史（不含本次请求、不执行工具）；
4. 交给模型与业务工具执行，只返回**本次新产生**的框架消息；历史不回写、不重复追加。

预算（S4-05b）：每 Run 用调用方传来的**已冻结**有效配置（08 8.5；``freeze_effective_harness``
在 Run 开始前完成，运行中改文件不影响本 Run）新建一个 :class:`~runtime.budget.RunBudget`，
模型与工具两条路都走它：取消、Run 墙钟到期、请求／工具／纠错池用尽都在启动新 effect 前拦下。
``retries={"tools": CORRECTION_POOL, "output": CORRECTION_POOL}``：框架只决定「需要纠错」，
真正放行与计数在适配层（每次带 ``RetryPromptPart`` 的追问扣共享纠错池，池尽即终态失败）。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from pydantic_ai import Agent, AgentRunResultEvent
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from runtime.budget import (
    CORRECTION_POOL,
    BudgetedModel,
    RunBudget,
    classify_model_failure,
)
from runtime.compression import (
    ContextCompressor,
    PromptEstimator,
    RequestProjection,
    agent_tool_characters,
    load_active_summary,
)
from runtime.context import (
    BusinessFacts,
    current_facts_request,
    framework_messages,
    load_conversation_interactions,
    message_red_flags,
    read_business_facts,
    recalc_intent_text,
    review_basis_request,
    review_intent_text,
)
from runtime.error_codes import MODEL_REQUEST_FAILED
from runtime.events import RunEventStream, visible_text_delta
from runtime.run_task import ActiveExecution, ExecutionFailure, FrameworkMessages
from runtime.tools import BusinessTools, ToolIdentity
from storage.db import Database
from storage.errors import RunStateConflict
from storage.run_repo import RunRepo

if (
    TYPE_CHECKING
):  # 仅类型检查期：runtime 不在运行期反向依赖配置模块（config 向下读目录）
    from config import EffectiveHarness
from app.draft_repo import Draft
from app.review_store import ReviewStore
from domain.stats.rules import InvalidReviewContent
from domain.stats.service import StatsService
from storage.summary_repo import SummaryRepo

#: Agent 身份：只在运行时装配内使用（不是模型 id，也不进产品事件）。
AGENT_NAME = "fit-agent"

#: 可见回答分批落盘的批次字符数（实现细节，07 7.4）：达到该长度才写一行，
#: 不逐 Token／逐传输块写库；最近未落盘的片段允许丢失（取消／失败时按未完成展示）。
PARTIAL_BATCH_CHARACTERS = 200


def agent_tool_functions(tools: BusinessTools) -> tuple[Callable[..., Any], ...]:
    """本 Agent 暴露的工具全集：注册与能力旁路扫描共用同一份清单。

    四类只读查询 + 四类 Pending 草稿创建；确认／丢弃／作废与正式事实写入不在其中。
    """
    return (
        tools.read_plan_guidance,
        tools.read_training_records,
        tools.read_week_completion,
        tools.read_personal_record,
        tools.propose_profile_draft,
        tools.propose_plan_draft,
        tools.propose_record_draft,
        tools.propose_arrangement_draft,
    )


def build_agent(model: Model, tools: BusinessTools) -> Agent[None, str]:
    """装配一次 Run 的 Agent：业务工具面 + 框架纠错预算固定在纠错池上限。

    工具来源只有 :class:`~runtime.tools.BusinessTools`：只读查询与四类 Pending 草稿创建。
    没有确认／丢弃／作废／直写正式事实的工具，也不注册任何草稿之外的写入能力。

    ``retries``：框架只决定「需要纠错」（工具参数校验失败时回注 ``RetryPromptPart``），
    上限取共享纠错池（2），与 Run 级计数一致，因此框架自己的预算不可能凭空多出一次
    适配层看不见的尝试；每次带纠错反馈的追问仍由 :class:`~runtime.budget.BudgetedModel`
    扣减共享池，池尽即终态失败（08 8.6）。
    """
    agent: Agent[None, str] = Agent(
        model,
        name=AGENT_NAME,
        retries={"tools": CORRECTION_POOL, "output": CORRECTION_POOL},
    )
    for function in agent_tool_functions(tools):
        agent.tool_plain(function)
    return agent


def _answer_settings(harness: EffectiveHarness) -> ModelSettings:
    """可见回答请求的模型参数：输出上限取已冻结的 Harness 值（08「容量、估算与溢出」）。

    思考 token 计入输出，因此这是**可见输出**上限而不是总输出上界（08「思考模式默认」）。
    """
    return ModelSettings(max_tokens=harness.max_output_tokens)


@dataclass(slots=True)
class _PreparedRun:
    """一次 Run 的上下文投影与 Agent（流式与非流式路径共用同一构造）。"""

    user_text: str
    agent: Agent[None, str]
    projection: RequestProjection


async def _prepare_run(
    active: ActiveExecution,
    *,
    db: Database,
    repo: RunRepo,
    model: Model,
    harness: EffectiveHarness,
    conversation_id: str,
    run_id: str,
    business_date: date,
    user_text: str | None = None,
    parent_draft_id: str | None = None,
    parent_kind: str | None = None,
    on_draft_persisted: Callable[[Any], None] | None = None,
    on_compression: Callable[[str], None] | None = None,
) -> _PreparedRun:
    """一次 Run 的上下文构造：事实重读 → 载历史 → 压缩投影 → Agent 装配。

    两条执行路径（非流式 ``agent.run`` 与生产流式 ``agent.run_stream_events``）都用本函数，
    因此事实注入、历史投影、工具面与预算逐字相同，不会出现两套上下文语义。

    ``user_text`` 缺省时读该 Run 的用户请求应用事实（对话路径）；重算 Run 没有用户消息，
    由调用方传入旧草稿意图文本（``runtime.context.recalc_intent_text``），并给出
    ``parent_draft_id``／``parent_kind`` 供工具层强制同类子草稿。
    """
    # 执行预算与墙钟（S4-05b）：Run 起点从这里计时；模型与工具两条路共用同一实例。
    budget = RunBudget(harness, cancelled=lambda: active.cancel_requested)
    if user_text is None:
        user_text = await repo.get_user_request_text(run_id)
    if user_text is None:
        # 用户请求与 pending Run 同事务写入；缺行即库内状态损坏，显式失败不猜内容。
        raise RuntimeError(f"Run 缺少用户请求应用事实，无法构造上下文: {run_id}")
    # C 层文本兜底（2026-09-12 拍板）：只扫当前 Run 最新用户消息；命中即本 Run 的
    # 安全复核强制不可用（注入事实与 read_plan_guidance 两条路同一合并）。
    message_hits = message_red_flags(user_text)
    facts = await read_business_facts(
        db,
        conversation_id=conversation_id,
        business_date=business_date,
        message_red_flags=message_hits,
    )
    tools = BusinessTools(
        db,
        ToolIdentity(
            conversation_id=conversation_id,
            run_id=run_id,
            business_date=business_date,
            cancel_requested=lambda: active.cancel_requested,
            message_red_flags=message_hits,
            budget=budget,
            on_draft_persisted=on_draft_persisted,
            parent_draft_id=parent_draft_id,
            parent_kind=parent_kind,
        ),
    )
    # 压缩与投影（S4-06b）：估算整套请求输入；触发点先摘要最老的完整交互，
    # 摘要经条件提交成功后才替换投影（失败保留旧上下文，放不下由容量闸结束）。
    estimator = PromptEstimator()
    budgeted = BudgetedModel(model, budget, estimator=estimator)
    summaries = SummaryRepo(db)
    summary = await load_active_summary(summaries, conversation_id)
    # 只取有效摘要覆盖终点之后的消息：摘要 + 尾段才是完整上下文（不重复投影已摘要历史）。
    interactions = await load_conversation_interactions(
        repo,
        conversation_id=conversation_id,
        current_run_id=run_id,
        after_seq=0 if summary is None else summary.covered_to_seq,
    )
    agent = build_agent(budgeted, tools)
    compressor = ContextCompressor(
        summaries=summaries,
        harness=harness,
        budget=budget,
        model=budgeted,
        estimator=estimator,
        conversation_id=conversation_id,
        run_id=run_id,
        tool_characters=agent_tool_characters(agent),
        on_compression=on_compression,
    )
    projection = await compressor.project(
        facts_request=current_facts_request(facts),
        interactions=interactions,
        summary=summary,
        user_text=user_text,
    )
    return _PreparedRun(user_text=user_text, agent=agent, projection=projection)


def build_run_work(
    *,
    db: Database,
    repo: RunRepo,
    model: Model,
    harness: EffectiveHarness,
    conversation_id: str,
    run_id: str,
    business_date: date,
) -> Callable[[ActiveExecution], Coroutine[Any, Any, FrameworkMessages]]:
    """把一次 Run 的上下文构造与**非流式**执行封成执行驱动的 ``work``（S4-03 接缝）。

    ``business_date`` 由调用方按固定业务时区给出（07 7.3）；本模块不取系统时钟、不读客户端时间。
    ``harness`` 是调用方在 Run 开始前冻结的**有效**配置（08 8.5）：本函数在 Run 开始时新建一个
    :class:`~runtime.budget.RunBudget`，本 Run 的限制不再受运行中配置变更影响。

    本入口不产生产品事件（没有增量输出）：生产对话路径走
    :func:`build_streaming_run_work`（回答块要实时进事件、部分回答要分批落盘）。两者共用
    :func:`_prepare_run`，事实／历史／工具／预算不因入口不同而不同。

    取消（08 8.3）：``work`` 只读驱动写入的取消标记——预算闸在任何新模型／工具／重试／纠错／
    退避 effect 前检查它，取消后不再启动新读取／写入；底层调用被中断时向上抛 ``CancelledError``，
    由驱动丢弃迟到结果（不写完成消息、不补终态）。
    """

    async def work(active: ActiveExecution) -> FrameworkMessages:
        prepared = await _prepare_run(
            active,
            db=db,
            repo=repo,
            model=model,
            harness=harness,
            conversation_id=conversation_id,
            run_id=run_id,
            business_date=business_date,
        )
        result = await prepared.agent.run(
            user_prompt=prepared.user_text,
            message_history=list(prepared.projection.message_history),
            model_settings=_answer_settings(harness),
        )
        return framework_messages(result.new_messages())

    return work


def build_streaming_run_work(
    *,
    db: Database,
    repo: RunRepo,
    model: Model,
    harness: EffectiveHarness,
    conversation_id: str,
    run_id: str,
    business_date: date,
    events: RunEventStream,
    partial_batch_characters: int = PARTIAL_BATCH_CHARACTERS,
    user_text: str | None = None,
    parent_draft_id: str | None = None,
    parent_kind: str | None = None,
) -> Callable[[ActiveExecution], Coroutine[Any, Any, FrameworkMessages]]:
    """生产流式执行入口（S4-07）：可见回答增量进产品事件，并按批次落盘部分回答。

    - 只把可见文本（``runtime.events.visible_text_delta``）映射成 ``answer`` 事件：隐藏推理、
      工具调用与结果、Token／Cache 与 provider 细节不进事件（08 8.7/8.8）。
    - 部分回答按 ``partial_batch_characters`` 分批写库（07 7.4：不逐 Token／逐传输块写库）；
      失败或中断时已落盘批次保留为**未完成**内容，最近未落盘的片段允许丢失。
    - **不重放**（2026-09-12 拍板）：流一旦交给消费方，即使尚无可见文本块也按分类结果结束，
      本层不建更高层重试；未分类异常不发明原因码（S4-03 语义）。
    - 完成仍由执行驱动在 ``complete_run`` 内把完整框架消息与 ``completed`` 同事务提交；
      本层不写成功消息、不写终态。
    """

    async def work(active: ActiveExecution) -> FrameworkMessages:
        batcher = _PartialAnswerBatcher(
            repo=repo, run_id=run_id, batch_characters=partial_batch_characters
        )
        try:
            prepared = await _prepare_run(
                active,
                db=db,
                repo=repo,
                model=model,
                harness=harness,
                conversation_id=conversation_id,
                run_id=run_id,
                business_date=business_date,
                user_text=user_text,
                parent_draft_id=parent_draft_id,
                parent_kind=parent_kind,
                on_draft_persisted=lambda draft: events.publish_draft(
                    run_id,
                    draft_id=draft.id,
                    kind=draft.kind,
                    revision=draft.revision,
                    status=draft.status,
                ),
                on_compression=lambda state: events.publish_compression(run_id, state),
            )
            async with prepared.agent.run_stream_events(
                user_prompt=prepared.user_text,
                message_history=list(prepared.projection.message_history),
                model_settings=_answer_settings(harness),
            ) as stream:
                async for event in stream:
                    if isinstance(event, AgentRunResultEvent):
                        await batcher.flush()
                        return framework_messages(event.result.new_messages())
                    chunk = visible_text_delta(event)
                    if not chunk:
                        continue
                    events.publish_answer(run_id, chunk)
                    await batcher.add(chunk)
            raise RuntimeError("流式执行结束但没有最终结果事件（框架契约变化）")
        except asyncio.CancelledError:
            raise  # 取消：不补存、不映射错误码（08 8.3；终态由执行驱动裁决）
        except BaseException as exc:  # noqa: BLE001 - 分类后按已拍边界结束，不重放
            await batcher.flush()  # 已产出的可见文本按未完成保留（07 7.4）
            failure = _stream_failure(exc, answered=batcher.answered)
            if failure is None:
                raise
            raise failure from exc

    return work


def build_recalc_run_work(
    *,
    db: Database,
    repo: RunRepo,
    model: Model,
    harness: EffectiveHarness,
    conversation_id: str,
    run_id: str,
    business_date: date,
    events: RunEventStream,
    parent: Draft,
) -> Callable[[ActiveExecution], Coroutine[Any, Any, FrameworkMessages]]:
    """重新生成草稿的执行入口（S4-08 Q1=C）：同一流式链路，输入换成旧草稿意图。

    不建第二套执行框架：预算、取消、SSE、部分回答、草稿就绪通知全部复用
    :func:`build_streaming_run_work`；只把 user prompt 换成按库内旧草稿生成的意图文本，并把
    旧草稿身份交给工具层强制「同类子草稿 + ``parent_draft_id``」。子草稿仍必须经既有
    propose_* 工具链与领域／安全校验落盘，重算 Run 不直接写草稿表。
    """
    return build_streaming_run_work(
        db=db,
        repo=repo,
        model=model,
        harness=harness,
        conversation_id=conversation_id,
        run_id=run_id,
        business_date=business_date,
        events=events,
        user_text=recalc_intent_text(parent),
        parent_draft_id=parent.id,
        parent_kind=parent.kind,
    )


#: 正文中的数字串（最大连续数字）：每个都必须逐字出现在冻结事实里（06 6.4）。
_BASIS_NUMBER = re.compile(r"\d+")


def require_basis_only_numbers(markdown: str, basis_text: str) -> None:
    """模型正文不得改写或编造数值：出现的每个数字串必须逐字来自冻结事实。

    确定性、保守：只做数字子串比对，不理解语义、不解析单位，也不把数字换算成另一种写法；
    事实里没有的数字串（例如自创的「已完成 999 次」）一律拒绝。已知上限：模型可以写与事实
    无关但不含数字的句子；本检查只守住「数值不得改写或编造」这条已拍边界（06 6.4）。
    """
    for number in _BASIS_NUMBER.findall(markdown):
        if number not in basis_text:
            # 只给出已冻结的终态原因码（细节不外发；新增语义码须 owner 拍板）。
            raise ExecutionFailure(MODEL_REQUEST_FAILED)


def build_review_run_work(
    *,
    db: Database,
    model: Model,
    harness: EffectiveHarness,
    business_date: date,
) -> Callable[[ActiveExecution], Coroutine[Any, Any, FrameworkMessages]]:
    """显式复盘生成 Run 的执行入口（S4-08 Q3=B）：确定性冻结 → 只写解释 → 事务内保存。

    与对话／重算 Run 同一驱动、预算、取消与 SSE 查询边界（调用方传同一 ``ExecutionDriver``）：
    本函数不建第二套状态机，也不注册任何业务工具——复盘只输出 Markdown 解释正文。

    - **先冻结**：Run 起点在同一事务内现算统计快照与精确来源修订 id（06 6.4），模型只收到
      这份事实；无定时／按周自动触发，只有本 Run 被显式请求才会执行。
    - **数值不可改写**：保存前逐字校验正文数字串均来自冻结事实；违反即终态
      ``model_request_failed``（不新增错误码），不保存、不落假正文。
    - **来源竞态回滚**：``ReviewStore.save_review`` 在同一事务内校验全部来源修订仍是当前
      修订；竞态（并发更正／作废）任一步失败整体回滚，不产生复盘行，Run 以失败结束。
    - **只追加**：重复显式生成是新增一行，不覆盖旧正文（06 6.4）。
    """

    async def work(active: ActiveExecution) -> FrameworkMessages:
        budget = RunBudget(harness, cancelled=lambda: active.cancel_requested)
        basis = await StatsService(db).review_basis(business_date=business_date)
        basis_request = review_basis_request(basis, business_date=business_date)
        agent: Agent[None, str] = Agent(BudgetedModel(model, budget), name=AGENT_NAME)
        result = await agent.run(
            user_prompt=review_intent_text(),
            message_history=[basis_request],
            model_settings=_answer_settings(harness),
        )
        markdown = result.output
        if not isinstance(markdown, str) or not markdown.strip():
            raise ExecutionFailure(MODEL_REQUEST_FAILED)
        require_basis_only_numbers(markdown, basis_request.parts[0].content)  # type: ignore[union-attr]
        try:
            await ReviewStore(db).save_review(
                body_markdown=markdown,
                basis=basis.snapshot,
                source_revision_ids=basis.source_revision_ids,
            )
        except InvalidReviewContent as exc:
            # 来源修订已不是当前修订（并发更正／作废）：整体回滚、不留下复盘行。
            raise ExecutionFailure(MODEL_REQUEST_FAILED) from exc
        return framework_messages(result.new_messages())

    return work


def _stream_failure(exc: BaseException, *, answered: bool) -> ExecutionFailure | None:
    """流中失败的分类：复用 S4-05 的已拍依据，已分类的失败原样保留。

    ``answered``：本次是否已产出可见文本。流已经交给消费方，因此**不重放**（2026-09-12 拍板）：
    ``decision.retryable`` 在此不适用，只取其错误码。无法分类的异常返回 ``None``：
    不发明原因码（S4-03 语义，Run 停在 ``running`` 由启动恢复收口）。
    """
    if isinstance(exc, ExecutionFailure):
        return exc
    decision = classify_model_failure(exc, output_emitted=answered)
    if decision is None:
        return None
    return ExecutionFailure(decision.error_code)


class _PartialAnswerBatcher:
    """可见回答分批落盘（07 7.4）：达到批次字符数才写一行，不逐传输块写库。

    ``flush`` 在 Run 已终态（取消条件写已提交）时静默放弃：取消后不补存晚到内容。
    """

    def __init__(self, *, repo: RunRepo, run_id: str, batch_characters: int) -> None:
        self._repo = repo
        self._run_id = run_id
        self._batch_characters = batch_characters
        self._pending: list[str] = []
        self._pending_characters = 0
        self._answered = False

    @property
    def answered(self) -> bool:
        """本 Run 是否已产出可见文本（流中失败分类的「是否已输出」依据）。"""
        return self._answered

    async def add(self, chunk: str) -> None:
        self._answered = True
        self._pending.append(chunk)
        self._pending_characters += len(chunk)
        if self._pending_characters >= self._batch_characters:
            await self.flush()

    async def flush(self) -> None:
        if not self._pending:
            return
        text = "".join(self._pending)
        self._pending.clear()
        self._pending_characters = 0
        try:
            await self._repo.save_partial_answer(self._run_id, text)
        except RunStateConflict:
            return  # 已终态（取消／失败已提交）：不再补存晚到内容（07 7.4）


def _with_current_facts(
    facts: BusinessFacts, history: Sequence[ModelMessage]
) -> list[ModelMessage]:
    """当前事实投影 + 已保存历史：事实每 Run 只注入一次，历史里不再夹带旧系统事实。"""
    return [current_facts_request(facts), *history]
