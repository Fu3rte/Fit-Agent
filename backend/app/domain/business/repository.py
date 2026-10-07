from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from app.domain.business.models import (
    CatalogExercise,
    ProfileContent,
    ProfileProposalStatus,
    ProfileRecord,
    ProfileSaveRecord,
    ProfileSnapshot,
)


class BusinessRepository(Protocol):
    def transaction(
        self, veto: Callable[[], bool] | None = None
    ) -> AbstractAsyncContextManager[None]: ...

    async def get_profile(self) -> ProfileRecord | None: ...

    async def save_profile(
        self, content: ProfileContent, version: int, updated_at: int
    ) -> None: ...

    # 画像待确认快照与保存幂等记录。

    async def insert_snapshot(self, snapshot: ProfileSnapshot) -> None: ...

    async def get_snapshot(self, proposal_id: str) -> ProfileSnapshot | None: ...

    async def list_snapshots(self, session_id: str) -> list[ProfileSnapshot]: ...

    async def find_snapshot_by_confirmation(
        self, session_id: str, confirmation_entry_id: str
    ) -> ProfileSnapshot | None: ...

    async def bind_display_entry(
        self, proposal_id: str, display_entry_id: str
    ) -> None: ...

    async def begin_save(self, proposal_id: str, confirmation_entry_id: str) -> None: ...

    async def set_snapshot_status(
        self, proposal_id: str, status: ProfileProposalStatus
    ) -> None: ...

    async def complete_save(self, record: ProfileSaveRecord) -> None: ...

    async def get_save_record(self, proposal_id: str) -> ProfileSaveRecord | None: ...

    async def list_save_records(self, session_id: str) -> list[ProfileSaveRecord]: ...

    async def find_save_record_by_confirmation(
        self, session_id: str, confirmation_entry_id: str
    ) -> ProfileSaveRecord | None: ...

    async def invalidate_pending(
        self, session_id: str, profile_id: int, keep_proposal_id: str
    ) -> None: ...

    async def delete_snapshots_for_entries(
        self, session_id: str, entry_ids: set[str]
    ) -> None: ...

    async def delete_snapshots_for_session(self, session_id: str) -> None: ...

    async def recover_interrupted_saves(self) -> int: ...

    async def replace_exercises(self, exercises: list[CatalogExercise]) -> None: ...

    async def count_exercises(self) -> int: ...
