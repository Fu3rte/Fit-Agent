import asyncio
import sqlite3
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from uuid import uuid4

import aiosqlite

from app.ai.messages import SystemMessage
from app.application.session.service import SessionService
from app.application.session.steering import SteeringCoordinator
from app.domain.session.errors import (
    OperationConflict,
    RunClosed,
    SessionMismatch,
    SteeringConsumptionConflict,
)
from app.domain.session.models import (
    SendCommand,
    SendRequest,
    SteeringCommand,
    SteeringRequest,
)
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository

BACKEND = Path(__file__).resolve().parents[1]
TEMP_ROOT = BACKEND / "temp" / "steering"


async def open_service(path: Path):
    database = await open_database(path)
    return database, SessionService(SqliteSessionRepository(database))


async def start_run(service: SessionService, session_id: str) -> str:
    await service.create_session(session_id, "Steering 会话")
    outcome = await service.accept_send(
        SendCommand(
            operation_id=str(uuid4()),
            session_id=session_id,
            request=SendRequest(text="原始请求"),
        ),
        system_message=SystemMessage(role="system", content="sys", timestamp=0),
    )
    return outcome.run.id


def steering_command(session_id: str, run_id: str, text: str) -> SteeringCommand:
    return SteeringCommand(
        operation_id=str(uuid4()),
        session_id=session_id,
        request=SteeringRequest(target_run_id=run_id, text=text),
    )


async def scenario_accept_consume(service: SessionService) -> None:
    session_id = "s-steering-consume"
    run_id = await start_run(service, session_id)
    coordinator = SteeringCoordinator(service)
    notified: list[dict] = []
    coordinator.open(session_id, run_id, notified.append)

    command = steering_command(session_id, run_id, "第一段")
    created, first = await coordinator.accept(run_id, command)
    assert created is True and first.status == "pending" and first.entry_id is None
    assert (await service.get_steering(session_id, first.id)).status == "pending"

    repeat_created, repeat = await coordinator.accept(run_id, command)
    assert repeat_created is False and repeat.id == first.id and repeat.status == "pending"

    taken = await coordinator.take(run_id)
    assert [message.content for message in taken] == ["第一段"]
    assert await coordinator.take(run_id) == []

    entry_id = await coordinator.consume(run_id, taken)
    assert entry_id is not None
    consumed = await service.get_steering(session_id, first.id)
    assert consumed.status == "consumed" and consumed.entry_id == entry_id
    entry = await service.get_entry(session_id, entry_id)
    assert entry.run_id == run_id and entry.messages[0] == first.message
    assert entry.messages[0].timestamp == first.message.timestamp
    run = await service.get_run(session_id, run_id)
    assert entry.parent_id == run.request_entry_id and run.last_entry_id == entry_id
    assert (await service.get_current_branch(session_id))[-1].id == entry_id
    assert notified == [
        {"steering_id": first.id, "status": "consumed", "entry_id": entry_id, "reason": None}
    ]

    repeat_created, repeat = await coordinator.accept(run_id, command)
    assert repeat_created is False and repeat.status == "consumed" and repeat.id == first.id

    try:
        await coordinator.accept(
            run_id,
            SteeringCommand(
                operation_id=command.operation_id,
                session_id=session_id,
                request=SteeringRequest(target_run_id=run_id, text="另一段"),
            ),
        )
        raise AssertionError("同键不同请求必须冲突")
    except OperationConflict:
        pass

    await coordinator.finish(session_id, run_id, "completed")
    assert (await service.get_run(session_id, run_id)).status == "completed"


async def scenario_withdraw_race(service: SessionService) -> None:
    session_id = "s-steering-race"
    run_id = await start_run(service, session_id)
    coordinator = SteeringCoordinator(service)
    notified: list[dict] = []
    coordinator.open(session_id, run_id, notified.append)

    _, first = await coordinator.accept(
        run_id, steering_command(session_id, run_id, "竞争")
    )
    taken = await coordinator.take(run_id)
    try:
        await coordinator.withdraw(run_id, session_id, first.id)
        raise AssertionError("消费占用期间撤回必须返回消费冲突")
    except SteeringConsumptionConflict:
        pass
    assert (await service.get_steering(session_id, first.id)).status == "pending"

    await coordinator.consume(run_id, taken)
    try:
        await coordinator.withdraw(run_id, session_id, first.id)
        raise AssertionError("已消费输入撤回必须返回消费冲突")
    except SteeringConsumptionConflict:
        pass

    _, second = await coordinator.accept(
        run_id, steering_command(session_id, run_id, "撤回")
    )
    withdrawal = await coordinator.withdraw(run_id, session_id, second.id)
    assert withdrawal.changed is True and withdrawal.steering.status == "withdrawn"
    assert (await service.get_steering(session_id, second.id)).status == "withdrawn"
    assert await coordinator.take(run_id) == []

    repeat = await coordinator.withdraw(run_id, session_id, second.id)
    assert repeat.changed is False and repeat.steering.status == "withdrawn"
    assert notified[-1] == {
        "steering_id": second.id,
        "status": "withdrawn",
        "entry_id": None,
        "reason": None,
    }

    _, third = await coordinator.accept(
        run_id, steering_command(session_id, run_id, "交错")
    )
    await coordinator.withdraw(run_id, session_id, third.id)
    assert await coordinator.take(run_id) == []

    await coordinator.finish(session_id, run_id, "completed")


