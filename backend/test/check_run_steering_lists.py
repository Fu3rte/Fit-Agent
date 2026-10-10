import asyncio
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

from app.ai.messages import AssistantMessage, SystemMessage, TextContent, Usage
from app.application.session.service import CREDENTIAL_SAFE_MESSAGE, SessionService
from app.application.session.steering import SteeringCoordinator
from app.domain.session.errors import RunNotFound
from app.domain.session.models import (
    EditCommand,
    EditRequest,
    RegenerateCommand,
    RegenerateRequest,
    SendCommand,
    SendRequest,
    SessionMessageEntry,
    SteeringCommand,
    SteeringRequest,
)
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from app.interfaces.http import app
from app.model_config import load_model_config
from test.regression_support import (
    Server,
    client,
    install_test_model_config,
    patch_default_database,
    temporary_root,
)

EVIDENCE = temporary_root("run-steering-lists")
RUN_STATUSES = {"running", "completed", "failed", "cancelled", "interrupted"}
RUN_FIELDS = {
    "session_id",
    "run_id",
    "request_entry_id",
    "last_entry_id",
    "status",
    "started_at",
    "finished_at",
    "error_code",
    "error_message",
}
STEERING_FIELDS = {
    "session_id",
    "run_id",
    "steering_id",
    "text",
    "timestamp",
    "status",
    "entry_id",
    "reason",
    "created_at",
    "updated_at",
}
USAGE = Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2)
SYSTEM_MESSAGE = SystemMessage(
    role="system", content="系统提示词，仅后端可见。", tools_added=[], timestamp=100
)
RUNS_SQL = (
    "SELECT session_id, id, request_entry_id, last_entry_id, status, started_at, "
    "finished_at, error_code, error_message FROM session_runs ORDER BY id"
)
STEERING_SQL = (
    "SELECT session_id, id, run_id, message, status, entry_id, reason, created_at, "
    "updated_at FROM steering_inputs ORDER BY id"
)
SESSIONS_SQL = (
    "SELECT id, title, active_leaf_id, created_at, updated_at FROM sessions ORDER BY id"
)


def assistant(value: str, timestamp: int) -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=[TextContent(type="text", text=value)],
        api="openai-completions",
        provider="example",
        model="model-1",
        usage=USAGE,
        stop_reason="stop",
        timestamp=timestamp,
    )


async def open_service(path: Path):
    database = await open_database(path)
    return database, SessionService(SqliteSessionRepository(database))


async def send(service: SessionService, session_id: str, text: str):
    outcome = await service.accept_send(
        SendCommand(
            operation_id=str(uuid4()),
            session_id=session_id,
            request=SendRequest(text=text),
        ),
        system_message=SYSTEM_MESSAGE,
    )
    return outcome.run


async def answer(
    service: SessionService, session_id: str, run, node_id: str, value: str
) -> None:
    outcome = await service.append_entry(
        SessionMessageEntry(
            session_id=session_id,
            id=node_id,
            parent_id=run.last_entry_id or run.request_entry_id,
            run_id=run.id,
            type="message",
            messages=[assistant(value, 300)],
            created_at=310,
        )
    )
    assert outcome.created


async def steer(service: SessionService, session_id: str, run_id: str, text: str):
    outcome = await service.accept_steering(
        SteeringCommand(
            operation_id=str(uuid4()),
            session_id=session_id,
            request=SteeringRequest(target_run_id=run_id, text=text),
        )
    )
    return outcome.steering


def write_raw(path: Path, statement: str, parameters: tuple) -> None:
    raw = sqlite3.connect(path, timeout=30)
    try:
        raw.execute(statement, parameters)
        raw.commit()
    finally:
        raw.close()


def set_started_at(path: Path, run_id: str, started_at: int) -> None:
    write_raw(
        path, "UPDATE session_runs SET started_at = ? WHERE id = ?", (started_at, run_id)
    )


def set_created_at(path: Path, steering_id: str, created_at: int) -> None:
    write_raw(
        path,
        "UPDATE steering_inputs SET created_at = ? WHERE id = ?",
        (created_at, steering_id),
    )


def order(pairs: list[tuple]) -> list[tuple]:
    return sorted(pairs, key=lambda item: (item[0], item[1]))


