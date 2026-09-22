# ST-02 顶层图拓扑：``safety_scan`` 唯一前置、七项合法 Intent 的分支归属与计划子图的接线方式。
# 依据：docs/subtask/ST-02-top-level-graph-safety-router.md「验收标准」。
# 计划子图用标记替身，本文件只验证拓扑与分流，不触库、不读 Skill。

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.application.agent.contracts import (
    ExistingDraftTarget,
    GeneratePlanRun,
    Intent,
    WorkflowState,
    initial_workflow_state,
)
from app.application.agent.prompts import SAFETY_STOP_MESSAGE
from app.application.agent.router import WORKFLOW_INTENTS
from app.application.agent.run_graph import (
    GENERAL_NODE,
    PLAN_NODE,
    PREPARE_PLAN_NODE,
    ROUTE_TARGETS,
    ROUTER_NODE,
    SAFETY_SCAN_NODE,
    SAFETY_STOP_NODE,
    GeneralOutcome,
    RunBranches,
    RunGraphNodes,
    build_agent_run_graph,
)
from app.domain.plans.schema import DeterministicResult, EvaluationResult

BUSINESS_DAY = date(2026, 6, 1)
PLAN_MARKER_NODE = "plan_marker"
RED_FLAG_REQUEST = "训练时胸部异常不适，还能继续吗？"

#: 七项合法 Intent 的 Router 响应：与 ``WORKFLOW_INTENTS`` 一一对应。
ROUTE_SCRIPTS: Mapping[Intent, Mapping[str, Any]] = {
    "view_schedule": {
        "domain": "workout_execution",
        "action": "query",
        "execution_type": "schedule_query",
    },
    "form_record": {
        "domain": "workout_execution",
        "action": "create",
        "execution_type": "form_record",
    },
    "natural_language_record": {
        "domain": "workout_execution",
        "action": "create",
        "execution_type": "natural_language_record",
    },
    "generate_plan": {"domain": "plan_management", "action": "create"},
    "adjust_plan": {"domain": "plan_management", "action": "modify"},
    "view_progress": {"domain": "analytics", "action": "query"},
    "general": {"domain": "general", "action": "chat"},
}


@dataclass
class RoutingModel:
    """Router 的结构化替身：按序出队，其余入口一律失败（未经脚本化的调用即测试失败）。"""

    scripts: list[Mapping[str, Any]] = field(default_factory=list)

    @property
    def structured_calls(self) -> int:
        return len(self._taken)

    _taken: list[Mapping[str, Any]] = field(default_factory=list)

    async def text(self, system_prompt: str, user_payload: str) -> str:
        raise AssertionError(f"Router 之外不应有文本调用：{system_prompt[:24]!r}")

    async def structured(
        self,
        system_prompt: str,
        user_payload: str,
        schema: type[Any],
    ) -> Any:
        assert self.scripts, "固定替身收到未脚本化的结构化调用"
        script = self.scripts.pop(0)
        self._taken.append(script)
        return schema.model_validate(script)

    async def tools(self, messages: Any, offered_tools: Any) -> Any:
        raise AssertionError("顶层图不应直接调用工具")


@dataclass
class RecordingBranches:
    """两个分支的替身：记录收到的 Intent，产出固定结果，不触库。"""

    general_intents: list[Intent] = field(default_factory=list)
    plan_intents: list[Intent] = field(default_factory=list)

    def as_run_branches(self) -> RunBranches:
        async def general(
            intent: Intent, state: WorkflowState, run: GeneratePlanRun
        ) -> GeneralOutcome:
            self.general_intents.append(intent)
            return GeneralOutcome(message=f"general:{intent}")

        async def plan_target(
            intent: Intent, run: GeneratePlanRun
        ) -> ExistingDraftTarget:
            self.plan_intents.append(intent)
            return ExistingDraftTarget()

        return RunBranches(general=general, plan_target=plan_target)


def _plan_marker_graph() -> CompiledStateGraph:
    """计划子图替身：单个标记节点，用于观察顶层图是否把请求交给 ``plan`` 分支。"""
    builder = StateGraph(WorkflowState, context_schema=GeneratePlanRun)
    builder.add_node(PLAN_MARKER_NODE, lambda state: {})
    builder.add_edge(START, PLAN_MARKER_NODE)
    builder.add_edge(PLAN_MARKER_NODE, END)
    return builder.compile(checkpointer=InMemorySaver())


@dataclass
class _Runtime:
    """节点直调时的运行上下文外壳：``RunGraphNodes`` 只读 ``context``。"""

    context: GeneratePlanRun


def _graph(
    model: RoutingModel, branches: RecordingBranches
) -> CompiledStateGraph:
    return build_agent_run_graph(
        plan_graph=_plan_marker_graph(),
        model=model,
        branches=branches.as_run_branches(),
        checkpointer=InMemorySaver(),
    )


def _state(request: str) -> WorkflowState:
    return initial_workflow_state(
        run_id="run-1",
        conversation_id="conv-1",
        user_id="user-1",
        client_request_id="request-1",
        business_day=BUSINESS_DAY,
        request=request,
    )


async def _run(graph: CompiledStateGraph, request: str) -> list[tuple[str, dict[str, Any]]]:
    """把一次 invocation 收成 ``(节点名, 该节点写回的增量)`` 序列。"""
    run = GeneratePlanRun(business_day=BUSINESS_DAY)
    config = {"configurable": {"thread_id": "conv-1"}}
    chunks: list[tuple[str, dict[str, Any]]] = []
    async for _namespace, chunk in graph.astream(
        _state(request), config, context=run, stream_mode="updates", subgraphs=True
    ):
        for name, update in chunk.items():
            chunks.append((name, dict(update or {})))
    return chunks


