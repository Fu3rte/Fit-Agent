import asyncio
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

import aiosqlite

from app.agent.agent_loop import run_agent_loop
from app.agent.config import AgentLoopConfig
from app.ai.messages import (
    AssistantMessage,
    TextContent,
    Usage,
    UserMessage,
)
from app.ai.stream import AssistantResponse
from app.application.session.service import SessionService
from app.application.session.steering import SteeringCoordinator
from test.check_steering import open_service, start_run, steering_command
from test.regression_support import install_test_model_config

BACKEND = Path(__file__).resolve().parents[1]
TEMP_ROOT = BACKEND / "temp" / "steering-consumption"
TRIGGER = "review_second_consume"


async def seed_two(service: SessionService, session_id: str):
    run_id = await start_run(service, session_id)
    coordinator = SteeringCoordinator(service)
    notified: list[dict] = []
    coordinator.open(session_id, run_id, notified.append)
    _, first = await coordinator.accept(
        run_id, steering_command(session_id, run_id, "第一条")
    )
    _, second = await coordinator.accept(
        run_id, steering_command(session_id, run_id, "第二条")
    )
    return run_id, coordinator, notified, first, second


async def arm_second_failure(path: Path, second_id: str) -> None:
    # 真实 SQLite 触发器：仅第二条消费的状态更新失败。
    raw = await aiosqlite.connect(path, isolation_level=None)
    try:
        await raw.execute(
            f"CREATE TRIGGER {TRIGGER} BEFORE UPDATE ON steering_inputs "
            f"WHEN NEW.id = '{second_id}' AND NEW.status = 'consumed' "
            "BEGIN SELECT RAISE(ABORT, '第二条消费失败'); END"
        )
        await raw.commit()
    finally:
        await raw.close()


async def disarm(path: Path) -> None:
    raw = await aiosqlite.connect(path, isolation_level=None)
    try:
        await raw.execute(f"DROP TRIGGER IF EXISTS {TRIGGER}")
        await raw.commit()
    finally:
        await raw.close()


async def fail_second_consume(service: SessionService, path: Path, session_id: str):
    run_id, coordinator, notified, first, second = await seed_two(service, session_id)
    messages = await coordinator.take(run_id)
    await arm_second_failure(path, second.id)
    failure = None
    try:
        await coordinator.consume(run_id, messages)
    except sqlite3.IntegrityError as error:
        failure = error
    finally:
        await disarm(path)
    assert failure is not None
    return run_id, coordinator, notified, first, second


def _assistant_message(model_spec, text: str) -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=[TextContent(type="text", text=text)],
        api=model_spec.api,
        provider=model_spec.provider,
        model=model_spec.id,
        usage=Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2),
        stop_reason="stop",
        timestamp=0,
    )


async def _assistant_events(model_spec):
    final = _assistant_message(model_spec, "完成")
    partial = final.model_copy(update={"stop_reason": "pending", "usage": None})
    yield {"type": "start", "partial": partial}
    yield {"type": "done", "reason": "stop", "message": final}


async def scenario_both_consumed(service: SessionService) -> dict:
    session_id = "s-consumption-both"
    run_id, coordinator, notified, first, second = await seed_two(service, session_id)
    messages = await coordinator.take(run_id)
    assert [message.content for message in messages] == ["第一条", "第二条"]

    last_entry_id = await coordinator.consume(run_id, messages)
    first_stored = await service.get_steering(session_id, first.id)
    second_stored = await service.get_steering(session_id, second.id)
    assert first_stored.status == "consumed" and first_stored.entry_id
    assert second_stored.status == "consumed" and second_stored.entry_id
    assert last_entry_id == second_stored.entry_id

    assert [item["steering_id"] for item in notified] == [first.id, second.id]
    assert [item["status"] for item in notified] == ["consumed", "consumed"]
    assert [item["entry_id"] for item in notified] == [
        first_stored.entry_id,
        second_stored.entry_id,
    ]

    first_entry = await service.get_entry(session_id, first_stored.entry_id)
    second_entry = await service.get_entry(session_id, second_stored.entry_id)
    run = await service.get_run(session_id, run_id)
    assert first_entry.parent_id == run.request_entry_id
    assert second_entry.parent_id == first_entry.id
    assert run.last_entry_id == second_entry.id
    assert (await service.get_current_branch(session_id))[-1].id == second_entry.id
    assert first_entry.messages[0] == first.message
    assert second_entry.messages[0] == second.message
    assert len(await service.list_entries(session_id)) == 4

    evidence = {
        "last_entry_id": last_entry_id,
        "notified": notified,
        "entries": 4,
    }
    await coordinator.finish(session_id, run_id, "completed")
    return evidence


async def scenario_partial_failure(service: SessionService, path: Path) -> dict:
    session_id = "s-consumption-partial"
    run_id, coordinator, notified, first, second = await fail_second_consume(
        service, path, session_id
    )

    first_stored = await service.get_steering(session_id, first.id)
    second_stored = await service.get_steering(session_id, second.id)
    assert first_stored.status == "consumed" and first_stored.entry_id
    assert second_stored.status == "pending" and second_stored.entry_id is None
    assert [item["status"] for item in notified] == ["consumed"]
    assert notified[0]["steering_id"] == first.id
    assert notified[0]["entry_id"] == first_stored.entry_id

    first_entry = await service.get_entry(session_id, first_stored.entry_id)
    assert first_entry.messages[0] == first.message
    run = await service.get_run(session_id, run_id)
    assert run.status == "running" and run.last_entry_id == first_stored.entry_id

    evidence = {
        "first_committed": True,
        "first_consumed_notification": True,
        "second_status": second_stored.status,
        "notifications": [item["status"] for item in notified],
    }
    await coordinator.finish(session_id, run_id, "failed")
    return evidence


