import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, Interrupt, StateSnapshot
from pydantic import BaseModel, ConfigDict

from domain.actions.repo import ExerciseRepo
from domain.actions.rules import RecordLoadMismatch, UnknownExercise
from domain.actions.schema import Exercise, LoadConvention
from domain.conversations.context import history_payload
from domain.plans.repo import PlanRepo
from domain.plans.schema import (
    BodyweightRepsPrescription,
    EvaluationResult,
    NeedsCalibration,
    Plan,
    PlanDraft,
    PlanSession,
    Prescription,
    RepsPrescription,
    TimedPrescription,
    WeightedRepsPrescription,
)
from domain.plans.service import (
    PlanDraftConflict,
    PlanNotFound,
    PlanPersistenceService,
)
from domain.profile.safety import message_red_flag_hits
from domain.records.rules import InvalidRecordFact
from domain.records.schema import SetType, WorkoutSetInput
from domain.records.service import WorkoutRecordsService
from domain.stats.service import StatsService
from graph.checkpointer import thread_config
from graph.model import ModelGateway
from graph.nodes import (
    ADJUST_PLAN_INTENT,
    ADJUSTMENT_SKILL_NAME,
    PLANNING_SKILL_NAME,
    ConfirmationConflict,
    GeneratePlanDeps,
    GeneratePlanNodes,
    GeneratePlanRun,
    request_model,
    request_structured_model,
)
from graph.router import (
    FitnessIntent,
    ScheduleDay,
    classify_intent,
    workflow_intent,
)
from graph.skills import LoadedSkill, SkillLoader
from graph.state import Intent, TerminationReason, WorkflowState

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


#: ``form_record`` 的可见引导。
FORM_RECORD_GUIDE = (
    "打卡记录请使用打卡表单：在「记录」页填写训练日期、动作、组数、次数与重量后提交，"
    "由既有记录接口写入；本流程不代写训练数据。"
)


class ExtractedWorkoutSet(BaseModel):
    """一组训练事实（模型提取结果）。"""

    model_config = ConfigDict(extra="forbid")

    exercise_id: str
    set_no: int
    set_type: SetType
    reps: int | None = None
    load_convention: LoadConvention | None = None
    weight_kg: float | None = None
    duration_seconds: int | None = None


class ExtractedWorkout(BaseModel):
    """一次自然语言打卡的结构化提取结果：业务自然日 ＋ 全部组。"""

    model_config = ConfigDict(extra="forbid")

    performed_on: date
    sets: list[ExtractedWorkoutSet]


NATURAL_LANGUAGE_RECORD_EXTRACTION_PROMPT = (
    "你是 Fit-Agent 的自然语言打卡提取器。只把用户这次训练描述提取为结构化事实，不写库、"
    "不计算任何统计、不决定候选计划日程。硬要求：\n"
    "1. 只使用 payload.actions 里给出的稳定 exercise_id；匹配不到的动作不要编造，也不要改写 id。\n"
    "2. performed_on 是训练发生的业务自然日（YYYY-MM-DD）；用户说“今天”／“昨天”时按 "
    "payload.business_day 折算。\n"
    "3. 每条组给出 set_no（同一动作内从 1 开始）、set_type（work／warmup／assisted）；外加重量"
    "动作给 weight_kg ＋ reps ＋ 与目录一致的 load_convention；纯自重动作只给 reps；计时动作"
    "只给 duration_seconds。"
)

NATURAL_LANGUAGE_RECORD_MESSAGE_PROMPT = (
    "你是 Fit-Agent 的打卡摘要生成器。把 payload.workout（后端已校验的结构化提取结果）写成一段"
    "面向用户的中文摘要，让用户确认日期、动作、组数、次数、重量与时长是否与本次训练一致。"
    "payload.candidate_plan_sessions 是数据库给出的当天未完成计划日程：恰一个时提示将自动关联；"
    "零个时不得许诺任何自动写入，必须提示用户显式选择「额外训练」才写为额外训练；"
    "多于一个时必须提示用户选择某个日程或标记为额外训练，"
    "不得替用户选择，也不得编造日程 ID。只输出这一段中文文本：不输出 JSON、不输出额外字段、"
    "不重算任何数值、不提 RIR／完成率／估算 1RM。"
)


