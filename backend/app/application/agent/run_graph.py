"""顶层 Agent Run 图：``safety_scan`` 唯一前置，``router_node`` 把七项合法 Intent 分到 General／计划两个分支。"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

from app.application.agent.contracts import (
    ADJUST_PLAN_INTENT,
    AgentRunResult,
    ExistingDraftTarget,
    GeneratePlanRun,
    Intent,
    WorkflowState,
)
from app.application.agent.prompts import SAFETY_STOP_MESSAGE
from app.application.agent.router import (
    NON_PLAN_INTENTS,
    WORKFLOW_INTENTS,
    classify_intent,
    workflow_intent,
)
from app.application.ports import ModelGateway
from app.domain.profile.safety import message_red_flag_hits

SAFETY_SCAN_NODE = "safety_scan"
SAFETY_STOP_NODE = "safety_stop"
ROUTER_NODE = "router_node"
GENERAL_NODE = "general"
PREPARE_PLAN_NODE = "prepare_plan"
PLAN_NODE = "plan"

#: 走计划子图的两项 Intent：其余五项会话 Intent 全部进 General 分支。
PLAN_INTENTS: tuple[Intent, ...] = ("generate_plan", ADJUST_PLAN_INTENT)

#: 七项合法 Intent → 分支入口节点：Router 的条件边直接读这张表，表外 Intent 无出口。
ROUTE_TARGETS: Mapping[Intent, str] = {
    **dict.fromkeys(NON_PLAN_INTENTS, GENERAL_NODE),
    **dict.fromkeys(PLAN_INTENTS, PREPARE_PLAN_NODE),
}

if set(ROUTE_TARGETS) != set(WORKFLOW_INTENTS.values()):
    raise ValueError("顶层分支表与 WORKFLOW_INTENTS 的合法 Intent 集合不一致")


@dataclass(frozen=True, slots=True)
class GeneralOutcome:
    """General 分支的一次结果：可见文本 ＋ 标准 ``ui_actions``（``waiting`` 事件的 payload）。"""

    message: str
    actions: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class RunBranches:
    """顶层图两个分支的既有实现：General 分支与计划分支入口处置，由 Run 服务注入。"""

    general: Callable[[Intent, WorkflowState, GeneratePlanRun], Awaitable[GeneralOutcome]]

    plan_target: Callable[[Intent, GeneratePlanRun], Awaitable[ExistingDraftTarget]]


def _run(runtime: Runtime[GeneratePlanRun]) -> GeneratePlanRun:
    """取回本次 invocation 的运行上下文；缺失即调用方错误。"""
    run = runtime.context
    if run is None:
        raise RuntimeError("顶层 Run 图缺少运行上下文：预算与业务日都由它给出")
    return run


def _intent(state: WorkflowState) -> Intent:
    """条件边已保证分支只接收七项合法 Intent；缺失即拓扑被改坏。"""
    intent = state.get("intent")
    if intent not in ROUTE_TARGETS:
        raise ValueError(f"分支收到表外 Intent：{intent!r}")
    return intent


def _fresh_plan_channels() -> WorkflowState:
    """计划路径每个 Run 的通道起点：修订预算、候选证据与本轮确定性／评估结果一律归零。

    同一 ``conversation_id`` 复用 thread 时 checkpoint 会保留这些键；不在这里归零，新 Run 的首次
    候选会带入上一 Run 的 ``revision_count`` 与修订理由，并在首次失败时直接丢弃。计划路径读的通道是
    ``deterministic_result`` 与 ``evaluation``（``evaluation_result`` 是顶层共用键）；终态一并归零，
    上一 Run 的 ``termination_reason`` 不跨 Run 残留。
    """
    return {
        "termination_reason": None,
        "revision_count": 0,
        "revision_feedback": (),
        "deterministic_result": None,
        "evaluation": None,
        "evaluation_result": None,
        "planner_evidence": (),
    }


class RunGraphNodes:
    def __init__(self, *, model: ModelGateway, branches: RunBranches) -> None:
        self._model = model
        self._branches = branches

    async def safety_scan(self, state: WorkflowState) -> WorkflowState:
        """所有模型与 Tool 调用的唯一前置：封闭红旗词表精确子串扫描，命中即写终止原因。"""
        hits = message_red_flag_hits(state["request"])
        if hits:
            return {"safety_hits": hits, "termination_reason": "safety_stop"}
        return {"safety_hits": ()}

    async def safety_stop(self, state: WorkflowState) -> WorkflowState:
        """安全短路：固定安全消息与终态，不进 Router、不调模型、不读写业务库。"""
        return {
            "final_result": AgentRunResult(
                intent=None,
                messages=(SAFETY_STOP_MESSAGE,),
                termination_reason="safety_stop",
                draft_plan_id=None,
            )
        }

    async def router_node(
        self, state: WorkflowState, runtime: Runtime[GeneratePlanRun]
    ) -> WorkflowState:
        """Router 的唯一实现：一次结构化分类，Schema 表外组合在解析期失败。"""
        run = _run(runtime)
        route = await classify_intent(
            state["request"],
            model=self._model,
            budget=run.budget,
            history=run.conversation_messages,
        )
        intent = workflow_intent(route)
        if intent != ADJUST_PLAN_INTENT:
            run.adjustment = None
        return {"intent": intent}

    async def general(
        self, state: WorkflowState, runtime: Runtime[GeneratePlanRun]
    ) -> WorkflowState:
        """五项会话 Intent 的唯一分支：既有实现产出 message ＋ ui_actions，本节点只收敛终态。"""
        intent = _intent(state)
        outcome = await self._branches.general(intent, state, _run(runtime))
        return {
            "ui_actions": outcome.actions,
            "final_result": AgentRunResult(
                intent=intent,
                messages=(outcome.message,),
                termination_reason=None,
                draft_plan_id=None,
            ),
        }

    async def prepare_plan(
        self, state: WorkflowState, runtime: Runtime[GeneratePlanRun]
    ) -> WorkflowState:
        """计划分支入口：归零上一 Run 的修订／评估通道，再复用唯一 draft 或替换身份，冲突在模型调用前失败。"""
        intent = _intent(state)
        target = await self._branches.plan_target(intent, _run(runtime))
        fresh = _fresh_plan_channels()
        if target.reused is not None:
            return {
                **fresh,
                "draft_plan_id": target.reused.id,
                "ui_actions": ({"draft_plan_id": target.reused.id},),
                "final_result": AgentRunResult(
                    intent=intent,
                    messages=(),
                    termination_reason=None,
                    draft_plan_id=target.reused.id,
                ),
            }
        return {**fresh, "draft_plan_id": target.replacement_id}


def _route_after_safety_scan(
    state: WorkflowState,
) -> Literal["safety_scan_hit", "safety_scan_pass"]:
    """安全命中短路到 ``safety_stop``，否则才进 Router。"""
    return (
        "safety_scan_hit"
        if state.get("termination_reason") == "safety_stop"
        else "safety_scan_pass"
    )


def _route_after_router(state: WorkflowState) -> Intent:
    """Router 的唯一出口：七项合法 Intent 各进一个顶层分支。"""
    return _intent(state)


def _route_after_prepare_plan(state: WorkflowState) -> Literal["reuse", "plan"]:
    """已有唯一 draft 的复用短路终态已定，其余才进计划子图。"""
    return "reuse" if state.get("final_result") is not None else "plan"


def build_agent_run_graph(
    *,
    plan_graph: CompiledStateGraph,
    model: ModelGateway,
    branches: RunBranches,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    """编译顶层 Run 图：计划子图只作为 ``plan`` 分支被接线，不由 Python 直接调用。"""
    nodes = RunGraphNodes(model=model, branches=branches)
    builder = StateGraph(WorkflowState, context_schema=GeneratePlanRun)
    builder.add_node(SAFETY_SCAN_NODE, nodes.safety_scan)
    builder.add_node(SAFETY_STOP_NODE, nodes.safety_stop)
    builder.add_node(ROUTER_NODE, nodes.router_node)
    builder.add_node(GENERAL_NODE, nodes.general)
    builder.add_node(PREPARE_PLAN_NODE, nodes.prepare_plan)
    builder.add_node(PLAN_NODE, plan_graph)
    builder.add_edge(START, SAFETY_SCAN_NODE)
    builder.add_conditional_edges(
        SAFETY_SCAN_NODE,
        _route_after_safety_scan,
        {"safety_scan_hit": SAFETY_STOP_NODE, "safety_scan_pass": ROUTER_NODE},
    )
    builder.add_edge(SAFETY_STOP_NODE, END)
    builder.add_conditional_edges(ROUTER_NODE, _route_after_router, dict(ROUTE_TARGETS))
    builder.add_edge(GENERAL_NODE, END)
    builder.add_conditional_edges(
        PREPARE_PLAN_NODE,
        _route_after_prepare_plan,
        {"reuse": END, "plan": PLAN_NODE},
    )
    return builder.compile(checkpointer=checkpointer)
