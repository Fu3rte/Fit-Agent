"""生成计划子图的节点与一次 Run 的运行上下文。"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, TypeVar

import anthropic
import openai
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from config import (
    GRAPH_RUN_TIMEOUT_SECONDS,
    MAX_MODEL_REQUESTS_PER_RUN,
    MAX_TOOL_CALLS_PER_RUN,
    MODEL_REQUEST_TIMEOUT_SECONDS,
)
from domain.actions.repo import ExerciseRepo
from domain.conversations.context import ContextMessage, history_payload
from domain.plans.repo import PlanRepo
from domain.plans.rules import (
    active_load_targets,
    filter_forbidden_exercises,
    known_forbidden_exercise_ids,
    resolve_progression,
    resolve_starting_load,
    validate_plan_adjustment,
    validate_plan_draft,
)
from domain.plans.schema import (
    DeterministicResult,
    EvaluationResult,
    PlanDraft,
    RubricResult,
    RubricVerdict,
    RuleFailure,
)
from domain.plans.service import (
    PlanActivationError,
    PlanActivationService,
    PlanPersistenceService,
)
from domain.profile.safety import message_red_flag_hits
from domain.profile.schema import Profile
from domain.profile.service import ProfileService
from domain.stats.repo import StatsRepo
from graph.context import MemoryAssembler, MemoryContext
from graph.model import ModelGateway, TModel, dump_model_payload
from graph.skills import LoadedSkill, SkillLoader
from graph.state import Intent, WorkflowState

PLANNING_SKILL_NAME = "workout-planning"

ADJUSTMENT_SKILL_NAME = "plan-adjustment"

ADJUST_PLAN_INTENT: Intent = "adjust_plan"

CONFIRMATION_ACTIONS: tuple[str, ...] = ("confirm", "reject")

PLANNER_SYSTEM_PROMPT = (
    "你是 Fit-Agent 的训练计划 Planner。依据 payload 里的六类上下文、已加载 Skill 与确定性候选动作，"
    "生成一份待用户确认的七天训练计划草案。硬要求：\n"
    "1. 只使用 candidate_actions 给出的稳定 exercise_id；禁用动作不在候选里，不得凭记忆补回。\n"
    "2. 负荷只能照抄候选动作的 starting_load：known 时连同来源训练与组序号照抄，"
    "needs_calibration 时不得给出任何具体重量。\n"
    "3. 处方类型必须与目录记录口径一致：reps_weight→weighted_reps、"
    "reps_bodyweight→bodyweight_reps、time→timed；自重与计时处方不得携带负荷字段。\n"
    "4. training_days 数量等于 weekly_frequency，日期落在 starts_on 起连续七天内且不重复。\n"
    "5. starts_on 不得早于 payload.business_day：计划从当天或未来起始。"
)
ADJUSTMENT_PLANNER_SYSTEM_PROMPT = (
    "你是 Fit-Agent 的训练计划 Planner，本次任务是在 payload.active_plan_draft（当前 active 计划）"
    "之上按用户请求做局部调整，产出一份待用户确认的新版本。硬要求：\n"
    "1. 只使用 candidate_actions 给出的稳定 exercise_id；禁用动作不在候选里，不得凭记忆补回。\n"
    "2. 未被本次调整证据推翻的训练日、动作与处方原样沿用（含 scheduled_on、sets、次数区间与"
    "未涉及的解释文字）；只改用户本次要求且证据支持的部分，不重构整份计划。\n"
    "3. 外加负重动作的具体负荷只能等于 payload.progression_decisions 里同动作的 decision.load_kg；"
    "decision.action 为 needs_calibration 时不得给出任何具体重量。progression_decisions 里没有的"
    "动作（active 没有目标处方的动作）照抄候选动作的 starting_load。\n"
    "4. 处方类型必须与目录记录口径一致：reps_weight→weighted_reps、"
    "reps_bodyweight→bodyweight_reps、time→timed；自重与计时处方不得携带负荷字段。\n"
    "5. training_days 数量等于 weekly_frequency，日期落在 starts_on 起连续七天内且不重复。\n"
    "6. starts_on 不得早于 payload.business_day：计划从当天或未来起始。"
)
EVALUATOR_SYSTEM_PROMPT = (
    "你是 Fit-Agent 的训练计划 Evaluator，只做判定、不改写计划、不重算业务事实。"
    "确定性领域校验已经通过，你只按三个维度判定候选计划：\n"
    "- goal_alignment：计划与用户已知目标是否匹配（硬门槛）。\n"
    "- schedule_reasonableness：七天内的安排是否合理（硬门槛；不替代代码的频率／日期检查，"
    "不引入新的数值阈值）。\n"
    "- explanation_quality：计划解释是否说清安排依据（建议项）。\n"
    "每个维度只给布尔判定与理由，不给数值评分、维度权重或总分。"
)


class RequiredProfileMissing(ValueError):
    """画像缺失必需事实（未建档或 ``weekly_frequency`` 不是明确值）。"""


class RequiredActivePlanMissing(ValueError):
    """没有 active 计划：调整计划前明确失败。"""


class ConfirmationConflict(PlanActivationError):
    """确认／拒绝的请求身份与等待中的 interrupt 不一致。"""


class ModelRequestBudgetExceeded(RuntimeError):
    """一次 Run 的模型请求预算已用尽（次数上限或 Run 时限）。"""


class ToolCallBudgetExceeded(RuntimeError):
    """一次 Run 的工具调用预算已用尽（次数上限或 Run 时限）。"""


@dataclass(slots=True)
class ModelRequestBudget:
    """一次 Run 的模型请求与工具调用预算：两者共享同一 ``started_at`` 与 Run 总时限。"""

    max_requests: int = MAX_MODEL_REQUESTS_PER_RUN
    run_timeout_seconds: float = GRAPH_RUN_TIMEOUT_SECONDS
    request_timeout_seconds: float = MODEL_REQUEST_TIMEOUT_SECONDS
    started_at: float = field(default_factory=time.monotonic)
    used: int = 0
    max_tool_calls: int = MAX_TOOL_CALLS_PER_RUN
    tool_calls: int = 0

    def remaining_run_seconds(self) -> float:
        """本 Run 的剩余时限（秒）。"""
        return self.run_timeout_seconds - (time.monotonic() - self.started_at)

    def take_tool_call(self) -> None:
        """登记一次工具调用；次数上限或 Run 时限已用尽即明确失败，调用方不执行工具。"""
        if self.tool_calls >= self.max_tool_calls:
            raise ToolCallBudgetExceeded(
                f"单次 Run 最多 {self.max_tool_calls} 次工具调用，已用尽（不消耗模型请求次数）"
            )
        if self.remaining_run_seconds() <= 0:
            raise ToolCallBudgetExceeded(
                f"单次 Run 的 {self.run_timeout_seconds} 秒时限已用尽（不消耗模型请求次数）"
            )
        self.tool_calls += 1

    def begin_request(self) -> float:
        """登记一次模型请求，返回该次请求可用的超时秒数（不超过 Run 剩余时限）。"""
        if self.used >= self.max_requests:
            raise ModelRequestBudgetExceeded(
                f"单次 Run 最多 {self.max_requests} 次模型请求，已用尽（不消耗修订次数）"
            )
        remaining = self.remaining_run_seconds()
        if remaining <= 0:
            raise ModelRequestBudgetExceeded(
                f"单次 Run 的 {self.run_timeout_seconds} 秒模型请求时限已用尽（不消耗修订次数）"
            )
        self.used += 1
        return min(self.request_timeout_seconds, remaining)


@dataclass(frozen=True, slots=True)
class AdjustmentContext:
    """调整计划的目标：预读的 active 身份、其统一 ``PlanDraft`` 与关联日程训练身份。"""

    plan_id: int
    active_draft: PlanDraft
    linked_workout_session_ids: tuple[int, ...]


@dataclass(slots=True)
class GeneratePlanRun:
    """一次 invocation 的运行上下文。"""

    business_day: date
    budget: ModelRequestBudget = field(default_factory=ModelRequestBudget)
    adjustment: AdjustmentContext | None = None
    regenerate: bool = False
    #: 本次请求之前的完整对话上下文投影；当前用户消息不在其中（由 ``request`` 单独给出）。
    conversation_messages: tuple[ContextMessage, ...] = ()


@dataclass(frozen=True, slots=True)
class GeneratePlanDeps:
    """生成计划子图的构造期依赖：领域服务、Skill 加载器、注入的模型 callable 与时钟。"""

    profiles: ProfileService
    catalog: ExerciseRepo
    stats: StatsRepo
    assembler: MemoryAssembler
    skills: SkillLoader
    persistence: PlanPersistenceService
    plans: PlanRepo
    activation: PlanActivationService
    model: ModelGateway
    now: Callable[[], datetime]


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


async def _read_active_plan(
    plans: PlanRepo, stats: StatsRepo
) -> AdjustmentContext:
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




#: 瞬时失败后的固定重试延迟（秒）；重试只发生一次。
MODEL_RETRY_DELAY_SECONDS = 1.0

#: 可重试的明确瞬时故障类型：SDK 把 httpx 的连接错误、读超时与协议错误包装成 ``APIConnectionError``
#: （``APITimeoutError`` 是其子类）；529 过载由 Anthropic 的显式过载失败给出（不在状态码集合内）。
_RETRYABLE_FAILURE_TYPES: tuple[type[Exception], ...] = (
    openai.APIConnectionError,
    anthropic.APIConnectionError,
    anthropic.OverloadedError,
)

#: 可重试的 HTTP 状态码：429 限流与 500／502／503／504 服务端瞬时故障。
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

#: 状态码判定的唯一适用类型：只有这两类的 ``status_code`` 是 Provider 的 HTTP 状态。
_STATUS_ERROR_TYPES: tuple[type[Exception], ...] = (
    openai.APIStatusError,
    anthropic.APIStatusError,
)

TResult = TypeVar("TResult")


def _is_transient_failure(error: BaseException) -> bool:
    """失败是否属于可重试的瞬时故障：只看 ``__cause__`` 链，取消与产品错误都不算。"""
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, _RETRYABLE_FAILURE_TYPES):
            return True
        if (
            isinstance(current, _STATUS_ERROR_TYPES)
            and current.status_code in _RETRYABLE_STATUS_CODES
        ):
            return True
        current = current.__cause__
    return False


async def _request_with_retry(
    request: Callable[[], Awaitable[TResult]], budget: ModelRequestBudget
) -> TResult:
    """模型请求的唯一物理尝试循环：瞬时失败固定延迟后重试一次，其余失败立即上抛。

    每个物理尝试各登记一次请求预算，重试同样占用次数与 Run 剩余时限；取消是
    ``BaseException``，不进入本分支，绝不被当作瞬时失败重试。
    """
    attempt = 0
    while True:
        timeout = budget.begin_request()
        try:
            async with asyncio.timeout(timeout):
                return await request()
        except Exception as error:
            if attempt > 0 or not _is_transient_failure(error):
                raise
            attempt += 1
            await asyncio.sleep(MODEL_RETRY_DELAY_SECONDS)


async def request_model(
    model: ModelGateway,
    system_prompt: str,
    payload: Mapping[str, Any],
    budget: ModelRequestBudget,
) -> str:
    """一次文本模型请求：先扣请求预算，再在剩余时限内调用注入的 callable，瞬时失败重试一次。"""
    user_payload = dump_model_payload(payload)
    return await _request_with_retry(
        lambda: model.text(system_prompt, user_payload), budget
    )


async def request_structured_model(
    model: ModelGateway,
    system_prompt: str,
    payload: Mapping[str, Any],
    budget: ModelRequestBudget,
    schema: type[TModel],
) -> TModel:
    """一次结构化模型请求：与文本请求共享同一份预算、超时与重试，Schema 由 Provider 约束。"""
    user_payload = dump_model_payload(payload)
    return await _request_with_retry(
        lambda: model.structured(system_prompt, user_payload, schema), budget
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
