import asyncio
import logging
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from time import perf_counter, time_ns
from uuid import UUID

from app.agent.telemetry import RunTrace
from app.agent.tool import AgentTool
from app.application.business.service import BusinessService
from app.application.session.coordination import coordinate
from app.application.session.credentials import CredentialFilter
from app.application.session.entries import SessionEntries
from app.application.session.operations import SessionOperations
from app.application.session.queries import SessionQueries
from app.application.session.runs import SessionRuns
from app.application.session.runtime import (
    RunState,
    accepting_runs,
    acquire_run_slot,
    current_run_scope,
    is_fault_closed,
    operation_lock,
    release_run_slot,
    runs,
    runs_lock,
    session_gate,
    session_run_limits,
    track,
)
from app.application.session.service import SessionService
from app.application.session.steering import SteeringCoordinator
from app.domain.session.errors import (
    CredentialDetected,
    EntryNotFound,
    IncompleteToolChain,
    InvalidTargetEntry,
    OperationConflict,
    OperationExpired,
    RunBusy,
    SessionMismatch,
    SessionNotFound,
)
from app.domain.session.models import OperationOutcome
from app.model_config import ModelConfig

logger = logging.getLogger(__name__)


class AdmissionUnavailable(Exception):
    def __init__(self, reason: str, retry_after: int | None = None):
        self.reason = reason
        self.retry_after = retry_after
        super().__init__(reason)


@dataclass
class AdmissionResult:
    outcome: OperationOutcome
    state: RunState | None = None


async def admit_run(
    *, service: SessionService, queries: SessionQueries, entries: SessionEntries,
    operations: SessionOperations, run_lifecycle: SessionRuns, session_id: str,
    operation_id: str,
    origin: str, operation_request: object,
    credentials: Callable[[str], tuple[str, ...]],
    prepare: Callable[[], Awaitable[tuple[ModelConfig, dict[str, AgentTool], tuple[str, ...], bool]]],
    accept: Callable[..., Awaitable[OperationOutcome]],
    coordinator: SteeringCoordinator, business: BusinessService,
    executor: ThreadPoolExecutor, trace_store,
    create_state: Callable[..., RunState], create_events: Callable,
) -> AdmissionResult:
    async with operation_lock(operation_id), session_gate(session_id):
        outcome = await operations.resolve_operation(
            operation_id, session_id, origin, operation_request,
            credentials=credentials(session_id),
        )
        if outcome is not None:
            if CredentialFilter(credentials(session_id)).contains(outcome.model_dump()):
                raise CredentialDetected()
            return AdmissionResult(outcome)
        if is_fault_closed():
            raise AdmissionUnavailable("service_unavailable")
        if not accepting_runs.is_set():
            raise AdmissionUnavailable("closing")
        await queries.get_session(session_id)
        if await queries.find_running_run_for_session(session_id) is not None:
            raise RunBusy()
        if not session_run_limits.available(session_id):
            raise AdmissionUnavailable(
                "rate_limited", session_run_limits.retry_after(session_id),
            )
        model, tools, secrets, disconnected = await prepare()
        run_scope = current_run_scope()
        if not acquire_run_slot(run_scope):
            raise AdmissionUnavailable("capacity_exceeded")
        slot_owned = True
        accepted_run = None
        try:
            outcome = await accept(service, tools, secrets)
            if not outcome.created:
                release_run_slot(run_scope)
                slot_owned = False
                return AdmissionResult(outcome)
            accepted_run = run = outcome.run
            if not session_run_limits.consume(session_id):
                logger.error("会话运行额度扣减失败（%s）", session_id)
            trace = RunTrace(
                session_id=session_id, operation_id=operation_id, run_id=run.id,
                contains=CredentialFilter(secrets).contains,
                accepted_at=time_ns() // 1_000_000, accepted_perf=perf_counter(),
            )
            state = create_state(UUID(session_id), run_id=UUID(run.id))
            state.run_scope = run_scope
            state.secrets = secrets
            state.operation_id = operation_id
            state.origin = origin
            state.request_entry_id = run.request_entry_id
            state.coordinator = coordinator
            coordinator.open(
                session_id, run.id, lambda data: state.publish("steering_status", data),
            )
            loop = asyncio.get_running_loop()
            task = loop.create_task(coordinate(
                run, state, session_id, tools, model, secrets,
                queries, entries, run_lifecycle, business, coordinator, executor, loop, trace, trace_store,
                create_events=create_events,
            ))
            state.task = task
            with runs_lock:
                runs[state.run_id] = state
            slot_owned = False
            with runs_lock:
                track(task)
                closing = not accepting_runs.is_set()
            if disconnected or closing:
                state.control.request_cancel("cancelled")
                coordinator.close(run.id)
            return AdmissionResult(outcome, state)
        except (
            OperationExpired, CredentialDetected, RunBusy, InvalidTargetEntry,
            EntryNotFound, IncompleteToolChain, OperationConflict,
            SessionMismatch, SessionNotFound,
        ):
            if slot_owned:
                release_run_slot(run_scope)
            raise
        except BaseException:
            if slot_owned:
                try:
                    if accepted_run is not None:
                        with suppress(Exception):
                            await run_lifecycle.finish_run(
                                session_id, accepted_run.id, "failed",
                                error_code="execution_failed",
                                error_message="执行失败，请重新发起请求。",
                            )
                finally:
                    release_run_slot(run_scope)
            raise
