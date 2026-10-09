import asyncio
import base64
import json
import sqlite3
import stat
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.ai.messages import SystemMessage
from app.application.session.service import SessionService
from app.application.session.steering import SteeringCoordinator
from app.domain.session.attachments import AttachmentError
from app.domain.session.errors import (
    CredentialDetected,
    OperationConflict,
    OperationExpired,
)
from app.domain.session.models import (
    EditCommand,
    EditRequest,
    RegenerateCommand,
    RegenerateRequest,
    SendCommand,
    SendRequest,
    SteeringCommand,
    SteeringRequest,
)
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository

ROOT = Path(__file__).resolve().parents[2] / "tmp/backend-plan-import/checks"
SYSTEM = SystemMessage(role="system", content="真实服务存储专项", timestamp=1)


def uid() -> str:
    return str(uuid4())


def upload(data: bytes = b"plan", name: str = "plan.md", identity: str | None = None) -> dict:
    return {"kind": "upload", "attachment_id": identity or uid(), "file_name": name,
            "data_base64": base64.b64encode(data).decode("ascii")}


def reference(identity: str) -> dict:
    return {"kind": "reference", "attachment_id": identity}


def send_command(session: str, text: str = "", attachments: list | None = None) -> SendCommand:
    return SendCommand(operation_id=uid(), session_id=session,
                       request=SendRequest(text=text, attachments=attachments or []))


