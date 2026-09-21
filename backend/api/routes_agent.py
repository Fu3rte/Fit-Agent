import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, cast
from uuid import uuid4

from anyio import CancelScope
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from langgraph.graph.state import CompiledStateGraph
from starlette.types import Send

from api.deps import current_business_date, iso_now
from api.dto import (
    AgentPlanBody,
    AgentRunBody,
    ConfirmWorkoutBody,
    InvalidRequestShape,
    personal_best_dto,
    plan_dto,
    record_dto,
    workout_set_inputs_from_dto,
)
from config import MODEL_CONTEXT_WINDOW_TOKENS
from domain.conversations.compaction import (
    DEFAULT_COMPACTION_SETTINGS,
    compact,
    estimate_context_tokens,
    prepare_compaction,
    should_compact,
)
from domain.conversations.context import build_context_entries, context_messages
from domain.conversations.repo import ConversationNotFound, ConversationRepo
from domain.conversations.schema import ConversationRun
from domain.plans.schema import Plan
from domain.plans.service import (
    PlanActivationConflict,
    PlanDraftConflict,
    PlanDraftStale,
    PlanNotFound,
    PlanRevalidationFailed,
)
from domain.records.service import WorkoutRecordNotFound, WorkoutRecordsService
from domain.stats.service import StatsService
from graph.checkpointer import thread_config
from graph.model import InvalidModelResponse, ModelCallFailed
from graph.nodes import (
    ConfirmationConflict,
    GeneratePlanDeps,
    GeneratePlanRun,
    ModelRequestBudgetExceeded,
    RequiredActivePlanMissing,
    RequiredProfileMissing,
)
from graph.workflow import (
    AgentEvent,
    AgentEventName,
    AgentRunDeps,
    AmbiguousExerciseName,
    invoke_confirmation,
    stream_agent_run,
)
from provider_settings import ModelConfigurationError
from storage.db import Database

router = APIRouter()


@dataclass(frozen=True, slots=True)
class AgentRuntime:
    """Agent 三端点的装配。"""

    graph: CompiledStateGraph
    deps: GeneratePlanDeps
    run_deps: AgentRunDeps


class _AgentRunStream(StreamingResponse):
    """客户端断开的收敛覆盖到下游 ``send`` 边界。"""

    def __init__(
        self, content: AsyncIterator[str], *, conversations: ConversationRepo, run_id: str
    ) -> None:
        super().__init__(content, media_type="text/event-stream")
        self._conversations = conversations
        self._run_id = run_id

    async def stream_response(self, send: Send) -> None:
        try:
            await super().stream_response(send)
        except asyncio.CancelledError:
            await _converge_cancelled_run(self._conversations, self._run_id)
            raise


@router.post("/api/agent/run")
async def run_agent(
    body: AgentRunBody,
    request: Request,
    business_day: date = Depends(current_business_date),
) -> StreamingResponse:
    """一次 Agent Run：``text/event-stream`` 响应，五类产品事件。

    已提交的消息与 Run 在模型调用之前落库；每个语义事件先落库再发送；同一
    ``client_request_id`` 重放时把已提交的事件原样重发，不重新调模型。
    """
    runtime = _runtime(request)
    db: Database = request.app.state.db
    conversations = ConversationRepo(db)
    chat_id = str(body.chat_id)
    thread_id = str(body.conversation_id)
    conversation = await conversations.read_conversation(chat_id)
    if conversation is None:
        raise ConversationNotFound(f"会话不存在：{chat_id}")
    existing = await conversations.read_run_by_client_request_id(body.client_request_id)
    if existing is not None:
        _require_same_identity(existing, chat_id=chat_id, thread_id=thread_id)
        return StreamingResponse(
            _replay_frames(conversations, existing.id), media_type="text/event-stream"
        )
    run_id = str(uuid4())
    entry_id = str(uuid4())
    created_at = iso_now()
    async with db.transaction() as conn:
        run, _user_entry = await conversations.begin_run_in_transaction(
            conn,
            conversation_id=chat_id,
            run_id=run_id,
            thread_id=thread_id,
            client_request_id=body.client_request_id,
            entry_id=entry_id,
            content=body.request,
            created_at=created_at,
        )
    if run.user_entry_id != entry_id:
        # 并发重放：预读未命中而事务内判定已存在同一 ``client_request_id`` 的 Run，
        # 不改状态、不调模型，只重发它已提交的事件。
        _require_same_identity(run, chat_id=chat_id, thread_id=thread_id)
        return StreamingResponse(
            _replay_frames(conversations, run.id), media_type="text/event-stream"
        )
    # 压缩、上下文重建与模型调用都不在本请求的数据库事务内；三段顺序固定在 ``_run_events`` 里。
    return _AgentRunStream(
        _persisted_frames(
            db,
            conversations,
            run.id,
            _run_events(runtime, db, run=run, body=body, business_day=business_day),
        ),
        conversations=conversations,
        run_id=run.id,
    )


