import asyncio
import logging
from collections.abc import Awaitable, Callable
from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextlib import suppress
from time import monotonic

from app.agent.agent_loop import ModelOutputLimitExceeded
from app.agent.budget import ModelRequestLimitExceeded
from app.agent.compaction import (
    CompactionSummaryError,
    ContextBudgetExceeded,
    NoSummarizableHistory,
)
from app.agent.governance import (
    CANCEL_COOPERATIVE_WAIT_SECONDS,
    DIAGNOSTIC_WRITE_WAIT_SECONDS,
    TERMINAL_PERSIST_WAIT_SECONDS,
    RunStopReason,
    stop_terminal,
)
from app.agent.telemetry import RunTrace, TraceSpan
from app.agent.tool import CredentialDetectedError
from app.ai.overflow import ContextOverflowError
from app.ai.quota import ProviderQuotaExceeded
from app.application.business.service import BusinessService
from app.application.session.credentials import CREDENTIAL_SAFE_MESSAGE
from app.application.session.entries import SessionEntries
from app.application.session.execution import RunEvents, execute
from app.application.session.queries import SessionQueries
from app.application.session.runs import SessionRuns
from app.application.session.runtime import (
    OUTPUT_TOO_LARGE_MESSAGE,
    RunState,
    enter_fault_closure,
    release_run_slot,
    runs,
    runs_lock,
)
from app.application.session.steering import SteeringCoordinator

logger = logging.getLogger(__name__)
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})
COMPACTION_FAILED_MESSAGE = "上下文压缩失败，请重新发起请求。"
CONTEXT_BUDGET_EXCEEDED_MESSAGE = "当前模型的上下文预算不足，无法继续执行。"
CONTEXT_OVERFLOW_MESSAGE = "恢复后仍超出容量，无法继续执行。"
MODEL_OUTPUT_LIMIT_MESSAGE = "回答达到输出上限，内容未完成"
PROVIDER_QUOTA_EXCEEDED_MESSAGE = "模型服务账户额度不足，无法继续执行。"
MODEL_TIMEOUT_MESSAGE = "模型响应超时，请重新发起请求。"
TOOL_TIMEOUT_MESSAGE = "工具执行超时，已结束本次运行。"
RUN_TIMEOUT_MESSAGE = "本次运行已超时结束。"
SLOW_CONSUMER_MESSAGE = "客户端读取过慢，已结束本次运行。"
STOP_REASON_MESSAGES: dict[str, str] = {
    "model_timeout": MODEL_TIMEOUT_MESSAGE,
    "tool_timeout": TOOL_TIMEOUT_MESSAGE,
    "run_timeout": RUN_TIMEOUT_MESSAGE,
    "slow_consumer": SLOW_CONSUMER_MESSAGE,
    "output_too_large": OUTPUT_TOO_LARGE_MESSAGE,
}


def retire(state: RunState) -> None:
    # 唯一收尾入口只释放一次：重复回调不得再次归还全局名额。
    with runs_lock:
        removed = runs.pop(state.run_id, None)
    if removed is not None:
        release_run_slot(state.run_scope)


def publish_terminal(state: RunState, run, stop_reason: str | None) -> None:
    # 公开终态由已提交运行记录决定；仅终态持久化成功后调用。
    if run.status == "completed":
        state.publish_terminal("done", {"status": "completed", "stop_reason": stop_reason})
        return
    cancelled = run.status == "cancelled"
    state.publish_terminal("error", {
        "status": run.status,
        "code": run.error_code or ("cancelled" if cancelled else "execution_failed"),
        "message": run.error_message
        or ("执行已取消。" if cancelled else "执行失败，请重新发起请求。"),
    })