async def scenario_close_and_finish(service: SessionService) -> None:
    session_id = "s-steering-close"
    run_id = await start_run(service, session_id)
    coordinator = SteeringCoordinator(service)
    notified: list[dict] = []
    coordinator.open(session_id, run_id, notified.append)

    command = steering_command(session_id, run_id, "落单")
    _, pending = await coordinator.accept(run_id, command)
    await coordinator.finish(session_id, run_id, "failed")
    run = await service.get_run(session_id, run_id)
    assert run.status == "failed" and run.finished_at is not None
    discarded = await service.get_steering(session_id, pending.id)
    assert discarded.status == "discarded" and discarded.reason == "failed"
    assert notified == [
        {
            "steering_id": pending.id,
            "status": "discarded",
            "entry_id": None,
            "reason": "failed",
        }
    ]
    repeat_created, repeat = await coordinator.accept(run_id, command)
    assert repeat_created is False and repeat.id == pending.id and repeat.status == "discarded"
    try:
        await coordinator.accept(run_id, steering_command(session_id, run_id, "结束后"))
        raise AssertionError("结束后的接收必须被拒绝")
    except RunClosed:
        pass

    closed_session = "s-steering-empty"
    closed_run = await start_run(service, closed_session)
    closed = SteeringCoordinator(service)
    closed.open(closed_session, closed_run, notified.append)
    assert await closed.take_or_close(closed_run) == []
    try:
        await closed.accept(
            closed_run, steering_command(closed_session, closed_run, "关闭后")
        )
        raise AssertionError("空队列关闭后的接收必须被拒绝")
    except RunClosed:
        pass
    await closed.finish(closed_session, closed_run, "completed")


async def scenario_run_ownership(service: SessionService) -> None:
    session_id = "s-steering-owner"
    abandoned = await start_run(service, session_id)
    await service.finish_run(session_id, abandoned, "completed")
    outcome = await service.accept_send(
        SendCommand(
            operation_id=str(uuid4()),
            session_id=session_id,
            request=SendRequest(text="第二轮"),
        ),
        system_message=SystemMessage(role="system", content="sys", timestamp=0),
    )
    second_run = outcome.run.id
    coordinator = SteeringCoordinator(service)
    coordinator.open(session_id, second_run, lambda data: None)

    _, steering = await coordinator.accept(
        second_run, steering_command(session_id, second_run, "归属")
    )
    try:
        await coordinator.withdraw(abandoned, session_id, steering.id)
        raise AssertionError("跨运行撤回必须返回会话不匹配")
    except SessionMismatch:
        pass

    withdrawal = await coordinator.withdraw(second_run, session_id, steering.id)
    assert withdrawal.changed is True and withdrawal.steering.status == "withdrawn"
    await coordinator.finish(session_id, second_run, "completed")


async def scenario_occupied_conflict(service, database, path) -> None:
    await database.connection.execute("PRAGMA busy_timeout = 5000")
    session_id = "s-steering-occupied"
    run_id = await start_run(service, session_id)
    coordinator = SteeringCoordinator(service)
    notified: list[dict] = []
    coordinator.open(session_id, run_id, notified.append)

    _, first = await coordinator.accept(
        run_id, steering_command(session_id, run_id, "消费中")
    )
    taken = await coordinator.take(run_id)

    # 真实 SQLite 排他锁：外部连接持锁，阻塞消费事务的首次读取，保持事务未提交。
    holder = await aiosqlite.connect(path, isolation_level=None)
    await holder.execute("BEGIN EXCLUSIVE")
    consume_task = asyncio.create_task(coordinator.consume(run_id, taken))
    try:
        await asyncio.sleep(0.3)
        assert not consume_task.done()

        started = time.monotonic()
        try:
            await coordinator.withdraw(run_id, session_id, first.id)
        except SteeringConsumptionConflict:
            pass
        else:
            raise AssertionError("占用期间撤回未返回消费冲突")
        assert time.monotonic() - started < 0.5, "撤回等待了消费事务提交"
    finally:
        await holder.execute("ROLLBACK")
        await holder.close()
    await consume_task
    assert len(notified) == 1 and notified[0]["status"] == "consumed"
    assert notified[0]["entry_id"] is not None
    assert (await service.get_steering(session_id, first.id)).status == "consumed"
    await coordinator.finish(session_id, run_id, "completed")


