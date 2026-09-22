import asyncio
from collections.abc import Mapping
from functools import partial
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot

from app.application.agent.contracts import (
    ADJUST_PLAN_INTENT,
    ConfirmationConflict,
    GeneratePlanDeps,
    GeneratePlanRun,
    WorkflowState,
    thread_config,
)
from app.application.agent.plan_nodes import (
    EvaluatorAgentNode,
    PlanDeterministicNodes,
    PlannerAgentNode,
    PlanWriteNodes,
    load_skill,
)
from app.application.services.plans_service import PlanNotFound
from app.domain.plans.schema import DeterministicResult, EvaluationResult, Plan

WAITING_CONFIRMATION_NODE = "wait_for_confirmation"

ConfirmationAction = Literal["confirm", "reject"]

NODE_NAMES: tuple[str, ...] = (
    "require_active_plan",
    "validate_required_profile",
    "load_skill",
    "planner_agent",
    "validate_plan",
    "evaluator_agent",
    "revise_once",
    "persist_draft",
    WAITING_CONFIRMATION_NODE,
    "activate_plan",
    "archive_draft",
    "discard_failed_candidate",
)


def _route_by_intent(state: WorkflowState) -> Literal["adjust", "generate"]:
    """子图入口分流：调整计划先校验 active 前提，生成计划直接进画像前提。"""
    return "adjust" if state.get("intent") == ADJUST_PLAN_INTENT else "generate"


def _bounded_route(
    passed: bool, revision_count: int | None
) -> Literal["pass", "revise", "discard"]:
    """有限修订分流的唯一实现：通过即放行；首版失败只修订一次，修订后仍失败即丢弃。"""
    if passed:
        return "pass"
    return "discard" if revision_count else "revise"


def _route_after_validate_plan(
    state: WorkflowState,
) -> Literal["pass", "revise", "discard"]:
    """结构校验后分流：与评估共用同一条有限修订路径。"""
    deterministic: DeterministicResult | None = state.get("deterministic_result")
    if deterministic is None:
        raise ValueError("结构校验分流缺少本轮确定性结果")
    return _bounded_route(deterministic.passed, state.get("revision_count"))


def _route_after_evaluator(
    state: WorkflowState,
) -> Literal["pass", "revise", "discard"]:
    """评估后分流：阻断失败只在 ``revision_count == 0`` 时回 Planner。"""
    evaluation: EvaluationResult | None = state.get("evaluation")
    if evaluation is None:
        raise ValueError("评估分流缺少本轮评估结果")
    return _bounded_route(evaluation.passed, state.get("revision_count"))


def _route_after_confirmation(state: WorkflowState) -> Literal["confirm", "reject"]:
    """确认边：只在 ``wait_for_confirmation`` 写下 ``confirmed``／``rejected`` 之后分流。"""
    confirmation = state.get("confirmation")
    if confirmation == "confirmed":
        return "confirm"
    if confirmation == "rejected":
        return "reject"
    raise ConfirmationConflict(f"确认分支缺少用户动作：confirmation={confirmation!r}")


