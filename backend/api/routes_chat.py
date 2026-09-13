"""对话／Run 路由与 SSE 事件流（stage4.md S4-07；08 8.7/8.8；§6 冻结传输拼写）。

传输拼写（stage4.md §6，逐字冻结）：``POST /api/sessions``、``GET /api/sessions/{session_id}``、
``POST /api/sessions/{session_id}/requests``（``{client_request_id, text}`` → ``{created, run}``）、
``GET /api/runs/{run_id}``、``POST /api/runs/{run_id}/retry``（``{client_request_id}``）、
``POST /api/runs/{run_id}/cancel``、``GET /api/runs/{run_id}/events``（SSE）。

边界：

- **不建第二套状态机**：Run 创建幂等、全局单 Run、手动重试与重启恢复全部复用
  :class:`~runtime.run_service.RunService` 与 :class:`~storage.run_repo.RunRepo`；执行仍由
  :class:`~runtime.run_task.ExecutionDriver` 唯一驱动，本层只提交请求与阅读事件。
- **执行在提交事务之后启动**（``ExecutionDriver.start``）：SSE 只是阅读者，断开或刷新既不取消
  也不重跑；取消只由 ``POST /cancel`` 触发（08 8.3）。
- **SSE 只发产品事件白名单**（:mod:`runtime.events`）：隐藏推理、框架原始事件、工具轨迹、
  Token／Cache 与密钥不可能进事件；heartbeat 只在传输层产生，不入库、不分配恢复游标。
- **模型只在 Run 真正开始时构造**（``app.state.model_factory``）：无凭据即 fail-closed，Run 以
  已冻结的 ``model_request_failed`` 结束，不新增错误码、不伪造成功（08 8.8）。
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import date
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic_ai.models import Model

from api.deps import ProviderNotConfigured, current_business_date
from api.dto import (
    InvalidRequestShape,
    json_object_body,
    run_dto,
    session_dto,
)
from runtime.agent_factory import (
    build_recalc_run_work,
    build_review_run_work,
    build_streaming_run_work,
)
from runtime.error_codes import MODEL_REQUEST_FAILED
from runtime.events import RunEventStream, run_product_events, sse_frame
from runtime.run_task import ActiveExecution, ExecutionFailure, FrameworkMessages
from storage.errors import NotFound
from storage.run_repo import RunRepo

router = APIRouter()


async def _session_response(db: Any, session_id: str) -> dict[str, Any]:
    """会话 + 全部 Run + 已保存消息（断线／刷新后的查询恢复入口）；不存在即明确未找到。"""
    repo = RunRepo(db)
    conversation = await repo.get_conversation(session_id)
    if conversation is None:
        raise NotFound(f"会话不存在：{session_id}")
    return session_dto(
        conversation,
        await repo.list_runs(session_id),
        await repo.list_messages(session_id),
    )


def _required_text(name: str, raw: object) -> str:
    """请求体文本字段：必须是去空白后非空的字符串（形状不符即 400 ``invalid_request``）。"""
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidRequestShape(f"{name} 必须是非空字符串：{raw!r}")
    return raw


def _start_run(
    request: Request,
    *,
    run_id: str,
    conversation_id: str,
    business_date: date,
    parent: Any = None,
    review: bool = False,
) -> None:
    """启动一次 Run 的唯一执行；名额在任何 await 之前同步登记（08 8.2/8.3）。

    ``parent`` 非空时是重新生成 Run（S4-08 Q1=C），``review`` 为真时是显式复盘生成 Run
    （S4-08 Q3=B）：两者都只换成对应执行入口，仍复用同一驱动、预算、取消与 SSE；不建
    第二套执行或状态机制。

    SSE 断开与页面刷新都不经过本函数：它们既不取消也不重跑，Run 继续执行到终态。
    """
    state = request.app.state
    db = state.db
    repo = RunRepo(db)
    events: RunEventStream = state.run_events
    factory: Callable[[], Awaitable[Model]] = state.model_factory

    async def work(active: ActiveExecution) -> FrameworkMessages:
        try:
            model = await factory()
        except ProviderNotConfigured as exc:
            # 无凭据 fail-closed：不发任何请求、不伪造成功；原因取已冻结的终态码（08 8.8）。
            raise ExecutionFailure(MODEL_REQUEST_FAILED) from exc
        if review:
            run_work = build_review_run_work(
                db=db,
                model=model,
                harness=state.harness_config,
                business_date=business_date,
            )
        elif parent is None:
            run_work = build_streaming_run_work(
                db=db,
                repo=repo,
                model=model,
                harness=state.harness_config,
                conversation_id=conversation_id,
                run_id=run_id,
                business_date=business_date,
                events=events,
            )
        else:
            run_work = build_recalc_run_work(
                db=db,
                repo=repo,
                model=model,
                harness=state.harness_config,
                conversation_id=conversation_id,
                run_id=run_id,
                business_date=business_date,
                events=events,
                parent=parent,
            )
        return await run_work(active)

    state.run_driver.start(run_id, work)


@router.post("/api/sessions")
async def create_session(request: Request) -> dict[str, Any]:
    """新建空会话：身份由服务端生成，不接受客户端指定（请求体必须是空对象）。"""
    await json_object_body(request, keys=frozenset())
    db = request.app.state.db
    session_id = uuid4().hex
    await RunRepo(db).create_conversation(session_id)
    return await _session_response(db, session_id)


@router.get("/api/sessions/{session_id}")
async def get_session(session_id: str, request: Request) -> dict[str, Any]:
    """会话查询：已保存消息（未完成回答标 ``complete=false``）+ 该会话全部 Run 状态。

    这是断线／刷新／重启后的恢复入口（08 8.7）：不依赖事件重放，也不重复拼接回答。
    """
    return await _session_response(request.app.state.db, session_id)


@router.post("/api/sessions/{session_id}/requests")
async def submit_request(
    session_id: str,
    request: Request,
    business_date: date = Depends(current_business_date),
) -> dict[str, Any]:
    """提交一次用户请求：``{client_request_id, text}`` → ``{created, run}``。

    幂等与忙碌判定全部在 :class:`~runtime.run_service.RunService`（同一事务内先按
    ``client_request_id`` 查重，再检查全局活跃 Run）：相同请求返回已有 Run 且**不**重启执行；
    不同请求遇活跃 Run 返回 409 ``conversation_busy``，不创建 Run 或消息（08 8.2）。
    """
    body = await json_object_body(
        request, keys=frozenset({"client_request_id", "text"})
    )
    client_request_id = _required_text("client_request_id", body["client_request_id"])
    text = _required_text("text", body["text"])
    db = request.app.state.db
    if await RunRepo(db).get_conversation(session_id) is None:
        raise NotFound(f"会话不存在：{session_id}")
    result = await request.app.state.run_service.submit_request(
        conversation_id=session_id,
        run_id=uuid4().hex,
        client_request_id=client_request_id,
        text=text,
    )
    if bool(result["created"]):
        _start_run(
            request,
            run_id=str(result["run"]["id"]),
            conversation_id=session_id,
            business_date=business_date,
        )
    return {"created": bool(result["created"]), "run": run_dto(result["run"])}


@router.post("/api/drafts/{draft_id}/recalc")
async def recalc_draft(
    draft_id: str,
    request: Request,
    business_date: date = Depends(current_business_date),
) -> dict[str, Any]:
    """按最新数据重新生成草稿（S4-08 Q1=C/Q2=A）：返回 ``{created, run}``。

    传输拼写沿用前端既有内部名 ``/recalc``（产品文案为「重新生成草稿」）；本入口只创建
    Agent Run，草稿由工具链在 Run 内产生：新草稿带 ``parent_draft_id``、绑定生成读取时的
    最新 ``context_version``，旧草稿不被修改、确认或合并，新草稿仍须用户确认。请求体为
    ``{client_request_id}``——相同键在 Run 创建前幂等返回已有 Run（``created=false``，不依赖
    子草稿是否已产生），不同请求遇全局活跃 Run 返回 409 ``conversation_busy``（幂等查重先于
    busy，同一事务）；待产生 Pending 子草稿后的重复请求沿用旧草稿匹配幂等（Q2=A）。
    """
    body = await json_object_body(request, keys=frozenset({"client_request_id"}))
    client_request_id = _required_text("client_request_id", body["client_request_id"])
    result = await request.app.state.run_service.regenerate_draft(
        parent_draft_id=draft_id,
        run_id=uuid4().hex,
        client_request_id=client_request_id,
    )
    if bool(result["created"]):
        _start_run(
            request,
            run_id=str(result["run"]["id"]),
            conversation_id=str(result["run"]["conversation_id"]),
            business_date=business_date,
            parent=result["parent"],
        )
    return {"created": bool(result["created"]), "run": run_dto(result["run"])}


@router.post("/api/reviews")
async def request_review(
    request: Request,
    business_date: date = Depends(current_business_date),
) -> dict[str, Any]:
    """显式请求生成复盘正文（S4-08 Q3=B）：返回 ``{created, run}``。

    复盘只由本独立入口触发：没有定时器、没有按周／自动任务，也没有聊天工具入口。请求体
    为 ``{client_request_id}``——相同键幂等返回已有 Run（``created=false``），不同请求遇
    全局活跃 Run 返回 409 ``conversation_busy``（幂等先于 busy，同一事务）。Run 执行时先
    冻结确定性统计快照与来源修订，模型只写解释正文；数值校验收不过或来源竞态均不保存，
    重复显式生成只追加新行。
    """
    body = await json_object_body(request, keys=frozenset({"client_request_id"}))
    client_request_id = _required_text("client_request_id", body["client_request_id"])
    result = await request.app.state.run_service.request_review(
        run_id=uuid4().hex,
        client_request_id=client_request_id,
    )
    if bool(result["created"]):
        _start_run(
            request,
            run_id=str(result["run"]["id"]),
            conversation_id=str(result["run"]["conversation_id"]),
            business_date=business_date,
            review=True,
        )
    return {"created": bool(result["created"]), "run": run_dto(result["run"])}


@router.get("/api/runs/{run_id}")
async def get_run(run_id: str, request: Request) -> dict[str, Any]:
    """按身份查询 Run：五态权威状态与可理解原因（SQLite 是事实源，08 8.7）。"""
    run = await RunRepo(request.app.state.db).get_run(run_id)
    if run is None:
        raise NotFound(f"Run 不存在：{run_id}")
    return {"run": run_dto(run)}


@router.post("/api/runs/{run_id}/retry")
async def retry_run(
    run_id: str,
    request: Request,
    business_date: date = Depends(current_business_date),
) -> dict[str, Any]:
    """手动重试：用旧 Run 的同一请求事实创建**新** Run（``retry_of_run_id`` 指向旧 Run）。

    不复活旧记录、不做断点续跑（08 8.1/8.4）；旧 Run 仍在执行时由全局单 Run 判定拒绝。
    """
    body = await json_object_body(request, keys=frozenset({"client_request_id"}))
    client_request_id = _required_text("client_request_id", body["client_request_id"])
    db = request.app.state.db
    previous = await RunRepo(db).get_run(run_id)
    if previous is None:
        raise NotFound(f"Run 不存在：{run_id}")
    result = await request.app.state.run_service.retry_request(
        previous_run_id=run_id,
        run_id=uuid4().hex,
        client_request_id=client_request_id,
    )
    if bool(result["created"]):
        _start_run(
            request,
            run_id=str(result["run"]["id"]),
            conversation_id=str(result["run"]["conversation_id"]),
            business_date=business_date,
        )
    return {"created": bool(result["created"]), "run": run_dto(result["run"])}


@router.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str, request: Request) -> dict[str, Any]:
    """显式取消（08 8.3）：条件更新为 ``cancelled`` 后尽力中断当前底层调用。

    只有本入口触发取消：SSE 断开、刷新与客户端离线都不取消。终态 Run 重复取消返回
    409 ``invalid_request``（状态不允许该操作），身份不存在返回 404。
    """
    await json_object_body(request, keys=frozenset())
    db = request.app.state.db
    if await RunRepo(db).get_run(run_id) is None:
        raise NotFound(f"Run 不存在：{run_id}")
    run = await request.app.state.run_driver.cancel(run_id)
    return {"run": run_dto(run)}


@router.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request) -> StreamingResponse:
    """Run 的 SSE 产品事件流：只发事件白名单，终态即结束；断线不取消、不重跑、不重放。

    15 秒没有业务事件时由传输层补一个 heartbeat（不入库、不分配 ID／恢复游标，08 8.7）。
    """
    repo = RunRepo(request.app.state.db)
    if await repo.get_run(run_id) is None:
        raise NotFound(f"Run 不存在：{run_id}")
    stream = run_product_events(
        repo=repo,
        events=request.app.state.run_events,
        run_id=run_id,
    )

    async def body() -> AsyncIterator[str]:
        async for name, payload in stream:
            yield sse_frame(name, payload)

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"cache-control": "no-store"},
    )
