"""生成计划子图的节点与一次 Run 的运行上下文。

节点按职责持有各自的强类型依赖边界：Planner 持有本次 Skill 的只读来源与 ``planning_tools`` 事实边界，Evaluator
持有固定 SkillSource 与 ``evaluation_tools`` 事实边界；两者的业务事实均来自真实工具调用。确定性节点只拿领域规则需要的只读端口；写入节点只拿业务写服务。
"""

from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import date
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from app.application.agent.budget import ModelRequestBudget, request_structured_model
from app.application.agent.contracts import (
    ADJUST_PLAN_INTENT,
    ADJUSTMENT_SKILL_NAME,
    CONFIRMATION_ACTIONS,
    PLAN_CONFIRMATION_KIND,
    PLAN_EVALUATION_SKILL_NAME,
    PLANNING_SKILL_NAME,
    AdjustmentContext,
    AgentRunResult,
    ConfirmationConflict,
    GeneratePlanRun,
    LoadedSkill,
    PlanDeterministicDeps,
    PlanLlmNodeDeps,
    PlanWriteDeps,
    RequiredActivePlanMissing,
    RequiredProfileMissing,
    TerminationReason,
    ToolEvidence,
    ToolExecutionContext,
    WorkflowState,
)
from app.application.agent.harness.registry import (
    require_canonical_candidates,
    run_plan_fact_loop,
)
from app.application.agent.harness.snapshot import (
    PLAN_REQUIRED_FACTS,
    MissingPlanFacts,
    plan_fact_evidence,
    run_fact_snapshot,
)
from app.application.agent.harness.tools.common import (
    ToolCallRecord,
    TrainingHarnessContext,
)
from app.application.agent.prompts import (
    ADJUSTMENT_PLANNER_SYSTEM_PROMPT,
    DISCARD_FAILED_CANDIDATE_MESSAGE,
    EVALUATOR_SYSTEM_PROMPT,
    PLAN_FACTS_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT,
)
from app.application.ports import Plans, SkillSource, Stats
from app.domain.conversations.context import history_payload
from app.domain.plans.rules import (
    active_load_targets,
    known_forbidden_exercise_ids,
    resolve_progression,
    resolve_starting_load,
    snapshot_mismatch_failures,
    validate_plan_adjustment,
    validate_plan_draft,
)
from app.domain.plans.schema import (
    DeterministicResult,
    EvaluationResult,
    PlanDraft,
    RubricResult,
    RuleFailure,
)
from app.domain.profile.schema import Profile
from config import MAX_PLAN_TOOL_CALLS_PER_LOOP

SEARCH_EXERCISES_TOOL = "search_exercises"

READ_USER_PROFILE_TOOL = "read_user_profile"

READ_ACTIVE_PLAN_TOOL = "read_active_plan"

_PLANNER_SKILLS = {
    "generate_plan": (PLANNING_SKILL_NAME, "references/planning-rules.md"),
    ADJUST_PLAN_INTENT: (ADJUSTMENT_SKILL_NAME, "references/adjustment-rules.md"),
}
_SKILL_EXAMPLES_PATH = "references/few-shots.md"
_EVALUATION_RULES_PATH = "references/evaluation-rubric.md"


def require_profile(profile: Profile | None) -> Profile:
    """画像未建档即明确失败。"""
    if profile is None:
        raise RequiredProfileMissing(
            "画像未建档：请先补充每周训练次数等必需事实后再生成计划"
        )
    return profile


def require_weekly_frequency(profile: Profile) -> int:
    """画像明确给出的每周训练次数；``unknown``／``denied`` 即 Planner 前明确失败。"""
    fact = profile.weekly_frequency
    if not fact.is_known or fact.value is None:
        raise RequiredProfileMissing(
            f"画像缺少每周训练次数（weekly_frequency={fact.state}）："
            "请补充每周训练次数后再生成计划"
        )
    return fact.value


async def load_skill(
    state: WorkflowState,
    runtime: Runtime[GeneratePlanRun],
    *,
    skills: SkillSource,
) -> WorkflowState:
    """按计划 Intent 读取唯一 Planner Skill 与必需规则 reference。"""
    intent = state.get("intent")
    name, rules_path = _planner_skill_spec(intent)
    metadata = {item.name: item for item in skills.list_metadata()}.get(name)
    if metadata is None:
        raise ValueError(f"Skill 元数据中缺少 Planner Skill：{name}")
    planner_skill = LoadedSkill(
        metadata=metadata,
        body=skills.read_skill(name),
        references=(skills.read_reference(name, rules_path),),
    )
    return {"loaded_skill": planner_skill}


