import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
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

from app.application.agent.budget import (
    ModelRequestBudgetExceeded,
)
from app.application.agent.contracts import (
    ADJUST_PLAN_INTENT,
    LOCAL_USER_ID,
    PLAN_CONFIRMATION_KIND,
    AgentEvent,
    AgentEventName,
    AgentRunDeps,
    AgentRunResult,
    AgentRuntime,
    ConfirmationConflict,
    ExistingDraftTarget,
    GeneratePlanRun,
    Intent,
    RequiredActivePlanMissing,
    RequiredProfileMissing,
    TerminationReason,
    ToolExecutionContext,
    WorkflowState,
    initial_workflow_state,
    thread_config,
)
from app.application.agent.harness.registry import (
    general_skill_bundle,
    general_system_prompt,
    general_ui_actions,
)
from app.application.agent.harness.snapshot import run_fact_snapshot
from app.application.agent.harness.tools.common import TrainingHarnessContext
from app.application.agent.harness.tools.get_workout_record_form import (
    workout_form_action,
)
from app.application.agent.harness.tools.prepare_workout_record import (
    MissingWorkoutRecordCandidate,
    workout_confirmation_action,
    workout_confirmation_payload,
)
from app.application.agent.prompts import (
    DISCARD_FAILED_CANDIDATE_MESSAGE,
    GENERAL_CHAT_SYSTEM_PROMPT,
    NATURAL_LANGUAGE_RECORD_SYSTEM_PROMPT,
    TOOL_HARNESS_SYSTEM_PROMPT,
)
from app.application.agent.router import NON_PLAN_INTENTS
from app.application.agent.run_graph import (
    GENERAL_NODE,
    PLAN_NODE,
    GeneralOutcome,
    RunBranches,
    build_agent_run_graph,
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
)
from app.domain.conversations.schema import ConversationRun
from app.domain.records.rules import InvalidRecordFact
from app.infrastructure.database.connection import Database
from app.infrastructure.database.repositories.conversations_repository import (
    ConversationRepo,
)
from config import MODEL_CONTEXT_WINDOW_TOKENS


def invalid_natural_language_record_message(error: Exception) -> str:
    """确认前校验失败的可见文本：照原样给出本项目自己的领域错误，并明确本次不写库。"""
    return f"自然语言打卡未通过校验：{error}。本次不写库；请修正训练描述后重试，或改用打卡表单。"


INTERRUPT_EVENT_KEY = "__interrupt__"

#: 计划分支的非正常终止 → 可见文本。
_PLAN_TERMINATION_MESSAGES: Mapping[str, str] = {
    "discard_failed_candidate": DISCARD_FAILED_CANDIDATE_MESSAGE,
}

#: Intent → harness 系统提示词：一般对话用对话提示词，自然语言打卡用打卡提示词，其余工具 Intent 用工具循环提示词。
HARNESS_SYSTEM_PROMPTS: Mapping[Intent, str] = {
    "general": GENERAL_CHAT_SYSTEM_PROMPT,
    "natural_language_record": NATURAL_LANGUAGE_RECORD_SYSTEM_PROMPT,
}


def agent_run_graph(
    plan_graph: CompiledStateGraph, deps: AgentRunDeps
) -> CompiledStateGraph:
    """顶层图的唯一构造点：计划子图只作为 ``plan`` 分支接线，checkpointer 沿用计划子图那一份。"""
    return build_agent_run_graph(
        plan_graph=plan_graph,
        model=deps.model,
        branches=RunBranches(
            general=lambda intent, state, run: _general_branch(
                intent, state, run, deps=deps
            ),
            plan_target=lambda intent, run: _plan_target(intent, run, deps=deps),
        ),
        checkpointer=plan_graph.checkpointer,
    )


def _node_frame_name(name: str, updates: Mapping[str, Any]) -> str:
    """节点帧的阶段名：General 分支用它本次接下的 Intent，五项会话 Intent 因此在前端可见。"""
    intent = updates.get("intent")
    if name == GENERAL_NODE and isinstance(intent, str):
        return intent
    return name


def _node_frame_visible(namespace: tuple[str, ...], name: str) -> bool:
    """节点帧的可见域：顶层节点与计划子图内部节点；计划包装帧与 General 分支内部的工具节点不透出。"""
    if namespace:
        return namespace[0].startswith(f"{PLAN_NODE}:")
    return name != PLAN_NODE