async def positive(root: Path) -> None:
    database = await open_database(root / "positive.db")
    repository = SqliteSessionRepository(database)
    service = SessionService(repository, project_root=root)
    session = uid()
    await service.create_session(session, "计划.md")
    first, second = upload("# 训练\r\n深蹲\ufeff".encode()), upload(b"notes", "notes.txt")
    command = send_command(session, "  整理  ", [first, second])
    accepted = await service.accept_send(command, system_message=SYSTEM)
    retry = await service.accept_send(command, system_message=SYSTEM)
    assert accepted.created and not retry.created and retry.run == accepted.run
    history = await service.get_session_history(session)
    user = history.entries[-1]
    assert user.messages[0].content == "  整理  "
    assert [item.attachment_id for item in user.attachments] == [first["attachment_id"], second["attachment_id"]]
    assert len((await repository.list_operations(session))) == 1
    assert len((await repository.list_entries(session))) == 2
    for changed in ([second, first], [first], [upload(b"changed", identity=first["attachment_id"]), second]):
        with pytest.raises(OperationConflict):
            await service.accept_send(command.model_copy(update={"request": SendRequest(text="  整理  ", attachments=changed)}), system_message=SYSTEM)
    await service.finish_run(session, accepted.run.id, "completed")
    pure = send_command(session, "", [reference(first["attachment_id"])])
    pure_outcome = await service.accept_send(pure, system_message=SYSTEM)
    assert (await service.get_session_history(session)).entries[-1].messages[0].content == ""
    await service.finish_run(session, pure_outcome.run.id, "completed")
    old_edit = EditCommand(operation_id=uid(), session_id=session,
                           request=EditRequest(target_entry_id=pure_outcome.run.request_entry_id, text=" "))
    edited = await service.accept_edit(old_edit)
    assert (await service.get_entry(session, edited.run.request_entry_id)).attachments[0].attachment_id == first["attachment_id"]
    assert await repository.get_entry(session, pure_outcome.run.request_entry_id) is None
    assert not (await service.accept_edit(old_edit)).created
    with pytest.raises(OperationConflict):
        await service.accept_edit(old_edit.model_copy(update={"request": EditRequest(target_entry_id=old_edit.request.target_entry_id, text=" ", attachments=[reference(first["attachment_id"])])}))
    assert edited.operation.request["accepted_attachments"][0]["attachment_id"] == first["attachment_id"]
    with pytest.raises(OperationExpired):
        await service.accept_send(pure, system_message=SYSTEM)
    await service.finish_run(session, edited.run.id, "completed")
    replacement = upload(b"replacement", "new.txt")
    replace = EditCommand(operation_id=uid(), session_id=session,
                          request=EditRequest(target_entry_id=edited.run.request_entry_id, text="", attachments=[reference(second["attachment_id"]), replacement]))
    replaced = await service.accept_edit(replace)
    assert [item.attachment_id for item in (await service.get_entry(session, replaced.run.request_entry_id)).attachments] == [second["attachment_id"], replacement["attachment_id"]]
    assert [item.attachment_id for item in (await service.get_session_history(session)).entries[-1].attachments] == [second["attachment_id"], replacement["attachment_id"]]
    await service.finish_run(session, replaced.run.id, "completed")
    regen_command = RegenerateCommand(operation_id=uid(), session_id=session,
                                      request=RegenerateRequest(target_entry_id=replaced.run.request_entry_id))
    regenerated = await service.accept_regenerate(regen_command)
    assert regenerated.run.request_entry_id == replaced.run.request_entry_id
    assert len((await service.get_entry(session, regenerated.run.request_entry_id)).attachments) == 2
    assert [item.attachment_id for item in (await service.get_session_history(session)).entries[-1].attachments] == [second["attachment_id"], replacement["attachment_id"]]
    assert not (await service.accept_regenerate(regen_command)).created
    await service.finish_run(session, regenerated.run.id, "completed")
    removed = await service.accept_edit(EditCommand(operation_id=uid(), session_id=session,
        request=EditRequest(target_entry_id=replaced.run.request_entry_id, text="移除", attachments=[])))
    assert (await service.get_entry(session, removed.run.request_entry_id)).attachments == []
    assert (await service.get_session_history(session)).entries[-1].attachments == []
    await service.finish_run(session, removed.run.id, "completed")
    with pytest.raises(AttachmentError) as empty:
        await service.accept_edit(EditCommand(operation_id=uid(), session_id=session,
            request=EditRequest(target_entry_id=removed.run.request_entry_id, text=" ")))
    assert empty.value.code == "invalid_request"
    assert (await service.get_entry(session, removed.run.request_entry_id)).messages[0].content == "移除"
    original_metadata, original_text = await service.get_attachment_content(session, first["attachment_id"])
    assert original_text.encode() == base64.b64decode(first["data_base64"])
    original_path = root / original_metadata.storage_ref
    workspace = original_path.parent.parent / "workspace"
    workspace.mkdir()
    (workspace / "work.txt").write_bytes(b"work")
    await service.delete_session(session)
    assert original_path.read_bytes() == original_text.encode()
    assert (workspace / "work.txt").read_bytes() == b"work"
    assert await repository.get_attachments([first["attachment_id"]]) == {}
    await database.close()
    reopened = await open_database(root / "positive.db")
    assert original_path.read_bytes() == original_text.encode()
    await reopened.close()