@router.post("/api/agent/confirm")
async def confirm_plan(
    body: AgentPlanBody,
    request: Request,
    business_day: date = Depends(current_business_date),
) -> dict[str, Any]:
    """用户确认：激活 draft，返回落库后的计划行（幂等已 active 时返回既有行，§3.2／§3.3）。"""
    source_run = await _confirmation_source_run(body, request)
    plan = await _confirmation(body, request, "confirm", business_day)
    await _append_confirmation(
        request,
        source_run=source_run,
        action="plan_confirmed",
        draft_plan_id=body.plan_id,
        text=f"用户已确认训练计划：计划 ID {body.plan_id}，本次确认后该计划为当前训练计划。",
    )
    return {"plan": plan_dto(plan)}


@router.post("/api/agent/reject")
async def reject_plan(
    body: AgentPlanBody,
    request: Request,
    business_day: date = Depends(current_business_date),
) -> dict[str, Any]:
    """用户拒绝：把 draft 归档，返回落库后的计划行（原 active 不变，永不写 ``rejected``，§3.2）。"""
    source_run = await _confirmation_source_run(body, request)
    plan = await _confirmation(body, request, "reject", business_day)
    await _append_confirmation(
        request,
        source_run=source_run,
        action="plan_rejected",
        draft_plan_id=body.plan_id,
        text=f"用户已拒绝训练计划：计划 ID {body.plan_id}，该草案已归档，未被采纳。",
    )
    return {"plan": plan_dto(plan)}


@router.post("/api/agent/confirm-workout")
async def confirm_workout(
    body: ConfirmWorkoutBody, request: Request
) -> dict[str, Any]:
    """自然语言打卡确认写入：复用表单写入服务；同一来源 Run 的重放返回已写入的记录与 PB。"""
    db = request.app.state.db
    conversations = ConversationRepo(db)
    source_run = await _confirmation_source_run(body, request)
    existing = await conversations.read_confirmations(
        source_run.id, "workout_confirmed"
    )
    if existing:
        # 重放：来源 Run 已确认过一次，不再写第二条训练记录，也不再追加确认 Entry。
        return await _confirmed_workout_response(
            db, int(existing[0].payload["workout_session_id"])
        )
    session = await WorkoutRecordsService(db).create(
        body.performed_on,
        workout_set_inputs_from_dto(body.sets),
        plan_session_id=body.plan_session_id,
        auto_link=body.auto_link,
    )
    await _append_confirmation(
        request,
        source_run=source_run,
        action="workout_confirmed",
        workout_session_id=session.id,
        text=(
            f"用户已确认写入训练记录：记录 ID {session.id}，"
            f"训练日期 {body.performed_on.isoformat()}。"
        ),
    )
    return await _confirmed_workout_response(db, session.id)


def _runtime(request: Request) -> AgentRuntime:
    """取回本 app 的 Agent 装配（lifespan 装配一次；不按请求重新编译图或重建服务）。"""
    runtime: AgentRuntime | None = getattr(
        request.app.state, "agent_runtime", None
    )
    if runtime is None:
        raise RuntimeError("Agent 运行时未装配：create_app 的 lifespan 尚未启动")
    return runtime


async def _confirmation(
    body: AgentPlanBody,
    request: Request,
    action: Literal["confirm", "reject"],
    business_day: date,
) -> Plan:
    """两条确认路径共用唯一确认入口：checkpoint 优先 ＋ 唯一 draft 兜底 ＋ 领域幂等。"""
    runtime = _runtime(request)
    return await invoke_confirmation(
        runtime.graph,
        conversation_id=str(body.conversation_id),
        plan_id=body.plan_id,
        action=action,
        run=GeneratePlanRun(business_day=business_day),
        deps=runtime.deps,
    )


async def _confirmation_source_run(
    body: AgentPlanBody | ConfirmWorkoutBody, request: Request
) -> ConversationRun:
    """确认动作的唯一前置校验：会话存在且 ``(chat_id, conversation_id)`` 解析出精确来源 Run。

    任何不匹配都在计划／打卡业务写入之前失败，确认 Entry 因此绝不绑定“最近一轮”。
    """
    conversations = ConversationRepo(request.app.state.db)
    chat_id = str(body.chat_id)
    thread_id = str(body.conversation_id)
    run = await conversations.read_run_by_thread_id(thread_id)
    if run is None:
        raise InvalidRequestShape(
            f"确认请求的 thread 没有来源 Run：thread={thread_id!r}"
        )
    _require_same_identity(run, chat_id=chat_id, thread_id=thread_id)
    return run


async def _confirmed_workout_response(
    db: Database, session_id: int
) -> dict[str, Any]:
    """确认写入的响应：按身份读回既有训练记录 ＋ 既有 Stats 现算 PB；记录已删除即领域 404。"""
    session = await WorkoutRecordsService(db).get(session_id)
    if session is None:
        raise WorkoutRecordNotFound(f"训练记录不存在：{session_id}")
    bests = await StatsService(db).list_personal_bests()
    return {
        "workout_session": record_dto(session),
        "personal_bests": [personal_best_dto(best) for best in bests],
    }


