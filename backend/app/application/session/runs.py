from collections.abc import Sequence

from app.agent.governance import DIAGNOSTIC_REASONS, RunDiagnosticReason
from app.application.session.credentials import (
    CREDENTIAL_ERROR_CODE,
    CREDENTIAL_SAFE_MESSAGE,
    contains_credential,
)
from app.application.session.positions import new_id, now_ms, run_position, set_leaf
from app.application.session.queries import SessionQueries
from app.domain.session.attachments import AttachmentError
from app.domain.session.errors import (
    RunClosed,
    SessionConflict,
    SessionMismatch,
    SteeringConsumptionConflict,
)
from app.domain.session.models import (
    RunOutcome,
    SessionMessageEntry,
    SessionRun,
    SteeringConsumption,
    SteeringInput,
    SteeringWithdrawal,
    TerminalRunStatus,
)
from app.domain.session.repository import SessionRepository


class SessionRuns:
    def __init__(
        self, repository: SessionRepository, queries: SessionQueries
    ) -> None:
        self._repository = repository
        self._queries = queries

    async def fail_on_credential_hit(
        self, session_id: str, run_id: str
    ) -> SessionRun:
        # 凭据命中的运行按原语义就地失败并丢弃待处理输入；调用方事务继续提交本次结果。
        async with self._repository.transaction():
            run = await self._queries.require_run(session_id, run_id)
            if run.status == "running":
                now = now_ms()
                run = run.model_copy(
                    update={
                        "status": "failed",
                        "finished_at": now,
                        "error_code": CREDENTIAL_ERROR_CODE,
                        "error_message": CREDENTIAL_SAFE_MESSAGE,
                    }
                )
                await self._repository.update_run(run)
                await self._discard_pending(session_id, run.id, "failed", now)
            return run

    async def consume_steering(
        self, session_id: str, run_id: str, steering_id: str
    ) -> SteeringConsumption:
        async with self._repository.transaction():
            steering = await self._queries.require_steering(session_id, steering_id)
            if steering.run_id != run_id:
                raise SessionConflict("Steering 输入不属于指定运行")
            if steering.status == "consumed":
                entry = await self._queries.require_message_entry(session_id, steering.entry_id)
                entry.attachments = await self._repository.list_entry_attachments(session_id, entry.id)
                if entry.attachments != steering.attachments:
                    raise AttachmentError("internal_error", "Steering 附件关系损坏")
                return SteeringConsumption(
                    created=False, steering=steering, entry=entry
                )
            if steering.status in {"withdrawn", "discarded"}:
                return SteeringConsumption(
                    created=False, steering=steering, entry=None
                )
            run = await self._queries.require_run(session_id, run_id)
            if run.status != "running":
                raise RunClosed("运行未在运行，拒绝消费 Steering")
            session = await self._queries.require_session(session_id)
            position = run_position(run)
            if session.active_leaf_id != position:
                raise SessionConflict("当前分支指针与运行进度不一致")
            entries = await self._repository.list_entries(session_id)
            await self._queries.ensure_pairable(session_id, entries, position)
            now = now_ms()
            entry = SessionMessageEntry(
                session_id=session_id,
                id=new_id(),
                parent_id=position,
                run_id=run_id,
                type="message",
                messages=[steering.message],
                created_at=now,
            )
            attachments = steering.attachments
            entry.attachments = attachments
            await self._repository.insert_entry(entry)
            await self._repository.bind_entry_attachments(
                session_id, entry.id, [item.attachment_id for item in attachments],
            )
            run = run.model_copy(update={"last_entry_id": entry.id})
            await self._repository.update_run(run)
            await set_leaf(self._repository, session, entry.id, now)
            steering = steering.model_copy(
                update={
                    "status": "consumed",
                    "entry_id": entry.id,
                    "updated_at": now,
                }
            )
            await self._repository.update_steering(steering)
            return SteeringConsumption(
                created=True, steering=steering, entry=entry
            )

    async def withdraw_steering(
        self, session_id: str, run_id: str, steering_id: str
    ) -> SteeringWithdrawal:
        async with self._repository.transaction():
            steering = await self._queries.require_steering(session_id, steering_id)
            if steering.run_id != run_id:
                raise SessionMismatch("Steering 输入不属于指定运行")
            if steering.status == "consumed":
                raise SteeringConsumptionConflict("已消费的 Steering 输入无法撤回")
            if steering.status != "pending":
                return SteeringWithdrawal(changed=False, steering=steering)
            steering = steering.model_copy(
                update={"status": "withdrawn", "updated_at": now_ms()}
            )
            await self._repository.update_steering(steering)
            return SteeringWithdrawal(changed=True, steering=steering)

    async def list_pending_steering(
        self, session_id: str, run_id: str
    ) -> list[SteeringInput]:
        await self._queries.require_run(session_id, run_id)
        return await self._repository.list_pending_steering(session_id, run_id)

    async def finish_run(
        self,
        session_id: str,
        run_id: str,
        status: TerminalRunStatus,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        stop_reason: str | None = None,
        credentials: Sequence[str] = (),
    ) -> RunOutcome:
        if contains_credential(
            {"error_code": error_code, "error_message": error_message}, credentials
        ):
            status = "failed"
            error_code = CREDENTIAL_ERROR_CODE
            error_message = CREDENTIAL_SAFE_MESSAGE
        async with self._repository.transaction():
            run = await self._queries.require_run(session_id, run_id)
            if run.status == status:
                return RunOutcome(changed=False, run=run)
            if run.status != "running":
                raise SessionConflict("运行已结束，无法再次结束")
            now = now_ms()
            run = run.model_copy(
                update={
                    "status": status,
                    "finished_at": now,
                    "error_code": error_code,
                    "error_message": error_message,
                    "stop_reason": stop_reason,
                }
            )
            await self._repository.update_run(run)
            await self._discard_pending(session_id, run_id, status, now)
            return RunOutcome(changed=True, run=run)

    async def record_run_diagnostic(
        self,
        session_id: str,
        run_id: str,
        diagnostic: RunDiagnosticReason,
    ) -> SessionRun:
        # 运行级诊断槽位：保留首个已记录证据，后续诊断不覆盖；供重启后核对故障原因。
        if diagnostic not in DIAGNOSTIC_REASONS:
            raise ValueError(f"未登记的运行诊断原因：{diagnostic}")
        async with self._repository.transaction():
            run = await self._queries.require_run(session_id, run_id)
            if run.stop_diagnostic is not None:
                return run
            run = run.model_copy(update={"stop_diagnostic": diagnostic})
            await self._repository.update_run(run)
            return run

    async def recover_interrupted(self) -> list[SessionRun]:
        async with self._repository.transaction():
            runs = await self._repository.list_running_runs()
            now = now_ms()
            recovered: list[SessionRun] = []
            for run in runs:
                run = run.model_copy(update={"status": "interrupted"})
                await self._repository.update_run(run)
                await self._discard_pending(
                    run.session_id, run.id, "interrupted", now
                )
                recovered.append(run)
            return recovered

    async def _discard_pending(
        self, session_id: str, run_id: str, reason: str, now: int
    ) -> None:
        pending = await self._repository.list_pending_steering(session_id, run_id)
        for steering in pending:
            await self._repository.update_steering(
                steering.model_copy(
                    update={
                        "status": "discarded",
                        "reason": reason,
                        "updated_at": now,
                    }
                )
            )
