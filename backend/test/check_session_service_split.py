import asyncio
import sqlite3
from pathlib import Path
from uuid import uuid4

from app.application.session.service import SessionService
from app.domain.session.errors import CredentialDetected, SessionConflict
from app.domain.session.models import (
    SendCommand,
    SendRequest,
    SteeringCommand,
    SteeringRequest,
)
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from test.check_session_store import (
    ASSISTANT_FINAL_SOURCE,
    entry,
    message,
    system_message,
)

ROOT = Path(__file__).resolve().parents[1] / "temp" / "session-service-split"


def send_command(session_id: str, text: str = "跨模块事务") -> SendCommand:
    return SendCommand(
        operation_id=str(uuid4()), session_id=session_id, request=SendRequest(text=text)
    )


async def check_shared_repository(root: Path) -> None:
    database = await open_database(root / "shared.db")
    service = SessionService(SqliteSessionRepository(database), project_root=root)
    try:
        # 所有能力共享同一实际 repository 与 database。
        assert service.queries._repository is service._repository
        assert service.entries._repository is service._repository
        assert service.runs._repository is service._repository
        assert service.operations._repository is service._repository
        assert service.attachments._repository is service._repository
        assert service.removal._repository is service._repository
        await service.create_session("shared", "共享")
        outcome = await service.accept_send(send_command("shared"), system_message=system_message())
        history = await service.queries.get_session_history("shared")
        assert outcome.run.id in {run.id for run in history.runs}
        assert history.session.active_leaf_id == outcome.run.request_entry_id
        await service.runs.finish_run("shared", outcome.run.id, "completed")
    finally:
        await database.close()


async def check_acceptance_atomicity(root: Path) -> None:
    database = await open_database(root / "atomic.db")
    service = SessionService(SqliteSessionRepository(database), project_root=root)
    try:
        await service.create_session("atomic", "原子")
        await database.connection.execute(
            "CREATE TEMP TRIGGER split_fail_operation BEFORE INSERT ON session_operations "
            "BEGIN SELECT RAISE(ABORT, 'split controlled failure'); END"
        )
        try:
            await service.accept_send(send_command("atomic"), system_message=system_message())
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("受理写入失败未传播")
        await database.connection.execute("DROP TRIGGER split_fail_operation")
        # 用户节点、运行、操作与分支指针同一事务回滚。
        assert await database.read(lambda connection: _count(connection)) == 0
        assert await service.queries.list_entries("atomic") == []
        assert await service.queries.list_runs("atomic") == []
        assert (await service.queries.get_session("atomic")).active_leaf_id is None
    finally:
        await database.close()


async def _count(connection) -> int:
    cursor = await connection.execute("SELECT count(*) FROM session_runs")
    row = await cursor.fetchone()
    await cursor.close()
    return row[0]


async def check_credential_and_steering(root: Path) -> None:
    database = await open_database(root / "credential.db")
    service = SessionService(SqliteSessionRepository(database), project_root=root)
    try:
        await service.create_session("cred", "凭据")
        accepted = await service.accept_send(
            send_command("cred", "正常"), system_message=system_message()
        )
        steering = await service.accept_steering(
            SteeringCommand(
                operation_id=str(uuid4()),
                session_id="cred",
                request=SteeringRequest(target_run_id=accepted.run.id, text="待丢弃"),
            )
        )
        outcome = await service.entries.append_entry(
            entry(
                "cred",
                "cred-a1",
                accepted.run.request_entry_id,
                accepted.run.id,
                message(ASSISTANT_FINAL_SOURCE, content=[{"type": "text", "text": "secret-key"}]),
                900,
            ),
            credentials=["secret-key"],
        )
        assert outcome.credential_detected and outcome.entry is None
        run = await service.queries.get_run("cred", accepted.run.id)
        assert run.status == "failed" and run.error_code == "credential_detected"
        dropped = await service.queries.get_steering("cred", steering.steering.id)
        assert dropped.status == "discarded" and dropped.reason == "failed"
        assert await database.read(lambda connection: _entry_count(connection, "cred-a1")) == 0
    finally:
        await database.close()


async def _entry_count(connection, entry_id: str) -> int:
    cursor = await connection.execute(
        "SELECT count(*) FROM session_entries WHERE id = ?", (entry_id,)
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row[0]


async def check_request_credentials(root: Path) -> None:
    database = await open_database(root / "request-credential.db")
    service = SessionService(SqliteSessionRepository(database), project_root=root)
    try:
        await service.create_session("request-cred", "请求凭据")
        try:
            await service.accept_send(
                send_command("request-cred", "包含 secret-key 的请求"),
                system_message=system_message(),
                credentials=["secret-key"],
            )
        except CredentialDetected:
            pass
        else:
            raise AssertionError("请求凭据未拒绝")
        assert await service.queries.list_runs("request-cred") == []
    finally:
        await database.close()


async def check_compaction_prepared_position(root: Path) -> None:
    database = await open_database(root / "compaction.db")
    repository = SqliteSessionRepository(database)
    service = SessionService(repository, project_root=root)
    try:
        await service.create_session("comp", "压缩")
        accepted = await service.accept_send(
            send_command("comp", "需要压缩"), system_message=system_message()
        )
        appended = await service.entries.append_entry(
            entry(
                "comp",
                "comp-a1",
                accepted.run.request_entry_id,
                accepted.run.id,
                message(ASSISTANT_FINAL_SOURCE),
                901,
            )
        )
        from test.check_compaction_session import baseline_node, compaction_node

        checkpoint = compaction_node("comp", "comp-c1", "comp-a1", accepted.run.request_entry_id, accepted.run.id)
        try:
            await service.entries.commit_compaction(
                checkpoint, baseline_node("comp", checkpoint), prepared_position="wrong"
            )
        except SessionConflict:
            pass
        else:
            raise AssertionError("准备位置不一致未拒绝")
        assert await repository.get_entry("comp", "comp-c1") is None
        assert (await service.queries.get_run("comp", accepted.run.id)).last_entry_id == appended.entry.id
    finally:
        await database.close()


async def check_independent_queries(root: Path) -> None:
    from app.application.session.queries import SessionQueries

    database = await open_database(root / "queries.db")
    repository = SqliteSessionRepository(database)
    service = SessionService(repository, project_root=root)
    try:
        await service.create_session("query", "独立查询")
        await service.accept_send(send_command("query"), system_message=system_message())
        queries = SessionQueries(repository)
        history = await queries.get_session_history("query")
        assert history.session.id == "query" and len(history.entries) == 2
        assert len(await queries.get_current_branch("query")) == 2
        projection = await queries.get_projection("query", history.session.active_leaf_id)
        assert projection.context.source_entry_ids == [entry.id for entry in projection.path]
    finally:
        await database.close()


async def run() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    for item in ROOT.glob("*.db"):
        item.unlink()
    await check_shared_repository(ROOT)
    await check_acceptance_atomicity(ROOT)
    await check_credential_and_steering(ROOT)
    await check_request_credentials(ROOT)
    await check_compaction_prepared_position(ROOT)
    await check_independent_queries(ROOT)
    print("PASS: 共享 repository、受理原子回滚、凭据失败与输入丢弃、准备位置核验、独立查询协作者")


if __name__ == "__main__":
    asyncio.run(run())
