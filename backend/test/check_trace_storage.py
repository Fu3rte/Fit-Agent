import asyncio
import json
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import time_ns

import aiosqlite
import pytest
from pydantic import ValidationError

from app.agent.telemetry import (
    RETENTION_DAYS,
    SCHEMA_VERSION,
    RunTrace,
    SpanName,
    SpanRecord,
    TraceStore,
    connect_trace_store,
    open_trace_store,
    trace_database_path,
    uuid4_canonical,
    uuid4_hex,
)

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND / "temp" / "trace" / "storage"
DBS = ROOT / "dbs"
EVIDENCE = ROOT / "evidence.json"
DAY_MS = 86_400_000

RESULTS: dict[str, object] = {}


def agent_trace(**overrides) -> RunTrace:
    values = {
        "session_id": uuid4_canonical(),
        "operation_id": uuid4_canonical(),
        "run_id": uuid4_canonical(),
    }
    values.update(overrides)
    return RunTrace(**values)


def identity(**overrides) -> dict:
    values = {
        "trace_id": uuid4_hex(),
        "session_id": uuid4_canonical(),
        "run_id": uuid4_canonical(),
        "operation_id": uuid4_canonical(),
    }
    values.update(overrides)
    return values


def record(
    span_id: int,
    parent: int | None,
    name: str,
    *,
    started_at: int,
    attributes: dict | None = None,
    **fields,
) -> SpanRecord:
    payload = {
        **identity(),
        "span_id": span_id,
        "parent_span_id": parent,
        "name": name,
        "started_at": started_at,
        "finished_at": started_at + 5,
        "duration_ms": 5.0,
        "status": "completed",
        "error_code": None,
        "attributes": attributes or {},
    }
    payload.update(fields)
    return SpanRecord.model_validate(payload)


def agent_records(now_ms: int, **ident) -> list[SpanRecord]:
    shared = identity(**ident)
    return [
        record(1, None, "agent_run", started_at=now_ms, attributes={"first_text_ms": 120.5}, **shared),
        record(2, 1, "turn", started_at=now_ms + 1, **shared),
        record(
            3,
            2,
            "model",
            started_at=now_ms + 2,
            attributes={"provider": "p", "model": "m", "call_kind": "normal", "input_tokens": 0},
            status="failed",
            error_code="model_rate_limited",
            **shared,
        ),
    ]


def confirmation_records(now_ms: int, **ident) -> list[SpanRecord]:
    shared = identity(run_id=None, **ident)
    return [record(1, None, "confirmation", started_at=now_ms, attributes={"business_kind": "plan"}, **shared)]


def run(coroutine) -> object:
    return asyncio.run(coroutine)


def database(name: str) -> Path:
    DBS.mkdir(parents=True, exist_ok=True)
    path = DBS / f"{name}.db"
    path.unlink(missing_ok=True)
    return path


async def opened(name: str) -> TraceStore:
    return await open_trace_store(database(name))


def current_data_snapshot() -> tuple:
    entries = []
    for path in sorted((BACKEND / "data").rglob("*")):
        if "__pycache__" in path.parts:
            continue
        stat = path.stat()
        entries.append((str(path.relative_to(BACKEND)), stat.st_size, stat.st_mtime_ns))
    return tuple(entries)


async def closing(store: TraceStore, body) -> object:
    # 场景中途断言失败也必须关闭连接：aiosqlite 的工作线程非 daemon，未关闭会拖住解释器退出。
    try:
        return await body(store)
    finally:
        await store.close()


