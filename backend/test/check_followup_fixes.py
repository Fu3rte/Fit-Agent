import asyncio
import io
import json
import logging
import sqlite3
from uuid import UUID, uuid4

from app.ai.messages import SystemMessage
from app.application.session.service import SessionService
from app.domain.session.models import (
    SendCommand,
    SendRequest,
    SteeringCommand,
    SteeringRequest,
)
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from app.interfaces.http import active, app, logger, retire, runs, runs_lock
from test.check_http import create_session, events, last_run, wait_idle
from test.regression_support import (
    Server,
    client,
    install_test_model_config,
    patch_default_database,
    temporary_root,
)

EVIDENCE = temporary_root("followup-fixes")
SYSTEM_MESSAGE = SystemMessage(
    role="system", content="按用户要求执行。", tools_added=[], timestamp=0
)
CANARY = "CANARY-SECRET-" + uuid4().hex


def call(coro):
    return asyncio.run_coroutine_threadsafe(coro, app.state.loop).result()


async def set_accepting(value: bool) -> None:
    app.state.accepting_runs = value


async def seed_restart(path):
    database = await open_database(path)
    try:
        service = SessionService(SqliteSessionRepository(database))
        session_id = str(uuid4())
        await service.create_session(session_id, "重启会话")
        first = await service.accept_send(
            SendCommand(
                operation_id=str(uuid4()),
                session_id=session_id,
                request=SendRequest(text="第一轮"),
            ),
            system_message=SYSTEM_MESSAGE,
        )
        await service.finish_run(session_id, first.run.id, "completed")
        second = await service.accept_send(
            SendCommand(
                operation_id=str(uuid4()),
                session_id=session_id,
                request=SendRequest(text="第二轮"),
            ),
            system_message=SYSTEM_MESSAGE,
        )
        run_id = second.run.id
        pending_op = str(uuid4())
        pending = await service.accept_steering(
            SteeringCommand(
                operation_id=pending_op,
                session_id=session_id,
                request=SteeringRequest(target_run_id=run_id, text="待消费"),
            )
        )
        consumed_op = str(uuid4())
        consumed = await service.accept_steering(
            SteeringCommand(
                operation_id=consumed_op,
                session_id=session_id,
                request=SteeringRequest(target_run_id=run_id, text="已消费"),
            )
        )
        consumption = await service.consume_steering(
            session_id, run_id, consumed.steering.id
        )
        assert consumption.created and consumption.entry is not None
        return {
            "session_id": session_id,
            "run_id": run_id,
            "completed_run_id": first.run.id,
            "pending_op": pending_op,
            "pending_id": pending.steering.id,
            "consumed_op": consumed_op,
            "consumed_id": consumed.steering.id,
            "consumed_entry_id": consumption.entry.id,
        }
    finally:
        await database.close()