def _planner_skill_spec(intent: str | None) -> tuple[str, str]:
    selected = _PLANNER_SKILLS.get(intent or "")
    if selected is None:
        raise ValueError(f"未登记的 Planner Intent：{intent!r}")
    return selected


class PlannerAgentNode:
    """Planner LLM Node：结构化模型入口、统一只读 SkillSource 与 ``planning_tools`` 事实边界。"""

    def __init__(self, deps: PlanLlmNodeDeps, *, skills: SkillSource) -> None:
        self._deps = deps
        self._skills = skills

    async def planner_agent(
        self, state: WorkflowState, runtime: Runtime[GeneratePlanRun]
    ) -> WorkflowState:
        """候选：Planner 先经 ``planning_tools`` ToolNode 读事实，再据此产出候选。

        ``revision_count == 0`` 产出首版候选，``revision_count == 1`` 依据上一版候选与结构化失败理由
        产出唯一一次修订；不存在第三次生成。事实来自本次真实完成的工具调用，候选动作只能取自其中
        ``search_exercises`` 返回的 canonical ``exercise_id``。
        """
        run = _run(runtime)
        intent = state.get("intent")
        snapshot, facts = await _read_plan_facts(self._deps, state=state, run=run)
        evidence = plan_fact_evidence(intent, snapshot=snapshot, tool_calls=facts)
        payload = await _planner_payload(state, run, facts=facts, deps=self._deps)
        skill_name, _ = _planner_skill_spec(intent)
        revision_count = state.get("revision_count") or 0
        if revision_count:
            previous_plan = state.get("draft_plan")
            if previous_plan is None:
                raise ValueError("Planner 修订状态缺少上一版计划")
            payload["revision"] = {
                "previous_plan": previous_plan.model_dump(mode="json"),
                "failures": list(state.get("revision_feedback") or ()),
            }
        if _needs_planner_examples(intent, payload):
            example = self._skills.read_reference(skill_name, _SKILL_EXAMPLES_PATH)
            payload["skill_examples"] = {
                "path": example.path,
                "text": example.text,
            }
        draft = await request_structured_model(
            self._deps.model,
            _planner_system_prompt(intent),
            payload,
            run.budget,
            PlanDraft,
        )
        require_canonical_candidates(draft, tool_calls=facts)
        return {
            "draft_plan": draft,
            "revision_count": revision_count,
            "planner_evidence": evidence,
        }


class EvaluatorAgentNode:
    """Evaluator LLM Node：统一只读 SkillSource、唯一模型入口与 ``evaluation_tools`` 事实边界。"""

    def __init__(self, deps: PlanLlmNodeDeps, *, skills: SkillSource) -> None:
        self._deps = deps
        self._skills = skills

    async def evaluator_agent(
        self, state: WorkflowState, runtime: Runtime[GeneratePlanRun]
    ) -> WorkflowState:
        """评估：确定性层已经通过，本节点用 ``evaluation_tools`` 独立重读同一批事实，再跑模型 Rubric。

        两个 ToolNode 的预算与调用轨迹彼此独立；评审读到的 revision 与候选证据不一致时以
        ``snapshot_mismatch`` 阻断。
        """
        run = _run(runtime)
        deterministic: DeterministicResult = state["deterministic_result"]
        snapshot, facts = await _read_plan_facts(self._deps, state=state, run=run)
        evidence = plan_fact_evidence(
            state.get("intent"), snapshot=snapshot, tool_calls=facts
        )
        metadata = next(
            (
                item
                for item in self._skills.list_metadata()
                if item.name == PLAN_EVALUATION_SKILL_NAME
            ),
            None,
        )
        if metadata is None:
            raise ValueError(
                f"Skill 元数据中缺少 Evaluator Skill：{PLAN_EVALUATION_SKILL_NAME}"
            )
        skill = {
            "name": metadata.name,
            "instructions": self._skills.read_skill(metadata.name),
            "rules_reference": self._skills.read_reference(
                metadata.name, _EVALUATION_RULES_PATH
            ).text,
        }
        rubric = await request_structured_model(
            self._deps.model,
            EVALUATOR_SYSTEM_PROMPT,
            _evaluator_payload(state, run, facts=facts, skill=skill),
            run.budget,
            RubricResult,
        )
        return {
            "evaluation": _evaluation_result(
                deterministic,
                rubric,
                state.get("revision_count") or 0,
                candidate=state.get("planner_evidence") or (),
                evaluation=evidence,
            )
        }