def invalid_natural_language_record_message(error: Exception) -> str:
    """确认前校验失败的可见文本：照原样给出本项目自己的领域错误，并明确本次不写库。"""
    return f"自然语言打卡未通过校验：{error}。本次不写库；请修正训练描述后重试，或改用打卡表单。"


SAFETY_STOP_MESSAGE = (
    "本次请求包含急性伤病相关描述：不生成训练计划，也不写入任何计划数据；"
    "请先咨询专业医疗人员，再回来安排训练。"
)

REJECT_DRAFT_MESSAGE = (
    "计划未通过评估（二次评估仍未通过）：本次不产生可激活计划，原计划保持不变。"
)

VIEW_PROGRESS_SYSTEM_PROMPT = (
    "你是 Fit-Agent 的进步看板解释器。payload 里的 personal_bests 与 trend_summary 都是后端已经"
    "算好的确定性统计结果：只能解释它们，不得重算、补算或推断任何数值，也不得编造记录里没有的成绩。"
    "不评价进步／退步／停滞／疲劳，不输出 RIR、估算 1RM、训练容量或完成率。"
    "只输出一段面向用户的中文说明文本。"
)

KNOWLEDGE_QA_SYSTEM_PROMPT = (
    "你是 Fit-Agent 的训练知识问答助手。payload.knowledge_type 决定本次知识源：\n"
    "- exercise_technique：payload.exercise_name 是用户问到的动作名，payload.catalog_exercise 是动作"
    "目录里归一化后的实体（可能为 null），payload.skills 为空。\n"
    "- methodology：payload.skills 是仓库内既有训练方法论 Skill 的正文与引用文件，"
    "payload.catalog_exercise 为空。\n"
    "硬要求：\n"
    "1. catalog_exercise 非空时，它的记录口径与负重口径照原样使用；catalog_exercise 为 null 时"
    "只讲该动作的一般技术要点，不编造目录事实。\n"
    "2. payload.skills 非空时以它为主要依据；依据不足时给出通行的一般方法论并说明这是一般性说明。\n"
    "3. 只给一般健身教育信息：不做医疗诊断、不给个体化医疗结论、不承诺疗效；涉及疼痛、伤病或身体"
    "异常时明确建议咨询专业医疗人员。\n"
    "4. 只输出一段面向用户的中文文本：不生成或修改训练计划、不输出 JSON、不输出额外字段。"
)

GENERAL_CHAT_SYSTEM_PROMPT = (
    "你是 Fit-Agent 的对话助手。payload.request 是本次用户请求，它不属于计划管理、打卡、统计、"
    "日程查询与训练知识问答业务。硬要求：只做一般性中文答复，不生成或修改训练计划、不写业务库、"
    "不替用户决定训练处方；不做医疗诊断、不给个体化医疗结论，涉及疼痛、伤病或身体异常时明确建议"
    "咨询专业医疗人员。"
)

AgentEventName = Literal["node", "message", "waiting", "done", "error"]

NON_PLAN_INTENTS: tuple[Intent, ...] = (
    "form_record",
    "natural_language_record",
    "view_progress",
    "view_schedule",
    "knowledge_qa",
    "general",
)

SCHEDULE_DAY_OFFSETS: Mapping[ScheduleDay, int] = {"today": 0, "tomorrow": 1}

NO_ACTIVE_PLAN_SCHEDULE_MESSAGE = (
    "当前没有已生效的训练计划：先生成并确认一份计划，日程查询才能给出具体安排。"
)

INTERRUPT_EVENT_KEY = "__interrupt__"


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """一条产品事件。"""

    event: AgentEventName
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ExistingDraftTarget:
    """计划分支对业务库唯一 draft 的处置。"""

    reused: Plan | None = None
    replacement_id: int | None = None


@dataclass(frozen=True, slots=True)
class AgentRunDeps:
    """``invoke_agent_run``／``stream_agent_run`` 的构造期依赖。"""

    model: ModelGateway
    stats: StatsService
    plans: PlanRepo
    persistence: PlanPersistenceService
    catalog: ExerciseRepo
    records: WorkoutRecordsService
    skills: SkillLoader


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """一次 Agent Run 的产品结果。"""

    intent: Intent | None
    messages: tuple[str, ...]
    termination_reason: TerminationReason | None
    draft_plan_id: int | None