async def scenario_failure_finalize(service: SessionService, path: Path) -> dict:
    session_id = "s-consumption-finalize"
    run_id, coordinator, notified, first, second = await fail_second_consume(
        service, path, session_id
    )

    await coordinator.finish(session_id, run_id, "failed")
    run = await service.get_run(session_id, run_id)
    first_stored = await service.get_steering(session_id, first.id)
    second_stored = await service.get_steering(session_id, second.id)
    assert run.status == "failed" and run.finished_at is not None
    assert first_stored.status == "consumed"
    assert second_stored.status == "discarded" and second_stored.reason == "failed"
    assert second_stored.entry_id is None
    assert [item["status"] for item in notified] == ["consumed", "discarded"]
    assert [item["steering_id"] for item in notified] == [first.id, second.id]

    return {
        "run_status": run.status,
        "first_status": first_stored.status,
        "second_status": second_stored.status,
        "second_reason": second_stored.reason,
        "notifications": [item["status"] for item in notified],
    }


async def scenario_no_model_after_failure(service: SessionService, path: Path) -> dict:
    session_id = "s-consumption-no-model"
    run_id, coordinator, notified, first, second = await seed_two(service, session_id)
    await arm_second_failure(path, second.id)

    calls: list[int] = []

    def stream_fn(model_spec, context, options):
        calls.append(1)
        return AssistantResponse(_assistant_events(model_spec))

    steering_calls = {"count": 0}

    async def get_steering():
        # 首次轮次前不返回输入，确保已经发起过一次真实模型请求。
        steering_calls["count"] += 1
        if steering_calls["count"] == 1:
            return []
        return await coordinator.take(run_id)

    async def on_consumed(messages):
        await coordinator.consume(run_id, messages)

    config = AgentLoopConfig(
        model=install_test_model_config(),
        max_turns=4,
        get_steering_messages=get_steering,
        on_steering_consumed=on_consumed,
    )
    emitted: list[dict] = []

    async def emit(event):
        emitted.append(event)

    failure = None
    try:
        await run_agent_loop(
            [UserMessage(role="user", content="开始", timestamp=0)],
            {"messages": [], "tools": {}},
            config,
            emit,
            None,
            stream_fn=stream_fn,
        )
    except BaseException as error:
        failure = error
    finally:
        await disarm(path)

    assert isinstance(failure, sqlite3.IntegrityError), failure
    assert calls == [1]
    assert [event["type"] for event in emitted] == [
        "trace_start",
        "turn_start",
        "message_start",
        "message_end",
        "turn_end",
        "turn_start",
        "turn_end",
        "trace_end",
    ]
    assert emitted[4]["status"] == "completed"
    assert emitted[6]["status"] == "failed"
    assert emitted[-1]["status"] == "failed"
    assert [item["status"] for item in notified] == ["consumed"]
    assert notified[0]["steering_id"] == first.id

    evidence = {
        "model_calls": len(calls),
        "second_consume_failed": True,
        "notifications": [item["status"] for item in notified],
    }
    await coordinator.finish(session_id, run_id, "failed")
    return evidence


async def scenario_repeated_consume(service: SessionService) -> dict:
    session_id = "s-consumption-repeat"
    run_id, coordinator, notified, first, second = await seed_two(service, session_id)
    messages = await coordinator.take(run_id)
    last_entry_id = await coordinator.consume(run_id, messages)
    entries_once = len(await service.list_entries(session_id))

    repeat = await coordinator.consume(run_id, messages)
    entries_repeat = len(await service.list_entries(session_id))
    assert repeat is None
    assert entries_repeat == entries_once
    assert [item["status"] for item in notified] == ["consumed", "consumed"]

    again = await service.consume_steering(session_id, run_id, first.id)
    assert again.created is False and again.steering.status == "consumed"
    assert len(await service.list_entries(session_id)) == entries_once

    evidence = {
        "last_entry_id": last_entry_id,
        "entries": entries_once,
        "notifications": [item["status"] for item in notified],
    }
    await coordinator.finish(session_id, run_id, "completed")
    return evidence


async def run() -> None:
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    evidence: dict[str, object] = {}
    with TemporaryDirectory(dir=TEMP_ROOT, ignore_cleanup_errors=True) as directory:
        path = Path(directory) / "steering-consumption.db"
        database, service = await open_service(path)
        try:
            evidence["both_consumed"] = await scenario_both_consumed(service)
            evidence["partial_failure"] = await scenario_partial_failure(service, path)
            evidence["failure_finalize"] = await scenario_failure_finalize(service, path)
            evidence["no_model_after_failure"] = await scenario_no_model_after_failure(
                service, path
            )
            evidence["repeated_consume"] = await scenario_repeated_consume(service)
        finally:
            await database.close()
    (TEMP_ROOT / "steering-consumption.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "PASS: 多条消费逐条提交即时确认、部分失败保留已提交节点与通知、"
        "失败收尾丢弃剩余 pending、消费失败后不发起模型请求、重复消费不新增节点与确认"
    )


def check() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    check()