class PlanDeterministicNodes:
    """确定性节点与有界修订：只持有领域规则需要的只读边界；不调模型、不写业务表。"""

    def __init__(self, deps: PlanDeterministicDeps) -> None:
        self._deps = deps

    async def validate_required_profile(self, state: WorkflowState) -> WorkflowState:
        """Planner 前的画像前提：``weekly_frequency`` 不是明确值即明确失败（stage4.md §3.2）。"""
        require_weekly_frequency(require_profile(await self._deps.profiles.read()))
        return {}

    async def require_active_plan(
        self, state: WorkflowState, runtime: Runtime[GeneratePlanRun]
    ) -> WorkflowState:
        """调整计划的 Planner 前前提：预读当前 active 并把身份写进运行上下文。"""
        _run(runtime).adjustment = await _read_active_plan(
            self._deps.plans, self._deps.stats
        )
        return {}

    async def validate_plan(
        self, state: WorkflowState, runtime: Runtime[GeneratePlanRun]
    ) -> WorkflowState:
        """确定性校验：Schema 已由 ``PlanDraft`` 承担，这里全量返回领域规则失败，不调用模型。"""
        profile = require_profile(await self._deps.profiles.read())
        failures = await self._deterministic_failures(
            state["draft_plan"], profile, _run(runtime).adjustment
        )
        return {
            "deterministic_result": DeterministicResult(
                passed=not failures, failures=failures
            )
        }

    async def revise_once(
        self, state: WorkflowState, runtime: Runtime[GeneratePlanRun]
    ) -> WorkflowState:
        """把结构化失败理由与 ``revision_count=1`` 交回 Planner；不存在第二次修订。"""
        if state.get("revision_count"):
            raise ValueError(
                f"修订只允许一次：revision_count={state.get('revision_count')!r}"
            )
        return {
            "revision_count": 1,
            "revision_feedback": _blocking_failures(state),
        }

    async def discard_failed_candidate(self, state: WorkflowState) -> WorkflowState:
        """二次阻断失败：清除可持久化候选引用后写内存终态，``plans``／``plan_sessions`` 保持不变。"""
        termination_reason: TerminationReason = "discard_failed_candidate"
        return {
            "draft_plan_id": None,
            "termination_reason": termination_reason,
            "final_result": AgentRunResult(
                intent=state.get("intent"),
                messages=(DISCARD_FAILED_CANDIDATE_MESSAGE,),
                termination_reason=termination_reason,
                draft_plan_id=None,
            ),
        }

    async def _deterministic_failures(
        self,
        draft: PlanDraft,
        profile: Profile,
        adjustment: AdjustmentContext | None,
    ) -> tuple[RuleFailure, ...]:
        """确定性层：生成计划用最近工作组规则，调整计划用当前 active 的渐进决策。"""
        catalog = await self._deps.catalog.list_all()
        common = {
            "exercises": {exercise.id: exercise for exercise in catalog},
            "profile_weekly_frequency": require_weekly_frequency(profile),
            "forbidden_exercise_ids": known_forbidden_exercise_ids(profile),
            "training_mode": (
                profile.training_mode.value
                if profile.training_mode.is_known
                else None
            ),
            "work_sets": await self._deps.stats.list_valid_work_sets(),
        }
        if adjustment is None:
            return validate_plan_draft(draft, **common)
        return validate_plan_adjustment(
            draft,
            active_draft=adjustment.active_draft,
            linked_workout_session_ids=adjustment.linked_workout_session_ids,
            **common,
        )