def check_steering_restart(evidence):
    # 重启后以数据库为运行存在、会话归属及操作身份的权威。
    path = patch_default_database("followup-fixes-restart")
    seed = asyncio.run(seed_restart(path))
    session_id = seed["session_id"]
    run_id = seed["run_id"]
    server = Server(app)
    with server, client(server.base_url) as http:
        operation = http.get(
            f"/api/sessions/{session_id}/operations/{seed['pending_op']}"
        ).json()
        assert operation["accepted"] is True
        assert operation["steering"]["status"] == "discarded"

        def retry(operation_id, message, session=None):
            return http.post(
                f"/api/agent/runs/{run_id}/steering",
                json={
                    "session_id": session or session_id,
                    "operation_id": operation_id,
                    "message": message,
                },
            )

        pending_retry = retry(seed["pending_op"], "待消费")
        assert pending_retry.status_code == 200, pending_retry.text
        pending_body = pending_retry.json()
        assert pending_body["created"] is False
        assert pending_body["steering_id"] == seed["pending_id"]
        assert pending_body["status"] == "discarded"
        assert pending_body["reason"] == "interrupted"
        assert pending_body["entry_id"] is None

        repeat = retry(seed["pending_op"], "待消费")
        assert repeat.status_code == 200
        assert repeat.json() == pending_body

        consumed_retry = retry(seed["consumed_op"], "已消费")
        assert consumed_retry.status_code == 200
        consumed_body = consumed_retry.json()
        assert consumed_body["created"] is False
        assert consumed_body["steering_id"] == seed["consumed_id"]
        assert consumed_body["status"] == "consumed"
        assert consumed_body["entry_id"] == seed["consumed_entry_id"]

        withdraw_consumed = http.post(
            f"/api/agent/runs/{run_id}/steering/{seed['consumed_id']}/withdraw",
            json={"session_id": session_id},
        )
        assert withdraw_consumed.status_code == 409
        assert (
            withdraw_consumed.json()["detail"]["code"]
            == "steering_consumption_conflict"
        )

        withdraw_discarded = http.post(
            f"/api/agent/runs/{run_id}/steering/{seed['pending_id']}/withdraw",
            json={"session_id": session_id},
        )
        assert withdraw_discarded.status_code == 200
        assert withdraw_discarded.json()["status"] == "discarded"

        cross_session = retry(seed["pending_op"], "待消费", session=str(uuid4()))
        assert cross_session.status_code == 409
        assert cross_session.json()["detail"]["code"] == "session_mismatch"

        cross_run = http.post(
            f"/api/agent/runs/{seed['completed_run_id']}/steering/"
            f"{seed['pending_id']}/withdraw",
            json={"session_id": session_id},
        )
        assert cross_run.status_code == 409
        assert cross_run.json()["detail"]["code"] == "session_mismatch"

        missing_run = http.post(
            f"/api/agent/runs/{uuid4()}/steering",
            json={
                "session_id": session_id,
                "operation_id": seed["pending_op"],
                "message": "待消费",
            },
        )
        assert missing_run.status_code == 404
        assert missing_run.json()["detail"]["code"] == "run_not_found"

        missing_steering = http.post(
            f"/api/agent/runs/{run_id}/steering/{uuid4()}/withdraw",
            json={"session_id": session_id},
        )
        assert missing_steering.status_code == 404
        assert missing_steering.json()["detail"]["code"] == "steering_not_found"

        service = app.state.session_service
        remaining = call(service.list_steering(session_id, run_id))
        assert sorted(item.id for item in remaining) == sorted(
            [seed["pending_id"], seed["consumed_id"]]
        )
        evidence["steering_restart"] = {
            "pending_retry": pending_body,
            "consumed_retry": consumed_body,
            "withdraw_consumed": withdraw_consumed.status_code,
            "cross_session": cross_session.status_code,
            "cross_run": cross_run.status_code,
            "missing_run": missing_run.status_code,
            "missing_steering": missing_steering.status_code,
            "steering_rows": len(remaining),
        }


def check_log_protection(path, http, evidence):
    # 收尾终态提交异常含敏感内容时，日志只保留受控诊断字段。
    session_id = str(uuid4())
    create_session(http, session_id, "收尾日志保护")
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    original = (logger.handlers[:], logger.propagate, logger.level)
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.ERROR)
    raw = sqlite3.connect(path, isolation_level=None, timeout=30)
    raw.execute(
        "CREATE TRIGGER review_canary_finalize BEFORE UPDATE ON session_runs "
        "WHEN NEW.status <> 'running' "
        f"BEGIN SELECT RAISE(ABORT, '{CANARY}'); END"
    )
    raw.commit()
    run_id = None
    try:
        with http.stream(
            "POST",
            "/api/agent/run",
            json={
                "session_id": session_id,
                "operation_id": str(uuid4()),
                "request": "只回答 OK，无需使用工具。",
            },
        ) as response:
            run_id = response.headers["X-Run-ID"]
            collected = list(events(response))
        text = output.getvalue()
        assert CANARY not in text
        assert "运行收尾任务异常" in text and "IntegrityError" in text
        assert all(event["event"] not in {"done", "error"} for event in collected)
        assert active.locked()
        run = http.get(f"/api/sessions/{session_id}/runs/{run_id}").json()
        assert run["status"] == "running" and run["finished_at"] is None
        evidence["log_protection"] = {
            "events": len(collected),
            "log_has_canary": CANARY in text,
            "log_controlled": "IntegrityError" in text,
            "status": run["status"],
        }
    finally:
        raw.execute("DROP TRIGGER review_canary_finalize")
        raw.commit()
        raw.close()
        logger.handlers = original[0]
        logger.propagate = original[1]
        logger.setLevel(original[2])
        handler.close()
        if run_id is not None:
            call(app.state.steering.finish(session_id, run_id, "failed"))
            retire(runs[UUID(run_id)])