async def steering_checks(root: Path) -> None:
    database = await open_database(root / "steering.db")
    repository = SqliteSessionRepository(database)
    service = SessionService(repository, project_root=root)
    session = uid()
    await service.create_session(session, "Steering")
    run = (await service.accept_send(send_command(session, "开始"), system_message=SYSTEM)).run
    events = []
    coordinator = SteeringCoordinator(service)
    coordinator.open(session, run.id, events.append)
    files = [upload(b"first"), upload(b"second", "second.txt")]
    command = SteeringCommand(operation_id=uid(), session_id=session,
        request=SteeringRequest(target_run_id=run.id, text="", attachments=files))
    created, pending = await coordinator.accept(run.id, command)
    assert created and len(pending.attachments) == 2
    assert len(await repository.list_entries(session)) == 2
    assert not (await coordinator.accept(run.id, command))[0]
    messages = await coordinator.take(run.id)
    assert len(messages) == 1 and messages[0].content == ""
    entry_id = await coordinator.consume(run.id, messages)
    assert len(events) == 1 and events[0]["entry_id"] == entry_id
    consumed = await service.consume_steering(session, run.id, pending.id)
    assert not consumed.created and consumed.entry.attachments == consumed.steering.attachments == pending.attachments
    history = await service.get_session_history(session)
    assert history.entries[-1].attachments == history.steering[0].attachments
    outcome = await service.get_operation_outcome(session, command.operation_id)
    assert outcome.steering.attachments == pending.attachments
    withdrawn_command = SteeringCommand(operation_id=uid(), session_id=session,
        request=SteeringRequest(target_run_id=run.id, text="撤回", attachments=[reference(files[0]["attachment_id"])]))
    _, withdrawable = await coordinator.accept(run.id, withdrawn_command)
    withdrawal = await coordinator.withdraw(run.id, session, withdrawable.id)
    assert withdrawal.steering.status == "withdrawn" and len(withdrawal.steering.attachments) == 1
    assert len(await repository.list_entries(session)) == 3
    conflict_id = uid()
    competing = [SteeringCommand(operation_id=uid(), session_id=session,
        request=SteeringRequest(target_run_id=run.id, text="", attachments=[upload(data, name, conflict_id)]))
        for data, name in ((b"md", "same.md"), (b"txt", "same.txt"))]
    results = await asyncio.gather(*(service.accept_steering(item) for item in competing), return_exceptions=True)
    winners = [item for item in results if not isinstance(item, BaseException)]
    losers = [item for item in results if isinstance(item, AttachmentError)]
    assert len(winners) == len(losers) == 1 and losers[0].code == "attachment_conflict"
    metadata = (await repository.get_attachments([conflict_id]))[conflict_id]
    winner_input = winners[0].operation.request["attachments"][0]
    assert metadata.file_name == winner_input["file_name"]
    assert (root / metadata.storage_ref).read_bytes() == base64.b64decode(winner_input["data_base64"], validate=True)
    assert len(list((root / metadata.storage_ref).parent.glob(conflict_id + ".*"))) == 1
    _, discarded = await coordinator.accept(run.id, SteeringCommand(operation_id=uid(), session_id=session,
        request=SteeringRequest(target_run_id=run.id, text="", attachments=[reference(files[1]["attachment_id"])])))
    await coordinator.finish(session, run.id, "cancelled")
    assert (await service.get_steering(session, discarded.id)).status == "discarded"
    assert len((await service.get_steering(session, discarded.id)).attachments) == 1
    resumed = (await service.accept_send(send_command(session, "恢复"), system_message=SYSTEM)).run
    pending_restart = await service.accept_steering(SteeringCommand(operation_id=uid(), session_id=session,
        request=SteeringRequest(target_run_id=resumed.id, text="", attachments=[reference(files[0]["attachment_id"])])))
    await database.close()
    reopened = await open_database(root / "steering.db")
    restored = SessionService(SqliteSessionRepository(reopened), project_root=root)
    await restored.recover_interrupted()
    restored_history = await restored.get_session_history(session)
    restored_entry = next(item for item in restored_history.entries if item.id == entry_id)
    restored_input = next(item for item in restored_history.steering if item.id == pending.id)
    assert restored_entry.attachments == restored_input.attachments == pending.attachments
    restarted = await restored.get_steering(session, pending_restart.steering.id)
    assert restarted.status == "discarded" and restarted.reason == "interrupted" and len(restarted.attachments) == 1
    assert not (await restored.accept_steering(pending_restart.operation and SteeringCommand(operation_id=pending_restart.operation.operation_id, session_id=session,
        request=SteeringRequest(target_run_id=resumed.id, text="", attachments=[reference(files[0]["attachment_id"])])))).created
    await reopened.close()


