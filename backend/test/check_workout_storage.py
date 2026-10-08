import asyncio
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import aiosqlite
import pytest
from pydantic import ValidationError

from app.ai.messages import AssistantMessage, TextContent, ToolCall, ToolResultMessage
from app.application.business.catalog import Catalog
from app.application.session.service import SessionService
from app.domain.business.errors import (
    WorkoutAccessDenied, WorkoutConfirmationInvalid, WorkoutNotFound,
    WorkoutProposalInvalidated, WorkoutProposalNotFound, WorkoutSaveProcessing,
    WorkoutVersionConflict,
)
from app.domain.business.models import (
    WorkoutContent, WorkoutExercise, WorkoutGetArguments, WorkoutListArguments,
    WorkoutProposal, WorkoutProposalArguments, WorkoutRecord, WorkoutSaveArguments,
    WorkoutSaveRecord, WorkoutSaveResult, WorkoutSet, WorkoutSnapshot,
    WorkoutStatusArguments, WorkoutStatusResult,
)
from app.domain.session.models import SendCommand, SendRequest, SessionMessageEntry
from app.infrastructure.persistence.sqlite.business_repository import SqliteBusinessRepository
from app.infrastructure.persistence.sqlite.database import (
    Database, SCHEMA_VERSION, _MIGRATIONS, apply_schema, open_database,
)
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from test.check_profile_snapshot_store import FULL_PROFILE, SYSTEM, USAGE, stamp
from app.domain.business.models import ProfileContent
from test.regression_support import temporary_root

ROOT = temporary_root("workout-storage")
CONTENT = {"exercises": [{"exercise_id": None, "name": "俯卧撑", "load_convention": None, "sets": []}], "notes": None}


def identifier() -> str:
    return str(uuid4())