def check_finalization_read_failure(path, http, evidence):
    # 真实删除运行记录，制造收尾读取失败。
    session_id = str(uuid4())
    create_session(http, session_id, "收尾读取失败")
    raw = sqlite3.connect(path, isolation_level=None, timeout=30)
    run_id = None
    try:
        with http.stream(
            "POST",
            "/api/agent/run",
            json={
                "session_id": session_id,
                "operation_id": str(uuid4()),
                "request": "只回答 OK，无需使用工具。",
            },
        ) as response:
            run_id = response.headers["X-Run-ID"]
            raw.execute(
                "DELETE FROM session_runs WHERE session_id = ? AND id = ?",
                (session_id, run_id),
            )
            raw.commit()
            collected = list(events(response))
        assert all(event["event"] not in {"done", "error"} for event in collected)
        assert active.locked()
        query = http.get(f"/api/sessions/{session_id}/runs/{run_id}")
        assert query.status_code == 404
        assert query.json()["detail"]["code"] == "run_not_found"
        evidence["read_failure"] = {
            "events": len(collected),
            "query": query.status_code,
        }
    finally:
        raw.close()
        if run_id is not None:
            with runs_lock:
                runs.pop(UUID(run_id), None)
            active.release()


def check_duplicate_while_closing(http, evidence):
    session_id = str(uuid4())
    create_session(http, session_id, "关闭期间查重")
    body = {
        "session_id": session_id,
        "operation_id": str(uuid4()),
        "request": "只回答 OK，无需使用工具。",
    }
    with http.stream("POST", "/api/agent/run", json=body) as response:
        run_id = response.headers["X-Run-ID"]
        collected = list(events(response))
    assert collected[-1]["event"] == "done"
    wait_idle()
    call(set_accepting(False))
    try:
        repeated = http.post("/api/agent/run", json=body)
        assert repeated.status_code == 200
        payload = repeated.json()
        assert payload["run_id"] == run_id and payload["status"] == "completed"
        fresh = http.post(
            "/api/agent/run",
            json={
                "session_id": session_id,
                "operation_id": str(uuid4()),
                "request": "关闭期间的新请求",
            },
        )
        assert fresh.status_code == 409
        assert fresh.json()["detail"]["code"] == "run_busy"
        assert last_run(session_id).id == run_id
    finally:
        call(set_accepting(True))
    evidence["duplicate_while_closing"] = {
        "repeat": repeated.status_code,
        "fresh": fresh.status_code,
    }


def check():
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    install_test_model_config()
    evidence = {}
    check_steering_restart(evidence)

    path = patch_default_database("followup-fixes")
    server = Server(app)
    with server, client(server.base_url) as http:
        check_log_protection(path, http, evidence)
        check_finalization_read_failure(path, http, evidence)
        check_duplicate_while_closing(http, evidence)
        assert not active.locked()

    (EVIDENCE / "followup-fixes.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "PASS: 收尾日志保护、收尾读取失败结束 SSE、关闭期间查重与 run_busy、"
        "重启后 Steering 原身份与边界"
    )


if __name__ == "__main__":
    check()
