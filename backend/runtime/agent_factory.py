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

from collections.abc import Callable, Coroutine, Sequence
from datetime import date
from typing import TYPE_CHECKING, Any

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model

from runtime.budget import CORRECTION_POOL, BudgetedModel, RunBudget
from runtime.compression import (
    ContextCompressor,
    PromptEstimator,
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
)
from runtime.run_task import ActiveExecution, FrameworkMessages
from runtime.tools import BusinessTools, ToolIdentity
from storage.db import Database
from storage.run_repo import RunRepo
from storage.summary_repo import SummaryRepo

if (
    TYPE_CHECKING
):  # 仅类型检查期：runtime 不在运行期反向依赖配置模块（config 向下读目录）
    from config import EffectiveHarness

#: Agent 身份：只在运行时装配内使用（不是模型 id，也不进产品事件）。
AGENT_NAME = "fit-agent"


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
    """把一次 Run 的上下文构造与执行封成执行驱动的 ``work``（S4-03 接缝）。

    ``business_date`` 由调用方按固定业务时区给出（07 7.3）；本模块不取系统时钟、不读客户端时间。
    ``harness`` 是调用方在 Run 开始前冻结的**有效**配置（08 8.5）：本函数在 Run 开始时新建一个
    :class:`~runtime.budget.RunBudget`，本 Run 的限制不再受运行中配置变更影响。

    取消（08 8.3）：``work`` 只读驱动写入的取消标记——预算闸在任何新模型／工具／重试／纠错／
    退避 effect 前检查它，取消后不再启动新读取／写入；底层调用被中断时向上抛 ``CancelledError``，
    由驱动丢弃迟到结果（不写完成消息、不补终态）。
    """

    async def work(active: ActiveExecution) -> FrameworkMessages:
        # 执行预算与墙钟（S4-05b）：Run 起点从这里计时；模型与工具两条路共用同一实例。
        budget = RunBudget(harness, cancelled=lambda: active.cancel_requested)
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
        )
        projection = await compressor.project(
            facts_request=current_facts_request(facts),
            interactions=interactions,
            summary=summary,
            user_text=user_text,
        )
        result = await agent.run(
            user_prompt=user_text, message_history=list(projection.message_history)
        )
        return framework_messages(result.new_messages())

    return work


def _with_current_facts(
    facts: BusinessFacts, history: Sequence[ModelMessage]
) -> list[ModelMessage]:
    """当前事实投影 + 已保存历史：事实每 Run 只注入一次，历史里不再夹带旧系统事实。"""
    return [current_facts_request(facts), *history]