def test_schema_baseline_and_version() -> None:
    async def scenario() -> None:
        async def body(store: TraceStore) -> dict:
            connection = store._connection
            cursor = await connection.execute("PRAGMA user_version")
            version = (await cursor.fetchone())[0]
            await cursor.close()
            assert version == SCHEMA_VERSION
            cursor = await connection.execute("PRAGMA foreign_keys")
            enabled = (await cursor.fetchone())[0]
            await cursor.close()
            assert enabled == 1
            cursor = await connection.execute("SELECT strict FROM pragma_table_list WHERE name = 'spans'")
            strict = (await cursor.fetchone())[0]
            await cursor.close()
            assert strict == 1
            cursor = await connection.execute(
                "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
            objects = [tuple(row) for row in await cursor.fetchall()]
            await cursor.close()
            assert objects == [
                ("index", "spans_agent_root"),
                ("index", "spans_confirmation_root"),
                ("table", "spans"),
            ]
            cursor = await connection.execute("PRAGMA foreign_key_list(spans)")
            keys = [tuple(row) for row in await cursor.fetchall()]
            await cursor.close()
            assert {row[2] for row in keys} == {"spans"}
            assert sorted((row[3], row[4]) for row in keys) == [
                ("parent_span_id", "span_id"),
                ("trace_id", "trace_id"),
            ]
            assert await store.trace_count() == 0
            return {"user_version": version, "foreign_keys": enabled, "strict": strict, "objects": objects}

        return await closing(await opened("schema"), body)

    RESULTS["schema"] = run(scenario())


def test_check_constraints_reject_out_of_contract_rows() -> None:
    async def scenario() -> None:
        async def body(store: TraceStore) -> list[str]:
            now_ms = time_ns() // 1_000_000
            good = agent_records(now_ms)
            await store.save(good)
            assert await store.trace_count() == 3
            connection = store._connection
            cases = {
                "未知 name": (
                    "INSERT INTO spans VALUES (?,1,NULL,?,?,?,'ghost',1,1,1.0,'completed',NULL,'{}')",
                    (uuid4_hex(), good[0].session_id, good[0].run_id, good[0].operation_id),
                ),
                "未知 status": (
                    "INSERT INTO spans VALUES (?,1,NULL,?,?,?,'turn',1,1,1.0,'unknown',NULL,'{}')",
                    (uuid4_hex(), good[0].session_id, good[0].run_id, good[0].operation_id),
                ),
                "负 duration_ms": (
                    "INSERT INTO spans VALUES (?,1,NULL,?,?,?,'turn',1,1,-1.0,'completed',NULL,'{}')",
                    (uuid4_hex(), good[0].session_id, good[0].run_id, good[0].operation_id),
                ),
                "非 JSON attributes": (
                    "INSERT INTO spans VALUES (?,1,NULL,?,?,?,'turn',1,1,1.0,'completed',NULL,'not json')",
                    (uuid4_hex(), good[0].session_id, good[0].run_id, good[0].operation_id),
                ),
                "非正 span_id": (
                    "INSERT INTO spans VALUES (?,0,NULL,?,?,?,'turn',1,1,1.0,'completed',NULL,'{}')",
                    (uuid4_hex(), good[0].session_id, good[0].run_id, good[0].operation_id),
                ),
                "STRICT 列类型": (
                    "INSERT INTO spans VALUES (?,1,NULL,?,?,?,'turn','x',1,1.0,'completed',NULL,'{}')",
                    (uuid4_hex(), good[0].session_id, good[0].run_id, good[0].operation_id),
                ),
            }
            rejected = []
            for label, (statement, parameters) in cases.items():
                with pytest.raises(sqlite3.IntegrityError):
                    await connection.execute(statement, parameters)
                rejected.append(label)
            assert await store.trace_count() == 3
            return rejected

        return await closing(await opened("checks"), body)

    RESULTS["checks"] = run(scenario())


def test_whole_trace_batch_and_round_trip() -> None:
    trace = agent_trace()

    def tool_body(span) -> str:
        span.set_attributes({"tool_name": "echo", "execution_ms": 3.5, "postprocess_ms": 0.5})
        return "tool-value"

    def summary_body(span) -> str:
        span.set_attributes({"stop_reason": "stop", "cache_read_tokens": 7})
        return "summary-value"

    def compaction_body(span) -> str:
        return span.start_span(
            "model", {"provider": "step", "model": "m1", "call_kind": "history_summary"}, summary_body
        )

    def turn_body(span) -> str:
        def model_body(model) -> str:
            model.set_attributes({"stop_reason": "length", "input_tokens": 10, "output_tokens": 20})
            return "model-value"

        span.start_span("model", {"provider": "step", "model": "m1", "call_kind": "normal"}, model_body)
        with ThreadPoolExecutor(max_workers=3) as pool:
            return ",".join(
                pool.map(lambda index: span.start_span("tool", {"tool_call_id": f"c-{index}"}, tool_body), range(3))
            )

    def root_body(root) -> str:
        root.set_attributes({"first_text_ms": 42.5})
        root.start_span("compaction", {"reason": "threshold"}, compaction_body)
        root.start_span("turn", {}, turn_body)
        return "done"

    assert trace.start_span("agent_run", {}, root_body) == "done"
    expected = trace.records()
    assert [item.name.value for item in expected] == [
        "agent_run",
        "compaction",
        "model",
        "turn",
        "model",
        "tool",
        "tool",
        "tool",
    ]
    assert [item.parent_span_id for item in expected] == [None, 1, 2, 1, 4, 4, 4, 4]

    async def scenario() -> dict:
        async def body(store: TraceStore) -> dict:
            await trace.finish(store)
            assert trace.released is True
            stored = await store.read_trace(trace.trace_id)
            assert [item.model_dump() for item in stored] == [item.model_dump() for item in expected]
            return stored

        stored = await closing(await opened("round_trip"), body)

        async def reopen_check(store: TraceStore) -> int:
            assert [item.model_dump() for item in await store.read_trace(trace.trace_id)] == [
                item.model_dump() for item in expected
            ]
            return await store.trace_count()

        rows_after_reopen = await closing(await open_trace_store(DBS / "round_trip.db"), reopen_check)
        models = [item for item in stored if item.name is SpanName.model]
        tools = [item for item in stored if item.name is SpanName.tool]
        assert sorted(item.attributes["call_kind"] for item in models) == ["history_summary", "normal"]
        assert {item.attributes["tool_name"] for item in tools} == {"echo"}
        assert len(tools) == 3
        return {
            "span_ids": [item.span_id for item in stored],
            "names": [item.name.value for item in stored],
            "parents": [item.parent_span_id for item in stored],
            "statuses": [item.status.value for item in stored],
            "durations_ms": [round(item.duration_ms, 3) for item in stored],
            "model_attributes": [item.attributes for item in models],
            "tool_attributes": tools[0].attributes,
            "rows_after_reopen": rows_after_reopen,
        }

    RESULTS["round_trip"] = run(scenario())


def test_transaction_atomicity_and_usability_after_failure() -> None:
    async def scenario() -> dict:
        async def body(store: TraceStore) -> dict:
            now_ms = time_ns() // 1_000_000
            first = agent_records(now_ms)
            duplicate = [first[0], first[0].model_copy(update={"run_id": uuid4_canonical()})]
            with pytest.raises(sqlite3.IntegrityError):
                await store.save(duplicate)
            after_failure = await store.trace_count()
            in_transaction = store._connection.in_transaction
            await store.save(first)
            return {
                "rows_after_failed_batch": after_failure,
                "in_transaction_after_failed_batch": in_transaction,
                "rows_after_retry": await store.trace_count(),
            }

        return await closing(await opened("atomic"), body)

    RESULTS["atomicity"] = run(scenario())


def test_commit_lock_conflict_and_connection_recovery() -> None:
    async def scenario() -> dict:
        path = database("commit_lock")
        store = await open_trace_store(path)
        connection = store._connection
        steps: dict[str, object] = {}
        reader: sqlite3.Connection | None = None
        try:
            cursor = await connection.execute("PRAGMA journal_mode")
            journal = (await cursor.fetchone())[0]
            await cursor.close()
            assert journal == "delete"
            await connection.execute("PRAGMA busy_timeout=0")
            cursor = await connection.execute("PRAGMA busy_timeout")
            timeout = (await cursor.fetchone())[0]
            await cursor.close()
            assert timeout == 0
            steps["journal_mode"] = journal
            steps["busy_timeout"] = timeout

            now_ms = time_ns() // 1_000_000
            committed = agent_records(now_ms)
            await store.save(committed)

            blocked = agent_records(now_ms + 1)
            reader = sqlite3.connect(path, isolation_level=None)
            reader.execute("PRAGMA busy_timeout=0")
            reader.execute("BEGIN")
            reader.execute("SELECT * FROM spans").fetchall()
            with pytest.raises(sqlite3.OperationalError, match="database is locked") as failure:
                await store.save(blocked)
            steps["commit_exception"] = f"{type(failure.value).__name__}: {failure.value}"

            # 提交失败后事务必须已收尾：连接不留活动事务，锁不阻塞同库其他读取。
            steps["in_transaction_after_commit_failure"] = connection.in_transaction
            assert connection.in_transaction is False
            assert await store.trace_count() == 3
            assert await store.read_trace(blocked[0].trace_id) == []
            steps["rows_after_failed_batch"] = await store.trace_count()
            steps["reader_snapshot_rows"] = reader.execute("SELECT count(*) FROM spans").fetchone()[0]
            assert steps["reader_snapshot_rows"] == 3

            reader.execute("ROLLBACK")
            recovered = agent_records(now_ms + 2)
            await store.save(recovered)
            steps["in_transaction_after_recovery"] = connection.in_transaction
            steps["rows_after_recovery"] = await store.trace_count()
            assert steps["rows_after_recovery"] == 6
            steps["recovered_trace_rows"] = len(await store.read_trace(recovered[0].trace_id))
            assert steps["recovered_trace_rows"] == 3
            steps["committed_trace_rows"] = len(await store.read_trace(committed[0].trace_id))
            assert steps["committed_trace_rows"] == 3
        finally:
            if reader is not None:
                reader.close()
            await store.close()
        return steps

    RESULTS["commit_lock"] = run(scenario())


def test_root_identity_unique_constraints() -> None:
    async def scenario() -> dict:
        async def body(store: TraceStore) -> dict:
            now_ms = time_ns() // 1_000_000
            first = agent_records(now_ms)
            await store.save(first)

            replay = agent_records(
                now_ms + 10,
                run_id=first[0].run_id,
                session_id=first[0].session_id,
                operation_id=first[0].operation_id,
            )
            assert replay[0].trace_id != first[0].trace_id
            with pytest.raises(sqlite3.IntegrityError):
                await store.save(replay)

            retry = agent_trace(
                run_id=first[0].run_id, session_id=first[0].session_id, operation_id=first[0].operation_id
            )
            assert retry.start_span("agent_run", {}, lambda root: root.start_span("turn", {}, lambda s: "x")) == "x"
            await retry.finish(store)
            assert retry.released is True
            counts = {
                "after_direct_replay": await store.trace_count(),
                "retry_trace_rows": len(await store.read_trace(retry.trace_id)),
            }

            confirmation = confirmation_records(now_ms)
            await store.save(confirmation)
            conflict = confirmation_records(now_ms + 1, operation_id=confirmation[0].operation_id)
            with pytest.raises(sqlite3.IntegrityError):
                await store.save(conflict)
            await store.save(confirmation_records(now_ms + 2))
            counts["final_rows"] = await store.trace_count()
            return counts

        return await closing(await opened("unique"), body)

    RESULTS["unique_root"] = run(scenario())


def test_parent_relationship_boundaries() -> None:
    async def scenario() -> dict:
        async def body(store: TraceStore) -> dict:
            now_ms = time_ns() // 1_000_000
            first = agent_records(now_ms)
            await store.save(first)
            foreign = agent_records(now_ms + 5)
            assert foreign[0].trace_id != first[0].trace_id
            with pytest.raises(sqlite3.IntegrityError):
                await store.save([foreign[1]])
            with pytest.raises(sqlite3.IntegrityError):
                await store.save([foreign[1].model_copy(update={"parent_span_id": 99})])
            return {"rows_after_fk_failures": await store.trace_count()}

        return await closing(await opened("parent"), body)

    now_ms = time_ns() // 1_000_000
    with pytest.raises(ValidationError):
        record(1, 1, "turn", started_at=now_ms)
    with pytest.raises(ValidationError):
        record(2, None, "model", started_at=now_ms)
    with pytest.raises(ValidationError):
        record(2, 1, "agent_run", started_at=now_ms)
    RESULTS["parent_scope"] = run(scenario())


def test_retention_thirty_days_whole_trace() -> None:
    async def scenario() -> dict:
        path = database("retention")
        steps: dict[str, object] = {}
        now_ms = time_ns() // 1_000_000
        expired = agent_records(now_ms - (RETENTION_DAYS + 1) * DAY_MS)
        boundary = agent_records(now_ms - RETENTION_DAYS * DAY_MS)
        fresh = agent_records(now_ms - (RETENTION_DAYS - 1) * DAY_MS)
        old_confirmation = confirmation_records(now_ms - (RETENTION_DAYS + 2) * DAY_MS)
        second_expired = agent_records(now_ms - (RETENTION_DAYS + 4) * DAY_MS)
        stale = agent_records(now_ms - (RETENTION_DAYS + 6) * DAY_MS)

        async def write_phase(store: TraceStore) -> None:
            await store.save(expired, now_ms=now_ms)
            steps["rows_after_expired_write"] = await store.trace_count()
            for batch in (boundary, fresh, old_confirmation):
                await store.save(batch, now_ms=now_ms)
            steps["rows_after_four_writes"] = await store.trace_count()
            # 写入事务内清理先于插入：过期链路在下一次写入时已被裁掉，本次新插入的过期链路仍保留。
            assert await store.read_trace(expired[0].trace_id) == []
            assert len(await store.read_trace(boundary[0].trace_id)) == 3
            assert len(await store.read_trace(fresh[0].trace_id)) == 3
            assert len(await store.read_trace(old_confirmation[0].trace_id)) == 1

        async def purge_phase(store: TraceStore) -> None:
            steps["purged_roots"] = await store.purge_expired(now_ms=now_ms)
            assert steps["purged_roots"] == 1
            assert await store.trace_count() == 6
            assert await store.read_trace(old_confirmation[0].trace_id) == []
            await store.save(second_expired, now_ms=now_ms)
            assert await store.trace_count() == 9
            await store.save(agent_records(now_ms + 5), now_ms=now_ms)
            assert await store.read_trace(second_expired[0].trace_id) == []
            assert await store.trace_count() == 9
            await store.save(stale, now_ms=now_ms)
            assert len(await store.read_trace(stale[0].trace_id)) == 3
            steps["rows_before_reopen"] = await store.trace_count()

        async def both_phases(store: TraceStore) -> None:
            await write_phase(store)
            await purge_phase(store)

        # 写入与清理阶段共用同一连接，避免打开时的真实时钟清理介入本阶段断言。
        await closing(await open_trace_store(path), both_phases)

        async def init_phase(store: TraceStore) -> None:
            # 初始化打开时同样按根 started_at 裁剪整条过期链路，此刻使用真实当前时间。
            assert await store.read_trace(stale[0].trace_id) == []
            assert await store.read_trace(boundary[0].trace_id) == []
            assert len(await store.read_trace(fresh[0].trace_id)) == 3
            steps["rows_after_init_purge"] = await store.trace_count()
            assert steps["rows_after_init_purge"] == 6

        await closing(await open_trace_store(path), init_phase)
        return steps

    RESULTS["retention"] = run(scenario())


def test_open_store_faults_isolated() -> None:
    async def scenario() -> dict:
        blocked = DBS / "blocked"
        blocked.mkdir(parents=True, exist_ok=True)
        (blocked / "occupies.db").write_text("占用", encoding="utf-8")
        target = blocked / "occupies.db" / "trace.db"
        faults = {}
        with pytest.raises(OSError):
            await open_trace_store(target)
        faults["path_blocked"] = "raises + None"
        assert await connect_trace_store(target) is None

        wrong_version = database("wrong_version")
        connection = await aiosqlite.connect(wrong_version)
        try:
            await connection.execute("CREATE TABLE spans (trace_id TEXT)")
            await connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
            await connection.commit()
        finally:
            await connection.close()
        with pytest.raises(RuntimeError):
            await open_trace_store(wrong_version)
        assert await connect_trace_store(wrong_version) is None
        faults["unsupported_version"] = "raises + None"

        foreign_objects = database("foreign")
        connection = await aiosqlite.connect(foreign_objects)
        try:
            await connection.execute("CREATE TABLE unrelated (id INTEGER)")
            await connection.commit()
        finally:
            await connection.close()
        with pytest.raises(RuntimeError):
            await open_trace_store(foreign_objects)
        faults["foreign_objects"] = "raises"

        broken_shape = database("broken_shape")
        connection = await aiosqlite.connect(broken_shape)
        try:
            await connection.execute("CREATE TABLE spans (trace_id TEXT, span_id INTEGER)")
            await connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            await connection.commit()
        finally:
            await connection.close()
        with pytest.raises(RuntimeError):
            await open_trace_store(broken_shape)
        faults["broken_shape"] = "raises"

        faults["reopen_verified"] = await closing(await open_trace_store(DBS / "schema.db"), lambda store: store.trace_count())
        return faults

    RESULTS["open_faults"] = run(scenario())


def store_connection_from_traceback(error: BaseException) -> aiosqlite.Connection:
    # 初始化期间的连接只在 open_trace_store 栈帧的局部变量里可见：据此核实异常路径已关闭它。
    frame = error.__traceback__
    while frame is not None:
        if frame.tb_frame.f_code.co_name == "open_trace_store":
            return frame.tb_frame.f_locals["connection"]
        frame = frame.tb_next
    raise AssertionError("初始化错误缺少 open_trace_store 栈帧")


def test_init_purge_failure_closes_connection() -> None:
    async def scenario() -> dict:
        path = database("init_purge")
        now_ms = time_ns() // 1_000_000
        expired = agent_records(now_ms - (RETENTION_DAYS + 5) * DAY_MS)
        store = await open_trace_store(path)
        try:
            # 先清理后插入：本次写入的过期链路此刻仍在库内，留给初始化清理。
            await store.save(expired, now_ms=now_ms)
            rows_written = await store.trace_count()
        finally:
            await store.close()
        assert rows_written == 3

        steps: dict[str, object] = {"rows_written": rows_written}
        setup = sqlite3.connect(path, isolation_level=None)
        try:
            setup.execute(
                "CREATE TRIGGER reject_expired_delete BEFORE DELETE ON spans "
                "BEGIN SELECT RAISE(ABORT, 'test purge blocked'); END"
            )
            try:
                with pytest.raises(sqlite3.IntegrityError, match="test purge blocked") as failure:
                    await open_trace_store(path)
                steps["init_exception"] = f"{type(failure.value).__name__}: {failure.value}"
                connection = store_connection_from_traceback(failure.value)
                with pytest.raises(ValueError, match="no active connection") as closed_execute:
                    await connection.execute("SELECT count(*) FROM spans")
                with pytest.raises(ValueError, match="no active connection") as closed_state:
                    connection.in_transaction
                steps["closed_execute_error"] = f"{type(closed_execute.value).__name__}: {closed_execute.value}"
                steps["closed_state_error"] = f"{type(closed_state.value).__name__}: {closed_state.value}"
                assert await connect_trace_store(path) is None
            finally:
                setup.execute("DROP TRIGGER reject_expired_delete")
        finally:
            setup.close()

        reopened = await open_trace_store(path)
        try:
            # 故障开库未留下锁与残留事务：同一库可再次打开，过期链路按根时间裁掉。
            steps["rows_after_clean_reopen"] = await reopened.trace_count()
            assert steps["rows_after_clean_reopen"] == 0
            assert await reopened.read_trace(expired[0].trace_id) == []
        finally:
            await reopened.close()
        return steps

    RESULTS["init_purge_fault"] = run(scenario())


def test_write_fault_isolated_and_released() -> None:
    async def scenario() -> dict:
        path = database("readonly")
        writer = await open_trace_store(path)
        await writer.close()
        connection = await aiosqlite.connect(
            f"file:///{path.as_posix().lstrip('/')}?mode=ro", uri=True, isolation_level=None
        )
        connection.row_factory = aiosqlite.Row
        store = TraceStore(connection)
        try:
            trace = agent_trace()
            assert trace.start_span("agent_run", {}, lambda root: root.start_span("turn", {}, lambda s: "value")) == "value"
            with pytest.raises(sqlite3.OperationalError):
                await store.save(trace.records())
            raw_failure = {"in_transaction": bool(connection.in_transaction), "memory_records": len(trace.records())}
            assert not connection.in_transaction

            failing = agent_trace()
            failing.start_span("agent_run", {}, lambda root: root.start_span("turn", {}, lambda s: "value"))
            await failing.finish(store)
            isolated = {"released": failing.released, "rows": await store.trace_count()}
        finally:
            await store.close()
        writable = await open_trace_store(path)
        try:
            isolated["rows_after_reopen"] = await writable.trace_count()
        finally:
            await writable.close()
        return {"raw_failure": raw_failure, "isolated": isolated}

    RESULTS["write_fault"] = run(scenario())


def test_finish_without_store_and_unsettled_root() -> None:
    async def scenario() -> dict:
        async def body(store: TraceStore) -> dict:
            trace = agent_trace()
            created: list = []

            async def inner(span):
                await asyncio.sleep(0.001)
                return "value"

            def start(span):
                pending_inner = inner(span)
                created.append(pending_inner)
                return pending_inner

            pending = trace.start_span("agent_run", {}, start)
            assert trace.records() == []
            await trace.finish(store)
            assert trace.released is True
            unsettled = await store.trace_count()
            pending.close()
            created[0].close()

            dropped = agent_trace()
            dropped.start_span("agent_run", {}, lambda root: root.start_span("turn", {}, lambda s: "x"))
            await dropped.finish(None)
            without_store = {"released": dropped.released, "rows": await store.trace_count()}

            kept = agent_trace()
            kept.start_span("agent_run", {}, lambda root: root.start_span("turn", {}, lambda s: "y"))
            await kept.finish(store)
            after_first = await store.trace_count()
            second = agent_trace()
            second.start_span("agent_run", {}, lambda root: "single")
            await second.finish(store)
            return {
                "unsettled_rows": unsettled,
                "without_store": without_store,
                "after_first_write": after_first,
                "after_second_write": await store.trace_count(),
                "recorders_released": [kept.released, second.released],
                "recorder_memory": [len(kept.records()), len(second.records())],
            }

        return await closing(await opened("finish"), body)

    RESULTS["finish"] = run(scenario())


def verify_production_paths_untouched(snapshot: tuple) -> dict:
    # 生产观测库在服务正常运行后可以合法存在：按测试前基线核查整个 data 目录的存在性、大小与修改时间原状。
    path = trace_database_path()
    assert path == BACKEND / "data" / "trace.db"
    existed = path.exists()
    assert current_data_snapshot() == snapshot
    return {"trace_database_existed": existed, "data_snapshot_entries": len(snapshot)}


def check() -> None:
    if ROOT.exists():
        shutil.rmtree(ROOT)
    DBS.mkdir(parents=True, exist_ok=True)
    snapshot = current_data_snapshot()
    for name, function in list(globals().items()):
        if name.startswith("test_") and callable(function):
            function()
    RESULTS["production_baseline"] = verify_production_paths_untouched(snapshot)
    EVIDENCE.write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(
        "Trace 单表存储：schema 基线与 user_version 与外键与 STRICT、CHECK 拒绝越界行、"
        "整批事务与失败回滚、提交锁冲突后事务收尾与同连接恢复、根身份唯一约束、"
        "父子关系限定同一 Trace、30 天整条链路清理与两个清理时机、"
        "初始化清理失败关闭连接、开库与写入故障隔离、记录器与连接分别管理、"
        "生产数据路径按测试前基线零改动通过"
    )


if __name__ == "__main__":
    check()