async def stream_agent_run(
    graph: CompiledStateGraph,
    state: WorkflowState,
    config: RunnableConfig,
    run: GeneratePlanRun,
    deps: AgentRunDeps,
) -> AsyncIterator[AgentEvent]:

    async with asyncio.timeout(run.budget.remaining_run_seconds()):
        intent: Intent | None = None
        target = ExistingDraftTarget()
        if not message_red_flag_hits(state["request"]):
            route = await classify_intent(
                state["request"],
                model=deps.model,
                budget=run.budget,
                history=run.conversation_messages,
            )
            intent = workflow_intent(route)
            if intent != ADJUST_PLAN_INTENT:
                run.adjustment = None
            if intent in NON_PLAN_INTENTS:
                yield AgentEvent("node", {"name": intent})
                if intent == "natural_language_record":
                    outcome = await _natural_language_record(
                        state["request"], run=run, deps=deps
                    )
                    yield AgentEvent("message", {"text": outcome.message})
                    if outcome.waiting is not None:
                        yield AgentEvent("waiting", outcome.waiting)
                    yield _done_event(
                        intent, termination_reason=None, draft_plan_id=None
                    )
                    return
                yield AgentEvent(
                    "message",
                    {
                        "text": await _non_plan_text(
                            intent, route, state["request"], run=run, deps=deps
                        )
                    },
                )
                yield _done_event(intent, termination_reason=None, draft_plan_id=None)
                return
            target = await _existing_draft_target(
                intent, regenerate=run.regenerate, deps=deps
            )
            if target.reused is not None:
                yield AgentEvent("waiting", {"draft_plan_id": target.reused.id})
                yield _done_event(
                    intent, termination_reason=None, draft_plan_id=target.reused.id
                )
                return

        updates: dict[str, Any] = {}
        async for chunk in graph.astream(
            {**state, "intent": intent, "draft_plan_id": target.replacement_id},
            config,
            context=run,
            stream_mode="updates",
        ):
            for name, update in chunk.items():
                if name == INTERRUPT_EVENT_KEY:
                    for pending in update:
                        if not isinstance(pending, Interrupt):
                            continue
                        if (event := _waiting_event(pending.value)) is not None:
                            yield event
                    continue
                if isinstance(update, Mapping):
                    updates.update(update)
                yield AgentEvent("node", {"name": name})
        termination_reason = updates.get("termination_reason")
        if termination_reason == "safety_stop":
            yield AgentEvent("message", {"text": SAFETY_STOP_MESSAGE})
        elif termination_reason == "reject_draft":
            yield AgentEvent("message", {"text": REJECT_DRAFT_MESSAGE})
        yield _done_event(
            intent,
            termination_reason=termination_reason,
            draft_plan_id=updates.get("draft_plan_id", target.replacement_id),
        )


async def invoke_agent_run(
    graph: CompiledStateGraph,
    state: WorkflowState,
    config: RunnableConfig,
    run: GeneratePlanRun,
    deps: AgentRunDeps,
) -> AgentRunResult:
    """一次 Agent Run 的唯一入口（收集形态）：把事件流收成结果对象。"""
    intent: Intent | None = None
    termination_reason: TerminationReason | None = None
    draft_plan_id: int | None = None
    produced_done = False
    messages: list[str] = []
    async for event in stream_agent_run(graph, state, config, run, deps):
        if event.event == "message":
            messages.append(str(event.data["text"]))
        elif event.event == "done":
            produced_done = True
            intent = event.data["intent"]
            termination_reason = event.data["termination_reason"]
            draft_plan_id = event.data["draft_plan_id"]
    if not produced_done:
        raise RuntimeError("Agent Run 没有产出 done 事件：调用方不得把它当成正常结束")
    return AgentRunResult(
        intent=intent,
        messages=tuple(messages),
        termination_reason=termination_reason,
        draft_plan_id=draft_plan_id,
    )


