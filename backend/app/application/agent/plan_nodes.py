"""生成计划子图的节点与一次 Run 的运行上下文。"""

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from langgraph.runtime import Runtime
from langgraph.types import interrupt

from app.application.agent.budget import request_structured_model
from app.application.agent.contracts import (
    ADJUSTMENT_SKILL_NAME,
    CONFIRMATION_ACTIONS,
    PLANNING_SKILL_NAME,
    AdjustmentContext,
    ConfirmationConflict,
    GeneratePlanDeps,
    GeneratePlanRun,
    LoadedSkill,
    RequiredActivePlanMissing,
    RequiredProfileMissing,
    WorkflowState,
)
from app.application.agent.memory import MemoryContext
from app.application.agent.prompts import (
    ADJUSTMENT_PLANNER_SYSTEM_PROMPT,
    EVALUATOR_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT,
)
from app.application.ports import Plans, Stats
from app.domain.conversations.context import history_payload
from app.domain.plans.rules import (
    active_load_targets,
    filter_forbidden_exercises,
    known_forbidden_exercise_ids,
    resolve_progression,
    resolve_starting_load,
    validate_plan_adjustment,
    validate_plan_draft,
)
from app.domain.plans.schema import (
    DeterministicResult,
    EvaluationResult,
    PlanDraft,
    RubricResult,
    RubricVerdict,
    RuleFailure,
)
from app.domain.profile.safety import message_red_flag_hits
from app.domain.profile.schema import Profile


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