def table_snapshot(path: Path, statement: str) -> list[tuple]:
    raw = sqlite3.connect(path, timeout=30)
    try:
        return raw.execute(statement).fetchall()
    finally:
        raw.close()


def snapshot_all(path: Path) -> tuple[list[tuple], list[tuple], list[tuple]]:
    return (
        table_snapshot(path, RUNS_SQL),
        table_snapshot(path, STEERING_SQL),
        table_snapshot(path, SESSIONS_SQL),
    )


def is_uuid(value: str) -> bool:
    return str(UUID(value)) == value


def assert_run_fields(run: dict, session_id: str) -> None:
    assert set(run) == RUN_FIELDS
    assert run["session_id"] == session_id and is_uuid(run["session_id"])
    assert is_uuid(run["run_id"]) and is_uuid(run["request_entry_id"])
    assert run["last_entry_id"] is None or is_uuid(run["last_entry_id"])
    assert run["status"] in RUN_STATUSES
    assert isinstance(run["started_at"], int)
    assert run["finished_at"] is None or isinstance(run["finished_at"], int)
    assert run["error_code"] is None or isinstance(run["error_code"], str)
    assert run["error_message"] is None or isinstance(run["error_message"], str)
    if run["status"] in {"running", "interrupted"}:
        assert run["finished_at"] is None
    else:
        assert run["finished_at"] is not None


def assert_steering_fields(item: dict, session_id: str, run_id: str) -> None:
    assert set(item) == STEERING_FIELDS
    assert item["session_id"] == session_id and item["run_id"] == run_id
    assert is_uuid(item["session_id"]) and is_uuid(item["run_id"])
    assert is_uuid(item["steering_id"])
    assert isinstance(item["text"], str) and item["text"]
    for field in ("timestamp", "created_at", "updated_at"):
        assert isinstance(item[field], int)
    assert item["status"] in {"pending", "consumed", "withdrawn", "discarded"}
    if item["status"] == "consumed":
        assert is_uuid(item["entry_id"])
        assert item["reason"] is None
    else:
        assert item["entry_id"] is None
        if item["status"] == "discarded":
            assert item["reason"] in {
                "completed",
                "failed",
                "cancelled",
                "interrupted",
            }
        else:
            assert item["reason"] is None


async def scenario_empty(service: SessionService) -> dict:
    await service.create_session("rl-empty", "空会话")
    assert await service.list_runs("rl-empty") == []
    return {"runs": 0}


async def scenario_run_order(service: SessionService, path: Path) -> dict:
    session_id = "rl-order"
    await service.create_session(session_id, "运行排序")
    first = await send(service, session_id, "问题一")
    await answer(service, session_id, first, "rs-a1", "回答一")
    await service.finish_run(session_id, first.id, "completed")
    failed = await send(service, session_id, "问题二")
    await service.finish_run(
        session_id,
        failed.id,
        "failed",
        error_code="execution_failed",
        error_message="执行失败，请重新发起请求。",
    )
    cancelled = await send(service, session_id, "问题三")
    await answer(service, session_id, cancelled, "rs-a3", "回答三")
    await service.finish_run(session_id, cancelled.id, "cancelled")
    latest = await send(service, session_id, "问题四")
    await service.finish_run(session_id, latest.id, "completed")

    # 打乱插入顺序并制造相同 started_at，验证 (started_at, run_id) 排序键。
    started = {first.id: 40, failed.id: 10, cancelled.id: 30, latest.id: 10}
    for run_id, value in started.items():
        set_started_at(path, run_id, value)

    runs = await service.list_runs(session_id)
    assert [(run.started_at, run.id) for run in runs] == order(
        [(value, run_id) for run_id, value in started.items()]
    )
    by_id = {run.id: run for run in runs}
    # 尚未产生助手消息的失败运行仍保留。
    assert by_id[failed.id].status == "failed"
    assert by_id[failed.id].last_entry_id is None
    assert by_id[failed.id].error_code == "execution_failed"
    assert by_id[failed.id].error_message == "执行失败，请重新发起请求。"
    assert by_id[failed.id].finished_at is not None
    assert by_id[first.id].last_entry_id == "rs-a1"
    assert by_id[cancelled.id].status == "cancelled"
    return {"runs": len(runs), "statuses": sorted(run.status for run in runs)}


