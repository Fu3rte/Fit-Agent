import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

from app.application.session.attachment_files import (
    AttachmentFiles,
    CreatedAttachment,
    PreparedUpload,
)
from app.application.session.credentials import reject_credentials
from app.application.session.operations import same_request
from app.application.session.queries import SessionQueries
from app.domain.session.attachments import (
    PROJECT_ROOT,
    AttachmentError,
    AttachmentInput,
    AttachmentMetadata,
)
from app.domain.session.models import EditCommand, SendCommand, SteeringCommand
from app.domain.session.repository import SessionRepository


class SessionAttachments:
    def __init__(
        self,
        repository: SessionRepository,
        queries: SessionQueries,
        *,
        project_root: Path | None = None,
    ) -> None:
        self._repository = repository
        self._queries = queries
        self._project_root = project_root or PROJECT_ROOT

    def files(self, credentials: Sequence[str]) -> AttachmentFiles:
        return AttachmentFiles(
            self._project_root,
            check_credentials=partial(reject_credentials, credentials=credentials),
        )

    @asynccontextmanager
    async def attachment_transaction(
        self, command: SendCommand | EditCommand | SteeringCommand, kind: str,
        credentials: Sequence[str],
    ) -> AsyncIterator[list[CreatedAttachment]]:
        created: list[CreatedAttachment] = []
        try:
            async with self._repository.transaction():
                yield created
        except BaseException:
            if not created:
                raise

            async def verify() -> None:
                async with self._repository.transaction():
                    if await self._repository.get_session(command.session_id) is None:
                        raise AttachmentError("internal_error", "会话受理事实无法核实")
                    operation = await self._repository.get_operation(command.operation_id)
                    invalidated = await self._repository.get_operation_invalidation(command.operation_id)
                    if invalidated is not None:
                        raise AttachmentError("internal_error", "受理事实无法核实")
                    committed = await self._repository.get_attachments(
                        [item.attachment_id for item in created]
                    )
                    if operation is not None:
                        if operation.session_id != command.session_id or operation.kind != kind or not same_request(kind, operation.request, command.request):
                            raise AttachmentError("internal_error", "受理身份无法核实")
                        run = await self._queries.require_run(operation.session_id, operation.run_id)
                        if kind == "steering":
                            steering = await self._queries.require_steering(operation.session_id, operation.steering_id)
                            if steering.run_id != run.id:
                                raise AttachmentError("internal_error", "受理关联无法核实")
                            actual = steering.attachments
                        else:
                            await self._queries.require_entry(operation.session_id, run.request_entry_id)
                            actual = await self._repository.list_entry_attachments(operation.session_id, run.request_entry_id)
                        expected = [AttachmentMetadata.model_validate(item) for item in operation.request["accepted_attachments"]]
                        if actual != expected or any(item.attachment_id not in committed for item in created):
                            raise AttachmentError("internal_error", "受理附件事实无法核实")
                        return
                    if committed:
                        raise AttachmentError("internal_error", "附件提交事实无法核实")
                    for item in created:
                        self.files(credentials).remove_created(item)
            pending = asyncio.create_task(verify())
            while not pending.done():
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError:
                    asyncio.current_task().uncancel()
            pending.result()
            raise

    async def accept_attachments(
        self, session_id: str, inputs: list[AttachmentInput],
        created: list[CreatedAttachment], now: int, credentials: Sequence[str],
    ) -> list[AttachmentMetadata]:
        if not inputs:
            return []
        files = self.files(credentials)
        existing = await self._repository.get_attachments(
            [item.attachment_id for item in inputs]
        )
        prepared = files.prepare(
            session_id, [item.model_dump() for item in inputs], existing=existing,
        )
        metadata: list[AttachmentMetadata] = []
        for item in prepared:
            if isinstance(item, PreparedUpload):
                try:
                    stored = files.write(
                        session_id, item, created_at=now, on_created=created.append,
                    )
                except FileExistsError as error:
                    raise AttachmentError("attachment_conflict", "附件 ID 已被占用") from error
                await self._repository.insert_attachment(stored)
                metadata.append(stored)
            else:
                metadata.append(item)
        return metadata

    async def get_attachment_content(
        self, session_id: str, attachment_id: str, *, credentials: Sequence[str] = (),
    ) -> tuple[AttachmentMetadata, str]:
        async with self._repository.transaction():
            await self._queries.require_session(session_id)
            metadata = (await self._repository.get_attachments([attachment_id])).get(attachment_id)
            if metadata is None:
                raise AttachmentError("attachment_not_found", "附件不存在")
            text = self.files(credentials).read(session_id, metadata)
            return metadata, text