async def _append_confirmation(
    request: Request,
    *,
    source_run: ConversationRun,
    action: Literal["plan_confirmed", "plan_rejected", "workout_confirmed"],
    text: str,
    draft_plan_id: int | None = None,
    workout_session_id: int | None = None,
) -> None:
    """业务事务成功后的确认 Entry：绑定精确来源 Run；同一 Run／动作／业务身份只写一次。"""
    db: Database = request.app.state.db
    conversations = ConversationRepo(db)
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
            created_at=iso_now(),
        )


def _require_same_identity(
    run: ConversationRun, *, chat_id: str, thread_id: str
) -> None:
    """Run 必须属于给定会话与 thread：否则明确失败，不静默复用别人的 Run。"""
    if run.conversation_id != chat_id or run.thread_id != thread_id:
        raise InvalidRequestShape(
            "Run 与会话或 thread 不一致："
            f"run={run.id!r} chat={run.conversation_id!r} thread={run.thread_id!r}"
        )


async def _run_events(
    runtime: AgentRuntime,
    db: Database,
    *,
    run: ConversationRun,
    body: AgentRunBody,
    business_day: date,
) -> AsyncIterator[AgentEvent]:
    """已落库用户 Entry 之后的运行链：先按阈值压缩，再重建上下文，最后委托工作流。

    只有摘要生成失败按 Pi ``_runAutoCompaction`` 的自动压缩失败语义处理（agent-session.ts:2500-2525
    捕获后返回 false）：本轮不压缩、原 Entry 继续用于本次模型调用。阈值判定、切点准备、数据库
    append 与 append 后重读的异常一律向外传播，由 :func:`_persisted_frames` 把 Run 收敛为
    ``failed`` 并发 ``error`` 帧：SQLite 是会话事实源，提交失败不得继续 Graph。
    """
    conversations = ConversationRepo(db)
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
                        created_at=iso_now(),
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
            conn, run.id, status="running", updated_at=iso_now()
        )
    async for event in stream_agent_run(
        runtime.graph,
        {"conversation_id": run.thread_id, "request": body.request},
        thread_config(run.thread_id),
        GeneratePlanRun(
            business_day=business_day,
            regenerate=body.regenerate,
            conversation_messages=history,
        ),
        runtime.run_deps,
    ):
        yield event


async def _persisted_frames(
    db: Database,
    conversations: ConversationRepo,
    run_id: str,
    events: AsyncIterator[AgentEvent],
) -> AsyncIterator[str]:
    """事件流 → SSE 文本帧：每个语义事件先落库再发送，运行错误只发一个 ``error`` 帧再关闭。

    客户端断开（``asyncio.CancelledError``）按取消语义收敛 Run，不发帧、不写 Assistant Entry。
    """
    sequence = 0
    messages: list[str] = []
    try:
        async for event in events:
            # 先落库再推进序号：落库回滚时该序号仍空缺，失败事件与 error 事件共用它。
            await _record_event(
                db, conversations, run_id, event, sequence + 1, messages
            )
            sequence += 1
            if event.event == "message":
                messages.append(str(event.data["text"]))
            yield _frame(event)
    except asyncio.CancelledError:
        # 客户端断开：生成器帧内的取消走同一次收敛；帧外交出的取消由 :class:`_AgentRunStream`
        # 的下游 send 边界收尾。
        await _converge_cancelled_run(conversations, run_id)
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
                created_at=iso_now(),
            )
        yield _frame(AgentEvent("error", {"message": message}))


async def _converge_cancelled_run(conversations: ConversationRepo, run_id: str) -> None:
    # AnyIO 在已取消作用域内反复重投取消，shield 保证收敛事务跑完。
    with CancelScope(shield=True):
        await conversations.cancel_active_run(run_id, updated_at=iso_now())


async def _record_event(
    db: Database,
    conversations: ConversationRepo,
    run_id: str,
    event: AgentEvent,
    sequence: int,
    messages: Sequence[str],
) -> None:
    """按事件类型落库：``waiting``／``done`` 的状态语义和事件同事务提交。"""
    async with db.transaction() as conn:
        if event.event == "waiting":
            await conversations.update_run_with_event_in_transaction(
                conn,
                run_id,
                event_type="waiting",
                sequence=sequence,
                payload=event.data,
                status="waiting",
                created_at=iso_now(),
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
                created_at=iso_now(),
            )
            return
        await conversations.append_event_in_transaction(
            conn,
            run_id=run_id,
            sequence=sequence,
            event_type=event.event,
            payload=event.data,
            created_at=iso_now(),
        )


async def _replay_frames(
    conversations: ConversationRepo, run_id: str
) -> AsyncIterator[str]:
    """幂等重放：同一 ``client_request_id`` 重发已提交事件，不重新执行、不重复写库。"""
    for event in await conversations.list_run_events(run_id):
        yield _frame(
            AgentEvent(cast(AgentEventName, event.event_type), event.payload)
        )


def _frame(event: AgentEvent) -> str:
    """一条 SSE 帧：事件名 ＋ JSON 数据；五种事件名与载荷键都来自 ``graph/workflow.py`` 的契约。"""
    return f"event: {event.event}\ndata: {json.dumps(event.data, ensure_ascii=False)}\n\n"


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