def check_models() -> dict:
    cases = 0
    for field, valid, invalid in (
        ("reps", [None, 1, 20], [0, -1, True, False, "1", 1.0, float("nan"), float("inf")]),
        ("weight_kg", [None, 0, 0.0, 2, 2.5], [-1, True, False, "1", float("nan"), float("inf"), -float("inf")]),
        ("duration_seconds", [None, 1, 1.5], [0, -1, True, False, "1", float("nan"), float("inf")]),
    ):
        for value in valid:
            WorkoutSet.model_validate({"reps": None, "weight_kg": None, "duration_seconds": None, field: value})
            cases += 1
        for value in invalid:
            with pytest.raises(ValidationError):
                WorkoutSet.model_validate({"reps": None, "weight_kg": None, "duration_seconds": None, field: value})
            cases += 1
    WorkoutContent.model_validate(CONTENT)
    all_null = {"reps": None, "weight_kg": None, "duration_seconds": None}
    exercise = CONTENT["exercises"][0]
    WorkoutExercise.model_validate({**exercise, "sets": [all_null, all_null]})
    for convention in ("per_implement", "barbell_total", "machine_display", "plates_total", "per_side", "added_weight", "assistance_weight"):
        WorkoutExercise.model_validate({**exercise, "load_convention": convention, "sets": [{**all_null, "weight_kg": 0}]})
        cases += 1
    with pytest.raises(ValidationError):
        WorkoutExercise.model_validate({**exercise, "sets": [{**all_null, "weight_kg": 0}]})
    for payload in ({**CONTENT, "exercises": []}, {"exercises": [exercise]}, {**CONTENT, "extra": 1}):
        with pytest.raises(ValidationError):
            WorkoutContent.model_validate(payload)
        cases += 1
    proposal = {"performed_on": "2026-06-01", "base_workout_id": None, "base_workout_version": None, "payload": CONTENT}
    WorkoutProposalArguments.model_validate(proposal)
    WorkoutProposalArguments.model_validate({**proposal, "base_workout_id": identifier(), "base_workout_version": 1})
    for payload in (
        {**proposal, "performed_on": "2026-02-29"}, {**proposal, "performed_on": "2026-6-01"},
        {**proposal, "performed_on": "20260601"}, {**proposal, "performed_on": "0000-01-01"},
        {**proposal, "base_workout_version": 1}, {**proposal, "base_workout_id": identifier()},
        {**proposal, "base_workout_id": identifier(), "base_workout_version": True},
        {**proposal, "session_id": identifier()},
        {key: value for key, value in proposal.items() if key != "base_workout_id"},
    ):
        with pytest.raises(ValidationError):
            WorkoutProposalArguments.model_validate(payload)
        cases += 1
    assert WorkoutListArguments().model_dump() == {"date_from": None, "date_to": None, "page": 1, "page_size": 10}
    for arguments in (
        {"date_from": "2026-06-02", "date_to": "2026-06-01"}, {"date_to": "2026-02-30"},
        {"page": True}, {"page": "1"}, {"page": 1.0}, {"page": 0},
        {"page_size": 101}, {"page_size": 0}, {"unknown": None},
    ):
        with pytest.raises(ValidationError):
            WorkoutListArguments.model_validate(arguments)
        cases += 1
    pid = identifier()
    prepared = WorkoutProposal(proposal_id=pid, **proposal)
    assert set(prepared.model_dump()) == {"proposal_id", "performed_on", "base_workout_id", "base_workout_version", "payload"}
    record = make_record("2026-06-01")
    saved = WorkoutSaveResult(**record.model_dump(), proposal_id=pid, saved_at=record.updated_at)
    assert set(record.model_dump()) == {"id", "performed_on", "version", "content", "created_at", "updated_at"}
    for state in ("pending", "processing", "invalidated", "conflicted"):
        WorkoutStatusResult(proposal_id=pid, status=state, result=None)
        with pytest.raises(ValidationError):
            WorkoutStatusResult(proposal_id=pid, status=state, result=saved)
        cases += 1
    WorkoutStatusResult(proposal_id=pid, status="saved", result=saved)
    with pytest.raises(ValidationError):
        WorkoutStatusResult(proposal_id=pid, status="saved", result=None)
    with pytest.raises(ValidationError):
        WorkoutStatusResult(proposal_id=identifier(), status="saved", result=saved)
    with pytest.raises(ValidationError):
        WorkoutSaveResult(**record.model_dump(), proposal_id=pid, saved_at=record.updated_at + 1)
    for model, payload in (
        (WorkoutGetArguments, {"workout_id": identifier()}),
        (WorkoutStatusArguments, {"proposal_id": pid}),
        (WorkoutSaveArguments, {"proposal_id": pid, "display_entry_id": identifier(), "confirmation_entry_id": identifier()}),
    ):
        model.model_validate(payload)
        with pytest.raises(ValidationError):
            model.model_validate({**payload, "run_id": identifier()})
        for key in payload:
            with pytest.raises(ValidationError):
                model.model_validate({**payload, key: "invalid-id"})
            cases += 1
    errors = (WorkoutAccessDenied, WorkoutConfirmationInvalid, WorkoutNotFound,
              WorkoutProposalInvalidated, WorkoutProposalNotFound, WorkoutSaveProcessing,
              WorkoutVersionConflict)
    for error in errors:
        assert set(error().detail()) == {"code", "message"}
    assert WorkoutNotFound().detail() == {"code": "workout_not_found", "message": "训练记录不存在。"}
    return {"schema_cases": cases, "empty_sets": True, "all_null_sets": True,
            "zero_weight_requires_convention": True, "error_codes": [error.code for error in errors]}


def make_record(performed_on: str) -> WorkoutRecord:
    now = stamp()
    return WorkoutRecord(id=identifier(), performed_on=performed_on, version=1,
                         content=WorkoutContent.model_validate(CONTENT), created_at=now, updated_at=now)