async def _final_state(graph: CompiledStateGraph, request: str) -> WorkflowState:
    run = GeneratePlanRun(business_day=BUSINESS_DAY)
    config = {"configurable": {"thread_id": "conv-1"}}
    await graph.ainvoke(_state(request), config, context=run)
    values: WorkflowState = (await graph.aget_state(config)).values
    return values


def test_router_and_tools_are_preceded_only_by_safety_scan() -> None:
    """``safety_scan`` 是 ``router_node`` 与两个分支的唯一前置：START 也只指向它。"""
    graph = _graph(RoutingModel(), RecordingBranches())
    edges = {(edge.source, edge.target) for edge in graph.get_graph().edges}

    assert {target for source, target in edges if source == "__start__"} == {
        SAFETY_SCAN_NODE
    }
    assert {source for source, target in edges if target == ROUTER_NODE} == {
        SAFETY_SCAN_NODE
    }
    assert {source for source, target in edges if target == GENERAL_NODE} == {
        ROUTER_NODE
    }
    assert {source for source, target in edges if target == PLAN_NODE} == {
        PREPARE_PLAN_NODE
    }
    assert {source for source, target in edges if target == PREPARE_PLAN_NODE} == {
        ROUTER_NODE
    }


def test_route_targets_cover_exactly_the_seven_legal_intents() -> None:
    """分支表与 ``WORKFLOW_INTENTS`` 的合法 Intent 集合一致：五项进 General、两项进计划。"""
    assert set(ROUTE_TARGETS) == set(WORKFLOW_INTENTS.values())
    assert {
        intent for intent, target in ROUTE_TARGETS.items() if target == GENERAL_NODE
    } == {
        "form_record",
        "natural_language_record",
        "view_progress",
        "view_schedule",
        "general",
    }
    assert {
        intent
        for intent, target in ROUTE_TARGETS.items()
        if target == PREPARE_PLAN_NODE
    } == {"generate_plan", "adjust_plan"}


@pytest.mark.parametrize("intent", list(ROUTE_SCRIPTS))
async def test_each_legal_intent_reaches_its_branch_with_one_router_call(
    intent: Intent,
) -> None:
    """七项合法 Intent 各自落到唯一分支：节点序列可观察，且只调一次 Router。"""
    model = RoutingModel(scripts=[dict(ROUTE_SCRIPTS[intent])])
    branches = RecordingBranches()
    names = [name for name, _ in await _run(_graph(model, branches), "今天练什么？")]

    assert names[:2] == [SAFETY_SCAN_NODE, ROUTER_NODE]
    assert model.structured_calls == 1
    if ROUTE_TARGETS[intent] == GENERAL_NODE:
        assert names[2:] == [GENERAL_NODE]
        assert branches.general_intents == [intent]
        assert branches.plan_intents == []
    else:
        assert names[2:] == [PREPARE_PLAN_NODE, PLAN_MARKER_NODE, PLAN_NODE]
        assert branches.plan_intents == [intent]
        assert branches.general_intents == []


async def test_plan_entry_resets_the_previous_runs_channels() -> None:
    """同一 thread 的新 Run 进计划分支时，上一 Run 的修订／评估／证据／终态通道必须归零。"""
    stale: WorkflowState = {
        **_state("给我一份计划"),
        "intent": "generate_plan",
        "revision_count": 1,
        "revision_feedback": ("goal_alignment: 目标不匹配",),
        "deterministic_result": DeterministicResult(passed=False, failures=()),
        "evaluation": EvaluationResult.model_validate({
            "passed": False,
            "deterministic": {"passed": True, "failures": []},
            "rubric": {
                "goal_alignment": {"passed": False, "reason": "目标不匹配"},
                "schedule_reasonableness": {"passed": True, "reason": "合理"},
                "explanation_quality": {"passed": True, "reason": "清楚"},
            },
            "blocking_failures": ["goal_alignment: 目标不匹配"],
            "warnings": [],
            "revision_count": 1,
        }),
        "termination_reason": "discard_failed_candidate",
    }
    nodes = RunGraphNodes(
        model=RoutingModel(), branches=RecordingBranches().as_run_branches()
    )
    runtime: Any = _Runtime(GeneratePlanRun(business_day=BUSINESS_DAY))

    update = await nodes.prepare_plan(stale, runtime)

    assert update == {
        "termination_reason": None,
        "revision_count": 0,
        "revision_feedback": (),
        "deterministic_result": None,
        "evaluation": None,
        "evaluation_result": None,
        "planner_evidence": (),
        "draft_plan_id": None,
    }


async def test_safety_hit_stops_before_router_with_zero_model_and_branch_calls() -> None:
    """红旗词命中：Router、General 与计划分支调用次数全为 0，终态是固定安全消息。"""
    model = RoutingModel(scripts=[dict(ROUTE_SCRIPTS["general"])])
    branches = RecordingBranches()
    graph = _graph(model, branches)
    chunks = await _run(graph, RED_FLAG_REQUEST)

    assert [name for name, _ in chunks] == [SAFETY_SCAN_NODE, SAFETY_STOP_NODE]
    assert chunks[0][1]["safety_hits"] == ("胸部异常不适",)
    assert model.structured_calls == 0
    assert branches.general_intents == []
    assert branches.plan_intents == []
    assert model.scripts == [dict(ROUTE_SCRIPTS["general"])]

    final = await _final_state(graph, RED_FLAG_REQUEST)
    assert final["termination_reason"] == "safety_stop"
    assert list(final["safety_hits"]) == ["胸部异常不适"]
    assert final["intent"] is None
    assert final["final_result"] is not None
    assert tuple(final["final_result"].messages) == (SAFETY_STOP_MESSAGE,)
