import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from app.ai.messages import UserMessage
from app.application.session.service import SessionService
from app.domain.session.errors import RunClosed, SteeringConsumptionConflict
from app.domain.session.models import (
    RunOutcome,
    SteeringCommand,
    SteeringInput,
    SteeringWithdrawal,
    TerminalRunStatus,
)


class _PriorityGate:
    # 按运行裁决：消费等待者存在时，撤回、受理与收尾让位，保证消费优先。
    def __init__(self) -> None:
        self._busy = False
        self._consume_waiting = 0
        self._condition = asyncio.Condition()

    async def _release(self) -> None:
        async with self._condition:
            self._busy = False
            self._condition.notify_all()

    @asynccontextmanager
    async def consume_priority(self) -> AsyncIterator[None]:
        async with self._condition:
            self._consume_waiting += 1
            try:
                while self._busy:
                    await self._condition.wait()
                self._busy = True
            finally:
                self._consume_waiting -= 1
        try:
            yield
        finally:
            await self._release()

    @asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        async with self._condition:
            while self._busy or self._consume_waiting:
                await self._condition.wait()
            self._busy = True
        try:
            yield
        finally:
            await self._release()


@dataclass
class RunSteeringBoundary:
    session_id: str
    notify: Callable[[dict], None]
    queue: deque[tuple[str, UserMessage]] = field(default_factory=deque)
    occupied: dict[int, str] = field(default_factory=dict)
    accepting: bool = True
    gate: _PriorityGate = field(default_factory=_PriorityGate)


class SteeringCoordinator:
    def __init__(self, service: SessionService) -> None:
        self._service = service
        self._boundaries: dict[str, RunSteeringBoundary] = {}

    def open(
        self, session_id: str, run_id: str, notify: Callable[[dict], None]
    ) -> None:
        self._boundaries[run_id] = RunSteeringBoundary(session_id, notify)

    def close(self, run_id: str) -> None:
        boundary = self._boundaries.get(run_id)
        if boundary is not None:
            boundary.accepting = False

    async def accept(
        self,
        run_id: str,
        command: SteeringCommand,
        *,
        credentials: Sequence[str] = (),
    ) -> tuple[bool, SteeringInput]:
        # 重复查重先于目标运行是否仍接受输入的检查。
        if await self._service.get_operation(command.operation_id) is not None:
            outcome = await self._service.accept_steering(
                command, credentials=credentials
            )
            return False, outcome.steering
        boundary = self._boundaries.get(run_id)
        if boundary is None:
            # 无内存边界：以数据库为操作身份权威，已提交重复返回原身份。
            if await self._service.get_operation(command.operation_id) is None:
                raise RunClosed("目标运行已关闭接受入口")
            outcome = await self._service.accept_steering(
                command, credentials=credentials
            )
            return False, outcome.steering
        async with boundary.gate.exclusive():
            boundary = self._boundaries.get(run_id)
            closed = boundary is None or not boundary.accepting
            if closed and (
                await self._service.get_operation(command.operation_id) is None
            ):
                # 入口关闭后新操作被拒绝；并发已提交的重复操作仍返回原身份。
                raise RunClosed("目标运行已关闭接受入口")
            outcome = await self._service.accept_steering(
                command, credentials=credentials
            )
            if outcome.created and boundary is not None:
                boundary.queue.append(
                    (outcome.steering.id, outcome.steering.message)
                )
            return outcome.created, outcome.steering

    async def withdraw(
        self, run_id: str, session_id: str, steering_id: str
    ) -> SteeringWithdrawal:
        boundary = self._boundaries.get(run_id)
        if boundary is not None and steering_id in boundary.occupied.values():
            raise SteeringConsumptionConflict("目标输入正在被消费")
        if boundary is None:
            return await self._service.withdraw_steering(
                session_id, run_id, steering_id
            )
        async with boundary.gate.exclusive():
            boundary = self._boundaries.get(run_id)
            if boundary is not None and steering_id in boundary.occupied.values():
                raise SteeringConsumptionConflict("目标输入正在被消费")
            withdrawal = await self._service.withdraw_steering(
                session_id, run_id, steering_id
            )
            if boundary is not None and withdrawal.changed:
                boundary.queue = deque(
                    (item_id, message)
                    for item_id, message in boundary.queue
                    if item_id != steering_id
                )
                boundary.notify({
                    "steering_id": steering_id,
                    "status": "withdrawn",
                    "entry_id": None,
                    "reason": None,
                })
            return withdrawal

    async def take(self, run_id: str) -> list[UserMessage]:
        boundary = self._boundary(run_id)
        async with boundary.gate.consume_priority():
            return self._drain(boundary)

    async def take_or_close(self, run_id: str) -> list[UserMessage]:
        boundary = self._boundary(run_id)
        async with boundary.gate.consume_priority():
            messages = self._drain(boundary)
            if not messages:
                boundary.accepting = False
            return messages

    async def consume(self, run_id: str, messages: list[UserMessage]) -> str | None:
        boundary = self._boundary(run_id)
        async with boundary.gate.consume_priority():
            last_entry_id: str | None = None
            for message in messages:
                steering_id = boundary.occupied.get(id(message))
                if steering_id is None:
                    continue
                # 提交前保持占用；单条事务成功后立即解除占用并发布该条确认。
                result = await self._service.consume_steering(
                    boundary.session_id, run_id, steering_id
                )
                boundary.occupied.pop(id(message), None)
                if not result.created:
                    continue
                last_entry_id = result.steering.entry_id
                boundary.notify({
                    "steering_id": result.steering.id,
                    "status": "consumed",
                    "entry_id": result.steering.entry_id,
                    "reason": None,
                })
            return last_entry_id

    async def finish(
        self,
        session_id: str,
        run_id: str,
        status: TerminalRunStatus,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> RunOutcome:
        boundary = self._boundaries.get(run_id)
        if boundary is None:
            return await self._service.finish_run(
                session_id,
                run_id,
                status,
                error_code=error_code,
                error_message=error_message,
            )
        async with boundary.gate.exclusive():
            boundary = self._boundaries.get(run_id)
            pending = await self._service.list_pending_steering(session_id, run_id)
            outcome = await self._service.finish_run(
                session_id,
                run_id,
                status,
                error_code=error_code,
                error_message=error_message,
            )
            if boundary is not None:
                boundary.accepting = False
                for steering in pending:
                    boundary.notify({
                        "steering_id": steering.id,
                        "status": "discarded",
                        "entry_id": None,
                        "reason": status,
                    })
                self._boundaries.pop(run_id, None)
            return outcome

    def _boundary(self, run_id: str) -> RunSteeringBoundary:
        boundary = self._boundaries.get(run_id)
        if boundary is None:
            raise RunClosed("运行已关闭接受入口")
        return boundary

    def _drain(self, boundary: RunSteeringBoundary) -> list[UserMessage]:
        messages: list[UserMessage] = []
        while boundary.queue:
            steering_id, message = boundary.queue.popleft()
            boundary.occupied[id(message)] = steering_id
            messages.append(message)
        return messages
