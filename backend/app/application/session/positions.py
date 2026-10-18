import time
import uuid

from app.domain.session.models import Session, SessionRun
from app.domain.session.repository import SessionRepository


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def new_id() -> str:
    return str(uuid.uuid4())


def new_run(session_id: str, request_entry_id: str, now: int) -> SessionRun:
    return SessionRun(
        session_id=session_id,
        id=new_id(),
        request_entry_id=request_entry_id,
        last_entry_id=None,
        status="running",
        started_at=now,
        finished_at=None,
        error_code=None,
        error_message=None,
        stop_reason=None,
        stop_diagnostic=None,
    )


def run_position(run: SessionRun) -> str:
    return (
        run.last_entry_id
        if run.last_entry_id is not None
        else run.request_entry_id
    )


async def set_leaf(
    repository: SessionRepository, session: Session, leaf_id: str, now: int
) -> None:
    await repository.update_session(
        session.model_copy(update={"active_leaf_id": leaf_id, "updated_at": now})
    )