async def scenario_consume_priority(service, database, path) -> None:
    session_id = "s-steering-priority"
    run_id = await start_run(service, session_id)
    coordinator = SteeringCoordinator(service)
    coordinator.open(session_id, run_id, lambda data: None)

    _, pending = await coordinator.accept(
        run_id, steering_command(session_id, run_id, "待竞争")
    )

    release = Event()

    def hold() -> int:
        release.wait(10)
        return 1

    await database.connection.create_function("review_hold", 0, hold)
    raw = await aiosqlite.connect(path, isolation_level=None)
    await raw.execute(
        "CREATE TRIGGER review_hold_insert BEFORE INSERT ON steering_inputs "
        "BEGIN SELECT review_hold(); END"
    )
    await raw.commit()
    await raw.close()
    # 受理在持有裁决期间被真实数据库触发器阻塞，制造消费与撤回共同等待裁决。
    blocking = asyncio.create_task(
        coordinator.accept(run_id, steering_command(session_id, run_id, "占位"))
    )
    try:
        await asyncio.sleep(0.3)
        assert not blocking.done()

        withdrawal = asyncio.create_task(
            coordinator.withdraw(run_id, session_id, pending.id)
        )
        await asyncio.sleep(0)
        consumption = asyncio.create_task(coordinator.take(run_id))
        await asyncio.sleep(0)
        assert not withdrawal.done() and not consumption.done()

        release.set()
        await blocking
    finally:
        release.set()
        raw = await aiosqlite.connect(path, isolation_level=None)
        await raw.execute("DROP TRIGGER review_hold_insert")
        await raw.commit()
        await raw.close()
    taken = await consumption
    try:
        await withdrawal
    except SteeringConsumptionConflict:
        pass
    else:
        raise AssertionError("消费优先时撤回必须返回消费冲突")
    assert [message.content for message in taken] == ["待竞争", "占位"]
    await coordinator.finish(session_id, run_id, "completed")


async def scenario_consume_failure(service, path) -> None:
    session_id = "s-steering-consume-failure"
    run_id = await start_run(service, session_id)
    coordinator = SteeringCoordinator(service)
    notified: list[dict] = []
    coordinator.open(session_id, run_id, notified.append)

    _, first = await coordinator.accept(
        run_id, steering_command(session_id, run_id, "消费失败")
    )
    taken = await coordinator.take(run_id)

    raw = await aiosqlite.connect(path, isolation_level=None)
    await raw.execute(
        "CREATE TRIGGER review_consume_fail BEFORE UPDATE ON steering_inputs "
        "WHEN NEW.status = 'consumed' BEGIN SELECT RAISE(ABORT, '注入消费失败'); END"
    )
    await raw.commit()
    await raw.close()
    failure = None
    try:
        await coordinator.consume(run_id, taken)
    except sqlite3.IntegrityError as error:
        failure = error
    assert failure is not None
    assert notified == []
    steering = await service.get_steering(session_id, first.id)
    assert steering.status == "pending" and steering.entry_id is None
    run = await service.get_run(session_id, run_id)
    assert run.status == "running" and run.last_entry_id is None

    raw = await aiosqlite.connect(path, isolation_level=None)
    await raw.execute("DROP TRIGGER review_consume_fail")
    await raw.commit()
    await raw.close()
    await coordinator.finish(session_id, run_id, "failed")


async def run() -> None:
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=TEMP_ROOT, ignore_cleanup_errors=True) as directory:
        path = Path(directory) / "steering.db"
        database, service = await open_service(path)
        try:
            await scenario_accept_consume(service)
            await scenario_withdraw_race(service)
            await scenario_close_and_finish(service)
            await scenario_run_ownership(service)
            await scenario_occupied_conflict(service, database, path)
            await scenario_consume_priority(service, database, path)
            await scenario_consume_failure(service, path)
        finally:
            await database.close()
    print(
        "Steering 队列用例：受理入队一次、重复返回原身份、消费原子提交、"
        "撤回与消费竞争边界、消费优先裁决、真实 SQLite 锁即时冲突、"
        "消费提交失败、空队列关闭、终态丢弃自检通过"
    )


def check() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    check()
