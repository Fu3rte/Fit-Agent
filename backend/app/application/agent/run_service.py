import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, cast
from uuid import uuid4

from anyio import CancelScope
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Interrupt
from pydantic import BaseModel, ConfigDict

from app.application.agent.budget import (
    ModelRequestBudgetExceeded,
    request_model,
    request_structured_model,
)
from app.application.agent.contracts import (
    ADJUST_PLAN_INTENT,
    ADJUSTMENT_SKILL_NAME,
    PLANNING_SKILL_NAME,
    AgentEvent,
    AgentEventName,
    AgentRunDeps,
    AgentRunResult,
    AgentRuntime,
    AmbiguousExerciseName,
    ConfirmationConflict,
    ExistingDraftTarget,
    GeneratePlanRun,
    Intent,
    LoadedSkill,
    RequiredActivePlanMissing,
    RequiredProfileMissing,
    TerminationReason,
    WorkflowState,
    thread_config,
)
from app.application.agent.harness.tools.training import TrainingHarnessContext
from app.application.agent.prompts import (
    FORM_RECORD_GUIDE,
    GENERAL_CHAT_SYSTEM_PROMPT,
    KNOWLEDGE_QA_SYSTEM_PROMPT,
    NATURAL_LANGUAGE_RECORD_EXTRACTION_PROMPT,
    NATURAL_LANGUAGE_RECORD_MESSAGE_PROMPT,
    REJECT_DRAFT_MESSAGE,
    SAFETY_STOP_MESSAGE,
    TOOL_HARNESS_SYSTEM_PROMPT,
)
from app.application.agent.router import (
    NON_PLAN_INTENTS,
    TOOL_INTENTS,
    FitnessIntent,
    classify_intent,
    workflow_intent,
)
from app.application.ports import (
    Conversations,
    InvalidModelResponse,
    ModelCallFailed,
    ModelConfigurationError,
    TransientModelError,
)
from app.application.services.plans_service import (
    PlanActivationConflict,
    PlanDraftConflict,
    PlanDraftStale,
    PlanNotFound,
    PlanRevalidationFailed,
)
from app.domain.actions.rules import RecordLoadMismatch, UnknownExercise
from app.domain.actions.schema import Exercise, LoadConvention
from app.domain.conversations.compaction import (
    DEFAULT_COMPACTION_SETTINGS,
    compact,
    estimate_context_tokens,
    prepare_compaction,
    should_compact,
)
from app.domain.conversations.context import (
    ContextMessage,
    build_context_entries,
    context_messages,
    history_payload,
)
from app.domain.conversations.schema import ConversationRun
from app.domain.plans.schema import PlanSession
from app.domain.profile.safety import message_red_flag_hits
from app.domain.records.rules import InvalidRecordFact
from app.domain.records.schema import SetType, WorkoutSetInput
from app.infrastructure.database.connection import Database
from app.infrastructure.database.repositories.conversations_repository import (
    ConversationRepo,
)
from config import MODEL_CONTEXT_WINDOW_TOKENS


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


def invalid_natural_language_record_message(error: Exception) -> str:
    """确认前校验失败的可见文本：照原样给出本项目自己的领域错误，并明确本次不写库。"""
    return f"自然语言打卡未通过校验：{error}。本次不写库；请修正训练描述后重试，或改用打卡表单。"


INTERRUPT_EVENT_KEY = "__interrupt__"


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
                text = (
                    await _tool_answer(intent, state["request"], run=run, deps=deps)
                    if intent in TOOL_INTENTS
                    else await _non_plan_text(
                        intent, route, state["request"], run=run, deps=deps
                    )
                )
                yield AgentEvent("message", {"text": text})
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


