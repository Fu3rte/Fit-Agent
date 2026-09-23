# ST-04 计划路径的确定性节点、有界修订与两个 ToolNode loop：结构校验与评估共用一条
# 「首版 → 修订一次 → 丢弃」路径，丢弃候选不写任何计划行，修订只接受 revision_count == 0；
# Planner 与 Evaluator 各自跑一次真实 ToolNode loop，证据只来自真实完成的工具调用。
# 本文件驱动计划节点、路由与 Planner／Evaluator Skill 的按需读取行为。

import json
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

from app.application.agent.contracts import (
    AdjustmentContext,
    ConfirmationConflict,
    GeneratePlanDeps,
    GeneratePlanRun,
    LoadedSkill,
    PlanDeterministicDeps,
    PlanLlmNodeDeps,
    PlanToolHarnesses,
    PlanWriteDeps,
    SkillMetadata,
    SkillReference,
    ToolEvidence,
    WorkflowState,
)
from app.application.agent.harness.registry import (
    EVALUATION_TOOLS_NODE_NAME,
    PLANNING_TOOLS,
    PLANNING_TOOLS_NODE_NAME,
    UnregisteredCandidateExercise,
    build_plan_tool_harnesses,
)
from app.application.agent.harness.snapshot import (
    PLAN_REQUIRED_FACTS,
    MissingPlanFacts,
)
from app.application.agent.harness.tools.exercise_dataset.store import (
    InMemoryCanonicalExerciseDataset,
)
from app.application.agent.plan_graph import (
    _route_after_evaluator,
    _route_after_validate_plan,
    build_generate_plan_graph,
)
from app.application.agent.plan_nodes import (
    EvaluatorAgentNode,
    PlanDeterministicNodes,
    PlannerAgentNode,
    _needs_planner_examples,
    _require_confirmation_action,
    load_skill,
)
from app.application.agent.prompts import DISCARD_FAILED_CANDIDATE_MESSAGE
from app.domain.actions.schema import Exercise
from app.domain.plans.schema import (
    DeterministicResult,
    EvaluationResult,
    PlanDraft,
    RubricResult,
    RuleFailure,
)
from app.domain.profile.schema import Fact, Profile
from app.domain.stats.schema import MetricChange, WorkoutGap
from config import TOOL_TIMEOUT_SECONDS

BUSINESS_DAY = date(2026, 6, 1)


def _profile(weekly_frequency: int = 1) -> Profile:
    return Profile(
        training_goal=Fact.known("增肌"),
        weekly_frequency=Fact.known(weekly_frequency),
        training_mode=Fact.known("bodyweight"),
        explicit_preferences=Fact.denied(),
        current_level=Fact.known("中级"),
        known_injuries=Fact.denied(),
        forbidden_exercise_ids=Fact.denied(),
    )


def _draft(exercise_id: str = "pull-up") -> PlanDraft:
    return PlanDraft.model_validate({
        "goal": "增肌",
        "starts_on": BUSINESS_DAY.isoformat(),
        "explanation": "每周一练",
        "weekly_frequency": 1,
        "training_days": [
            {
                "scheduled_on": BUSINESS_DAY.isoformat(),
                "exercises": [
                    {
                        "exercise_id": exercise_id,
                        "sets": 3,
                        "prescription": {"type": "bodyweight_reps", "reps_min": 5, "reps_max": 8},
                    }
                ],
            }
        ],
    })


@dataclass
class _Catalog:
    exercises: tuple[Exercise, ...]

    async def list_all(self) -> tuple[Exercise, ...]:
        return self.exercises


def _pull_up() -> Exercise:
    """目录里的自重引体：候选动作的唯一 canonical id，供真实 search_exercises 命中。"""
    return Exercise(
        id="pull-up",
        standard_name_zh="自重引体向上",
        aliases=("垂直拉",),
        equipment_variant="bodyweight",
        record_type="reps_bodyweight",
        load_convention=None,
        min_load_increment_kg=None,
        recommendable=True,
        modes=(),
        source_ref="exercises-dataset:0652",
        attribution="测试目录",
    )


def _plank() -> Exercise:
    return Exercise(
        id="plank",
        standard_name_zh="平板支撑",
        aliases=("平板", "plank"),
        equipment_variant="bodyweight",
        record_type="time",
        load_convention=None,
        min_load_increment_kg=None,
        recommendable=True,
        modes=("核心",),
        source_ref="refactor-log/stage2.md:§3",
        attribution="Fit-Agent Stage 2 口径",
    )


def _catalog() -> _Catalog:
    return _Catalog((_pull_up(), _plank()))


@dataclass
class _Profiles:
    profile: Profile | None

    async def read(self) -> Profile | None:
        return self.profile


