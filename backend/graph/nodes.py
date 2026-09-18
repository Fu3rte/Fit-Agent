"""生成计划子图的节点与一次 Run 的运行上下文（stage4.md §3.2–§3.7、§5.3、§6 Subtask 04）。

节点只编排既有领域能力：安全分流复用 ``domain.profile.safety``，画像／目录／统计事实经既有领域服务
读取，禁用动作过滤、起始负荷、渐进决策与确定性校验复用 ``domain.plans.rules``，计划与 Evaluator
结构复用 ``domain.plans.schema``，active 预读、draft／rejected 写入复用 ``domain.plans.service``。
同一套节点按 ``intent`` 切换两种模式（stage5.md §3.4）：``generate_plan`` 生成新计划；``adjust_plan``
在预读的当前 active 计划上做局部调整（只加载 ``plan-adjustment``、PB 只装配 active 涉及动作、外加负重
负荷按确定性渐进决策给出、新 draft 写 ``source_plan_id``）。本模块不实现 Router（``graph/router.py``
由运行入口 ``graph/workflow.py::invoke_agent_run`` 先调用）；确认／拒绝节点只把用户动作交给
``domain.plans.service`` 的同一个激活／归档事务（节点内不散写多张表），也不扩安全词表或新建评分、
权重与阈值。

模型只经构造期注入的 callable 调用（:attr:`GeneratePlanDeps.model`）：测试注入固定替身，生产由
``graph.model.openai_compatible_model_call`` 提供 OpenAI 兼容入口；API Key／Base URL／模型名只从
环境变量读取，不进入 State、业务库或 checkpoint。

四条边界：

- **安全优先**：``safety_check`` 是入口节点，命中即 ``safety_stop``，发生在 Skill 加载、
  MemoryAssembler 装配与 Planner 之前，不产生模型调用或计划记录。
- **画像缺失在 Planner 前失败**：``weekly_frequency`` 不是 ``known`` 时抛 :class:`RequiredProfileMissing`，
  不调 Planner、不写 draft／rejected、不用默认频率或模型猜测。
- **调整计划不脱离旧计划**：``require_active_plan`` 在 ``load_context``／Planner 前预读当前 active；
  没有 active 即抛 :class:`RequiredActivePlanMissing`，不调 Planner、不写 draft／rejected、不动原 active。
  预读结果只进运行上下文（``GeneratePlanRun.adjustment``），不进 State、不落 checkpoint（stage5.md §3.9）。
- **模型调用不在数据库事务内**：模型调用只发生在 :meth:`GeneratePlanNodes._request_model`；业务写入
  只在 ``PlanPersistenceService`` 自己的短事务内。
- **确认只有一个领域提交入口**：``activate_plan``／``archive_draft`` 只调用 ``PlanActivationService``
  的 ``activate``／``reject``；resume 载荷只带动作与 ``plan_id``，身份不一致即明确冲突、不写任何行
  （stage5.md §3.1–§3.3）。
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
    ActiveLoadTarget,
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
    KnownLoad,
    PlanDraft,
    RubricResult,
    RubricVerdict,
    RuleFailure,
    WeightedRepsPrescription,
)
from domain.plans.service import (
    PlanActivationError,
    PlanActivationService,
    PlanPersistenceService,
    PlanReadService,
)
from domain.profile.safety import message_red_flag_hits
from domain.profile.schema import Profile
from domain.profile.service import ProfileService
from domain.stats.repo import StatsRepo
from graph.context import MemoryAssembler, MemoryContext
from graph.model import ModelCall, parse_model_json
from graph.skills import LoadedSkill, SkillLoader
from graph.state import Intent, WorkflowState

#: 生成计划子图加载的 Skill：Router 判定 ``generate_plan`` 后只加载命中的那一个（讨论总结 §6.2）。
PLANNING_SKILL_NAME = "workout-planning"

#: 调整计划加载的 Skill：Router 判定 ``adjust_plan`` 后只加载它，与生成计划共用同一套节点（stage5.md §3.4）。
ADJUSTMENT_SKILL_NAME = "plan-adjustment"

#: 调整计划的 intent 值（``graph.state.INTENTS`` 之一）：条件边按它先进入 active 预读节点。
ADJUST_PLAN_INTENT: Intent = "adjust_plan"

#: 确认等待处允许的两个用户动作：resume 载荷 ``action`` 的全部合法取值（stage5.md §3.3）。
CONFIRMATION_ACTIONS: tuple[str, ...] = ("confirm", "reject")

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
#: 调整计划模式的 Planner 系统提示词：生成模式「负荷照抄 starting_load」的规则不适用于调整，
#: 外加负重的具体负荷改由确定性渐进决策给出，且未被证据推翻的内容必须原样沿用（stage5.md §3.4）。
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
    "6. 只输出一个 JSON 对象，字段与下方 Schema 完全一致，不输出任何解释文字或额外字段。\n"
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


class RequiredActivePlanMissing(ValueError):
    """没有 active 计划：调整计划前明确失败，不调 Planner、不写 draft／rejected（stage5.md §3.4）。"""


class ConfirmationConflict(PlanActivationError):
    """确认／拒绝的请求身份与等待中的 interrupt 不一致，或 resume 载荷形状非法：明确冲突，不写任何行。

    继承领域激活错误基类：确认路径的冲突都在 ``domain.plans.service`` 的同一族错误里，调用方按同一
    形状映射 HTTP 状态（stage5.md §3.3、§3.7）。
    """


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


@dataclass(frozen=True, slots=True)
class AdjustmentContext:
    """调整计划的目标：预读的 active 身份、其统一 ``PlanDraft`` 与关联日程训练身份。

    ``active_draft`` 是 ``plans.structured_content`` 按统一 Schema 解析的结果（PB 过滤口径从它提取）；
    ``linked_workout_session_ids`` 只含关联该 active 日程的训练——额外训练既不计入渐进历史，也不打断
    连续性（``domain.plans.rules``）。
    """

    plan_id: int
    active_draft: PlanDraft
    linked_workout_session_ids: tuple[int, ...]


@dataclass(slots=True)
class GeneratePlanRun:
    """一次 invocation 的运行上下文：业务日期、模型请求预算、调整预读结果与 regenerate 信号（不进 State）。

    ``adjustment`` 由 ``require_active_plan`` 节点在调整分支里写入：它只属于本次 Run，不写 State、
    不落 checkpoint（stage5.md §3.9）。``regenerate`` 是 HTTP 请求体里的显式重新生成标志，同样只属于
    本次 Run：它不是业务事实，也不进冻结的 ``WorkflowState`` 字段集合。
    """

    business_day: date
    budget: ModelRequestBudget = field(default_factory=ModelRequestBudget)
    adjustment: AdjustmentContext | None = None
    #: 显式重新生成：同类替换既有 draft 的信号（stage5.md §3.4 第 6 条；由传输层从请求体读入）。
    regenerate: bool = False


@dataclass(frozen=True, slots=True)
class GeneratePlanDeps:
    """生成计划子图的构造期依赖：领域服务、Skill 加载器、注入的模型 callable 与时钟。

    ``plans`` 是计划只读入口，只供调整分支的 ``require_active_plan`` 预读当前 active（§3.4）。
    """

    profiles: ProfileService
    catalog: ActionCatalogService
    stats: StatsRepo
    assembler: MemoryAssembler
    skills: SkillLoader
    persistence: PlanPersistenceService
    plans: PlanReadService
    #: 唯一的确认／拒绝事务入口：``activate_plan``／``archive_draft`` 与 checkpoint 兜底共用它。
    activation: PlanActivationService
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

        ``message_red_flag_hits`` 在本子图的唯一调用位置（运行入口
        ``graph/workflow.py::stream_agent_run`` 在同一 Run 进入子图前用同一份封闭词表预检，保证安全
        命中先于 Router、已有 draft 的复用／冲突／regenerate 与 active 预读，stage5.md §3.5）；本节点不加载
        Skill、不装配记忆、不调模型、不写业务库；命中后的分支由条件边根据 ``termination_reason``
        转入 ``safety_stop``。

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

    async def require_active_plan(
        self, state: WorkflowState, runtime: Runtime[GeneratePlanRun]
    ) -> WorkflowState:
        """调整计划的 Planner 前前提：预读当前 active 并把身份写进运行上下文（stage5.md §3.4）。

        无 active 即抛 :class:`RequiredActivePlanMissing`：不调 Planner、不写 draft／rejected、不动原
        active。预读结果（active id、统一 ``PlanDraft``、关联日程训练身份）只进
        ``GeneratePlanRun.adjustment``，不进 State、不落 checkpoint（stage5.md §3.9）。
        """
        _run(runtime).adjustment = await _read_active_plan(
            self._deps.plans, self._deps.stats
        )
        return {}

    async def load_context(
        self, state: WorkflowState, runtime: Runtime[GeneratePlanRun]
    ) -> WorkflowState:
        """固定范围装配：生成计划传 ``exercise_ids=None``（全部已有动作 PB）；调整计划只装配当前
        active 计划涉及的稳定 ``exercise_id``（训练日／动作顺序去重，stage5.md §3.4）。"""
        context = await self._deps.assembler.assemble(
            state["request"],
            business_day=_run(runtime).business_day,
            exercise_ids=_active_exercise_ids(_run(runtime).adjustment),
        )
        return {"context": context}

    async def load_skill(
        self, state: WorkflowState, runtime: Runtime[GeneratePlanRun]
    ) -> WorkflowState:
        """命中后才加载正文：只加载本次 intent 命中的那一个（生成 ``workout-planning``／调整
        ``plan-adjustment``），不全量加载 Skills。"""
        name = (
            PLANNING_SKILL_NAME
            if _run(runtime).adjustment is None
            else ADJUSTMENT_SKILL_NAME
        )
        return {"loaded_skill": self._deps.skills.load(name)}

    async def planner(
        self, state: WorkflowState, runtime: Runtime[GeneratePlanRun]
    ) -> WorkflowState:
        """首个候选：Planner 只接收六类上下文、已加载 Skill 与确定性候选动作（stage4.md §6 Subtask 04
        任务 4；调整分支还要 active 的统一 ``PlanDraft`` 与渐进决策，stage5.md §3.4）。

        ``revision_count`` 写常量 0：本节点是每个 Run 的第一个候选（``revise_once`` 是唯一写 1 的地方），
        因此不能让同一 thread 上一次 Run 留下的计数决定本次 Run 的修订预算（stage4.md §3.6）。
        """
        adjustment = _run(runtime).adjustment
        payload = await self._planner_payload(state, adjustment)
        text = await self._request_model(
            _planner_system_prompt(adjustment), payload, _run(runtime).budget
        )
        return {
            "draft_plan": parse_model_json(text, PlanDraft),
            "revision_count": 0,
        }

    async def evaluator(
        self, state: WorkflowState, runtime: Runtime[GeneratePlanRun]
    ) -> WorkflowState:
        """分层评估：确定性领域校验先跑，只有通过才调用模型 Rubric（stage4.md §3.5）。

        确定性层按模式取规则：生成计划用最近工作组口径，调整计划用当前 active 的渐进决策口径
        （两者共用同一组规则标识，stage5.md §3.4）。
        """
        context: MemoryContext = state["context"]
        draft: PlanDraft = state["draft_plan"]
        revision_count = state.get("revision_count") or 0
        profile = require_profile(context.profile)
        adjustment = _run(runtime).adjustment
        failures = await self._deterministic_failures(draft, profile, adjustment)
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
        adjustment = _run(runtime).adjustment
        payload = await self._planner_payload(state, adjustment)
        payload["revision"] = {
            "previous_plan": state["draft_plan"].model_dump(mode="json"),
            "evaluation": evaluation.model_dump(mode="json"),
        }
        text = await self._request_model(
            _planner_system_prompt(adjustment), payload, _run(runtime).budget
        )
        return {"draft_plan": parse_model_json(text, PlanDraft), "revision_count": 1}

    async def persist_draft(
        self, state: WorkflowState, runtime: Runtime[GeneratePlanRun]
    ) -> WorkflowState:
        """通过路径：先提交业务 draft，再写入 State 的 ``draft_plan_id`` 并进入等待（stage4.md §3.10）。

        ``source_plan_id`` 按模式给出：生成计划为 NULL，调整计划为本次预读的当前 active.id（stage5.md §3.4）。
        """
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
        """确认等待：interrupt 载荷只携带 ``draft_plan_id``；恢复时按 resume 载荷写下用户动作。

        resume 载荷只允许 ``action`` 与 ``plan_id``（不复制完整计划、评估或业务事实），且 ``plan_id``
        必须等于本次 interrupt 的 ``draft_plan_id``，不一致即明确冲突、不写任何行（stage5.md §3.3）。
        路由只读这里写下的 ``confirmation``：条件边不重复解析载荷。
        """
        action = _require_confirmation_action(
            interrupt({"draft_plan_id": state["draft_plan_id"]}),
            expected_plan_id=state["draft_plan_id"],
        )
        return {"confirmation": "confirmed" if action == "confirm" else "rejected"}

    async def activate_plan(
        self, state: WorkflowState, runtime: Runtime[GeneratePlanRun]
    ) -> WorkflowState:
        """用户确认：把 §3.1 的激活事务交给领域服务，本节点不自己写 ``plans``／``plan_sessions``。

        操作目标就是 interrupt 的 ``draft_plan_id``（入口与 ``wait_for_confirmation`` 都已校验请求
        ``plan_id`` 与它相等）；业务日与时间戳由本次 invocation 注入；重复确认由
        ``PlanActivationService`` 按 §3.2 幂等，本节点不缓存也不二次提交。
        """
        confirmed_at = self._created_at()
        await self._deps.activation.activate(
            state["draft_plan_id"],
            business_day=_run(runtime).business_day,
            confirmed_at=confirmed_at,
            archived_at=confirmed_at,
        )
        return {"confirmation": "confirmed"}

    async def archive_draft(self, state: WorkflowState) -> WorkflowState:
        """用户拒绝：``draft -> archived``（原 active 不变，永不写 ``rejected``，stage5.md §3.2）。"""
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
        """确定性层：生成计划用最近工作组规则，调整计划用当前 active 的渐进决策（stage5.md §3.4）。

        两者共用同一组规则标识与同一份目录／画像／有效工作组事实；调整计划额外需要预读的 active
        ``PlanDraft`` 与其关联日程训练身份。
        """
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
        """Planner 输入：六类上下文（字段名即读取清单）＋ 已加载 Skill ＋ 确定性候选动作。

        调整分支额外附上当前 active 的统一 ``PlanDraft``（未被证据推翻的内容以此为起点）与按确定性
        渐进规则算出的决策（外加负重负荷只能等于它）。
        """
        context: MemoryContext = state["context"]
        skill: LoadedSkill = state["loaded_skill"]
        payload = asdict(context)
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
        """active 里有目标处方的动作的渐进决策（stage5.md §3.4 第 3 条）。

        输入面与 Evaluator 的确定性校验完全一致（``domain.plans.rules.resolve_progression``）：目标组数／
        次数区间／目标负荷来自 active 的统一 ``PlanDraft``，训练历史只取与该 active 关联日程的那些。
        目录里缺该动作或它不是外加负重口径时不给出决策：那属于结构和目录检查的报错范围，不在这里猜。
        """
        catalog = await self._deps.catalog.list_all()
        exercises = {exercise.id: exercise for exercise in catalog}
        work_sets = await self._deps.stats.list_valid_work_sets()
        decisions: list[dict[str, Any]] = []
        for exercise_id, target in _active_load_targets(adjustment.active_draft).items():
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


def _require_confirmation_action(resume: Any, *, expected_plan_id: int | None) -> str:
    """校验确认 resume 载荷并返回用户动作；形状或身份不符即明确冲突（stage5.md §3.3）。

    载荷只允许 ``action`` 与 ``plan_id`` 两个键（不复制完整计划、评估或业务事实），且 ``plan_id``
    必须等于请求要操作的那个 draft；两者任一不符都不进入确认分支，也不写任何行。
    """
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
    plans: PlanReadService, stats: StatsRepo
) -> AdjustmentContext:
    """预读调整计划的目标：当前 active 必须存在，缺失即明确失败（stage5.md §3.4 第 1 条）。

    同时把它的 ``structured_content`` 按统一 ``PlanDraft`` 解析（调用方从它提取稳定 ``exercise_id``
    过滤 PB），并给出与该 active 关联日程的训练身份（渐进决策只统计它们，额外训练不计入也不打断）。
    本函数不调模型、不写任何行：失败时没有 Planner 调用，也没有 draft／rejected。
    """
    active = await plans.get_active()
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
    """调整计划的 PB 过滤口径：active 计划涉及的稳定 ``exercise_id``，去重且顺序稳定。

    ``None`` 即生成计划口径（不过滤，装配全部已有动作 PB）。
    """
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


def _active_load_targets(active_draft: PlanDraft) -> dict[str, ActiveLoadTarget]:
    """active 里每个动作的目标处方：渐进决策的输入面（没有具体负荷的动作不产生目标）。

    与 ``domain.plans.rules`` 的确定性校验同一口径（同一动作重复出现时取第一处，顺序稳定、不猜合并）；
    Evaluator 会用规则里的那份重算并校验候选负荷，两份口径漂移只会让调整得不出可通过的候选。
    """
    targets: dict[str, ActiveLoadTarget] = {}
    for day in active_draft.training_days:
        for planned in day.exercises:
            prescription = planned.prescription
            if not isinstance(prescription, WeightedRepsPrescription) or not isinstance(
                prescription.load, KnownLoad
            ):
                continue
            targets.setdefault(
                planned.exercise_id,
                ActiveLoadTarget(
                    sets=planned.sets,
                    reps_min=prescription.reps_min,
                    reps_max=prescription.reps_max,
                    target_load_kg=prescription.load.weight_kg,
                ),
            )
    return targets


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