class PlanWriteNodes:
    """计划写入节点：只委托业务写服务；本节点不自己写 ``plans``／``plan_sessions``。"""

    def __init__(self, deps: PlanWriteDeps) -> None:
        self._deps = deps

    async def persist_draft(
        self, state: WorkflowState, runtime: Runtime[GeneratePlanRun]
    ) -> WorkflowState:
        """通过路径：先提交业务 draft，再写入 State 的 ``draft_plan_id`` 并进入等待。"""
        draft: PlanDraft = state["draft_plan"]
        evaluation: EvaluationResult | None = state.get("evaluation")
        if evaluation is None:
            raise ValueError("通过路径缺少本轮评估结果")
        adjustment = _run(runtime).adjustment
        written = await self._deps.plans.persist_draft(
            draft,
            evaluation,
            existing_draft_id=state.get("draft_plan_id"),
            created_at=self._created_at(),
            source_plan_id=None if adjustment is None else adjustment.plan_id,
        )
        return {
            "draft_plan_id": written.id,
            "draft_plan": draft,
            "evaluation": evaluation,
            "confirmation": "pending",
        }

    async def wait_for_confirmation(self, state: WorkflowState) -> WorkflowState:
        """确认等待：interrupt 载荷只携带稳定身份（kind／run_id／draft_plan_id）；恢复时按 resume 载荷校验身份并写下用户动作。"""
        plan_id = state["draft_plan_id"]
        action = _require_confirmation_action(
            interrupt(
                {
                    "kind": PLAN_CONFIRMATION_KIND,
                    "run_id": state["run_id"],
                    "draft_plan_id": plan_id,
                }
            ),
            expected_plan_id=plan_id,
            expected_run_id=state["run_id"],
        )
        return {"confirmation": "confirmed" if action == "confirm" else "rejected"}

    async def activate_plan(
        self, state: WorkflowState, runtime: Runtime[GeneratePlanRun]
    ) -> WorkflowState:
        """用户确认：把激活事务交给领域服务，本节点不自己写 ``plans``／``plan_sessions``。"""
        confirmed_at = self._created_at()
        await self._deps.plans.activate_plan(
            state["draft_plan_id"],
            business_day=_run(runtime).business_day,
            confirmed_at=confirmed_at,
            archived_at=confirmed_at,
        )
        return {"confirmation": "confirmed"}

    async def archive_draft(self, state: WorkflowState) -> WorkflowState:
        """用户拒绝：``draft -> archived``（原 active 不变）。"""
        await self._deps.plans.archive_draft(
            state["draft_plan_id"], archived_at=self._created_at()
        )
        return {"confirmation": "rejected", "termination_reason": "archive_draft"}

    def _created_at(self) -> str:
        """业务记录时间由注入时钟给出（节点不读系统时钟）。"""
        return self._deps.now().isoformat()


# ---------- 内部：事实读取、载荷、确定性校验与时钟 ----------


async def _read_plan_facts(
    deps: PlanLlmNodeDeps, *, state: WorkflowState, run: GeneratePlanRun
) -> tuple[ToolExecutionContext, tuple[ToolCallRecord, ...]]:
    """一次计划事实的 ToolNode loop：白名单在装配期固化，模型的工具调用进入真实轨迹。"""
    intent = state.get("intent")
    required = PLAN_REQUIRED_FACTS.get(intent or "")
    if required is None:
        raise ValueError(f"未登记的计划 Intent：{intent!r}")
    snapshot = await run_fact_snapshot(
        deps.revisions,
        schema_version=deps.schema_version,
        user_id=state["user_id"],
        run_id=state["run_id"],
        business_day=run.business_day,
    )
    facts = await run_plan_fact_loop(
        deps.harness,
        context=TrainingHarnessContext(
            model=deps.model,
            budget=_plan_loop_budget(run),
            business_day=run.business_day,
            profiles=deps.profiles,
            plans=deps.plans,
            catalog=deps.catalog,
            records=deps.records,
            stats=deps.stats,
            dataset=deps.dataset,
            snapshot=snapshot,
        ),
        messages=_fact_loop_messages(
            state["request"], business_day=run.business_day, required=required
        ),
    )
    return snapshot, facts