@dataclass
class _Plans:
    async def read_active(self) -> Any:
        return SimpleNamespace(
            id=1,
            version=1,
            structured_content=_draft().model_dump(mode="json"),
        )


@dataclass
class _Records:
    async def list_recent(
        self,
        limit: int,
        *,
        from_on: date | None = None,
        to_on: date | None = None,
        exercise_ids: Collection[str] = (),
    ) -> tuple[Any, ...]:
        return ()


def _rubric() -> RubricResult:
    return RubricResult.model_validate({
        "goal_alignment": {"passed": True, "reason": "匹配"},
        "schedule_reasonableness": {"passed": True, "reason": "合理"},
        "explanation_quality": {"passed": True, "reason": "清楚"},
    })


@dataclass
class _Stats:
    async def list_valid_work_sets(self) -> tuple[Any, ...]:
        return ()

    async def list_linked_workouts(self) -> tuple[Any, ...]:
        return ()

    async def calendar_month(self, year: int, month: int) -> dict[str, Any]:
        return {}

    async def list_personal_bests(self) -> tuple[Any, ...]:
        return ()

    async def trend_summary(self, business_day: date) -> dict[str, Any]:
        return {}

    async def weight_change_between(
        self, from_on: date, to_on: date
    ) -> MetricChange:
        return MetricChange("no_data", None, None, None, None, None)

    async def days_since_last_workout(self, business_day: date) -> WorkoutGap:
        return WorkoutGap("no_data", None, None)


class _Revisions:
    """revision 端口替身：测试直接推高域值来模拟写入后的快照变化；catalog 由 schema_version 承担。"""

    def __init__(self) -> None:
        self.values = {"profile": 1, "workouts": 2, "plans": 3}

    async def read_all(self) -> dict[str, int]:
        return dict(self.values)