def _done_event(
    intent: Intent | None,
    *,
    termination_reason: TerminationReason | None,
    draft_plan_id: int | None,
) -> AgentEvent:
    """``done`` 事件的唯一构造：正常结束的四个键由 §3.8 表格给出，不增不减。"""
    return AgentEvent(
        "done",
        {
            "ok": True,
            "intent": intent,
            "termination_reason": termination_reason,
            "draft_plan_id": draft_plan_id,
        },
    )


def _waiting_event(payload: Any) -> AgentEvent | None:
    """``waiting`` 事件：只取 interrupt 载荷里的 ``draft_plan_id``（§3.8）；形状不符就不发。"""
    if isinstance(payload, Mapping) and isinstance(payload.get("draft_plan_id"), int):
        return AgentEvent("waiting", {"draft_plan_id": payload["draft_plan_id"]})
    return None


async def _existing_draft_target(
    intent: Intent, *, regenerate: bool, deps: AgentRunDeps
) -> ExistingDraftTarget:
    """判定已有唯一 draft 的处置；冲突即 :class:`PlanDraftConflict`，不调模型、不写入。"""
    draft = await deps.persistence.get_unique_draft()
    if draft is None:
        return ExistingDraftTarget()
    is_adjust_draft = draft.source_plan_id is not None
    if is_adjust_draft != (intent == ADJUST_PLAN_INTENT):
        raise PlanDraftConflict(
            f"已有{'调整' if is_adjust_draft else '生成'}计划 draft，不能按本次 intent 替换：{draft.id}"
        )
    if not regenerate:
        if intent == ADJUST_PLAN_INTENT:
            raise PlanDraftConflict(
                f"已有调整计划 draft，普通调整请求不覆盖既有 draft：{draft.id}"
            )
        return ExistingDraftTarget(reused=draft)
    if intent == ADJUST_PLAN_INTENT:
        active = await deps.plans.read_active()
        if active is None or active.id != draft.source_plan_id:
            raise PlanDraftConflict(
                "既有调整计划 draft 的来源计划已不是当前 active："
                f"source_plan_id={draft.source_plan_id}"
            )
    return ExistingDraftTarget(replacement_id=draft.id)


async def _non_plan_text(
    intent: Intent,
    route: FitnessIntent,
    request: str,
    *,
    run: GeneratePlanRun,
    deps: AgentRunDeps,
) -> str:
    """非计划分支的可见文本：表单引导、统计解释、日程查询、知识问答与一般对话。"""
    if intent == "view_progress":
        return await _progress_explanation(request, run=run, deps=deps)
    if intent == "view_schedule":
        return await _schedule_text(route, run=run, deps=deps)
    if intent == "knowledge_qa":
        return await _knowledge_answer(route, request, run=run, deps=deps)
    if intent == "general":
        return await _general_answer(request, run=run, deps=deps)
    return FORM_RECORD_GUIDE


@dataclass(frozen=True, slots=True)
class NaturalLanguageRecordOutcome:
    """自然语言打卡分支的一次结果：可见摘要 ＋ ``waiting`` 载荷。"""

    message: str
    waiting: dict[str, Any] | None


async def _natural_language_record(
    request: str, *, run: GeneratePlanRun, deps: AgentRunDeps
) -> NaturalLanguageRecordOutcome:
    """自然语言打卡的唯一实现：提取 → 校验 → 候选日程 → 可读摘要。"""
    catalog = await deps.catalog.list_all()
    extraction = await request_structured_model(
        deps.model,
        NATURAL_LANGUAGE_RECORD_EXTRACTION_PROMPT,
        {
            "request": request,
            **history_payload(run.conversation_messages),
            "business_day": run.business_day.isoformat(),
            "actions": [_action_payload(exercise) for exercise in catalog],
        },
        run.budget,
        ExtractedWorkout,
    )
    try:
        day, facts = await deps.records.validate_record_facts(
            extraction.performed_on,
            tuple(_workout_set_input(item) for item in extraction.sets),
        )
    except (InvalidRecordFact, UnknownExercise, RecordLoadMismatch) as error:
        return NaturalLanguageRecordOutcome(
            message=invalid_natural_language_record_message(error), waiting=None
        )
    candidates = await deps.records.list_unfinished_plan_sessions(day)
    workout = _workout_payload(day, facts)
    candidate_payloads = [
        _candidate_plan_session_payload(session) for session in candidates
    ]
    message = (
        await request_model(
            deps.model,
            NATURAL_LANGUAGE_RECORD_MESSAGE_PROMPT,
            {
                "request": request,
                **history_payload(run.conversation_messages),
                "workout": workout,
                "candidate_plan_sessions": candidate_payloads,
            },
            run.budget,
        )
    ).strip()
    return NaturalLanguageRecordOutcome(
        message=message,
        waiting={
            "workout": workout,
            "candidate_plan_sessions": candidate_payloads,
        },
    )


