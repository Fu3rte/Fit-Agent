import asyncio
from collections.abc import Mapping
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
from app.application.agent.plan_nodes import GeneratePlanNodes
from app.application.services.plans_service import PlanNotFound
from app.domain.plans.schema import EvaluationResult, Plan

WAITING_CONFIRMATION_NODE = "wait_for_confirmation"

ConfirmationAction = Literal["confirm", "reject"]

NODE_NAMES: tuple[str, ...] = (
    "safety_check",
    "safety_stop",
    "require_active_plan",
    "validate_required_profile",
    "load_context",
    "load_skill",
    "planner",
    "evaluator",
    "revise_once",
    "persist_draft",
    WAITING_CONFIRMATION_NODE,
    "activate_plan",
    "archive_draft",
    "reject_draft",
)


def _route_after_safety_check(
    state: WorkflowState,
) -> Literal["hit", "adjust", "generate"]:
    """安全分流与调整分支：``safety_check`` 命中封闭词表时已写入 ``termination_reason='safety_stop'``。"""
    if state.get("termination_reason") == "safety_stop":
        return "hit"
    return "adjust" if state.get("intent") == ADJUST_PLAN_INTENT else "generate"


def _route_after_evaluator(state: WorkflowState) -> Literal["pass", "revise", "reject"]:
    """评估后分流：通过进持久化；阻断失败只在 ``revision_count == 0`` 时回 Planner 一次。"""
    evaluation: EvaluationResult = state["evaluation"]
    if evaluation.passed:
        return "pass"
    if state.get("revision_count") or 0:
        return "reject"
    return "revise"


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
    """按上述拓扑编译生成计划子图；节点构造时注入 ``deps``（含模型 callable）。"""
    nodes = GeneratePlanNodes(deps)
    builder = StateGraph(WorkflowState, context_schema=GeneratePlanRun)
    builder.add_node("safety_check", nodes.safety_check)
    builder.add_node("safety_stop", nodes.safety_stop)
    builder.add_node("require_active_plan", nodes.require_active_plan)
    builder.add_node("validate_required_profile", nodes.validate_required_profile)
    builder.add_node("load_context", nodes.load_context)
    builder.add_node("load_skill", nodes.load_skill)
    builder.add_node("planner", nodes.planner)
    builder.add_node("evaluator", nodes.evaluator)
    builder.add_node("revise_once", nodes.revise_once)
    builder.add_node("persist_draft", nodes.persist_draft)
    builder.add_node(WAITING_CONFIRMATION_NODE, nodes.wait_for_confirmation)
    builder.add_node("activate_plan", nodes.activate_plan)
    builder.add_node("archive_draft", nodes.archive_draft)
    builder.add_node("reject_draft", nodes.reject_draft)

    builder.add_edge(START, "safety_check")
    builder.add_conditional_edges(
        "safety_check",
        _route_after_safety_check,
        {
            "hit": "safety_stop",
            "adjust": "require_active_plan",
            "generate": "validate_required_profile",
        },
    )
    builder.add_edge("safety_stop", END)
    builder.add_edge("require_active_plan", "validate_required_profile")
    builder.add_edge("validate_required_profile", "load_context")
    builder.add_edge("load_context", "load_skill")
    builder.add_edge("load_skill", "planner")
    builder.add_edge("planner", "evaluator")
    builder.add_conditional_edges(
        "evaluator",
        _route_after_evaluator,
        {
            "pass": "persist_draft",
            "revise": "revise_once",
            "reject": "reject_draft",
        },
    )
    builder.add_edge("revise_once", "evaluator")
    builder.add_edge("persist_draft", WAITING_CONFIRMATION_NODE)
    builder.add_conditional_edges(
        WAITING_CONFIRMATION_NODE,
        _route_after_confirmation,
        {"confirm": "activate_plan", "reject": "archive_draft"},
    )
    builder.add_edge("activate_plan", END)
    builder.add_edge("archive_draft", END)
    builder.add_edge("reject_draft", END)
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
    plan_id: int,
    action: ConfirmationAction,
    run: GeneratePlanRun,
    deps: GeneratePlanDeps,
) -> Plan:
    """确认／拒绝的唯一入口：checkpoint 优先，无法恢复时按唯一 draft 兜底。"""
    config = thread_config(conversation_id)
    waiting_plan_id = _waiting_confirmation_plan_id(await graph.aget_state(config))
    if waiting_plan_id is not None:
        if waiting_plan_id != plan_id:
            raise ConfirmationConflict(
                "请求 plan_id 与等待确认的 draft 不一致："
                f"{plan_id} != {waiting_plan_id}"
            )
        await graph.ainvoke(
            Command(resume={"action": action, "plan_id": plan_id}),
            config,
            context=run,
        )
        confirmed = await deps.plans.read_by_id(plan_id)
        if confirmed is None:
            raise PlanNotFound(f"计划不存在：{plan_id}")
        return confirmed
    draft = await deps.persistence.get_unique_draft()
    if draft is not None and draft.id != plan_id:
        raise ConfirmationConflict(
            f"请求 plan_id 与业务库唯一 draft 不一致：{plan_id} != {draft.id}"
        )
    timestamp = deps.now().isoformat()
    if action == "confirm":
        return await deps.activation.activate(
            plan_id,
            business_day=run.business_day,
            confirmed_at=timestamp,
            archived_at=timestamp,
        )
    return await deps.activation.reject(plan_id, archived_at=timestamp)


def _waiting_confirmation_plan_id(snapshot: StateSnapshot) -> int | None:
    """图是否正停在确认 interrupt；是则返回 interrupt 载荷里的 ``draft_plan_id``，否则 ``None``。"""
    if WAITING_CONFIRMATION_NODE not in snapshot.next:
        return None
    for pending in snapshot.interrupts:
        value = pending.value
        if isinstance(value, Mapping) and isinstance(value.get("draft_plan_id"), int):
            return value["draft_plan_id"]
    return None