def build_generate_plan_graph(
    deps: GeneratePlanDeps,
    *,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    """按上述拓扑编译生成计划子图；节点构造时按职责注入各自的依赖边界。"""
    deterministic = PlanDeterministicNodes(deps.deterministic)
    planner = PlannerAgentNode(deps.planner)
    evaluator = EvaluatorAgentNode(deps.evaluator)
    writes = PlanWriteNodes(deps.writes)
    builder = StateGraph(WorkflowState, context_schema=GeneratePlanRun)
    builder.add_node("require_active_plan", deterministic.require_active_plan)
    builder.add_node("validate_required_profile", deterministic.validate_required_profile)
    builder.add_node("load_skill", partial(load_skill, skills=deps.skills))
    builder.add_node("planner_agent", planner.planner_agent)
    builder.add_node("validate_plan", deterministic.validate_plan)
    builder.add_node("evaluator_agent", evaluator.evaluator_agent)
    builder.add_node("revise_once", deterministic.revise_once)
    builder.add_node("persist_draft", writes.persist_draft)
    builder.add_node(WAITING_CONFIRMATION_NODE, writes.wait_for_confirmation)
    builder.add_node("activate_plan", writes.activate_plan)
    builder.add_node("archive_draft", writes.archive_draft)
    builder.add_node("discard_failed_candidate", deterministic.discard_failed_candidate)

    builder.add_conditional_edges(
        START,
        _route_by_intent,
        {"adjust": "require_active_plan", "generate": "validate_required_profile"},
    )
    builder.add_edge("require_active_plan", "validate_required_profile")
    builder.add_edge("validate_required_profile", "load_skill")
    builder.add_edge("load_skill", "planner_agent")
    builder.add_edge("planner_agent", "validate_plan")
    builder.add_conditional_edges(
        "validate_plan",
        _route_after_validate_plan,
        {
            "pass": "evaluator_agent",
            "revise": "revise_once",
            "discard": "discard_failed_candidate",
        },
    )
    builder.add_conditional_edges(
        "evaluator_agent",
        _route_after_evaluator,
        {
            "pass": "persist_draft",
            "revise": "revise_once",
            "discard": "discard_failed_candidate",
        },
    )
    builder.add_edge("revise_once", "planner_agent")
    builder.add_edge("persist_draft", WAITING_CONFIRMATION_NODE)
    builder.add_conditional_edges(
        WAITING_CONFIRMATION_NODE,
        _route_after_confirmation,
        {"confirm": "activate_plan", "reject": "archive_draft"},
    )
    builder.add_edge("activate_plan", END)
    builder.add_edge("archive_draft", END)
    builder.add_edge("discard_failed_candidate", END)
    return builder.compile(checkpointer=checkpointer)


async def invoke_generate_plan(
    graph: CompiledStateGraph,
    state: WorkflowState,
    config: RunnableConfig,
    run: GeneratePlanRun,
) -> dict[str, Any]:
    """一次生成计划 invocation 的唯一入口：用 Run 时限包住完整一次 Graph 调用。"""
    async with asyncio.timeout(run.budget.remaining_run_seconds()):
        return await graph.ainvoke(state, config, context=run)


async def invoke_confirmation(
    graph: CompiledStateGraph,
    *,
    conversation_id: str,
    run_id: str,
    plan_id: int,
    action: ConfirmationAction,
    run: GeneratePlanRun,
    deps: GeneratePlanDeps,
) -> Plan:
    """确认／拒绝的唯一入口：checkpoint 优先，无法恢复时按唯一 draft 兜底。

    ``graph`` 是顶层 Run 图：确认 interrupt 由计划子图的 ``wait_for_confirmation`` 抛出，父图把它记为
    ``plan`` 任务的等待状态，恢复必须从顶层图发出，``Command(resume=...)`` 才会落到该子图节点上。
    """
    config = thread_config(conversation_id)
    waiting_plan_id = _waiting_confirmation_plan_id(await graph.aget_state(config))
    if waiting_plan_id is not None:
        if waiting_plan_id != plan_id:
            raise ConfirmationConflict(
                "请求 plan_id 与等待确认的 draft 不一致："
                f"{plan_id} != {waiting_plan_id}"
            )
        await graph.ainvoke(
            Command(resume={"action": action, "run_id": run_id, "plan_id": plan_id}),
            config,
            context=run,
        )
        confirmed = await deps.deterministic.plans.read_by_id(plan_id)
        if confirmed is None:
            raise PlanNotFound(f"计划不存在：{plan_id}")
        return confirmed
    draft = await deps.writes.plans.get_unique_draft()
    if draft is not None and draft.id != plan_id:
        raise ConfirmationConflict(
            f"请求 plan_id 与业务库唯一 draft 不一致：{plan_id} != {draft.id}"
        )
    timestamp = deps.writes.now().isoformat()
    if action == "confirm":
        return await deps.writes.plans.activate_plan(
            plan_id,
            business_day=run.business_day,
            confirmed_at=timestamp,
            archived_at=timestamp,
        )
    return await deps.writes.plans.archive_draft(plan_id, archived_at=timestamp)


def _waiting_confirmation_plan_id(snapshot: StateSnapshot) -> int | None:
    """图是否正停在计划确认 interrupt；是则返回载荷里的 ``draft_plan_id``，否则 ``None``。

    ``wait_for_confirmation`` 的 ``kind=plan_confirmation`` 载荷唯一，顶层快照的 ``interrupts`` 因此可直接判别。
    """
    for pending in snapshot.interrupts:
        value = pending.value
        if (
            isinstance(value, Mapping)
            and value.get("kind") == "plan_confirmation"
            and isinstance(value.get("draft_plan_id"), int)
        ):
            return value["draft_plan_id"]
    return None