async def scenario_steering(service: SessionService, path: Path) -> dict:
    session_id = "rl-steering"
    await service.create_session(session_id, "输入状态")
    target = await send(service, session_id, "运行一")
    consumed = await steer(service, session_id, target.id, "补充一")
    await service.consume_steering(session_id, target.id, consumed.id)
    withdrawn = await steer(service, session_id, target.id, "补充二")
    await service.withdraw_steering(session_id, target.id, withdrawn.id)
    discarded = await steer(service, session_id, target.id, "补充三")
    await service.finish_run(session_id, target.id, "cancelled")

    idle = await send(service, session_id, "运行二")
    await answer(service, session_id, idle, "st-a2", "回答二")
    await service.finish_run(session_id, idle.id, "completed")

    pending_run = await send(service, session_id, "运行三")
    pending = await steer(service, session_id, pending_run.id, "补充四")
    items = await service.list_steering(session_id, pending_run.id)
    assert [item.id for item in items] == [pending.id]
    assert items[0].status == "pending"
    assert items[0].entry_id is None and items[0].reason is None
    await service.consume_steering(session_id, pending_run.id, pending.id)
    await service.finish_run(session_id, pending_run.id, "failed")

    created = {consumed.id: 50, withdrawn.id: 20, discarded.id: 20}
    for steering_id, value in created.items():
        set_created_at(path, steering_id, value)

    items = await service.list_steering(session_id, target.id)
    assert [(item.created_at, item.id) for item in items] == order(
        [(value, steering_id) for steering_id, value in created.items()]
    )
    by_id = {item.id: item for item in items}
    assert by_id[consumed.id].status == "consumed"
    assert by_id[consumed.id].entry_id is not None
    assert by_id[consumed.id].reason is None
    assert by_id[withdrawn.id].status == "withdrawn"
    assert by_id[withdrawn.id].entry_id is None
    assert by_id[withdrawn.id].reason is None
    assert by_id[discarded.id].status == "discarded"
    assert by_id[discarded.id].entry_id is None
    assert by_id[discarded.id].reason == "cancelled"
    # 文本与原始消息时间戳保持接收时原值。
    assert by_id[consumed.id].message.content == "补充一"
    assert by_id[consumed.id].message.timestamp == consumed.message.timestamp
    assert await service.list_steering(session_id, idle.id) == []
    return {"statuses": [item.status for item in items], "idle": 0}


async def scenario_queue(service: SessionService) -> dict:
    # 读取期间运行保持 running、输入保持 pending，内存队列与消费链路不受影响。
    session_id = "rl-queue"
    await service.create_session(session_id, "队列保持原样")
    run = await send(service, session_id, "运行一")
    events: list[dict] = []
    coordinator = SteeringCoordinator(service)
    coordinator.open(session_id, run.id, events.append)
    queued = []
    for text in ("队列一", "队列二"):
        _, accepted = await coordinator.accept(
            run.id,
            SteeringCommand(
                operation_id=str(uuid4()),
                session_id=session_id,
                request=SteeringRequest(target_run_id=run.id, text=text),
            ),
        )
        queued.append(accepted)
    before = [(item.created_at, item.updated_at) for item in queued]

    for _ in range(5):
        items = await service.list_steering(session_id, run.id)
        assert [item.status for item in items] == ["pending", "pending"]
        runs = await service.list_runs(session_id)
        assert [item.status for item in runs if item.id == run.id] == ["running"]

    assert [(item.created_at, item.updated_at) for item in queued] == before
    messages = await coordinator.take(run.id)
    assert [message.content for message in messages] == ["队列一", "队列二"]
    await coordinator.consume(run.id, messages)
    assert [event["status"] for event in events] == ["consumed", "consumed"]
    items = await service.list_steering(session_id, run.id)
    assert [item.status for item in items] == ["consumed", "consumed"]
    assert all(item.entry_id is not None for item in items)
    await service.finish_run(session_id, run.id, "completed")
    return {"queued": len(queued), "consumed_events": len(events)}


