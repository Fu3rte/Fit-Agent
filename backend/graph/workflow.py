"""生成计划子图：直接以 ``intent='generate_plan'`` 驱动，不实现 Router（stage4.md §2.2、§5.3、§6 Subtask 04）。

拓扑（stage4.md §6 Subtask 04 任务 2、讨论总结 §4.2）：

```text
safety_check
├─ hit → safety_stop（结束：不生成计划）
└─ clear → validate_required_profile → load_context → load_skill → planner → evaluator
    ├─ pass → persist_draft → wait_for_confirmation
    ├─ 阻断失败且 revision_count == 0 → revise_once → evaluator
    └─ 阻断失败且 revision_count >= 1 → reject_draft（结束）
```

硬边界：

- **不包装 Router／第三个 Agent**：没有意图分类节点，Stage 4 直接以 ``intent='generate_plan'`` 驱动；
  调整计划链路、HTTP／SSE 与确认页面都不在本模块。
- **一次修订上限**：``_route_after_evaluator`` 在 ``revision_count >= 1`` 时只走 ``reject_draft``，
  不再回到 Planner；``revise_once`` 因此最多执行一次。
- **运行预算**：每次 invocation 由调用方传入 ``graph.nodes.GeneratePlanRun``（业务日期 ＋
  模型请求预算：单次请求 60 秒、单 Run 180 秒、最多 5 次）；该 Run 时限由
  :func:`invoke_generate_plan` 包住**完整一次** Graph 调用（含非模型节点），预算本身不写 State、
  不落 checkpoint。调用方不得绕过该入口直接 ``graph.ainvoke``。
- **Checkpointer 由调用方给出**：``wait_for_confirmation`` 的 interrupt 需要它；编译本子图不改变
  Stage 3 的存档生命周期（``graph/checkpointer.py``）。
"""

import asyncio
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from domain.plans.schema import EvaluationResult
from graph.nodes import GeneratePlanDeps, GeneratePlanNodes, GeneratePlanRun
from graph.state import WorkflowState

#: 生成计划子图的节点名（顺序即 stage4.md §6 Subtask 04 任务 2 的拓扑顺序）。
NODE_NAMES: tuple[str, ...] = (
    "safety_check",
    "safety_stop",
    "validate_required_profile",
    "load_context",
    "load_skill",
    "planner",
    "evaluator",
    "revise_once",
    "persist_draft",
    "wait_for_confirmation",
    "reject_draft",
)


def _route_after_safety_check(state: WorkflowState) -> Literal["hit", "clear"]:
    """安全分流：``safety_check`` 命中封闭词表时已写入 ``termination_reason='safety_stop'``。

    判定本身只在 ``graph/nodes.py::GeneratePlanNodes.safety_check`` 里做（``message_red_flag_hits``），
    条件边只读结果，不重做语义判断；未命中的分支也会写明 ``termination_reason=None``，
    因此这里读到的是本次 Run 的结论，而不是同一 thread 上一次 Run 留在 checkpoint 里的值。
    """
    return "hit" if state.get("termination_reason") == "safety_stop" else "clear"


def _route_after_evaluator(state: WorkflowState) -> Literal["pass", "revise", "reject"]:
    """评估后分流：通过进持久化；阻断失败只在 ``revision_count == 0`` 时回 Planner 一次。"""
    evaluation: EvaluationResult = state["evaluation"]
    if evaluation.passed:
        return "pass"
    if state.get("revision_count") or 0:
        return "reject"
    return "revise"


def build_generate_plan_graph(
    deps: GeneratePlanDeps,
    *,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    """按上述拓扑编译生成计划子图；节点构造时注入 ``deps``（含模型 callable）。

    调用方按每次 invocation 传入运行上下文：经 :func:`invoke_generate_plan` 执行
    （Run 时限包住完整一次 Graph 调用），不直接 ``ainvoke``。
    """
    nodes = GeneratePlanNodes(deps)
    builder = StateGraph(WorkflowState, context_schema=GeneratePlanRun)
    builder.add_node("safety_check", nodes.safety_check)
    builder.add_node("safety_stop", nodes.safety_stop)
    builder.add_node(
        "validate_required_profile", nodes.validate_required_profile
    )
    builder.add_node("load_context", nodes.load_context)
    builder.add_node("load_skill", nodes.load_skill)
    builder.add_node("planner", nodes.planner)
    builder.add_node("evaluator", nodes.evaluator)
    builder.add_node("revise_once", nodes.revise_once)
    builder.add_node("persist_draft", nodes.persist_draft)
    builder.add_node("wait_for_confirmation", nodes.wait_for_confirmation)
    builder.add_node("reject_draft", nodes.reject_draft)

    builder.add_edge(START, "safety_check")
    builder.add_conditional_edges(
        "safety_check",
        _route_after_safety_check,
        {"hit": "safety_stop", "clear": "validate_required_profile"},
    )
    builder.add_edge("safety_stop", END)
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
    builder.add_edge("persist_draft", "wait_for_confirmation")
    builder.add_edge("wait_for_confirmation", END)
    builder.add_edge("reject_draft", END)
    return builder.compile(checkpointer=checkpointer)


async def invoke_generate_plan(
    graph: CompiledStateGraph,
    state: WorkflowState,
    config: RunnableConfig,
    run: GeneratePlanRun,
) -> dict[str, Any]:
    """一次生成计划 invocation 的唯一入口：用 Run 时限包住**完整**一次 Graph 调用。

    ``asyncio.timeout`` 的时限取自本次 invocation 的运行上下文
    （``run.budget.remaining_run_seconds()``），因此 180 秒上限不只限制模型调用资格：任何节点
    （包括 ``load_context`` 等非模型节点）超出 Run 时限都会以 :class:`TimeoutError` 终止本次 Run。
    若超时发生在持久化前，不写 draft／rejected、不动原 active；若业务 draft 已提交后才超时，则遵守
    stage4.md §3.10／§9.1 的跨库边界，不伪装回滚已提交的业务记录。单次请求 60 秒与最多 5 次请求的
    上限仍由 ``graph.nodes.ModelRequestBudget.begin_request`` 把握。

    生产接线（Stage 5）与测试都用本函数，不另行直接 ``ainvoke``。
    """
    async with asyncio.timeout(run.budget.remaining_run_seconds()):
        return await graph.ainvoke(state, config, context=run)
