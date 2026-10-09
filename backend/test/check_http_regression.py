import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

from app.ai.messages import SystemMessage, ToolResultMessage
from app.application.session.service import SessionService
from app.domain.session.models import (
    SendCommand,
    SendRequest,
    SteeringCommand,
    SteeringRequest,
)
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from app.interfaces.http import active, app, retire, runs
from app.model_config import load_model_config
from test.check_http import (
    create_session,
    events,
    stored_messages,
    validate_events,
    wait_idle,
)
from test.regression_support import (
    Server,
    client,
    patch_default_database,
    seed_note,
    temporary_root,
)

EVIDENCE = temporary_root("http-regression")
SYSTEM_MESSAGE = SystemMessage(
    role="system", content="按用户要求执行。", tools_added=[], timestamp=0
)
# 真实模型长输出流提供可观测的运行中窗口，用于断连、关闭与取消收尾场景。
BUSY_REQUEST = "逐行输出从 1 到 100000 的整数，不要省略。无需使用工具。"


def service_call(coro):
    return asyncio.run_coroutine_threadsafe(coro, app.state.loop).result()


def seed_legacy(path):
    async def seed():
        database = await open_database(path)
        try:
            service = SessionService(SqliteSessionRepository(database))
            session_id = str(uuid4())
            await service.create_session(session_id, "遗留会话")
            send_op = str(uuid4())
            send = await service.accept_send(
                SendCommand(
                    operation_id=send_op,
                    session_id=session_id,
                    request=SendRequest(text="遗留运行"),
                ),
                system_message=SYSTEM_MESSAGE,
            )
            steering_op = str(uuid4())
            pending = await service.accept_steering(
                SteeringCommand(
                    operation_id=steering_op,
                    session_id=session_id,
                    request=SteeringRequest(
                        target_run_id=send.run.id, text="遗留输入"
                    ),
                )
            )
            return {
                "session_id": session_id,
                "run_id": send.run.id,
                "send_op": send_op,
                "steering_op": steering_op,
                "steering_id": pending.steering.id,
            }
        finally:
            await database.close()

    return asyncio.run(seed())


def post_run(base_url, payload):
    with client(base_url) as http:
        with http.stream("POST", "/api/agent/run", json=payload) as response:
            content_type = response.headers.get("content-type", "")
            if content_type.startswith("text/event-stream"):
                return response.status_code, "sse", response.headers.get("X-Run-ID")
            return response.status_code, "json", response.read().decode("utf-8")


def check_duplicate_and_queries(server, http, evidence):
    session_id = str(uuid4())
    create_session(http, session_id, "回归会话")
    operation_id = str(uuid4())
    body = {
        "session_id": session_id,
        "operation_id": operation_id,
        "request": "只回答 OK，无需使用工具。",
    }
    with http.stream("POST", "/api/agent/run", json=body) as response:
        run_id = response.headers["X-Run-ID"]
        first = list(events(response))
    wait_idle()
    validate_events(first)
    assert first[-1]["event"] == "done"

    repeated = http.post("/api/agent/run", json=body)
    assert repeated.status_code == 200
    assert repeated.headers["content-type"].startswith("application/json")
    assert repeated.json() == {
        "operation_id": operation_id,
        "session_id": session_id,
        "run_id": run_id,
        "request_entry_id": repeated.json()["request_entry_id"],
        "status": "completed",
    }
    UUID(repeated.json()["request_entry_id"])
    assert http.post("/api/agent/run", json={**body, "request": "不同请求"}).status_code == 409

    operation = http.get(f"/api/sessions/{session_id}/operations/{operation_id}")
    assert operation.status_code == 200
    assert operation.json()["accepted"] is True
    assert operation.json()["kind"] == "send"
    assert operation.json()["run"]["run_id"] == run_id
    assert operation.json()["run"]["status"] == "completed"
    assert operation.json()["steering"] is None

    run = http.get(f"/api/sessions/{session_id}/runs/{run_id}")
    assert run.status_code == 200 and run.json() == operation.json()["run"]

    unknown = http.get(f"/api/sessions/{session_id}/operations/{uuid4()}")
    assert unknown.status_code == 200
    assert unknown.json()["accepted"] is False and unknown.json()["run"] is None
    assert http.get(f"/api/sessions/{session_id}/runs/{uuid4()}").status_code == 404
    assert http.get(f"/api/sessions/{uuid4()}/operations/{uuid4()}").status_code == 404
    evidence["duplicate_and_queries"] = {
        "run_id": run_id,
        "repeat": repeated.json(),
        "operation": operation.json(),
    }