async def scenario_deleted(service: SessionService) -> dict:
    session_id = "rl-deleted"
    await service.create_session(session_id, "编辑及重新生成")
    original = await send(service, session_id, "原始问题")
    await answer(service, session_id, original, "dl-a1", "原回答")
    await service.finish_run(session_id, original.id, "completed")
    later = await send(service, session_id, "后续问题")
    removed = await steer(service, session_id, later.id, "后续输入")
    await service.withdraw_steering(session_id, later.id, removed.id)
    await service.finish_run(session_id, later.id, "cancelled")

    edited = await service.accept_edit(
        EditCommand(
            operation_id=str(uuid4()),
            session_id=session_id,
            request=EditRequest(
                target_entry_id=original.request_entry_id, text="修改后问题"
            ),
        )
    )
    assert [run.id for run in await service.list_runs(session_id)] == [edited.run.id]
    for gone in (original.id, later.id):
        try:
            await service.list_steering(session_id, gone)
        except RunNotFound:
            pass
        else:
            raise AssertionError("已删除运行仍可查询")

    await answer(service, session_id, edited.run, "dl-a2", "修改后回答")
    await service.finish_run(session_id, edited.run.id, "completed")
    regenerated = await service.accept_regenerate(
        RegenerateCommand(
            operation_id=str(uuid4()),
            session_id=session_id,
            request=RegenerateRequest(target_entry_id=edited.run.request_entry_id),
        )
    )
    runs = await service.list_runs(session_id)
    assert [run.id for run in runs] == [regenerated.run.id]
    assert regenerated.run.request_entry_id == edited.run.request_entry_id
    assert await service.list_steering(session_id, regenerated.run.id) == []
    await service.finish_run(session_id, regenerated.run.id, "completed")
    return {"retained": regenerated.run.id, "runs": len(runs)}


async def scenario_concurrency(service: SessionService) -> dict:
    session_id = "rl-conc"
    await service.create_session(session_id, "读取并发")

    async def writer() -> None:
        for index in range(3):
            run = await send(service, session_id, f"问题 {index}")
            await answer(service, session_id, run, f"cc-{index}", f"回答 {index}")
            await service.finish_run(session_id, run.id, "completed")

    async def reader() -> int:
        for _ in range(20):
            runs = await service.list_runs(session_id)
            pairs = [(run.started_at, run.id) for run in runs]
            assert pairs == order(pairs)
            assert len({run.id for run in runs}) == len(runs)
            for run in runs:
                assert run.status in RUN_STATUSES
                if run.status not in {"running", "interrupted"}:
                    assert run.finished_at is not None
        return 1

    results = await asyncio.gather(writer(), reader(), reader())
    assert results[1:] == [1, 1]
    assert len(await service.list_runs(session_id)) == 3
    return {"runs": 3}


async def scenario_interrupted(root: Path) -> dict:
    path = root / "interrupted.db"
    database, service = await open_service(path)
    session_id = "rl-interrupted"
    await service.create_session(session_id, "中断恢复")
    run = await send(service, session_id, "未完成")
    pending = await steer(service, session_id, run.id, "遗留输入")
    await database.close()

    reopened, recovered = await open_service(path)
    try:
        await recovered.recover_interrupted()
        runs = await recovered.list_runs(session_id)
        assert [item.status for item in runs] == ["interrupted"]
        assert runs[0].finished_at is None
        items = await recovered.list_steering(session_id, run.id)
        assert [item.id for item in items] == [pending.id]
        assert items[0].status == "discarded"
        assert items[0].reason == "interrupted"
        assert items[0].entry_id is None
        return {"run_status": runs[0].status, "reason": items[0].reason}
    finally:
        await reopened.close()


async def service_checks(root: Path) -> dict:
    path = root / "main.db"
    database, service = await open_service(path)
    try:
        evidence = {
            "empty": await scenario_empty(service),
            "run_order": await scenario_run_order(service, path),
            "steering": await scenario_steering(service, path),
            "queue": await scenario_queue(service),
            "deleted": await scenario_deleted(service),
            "concurrency": await scenario_concurrency(service),
        }
    finally:
        await database.close()
    evidence["interrupted"] = await scenario_interrupted(root)
    return evidence