async def stream_agent_run(
    graph: CompiledStateGraph,
    state: WorkflowState,
    config: RunnableConfig,
    run: GeneratePlanRun,
    deps: AgentRunDeps,
) -> AsyncIterator[AgentEvent]:
    """一次 Agent Run 的事件流：节点事件逐个透出，终态由 State 的 ``final_result`` 收敛。

    ``graph`` 是计划子图：安全扫描、路由、General 分支与作为 ``plan`` 节点接线的它本身都在
    ``agent_run_graph`` 构造的顶层图上，计划子图的节点事件（确认 interrupt 与 ``waiting`` 载荷）照旧透出。
    """
    async with asyncio.timeout(run.budget.remaining_run_seconds()):
        updates: dict[str, Any] = {}
        #: 同一 interrupt 会先随子图、再随父图各透出一次：按 interrupt 身份去重，只发一次 waiting。
        emitted_interrupts: set[str] = set()
        async for namespace, chunk in agent_run_graph(graph, deps).astream(
            state,
            config,
            context=run,
            stream_mode="updates",
            subgraphs=True,
        ):
            for name, update in chunk.items():
                if name == INTERRUPT_EVENT_KEY:
                    for pending in update:
                        if not isinstance(pending, Interrupt):
                            continue
                        if pending.id in emitted_interrupts:
                            continue
                        emitted_interrupts.add(pending.id)
                        if (event := _waiting_event(pending.value)) is not None:
                            yield event
                    continue
                if isinstance(update, Mapping):
                    updates.update(update)
                if _node_frame_visible(namespace, name):
                    yield AgentEvent(
                        "node", {"name": _node_frame_name(name, updates)}
                    )
        final_result = updates.get("final_result")
        if isinstance(final_result, AgentRunResult):
            for text in final_result.messages:
                yield AgentEvent("message", {"text": text})
            for action in updates.get("ui_actions", ()):
                # 只有 ``workout_confirmation`` 需要等用户；未标注 ``type`` 的既有计划动作照旧透出。
                if action.get("type") in (None, "workout_confirmation"):
                    yield AgentEvent("waiting", action)
            yield _done_event(
                final_result.intent,
                termination_reason=final_result.termination_reason,
                draft_plan_id=final_result.draft_plan_id,
            )
            return
        if (
            message := _PLAN_TERMINATION_MESSAGES.get(
                updates.get("termination_reason")
            )
        ) is not None:
            yield AgentEvent("message", {"text": message})
        yield _done_event(
            updates.get("intent"),
            termination_reason=updates.get("termination_reason"),
            draft_plan_id=updates.get("draft_plan_id"),
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
    """``waiting`` 事件：只取计划确认 interrupt 载荷里的 ``draft_plan_id``（§3.8）；形状不符就不发。"""
    if (
        isinstance(payload, Mapping)
        and payload.get("kind") == PLAN_CONFIRMATION_KIND
        and isinstance(payload.get("draft_plan_id"), int)
    ):
        return AgentEvent("waiting", {"draft_plan_id": payload["draft_plan_id"]})
    return None


async def _general_branch(
    intent: Intent, state: WorkflowState, run: GeneratePlanRun, *, deps: AgentRunDeps
) -> GeneralOutcome:
    """五项会话 Intent 的唯一分支：按 Intent 的只读白名单取事实，输出 message ＋ ui_actions。"""
    request = state["request"]
    if intent == "natural_language_record":
        return await _natural_language_record(request, state, run=run, deps=deps)
    if intent not in NON_PLAN_INTENTS:
        raise ValueError(f"General 分支收到未登记的会话 Intent：{intent!r}")
    messages = await _tool_messages(intent, request, run=run, deps=deps)
    return GeneralOutcome(
        message=harness_answer(messages),
        actions=(
            (workout_form_action(),)
            if intent == "form_record"
            else general_ui_actions(intent, messages)
        ),
    )


async def _plan_target(
    intent: Intent, run: GeneratePlanRun, *, deps: AgentRunDeps
) -> ExistingDraftTarget:
    """计划分支入口处置的既有实现：唯一 draft 的复用短路与替换身份判定。"""
    return await _existing_draft_target(intent, regenerate=run.regenerate, deps=deps)


async def _existing_draft_target(
    intent: Intent, *, regenerate: bool, deps: AgentRunDeps
) -> ExistingDraftTarget:
    """判定已有唯一 draft 的处置；冲突即 :class:`PlanDraftConflict`，不调模型、不写入。"""
    draft = await deps.plan_writes.get_unique_draft()
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


def harness_context(
    run: GeneratePlanRun,
    *,
    deps: AgentRunDeps,
    snapshot: ToolExecutionContext | None = None,
) -> TrainingHarnessContext:
    """General 只读工具的运行上下文：业务日、共享预算、历史投影与既有只读入口，身份不进模型参数。"""
    return TrainingHarnessContext(
        model=deps.model,
        budget=run.budget,
        business_day=run.business_day,
        profiles=deps.profiles,
        plans=deps.plans,
        catalog=deps.catalog,
        records=deps.records,
        stats=deps.stats,
        dataset=deps.dataset,
        snapshot=snapshot,
        conversation_history=run.conversation_messages,
    )


async def _tool_messages(
    intent: Intent,
    request: str,
    *,
    run: GeneratePlanRun,
    deps: AgentRunDeps,
    snapshot: ToolExecutionContext | None = None,
) -> tuple[BaseMessage, ...]:
    """工具类 Intent 的唯一实现：本次 Intent 白名单的 general_tools 出消息；调用由模型自选。

    ``snapshot`` 由需要 Run 事实快照的 Intent 传入，其余 Intent 为 None。
    """
    skill = general_skill_bundle(deps.skills, intent)
    result = await deps.tool_harnesses[intent].ainvoke(
        {
            "messages": harness_messages(
                request,
                history=run.conversation_messages,
                system_prompt=general_system_prompt(
                    HARNESS_SYSTEM_PROMPTS.get(intent, TOOL_HARNESS_SYSTEM_PROMPT).format(
                        business_day=run.business_day.isoformat()
                    ),
                    skill,
                ),
            )
        },
        context=harness_context(run, deps=deps, snapshot=snapshot),
    )
    return tuple(result["messages"])


def harness_messages(
    request: str,
    *,
    history: Sequence[ContextMessage],
    system_prompt: str,
) -> list[BaseMessage]:
    """harness 的消息序列：system ＋ 已重建历史 ＋ 当前请求（只出现一次）。"""
    return [
        SystemMessage(content=system_prompt),
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


async def _natural_language_record(
    request: str, state: WorkflowState, *, run: GeneratePlanRun, deps: AgentRunDeps
) -> GeneralOutcome:
    """自然语言打卡的唯一实现：本次 Intent 的 ToolNode loop 产出候选；无有效候选即明确失败。

    确认动作只取唯一一次成功的 ``prepare_workout_record`` 调用；领域错误与缺候选沿 Run 错误边界收敛，
    两条路径都不写业务训练表。
    """
    snapshot = await run_fact_snapshot(
        deps.revisions,
        schema_version=deps.schema_version,
        user_id=state["user_id"],
        run_id=state["run_id"],
        business_day=run.business_day,
    )
    try:
        messages = await _tool_messages(
            "natural_language_record",
            request,
            run=run,
            deps=deps,
            snapshot=snapshot,
        )
    except (InvalidRecordFact, UnknownExercise, RecordLoadMismatch) as error:
        return GeneralOutcome(message=invalid_natural_language_record_message(error))
    payload = workout_confirmation_payload(messages)
    return GeneralOutcome(
        message=harness_answer(messages),
        actions=(workout_confirmation_action(payload),),
    )


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
        initial_workflow_state(
            run_id=run.id,
            conversation_id=run.conversation_id,
            user_id=LOCAL_USER_ID,
            client_request_id=run.client_request_id,
            business_day=business_day,
            request=request,
        ),
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

#: 自然语言打卡没有唯一一次成功的 ``prepare_workout_record`` 调用：不产生确认动作，也不写库。
MISSING_WORKOUT_RECORD_CANDIDATE_MESSAGE = "模型未产出有效打卡候选，本次不写库，请重试"

_FIXED_ERROR_MESSAGES: tuple[tuple[type[Exception], str], ...] = (
    (ModelConfigurationError, MODEL_CONFIGURATION_ERROR_MESSAGE),
    (InvalidModelResponse, INVALID_MODEL_RESPONSE_MESSAGE),
    (MissingWorkoutRecordCandidate, MISSING_WORKOUT_RECORD_CANDIDATE_MESSAGE),
)

_PRODUCT_ERROR_TYPES: tuple[type[Exception], ...] = (
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