def _workout_set_input(item: ExtractedWorkoutSet) -> WorkoutSetInput:
    """提取结果的一条组 → 写入侧的组事实。"""
    return WorkoutSetInput(
        exercise_id=item.exercise_id,
        set_no=item.set_no,
        reps=item.reps,
        set_type=item.set_type,
        load_convention=item.load_convention,
        weight_kg=item.weight_kg,
        duration_seconds=item.duration_seconds,
    )


def _action_payload(exercise: Exercise) -> dict[str, Any]:
    """可读动作目录的一行：模型据此把自然语言动作匹配为稳定 ``exercise_id``。"""
    return {
        "exercise_id": exercise.id,
        "standard_name_zh": exercise.standard_name_zh,
        "record_type": exercise.record_type,
        "load_convention": exercise.load_convention,
    }


def _workout_payload(day: date, facts: Sequence[WorkoutSetInput]) -> dict[str, Any]:
    """``waiting.workout``（前端确认 UI 的编辑数据源）：日期、组事实与关联日程默认值。"""
    return {
        "performed_on": day.isoformat(),
        "sets": [
            {
                "exercise_id": fact.exercise_id,
                "set_no": fact.set_no,
                "set_type": fact.set_type,
                "load_convention": fact.load_convention,
                "weight_kg": fact.weight_kg,
                "reps": fact.reps,
                "duration_seconds": fact.duration_seconds,
            }
            for fact in facts
        ],
        "plan_session_id": None,
        "auto_link": True,
    }


def _candidate_plan_session_payload(session: PlanSession) -> dict[str, Any]:
    """``waiting.candidate_plan_sessions`` 的一行。"""
    return {
        "id": session.id,
        "plan_id": session.plan_id,
        "scheduled_on": session.scheduled_on.isoformat(),
    }


async def _schedule_text(
    route: FitnessIntent, *, run: GeneratePlanRun, deps: AgentRunDeps
) -> str:
    """``view_schedule`` 的唯一实现：active 计划草案按查询日确定性成文，不调模型、不写库。"""
    target = _schedule_target(route, run.business_day)
    active = await deps.plans.read_active()
    if active is None:
        return NO_ACTIVE_PLAN_SCHEDULE_MESSAGE
    draft = PlanDraft.model_validate(active.structured_content)
    day = next(
        (item for item in draft.training_days if item.scheduled_on == target), None
    )
    if day is None:
        training_days = "、".join(
            item.scheduled_on.isoformat() for item in draft.training_days
        )
        return (
            f"{target.isoformat()} 不是计划里的训练日，当天按休息安排；"
            f"本次计划的训练日为 {training_days}。"
        )
    catalog = {exercise.id: exercise for exercise in await deps.catalog.list_all()}
    lines = [f"{target.isoformat()} 的训练安排（计划目标：{draft.goal}）："]
    lines.extend(
        f"- {_exercise_name(catalog, planned.exercise_id)}：{planned.sets} 组，"
        f"{_prescription_text(planned.prescription)}"
        for planned in day.exercises
    )
    return "\n".join(lines)


def _schedule_target(route: FitnessIntent, business_day: date) -> date:
    """查询日 → 业务日期；``schedule_day`` 缺失属于无法成文的组合。"""
    day = route.schedule_day
    if day is None:
        raise ValueError("view_schedule 缺少 schedule_day：Schema 已拒绝该组合")
    return business_day + timedelta(days=SCHEDULE_DAY_OFFSETS[day])


def _exercise_name(catalog: Mapping[str, Exercise], exercise_id: str) -> str:
    """动作显示名：目录命中用 ``standard_name_zh``，未命中回落到稳定 ``exercise_id``。"""
    exercise = catalog.get(exercise_id)
    return exercise_id if exercise is None else exercise.standard_name_zh


