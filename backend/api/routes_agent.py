"""Agent 三端点：``POST /api/agent/run``（SSE）、``/api/agent/confirm``、``/api/agent/reject``。

正本：``refactor-log/stage5.md`` §3.7（HTTP 与错误契约）／§3.8（SSE 事件契约）／§4.4；
``LANGGRAPH_REFACTOR_PLAN.md`` §9.4；``Fit-Agent-LangGraph-重构讨论总结.md`` §9／§10。

传输边界（本模块只做传输与事件序列化，不做领域规则、不写 SQL、不自己写计划行）：

- **事件来源是 LangGraph stream**：五类产品事件由 ``graph/workflow.py::stream_agent_run`` 从节点更新与
  确认 interrupt 产出（``node``／``message``／``waiting``／``done``），本层只把它序列化成 SSE 文本帧，
  不发送隐藏推理、完整系统提示词、Provider 配置或原始 LangChain 事件。
- **错误边界严格二分**：请求 JSON 形状、字段类型与 ``conversation_id`` UUID 非法在流建立**前**按既有
  JSON 错误形状拒绝（``api/dto.py`` 的 DTO 与统一处理器）；流建立后的模型配置、超时、Router、无
  active、draft 冲突等运行错误只发**一个** SSE ``error`` 后关闭，不混用 JSON。``confirm``／``reject``
  的领域错误复用同一 JSON 形状与明确 HTTP 状态（映射在 ``api/dto.py``）。
- **不回显敏感信息**：``error`` 的可见文本只取本项目自己写的产品错误，Provider／SDK／SQLite／超时异常
  一律固定文本（``str(exc)`` 可能带 Base URL、模型名、SQL、文件路径或堆栈）。
- **断线不是信号**：客户端断开只结束本次流，不触发额外业务写入，也不回滚已完成的 draft 持久化；恢复
  页面用既有 ``GET /api/plans`` 定位唯一 draft，本模块不新增 checkpoint 查询端点。
"""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from langgraph.graph.state import CompiledStateGraph

from api.deps import current_business_date
from api.dto import AgentPlanBody, AgentRunBody, plan_dto
from domain.plans.schema import Plan
from domain.plans.service import (
    PlanActivationConflict,
    PlanDraftConflict,
    PlanDraftStale,
    PlanNotFound,
    PlanRevalidationFailed,
)
from graph.checkpointer import thread_config
from graph.model import InvalidModelResponse, ModelCallFailed, ModelConfigurationError
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

router = APIRouter()


@dataclass(frozen=True, slots=True)
class AgentRuntime:
    """Agent 三端点的装配：编译图、计划子图依赖与运行入口依赖（``api/app.py`` lifespan 装配）。

    ``deps``／``run_deps`` 共用同一个模型 callable（唯一模型入口）与同一个 ``PlanActivationService``
    （确认／拒绝的唯一领域提交入口）；测试在同一 app 上换成固定替身的运行时，模型永不读环境变量。
    """

    graph: CompiledStateGraph
    deps: GeneratePlanDeps
    run_deps: AgentRunDeps


@router.post("/api/agent/run")
async def run_agent(
    body: AgentRunBody,
    request: Request,
    business_day: date = Depends(current_business_date),
) -> StreamingResponse:
    """一次 Agent Run：``text/event-stream`` 响应，五类产品事件，不混用 JSON（§3.7／§3.8）。

    ``conversation_id`` 即 Checkpointer 的 ``thread_id``；``regenerate`` 按 §3.4 的同类替换规则处理
    （缺省 false 且已有同类 draft 时不调模型）。业务日期与 Run 预算由本次请求注入，客户端不传。
    """
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
    """两条确认路径共用唯一确认入口（§3.3）：checkpoint 优先 ＋ 唯一 draft 兜底 ＋ 领域幂等。

    请求 ``plan_id`` 是唯一操作目标：与等待中 interrupt／唯一 draft 的 ID 不一致即
    :class:`~graph.nodes.ConfirmationConflict`（409），不写任何行。
    """
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
    """事件流 → SSE 文本帧；运行错误只发一个 ``error`` 事件再关闭，不混用 JSON（§3.7）。

    客户端断开（取消／生成器被关闭）不被当成错误，也不触发任何写入或回滚：断线不是取消、确认或拒绝
    信号（§3.8）。
    """
    try:
        async for event in events:
            yield _frame(event)
    except Exception as error:  # 任何登记或未登记的运行错误都只变成一条 SSE error
        yield _frame(AgentEvent("error", {"message": agent_run_error_message(error)}))


def _frame(event: AgentEvent) -> str:
    """一条 SSE 帧：事件名 ＋ JSON 数据；五种事件名与载荷键都来自 ``graph/workflow.py`` 的契约。"""
    return f"event: {event.event}\ndata: {json.dumps(event.data, ensure_ascii=False)}\n\n"


# ---------- SSE error 的可见文本：只回显本项目自己的产品错误（§3.7） ----------

#: 模型配置缺失的固定文本：不回显环境变量名（§3.8 的 error 事件不发送 Provider 配置）。
MODEL_CONFIGURATION_ERROR_MESSAGE = (
    "模型服务未配置：本次运行未产生计划写入，请检查服务端模型配置后重试"
)

#: 模型响应不符统一 Schema 的固定文本：不回显原始模型输出（§3.8 不发送原始模型事件）。
INVALID_MODEL_RESPONSE_MESSAGE = (
    "模型响应不符合统一 Schema：本次运行未产生计划写入，请稍后重试"
)

#: 不回显 ``str(exc)`` 的异常类型 → 固定文本（上面的两条）。
_FIXED_ERROR_MESSAGES: tuple[tuple[type[Exception], str], ...] = (
    (ModelConfigurationError, MODEL_CONFIGURATION_ERROR_MESSAGE),
    (InvalidModelResponse, INVALID_MODEL_RESPONSE_MESSAGE),
)

#: 可以在 SSE ``error`` 中原样返回的产品异常：全部是本项目自己写、面向用户的错误文本，不含密钥、
#: 端点、模型名、SQL、文件路径或堆栈（``ModelCallFailed`` 的文本在生产模型入口
#: ``api/app.py::_lazy_model_call`` 已换成固定文本）。
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

#: 其它运行错误（Provider／SDK／SQLite／超时等）的固定文本：``str(exc)`` 可能带 Base URL、模型名、
#: SQL、文件路径或堆栈，一律不透传（§3.7 收尾条）。
AGENT_RUN_ERROR_MESSAGE = "本次运行失败：未产生可激活计划，请稍后重试"


def agent_run_error_message(error: BaseException) -> str:
    """SSE ``error`` 事件的可见文本：本项目产品错误照原样，其余一律固定文本（不回显敏感信息）。

    除 ``asyncio.CancelledError`` 之外的任何运行错误都会走到这里；断线（取消或生成器被关闭）是
    ``BaseException``，不会进入本映射，也不被当成错误。
    """
    for error_type, message in _FIXED_ERROR_MESSAGES:
        if isinstance(error, error_type):
            return message
    if isinstance(error, _PRODUCT_ERROR_TYPES):
        return str(error)
    return AGENT_RUN_ERROR_MESSAGE
