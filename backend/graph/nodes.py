"""生成计划子图的节点与一次 Run 的运行上下文（stage4.md §3.2–§3.7、§5.3、§6 Subtask 04）。

节点只编排既有领域能力：安全分流复用 ``domain.profile.safety``，画像／目录／统计事实经既有领域服务
读取，禁用动作过滤、起始负荷与确定性校验复用 ``domain.plans.rules``，计划与 Evaluator 结构复用
``domain.plans.schema``，draft／rejected 写入复用 ``domain.plans.service``。本模块不实现 Router、
调整计划链路、确认／拒绝／激活事务，也不扩安全词表或新建评分、权重与阈值。

模型只经构造期注入的 callable 调用（:attr:`GeneratePlanDeps.model`）：测试注入固定替身，生产由
``graph.model.openai_compatible_model_call`` 提供 OpenAI 兼容入口；API Key／Base URL／模型名只从
环境变量读取，不进入 State、业务库或 checkpoint。

四条边界：

- **安全优先**：``safety_check`` 是入口节点，命中即 ``safety_stop``，发生在 Skill 加载、
  MemoryAssembler 装配与 Planner 之前，不产生模型调用或计划记录。
- **画像缺失在 Planner 前失败**：``weekly_frequency`` 不是 ``known`` 时抛 :class:`RequiredProfileMissing`，
  不调 Planner、不写 draft／rejected、不用默认频率或模型猜测。
- **模型调用不在数据库事务内**：模型调用只发生在 :meth:`GeneratePlanNodes._request_model`；业务写入
  只在 ``PlanPersistenceService`` 自己的短事务内。
- **Revise 上限由条件边表达**：``revision_count >= 1`` 时阻断失败直接进入 ``reject_draft``，
  不再回到 Planner（``graph/workflow.py::_route_after_evaluator``）。每个 Run 的首个候选与入口节点都
  显式写出本次 Run 的 ``revision_count``／``termination_reason``，不让同一 thread 上一次 Run 的存档
  决定本次 Run 的路由与修订预算（stage4.md §3.6）。
"""

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any

from langgraph.runtime import Runtime
from langgraph.types import interrupt

from config import (
    GRAPH_RUN_TIMEOUT_SECONDS,
    MAX_MODEL_REQUESTS_PER_RUN,
    MODEL_REQUEST_TIMEOUT_SECONDS,
)
from domain.actions.service import ActionCatalogService
from domain.plans.rules import (
    filter_forbidden_exercises,
    known_forbidden_exercise_ids,
    resolve_starting_load,
    validate_plan_draft,
)
from domain.plans.schema import (
    DeterministicResult,
    EvaluationResult,
    PlanDraft,
    RubricResult,
    RubricVerdict,
)
from domain.plans.service import PlanPersistenceService
from domain.profile.safety import message_red_flag_hits
from domain.profile.schema import Profile
from domain.profile.service import ProfileService
from domain.stats.repo import StatsRepo
from graph.context import MemoryAssembler, MemoryContext
from graph.model import ModelCall, parse_model_json
from graph.skills import LoadedSkill, SkillLoader
from graph.state import WorkflowState

#: 生成计划子图加载的 Skill：Router 判定 ``generate_plan`` 后只加载命中的那一个（讨论总结 §6.2）。
PLANNING_SKILL_NAME = "workout-planning"

#: 两份提示词都要带完整 Schema：Skill 只声明「交由 Stage 4 统一 Schema 校验」，不定义字段名
#: （``backend/skills/workout-planning/SKILL.md`` §结构化输出要求），字段名与形状只能由此给出。
_PLAN_SCHEMA_TEXT = json.dumps(
    PlanDraft.model_json_schema(), ensure_ascii=False, sort_keys=True
)
_RUBRIC_SCHEMA_TEXT = json.dumps(
    RubricResult.model_json_schema(), ensure_ascii=False, sort_keys=True
)