async def seed(database: Database) -> dict:
    service = SessionService(SqliteSessionRepository(database))
    session_id = identifier()
    await service.create_session_result(session_id, "训练记录存储验证")
    accepted = await service.accept_send(
        SendCommand(operation_id=identifier(), session_id=session_id, request=SendRequest(text="记录六月一日俯卧撑")),
        system_message=SYSTEM,
    )
    assert accepted.run is not None
    run = accepted.run
    source_id, display_id, pid = identifier(), identifier(), identifier()
    proposal = WorkoutProposal(proposal_id=pid, performed_on="2026-06-01", base_workout_id=None,
                               base_workout_version=None, payload=WorkoutContent.model_validate(CONTENT))
    now = stamp()
    await service.append_entry(SessionMessageEntry(
        session_id=session_id, id=source_id, parent_id=run.request_entry_id, run_id=run.id,
        type="message", created_at=now,
        messages=[AssistantMessage(role="assistant", content=[ToolCall(type="toolCall", id="prepare-1", name="prepare_workout",
            arguments={key: value for key, value in proposal.model_dump().items() if key != "proposal_id"})],
            api="openai-completions", provider="example", model="model-1", usage=USAGE, stop_reason="toolUse", timestamp=now)],
    ))
    await service.append_entry(SessionMessageEntry(
        session_id=session_id, id=display_id, parent_id=source_id, run_id=run.id, type="message", created_at=now + 1,
        messages=[ToolResultMessage(role="toolResult", tool_call_id="prepare-1", tool_name="prepare_workout",
            content=[TextContent(type="text", text=proposal.model_dump_json())], is_error=False, timestamp=now + 1)],
    ))
    await service.finish_run(session_id, run.id, "completed")
    confirmed = await service.accept_send(
        SendCommand(operation_id=identifier(), session_id=session_id, request=SendRequest(text="确认保存")), system_message=SYSTEM,
    )
    assert confirmed.run is not None
    await service.finish_run(session_id, confirmed.run.id, "completed")
    return {"session": session_id, "request": run.request_entry_id, "source": source_id,
            "display": display_id, "confirmation": confirmed.run.request_entry_id, "proposal": pid}


def snapshot(ids: dict, **updates) -> WorkoutSnapshot:
    values = dict(proposal_id=ids["proposal"], session_id=ids["session"], request_entry_id=ids["request"],
        source_entry_id=ids["source"], performed_on="2026-06-01", base_workout_id=None,
        base_workout_version=None, payload=WorkoutContent.model_validate(CONTENT), status="pending", created_at=stamp())
    values.update(updates)
    return WorkoutSnapshot(**values)


def save_record(ids: dict, record: WorkoutRecord) -> WorkoutSaveRecord:
    return WorkoutSaveRecord(proposal_id=ids["proposal"], session_id=ids["session"], display_entry_id=ids["display"],
        confirmation_entry_id=ids["confirmation"], saved_at=record.updated_at,
        result=WorkoutSaveResult(**record.model_dump(), proposal_id=ids["proposal"], saved_at=record.updated_at))


async def raw(database: Database, sql: str, args: tuple = ()) -> None:
    async def run(connection):
        cursor = await connection.execute(sql, args)
        await cursor.close()
    await database.read(run)


async def check_migration() -> dict:
    path = ROOT / f"migration-{identifier()}.db"
    connection = await aiosqlite.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    await connection.execute("PRAGMA foreign_keys = ON")
    for version in range(1, 6):
        await apply_schema(connection, _MIGRATIONS[version].read_text(encoding="utf-8"), version)
    database = Database(connection)
    repo = SqliteBusinessRepository(database)
    ids = await seed(database)
    profile = ProfileContent.model_validate(FULL_PROFILE)
    exercises = Catalog.load().all()
    async with repo.transaction():
        await repo.save_profile(profile, 3, stamp())
        await repo.replace_exercises(exercises)
    before = await repo.get_profile()
    before_session = await SqliteSessionRepository(database).get_session(ids["session"])
    before_entries = await SqliteSessionRepository(database).list_entries(ids["session"])
    cursor = await database.connection.execute("SELECT * FROM exercises ORDER BY id")
    before_catalog = [tuple(row) for row in await cursor.fetchall()]
    await cursor.close()
    await database.close()
    database = await open_database(path)
    repo = SqliteBusinessRepository(database)
    assert await repo.get_profile() == before
    assert await repo.count_exercises() == len(exercises)
    assert await SqliteSessionRepository(database).get_session(ids["session"]) == before_session
    assert await SqliteSessionRepository(database).list_entries(ids["session"]) == before_entries
    cursor = await database.connection.execute("SELECT * FROM exercises ORDER BY id")
    assert [tuple(row) for row in await cursor.fetchall()] == before_catalog
    await cursor.close()
    cursor = await database.connection.execute("PRAGMA user_version")
    assert (await cursor.fetchone())[0] == SCHEMA_VERSION == 7
    await cursor.close()
    with pytest.raises(sqlite3.OperationalError):
        await apply_schema(database.connection, "CREATE TABLE migration_guard (id INTEGER); SELECT * FROM absent_table;", 7)
    cursor = await database.connection.execute("SELECT name FROM sqlite_master WHERE name = 'migration_guard'")
    assert await cursor.fetchone() is None
    await cursor.close()
    await database.close()
    reopened = await open_database(path)
    await reopened.close()
    return {"upgraded_from": 5, "schema_version": SCHEMA_VERSION, "catalog_rows_preserved": len(exercises),
            "profile_and_session_preserved": True, "migration_failure_rolled_back": True, "reopen_verified": True}