async def _tool_answer(
    intent: Intent, request: str, *, run: GeneratePlanRun, deps: AgentRunDeps
) -> str:
    """``view_schedule``／``view_progress`` 的唯一实现：只读工具 harness 产出最终可见文本。"""
    context = TrainingHarnessContext(
        model=deps.model,
        budget=run.budget,
        business_day=run.business_day,
        plans=deps.plans,
        catalog=deps.catalog,
        records=deps.records,
        stats=deps.stats,
    )
    result = await deps.tool_harnesses[intent].ainvoke(
        {
            "messages": harness_messages(
                request,
                business_day=run.business_day,
                history=run.conversation_messages,
            )
        },
        context=context,
    )
    return harness_answer(result["messages"])


def harness_messages(
    request: str, *, business_day: date, history: Sequence[ContextMessage]
) -> list[BaseMessage]:
    """harness 的消息序列：业务日 system ＋ 已重建历史 ＋ 当前请求（只出现一次）。"""
    return [
        SystemMessage(
            content=TOOL_HARNESS_SYSTEM_PROMPT.format(
                business_day=business_day.isoformat()
            )
        ),
        *(
            HumanMessage(content=message.text)
            if message.role == "user"
            else AIMessage(content=message.text)
            for message in history
        ),
        HumanMessage(content=request),
    ]


def harness_answer(messages: Sequence[BaseMessage]) -> str:
    """harness 的最终可见文本：最后一个 ``AIMessage`` 的字符串 content。"""
    final = next(
        (
            message
            for message in reversed(messages)
            if isinstance(message, AIMessage)
        ),
        None,
    )
    if final is None:
        raise InvalidModelResponse("工具回答没有产生最终 AIMessage")
    if not isinstance(final.content, str):
        raise InvalidModelResponse("工具回答的最终响应不是纯文本：无法作为可见文本使用")
    return final.content.strip()