async def boundaries(root: Path) -> None:
    database = await open_database(root / "boundaries.db")
    repository = SqliteSessionRepository(database)
    service = SessionService(repository, project_root=root)
    session, other = uid(), uid()
    await service.create_session(session, "大小")
    await service.create_session(other, "归属")
    exact = upload(b"x" * 100000)
    accepted = await service.accept_send(send_command(session, "", [exact]), system_message=SYSTEM)
    await service.finish_run(session, accepted.run.id, "completed")
    for attachments, code in (([upload(b"x" * 100001)], "attachment_size_exceeded"),
        ([reference(exact["attachment_id"]), upload(b"x")], "attachment_size_exceeded"),
        ([reference(uid())], "attachment_not_found"),
        ([reference(exact["attachment_id"])] * 2, "invalid_request"),
        ([upload(identity=exact["attachment_id"])], "attachment_conflict")):
        with pytest.raises(AttachmentError) as failure:
            await service.accept_send(send_command(session, "", attachments), system_message=SYSTEM)
        assert failure.value.code == code
    with pytest.raises(AttachmentError) as denied:
        await service.accept_send(send_command(other, "", [reference(exact["attachment_id"])]), system_message=SYSTEM)
    assert denied.value.code == "attachment_access_denied"
    for item in (upload(b"protected-credential"), upload(name="protected-credential.md")):
        with pytest.raises(CredentialDetected):
            await service.accept_send(send_command(session, "", [item]), system_message=SYSTEM, credentials=("protected-credential",))
    for item in (upload(b"\xff"), {**upload(), "data_base64": "!"}, upload(name="bad.pdf")):
        with pytest.raises(ValidationError):
            send_command(session, "", [item])
    with pytest.raises(CredentialDetected):
        await service.accept_send(send_command(session, "", [reference(exact["attachment_id"])]), system_message=SYSTEM, credentials=("xxx",))
    metadata = (await repository.get_attachments([exact["attachment_id"]]))[exact["attachment_id"]]
    path = root / metadata.storage_ref
    path.chmod(stat.S_IWRITE | stat.S_IREAD)
    path.write_bytes(b"\xff" * 100000)
    with pytest.raises(UnicodeDecodeError):
        await service.get_attachment_content(session, exact["attachment_id"])
    assert len(await repository.list_operations(session)) == 1
    await database.close()