async def seed_http(path: Path) -> dict:
    database, service = await open_service(path)
    try:
        empty_session = str(uuid4())
        await service.create_session(empty_session, "空会话")

        deleted_session = str(uuid4())
        await service.create_session(deleted_session, "编辑后删除")
        removed_run = await send(service, deleted_session, "原始问题")
        removed_input = await steer(service, deleted_session, removed_run.id, "原始输入")
        await service.withdraw_steering(deleted_session, removed_run.id, removed_input.id)
        await answer(service, deleted_session, removed_run, str(uuid4()), "原回答")
        await service.finish_run(deleted_session, removed_run.id, "completed")
        edited = await service.accept_edit(
            EditCommand(
                operation_id=str(uuid4()),
                session_id=deleted_session,
                request=EditRequest(
                    target_entry_id=removed_run.request_entry_id, text="修改后问题"
                ),
            )
        )
        await service.finish_run(deleted_session, edited.run.id, "completed")

        session_id = str(uuid4())
        await service.create_session(session_id, "运行及输入")
        cancelled = await send(service, session_id, "运行一")
        consumed = await steer(service, session_id, cancelled.id, "补充一")
        await service.consume_steering(session_id, cancelled.id, consumed.id)
        withdrawn = await steer(service, session_id, cancelled.id, "补充二")
        await service.withdraw_steering(session_id, cancelled.id, withdrawn.id)
        discarded = await steer(service, session_id, cancelled.id, "补充三")
        await service.finish_run(session_id, cancelled.id, "cancelled")

        failed = await send(service, session_id, "运行二")
        await service.finish_run(
            session_id,
            failed.id,
            "failed",
            error_code="execution_failed",
            error_message="执行失败，请重新发起请求。",
        )

        completed = await send(service, session_id, "运行三")
        await answer(service, session_id, completed, str(uuid4()), "回答三")
        await service.finish_run(session_id, completed.id, "completed")

        # 运行四保持 running 且输入保持 pending：服务启动恢复改为 interrupted 及 discarded。
        running = await send(service, session_id, "运行四")
        pending = await steer(service, session_id, running.id, "补充四")

        started = {
            cancelled.id: 40,
            failed.id: 10,
            completed.id: 30,
            running.id: 10,
            edited.run.id: 70,
        }
        for run_id, value in started.items():
            set_started_at(path, run_id, value)
        created = {consumed.id: 50, withdrawn.id: 20, discarded.id: 20, pending.id: 60}
        for steering_id, value in created.items():
            set_created_at(path, steering_id, value)
        return {
            "session_id": session_id,
            "empty_session": empty_session,
            "cancelled_run": cancelled.id,
            "failed_run": failed.id,
            "completed_run": completed.id,
            "recovered_run": running.id,
            "recovered_input": pending.id,
            "consumed_input": consumed.id,
            "withdrawn_input": withdrawn.id,
            "discarded_input": discarded.id,
            "deleted_session": deleted_session,
            "deleted_run": removed_run.id,
            "edit_run": edited.run.id,
        }
    finally:
        await database.close()


def insert_leaky_records(path: Path, session_id: str, run_id: str, key: str) -> None:
    # 公开字段命中凭据：运行错误说明与输入正文均不得离开后端。
    raw = sqlite3.connect(path, timeout=30)
    try:
        raw.execute(
            "INSERT INTO sessions (id, title, active_leaf_id, created_at, updated_at) "
            "VALUES (?, ?, NULL, 1, 1)",
            (session_id, "凭据保护"),
        )
        raw.execute(
            "INSERT INTO session_entries "
            "(session_id, id, parent_id, run_id, type, messages, created_at) "
            "VALUES (?, ?, NULL, NULL, 'message', ?, 1)",
            (
                session_id,
                "leak-entry",
                json.dumps(
                    [{"role": "user", "content": "原始问题", "timestamp": 1}],
                    ensure_ascii=False,
                ),
            ),
        )
        raw.execute(
            "INSERT INTO session_runs "
            "(session_id, id, request_entry_id, last_entry_id, status, started_at, "
            "finished_at, error_code, error_message) "
            "VALUES (?, ?, 'leak-entry', NULL, 'failed', 2, 4, 'execution_failed', ?)",
            (session_id, run_id, f"失败说明含 {key}"),
        )
        raw.execute(
            "INSERT INTO steering_inputs "
            "(session_id, id, run_id, message, status, entry_id, reason, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, 'withdrawn', NULL, NULL, 3, 3)",
            (
                session_id,
                "leak-input",
                run_id,
                json.dumps(
                    {"role": "user", "content": f"记住 {key}", "timestamp": 3},
                    ensure_ascii=False,
                ),
            ),
        )
        raw.commit()
    finally:
        raw.close()


