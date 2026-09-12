"""PydanticAI Agent 装配与一次 Run 的执行入口（stage4.md S4-04；08 8.5/8.6 的参数与预算归 S4-05）。

装配只做三件事：注册 :class:`~runtime.tools.BusinessTools` 的业务工具、固定「不做框架级隐式
重试」、把执行封成 S4-03 执行驱动要的 ``work``（``ActiveExecution`` → 新框架消息）。

一次 Run 的执行顺序（每 Run 重做，不用上一次的投影）：

1. 读本 Run 的用户请求应用事实（与 pending Run 同事务保存的那一条）；
2. 从应用层重读当前业务事实（档案／限制／计划指导），作为**唯一**一条系统事实部件注入；
3. 原生反序列化已保存历史（不含本次请求、不执行工具）；
4. 交给模型与业务工具执行，只返回**本次新产生**的框架消息；历史不回写、不重复追加。

``retries={"tools": 0, "output": 0}``：本片不实现纠错预算，框架级隐式重试会绕过 Run 级预算
（S4-01 已证框架 per-tool 预算是 per-tool 而非 Run 级）。S4-05 按已拍参数（重试池 1 次、
纠错池 2 次）从适配层驱动纠正尝试，不在本片预置。
"""

from collections.abc import Callable, Coroutine, Sequence
from datetime import date
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model

from runtime.context import (
    BusinessFacts,
    current_facts_request,
    framework_messages,
    load_conversation_history,
    message_red_flags,
    read_business_facts,
)
from runtime.run_task import ActiveExecution, FrameworkMessages
from runtime.tools import BusinessTools, ToolIdentity
from storage.db import Database
from storage.run_repo import RunRepo

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
    """装配一次 Run 的 Agent：业务工具面 + 无框架级隐式重试。

    工具来源只有 :class:`~runtime.tools.BusinessTools`：只读查询与四类 Pending 草稿创建。
    没有确认／丢弃／作废／直写正式事实的工具，也不注册任何草稿之外的写入能力。
    """
    agent: Agent[None, str] = Agent(
        model,
        name=AGENT_NAME,
        retries={"tools": 0, "output": 0},
    )
    for function in agent_tool_functions(tools):
        agent.tool_plain(function)
    return agent


def build_run_work(
    *,
    db: Database,
    repo: RunRepo,
    model: Model,
    conversation_id: str,
    run_id: str,
    business_date: date,
) -> Callable[[ActiveExecution], Coroutine[Any, Any, FrameworkMessages]]:
    """把一次 Run 的上下文构造与执行封成执行驱动的 ``work``（S4-03 接缝）。

    ``business_date`` 由调用方按固定业务时区给出（07 7.3）；本模块不取系统时钟、不读客户端时间。

    取消（08 8.3）：``work`` 只读驱动写入的取消标记——取消后工具不再启动新的读取／写入，
    底层调用被中断时向上抛 ``CancelledError``，由驱动丢弃迟到结果（不写完成消息、不补终态）。
    """

    async def work(active: ActiveExecution) -> FrameworkMessages:
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
        history = await load_conversation_history(
            repo, conversation_id=conversation_id, current_run_id=run_id
        )
        tools = BusinessTools(
            db,
            ToolIdentity(
                conversation_id=conversation_id,
                run_id=run_id,
                business_date=business_date,
                cancel_requested=lambda: active.cancel_requested,
                message_red_flags=message_hits,
            ),
        )
        agent = build_agent(model, tools)
        result = await agent.run(
            user_prompt=user_text,
            message_history=_with_current_facts(facts, history),
        )
        return framework_messages(result.new_messages())

    return work


def _with_current_facts(
    facts: BusinessFacts, history: Sequence[ModelMessage]
) -> list[ModelMessage]:
    """当前事实投影 + 已保存历史：事实每 Run 只注入一次，历史里不再夹带旧系统事实。"""
    return [current_facts_request(facts), *history]