async def check_store() -> dict:
    path = ROOT / f"store-{identifier()}.db"
    database = await open_database(path)
    repo = SqliteBusinessRepository(database)
    ids = await seed(database)
    other = await seed(database)
    record = make_record("2026-06-01")
    pending = snapshot(ids)
    try:
        assert await repo.get_workout(identifier()) is None
        assert await repo.get_workout_by_date("2026-06-01") is None
        empty = await repo.list_workouts(WorkoutListArguments())
        assert empty.items == [] and empty.total == 0
        async with repo.transaction():
            await repo.insert_workout_snapshot(pending)
        assert await repo.get_workout_snapshot(pending.proposal_id) == pending
        with pytest.raises(sqlite3.IntegrityError):
            await repo.insert_workout_snapshot(pending)
        with pytest.raises(sqlite3.IntegrityError):
            await repo.insert_workout_snapshot(snapshot(ids, proposal_id=identifier(), session_id=other["session"]))
        with pytest.raises(sqlite3.IntegrityError):
            await repo.insert_workout_snapshot(snapshot(ids, proposal_id=identifier(), request_entry_id=ids["source"]))
        with pytest.raises(sqlite3.IntegrityError):
            await repo.insert_workout_snapshot(snapshot(ids, proposal_id=identifier(), source_entry_id=ids["request"]))
        for column, value in (("payload", "{}"), ("performed_on", "2026-06-02"), ("base_workout_version", 2)):
            with pytest.raises(sqlite3.IntegrityError):
                await raw(database, f"UPDATE workout_snapshots SET {column} = ? WHERE proposal_id = ?", (value, pending.proposal_id))
        with pytest.raises(sqlite3.IntegrityError):
            await repo.bind_workout_display_entry(pending.proposal_id, ids["source"])
        await repo.bind_workout_display_entry(pending.proposal_id, ids["display"])
        with pytest.raises(sqlite3.IntegrityError):
            await repo.begin_workout_save(pending.proposal_id, ids["display"])
        await repo.begin_workout_save(pending.proposal_id, ids["confirmation"])
        await repo.begin_workout_save(pending.proposal_id, ids["confirmation"])
        located = await repo.find_workout_snapshot_by_confirmation(ids["session"], ids["confirmation"])
        assert located.proposal_id == pending.proposal_id
        second = snapshot(ids, proposal_id=identifier())
        later = snapshot(ids, proposal_id=identifier(), performed_on="2026-06-02")
        async with repo.transaction():
            await repo.insert_workout_snapshot(second)
            await repo.insert_workout_snapshot(later)
            await repo.bind_workout_display_entry(second.proposal_id, ids["display"])
        with pytest.raises(sqlite3.IntegrityError):
            await repo.begin_workout_save(second.proposal_id, ids["confirmation"])
        assert (await repo.get_workout_snapshot(second.proposal_id)).confirmation_entry_id is None
        with pytest.raises(RuntimeError, match="事务中断"):
            async with repo.transaction():
                await repo.invalidate_pending_workouts(ids["session"], "2026-06-01", identifier())
                raise RuntimeError("事务中断")
        assert (await repo.get_workout_snapshot(second.proposal_id)).status == "pending"
        await repo.invalidate_pending_workouts(ids["session"], "2026-06-01", pending.proposal_id)
        assert (await repo.get_workout_snapshot(second.proposal_id)).status == "invalidated"
        assert (await repo.get_workout_snapshot(later.proposal_id)).status == "pending"
        assert (await repo.get_workout_snapshot(pending.proposal_id)).status == "processing"
        with pytest.raises(RuntimeError, match="事务中断"):
            async with repo.transaction():
                await repo.insert_workout(record)
                await repo.complete_workout_save(save_record(ids, record))
                raise RuntimeError("事务中断")
        assert await repo.get_workout(record.id) is None
        assert await repo.get_workout_save_record(pending.proposal_id) is None
        assert (await repo.get_workout_snapshot(pending.proposal_id)).status == "processing"
        await database.close()
        database = await open_database(path)
        repo = SqliteBusinessRepository(database)
        assert await repo.recover_interrupted_workout_saves() == 1
        assert await repo.recover_interrupted_workout_saves() == 0
        recovered = await repo.get_workout_snapshot(pending.proposal_id)
        assert recovered.status == "pending" and recovered.confirmation_entry_id == ids["confirmation"]
        async with repo.transaction():
            await repo.begin_workout_save(pending.proposal_id, ids["confirmation"])
            await repo.insert_workout(record)
            await repo.complete_workout_save(save_record(ids, record))
        fixed = await repo.get_workout_save_record(pending.proposal_id)
        assert fixed == save_record(ids, record)
        assert await repo.find_workout_save_record_by_confirmation(ids["session"], ids["confirmation"]) == fixed
        assert (await repo.list_workout_save_records(ids["session"])) == [fixed]
        with pytest.raises(sqlite3.IntegrityError):
            async with repo.transaction():
                await repo.complete_workout_save(save_record(ids, record))
        with pytest.raises(sqlite3.IntegrityError):
            await raw(database, "UPDATE workout_save_records SET result = ? WHERE proposal_id = ?", ("{}", pending.proposal_id))
        with pytest.raises(sqlite3.IntegrityError):
            await repo.insert_workout(make_record(record.performed_on))
        changed = WorkoutContent.model_validate({**CONTENT, "notes": "肩部不适，实际完成"})
        async with repo.transaction():
            assert await repo.update_workout(record.id, 1, changed, record.updated_at + 10)
        latest = await repo.get_workout_by_date(record.performed_on)
        assert latest.id == record.id and latest.version == 2 and latest.created_at == record.created_at
        assert latest.updated_at == record.updated_at + 10 and latest.content == changed
        assert not await repo.update_workout(record.id, 1, record.content, record.updated_at + 20)
        assert await repo.get_workout(record.id) == latest
        assert (await repo.get_workout_save_record(pending.proposal_id)).result.version == 1
        async with repo.transaction():
            for day in range(2, 15):
                await repo.insert_workout(make_record(f"2026-06-{day:02d}"))
        concurrent_record = await repo.get_workout_by_date("2026-06-14")

        async def concurrent_update() -> bool:
            async with repo.transaction():
                return await repo.update_workout(concurrent_record.id, 1, changed, stamp())

        outcomes = await asyncio.gather(concurrent_update(), concurrent_update())
        assert sorted(outcomes) == [False, True]
        assert (await repo.get_workout(concurrent_record.id)).version == 2
        page = await repo.list_workouts(WorkoutListArguments())
        assert page.total == 14 and len(page.items) == 10
        assert [item.performed_on for item in page.items] == [f"2026-06-{day:02d}" for day in range(14, 4, -1)]
        assert len((await repo.list_workouts(WorkoutListArguments(page=2))).items) == 4
        assert (await repo.list_workouts(WorkoutListArguments(page=3))).items == []
        same_day = await repo.list_workouts(WorkoutListArguments(date_from="2026-06-01", date_to="2026-06-01"))
        assert same_day.total == 1 and same_day.items == [latest]
        assert (await repo.list_workouts(WorkoutListArguments(date_from="2026-06-10"))).total == 5
        assert (await repo.list_workouts(WorkoutListArguments(date_to="2026-06-03"))).total == 3
        await repo.delete_workout_snapshots_for_entries(ids["session"], {ids["display"]})
        assert await repo.get_workout_snapshot(pending.proposal_id) is None
        assert await repo.get_workout_save_record(pending.proposal_id) == fixed
        await repo.delete_workout_snapshots_for_entries(ids["session"], set())
        await repo.delete_workout_snapshots_for_session(ids["session"])
        assert await repo.list_workout_snapshots(ids["session"]) == []
        sessions = SqliteSessionRepository(database)
        async with sessions.transaction():
            await sessions.defer_foreign_keys()
            await sessions.delete_session(ids["session"])
        assert await repo.get_workout(record.id) == latest
        assert await repo.get_workout_save_record(pending.proposal_id) == fixed
        await database.close()
        database = await open_database(path)
        repo = SqliteBusinessRepository(database)
        assert await repo.get_workout(record.id) == latest
        assert await repo.get_workout_save_record(pending.proposal_id) == fixed
        return {"same_day_unique": True, "stable_id": record.id, "latest_version": 2,
                "fixed_result_version": 1, "rollback": True, "recovery": [1, 0], "pagination_total": 14,
                "cleanup_retains_record_and_result": True, "restart_retains_saved": True,
                "roles_ownership_immutability_and_confirmation_unique": True,
                "concurrent_cas_single_winner": outcomes}
    finally:
        await database.close()