async def _planner_payload(
    state: WorkflowState,
    run: GeneratePlanRun,
    *,
    facts: Sequence[ToolCallRecord],
    deps: PlanLlmNodeDeps,
) -> dict[str, Any]:
    """Planner 输入：选定 Skill 的指令与规则、本次真实工具事实、候选动作及请求上下文。"""
    intent = state.get("intent")
    skill_name, rules_path = _planner_skill_spec(intent)
    skill = state.get("loaded_skill")
    if skill is None:
        raise ValueError("Planner 状态缺少已装载 Skill")
    if skill.metadata.name != skill_name:
        raise ValueError(f"Planner Skill 与 Intent 不一致：{skill.metadata.name!r}")
    rules = next(
        (reference for reference in skill.references if reference.path == rules_path),
        None,
    )
    if rules is None:
        raise ValueError(f"Planner Skill 缺少必需规则 reference：{rules_path}")
    payload: dict[str, Any] = {
        "request": state["request"],
        "intent": intent,
        "business_day": run.business_day.isoformat(),
        "skill": {
            "name": skill.metadata.name,
            "instructions": skill.body,
            "rules_reference": rules.text,
        },
        "facts": _fact_payloads(facts),
        "candidate_actions": await _candidate_actions(facts, deps),
        # 无历史时不出现该键：与 Router／Evaluator 的载荷形状一致。
        **history_payload(run.conversation_messages),
    }
    if state.get("intent") == ADJUST_PLAN_INTENT:
        adjustment = run.adjustment
        if adjustment is None:
            raise RequiredActivePlanMissing(
                "调整计划缺少预读的 active 计划上下文"
            )
        active = dict(_tool_result(facts, READ_ACTIVE_PLAN_TOOL)["active_plan"])
        active["explanation"] = adjustment.active_draft.explanation
        payload["active_plan"] = active
        payload["progression_decisions"] = await _progression_decisions(
            adjustment, deps
        )
    return payload


def _needs_planner_examples(intent: str | None, payload: Mapping[str, Any]) -> bool:
    """空候选／待校准负荷触发示例；修订按相关失败码触发。"""
    if "revision" in payload:
        failures = payload["revision"]["failures"]
        codes = {failure.partition(":")[0] for failure in failures}
        return "load_source_mismatch" in codes or (
            intent == "generate_plan" and "unknown_exercise" in codes
        )
    if intent == "generate_plan":
        candidates = payload["candidate_actions"]
        return not candidates or any(
            item["record_type"] == "reps_weight"
            and item["starting_load"]["status"] == "needs_calibration"
            for item in candidates
        )
    if intent == ADJUST_PLAN_INTENT:
        return any(
            item["decision"]["action"] == "needs_calibration"
            for item in payload["progression_decisions"]
        )
    raise ValueError(f"未登记的 Planner Intent：{intent!r}")


def _evaluator_payload(
    state: WorkflowState,
    run: GeneratePlanRun,
    *,
    facts: Sequence[ToolCallRecord],
    skill: dict[str, str],
) -> dict[str, Any]:
    """Rubric 输入：固定 Skill 的指令与规则、本次工具事实及请求历史。"""
    return {
        "request": state["request"],
        "plan": state["draft_plan"].model_dump(mode="json"),
        "business_day": run.business_day.isoformat(),
        "skill": skill,
        "facts": _fact_payloads(facts),
        # 无历史时不出现该键：与 Router／Planner 的载荷形状一致。
        **history_payload(run.conversation_messages),
    }


async def _progression_decisions(
    adjustment: AdjustmentContext, deps: PlanLlmNodeDeps
) -> list[dict[str, Any]]:
    """调整计划的渐进决策：active 目标、目录增量与关联工作组确定性归约。"""
    exercises = {
        exercise.id: exercise for exercise in await deps.catalog.list_all()
    }
    targets = [
        (exercise_id, target, exercise.min_load_increment_kg)
        for exercise_id, target in active_load_targets(
            adjustment.active_draft
        ).items()
        if (exercise := exercises.get(exercise_id)) is not None
        and exercise.min_load_increment_kg is not None
    ]
    work_sets = await deps.stats.list_valid_work_sets()
    return [
        {
            "exercise_id": exercise_id,
            **asdict(target),
            "decision": asdict(
                resolve_progression(
                    work_sets,
                    linked_workout_session_ids=adjustment.linked_workout_session_ids,
                    target_sets=target.sets,
                    reps_min=target.reps_min,
                    reps_max=target.reps_max,
                    target_load_kg=target.target_load_kg,
                    increment_kg=increment_kg,
                )
            ),
        }
        for exercise_id, target, increment_kg in targets
    ]