def terminal_decision(
    failure: BaseException | None,
    cancelled: bool,
    stop_reason: RunStopReason | None = None,
) -> tuple[str, str | None, str | None]:
    # 凭据命中优先于取消与停止原因；其余保持停止、取消、失败、完成的裁决顺序。
    if isinstance(failure, CredentialDetectedError):
        return "failed", "credential_detected", CREDENTIAL_SAFE_MESSAGE
    if stop_reason is not None:
        status, code = stop_terminal(stop_reason)
        message = STOP_REASON_MESSAGES.get(code)
        if message is None:
            message = "执行已取消。" if status == "cancelled" else "执行失败，请重新发起请求。"
        return status, code, message
    if isinstance(failure, CompactionSummaryError):
        return "failed", "compaction_failed", COMPACTION_FAILED_MESSAGE
    if isinstance(failure, (ContextBudgetExceeded, NoSummarizableHistory)):
        return "failed", "context_budget_exceeded", CONTEXT_BUDGET_EXCEEDED_MESSAGE
    if isinstance(failure, ModelRequestLimitExceeded):
        return "failed", "model_request_limit_exceeded", str(failure)
    if isinstance(failure, ProviderQuotaExceeded):
        return "failed", "provider_quota_exceeded", PROVIDER_QUOTA_EXCEEDED_MESSAGE
    if isinstance(failure, ModelOutputLimitExceeded):
        return "failed", "model_output_limit_exceeded", MODEL_OUTPUT_LIMIT_MESSAGE
    if cancelled:
        return "cancelled", "cancelled", "执行已取消。"
    if isinstance(failure, ContextOverflowError):
        return "failed", "context_overflow", CONTEXT_OVERFLOW_MESSAGE
    if failure is not None:
        return "failed", "execution_failed", "执行失败，请重新发起请求。"
    return "completed", None, None


FAULT_CLOSED = object()
READ_TIMEOUT = object()


def _consume_future(future) -> None:
    # 迟到的底层结果只取回异常，禁止再次发布终态或释放名额。
    with suppress(Exception):
        if not future.cancelled():
            future.exception()


async def _record_diagnostic(
    runs: SessionRuns, session_id: str, run_id: str, reason: str
) -> None:
    for attempt in range(3):
        try:
            await asyncio.wait_for(
                runs.record_run_diagnostic(session_id, run_id, reason),
                timeout=DIAGNOSTIC_WRITE_WAIT_SECONDS,
            )
            return
        except TimeoutError:
            if attempt == 2:
                logger.error("运行诊断写入超时（%s）", reason)
                return
            await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error("运行诊断写入失败（%s/%s）", reason, type(error).__name__)
            return


def _schedule_diagnostic(
    state: RunState,
    runs: SessionRuns,
    session_id: str,
    run_id: str,
    reason: str,
) -> None:
    task = asyncio.create_task(_record_diagnostic(runs, session_id, run_id, reason))
    state.diagnostic_tasks.append(task)
    task.add_done_callback(_consume_future)


async def _bounded_read(
    state: RunState, awaitable: Awaitable, timeout: float
) -> object:
    # 超时不取消底层任务、不回滚，保留句柄供核对真实结果。
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=max(0.0, timeout))
    except BaseException:
        state.inflight_reads.append(task)
        task.add_done_callback(_consume_future)
        raise
    if task in done:
        return task.result()
    state.inflight_reads.append(task)
    task.add_done_callback(_consume_future)
    return READ_TIMEOUT


def _finalization_read_budget(_state: RunState) -> float:
    return TERMINAL_PERSIST_WAIT_SECONDS


async def _watch_control(
    state: RunState, coordinator: SteeringCoordinator, run_id: str
) -> None:
    deadline = state.control.deadline
    while not state.control.cancelled:
        if deadline is not None and deadline.expired():
            state.control.request_cancel("run_timeout")
            break
        wait = 0.05 if deadline is None else min(0.05, max(0.001, deadline.remaining()))
        await asyncio.sleep(wait)
    state.control.note_stop()
    coordinator.close(run_id)


async def _fault_close(
    state: RunState,
    root: TraceSpan,
    coordinator: SteeringCoordinator,
    runs: SessionRuns,
    session_id: str,
    run_id: str,
    reason: str,
    work,
    concurrent_future,
) -> object:
    # 故障封闭保留运行占用与真实句柄，诊断异步写入。
    state.mark_fault(reason, concurrent_future, state.tool_tracker.pending())
    _schedule_diagnostic(state, runs, session_id, run_id, reason)
    root.set_status("failed", reason)
    coordinator.close(run_id)
    enter_fault_closure(state.run_scope)
    if work is not None and not work.done():
        work.add_done_callback(_consume_future)
    logger.error("运行进入故障封闭（%s/%s）", run_id, reason)
    return FAULT_CLOSED


async def _wait_tools(state: RunState) -> bool:
    # 从首次停止时刻起算共享收尾预算，阻塞等待放入线程。
    cooperative_deadline = state.control.cooperative_deadline()
    if cooperative_deadline is None:
        cooperative_deadline = monotonic() + CANCEL_COOPERATIVE_WAIT_SECONDS
    remaining = max(0.0, cooperative_deadline - monotonic())
    return await asyncio.to_thread(state.tool_tracker.wait_all, remaining)