def check_unknown_result_query_during_run(server, http, evidence):
    session_id = str(uuid4())
    create_session(http, session_id, "运行中查询")
    operation_id = str(uuid4())
    with http.stream(
        "POST",
        "/api/agent/run",
        json={
            "session_id": session_id,
            "operation_id": operation_id,
            "request": f"必须调用 read 读取 {seed_note(session_id)} 一次，然后报告读取结果。",
        },
    ) as response:
        run_id = response.headers["X-Run-ID"]
        observed = None
        for event in events(response):
            if event["event"] == "tool_start" and observed is None:
                assert active.locked()
                run = http.get(f"/api/sessions/{session_id}/runs/{run_id}")
                operation = http.get(
                    f"/api/sessions/{session_id}/operations/{operation_id}"
                )
                observed = {"run": run.json(), "operation": operation.json()}
    assert observed is not None
    assert observed["run"]["run_id"] == run_id
    assert observed["run"]["status"] == "running"
    assert observed["run"]["finished_at"] is None
    assert observed["operation"]["accepted"] is True
    assert observed["operation"]["run"]["status"] == "running"
    wait_idle()
    assert (
        http.get(f"/api/sessions/{session_id}/runs/{run_id}").json()["status"]
        == "completed"
    )
    evidence["unknown_result_query"] = observed


def check_concurrent_acceptance(server, http, evidence):
    session_id = str(uuid4())
    create_session(http, session_id, "并发受理")
    operation_id = str(uuid4())
    body = {
        "session_id": session_id,
        "operation_id": operation_id,
        "request": "只回答 OK，无需使用工具。",
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(post_run, server.base_url, body) for _ in range(2)]
        outcomes = [future.result() for future in futures]
    streams = [outcome for outcome in outcomes if outcome[1] == "sse"]
    others = [outcome for outcome in outcomes if outcome[1] != "sse"]
    assert len(streams) == 1 and streams[0][0] == 200, outcomes
    winner = streams[0][2]
    UUID(winner)
    assert len(others) == 1
    status, _, payload = others[0]
    if status == 200:
        assert json.loads(payload)["run_id"] == winner
    else:
        assert status == 409 and json.loads(payload)["detail"]["code"] == "run_busy"
    wait_idle()
    assert len(service_call(app.state.session_service.list_runs(session_id))) == 1
    accepted = http.get(f"/api/sessions/{session_id}/operations/{operation_id}")
    assert accepted.json()["accepted"] is True
    assert accepted.json()["run"]["run_id"] == winner
    evidence["concurrent_acceptance"] = {
        "statuses": [(status, kind) for status, kind, _ in outcomes],
        "run_id": winner,
    }