async def _candidate_actions(
    facts: Sequence[ToolCallRecord], deps: PlanLlmNodeDeps
) -> list[dict[str, Any]]:
    """候选动作：检索决定身份，目录与工作组确定性补齐计划负荷事实。"""
    forbidden = _forbidden_exercise_ids(facts)
    profile = _tool_result(facts, READ_USER_PROFILE_TOOL)["profile"]
    training_mode = None
    if profile is not None and profile["training_mode"]["state"] == "known":
        training_mode = profile["training_mode"]["value"]
    catalog = {exercise.id: exercise for exercise in await deps.catalog.list_all()}
    work_sets = await deps.stats.list_valid_work_sets()
    candidates: list[dict[str, Any]] = []
    for record in facts:
        if record.tool_name != SEARCH_EXERCISES_TOOL:
            continue
        for item in record.payload["exercises"]:
            exercise_id = item["exercise_id"]
            if not item["recommendable"] or exercise_id in forbidden:
                continue
            exercise = catalog[exercise_id]
            if training_mode == "bodyweight" and exercise.equipment_variant != "bodyweight":
                continue
            candidates.append(
                {
                    "exercise_id": exercise_id,
                    "standard_name_zh": item["standard_name_zh"],
                    "record_type": item["record_type"],
                    "load_convention": item["load_convention"],
                    "min_load_increment_kg": exercise.min_load_increment_kg,
                    "starting_load": resolve_starting_load(
                        work_sets, exercise_id=exercise_id
                    ).model_dump(mode="json"),
                }
            )
    return candidates


def _forbidden_exercise_ids(facts: Sequence[ToolCallRecord]) -> frozenset[str]:
    """本次 ``read_user_profile`` 真实返回的画像里明确禁用的动作 ID。"""
    profile = _tool_result(facts, READ_USER_PROFILE_TOOL)["profile"]
    if profile is None:
        return frozenset()
    fact = profile["forbidden_exercise_ids"]
    return frozenset(fact["value"] or ()) if fact["state"] == "known" else frozenset()


def _tool_result(facts: Sequence[ToolCallRecord], tool_name: str) -> Any:
    """本次真实调用轨迹里指定工具的返回载荷；必需事实已在证据校验里保证它存在。"""
    for record in facts:
        if record.tool_name == tool_name:
            return record.payload
    raise MissingPlanFacts(f"本次工具轨迹里没有 {tool_name} 的返回")


def _require_confirmation_action(
    resume: Any, *, expected_plan_id: int | None, expected_run_id: str
) -> str:
    """校验确认 resume 载荷并返回用户动作；形状、来源 Run 或业务目标不符即明确冲突。"""
    if not isinstance(resume, Mapping):
        raise ConfirmationConflict(f"确认 resume 载荷必须是对象：{type(resume).__name__}")
    if set(resume) != {"action", "run_id", "plan_id"}:
        raise ConfirmationConflict(
            f"确认 resume 载荷只能携带 action、run_id 与 plan_id：{sorted(resume)}"
        )
    action = resume["action"]
    if action not in CONFIRMATION_ACTIONS:
        raise ConfirmationConflict(f"确认 resume 载荷的动作非法：{action!r}")
    if resume["run_id"] != expected_run_id:
        raise ConfirmationConflict(
            "确认请求的来源 Run 与等待确认的 Run 不一致："
            f"{resume['run_id']!r} != {expected_run_id!r}"
        )
    if resume["plan_id"] != expected_plan_id:
        raise ConfirmationConflict(
            "请求 plan_id 与 interrupt 的 draft_plan_id 不一致："
            f"{resume['plan_id']!r} != {expected_plan_id!r}"
        )
    return action


def _run(runtime: Runtime[GeneratePlanRun]) -> GeneratePlanRun:
    """取回本次 invocation 的运行上下文；缺失即调用方错误（业务日期与模型请求预算都在里面）。"""
    run = runtime.context
    if run is None:
        raise RuntimeError(
            "生成计划子图必须传入运行上下文 GeneratePlanRun（业务日期与模型请求预算）"
        )
    return run