def check_pagination() -> dict:
    from app.infrastructure.persistence.sqlite import database as database_module
    from app.interfaces.http import app
    from test.check_profile_confirmation import Fixture
    from test.check_workout_tools import batch, body, call
    from test.regression_support import Server, client

    large_page = 9223372036854775807
    empty_path = ROOT / f"pagination-empty-{identifier()}.db"
    path = ROOT / f"pagination-{identifier()}.db"
    cases = [
        ({}, 14, [f"2026-06-{day:02d}" for day in range(14, 4, -1)]),
        ({"page": 2}, 14, [f"2026-06-{day:02d}" for day in range(4, 0, -1)]),
        ({"page": 3}, 14, []),
        ({"page": large_page, "page_size": 10}, 14, []),
        ({"page": 10 ** 100}, 14, []),
        ({"date_from": "2026-06-03", "date_to": "2026-06-05"}, 3,
         ["2026-06-05", "2026-06-04", "2026-06-03"]),
        ({"date_from": "2026-06-03", "date_to": "2026-06-05", "page": 2}, 3, []),
        ({"date_from": "2026-06-03", "date_to": "2026-06-05", "page": large_page}, 3, []),
    ]

    async def verify_repository_and_tools() -> list[dict]:
        empty_database = await open_database(empty_path)
        await empty_database.close()
        fixture = await Fixture(path).seeded()
        try:
            session = await fixture.session("训练分页回归")
            arguments = {"page": large_page, "page_size": 10}
            empty = await fixture.repository.list_workouts(WorkoutListArguments(**arguments))
            assert empty.model_dump() == {"items": [], **arguments, "total": 0}
            _, messages, _ = await batch(fixture, session, [call("list_workouts", arguments)])
            assert not messages[0].is_error and body(messages[0]) == empty.model_dump()
            async with fixture.repository.transaction():
                for day in range(1, 15):
                    await fixture.repository.insert_workout(make_record(f"2026-06-{day:02d}"))
            expected = []
            for arguments, total, dates in cases:
                result = await fixture.repository.list_workouts(WorkoutListArguments(**arguments))
                assert result.total == total
                assert [item.performed_on for item in result.items] == dates
                assert result.page == arguments.get("page", 1) and result.page_size == arguments.get("page_size", 10)
                expected.append(result.model_dump())
            _, messages, _ = await batch(fixture, session,
                [call("list_workouts", arguments) for arguments, _, _ in cases])
            assert all(not message.is_error for message in messages)
            assert [body(message) for message in messages] == expected
            return expected
        finally:
            await fixture.close()

    expected = asyncio.run(verify_repository_and_tools())
    original_path = database_module.default_database_path
    try:
        for database_path, queries in (
            (empty_path, [({"page": large_page, "page_size": 10},
                           {"items": [], "page": large_page, "page_size": 10, "total": 0})]),
            (path, [(case[0], result) for case, result in zip(cases, expected)]),
        ):
            database_module.default_database_path = lambda: database_path
            with Server(app) as server, client(server.base_url) as http:
                for arguments, result in queries:
                    response = http.get("/api/workouts", params=arguments)
                    assert response.status_code == 200, response.text
                    assert response.headers["cache-control"] == "no-store"
                    assert response.json() == result
    finally:
        database_module.default_database_path = original_path
    return {"large_page": large_page, "unbounded_page": str(10 ** 100),
            "empty_total": 0, "populated_total": 14, "cases": len(cases),
            "repository_tool_http_identical": True}


def check() -> None:
    evidence = {"models": check_models(), "migration": asyncio.run(check_migration()),
                "store": asyncio.run(check_store()), "pagination": check_pagination()}
    (ROOT / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS: workout strict schema; SQLite migration 5→7 and rollback; profile/session/catalog preserved; unique date; stable ID/version CAS; snapshots/bindings; atomic rollback/recovery; pagination; fixed results; cleanup/restart")


if __name__ == "__main__":
    check()
