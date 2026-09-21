"""Agent 三端点：Run 的幂等判定与流式响应、计划确认／拒绝、自然语言打卡确认。"""

from datetime import date
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.api.dependencies import app_services, current_business_date, iso_now
from app.api.errors import InvalidRequestShape
from app.api.schemas.agent_dto import (
    AgentPlanBody,
    AgentRunBody,
    ConfirmWorkoutBody,
)
from app.api.schemas.analytics_dto import personal_best_dto
from app.api.schemas.log_dto import record_dto, workout_set_inputs_from_dto
from app.api.schemas.plan_dto import plan_dto
from app.api.streaming import AgentRunStream, stream_frames
from app.application.agent.contracts import AgentRuntime, GeneratePlanRun
from app.application.agent.plan_graph import invoke_confirmation
from app.application.agent.run_service import (
    append_confirmation,
    persisted_events,
    replay_events,
    run_events,
)
from app.application.ports import ConversationNotFound
from app.application.services.records_service import (
    WorkoutRecordNotFound,
    WorkoutRecordsService,
)
from app.application.services.stats_service import StatsService
from app.domain.conversations.schema import ConversationRun
from app.domain.plans.schema import Plan

router = APIRouter()


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
    services = app_services(request)
    conversations = services.conversations_repo
    chat_id = str(body.chat_id)
    thread_id = str(body.conversation_id)
    conversation = await conversations.read_conversation(chat_id)
    if conversation is None:
        raise ConversationNotFound(f"会话不存在：{chat_id}")
    existing = await conversations.read_run_by_client_request_id(body.client_request_id)
    if existing is not None:
        _require_same_identity(existing, chat_id=chat_id, thread_id=thread_id)
        return StreamingResponse(
            stream_frames(replay_events(conversations, existing.id)),
            media_type="text/event-stream",
        )
    run_id = str(uuid4())
    entry_id = str(uuid4())
    created_at = iso_now()
    async with services.db.transaction() as conn:
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
            stream_frames(replay_events(conversations, run.id)),
            media_type="text/event-stream",
        )
    # 压缩、上下文重建与模型调用都不在本请求的数据库事务内；三段顺序固定在 ``run_events`` 里。
    return AgentRunStream(
        stream_frames(
            persisted_events(
                services.db,
                conversations,
                run.id,
                run_events(
                    runtime,
                    services.db,
                    conversations,
                    run=run,
                    request=body.request,
                    regenerate=body.regenerate,
                    business_day=business_day,
                    now=iso_now,
                ),
                now=iso_now,
            )
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
    services = app_services(request)
    source_run = await _confirmation_source_run(body, request)
    existing = await services.conversations_repo.read_confirmations(
        source_run.id, "workout_confirmed"
    )
    if existing:
        # 重放：来源 Run 已确认过一次，不再写第二条训练记录，也不再追加确认 Entry。
        return await _confirmed_workout_response(
            services.records,
            services.stats,
            int(existing[0].payload["workout_session_id"]),
        )
    session = await services.records.create(
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
    return await _confirmed_workout_response(services.records, services.stats, session.id)


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
    conversations = app_services(request).conversations_repo
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
    records: WorkoutRecordsService, stats: StatsService, session_id: int
) -> dict[str, Any]:
    """确认写入的响应：按身份读回既有训练记录 ＋ 既有 Stats 现算 PB；记录已删除即领域 404。"""
    session = await records.get(session_id)
    if session is None:
        raise WorkoutRecordNotFound(f"训练记录不存在：{session_id}")
    bests = await stats.list_personal_bests()
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
    services = app_services(request)
    await append_confirmation(
        services.db,
        services.conversations_repo,
        source_run=source_run,
        action=action,
        text=text,
        draft_plan_id=draft_plan_id,
        workout_session_id=workout_session_id,
        now=iso_now,
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