async def coordinate(
    run,
    state: RunState,
    session_id: str,
    tools: dict,
    model,
    secrets: tuple[str, ...],
    queries: SessionQueries,
    entries: SessionEntries,
    runs: SessionRuns,
    business: BusinessService,
    coordinator: SteeringCoordinator,
    executor: ThreadPoolExecutor,
    loop,
    trace: RunTrace,
    store,
    *,
    create_events: Callable[..., RunEvents],
) -> None:
    # 独立收尾任务持有终态所有权；提交后依次结算根、写入 Trace、发布终态。
    failure: BaseException | None = None
    stop_reason: str | None = None

    async def settle(root: TraceSpan):
        nonlocal failure, stop_reason
        watcher = asyncio.create_task(_watch_control(state, coordinator, run.id))
        concurrent_future = None
        future = None
        try:
            try:
                projection = await _bounded_read(
                    state,
                    queries.get_projection(session_id, run.request_entry_id),
                    state.control.deadline.remaining() + CANCEL_COOPERATIVE_WAIT_SECONDS,
                )
                if projection is READ_TIMEOUT:
                    state.control.request_cancel("run_timeout")
                    return await _fault_close(
                        state, root, coordinator, runs, session_id, run.id,
                        "terminal_persist_failed", None, None,
                    )
                branch = projection.context.messages
                concurrent_future = executor.submit(
                    execute, run, session_id, branch, tools, state, queries, entries, business,
                    loop, model, secrets, root,
                    create_events=lambda guard, parents: create_events(
                        state.publish, guard, parents, root.record_first_text
                    ),
                )
                future = asyncio.wrap_future(concurrent_future)
            except BaseException as error:
                failure = error

            if future is not None:
                while True:
                    done, _ = await asyncio.wait({future}, timeout=0.05)
                    if done:
                        break
                    if state.control.cancelled:
                        state.control.note_stop()
                        deadline = state.control.cooperative_deadline()
                        if deadline is not None and monotonic() >= deadline:
                            return await _fault_close(
                                state, root, coordinator, runs, session_id, run.id,
                                "worker_stuck", future, concurrent_future,
                            )
                try:
                    stop_reason = future.result()
                except BaseException as error:
                    failure = error

            if not await _wait_tools(state):
                return await _fault_close(
                    state, root, coordinator, runs, session_id, run.id,
                    "worker_stuck", future, concurrent_future,
                )

            cancelled = isinstance(failure, CancelledError) or state.cancel.is_set()
            recorded_stop = state.control.stop.reason
            status, code, message = terminal_decision(failure, cancelled, recorded_stop)
            try:
                current = await _bounded_read(
                    state,
                    queries.get_run(session_id, run.id),
                    _finalization_read_budget(state),
                )
            except BaseException:
                return await _fault_close(
                    state, root, coordinator, runs, session_id, run.id,
                    "terminal_persist_failed", future, concurrent_future,
                )
            if current is READ_TIMEOUT:
                return await _fault_close(
                    state, root, coordinator, runs, session_id, run.id,
                    "terminal_persist_failed", future, concurrent_future,
                )
            if current.status in TERMINAL_RUN_STATUSES:
                status, code, message = (
                    current.status, current.error_code, current.error_message,
                )

            # 终态持久化超时不回滚、不再次提交。
            finish_task = asyncio.create_task(coordinator.finish(
                session_id, run.id, status,
                error_code=code, error_message=message, stop_reason=recorded_stop,
            ))
            done, _ = await asyncio.wait(
                {finish_task}, timeout=TERMINAL_PERSIST_WAIT_SECONDS
            )
            if not done:
                finish_task.add_done_callback(_consume_future)
                return await _fault_close(
                    state, root, coordinator, runs, session_id, run.id,
                    "terminal_persist_failed", future, concurrent_future,
                )
            try:
                outcome = finish_task.result()
            except BaseException:
                return await _fault_close(
                    state, root, coordinator, runs, session_id, run.id,
                    "terminal_persist_failed", future, concurrent_future,
                )
            root_status = {"completed": "completed", "cancelled": "cancelled"}.get(
                status, "failed"
            )
            root.set_status(root_status, code)
            return outcome
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher

    try:
        outcome = await trace.start_span("agent_run", {}, settle)
    except BaseException:
        state.mark_fault("terminal_persist_failed", None, state.tool_tracker.pending())
        _schedule_diagnostic(state, runs, session_id, run.id, "terminal_persist_failed")
        coordinator.close(str(run.id))
        enter_fault_closure(state.run_scope)
        await trace.finish(store)
        raise
    await trace.finish(store)
    if outcome is FAULT_CLOSED:
        return
    publish_terminal(state, outcome.run, stop_reason)
    retire(state)