def check_steering_races(server, http, evidence):
    session_id = str(uuid4())
    create_session(http, session_id, "Steering 竞争")
    started = {"S1": None, "S2": None}

    def steer(text):
        operation_id = str(uuid4())
        response = http.post(
            f"/api/agent/runs/{run_id}/steering",
            json={
                "session_id": session_id,
                "operation_id": operation_id,
                "message": text,
            },
        )
        assert response.status_code == 200, response.text
        return operation_id, response.json()["steering_id"]

    with http.stream(
        "POST",
        "/api/agent/run",
        json={
            "session_id": session_id,
            "operation_id": str(uuid4()),
            "request": f"必须调用 read 读取 {seed_note(session_id)} 一次，然后报告读取结果。",
        },
    ) as response:
        run_id = response.headers["X-Run-ID"]
        collected = []
        for event in events(response):
            collected.append(event)
            if event["event"] == "tool_start" and started["S1"] is None:
                started["S1"] = steer("追加一")
                started["S2"] = steer("追加二")
                withdrawn = http.post(
                    f"/api/agent/runs/{run_id}/steering/{started['S1'][1]}/withdraw",
                    json={"session_id": session_id},
                )
                assert withdrawn.status_code == 200
                assert withdrawn.json()["status"] == "withdrawn"
    wait_idle()

    def state(operation_id):
        body = http.get(
            f"/api/sessions/{session_id}/operations/{operation_id}"
        ).json()
        return body["steering"]

    withdrawn = state(started["S1"][0])
    assert withdrawn["status"] == "withdrawn" and withdrawn["entry_id"] is None
    consumed = state(started["S2"][0])
    assert consumed["status"] == "consumed" and consumed["entry_id"] is not None
    conflict = http.post(
        f"/api/agent/runs/{run_id}/steering/{started['S2'][1]}/withdraw",
        json={"session_id": session_id},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "steering_consumption_conflict"
    closed = http.post(
        f"/api/agent/runs/{run_id}/steering",
        json={"session_id": session_id, "operation_id": str(uuid4()), "message": "关闭后"},
    )
    assert closed.status_code == 409
    assert closed.json()["detail"]["code"] == "run_closed"
    statuses = [
        (event["data"]["steering_id"], event["data"]["status"])
        for event in collected
        if event["event"] == "steering_status"
    ]
    assert (started["S1"][1], "withdrawn") in statuses
    assert (started["S2"][1], "consumed") in statuses
    assert (started["S1"][1], "consumed") not in statuses
    evidence["steering_races"] = {
        "statuses": statuses,
        "withdrawn": withdrawn,
        "consumed": consumed,
    }


def check_steering_discard_on_cancel(server, http, evidence):
    session_id = str(uuid4())
    create_session(http, session_id, "取消丢弃")
    steering_op = str(uuid4())
    with http.stream(
        "POST",
        "/api/agent/run",
        json={
            "session_id": session_id,
            "operation_id": str(uuid4()),
            "request": BUSY_REQUEST,
        },
    ) as response:
        run_id = response.headers["X-Run-ID"]
        for event in events(response):
            if event["event"] == "message_update":
                accepted = http.post(
                    f"/api/agent/runs/{run_id}/steering",
                    json={
                        "session_id": session_id,
                        "operation_id": steering_op,
                        "message": "取消前追加",
                    },
                )
                assert accepted.status_code == 200
                steering_id = accepted.json()["steering_id"]
                break
    wait_idle()
    assert http.get(f"/api/sessions/{session_id}/runs/{run_id}").json()["status"] == "cancelled"
    dropped = http.get(
        f"/api/sessions/{session_id}/operations/{steering_op}"
    ).json()["steering"]
    assert dropped["steering_id"] == steering_id
    assert dropped["status"] == "discarded" and dropped["reason"] == "cancelled"
    again = http.post(
        f"/api/agent/runs/{run_id}/steering/{steering_id}/withdraw",
        json={"session_id": session_id},
    )
    assert again.status_code == 200
    assert again.json()["status"] == "discarded"
    evidence["steering_discard_on_cancel"] = {"dropped": dropped, "withdraw": again.json()}


def check_commit_before_notify(server, http, evidence):
    session_id = str(uuid4())
    create_session(http, session_id, "提交先于通知")
    observed = []

    with http.stream(
        "POST",
        "/api/agent/run",
        json={
            "session_id": session_id,
            "operation_id": str(uuid4()),
            "request": f"必须调用 read 读取 {seed_note(session_id)} 一次，然后报告读取结果。",
        },
    ) as response:
        run_id = response.headers["X-Run-ID"]
        for event in events(response):
            kind = event["event"]
            if kind == "tool_result":
                history = stored_messages(session_id)
                assert any(
                    isinstance(message, ToolResultMessage)
                    and message.tool_call_id == event["data"]["tool_call_id"]
                    for message in history
                )
                observed.append("tool_result:committed")
            elif kind == "message_end":
                history = stored_messages(session_id)
                assert any(
                    entry.id == event["data"]["message_id"] for entry in service_entries(session_id)
                )
                observed.append("message_end:committed")
            elif kind == "done":
                assert (
                    http.get(f"/api/sessions/{session_id}/runs/{run_id}").json()["status"]
                    == "completed"
                )
                observed.append("done:committed")
    wait_idle()
    assert {"tool_result:committed", "message_end:committed", "done:committed"} <= set(observed)
    evidence["commit_before_notify"] = observed


def service_entries(session_id):
    service = app.state.session_service

    async def load():
        return await service.list_entries(session_id)

    return service_call(load())


def check_credential_interception(server, http, evidence):
    key = load_model_config().OPENAI_API_KEY
    session_id = str(uuid4())
    create_session(http, session_id, "凭据拦截")
    blocked = http.post(
        "/api/agent/run",
        json={
            "session_id": session_id,
            "operation_id": str(uuid4()),
            "request": f"记住这段凭据 {key}",
        },
    )
    assert blocked.status_code == 422
    assert blocked.json()["detail"]["code"] == "credential_detected"
    assert http.post(
        "/api/sessions", json={"session_id": str(uuid4()), "title": f"标题 {key}"}
    ).status_code == 422
    assert stored_messages(session_id) == [] and not active.locked()
    evidence["credential_interception"] = blocked.json()


def check_restart_recovery(server, http, seed, evidence):
    session_id = seed["session_id"]
    run = http.get(f"/api/sessions/{session_id}/runs/{seed['run_id']}")
    assert run.status_code == 200
    assert run.json()["status"] == "interrupted"
    assert run.json()["finished_at"] is None
    send = http.get(f"/api/sessions/{session_id}/operations/{seed['send_op']}").json()
    assert send["accepted"] is True and send["run"]["status"] == "interrupted"
    drop = http.get(
        f"/api/sessions/{session_id}/operations/{seed['steering_op']}"
    ).json()
    assert drop["accepted"] is True
    assert drop["steering"]["status"] == "discarded"
    assert drop["steering"]["reason"] == "interrupted"
    assert drop["steering"]["entry_id"] is None
    evidence["restart_recovery"] = {
        "run": run.json(),
        "send": send,
        "steering": drop,
    }


def check_shutdown_finalization(path, evidence):
    server = Server(app)
    with server:
        with client(server.base_url) as http:
            session_id = str(uuid4())
            create_session(http, session_id, "关闭收尾")
            operation_id = str(uuid4())
            with http.stream(
                "POST",
                "/api/agent/run",
                json={
                    "session_id": session_id,
                    "operation_id": operation_id,
                    "request": BUSY_REQUEST,
                },
            ) as response:
                run_id = response.headers["X-Run-ID"]
                for event in events(response):
                    if event["event"] == "message_update":
                        assert active.locked()
                        break
                shutdown = {"run_id": run_id, "session_id": session_id}

    async def inspect():
        database = await open_database(path)
        try:
            service = SessionService(SqliteSessionRepository(database))
            return await service.get_run(
                shutdown["session_id"], shutdown["run_id"]
            ), await service.list_steering(
                shutdown["session_id"], shutdown["run_id"]
            )
        finally:
            await database.close()

    run, steering = asyncio.run(inspect())
    assert run.status in {"cancelled", "failed", "completed"}
    assert run.finished_at is not None
    assert all(item.status != "pending" for item in steering)
    assert not active.locked()
    evidence["shutdown_finalization"] = {
        "status": run.status,
        "finished_at": run.finished_at,
        "steering": [item.status for item in steering],
    }


def check_immediate_disconnect(http, evidence):
    session_id = str(uuid4())
    create_session(http, session_id, "立即断连")
    with http.stream(
        "POST",
        "/api/agent/run",
        json={
            "session_id": session_id,
            "operation_id": str(uuid4()),
            "request": BUSY_REQUEST,
        },
    ) as response:
        assert response.status_code == 200
        run_id = response.headers["X-Run-ID"]
        # 不读取正文，立即关闭连接：已受理运行必须仍能收尾，主事件循环不得死锁。
    wait_idle()
    run = http.get(f"/api/sessions/{session_id}/runs/{run_id}").json()
    assert run["status"] in {"cancelled", "failed"}
    assert run["finished_at"] is not None
    assert not active.locked()
    evidence["immediate_disconnect"] = {"status": run["status"]}


def check_terminal_commit_failure(path, http, evidence):
    session_id = str(uuid4())
    create_session(http, session_id, "终态提交失败")
    raw = sqlite3.connect(path, isolation_level=None)
    raw.execute(
        "CREATE TABLE review_deferred (session_id TEXT NOT NULL, run_id TEXT NOT NULL, "
        "FOREIGN KEY (session_id, run_id) REFERENCES session_runs(session_id, id) "
        "DEFERRABLE INITIALLY DEFERRED) STRICT"
    )
    raw.execute(
        "CREATE TRIGGER review_commit_fail AFTER UPDATE ON session_runs "
        "WHEN NEW.status <> 'running' "
        "BEGIN INSERT INTO review_deferred (session_id, run_id) "
        "VALUES ('missing-session', 'missing-run'); END"
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
        # 终态提交失败：不发布 done / error，查询保持真实状态，占用不释放。
        assert collected
        assert all(event["event"] not in {"done", "error"} for event in collected)
        assert active.locked()
        run = http.get(f"/api/sessions/{session_id}/runs/{run_id}").json()
        assert run["status"] == "running" and run["finished_at"] is None
        evidence["terminal_commit_failure"] = {
            "events": len(collected),
            "status": run["status"],
        }
    finally:
        raw.execute("DROP TRIGGER review_commit_fail")
        raw.execute("DROP TABLE review_deferred")
        raw.commit()
        raw.close()
    service = app.state.session_service
    service_call(service.finish_run(session_id, run_id, "failed"))
    retire(runs[UUID(run_id)])


def check() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = patch_default_database("http-regression")
    seed = seed_legacy(path)
    evidence = {}
    server = Server(app)
    with server, client(server.base_url) as http:
        check_restart_recovery(server, http, seed, evidence)
        check_duplicate_and_queries(server, http, evidence)
        check_unknown_result_query_during_run(server, http, evidence)
        check_concurrent_acceptance(server, http, evidence)
        check_steering_races(server, http, evidence)
        check_steering_discard_on_cancel(server, http, evidence)
        check_commit_before_notify(server, http, evidence)
        check_credential_interception(server, http, evidence)
        check_immediate_disconnect(http, evidence)
        check_terminal_commit_failure(path, http, evidence)
        assert not active.locked()
    check_shutdown_finalization(path, evidence)
    (EVIDENCE / "http-regression.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "PASS: 重复请求、并发受理、运行中查询、Steering 竞争、凭据拦截、重启恢复、"
        "立即断连收尾、终态提交失败不发布确认与关闭收尾"
    )


if __name__ == "__main__":
    check()