def _prescription_text(prescription: Prescription) -> str:
    """处方 → 确定性文本：三种处方各自给出次数区间与负荷或时长。"""
    if isinstance(prescription, WeightedRepsPrescription):
        load = prescription.load
        weight = (
            "重量待校准"
            if isinstance(load, NeedsCalibration)
            else f"{load.weight_kg:g} kg"
        )
        return f"{_reps_text(prescription)}，{weight}"
    if isinstance(prescription, BodyweightRepsPrescription):
        return f"{_reps_text(prescription)}（自重）"
    if isinstance(prescription, TimedPrescription):
        return (
            f"{prescription.duration_seconds_min}–"
            f"{prescription.duration_seconds_max} 秒"
        )
    raise ValueError(f"未知处方类型：{prescription!r}")


def _reps_text(prescription: RepsPrescription) -> str:
    """次数区间文本：上下限相同时只写一个值。"""
    if prescription.reps_min == prescription.reps_max:
        return f"{prescription.reps_min} 次"
    return f"{prescription.reps_min}–{prescription.reps_max} 次"


async def _knowledge_answer(
    route: FitnessIntent, request: str, *, run: GeneratePlanRun, deps: AgentRunDeps
) -> str:
    """``knowledge_qa`` 的唯一实现：知识类型决定装配的知识源，模型只作答、不写库。"""
    methodology = route.knowledge_type == "methodology"
    skills = (
        [
            _skill_payload(deps.skills.load(name))
            for name in (PLANNING_SKILL_NAME, ADJUSTMENT_SKILL_NAME)
        ]
        if methodology
        else []
    )
    matched = (
        None
        if methodology
        else _matched_exercise(await deps.catalog.list_all(), route.exercise_name)
    )
    text = await request_model(
        deps.model,
        KNOWLEDGE_QA_SYSTEM_PROMPT,
        {
            "request": request,
            **history_payload(run.conversation_messages),
            "knowledge_type": route.knowledge_type,
            "exercise_name": route.exercise_name,
            "catalog_exercise": None if matched is None else _action_payload(matched),
            "skills": skills,
        },
        run.budget,
    )
    return text.strip()


async def _general_answer(
    request: str, *, run: GeneratePlanRun, deps: AgentRunDeps
) -> str:
    """``general`` 的唯一实现：一次文本模型调用，不写库、不进入计划子图。"""
    text = await request_model(
        deps.model,
        GENERAL_CHAT_SYSTEM_PROMPT,
        {"request": request, **history_payload(run.conversation_messages)},
        run.budget,
    )
    return text.strip()


def _matched_exercise(catalog: Sequence[Exercise], name: str | None) -> Exercise | None:
    """动作名归一化：精确匹配 ``standard_name_zh``，再取被名称完整包含的最长目录名；无命中即 None。"""
    if name is None:
        return None
    exact = [exercise for exercise in catalog if exercise.standard_name_zh == name]
    if exact:
        return exact[0]
    contained = [exercise for exercise in catalog if exercise.standard_name_zh in name]
    return max(
        contained, key=lambda exercise: len(exercise.standard_name_zh), default=None
    )


def _skill_payload(skill: LoadedSkill) -> dict[str, Any]:
    """知识问答的知识源：Skill 名称、正文与它引用的 reference 原文。"""
    return {
        "name": skill.metadata.name,
        "description": skill.metadata.description,
        "body": skill.body,
        "references": [
            {"path": reference.path, "text": reference.text}
            for reference in skill.references
        ],
    }


async def _progress_explanation(
    request: str, *, run: GeneratePlanRun, deps: AgentRunDeps
) -> str:
    """``view_progress`` 的模型解释：统计由既有 ``StatsService`` 现算，模型只解释这些数值。"""
    payload = {
        "request": request,
        **history_payload(run.conversation_messages),
        "personal_bests": [
            asdict(best) for best in await deps.stats.list_personal_bests()
        ],
        "trend_summary": asdict(await deps.stats.trend_summary(run.business_day)),
    }
    text = await request_model(
        deps.model, VIEW_PROGRESS_SYSTEM_PROMPT, payload, run.budget
    )
    return text.strip()