#: Planner 系统提示词：只组织注入事实，不替确定性规则下结论（stage4.md §3.1／§3.4）。
PLANNER_SYSTEM_PROMPT = (
    "你是 Fit-Agent 的训练计划 Planner。依据 payload 里的六类上下文、已加载 Skill 与确定性候选动作，"
    "生成一份待用户确认的七天训练计划草案。硬要求：\n"
    "1. 只使用 candidate_actions 给出的稳定 exercise_id；禁用动作不在候选里，不得凭记忆补回。\n"
    "2. 负荷只能照抄候选动作的 starting_load：known 时连同来源训练与组序号照抄，"
    "needs_calibration 时不得给出任何具体重量。\n"
    "3. 处方类型必须与目录记录口径一致：reps_weight→weighted_reps、"
    "reps_bodyweight→bodyweight_reps、time→timed；自重与计时处方不得携带负荷字段。\n"
    "4. training_days 数量等于 weekly_frequency，日期落在 starts_on 起连续七天内且不重复。\n"
    "5. 只输出一个 JSON 对象，字段与下方 Schema 完全一致，不输出任何解释文字或额外字段。\n"
    "统一计划 Schema：" + _PLAN_SCHEMA_TEXT
)
#: Evaluator 系统提示词：只判定，不改写计划，也不重算业务事实（stage4.md §3.5）。
EVALUATOR_SYSTEM_PROMPT = (
    "你是 Fit-Agent 的训练计划 Evaluator，只做判定、不改写计划、不重算业务事实。"
    "确定性领域校验已经通过，你只按三个维度判定候选计划：\n"
    "- goal_alignment：计划与用户已知目标是否匹配（硬门槛）。\n"
    "- schedule_reasonableness：七天内的安排是否合理（硬门槛；不替代代码的频率／日期检查，"
    "不引入新的数值阈值）。\n"
    "- explanation_quality：计划解释是否说清安排依据（建议项）。\n"
    "只输出一个 JSON 对象，字段与下方 Schema 完全一致：三个字段 goal_alignment、"
    "schedule_reasonableness、explanation_quality，每个形如 {passed: bool, reason: str}；"
    "不输出数值评分、维度权重或总分。\n"
    "Rubric Schema：" + _RUBRIC_SCHEMA_TEXT
)


class RequiredProfileMissing(ValueError):
    """画像缺失必需事实（未建档或 ``weekly_frequency`` 不是明确值）：Planner 前明确失败，不猜默认值。"""


class ModelRequestBudgetExceeded(RuntimeError):
    """一次 Run 的模型请求预算已用尽（次数上限或 Run 时限）：运行错误，不消耗修订、不创建 rejected。"""


@dataclass(slots=True)
class ModelRequestBudget:
    """一次 workflow invocation 的模型请求预算（stage4.md §3.7 决策 8B）。

    放在运行上下文里，不新增 WorkflowState 字段、不持久化 Provider 配置；模型调用前先扣预算，
    超限或超出 Run 时限即 :class:`ModelRequestBudgetExceeded`。同一份 Run 时限也界定整次 invocation：
    :meth:`remaining_run_seconds` 由 ``graph/workflow.py::invoke_generate_plan`` 用来包住完整一次
    Graph 调用（非模型节点同样受限）。
    """

    max_requests: int = MAX_MODEL_REQUESTS_PER_RUN
    run_timeout_seconds: float = GRAPH_RUN_TIMEOUT_SECONDS
    request_timeout_seconds: float = MODEL_REQUEST_TIMEOUT_SECONDS
    started_at: float = field(default_factory=time.monotonic)
    used: int = 0

    def remaining_run_seconds(self) -> float:
        """本 Run 的剩余时限（秒）：完整一次 invocation 的 180 秒上限也用它
        （``graph/workflow.py::invoke_generate_plan``），不只看模型调用资格。"""
        return self.run_timeout_seconds - (time.monotonic() - self.started_at)

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


@dataclass(slots=True)
class GeneratePlanRun:
    """一次 invocation 的运行上下文：业务日期与模型请求预算（不进 State、不落 checkpoint）。"""

    business_day: date
    budget: ModelRequestBudget = field(default_factory=ModelRequestBudget)


@dataclass(frozen=True, slots=True)
class GeneratePlanDeps:
    """生成计划子图的构造期依赖：领域服务、Skill 加载器、注入的模型 callable 与时钟。"""

    profiles: ProfileService
    catalog: ActionCatalogService
    stats: StatsRepo
    assembler: MemoryAssembler
    skills: SkillLoader
    persistence: PlanPersistenceService
    model: ModelCall
    now: Callable[[], datetime]


def require_profile(profile: Profile | None) -> Profile:
    """画像未建档即明确失败：不伪造空画像，也不让后续节点拿到 ``None``。"""
    if profile is None:
        raise RequiredProfileMissing(
            "画像未建档：请先补充每周训练次数等必需事实后再生成计划"
        )
    return profile


def require_weekly_frequency(profile: Profile) -> int:
    """画像明确给出的每周训练次数；``unknown``／``denied`` 即 Planner 前明确失败（stage4.md §3.2）。"""
    fact = profile.weekly_frequency
    if not fact.is_known or fact.value is None:
        raise RequiredProfileMissing(
            f"画像缺少每周训练次数（weekly_frequency={fact.state}）："
            "请补充每周训练次数后再生成计划"
        )
    return fact.value