async def _read_active_plan(plans: Plans, stats: Stats) -> AdjustmentContext:
    """预读调整计划的目标：当前 active 必须存在，缺失即明确失败。"""
    active = await plans.read_active()
    if active is None:
        raise RequiredActivePlanMissing(
            "没有 active 计划：调整计划前请先生成并确认一份计划"
        )
    session_ids = {session.id for session in await plans.list_sessions(active.id)}
    return AdjustmentContext(
        plan_id=active.id,
        active_draft=PlanDraft.model_validate(active.structured_content),
        linked_workout_session_ids=tuple(
            fact.workout_session_id
            for fact in await stats.list_linked_workouts()
            if fact.plan_session_id in session_ids
        ),
    )


def _planner_system_prompt(intent: str | None) -> str:
    """Planner 的系统提示词按意图选择：生成计划照抄起始负荷，调整计划按渐进决策给出负荷。"""
    return (
        ADJUSTMENT_PLANNER_SYSTEM_PROMPT
        if intent == ADJUST_PLAN_INTENT
        else PLANNER_SYSTEM_PROMPT
    )


def _blocking_failures(state: WorkflowState) -> tuple[str, ...]:
    """本次阻断失败理由统一口径：本轮结构校验失败优先，否则用本轮评估的阻断列表。"""
    deterministic = state.get("deterministic_result")
    if deterministic is not None and not deterministic.passed:
        return tuple(
            f"{failure.code}: {failure.message}" for failure in deterministic.failures
        )
    evaluation = state.get("evaluation")
    if evaluation is not None:
        return tuple(evaluation.blocking_failures)
    raise ValueError("阻断失败理由缺失：本轮既没有结构校验失败也没有评估结果")


def _fact_loop_messages(
    request: str, *, business_day: date, required: Sequence[str]
) -> list[BaseMessage]:
    """事实采集的消息序列：业务日与本次 Intent 的必需工具写进 system，当前请求只出现一次。"""
    return [
        SystemMessage(
            content=PLAN_FACTS_SYSTEM_PROMPT.format(
                business_day=business_day.isoformat(),
                required_facts="、".join(required),
            )
        ),
        HumanMessage(content=request),
    ]


def _fact_payloads(facts: Sequence[ToolCallRecord]) -> list[dict[str, Any]]:
    """工具事实的模型载荷：按真实调用顺序逐条给出工具名与它返回的结果。"""
    return [{"tool": record.tool_name, "result": record.payload} for record in facts]


def _plan_loop_budget(run: GeneratePlanRun) -> ModelRequestBudget:
    """本次 ToolNode loop 的独立预算：次数与工具调用各自计，Run 剩余时限作为该 loop 的时限。"""
    return ModelRequestBudget(
        run_timeout_seconds=run.budget.remaining_run_seconds(),
        max_tool_calls=MAX_PLAN_TOOL_CALLS_PER_LOOP,
    )


def _evaluation_result(
    deterministic: DeterministicResult,
    rubric: RubricResult,
    revision_count: int,
    *,
    candidate: tuple[ToolEvidence, ...],
    evaluation: tuple[ToolEvidence, ...],
) -> EvaluationResult:
    """合成 Evaluator 结果：快照不一致与两个硬门槛同为阻断，解释质量只进 ``warnings``。"""
    mismatches = snapshot_mismatch_failures(candidate, evaluation)
    if mismatches:
        deterministic = DeterministicResult(
            passed=False, failures=(*deterministic.failures, *mismatches)
        )
    blocking = [
        f"{failure.code}: {failure.message}" for failure in deterministic.failures
    ]
    warnings: list[str] = []
    if not rubric.goal_alignment.passed:
        blocking.append(f"goal_alignment: {rubric.goal_alignment.reason}")
    if not rubric.schedule_reasonableness.passed:
        blocking.append(
            f"schedule_reasonableness: {rubric.schedule_reasonableness.reason}"
        )
    if not rubric.explanation_quality.passed:
        warnings.append(f"explanation_quality: {rubric.explanation_quality.reason}")
    return EvaluationResult(
        passed=(
            deterministic.passed
            and rubric.goal_alignment.passed
            and rubric.schedule_reasonableness.passed
        ),
        deterministic=deterministic,
        rubric=rubric,
        blocking_failures=tuple(blocking),
        warnings=tuple(warnings),
        revision_count=revision_count,
        evidence=evaluation,
    )