class GeneratePlanNodes:
    def __init__(self, deps: GeneratePlanDeps) -> None:
        self._deps = deps

    async def safety_check(self, state: WorkflowState) -> WorkflowState:
        """入口节点：对当前请求做封闭词表精确子串扫描，命中即写入终止原因。"""
        if message_red_flag_hits(state["request"]):
            return {"termination_reason": "safety_stop"}
        return {"termination_reason": None}

    async def safety_stop(self, state: WorkflowState) -> WorkflowState:
        """安全终止分支：不生成计划、不写业务记录、不调用模型（面向用户的提示文本由传输层给出）。"""
        return {"termination_reason": "safety_stop"}

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

    async def load_context(
        self, state: WorkflowState, runtime: Runtime[GeneratePlanRun]
    ) -> WorkflowState:
        """固定范围装配：生成计划传 ``exercise_ids=None``，调整计划只装配当前 active 涉及的稳定 ``exercise_id``。"""
        context = await self._deps.assembler.assemble(
            state["request"],
            business_day=_run(runtime).business_day,
            exercise_ids=_active_exercise_ids(_run(runtime).adjustment),
            conversation_messages=_run(runtime).conversation_messages,
        )
        return {"context": context}

    async def load_skill(
        self, state: WorkflowState, runtime: Runtime[GeneratePlanRun]
    ) -> WorkflowState:
        """命中后才加载正文：只加载本次 intent 命中的那一个。"""
        name = (
            PLANNING_SKILL_NAME
            if _run(runtime).adjustment is None
            else ADJUSTMENT_SKILL_NAME
        )
        return {"loaded_skill": self._deps.skills.load(name)}

    async def planner(
        self, state: WorkflowState, runtime: Runtime[GeneratePlanRun]
    ) -> WorkflowState:
        """首个候选：Planner 只接收六类上下文、已加载 Skill 与确定性候选动作。"""
        adjustment = _run(runtime).adjustment
        payload = await self._planner_payload(state, adjustment)
        draft = await request_structured_model(
            self._deps.model,
            _planner_system_prompt(adjustment),
            payload,
            _run(runtime).budget,
            PlanDraft,
        )
        return {"draft_plan": draft, "revision_count": 0}

    async def evaluator(
        self, state: WorkflowState, runtime: Runtime[GeneratePlanRun]
    ) -> WorkflowState:
        """分层评估：确定性领域校验先跑，只有通过才调用模型 Rubric。"""
        context: MemoryContext = state["context"]
        draft: PlanDraft = state["draft_plan"]
        revision_count = state.get("revision_count") or 0
        profile = require_profile(context.profile)
        adjustment = _run(runtime).adjustment
        failures = await self._deterministic_failures(draft, profile, adjustment)
        deterministic = DeterministicResult(passed=not failures, failures=failures)
        rubric_ran = deterministic.passed
        rubric = (
            await request_structured_model(
                self._deps.model,
                EVALUATOR_SYSTEM_PROMPT,
                self._evaluator_payload(context, draft),
                _run(runtime).budget,
                RubricResult,
            )
            if rubric_ran
            else _rubric_not_run()
        )
        return {
            "evaluation": _evaluation_result(
                deterministic, rubric, revision_count, rubric_ran=rubric_ran
            )
        }

    async def revise_once(
        self, state: WorkflowState, runtime: Runtime[GeneratePlanRun]
    ) -> WorkflowState:
        """把结构化阻断理由交回 Planner 修订一次；``revision_count`` 置 1。"""
        evaluation: EvaluationResult = state["evaluation"]
        adjustment = _run(runtime).adjustment
        payload = await self._planner_payload(state, adjustment)
        payload["revision"] = {
            "previous_plan": state["draft_plan"].model_dump(mode="json"),
            "evaluation": evaluation.model_dump(mode="json"),
        }
        draft = await request_structured_model(
            self._deps.model,
            _planner_system_prompt(adjustment),
            payload,
            _run(runtime).budget,
            PlanDraft,
        )
        return {"draft_plan": draft, "revision_count": 1}

    async def persist_draft(
        self, state: WorkflowState, runtime: Runtime[GeneratePlanRun]
    ) -> WorkflowState:
        """通过路径：先提交业务 draft，再写入 State 的 ``draft_plan_id`` 并进入等待。"""
        draft: PlanDraft = state["draft_plan"]
        evaluation: EvaluationResult = state["evaluation"]
        adjustment = _run(runtime).adjustment
        written = await self._deps.persistence.persist_plan_result(
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
        """确认等待：interrupt 载荷只携带 ``draft_plan_id``；恢复时按 resume 载荷写下用户动作。"""
        action = _require_confirmation_action(
            interrupt({"draft_plan_id": state["draft_plan_id"]}),
            expected_plan_id=state["draft_plan_id"],
        )
        return {"confirmation": "confirmed" if action == "confirm" else "rejected"}

    async def activate_plan(
        self, state: WorkflowState, runtime: Runtime[GeneratePlanRun]
    ) -> WorkflowState:
        """用户确认：把激活事务交给领域服务，本节点不自己写 ``plans``／``plan_sessions``。"""
        confirmed_at = self._created_at()
        await self._deps.activation.activate(
            state["draft_plan_id"],
            business_day=_run(runtime).business_day,
            confirmed_at=confirmed_at,
            archived_at=confirmed_at,
        )
        return {"confirmation": "confirmed"}

    async def archive_draft(self, state: WorkflowState) -> WorkflowState:
        """用户拒绝：``draft -> archived``（原 active 不变）。"""
        await self._deps.activation.reject(
            state["draft_plan_id"], archived_at=self._created_at()
        )
        return {"confirmation": "rejected", "termination_reason": "archive_draft"}

    async def reject_draft(self, state: WorkflowState) -> WorkflowState:
        """二次阻断失败：写 ``rejected`` 终态并结束，不进入等待确认、不产生可激活计划。"""
        await self._deps.persistence.persist_plan_result(
            state["draft_plan"],
            state["evaluation"],
            existing_draft_id=state.get("draft_plan_id"),
            created_at=self._created_at(),
        )
        return {"termination_reason": "reject_draft"}

    # ---------- 内部：载荷、确定性校验、模型请求与时钟 ----------

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

    async def _planner_payload(
        self, state: WorkflowState, adjustment: AdjustmentContext | None
    ) -> dict[str, Any]:
        """Planner 输入：六类上下文 ＋ 已加载 Skill ＋ 确定性候选动作。"""
        context: MemoryContext = state["context"]
        skill: LoadedSkill = state["loaded_skill"]
        payload = asdict(context)
        payload.pop("conversation_messages")
        # 无历史时不出现该键：与 Router／Evaluator 的载荷形状一致。
        payload.update(history_payload(context.conversation_messages))
        payload["skill"] = asdict(skill)
        payload["candidate_actions"] = await self._candidate_actions(context.profile)
        if adjustment is not None:
            payload["active_plan_draft"] = adjustment.active_draft.model_dump(mode="json")
            payload["progression_decisions"] = await self._progression_decisions(
                adjustment
            )
        return payload

    async def _progression_decisions(
        self, adjustment: AdjustmentContext
    ) -> list[dict[str, Any]]:
        """active 里有目标处方的动作的渐进决策。"""
        catalog = await self._deps.catalog.list_all()
        exercises = {exercise.id: exercise for exercise in catalog}
        work_sets = await self._deps.stats.list_valid_work_sets()
        decisions: list[dict[str, Any]] = []
        for exercise_id, target in active_load_targets(adjustment.active_draft).items():
            exercise = exercises.get(exercise_id)
            if exercise is None or exercise.min_load_increment_kg is None:
                continue
            decision = resolve_progression(
                work_sets,
                linked_workout_session_ids=adjustment.linked_workout_session_ids,
                target_sets=target.sets,
                reps_min=target.reps_min,
                reps_max=target.reps_max,
                target_load_kg=target.target_load_kg,
                increment_kg=exercise.min_load_increment_kg,
            )
            decisions.append(
                {
                    "exercise_id": exercise_id,
                    **asdict(target),
                    "decision": asdict(decision),
                }
            )
        return decisions

    async def _candidate_actions(self, profile: Profile | None) -> list[dict[str, Any]]:
        """候选动作：``recommendable`` 目录动作先确定性删除禁用 ID，再附确定性起始负荷。"""
        forbidden = known_forbidden_exercise_ids(profile) if profile is not None else ()
        catalog = await self._deps.catalog.list_all()
        candidates = filter_forbidden_exercises(
            [exercise for exercise in catalog if exercise.recommendable],
            forbidden_exercise_ids=forbidden,
        )
        work_sets = await self._deps.stats.list_valid_work_sets()
        return [
            {
                "exercise_id": exercise.id,
                "standard_name_zh": exercise.standard_name_zh,
                "record_type": exercise.record_type,
                "load_convention": exercise.load_convention,
                "min_load_increment_kg": exercise.min_load_increment_kg,
                "starting_load": resolve_starting_load(
                    work_sets, exercise_id=exercise.id
                ).model_dump(mode="json"),
            }
            for exercise in candidates
        ]

    def _evaluator_payload(
        self, context: MemoryContext, draft: PlanDraft
    ) -> dict[str, Any]:
        """Rubric 输入：当前请求、画像事实、候选计划与本次请求之前的对话历史。

        不含确定性失败项（未通过时根本不调用模型）；当前用户消息只在 ``request`` 出现一次。
        """
        payload: dict[str, Any] = {
            "request": context.request,
            "profile": asdict(context.profile),
            "plan": draft.model_dump(mode="json"),
            "business_day": context.business_day.isoformat(),
            # 无历史时不出现该键：与 Router／Planner 的载荷形状一致。
            **history_payload(context.conversation_messages),
        }
        return payload

    def _created_at(self) -> str:
        """业务记录时间由注入时钟给出（节点不读系统时钟）。"""
        return self._deps.now().isoformat()


def _require_confirmation_action(resume: Any, *, expected_plan_id: int | None) -> str:
    """校验确认 resume 载荷并返回用户动作；形状或身份不符即明确冲突。"""
    if not isinstance(resume, Mapping):
        raise ConfirmationConflict(f"确认 resume 载荷必须是对象：{type(resume).__name__}")
    if set(resume) != {"action", "plan_id"}:
        raise ConfirmationConflict(
            f"确认 resume 载荷只能携带 action 与 plan_id：{sorted(resume)}"
        )
    action = resume["action"]
    if action not in CONFIRMATION_ACTIONS:
        raise ConfirmationConflict(f"确认 resume 载荷的动作非法：{action!r}")
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


def _active_exercise_ids(adjustment: AdjustmentContext | None) -> tuple[str, ...] | None:
    """调整计划的 PB 过滤口径：active 计划涉及的稳定 ``exercise_id``，去重且顺序稳定。"""
    if adjustment is None:
        return None
    seen: dict[str, None] = {}
    for day in adjustment.active_draft.training_days:
        for planned in day.exercises:
            seen.setdefault(planned.exercise_id, None)
    return tuple(seen)


def _planner_system_prompt(adjustment: AdjustmentContext | None) -> str:
    """Planner 的系统提示词按模式选择：生成计划照抄起始负荷，调整计划按渐进决策给出负荷。"""
    return (
        PLANNER_SYSTEM_PROMPT
        if adjustment is None
        else ADJUSTMENT_PLANNER_SYSTEM_PROMPT
    )


def _rubric_not_run() -> RubricResult:
    """确定性层未通过：不调用模型 Rubric，三个维度都按未通过记录。"""
    reason = "确定性领域校验未通过：未调用模型 Rubric"
    return RubricResult(
        goal_alignment=RubricVerdict(passed=False, reason=reason),
        schedule_reasonableness=RubricVerdict(passed=False, reason=reason),
        explanation_quality=RubricVerdict(passed=False, reason=reason),
    )


def _evaluation_result(
    deterministic: DeterministicResult,
    rubric: RubricResult,
    revision_count: int,
    *,
    rubric_ran: bool,
) -> EvaluationResult:
    """合成 Evaluator 结果：只有两个硬门槛影响 ``passed``，解释质量只进 ``warnings``。"""
    blocking = [
        f"{failure.code}: {failure.message}" for failure in deterministic.failures
    ]
    warnings: list[str] = []
    if rubric_ran:
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
    )