def live_read_checks(http, session_id: str) -> dict:
    # 真实执行期间读取：运行状态为 running，查询不占用运行、不改动输入队列。
    with http.stream(
        "POST",
        "/api/agent/run",
        json={
            "session_id": session_id,
            "operation_id": str(uuid4()),
            "request": "只回答两个字：完成",
        },
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        run_id = response.headers["X-Run-ID"]
        assert is_uuid(run_id)

        mid_runs = http.get(f"/api/sessions/{session_id}/runs").json()
        assert [run["run_id"] for run in mid_runs["runs"]] == [run_id]
        running = mid_runs["runs"][0]
        assert running["status"] == "running"
        assert running["finished_at"] is None

        accepted = http.post(
            f"/api/agent/runs/{run_id}/steering",
            json={
                "session_id": session_id,
                "operation_id": str(uuid4()),
                "message": "补充：执行期间受理",
            },
        )
        assert accepted.status_code == 200, accepted.text
        steering_id = accepted.json()["steering_id"]

        mid_steering = http.get(
            f"/api/sessions/{session_id}/runs/{run_id}/steering"
        ).json()
        assert [item["steering_id"] for item in mid_steering["steering"]] == [
            steering_id
        ]
        mid_item = mid_steering["steering"][0]
        assert_steering_fields(mid_item, session_id, run_id)
        assert mid_item["text"] == "补充：执行期间受理"
        assert mid_item["status"] in {"pending", "consumed"}

        lines = list(response.iter_lines())
    assert lines, "执行流未产生事件"

    final_runs = http.get(f"/api/sessions/{session_id}/runs").json()
    final_run = final_runs["runs"][0]
    assert final_run["status"] in {"completed", "failed", "cancelled", "interrupted"}
    assert isinstance(final_run["finished_at"], int)
    final_steering = http.get(
        f"/api/sessions/{session_id}/runs/{run_id}/steering"
    ).json()["steering"]
    assert [item["steering_id"] for item in final_steering] == [steering_id]
    assert final_steering[0]["status"] in {"consumed", "withdrawn", "discarded"}
    return {"mid_status": running["status"], "final_status": final_run["status"]}


def http_checks(evidence: dict) -> None:
    install_test_model_config()
    path = patch_default_database("run-steering-lists-http")
    seed = asyncio.run(seed_http(path))
    runs_url = f"/api/sessions/{seed['session_id']}/runs"
    steering_url = (
        f"/api/sessions/{seed['session_id']}/runs/{seed['cancelled_run']}/steering"
    )

    server = Server(app)
    with server, client(server.base_url) as http:
        # 启动恢复已把运行四改为 interrupted、遗留输入改为 discarded。
        before = snapshot_all(path)

        listed = http.get(runs_url)
        assert listed.status_code == 200, listed.text
        assert listed.headers["cache-control"] == "no-store"
        assert listed.headers["content-type"].startswith("application/json")
        body = listed.json()
        assert set(body) == {"session_id", "runs"}
        assert body["session_id"] == seed["session_id"]
        runs = body["runs"]
        assert [(run["started_at"], run["run_id"]) for run in runs] == order(
            [
                (10, seed["failed_run"]),
                (10, seed["recovered_run"]),
                (30, seed["completed_run"]),
                (40, seed["cancelled_run"]),
            ]
        )
        for run in runs:
            assert_run_fields(run, seed["session_id"])
        by_id = {run["run_id"]: run for run in runs}
        assert by_id[seed["cancelled_run"]]["last_entry_id"] is not None
        assert by_id[seed["failed_run"]]["status"] == "failed"
        assert by_id[seed["failed_run"]]["last_entry_id"] is None
        assert by_id[seed["failed_run"]]["error_code"] == "execution_failed"
        assert by_id[seed["failed_run"]]["error_message"] == "执行失败，请重新发起请求。"
        assert by_id[seed["completed_run"]]["status"] == "completed"
        assert by_id[seed["recovered_run"]]["status"] == "interrupted"
        assert by_id[seed["recovered_run"]]["finished_at"] is None
        assert by_id[seed["recovered_run"]]["error_code"] is None

        steering = http.get(steering_url)
        assert steering.status_code == 200, steering.text
        assert steering.headers["cache-control"] == "no-store"
        assert steering.headers["content-type"].startswith("application/json")
        items = steering.json()
        assert set(items) == {"session_id", "run_id", "steering"}
        assert items["session_id"] == seed["session_id"]
        assert items["run_id"] == seed["cancelled_run"]
        records = items["steering"]
        for item in records:
            assert_steering_fields(item, seed["session_id"], seed["cancelled_run"])
        assert [(item["created_at"], item["steering_id"]) for item in records] == order(
            [
                (20, seed["withdrawn_input"]),
                (20, seed["discarded_input"]),
                (50, seed["consumed_input"]),
            ]
        )
        assert {item["steering_id"]: item["status"] for item in records} == {
            seed["consumed_input"]: "consumed",
            seed["withdrawn_input"]: "withdrawn",
            seed["discarded_input"]: "discarded",
        }
        stored = {item["steering_id"]: item for item in records}
        assert stored[seed["consumed_input"]]["text"] == "补充一"
        assert stored[seed["discarded_input"]]["reason"] == "cancelled"
        assert stored[seed["withdrawn_input"]]["entry_id"] is None

        recovered = http.get(
            f"/api/sessions/{seed['session_id']}/runs/{seed['recovered_run']}/steering"
        )
        assert recovered.status_code == 200, recovered.text
        recovered_items = recovered.json()["steering"]
        assert [item["steering_id"] for item in recovered_items] == [
            seed["recovered_input"]
        ]
        assert recovered_items[0]["status"] == "discarded"
        assert recovered_items[0]["reason"] == "interrupted"
        assert recovered_items[0]["text"] == "补充四"

        for run_id in (seed["failed_run"], seed["completed_run"]):
            empty = http.get(
                f"/api/sessions/{seed['session_id']}/runs/{run_id}/steering"
            )
            assert empty.status_code == 200, empty.text
            assert empty.json() == {
                "session_id": seed["session_id"],
                "run_id": run_id,
                "steering": [],
            }

        empty_runs = http.get(f"/api/sessions/{seed['empty_session']}/runs")
        assert empty_runs.status_code == 200
        assert empty_runs.json() == {
            "session_id": seed["empty_session"],
            "runs": [],
        }

        # 编辑并重新生成删除的旧运行及旧输入不返回。
        deleted = http.get(f"/api/sessions/{seed['deleted_session']}/runs")
        assert deleted.status_code == 200
        assert [run["run_id"] for run in deleted.json()["runs"]] == [seed["edit_run"]]
        gone = http.get(
            f"/api/sessions/{seed['deleted_session']}/runs/"
            f"{seed['deleted_run']}/steering"
        )
        assert gone.status_code == 404, gone.text
        assert gone.json() == {
            "detail": {"code": "run_not_found", "message": "运行不存在。"}
        }

        missing_session_runs = http.get(f"/api/sessions/{uuid4()}/runs")
        assert missing_session_runs.status_code == 404
        assert missing_session_runs.json() == {
            "detail": {"code": "session_not_found", "message": "会话不存在。"}
        }
        missing_session_steering = http.get(
            f"/api/sessions/{uuid4()}/runs/{seed['cancelled_run']}/steering"
        )
        assert missing_session_steering.status_code == 404
        assert missing_session_steering.json()["detail"]["code"] == "session_not_found"

        # 运行属于其他会话时同样返回 run_not_found。
        cross_session = http.get(
            f"/api/sessions/{seed['empty_session']}/runs/{seed['cancelled_run']}/steering"
        )
        assert cross_session.status_code == 404, cross_session.text
        assert cross_session.json() == {
            "detail": {"code": "run_not_found", "message": "运行不存在。"}
        }
        missing_run = http.get(
            f"/api/sessions/{seed['session_id']}/runs/{uuid4()}/steering"
        )
        assert missing_run.status_code == 404
        assert missing_run.json()["detail"]["code"] == "run_not_found"

        # 紧凑形式必须按非法请求拒绝，不得降级为 200 或 404。
        compact_session = seed["session_id"].replace("-", "")
        compact_run = seed["cancelled_run"].replace("-", "")
        for response in (
            http.get("/api/sessions/not-a-uuid/runs"),
            http.get(f"/api/sessions/{seed['session_id']}/runs/not-a-uuid/steering"),
            http.get(f"/api/sessions/{compact_session}/runs"),
            http.get(f"/api/sessions/{seed['session_id']}/runs/{compact_run}/steering"),
        ):
            assert response.status_code == 422, response.text
            assert response.json() == {
                "detail": {"code": "invalid_request", "message": "请求字段不合法。"}
            }

        # 带连字符的大写形式归一为小写规范值，响应与规范请求逐字节一致。
        upper = http.get(f"/api/sessions/{seed['session_id'].upper()}/runs")
        assert upper.status_code == 200, upper.text
        assert upper.json() == body

        for host in ("evil.example", "localhost:9000"):
            for url in (runs_url, steering_url):
                assert http.get(url, headers={"Host": host}).status_code == 403
        for origin in ("null", "https://evil.example"):
            for url in (runs_url, steering_url):
                assert http.get(url, headers={"Origin": origin}).status_code == 403

        # 重复读取响应稳定，且不改动队列、输入状态、运行状态及会话位置。
        runs_text = {http.get(runs_url).text for _ in range(20)}
        steering_text = {http.get(steering_url).text for _ in range(20)}
        assert len(runs_text) == 1 and len(steering_text) == 1
        assert snapshot_all(path) == before

        leaky = str(uuid4())
        leak_run = str(uuid4())
        key = load_model_config().api_key
        insert_leaky_records(path, leaky, leak_run, key)
        blocked_runs = http.get(f"/api/sessions/{leaky}/runs")
        assert blocked_runs.status_code == 422, blocked_runs.text
        assert blocked_runs.json() == {
            "detail": {
                "code": "credential_detected",
                "message": CREDENTIAL_SAFE_MESSAGE,
            }
        }
        assert key not in blocked_runs.text
        blocked_steering = http.get(f"/api/sessions/{leaky}/runs/{leak_run}/steering")
        assert blocked_steering.status_code == 422, blocked_steering.text
        assert blocked_steering.json() == {
            "detail": {
                "code": "credential_detected",
                "message": CREDENTIAL_SAFE_MESSAGE,
            }
        }
        assert key not in blocked_steering.text

        live_session = str(uuid4())
        assert http.post(
            "/api/sessions",
            json={"session_id": live_session, "title": "运行中真实读取"},
        ).status_code in {200, 201}
        live = live_read_checks(http, live_session)

        evidence["http"] = {
            "runs": [run["run_id"] for run in runs],
            "statuses": sorted(run["status"] for run in runs),
            "steering": [item["status"] for item in records],
            "deleted_session_runs": [run["run_id"] for run in deleted.json()["runs"]],
            "credential_blocked": [
                blocked_runs.status_code,
                blocked_steering.status_code,
            ],
            "live": live,
        }

    async def persisted() -> list[str]:
        database, service = await open_service(path)
        try:
            return [run.id for run in await service.list_runs(seed["session_id"])]
        finally:
            await database.close()

    assert asyncio.run(persisted()) == [run["run_id"] for run in runs]


def check() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=EVIDENCE, ignore_cleanup_errors=True) as directory:
        evidence = asyncio.run(service_checks(Path(directory)))
        http_checks(evidence)
    (EVIDENCE / "run-steering-lists.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "PASS: 运行及 Steering 列表覆盖空列表、排序键、字段与 null、全部持久化状态、"
        "无助手消息的失败运行、执行期间真实读取、启动恢复、编辑及重新生成删除、"
        "跨会话运行、资源不存在、非法 UUID、Host/Origin 及凭据边界"
    )


if __name__ == "__main__":
    check()