async def transaction_facts(root: Path) -> None:
    database = await open_database(root / "transactions.db")
    repository = SqliteSessionRepository(database)
    service = SessionService(repository, project_root=root)
    session = uid()
    await service.create_session(session, "事务")
    missing = uid()
    failure_ddl = f"CREATE TEMP TRIGGER fail_commit AFTER INSERT ON session_operations BEGIN INSERT INTO session_entry_attachments SELECT NEW.session_id, request_entry_id, '{missing}', 9 FROM session_runs WHERE id = NEW.run_id; END"
    await database.connection.execute(failure_ddl)
    await database.connection.execute("PRAGMA defer_foreign_keys = ON")
    failed_upload = upload()
    failed = send_command(session, "", [failed_upload])
    with pytest.raises(sqlite3.IntegrityError):
        await service.accept_send(failed, system_message=SYSTEM)
    assert await repository.get_operation(failed.operation_id) is None
    assert await repository.get_attachments([failed_upload["attachment_id"]]) == {}
    assert not list((root / "tmp/sessions" / session / "attachments").glob(failed_upload["attachment_id"] + ".*"))
    assert (await service.get_session_history(session)).entries == []
    await database.connection.execute("DROP TRIGGER fail_commit")
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), Event()

    def hold_commit(action, arg1, arg2, db_name, source):
        if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT":
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
        return sqlite3.SQLITE_OK

    await database.connection.set_authorizer(hold_commit)
    committed_upload = upload(b"committed despite cancelled await")
    committed = send_command(session, "", [committed_upload])
    task = asyncio.create_task(service.accept_send(committed, system_message=SYSTEM))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    await asyncio.sleep(0.02)
    task.cancel()
    release.set()
    result = (await asyncio.gather(task, return_exceptions=True))[0]
    assert isinstance(result, asyncio.CancelledError)
    await database.connection.set_authorizer(None)
    fact = await service.get_operation_outcome(session, committed.operation_id)
    assert fact is not None and fact.run.status == "running"
    metadata = (await repository.get_attachments([committed_upload["attachment_id"]]))[committed_upload["attachment_id"]]
    assert (root / metadata.storage_ref).read_bytes() == b"committed despite cancelled await"
    assert not (await service.accept_send(committed, system_message=SYSTEM)).created
    await service.finish_run(session, fact.run.id, "completed")
    await database.connection.execute(failure_ddl)
    replacement = upload(b"failed replacement")
    failed_edit = EditCommand(operation_id=uid(), session_id=session,
        request=EditRequest(target_entry_id=fact.run.request_entry_id, text="", attachments=[replacement]))
    with pytest.raises(sqlite3.IntegrityError):
        await service.accept_edit(failed_edit)
    await database.connection.execute("DROP TRIGGER fail_commit")
    retained = await service.get_entry(session, fact.run.request_entry_id)
    assert retained.attachments == [metadata]
    assert await repository.get_operation(committed.operation_id) is not None
    assert await repository.get_operation(failed_edit.operation_id) is None
    assert not list((root / "tmp/sessions" / session / "attachments").glob(replacement["attachment_id"] + ".*"))
    assert (root / metadata.storage_ref).read_bytes() == b"committed despite cancelled await"
    entered.clear()
    release.clear()

    def hold_insert() -> int:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return 1

    await database.connection.create_function("hold_attachment_insert", 0, hold_insert)
    await database.connection.execute("CREATE TEMP TRIGGER cancel_insert AFTER INSERT ON session_operations BEGIN SELECT hold_attachment_insert(); END")
    cancelled_upload = upload(b"uncommitted")
    cancelled = send_command(session, "", [cancelled_upload])
    task = asyncio.create_task(service.accept_send(cancelled, system_message=SYSTEM))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    release.set()
    assert isinstance((await asyncio.gather(task, return_exceptions=True))[0], asyncio.CancelledError)
    await database.connection.execute("DROP TRIGGER cancel_insert")
    assert await repository.get_operation(cancelled.operation_id) is None
    assert not list((root / "tmp/sessions" / session / "attachments").glob(cancelled_upload["attachment_id"] + ".*"))
    assert (root / metadata.storage_ref).read_bytes() == b"committed despite cancelled await"
    assert not database.connection.in_transaction
    await database.close()


async def legacy_operation(root: Path) -> None:
    database = await open_database(root / "legacy-operation.db")
    repository = SqliteSessionRepository(database)
    service = SessionService(repository, project_root=root)
    session = uid()
    await service.create_session(session, "旧账本")
    command = SendCommand(operation_id=uid(), session_id=session, request=SendRequest(text="legacy"))
    accepted = await service.accept_send(command, system_message=SYSTEM)
    await database.connection.execute("UPDATE session_operations SET request = ? WHERE operation_id = ?",
                                      (json.dumps({"text": "legacy"}), command.operation_id))
    await database.close()
    reopened = await open_database(root / "legacy-operation.db")
    restored = SessionService(SqliteSessionRepository(reopened), project_root=root)
    await restored.recover_interrupted()
    retried = await restored.accept_send(command, system_message=SYSTEM)
    assert not retried.created and retried.run.id == accepted.run.id and retried.run.status == "interrupted"
    assert "accepted_attachments" not in retried.operation.request
    await reopened.close()


async def main() -> None:
    root = ROOT / "attachment-session" / uuid4().hex
    root.mkdir(parents=True)
    await positive(root)
    await steering_checks(root)
    await boundaries(root)
    await transaction_facts(root)
    await legacy_operation(root)
    (root / "evidence.json").write_text(json.dumps({"service": "passed", "real_sqlite": True,
        "commit_cancel_fact": "accepted", "uncommitted_cancel": "cleaned", "multi_attachment": True,
        "model_execution": "not_run"}), encoding="utf-8")
    print("PASS: real attachment session send/edit/regenerate/Steering/history/restart/delete; boundaries; concurrent UUID; COMMIT failure/cancellation facts")
    print(root)


if __name__ == "__main__":
    asyncio.run(main())
