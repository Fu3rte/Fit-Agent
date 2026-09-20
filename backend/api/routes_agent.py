import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from langgraph.graph.state import CompiledStateGraph

from api.deps import current_business_date
from api.dto import (
    AgentPlanBody,
    AgentRunBody,
    ConfirmWorkoutBody,
    personal_best_dto,
    plan_dto,
    record_dto,
    workout_set_inputs_from_dto,
)
from domain.plans.schema import Plan
from domain.plans.service import (
    PlanActivationConflict,
    PlanDraftConflict,
    PlanDraftStale,
    PlanNotFound,
    PlanRevalidationFailed,
)
from domain.records.service import WorkoutRecordsService
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
    AgentRunDeps,
    invoke_confirmation,
    stream_agent_run,
)
from provider_settings import ModelConfigurationError

router = APIRouter()


@dataclass(frozen=True, slots=True)
class AgentRuntime:
    """Agent 三端点的装配。"""

    graph: CompiledStateGraph
    deps: GeneratePlanDeps
    run_deps: AgentRunDeps


@router.post("/api/agent/run")
async def run_agent(
    body: AgentRunBody,
    request: Request,
    business_day: date = Depends(current_business_date),
) -> StreamingResponse:
    """一次 Agent Run：``text/event-stream`` 响应，五类产品事件。"""
    runtime = _runtime(request)
    events = stream_agent_run(
        runtime.graph,
        {"conversation_id": str(body.conversation_id), "request": body.request},
        thread_config(str(body.conversation_id)),
        GeneratePlanRun(business_day=business_day, regenerate=body.regenerate),
        runtime.run_deps,
    )
    return StreamingResponse(_sse_frames(events), media_type="text/event-stream")


@router.post("/api/agent/confirm")
async def confirm_plan(
    body: AgentPlanBody,
    request: Request,
    business_day: date = Depends(current_business_date),
) -> dict[str, Any]:
    """用户确认：激活 draft，返回落库后的计划行（幂等已 active 时返回既有行，§3.2／§3.3）。"""
    return {"plan": plan_dto(await _confirmation(body, request, "confirm", business_day))}


@router.post("/api/agent/reject")
async def reject_plan(
    body: AgentPlanBody,
    request: Request,
    business_day: date = Depends(current_business_date),
) -> dict[str, Any]:
    """用户拒绝：把 draft 归档，返回落库后的计划行（原 active 不变，永不写 ``rejected``，§3.2）。"""
    return {"plan": plan_dto(await _confirmation(body, request, "reject", business_day))}


@router.post("/api/agent/confirm-workout")
async def confirm_workout(
    body: ConfirmWorkoutBody, request: Request
) -> dict[str, Any]:
    """自然语言打卡确认写入：复用表单写入服务，写入成功后经既有 Stats 重查 PB。"""
    db = request.app.state.db
    session = await WorkoutRecordsService(db).create(
        body.performed_on,
        workout_set_inputs_from_dto(body.sets),
        plan_session_id=body.plan_session_id,
        auto_link=body.auto_link,
    )
    bests = await StatsService(db).list_personal_bests()
    return {
        "workout_session": record_dto(session),
        "personal_bests": [personal_best_dto(best) for best in bests],
    }


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


async def _sse_frames(events: AsyncIterator[AgentEvent]) -> AsyncIterator[str]:
    """事件流 → SSE 文本帧；运行错误只发一个 ``error`` 事件再关闭。"""
    try:
        async for event in events:
            yield _frame(event)
    except Exception as error:
        yield _frame(AgentEvent("error", {"message": agent_run_error_message(error)}))


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