class _Persistence:
    """计划写服务替身：丢弃路径一旦碰它就是失败；只读 revision 端口照常提供。"""

    def __init__(self) -> None:
        self.revisions = _Revisions()

    async def persist_draft(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("丢弃候选不得写 plans 行")


@dataclass
class _Model:
    """模型替身：工具循环按每次 loop 的脚本出队，结构化请求只回候选与通过的 Rubric；记下发给模型的载荷。"""

    tool_loops: list[tuple[str, ...]] = field(default_factory=list)
    payloads: list[str] = field(default_factory=list)
    _awaiting_result: bool = False

    async def tools(self, messages: Any, offered_tools: Any) -> AIMessage:
        if self._awaiting_result:
            self._awaiting_result = False
            return AIMessage(content="事实已读完")
        self._awaiting_result = True
        names = self.tool_loops.pop(0) if self.tool_loops else ()
        return AIMessage(
            content="",
            tool_calls=[_tool_call(name, index) for index, name in enumerate(names)],
        )

    async def structured(
        self, system_prompt: str, payload: str, schema: type[Any]
    ) -> Any:
        self.payloads.append(payload)
        return _rubric() if schema is RubricResult else _draft()


def _tool_call(name: str, index: int) -> dict[str, Any]:
    """模型发起的一次只读工具调用：参数与目标工具的模型可见 Schema 一致。"""
    args: dict[str, Any] = {}
    if name == "search_exercises":
        args["query"] = "引体"
    elif name == "read_training_calendar":
        args = {"year": BUSINESS_DAY.year, "month": BUSINESS_DAY.month}
    return {"name": name, "args": args, "id": f"call-{index}", "type": "tool_call"}


#: generate_plan 的必需事实；adjust_plan 在此基础上追加 active 计划与日历。
GENERATE_FACTS = (
    "read_user_profile",
    "read_training_history",
    "read_progress",
    "search_exercises",
)
ADJUST_FACTS = (*GENERATE_FACTS, "read_active_plan", "read_training_calendar")


@dataclass
class _Skills:
    metadata_reads: int = 0
    body_reads: list[str] = field(default_factory=list)
    reference_reads: list[tuple[str, str]] = field(default_factory=list)

    def list_metadata(self) -> tuple[SkillMetadata, ...]:
        self.metadata_reads += 1
        return tuple(
            SkillMetadata(name=name, description=f"发现描述：{name}")
            for name in ("workout-planning", "plan-adjustment", "plan-evaluation", "fitness-knowledge")
        )

    def read_skill(self, name: str) -> str:
        self.body_reads.append(name)
        return f"专属指令：{name}"

    def read_reference(self, name: str, relative_path: str) -> SkillReference:
        self.reference_reads.append((name, relative_path))
        return SkillReference(path=relative_path, text=f"专属 reference：{name}:{relative_path}")

@dataclass
class _Deps:
    catalog: _Catalog
    stats: _Stats
    persistence: _Persistence
    schema_version: int = 8
    profiles: _Profiles = field(default_factory=lambda: _Profiles(_profile()))
    plans: _Plans = field(default_factory=_Plans)
    records: _Records = field(default_factory=_Records)
    model: _Model = field(default_factory=_Model)
    skills: _Skills = field(default_factory=_Skills)
    plan_harnesses: PlanToolHarnesses = field(
        default_factory=lambda: build_plan_tool_harnesses(
            timeout_seconds=TOOL_TIMEOUT_SECONDS
        )
    )


def _read_deps(deps: _Deps, *, evaluation: bool) -> PlanLlmNodeDeps:
    """一次 LLM Node 的构造期依赖：唯一模型入口 ＋ 该路径自己的 ToolNode 与只读端口。"""
    return PlanLlmNodeDeps(
        model=deps.model,
        harness=(
            deps.plan_harnesses.evaluation
            if evaluation
            else deps.plan_harnesses.planning
        ),
        profiles=deps.profiles,
        records=deps.records,
        catalog=deps.catalog,
        plans=deps.plans,
        stats=deps.stats,
        revisions=deps.persistence.revisions,
        schema_version=deps.schema_version,
        dataset=InMemoryCanonicalExerciseDataset(
            deps.catalog.exercises, catalog_revision=deps.schema_version
        ),
    )


def _planner(deps: _Deps) -> PlannerAgentNode:
    return PlannerAgentNode(_read_deps(deps, evaluation=False), skills=deps.skills)


def _evaluator(deps: _Deps) -> EvaluatorAgentNode:
    return EvaluatorAgentNode(_read_deps(deps, evaluation=True), skills=deps.skills)


def _deterministic(deps: _Deps) -> PlanDeterministicNodes:
    return PlanDeterministicNodes(
        PlanDeterministicDeps(
            profiles=deps.profiles,
            catalog=deps.catalog,
            plans=deps.plans,
            stats=deps.stats,
        )
    )


def _nodes() -> PlanDeterministicNodes:
    return _deterministic(
        _Deps(catalog=_Catalog(()), stats=_Stats(), persistence=_Persistence())
    )


@dataclass
class _Runtime:
    context: GeneratePlanRun


def _runtime_context(run: GeneratePlanRun) -> Runtime[GeneratePlanRun]:
    return cast("Runtime[GeneratePlanRun]", _Runtime(run))


def _deps() -> _Deps:
    return _Deps(catalog=_catalog(), stats=_Stats(), persistence=_Persistence())


def _generate_deps(deps: _Deps) -> GeneratePlanDeps:
    """计划子图的四个边界：写入边界在拓扑测试里不需要真实服务。"""
    return GeneratePlanDeps(
        planner=_read_deps(deps, evaluation=False),
        evaluator=_read_deps(deps, evaluation=True),
        deterministic=PlanDeterministicDeps(
            profiles=deps.profiles,
            catalog=deps.catalog,
            plans=deps.plans,
            stats=deps.stats,
        ),
        writes=cast(PlanWriteDeps, object()),
        skills=deps.skills,
    )


def _state(draft: PlanDraft, *, revision_count: int = 0) -> WorkflowState:
    return {
        "request": "给我一份计划",
        "draft_plan": draft,
        "revision_count": revision_count,  # type: ignore[typeddict-item]
    }


async def test_validate_plan_returns_every_rule_failure_without_a_model_call() -> None:
    """结构校验节点只做确定性校验：未收录的动作逐条列出，不调用模型。"""
    state = await _nodes().validate_plan(_state(_draft()), _Runtime(GeneratePlanRun(BUSINESS_DAY)))

    deterministic: DeterministicResult = state["deterministic_result"]
    assert deterministic.passed is False
    assert {failure.code for failure in deterministic.failures}


def test_validate_plan_and_evaluator_share_one_bounded_revision_path() -> None:
    """结构校验失败与评估失败的分流完全一致：首版回 Planner，修订后丢弃。"""
    failing = {"deterministic_result": DeterministicResult(passed=False, failures=())}
    passed = {"deterministic_result": DeterministicResult(passed=True, failures=())}
    evaluation = {"evaluation": EvaluationResult.model_validate({
        "passed": False,
        "deterministic": {"passed": True, "failures": []},
        "rubric": {
            "goal_alignment": {"passed": False, "reason": "目标不匹配"},
            "schedule_reasonableness": {"passed": True, "reason": "合理"},
            "explanation_quality": {"passed": True, "reason": "清楚"},
        },
        "blocking_failures": ["goal_alignment: 目标不匹配"],
        "warnings": [],
        "revision_count": 0,
    })}

    assert _route_after_validate_plan(passed) == "pass"
    assert _route_after_validate_plan(failing) == "revise"
    assert _route_after_validate_plan({**failing, "revision_count": 1}) == "discard"
    assert _route_after_evaluator(evaluation) == "revise"
    assert _route_after_evaluator({**evaluation, "revision_count": 1}) == "discard"


async def test_revise_once_refuses_a_second_revision_and_carries_failures() -> None:
    """修订节点只接受 ``revision_count == 0``：置 1 并带走本次失败理由。"""
    state = _state(_draft())
    state["deterministic_result"] = DeterministicResult(
        passed=False, failures=(RuleFailure(code="unknown_exercise", message="未收录"),)
    )
    nodes = _nodes()

    update = await nodes.revise_once(state, _Runtime(GeneratePlanRun(BUSINESS_DAY)))

    assert update == {"revision_count": 1, "revision_feedback": ("unknown_exercise: 未收录",)}
    with pytest.raises(ValueError):
        await nodes.revise_once(
            {**state, "revision_count": 1}, _Runtime(GeneratePlanRun(BUSINESS_DAY))
        )


async def test_discard_failed_candidate_clears_the_candidate_without_persistence() -> None:
    """二次失败：清掉可持久化引用并写内存终态，不写 plans。"""
    state = _state(_draft(), revision_count=1)
    state["draft_plan_id"] = None

    update = await _nodes().discard_failed_candidate(state)

    assert update["draft_plan_id"] is None
    assert update["termination_reason"] == "discard_failed_candidate"
    final_result = update["final_result"]
    assert final_result.termination_reason == "discard_failed_candidate"
    assert final_result.draft_plan_id is None
    assert final_result.messages == (DISCARD_FAILED_CANDIDATE_MESSAGE,)


async def test_revise_once_carries_the_current_cycle_failures_not_a_retained_evaluation() -> None:
    """上一轮评估结果尚在同一 thread 时，修订理由仍必须来自本轮结构校验的失败列表。"""
    state = _state(_draft())
    state["evaluation"] = EvaluationResult.model_validate({
        "passed": False,
        "deterministic": {"passed": True, "failures": []},
        "rubric": {
            "goal_alignment": {"passed": True, "reason": "匹配"},
            "schedule_reasonableness": {"passed": False, "reason": "上一轮的判断"},
            "explanation_quality": {"passed": True, "reason": "清楚"},
        },
        "blocking_failures": ["schedule_reasonableness: 上一轮的判断"],
        "warnings": [],
        "revision_count": 1,
    })
    state["deterministic_result"] = DeterministicResult(
        passed=False, failures=(RuleFailure(code="unknown_exercise", message="未收录"),)
    )

    update = await _nodes().revise_once(state, _Runtime(GeneratePlanRun(BUSINESS_DAY)))

    assert update == {"revision_count": 1, "revision_feedback": ("unknown_exercise: 未收录",)}


def test_plan_graph_topology_routes_revision_back_to_the_planner() -> None:
    """计划子图：修订回边指向 ``planner_agent``，丢弃节点收口，评估不再自己跑确定性校验。"""
    graph = build_generate_plan_graph(_generate_deps(_deps()))
    edges = {
        (edge.source, edge.target) for edge in graph.get_graph().edges
    }

    assert ("planner_agent", "validate_plan") in edges
    assert ("validate_plan", "evaluator_agent") in edges
    assert ("validate_plan", "revise_once") in edges
    assert ("validate_plan", "discard_failed_candidate") in edges
    assert ("evaluator_agent", "persist_draft") in edges
    assert ("evaluator_agent", "revise_once") in edges
    assert ("evaluator_agent", "discard_failed_candidate") in edges
    assert ("revise_once", "planner_agent") in edges
    assert not any(source == "revise_once" and target == "evaluator_agent" for source, target in edges)


def _planner_state(
    *, intent: str = "generate_plan", planner_evidence: tuple[ToolEvidence, ...] = ()
) -> WorkflowState:
    """Planner／Evaluator 的输入状态：身份、业务日、Skill 与候选证据都由 Run 注入。"""
    state = _state(_draft())
    state["intent"] = intent  # type: ignore[typeddict-item]
    state["user_id"] = "user-1"
    state["run_id"] = "run-1"
    state["business_day"] = BUSINESS_DAY
    skill_name = "plan-adjustment" if intent == "adjust_plan" else "workout-planning"
    rules_path = (
        "references/adjustment-rules.md"
        if intent == "adjust_plan"
        else "references/planning-rules.md"
    )
    state["loaded_skill"] = LoadedSkill(
        metadata=SkillMetadata(name=skill_name, description="发现描述"),
        body=f"专属指令：{skill_name}",
        references=(SkillReference(path=rules_path, text=f"专属规则：{skill_name}"),),
    )
    state["planner_evidence"] = planner_evidence
    return state


async def test_graph_skips_evaluator_when_deterministic_validation_fails() -> None:
    profile = _profile(weekly_frequency=2)
    deps = _Deps(
        catalog=_catalog(),
        stats=_Stats(),
        persistence=_Persistence(),
        profiles=_Profiles(profile),
        model=_Model(tool_loops=[GENERATE_FACTS, GENERATE_FACTS]),
    )
    run = GeneratePlanRun(BUSINESS_DAY)

    graph = cast(Any, build_generate_plan_graph(_generate_deps(deps)))
    result = await graph.ainvoke(_planner_state(), context=run)

    assert result["deterministic_result"].passed is False
    assert "evaluation" not in result
    assert deps.skills.body_reads == ["workout-planning"]
    assert deps.skills.reference_reads == [
        ("workout-planning", "references/planning-rules.md")
    ]
    assert deps.model.payloads and len(deps.model.payloads) == 2
    assert run.budget.used == 2


async def test_planner_and_evaluator_consume_one_candidate_snapshot() -> None:
    """两个 ToolNode loop 各跑一次：证据逐条来自真实调用，模型载荷里没有身份与 revision 字段。"""
    deps = _Deps(
        catalog=_catalog(),
        stats=_Stats(),
        persistence=_Persistence(),
        model=_Model(tool_loops=[GENERATE_FACTS, GENERATE_FACTS]),
    )
    run = GeneratePlanRun(BUSINESS_DAY)
    planner = _planner_state()

    written = await _planner(deps).planner_agent(planner, _Runtime(run))

    evidence: tuple[ToolEvidence, ...] = written["planner_evidence"]
    assert [
        (item.tool_name, item.revision_domain, item.revision) for item in evidence
    ] == [
        ("read_user_profile", "profile", 1),
        ("read_training_history", "workouts", 2),
        ("read_progress", "workouts", 2),
        ("search_exercises", "catalog", 8),
    ]
    assert "user-1" not in deps.model.payloads[0]
    assert "revision" not in deps.model.payloads[0]
    # 两个 loop 的预算彼此独立：工具调用不进 Run 预算，只有结构化请求计入。
    assert run.budget.tool_calls == 0
    assert run.budget.used == 1

    evaluated = await _evaluator(deps).evaluator_agent(
        {
            **planner,
            **written,
            "deterministic_result": DeterministicResult(passed=True, failures=()),
        },
        _Runtime(run),
    )

    evaluation: EvaluationResult = evaluated["evaluation"]
    assert evaluation.evidence == evidence
    assert evaluation.passed is True
    assert evaluation.blocking_failures == ()
    assert run.budget.tool_calls == 0
    assert run.budget.used == 2


async def test_planner_payload_facts_come_only_from_the_real_tool_loop() -> None:
    """Planner 事实来自真实 Tool loop；候选身份由检索返回，负荷由目录与工作组确定性补齐。"""
    deps = _Deps(
        catalog=_catalog(),
        stats=_Stats(),
        persistence=_Persistence(),
        model=_Model(tool_loops=[GENERATE_FACTS, GENERATE_FACTS]),
    )
    run = GeneratePlanRun(BUSINESS_DAY)

    await _planner(deps).planner_agent(_planner_state(), _Runtime(run))
    payload = json.loads(deps.model.payloads[0])

    assert set(payload) == {
        "request",
        "intent",
        "business_day",
        "skill",
        "facts",
        "candidate_actions",
    }
    assert [fact["tool"] for fact in payload["facts"]] == list(GENERATE_FACTS)
    assert payload["candidate_actions"] == [
        {
            "exercise_id": "pull-up",
            "standard_name_zh": "自重引体向上",
            "record_type": "reps_bodyweight",
            "load_convention": None,
            "min_load_increment_kg": None,
            "starting_load": {"status": "needs_calibration"},
        }
    ]
    assert payload["skill"] == {
        "name": "workout-planning",
        "instructions": "专属指令：workout-planning",
        "rules_reference": "专属规则：workout-planning",
    }
    assert "skill_examples" not in payload
    assert "plan-adjustment" not in deps.model.payloads[0]
    assert "fitness-knowledge" not in deps.model.payloads[0]
    assert "发现描述" not in deps.model.payloads[0]
    assert deps.skills.reference_reads == []

    evaluated = await _evaluator(deps).evaluator_agent(
        {
            **_planner_state(),
            "draft_plan": _draft(),
            "deterministic_result": DeterministicResult(passed=True, failures=()),
        },
        _Runtime(run),
    )
    rubric_payload = json.loads(deps.model.payloads[1])
    assert set(rubric_payload) == {
        "request",
        "plan",
        "business_day",
        "skill",
        "facts",
    }
    assert [fact["tool"] for fact in rubric_payload["facts"]] == list(GENERATE_FACTS)
    assert rubric_payload["skill"] == {
        "name": "plan-evaluation",
        "instructions": "专属指令：plan-evaluation",
        "rules_reference": "专属 reference：plan-evaluation:references/evaluation-rubric.md",
    }
    assert deps.skills.body_reads == ["plan-evaluation"]
    assert deps.skills.reference_reads == [
        ("plan-evaluation", "references/evaluation-rubric.md"),
    ]
    assert evaluated["evaluation"].evidence


async def test_candidate_actions_drop_the_actions_the_profile_forbids() -> None:
    """画像禁用 ID 来自 read_user_profile 的真实返回：禁用动作不进候选，仍留在 facts 里。"""
    profile = Profile(
        training_goal=Fact.known("增肌"),
        weekly_frequency=Fact.known(1),
        training_mode=Fact.known("bodyweight"),
        explicit_preferences=Fact.denied(),
        current_level=Fact.known("中级"),
        known_injuries=Fact.denied(),
        forbidden_exercise_ids=Fact.known(("pull-up",)),
    )
    deps = _Deps(
        catalog=_catalog(),
        stats=_Stats(),
        persistence=_Persistence(),
        profiles=_Profiles(profile),
        model=_Model(tool_loops=[GENERATE_FACTS]),
    )

    await _planner(deps).planner_agent(
        _planner_state(), _Runtime(GeneratePlanRun(BUSINESS_DAY))
    )

    payload = json.loads(deps.model.payloads[0])
    assert payload["candidate_actions"] == []
    assert [fact["tool"] for fact in payload["facts"]] == list(GENERATE_FACTS)
    assert payload["skill_examples"] == {
        "path": "references/few-shots.md",
        "text": "专属 reference：workout-planning:references/few-shots.md",
    }
    assert deps.skills.reference_reads == [
        ("workout-planning", "references/few-shots.md")
    ]


async def test_adjust_planner_and_evaluator_use_their_selected_skills() -> None:
    deps = _Deps(
        catalog=_catalog(),
        stats=_Stats(),
        persistence=_Persistence(),
        model=_Model(tool_loops=[ADJUST_FACTS, ADJUST_FACTS]),
    )
    state = _planner_state(intent="adjust_plan")
    run = GeneratePlanRun(
        BUSINESS_DAY,
        adjustment=AdjustmentContext(
            plan_id=1, active_draft=_draft(), linked_workout_session_ids=()
        ),
    )

    written = await _planner(deps).planner_agent(state, _runtime_context(run))
    planner_payload = json.loads(deps.model.payloads[0])
    assert planner_payload["skill"] == {
        "name": "plan-adjustment",
        "instructions": "专属指令：plan-adjustment",
        "rules_reference": "专属规则：plan-adjustment",
    }
    assert "plan-evaluation" not in deps.model.payloads[0]
    assert deps.skills.reference_reads == []

    await _evaluator(deps).evaluator_agent(
        {
            **state,
            **written,
            "deterministic_result": DeterministicResult(passed=True, failures=()),
        },
        _runtime_context(run),
    )
    evaluator_payload = json.loads(deps.model.payloads[1])

    assert evaluator_payload["skill"] == {
        "name": "plan-evaluation",
        "instructions": "专属指令：plan-evaluation",
        "rules_reference": "专属 reference：plan-evaluation:references/evaluation-rubric.md",
    }
    assert set(evaluator_payload) == {
        "request", "plan", "business_day", "skill", "facts"
    }
    assert "发现描述" not in deps.model.payloads[1]
    assert deps.skills.body_reads == ["plan-evaluation"]
    assert deps.skills.reference_reads == [
        ("plan-evaluation", "references/evaluation-rubric.md"),
    ]


def test_planner_few_shots_conditions_follow_current_candidate_and_revision_issue() -> None:
    candidates = {
        "candidate_actions": [
            {
                "record_type": "reps_weight",
                "starting_load": {"status": "needs_calibration"},
            }
        ]
    }
    assert _needs_planner_examples("generate_plan", {"candidate_actions": []})
    assert _needs_planner_examples("generate_plan", candidates)
    assert not _needs_planner_examples(
        "generate_plan",
        {
            "candidate_actions": [
                {
                    "record_type": "reps_bodyweight",
                    "starting_load": {"status": "needs_calibration"},
                }
            ]
        },
    )
    assert not _needs_planner_examples(
        "adjust_plan",
        {"progression_decisions": [{"decision": {"action": "keep"}}]},
    )
    assert _needs_planner_examples(
        "adjust_plan",
        {"progression_decisions": [{"decision": {"action": "needs_calibration"}}]},
    )
    assert not _needs_planner_examples(
        "adjust_plan",
        {"progression_decisions": [{"decision": {"action": "increase"}}]},
    )
    assert _needs_planner_examples(
        "generate_plan", {"revision": {"failures": ["unknown_exercise: 未收录"]}}
    )
    assert not _needs_planner_examples(
        "generate_plan", {"revision": {"failures": ["weekly_frequency_mismatch: 频率"]}}
    )
    assert _needs_planner_examples(
        "adjust_plan", {"revision": {"failures": ["load_source_mismatch: 负荷"]}}
    )
    assert not _needs_planner_examples(
        "adjust_plan", {"revision": {"failures": ["goal_alignment: 目标"]}}
    )


async def test_planner_reuses_selected_skill_for_the_single_revision() -> None:
    deps = _Deps(
        catalog=_catalog(),
        stats=_Stats(),
        persistence=_Persistence(),
        model=_Model(tool_loops=[GENERATE_FACTS, GENERATE_FACTS]),
    )
    run = GeneratePlanRun(BUSINESS_DAY)
    state = _planner_state()
    state.update(await load_skill(state, _runtime_context(run), skills=deps.skills))
    planner = _planner(deps)

    first = await planner.planner_agent(state, _runtime_context(run))
    revised = await planner.planner_agent(
        {
            **state,
            **first,
            "revision_count": 1,
            "revision_feedback": ("goal_alignment: 目标不匹配",),
        },
        _runtime_context(run),
    )

    assert deps.skills.metadata_reads == 1
    assert deps.skills.body_reads == ["workout-planning"]
    assert deps.skills.reference_reads == [
        ("workout-planning", "references/planning-rules.md")
    ]
    first_payload, revision_payload = map(json.loads, deps.model.payloads)
    assert first_payload["skill"] == revision_payload["skill"]
    assert "skill_examples" not in first_payload
    assert "skill_examples" not in revision_payload
    assert revision_payload["revision"]["failures"] == [
        "goal_alignment: 目标不匹配"
    ]
    assert "loaded_skill" not in revised
    assert run.budget.used == 2


async def test_planner_loads_few_shots_only_for_a_relevant_revision() -> None:
    profile = Profile(
        training_goal=Fact.known("增肌"),
        weekly_frequency=Fact.known(1),
        training_mode=Fact.known("bodyweight"),
        explicit_preferences=Fact.denied(),
        current_level=Fact.known("中级"),
        known_injuries=Fact.denied(),
        forbidden_exercise_ids=Fact.known(("pull-up",)),
    )
    deps = _Deps(
        catalog=_catalog(),
        stats=_Stats(),
        persistence=_Persistence(),
        profiles=_Profiles(profile),
        model=_Model(tool_loops=[GENERATE_FACTS, GENERATE_FACTS]),
    )
    run = GeneratePlanRun(BUSINESS_DAY)
    state = _planner_state()
    state.update(await load_skill(state, _runtime_context(run), skills=deps.skills))
    planner = _planner(deps)

    first = await planner.planner_agent(state, _runtime_context(run))
    revised = await planner.planner_agent(
        {
            **state,
            **first,
            "revision_count": 1,
            "revision_feedback": ("unknown_exercise: 动作不在候选中",),
        },
        _runtime_context(run),
    )
    first_payload, revision_payload = map(json.loads, deps.model.payloads)

    assert first_payload["candidate_actions"] == []
    assert first_payload["skill_examples"] == revision_payload["skill_examples"]
    assert deps.skills.reference_reads == [
        ("workout-planning", "references/planning-rules.md"),
        ("workout-planning", "references/few-shots.md"),
        ("workout-planning", "references/few-shots.md"),
    ]
    assert "loaded_skill" not in revised


async def test_evaluator_blocks_with_snapshot_mismatch_when_a_revision_moved() -> None:
    """候选读取后事实域 revision 变化：评审以 ``snapshot_mismatch`` 阻断，并自报读到的证据。"""
    deps = _Deps(
        catalog=_catalog(),
        stats=_Stats(),
        persistence=_Persistence(),
        model=_Model(tool_loops=[GENERATE_FACTS, GENERATE_FACTS]),
    )
    nodes = _planner(deps)
    run = GeneratePlanRun(BUSINESS_DAY)
    planner = _planner_state()

    written = await nodes.planner_agent(planner, _Runtime(run))
    deps.persistence.revisions.values["workouts"] = 3

    evaluated = await _evaluator(deps).evaluator_agent(
        {
            **planner,
            **written,
            "deterministic_result": DeterministicResult(passed=True, failures=()),
        },
        _Runtime(run),
    )

    evaluation: EvaluationResult = evaluated["evaluation"]
    assert evaluation.passed is False
    assert evaluation.deterministic.passed is False
    assert [failure.code for failure in evaluation.deterministic.failures]
    assert evaluation.blocking_failures
    assert all(
        failure.startswith("snapshot_mismatch:")
        for failure in evaluation.blocking_failures
    )
    assert evaluation.evidence != written["planner_evidence"]
    assert next(
        item.revision
        for item in evaluation.evidence
        if item.revision_domain == "workouts"
    ) == 3


async def test_adjust_plan_fast_fails_without_the_active_plan_and_calendar_facts() -> None:
    """调整场景缺 active 计划与日历事实即明确失败：缺项候选不进模型、不进评审。"""
    deps = _Deps(
        catalog=_catalog(),
        stats=_Stats(),
        persistence=_Persistence(),
        model=_Model(tool_loops=[GENERATE_FACTS]),
    )

    with pytest.raises(MissingPlanFacts) as failure:
        await _planner(deps).planner_agent(
            _planner_state(intent="adjust_plan"),
            _Runtime(GeneratePlanRun(BUSINESS_DAY)),
        )

    assert "read_active_plan" in str(failure.value)
    assert "read_training_calendar" in str(failure.value)
    assert deps.model.payloads == []


async def test_generate_plan_fast_fails_without_the_progress_fact() -> None:
    """生成场景缺进展事实即明确失败：必需事实按真实调用轨迹校验。"""
    deps = _Deps(
        catalog=_catalog(),
        stats=_Stats(),
        persistence=_Persistence(),
        model=_Model(
            tool_loops=[
                ("read_user_profile", "read_training_history", "search_exercises")
            ]
        ),
    )

    with pytest.raises(MissingPlanFacts) as failure:
        await _planner(deps).planner_agent(
            _planner_state(), _Runtime(GeneratePlanRun(BUSINESS_DAY))
        )

    assert "read_progress" in str(failure.value)


async def test_adjust_plan_loop_reads_all_six_required_facts() -> None:
    """调整场景的 loop 真实调用六项事实：候选证据列出全部六条真实调用。"""
    deps = _Deps(
        catalog=_catalog(),
        stats=_Stats(),
        persistence=_Persistence(),
        model=_Model(tool_loops=[ADJUST_FACTS]),
    )

    written = await _planner(deps).planner_agent(
        _planner_state(intent="adjust_plan"),
        _Runtime(
            GeneratePlanRun(
                BUSINESS_DAY,
                adjustment=AdjustmentContext(
                    plan_id=1,
                    active_draft=_draft(),
                    linked_workout_session_ids=(),
                ),
            )
        ),
    )

    evidence: tuple[ToolEvidence, ...] = written["planner_evidence"]
    assert tuple(dict.fromkeys(item.tool_name for item in evidence)) == ADJUST_FACTS


async def test_candidate_outside_the_real_search_results_fails() -> None:
    """真实 search_exercises 没有返回候选动作的 id：越界候选明确失败，不进评审。"""
    deps = _Deps(
        catalog=_Catalog((_plank(),)),
        stats=_Stats(),
        persistence=_Persistence(),
        model=_Model(tool_loops=[GENERATE_FACTS]),
    )

    with pytest.raises(UnregisteredCandidateExercise) as failure:
        await _planner(deps).planner_agent(
            _planner_state(), _Runtime(GeneratePlanRun(BUSINESS_DAY))
        )

    assert "pull-up" in str(failure.value)


def test_planning_and_evaluation_whitelists_stay_independent() -> None:
    """两个 ToolNode 共用同一只读 Registry 与同一白名单元组，节点名与调用轨迹各自独立。"""
    harnesses = build_plan_tool_harnesses(timeout_seconds=TOOL_TIMEOUT_SECONDS)
    planning_nodes = harnesses.planning.get_graph().nodes
    evaluation_nodes = harnesses.evaluation.get_graph().nodes

    assert set(PLAN_REQUIRED_FACTS["generate_plan"]) < set(
        PLAN_REQUIRED_FACTS["adjust_plan"]
    )
    assert {tool.name for tool in PLANNING_TOOLS} >= set(
        PLAN_REQUIRED_FACTS["adjust_plan"]
    )
    assert PLANNING_TOOLS_NODE_NAME in planning_nodes
    assert EVALUATION_TOOLS_NODE_NAME in evaluation_nodes
    assert PLANNING_TOOLS_NODE_NAME not in evaluation_nodes
    assert EVALUATION_TOOLS_NODE_NAME not in planning_nodes


def test_confirmation_resume_requires_the_waiting_run_and_plan_identity() -> None:
    """恢复载荷必须同时对上等待中的来源 Run 与业务目标；任一不匹配即快速失败。"""
    assert _require_confirmation_action(
        {"action": "confirm", "run_id": "run-1", "plan_id": 7},
        expected_plan_id=7,
        expected_run_id="run-1",
    ) == "confirm"

    for resume in (
        {"action": "confirm", "run_id": "run-2", "plan_id": 7},
        {"action": "confirm", "run_id": "run-1", "plan_id": 8},
        {"action": "confirm", "plan_id": 7},
        {"action": "archive", "run_id": "run-1", "plan_id": 7},
    ):
        with pytest.raises(ConfirmationConflict):
            _require_confirmation_action(
                resume, expected_plan_id=7, expected_run_id="run-1"
            )