class GeneratePlanNodes:
    """生成计划子图的节点集合：构造时注入服务与模型 callable，节点自身不实现领域规则。"""

    def __init__(self, deps: GeneratePlanDeps) -> None:
        self._deps = deps

    async def safety_check(self, state: WorkflowState) -> WorkflowState:
        """入口节点：对当前请求做封闭词表精确子串扫描，命中即写入终止原因。

        ``message_red_flag_hits`` 在本子图的唯一调用位置；本节点不加载 Skill、不装配记忆、不调模型、
        不写业务库；命中后的分支由条件边根据 ``termination_reason`` 转入 ``safety_stop``。

        两个分支都显式写 ``termination_reason``：``conversation_id`` 即 thread_id，同一会话的上一次 Run
        可能在同一 thread 的 checkpoint 里留下 ``safety_stop``／``reject_draft``，不显式覆盖就会让条件边
        读到上一次 Run 的结论（stage4.md §3.6「一个新用户请求可以启动新的 Run」）。
        """
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

    async def load_context(
        self, state: WorkflowState, runtime: Runtime[GeneratePlanRun]
    ) -> WorkflowState:
        """固定范围装配：生成计划继续传 ``exercise_ids=None``（全部已有动作 PB）。"""
        context = await self._deps.assembler.assemble(
            state["request"],
            business_day=_run(runtime).business_day,
            exercise_ids=None,
        )
        return {"context": context}

    async def load_skill(self, state: WorkflowState) -> WorkflowState:
        """命中后才加载正文：只加载 ``workout-planning``，不全量加载 Skills。"""
        return {"loaded_skill": self._deps.skills.load(PLANNING_SKILL_NAME)}

    async def planner(
        self, state: WorkflowState, runtime: Runtime[GeneratePlanRun]
    ) -> WorkflowState:
        """首个候选：Planner 只接收六类上下文与确定性过滤后的候选动作（stage4.md §6 Subtask 04 任务 4）。

        ``revision_count`` 写常量 0：本节点是每个 Run 的第一个候选（``revise_once`` 是唯一写 1 的地方），
        因此不能让同一 thread 上一次 Run 留下的计数决定本次 Run 的修订预算（stage4.md §3.6）。
        """
        payload = await self._planner_payload(state)
        text = await self._request_model(
            PLANNER_SYSTEM_PROMPT, payload, _run(runtime).budget
        )
        return {
            "draft_plan": parse_model_json(text, PlanDraft),
            "revision_count": 0,
        }

    async def evaluator(
        self, state: WorkflowState, runtime: Runtime[GeneratePlanRun]
    ) -> WorkflowState:
        """分层评估：确定性领域校验先跑，只有通过才调用模型 Rubric（stage4.md §3.5）。"""
        context: MemoryContext = state["context"]
        draft: PlanDraft = state["draft_plan"]
        revision_count = state.get("revision_count") or 0
        profile = require_profile(context.profile)
        catalog = await self._deps.catalog.list_all()
        failures = validate_plan_draft(
            draft,
            exercises={exercise.id: exercise for exercise in catalog},
            profile_weekly_frequency=require_weekly_frequency(profile),
            forbidden_exercise_ids=known_forbidden_exercise_ids(profile),
            work_sets=await self._deps.stats.list_valid_work_sets(),
        )
        deterministic = DeterministicResult(passed=not failures, failures=failures)
        rubric_ran = deterministic.passed
        rubric = (
            parse_model_json(
                await self._request_model(
                    EVALUATOR_SYSTEM_PROMPT,
                    self._evaluator_payload(context, draft),
                    _run(runtime).budget,
                ),
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
        """把结构化阻断理由交回 Planner 修订一次；``revision_count`` 置 1。

        路由前提：条件边只在 ``revision_count == 0`` 且阻断失败时进入本节点，因此不会出现第二次修订。
        """
        evaluation: EvaluationResult = state["evaluation"]
        payload = await self._planner_payload(state)
        payload["revision"] = {
            "previous_plan": state["draft_plan"].model_dump(mode="json"),
            "evaluation": evaluation.model_dump(mode="json"),
        }
        text = await self._request_model(
            PLANNER_SYSTEM_PROMPT, payload, _run(runtime).budget
        )
        return {"draft_plan": parse_model_json(text, PlanDraft), "revision_count": 1}

    async def persist_draft(self, state: WorkflowState) -> WorkflowState:
        """通过路径：先提交业务 draft，再写入 State 的 ``draft_plan_id`` 并进入等待（stage4.md §3.10）。"""
        draft: PlanDraft = state["draft_plan"]
        evaluation: EvaluationResult = state["evaluation"]
        written = await self._deps.persistence.persist_plan_result(
            draft,
            evaluation,
            existing_draft_id=state.get("draft_plan_id"),
            created_at=self._created_at(),
        )
        return {
            "draft_plan_id": written.id,
            "draft_plan": draft,
            "evaluation": evaluation,
            "confirmation": "pending",
        }

    async def wait_for_confirmation(self, state: WorkflowState) -> WorkflowState:
        """interrupt 载荷只携带 ``draft_plan_id``；确认／拒绝／激活事务留 Stage 5（stage4.md §3.10）。"""
        interrupt({"draft_plan_id": state["draft_plan_id"]})
        return {}

    async def reject_draft(self, state: WorkflowState) -> WorkflowState:
        """二次阻断失败：写 ``rejected`` 终态并结束，不进入等待确认、不产生可激活计划。"""
        await self._deps.persistence.persist_plan_result(
            state["draft_plan"],
            state["evaluation"],
            existing_draft_id=state.get("draft_plan_id"),
            created_at=self._created_at(),
        )
        return {"termination_reason": "reject_draft"}

    # ---------- 内部：载荷、模型请求与时钟 ----------

    async def _planner_payload(self, state: WorkflowState) -> dict[str, Any]:
        """Planner 输入：六类上下文（字段名即读取清单）＋ 已加载 Skill ＋ 确定性候选动作。"""
        context: MemoryContext = state["context"]
        skill: LoadedSkill = state["loaded_skill"]
        payload = asdict(context)
        payload["skill"] = asdict(skill)
        payload["candidate_actions"] = await self._candidate_actions(context.profile)
        return payload

    async def _candidate_actions(self, profile: Profile | None) -> list[dict[str, Any]]:
        """候选动作：``recommendable`` 目录动作先确定性删除禁用 ID，再附确定性起始负荷。

        禁用 ID 只取画像 ``known`` 值（``known_injuries`` 文本不推导新 ID）；起始负荷复用
        ``resolve_starting_load``（没有有效工作组历史时只能是 ``needs_calibration``）。
        """
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
        """Rubric 输入：当前请求、画像事实与候选计划；不含确定性失败项（未通过时根本不调用模型）。"""
        return {
            "request": context.request,
            "profile": asdict(context.profile),
            "plan": draft.model_dump(mode="json"),
        }

    async def _request_model(
        self,
        system_prompt: str,
        payload: Mapping[str, Any],
        budget: ModelRequestBudget,
    ) -> str:
        """一次模型请求：先扣请求预算，再在剩余时限内调用注入的 callable。

        模型调用不包在任何数据库事务里；超时或调用失败都向上抛，作为运行错误终止本次 Run。
        """
        timeout = budget.begin_request()
        async with asyncio.timeout(timeout):
            return await self._deps.model(system_prompt, _dump_payload(payload))

    def _created_at(self) -> str:
        """业务记录时间由注入时钟给出（节点不读系统时钟）。"""
        return self._deps.now().isoformat()


def _run(runtime: Runtime[GeneratePlanRun]) -> GeneratePlanRun:
    """取回本次 invocation 的运行上下文；缺失即调用方错误（业务日期与模型请求预算都在里面）。"""
    run = runtime.context
    if run is None:
        raise RuntimeError(
            "生成计划子图必须传入运行上下文 GeneratePlanRun（业务日期与模型请求预算）"
        )
    return run


def _dump_payload(payload: Mapping[str, Any]) -> str:
    """载荷 → 模型输入文本：日期等非 JSON 原生值按文本写出，排序固定便于复现。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _rubric_not_run() -> RubricResult:
    """确定性层未通过：不调用模型 Rubric，三个维度都按未通过记录，不伪造通过。

    这三个否定判定只是「未运行」的显式记录（Schema 要求三段都在）；它们不是真实 Rubric 结论，
    因此 :func:`_evaluation_result` 在 ``rubric_ran=False`` 时不把它们写进阻断项或 warning。
    """
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
    """合成 Evaluator 结果：只有两个硬门槛影响 ``passed``，解释质量只进 ``warnings``。

    ``rubric_ran`` 为假表示确定性层未通过、模型 Rubric 没有运行：此时阻断项与 warning 只由真实
    确定性失败给出，不把未运行的三个维度伪造成阻断失败或解释质量 warning（stage4.md §3.5 分层）。
    """
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