async def _non_plan_text(
    intent: Intent,
    route: FitnessIntent,
    request: str,
    *,
    run: GeneratePlanRun,
    deps: AgentRunDeps,
) -> str:
    """非计划分支的可见文本：表单引导、知识问答与一般对话。"""
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
        "aliases": list(exercise.aliases),
        "equipment_variant": exercise.equipment_variant,
        "modes": list(exercise.modes),
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
    matched: Exercise | None = None
    if not methodology:
        catalog = await deps.catalog.list_all()
        matched = _matched_exercise(
            tuple(exercise for exercise in catalog if exercise.recommendable),
            route.exercise_name,
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
    """动作名归一化：标准名精确 → alias 精确 → 标准名完整包含 → alias 完整包含；无命中即 None。

    精确阶段命中多个候选立即报歧义，不自行挑选；包含阶段按级取最长名称，
    标准名包含非空时不再看 alias 包含。
    """
    if name is None:
        return None
    exact = [exercise for exercise in catalog if exercise.standard_name_zh == name]
    if not exact:
        exact = [exercise for exercise in catalog if name in exercise.aliases]
    if exact:
        if len(exact) > 1:
            raise AmbiguousExerciseName()
        return exact[0]
    contained: list[tuple[int, Exercise]] = [
        (len(exercise.standard_name_zh), exercise)
        for exercise in catalog
        if exercise.standard_name_zh in name
    ]
    if not contained:
        contained = [
            (len(alias), exercise)
            for exercise in catalog
            for alias in exercise.aliases
            if alias in name
        ]
    if not contained:
        return None
    return max(contained, key=lambda item: item[0])[1]


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


async def run_events(
    runtime: AgentRuntime,
    db: Database,
    conversations: ConversationRepo,
    *,
    run: ConversationRun,
    request: str,
    regenerate: bool,
    business_day: date,
    now: Callable[[], str],
) -> AsyncIterator[AgentEvent]:
    """已落库用户 Entry 之后的运行链：先按阈值压缩，再重建上下文，最后委托工作流。

    只有摘要生成失败按 Pi ``_runAutoCompaction`` 的自动压缩失败语义处理（agent-session.ts:2500-2525
    捕获后返回 false）：本轮不压缩、原 Entry 继续用于本次模型调用。阈值判定、切点准备、数据库
    append 与 append 后重读的异常一律向外传播，由 :func:`persisted_events` 把 Run 收敛为
    ``failed`` 并发 ``error`` 事件：SQLite 是会话事实源，提交失败不得继续 Graph。
    """
    chat_id = run.conversation_id
    entries = await conversations.list_entries(chat_id)
    if should_compact(
        estimate_context_tokens(build_context_entries(entries)),
        MODEL_CONTEXT_WINDOW_TOKENS,
        DEFAULT_COMPACTION_SETTINGS,
    ):
        preparation = prepare_compaction(entries, DEFAULT_COMPACTION_SETTINGS)
        if preparation is not None:
            try:
                result = await compact(preparation, runtime.run_deps.model.text)
            except Exception:
                # 摘要生成失败：本轮不写 compaction Entry，原全部 Entry 继续参与本轮模型调用。
                pass
            else:
                async with db.transaction() as conn:
                    await conversations.append_entry_in_transaction(
                        conn,
                        conversation_id=chat_id,
                        entry_id=str(uuid4()),
                        entry_type="compaction",
                        payload=result.to_payload(),
                        created_at=now(),
                    )
                entries = await conversations.list_entries(chat_id)
    # 当前用户 Entry 只通过本次 state 的 request 出现一次：投影前按身份精确排除它。
    history = context_messages(
        tuple(
            entry
            for entry in build_context_entries(entries)
            if entry.id != run.user_entry_id
        )
    )
    async with db.transaction() as conn:
        await conversations.update_run_status_in_transaction(
            conn, run.id, status="running", updated_at=now()
        )
    async for event in stream_agent_run(
        runtime.graph,
        {"conversation_id": run.thread_id, "request": request},
        thread_config(run.thread_id),
        GeneratePlanRun(
            business_day=business_day,
            regenerate=regenerate,
            conversation_messages=history,
        ),
        runtime.run_deps,
    ):
        yield event


async def persisted_events(
    db: Database,
    conversations: ConversationRepo,
    run_id: str,
    events: AsyncIterator[AgentEvent],
    *,
    now: Callable[[], str],
) -> AsyncIterator[AgentEvent]:
    """事件流 → 已落库的产品事件：每个语义事件先落库再交给成帧层，运行错误只发一个 ``error`` 事件。

    客户端断开（``asyncio.CancelledError``）按取消语义收敛 Run，不发事件、不写 Assistant Entry。
    """
    sequence = 0
    messages: list[str] = []
    try:
        async for event in events:
            # 先落库再推进序号：落库回滚时该序号仍空缺，失败事件与 error 事件共用它。
            await _record_event(
                db, conversations, run_id, event, sequence + 1, messages, now=now
            )
            sequence += 1
            if event.event == "message":
                messages.append(str(event.data["text"]))
            yield event
    except asyncio.CancelledError:
        # 客户端断开：生成器事件内的取消走同一次收敛；事件外交出的取消由 ``streaming`` 的
        # 下游 send 边界收尾。
        await converge_cancelled_run(conversations, run_id, now=now)
        raise
    except Exception as error:
        message = agent_run_error_message(error)
        async with db.transaction() as conn:
            await conversations.update_run_with_event_in_transaction(
                conn,
                run_id,
                event_type="error",
                sequence=sequence + 1,
                payload={"message": message},
                status="failed",
                error_code=type(error).__name__,
                created_at=now(),
            )
        yield AgentEvent("error", {"message": message})


async def converge_cancelled_run(
    conversations: Conversations, run_id: str, *, now: Callable[[], str]
) -> None:
    # AnyIO 在已取消作用域内反复重投取消，shield 保证收敛事务跑完。
    with CancelScope(shield=True):
        await conversations.cancel_active_run(run_id, updated_at=now())


async def append_confirmation(
    db: Database,
    conversations: ConversationRepo,
    *,
    source_run: ConversationRun,
    action: Literal["plan_confirmed", "plan_rejected", "workout_confirmed"],
    text: str,
    draft_plan_id: int | None = None,
    workout_session_id: int | None = None,
    now: Callable[[], str],
) -> None:
    """业务事务成功后的确认 Entry：绑定精确来源 Run；同一 Run／动作／业务身份只写一次。"""
    async with db.transaction() as conn:
        await conversations.append_confirmation_once_in_transaction(
            conn,
            conversation_id=source_run.conversation_id,
            run_id=source_run.id,
            entry_id=str(uuid4()),
            action=action,
            text=text,
            draft_plan_id=draft_plan_id,
            workout_session_id=workout_session_id,
            created_at=now(),
        )


async def _record_event(
    db: Database,
    conversations: ConversationRepo,
    run_id: str,
    event: AgentEvent,
    sequence: int,
    messages: Sequence[str],
    *,
    now: Callable[[], str],
) -> None:
    """按事件类型落库：``waiting``／``done`` 的状态语义和事件同事务提交。"""
    created_at = now()
    async with db.transaction() as conn:
        if event.event == "waiting":
            await conversations.update_run_with_event_in_transaction(
                conn,
                run_id,
                event_type="waiting",
                sequence=sequence,
                payload=event.data,
                status="waiting",
                created_at=created_at,
            )
            return
        if event.event == "done":
            # 有可见输出且未进入 waiting 时才写完整 Assistant Entry：内容与状态同事务判定；
            # 非 waiting 的空白内容由 repo 在写 done 之前拒绝，本帧据此转成 ``error``。
            await conversations.finish_run_in_transaction(
                conn,
                run_id,
                sequence=sequence,
                payload=event.data,
                content="\n\n".join(messages),
                entry_id=str(uuid4()),
                created_at=created_at,
            )
            return
        await conversations.append_event_in_transaction(
            conn,
            run_id=run_id,
            sequence=sequence,
            event_type=event.event,
            payload=event.data,
            created_at=created_at,
        )


async def replay_events(
    conversations: Conversations, run_id: str
) -> AsyncIterator[AgentEvent]:
    """幂等重放：同一 ``client_request_id`` 重发已提交事件，不重新执行、不重复写库。"""
    for event in await conversations.list_run_events(run_id):
        yield AgentEvent(cast(AgentEventName, event.event_type), event.payload)


MODEL_CONFIGURATION_ERROR_MESSAGE = (
    "模型服务未配置：本次运行未产生计划写入，请检查服务端模型配置后重试"
)

INVALID_MODEL_RESPONSE_MESSAGE = (
    "模型响应不符合统一 Schema：本次运行未产生计划写入，请稍后重试"
)

_FIXED_ERROR_MESSAGES: tuple[tuple[type[Exception], str], ...] = (
    (ModelConfigurationError, MODEL_CONFIGURATION_ERROR_MESSAGE),
    (InvalidModelResponse, INVALID_MODEL_RESPONSE_MESSAGE),
)

_PRODUCT_ERROR_TYPES: tuple[type[Exception], ...] = (
    AmbiguousExerciseName,
    RequiredProfileMissing,
    RequiredActivePlanMissing,
    ConfirmationConflict,
    PlanNotFound,
    PlanActivationConflict,
    PlanDraftStale,
    PlanRevalidationFailed,
    PlanDraftConflict,
    ModelRequestBudgetExceeded,
    ModelCallFailed,
    TransientModelError,
)

AGENT_RUN_ERROR_MESSAGE = "本次运行失败：未产生可激活计划，请稍后重试"


def agent_run_error_message(error: BaseException) -> str:
    """SSE ``error`` 事件的可见文本：本项目产品错误照原样，其余一律固定文本。"""
    for error_type, message in _FIXED_ERROR_MESSAGES:
        if isinstance(error, error_type):
            return message
    if isinstance(error, _PRODUCT_ERROR_TYPES):
        return str(error)
    return AGENT_RUN_ERROR_MESSAGE
