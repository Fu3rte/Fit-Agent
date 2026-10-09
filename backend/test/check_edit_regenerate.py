import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

from app.interfaces.http import active, app
from test.check_http import (
    create_session,
    events,
    validate_events,
    wait_idle,
)
from test.regression_support import (
    Server,
    client,
    patch_default_database,
    temporary_root,
)

EVIDENCE = temporary_root("edit-regenerate")
PLAIN_PROMPT = "只回答 OK，无需使用工具。"
# 真实模型长输出流保持运行中窗口，用于忙碌冲突与取消收尾断言。
BUSY_PROMPT = "逐行输出从 1 到 100000 的整数，不要省略。无需使用工具。"
EXPIRED_DETAIL = {
    "code": "operation_conflict",
    "reason": "operation_expired",
    "message": "该操作已失效。",
}


def history(http, session_id: str) -> dict:
    response = http.get(f"/api/sessions/{session_id}/history")
    assert response.status_code == 200, response.text
    return response.json()


def entry_ids(body: dict) -> list[str]:
    return [entry["entry_id"] for entry in body["entries"]]


def assistant_entries(body: dict) -> list[dict]:
    return [
        entry for entry in body["entries"] if entry["message"]["role"] == "assistant"
    ]


def run_ids(body: dict) -> list[str]:
    return [run["run_id"] for run in body["runs"]]


def stream_run(http, path: str, body: dict):
    with http.stream("POST", path, json=body) as response:
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/event-stream")
        result = list(events(response))
        return response.headers, result


def post_edit(base_url: str, body: dict) -> tuple:
    with client(base_url) as http:
        with http.stream("POST", "/api/agent/edit", json=body) as response:
            content_type = response.headers.get("content-type", "")
            if content_type.startswith("text/event-stream"):
                return response.status_code, "sse", response.headers.get("X-Run-ID")
            return response.status_code, "json", response.read().decode("utf-8")


def send(http, session_id: str, prompt: str) -> tuple[dict, str]:
    headers, result = stream_run(
        http,
        "/api/agent/run",
        {
            "session_id": session_id,
            "operation_id": str(uuid4()),
            "request": prompt,
        },
    )
    wait_idle()
    validate_events(result)
    assert result[-1]["event"] == "done"
    return headers, headers["X-Request-Entry-ID"]


def check_flow(http, evidence: dict) -> dict:
    session_id = str(uuid4())
    create_session(http, session_id, "编辑重生成")
    send_op = str(uuid4())
    headers, first = stream_run(
        http,
        "/api/agent/run",
        {"session_id": session_id, "operation_id": send_op, "request": PLAIN_PROMPT},
    )
    wait_idle()
    validate_events(first)
    assert first[-1]["event"] == "done"
    send_run = headers["X-Run-ID"]
    user1 = headers["X-Request-Entry-ID"]
    assert user1 in entry_ids(history(http, session_id))

    # 编辑：删除目标用户消息及其全部后续内容。
    edit_op = str(uuid4())
    edited_text = "只回答完成，无需使用工具。"
    headers, second = stream_run(
        http,
        "/api/agent/edit",
        {
            "session_id": session_id,
            "operation_id": edit_op,
            "target_entry_id": user1,
            "request": edited_text,
        },
    )
    wait_idle()
    validate_events(second)
    assert second[-1]["event"] == "done"
    edit_run = headers["X-Run-ID"]
    new_user = headers["X-Request-Entry-ID"]
    assert new_user != user1
    body = history(http, session_id)
    ids = entry_ids(body)
    assert user1 not in ids and new_user in ids
    assert body["entries"][0]["message"]["role"] == "system"
    assert body["entries"][1]["entry_id"] == new_user
    assert send_run not in run_ids(body) and edit_run in run_ids(body)

    # 旧操作记录删除后编号失效；重试返回 operation_expired。
    expired = http.get(f"/api/sessions/{session_id}/operations/{send_op}")
    assert expired.status_code == 409, expired.text
    assert expired.json()["detail"] == EXPIRED_DETAIL

    # 同键同请求返回原运行 JSON，不重复调度。
    retry = http.post(
        "/api/agent/edit",
        json={
            "session_id": session_id,
            "operation_id": edit_op,
            "target_entry_id": user1,
            "request": edited_text,
        },
    )
    assert retry.status_code == 200
    assert retry.headers["content-type"].startswith("application/json")
    repeated = retry.json()
    assert repeated["operation_id"] == edit_op
    assert repeated["session_id"] == session_id
    assert repeated["run_id"] == edit_run
    assert repeated["request_entry_id"] == new_user
    assert repeated["status"] == "completed"
    UUID(repeated["run_id"])

    # 同键不同请求返回操作冲突，且不带 operation_expired。
    conflict = http.post(
        "/api/agent/edit",
        json={
            "session_id": session_id,
            "operation_id": edit_op,
            "target_entry_id": user1,
            "request": "不同文本",
        },
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"] == {
        "code": "operation_conflict",
        "message": "操作请求冲突。",
    }

    # 操作查询返回 kind=edit、运行对象与 steering=null。
    operation = http.get(
        f"/api/sessions/{session_id}/operations/{edit_op}"
    ).json()
    assert operation["accepted"] is True
    assert operation["kind"] == "edit"
    assert operation["run"]["run_id"] == edit_run
    assert operation["steering"] is None

    # 重新生成：保留目标及祖先，删除目标之后的全部内容。
    regen_op = str(uuid4())
    headers, third = stream_run(
        http,
        "/api/agent/regenerate",
        {
            "session_id": session_id,
            "operation_id": regen_op,
            "target_entry_id": new_user,
        },
    )
    wait_idle()
    validate_events(third)
    assert third[-1]["event"] == "done"
    regen_run = headers["X-Run-ID"]
    assert headers["X-Request-Entry-ID"] == new_user
    body = history(http, session_id)
    assert [
        entry["entry_id"]
        for entry in body["entries"]
        if entry["message"]["role"] == "user"
    ] == [new_user]
    assert all(entry["run_id"] == regen_run for entry in assistant_entries(body))
    operation = http.get(
        f"/api/sessions/{session_id}/operations/{regen_op}"
    ).json()
    assert operation["accepted"] is True
    assert operation["kind"] == "regenerate"
    assert operation["steering"] is None
    assert operation["run"]["run_id"] == regen_run

    # 目标校验与请求校验。
    assistant_id = assistant_entries(body)[0]["entry_id"]
    invalid = http.post(
        "/api/agent/edit",
        json={
            "session_id": session_id,
            "operation_id": str(uuid4()),
            "target_entry_id": assistant_id,
            "request": "非法目标",
        },
    )
    assert invalid.status_code == 409, invalid.text
    assert invalid.json()["detail"]["code"] == "invalid_target_entry"
    missing = http.post(
        "/api/agent/regenerate",
        json={
            "session_id": session_id,
            "operation_id": str(uuid4()),
            "target_entry_id": str(uuid4()),
        },
    )
    assert missing.status_code == 404, missing.text
    assert missing.json()["detail"]["code"] == "entry_not_found"
    unknown_session = http.post(
        "/api/agent/edit",
        json={
            "session_id": str(uuid4()),
            "operation_id": str(uuid4()),
            "target_entry_id": new_user,
            "request": "无会话",
        },
    )
    assert unknown_session.status_code == 404
    assert unknown_session.json()["detail"]["code"] == "session_not_found"
    blank = http.post(
        "/api/agent/edit",
        json={
            "session_id": session_id,
            "operation_id": str(uuid4()),
            "target_entry_id": new_user,
            "request": "   ",
        },
    )
    assert blank.status_code == 422 and blank.json()["detail"]["code"] == "invalid_request"
    extra = http.post(
        "/api/agent/edit",
        json={
            "session_id": session_id,
            "operation_id": str(uuid4()),
            "target_entry_id": new_user,
            "request": "x",
            "unknown": 1,
        },
    )
    assert extra.status_code == 422 and extra.json()["detail"]["code"] == "invalid_request"

    evidence["flow"] = {
        "session_id": session_id,
        "send_op": send_op,
        "edit_op": edit_op,
        "regen_op": regen_op,
        "new_user": new_user,
    }
    return evidence["flow"]


def check_busy_and_cancel(http, evidence: dict) -> None:
    session_id = str(uuid4())
    create_session(http, session_id, "忙碌与取消")
    _, user1 = send(http, session_id, PLAIN_PROMPT)

    with http.stream(
        "POST",
        "/api/agent/edit",
        json={
            "session_id": session_id,
            "operation_id": str(uuid4()),
            "target_entry_id": user1,
            "request": BUSY_PROMPT,
        },
    ) as response:
        edit_run = response.headers["X-Run-ID"]
        new_user = response.headers["X-Request-Entry-ID"]
        for event in events(response):
            if event["event"] == "message_update":
                assert active.locked()
                busy = http.post(
                    "/api/agent/edit",
                    json={
                        "session_id": session_id,
                        "operation_id": str(uuid4()),
                        "target_entry_id": new_user,
                        "request": "忙碌时编辑",
                    },
                )
                assert busy.status_code == 409, busy.text
                assert busy.json()["detail"]["code"] == "run_busy"
                break
    wait_idle()
    run = http.get(f"/api/sessions/{session_id}/runs/{edit_run}").json()
    assert run["status"] in {"cancelled", "failed"}, run
    body = history(http, session_id)
    ids = entry_ids(body)
    assert user1 not in ids and new_user in ids
    evidence["busy_cancel"] = {"status": run["status"], "session_id": session_id}


def check_rollback(path, http, evidence: dict) -> None:
    session_id = str(uuid4())
    create_session(http, session_id, "受理回滚")
    _, user1 = send(http, session_id, PLAIN_PROMPT)
    send_ops = http.get(f"/api/sessions/{session_id}/history").json()
    assert user1 in entry_ids(send_ops)

    raw = sqlite3.connect(path, isolation_level=None)
    raw.execute(
        "CREATE TRIGGER review_block_invalidation "
        "BEFORE INSERT ON session_operation_invalidations "
        "BEGIN SELECT RAISE(ABORT, '注入失效写入失败'); END"
    )
    raw.commit()
    operation_id = str(uuid4())
    try:
        response = http.post(
            "/api/agent/edit",
            json={
                "session_id": session_id,
                "operation_id": operation_id,
                "target_entry_id": user1,
                "request": "回滚测试",
            },
        )
        assert response.status_code == 500, response.text
        assert response.json()["detail"]["code"] == "internal_error"
    finally:
        raw.execute("DROP TRIGGER review_block_invalidation")
        raw.commit()
        raw.close()

    body = history(http, session_id)
    assert user1 in entry_ids(body)
    assert len(run_ids(body)) == 1
    assert http.get(
        f"/api/sessions/{session_id}/operations/{operation_id}"
    ).json()["accepted"] is False
    assert not active.locked()
    evidence["rollback"] = {"entries": len(body["entries"])}


def check_concurrent(server, http, evidence: dict) -> None:
    session_id = str(uuid4())
    create_session(http, session_id, "并发编辑")
    send_run, user1 = send(http, session_id, PLAIN_PROMPT)
    operation_id = str(uuid4())
    payload = {
        "session_id": session_id,
        "operation_id": operation_id,
        "target_entry_id": user1,
        "request": PLAIN_PROMPT,
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(post_edit, server.base_url, payload) for _ in range(2)]
        outcomes = [future.result() for future in futures]
    streams = [outcome for outcome in outcomes if outcome[1] == "sse"]
    others = [outcome for outcome in outcomes if outcome[1] != "sse"]
    assert len(streams) == 1 and streams[0][0] == 200, outcomes
    winner = streams[0][2]
    UUID(winner)
    assert len(others) == 1
    status, kind, payload_text = others[0]
    assert status == 200 and kind == "json", outcomes
    assert json.loads(payload_text)["run_id"] == winner
    wait_idle()
    body = history(http, session_id)
    assert user1 not in entry_ids(body)
    assert run_ids(body) == [winner]
    operation = http.get(
        f"/api/sessions/{session_id}/operations/{operation_id}"
    ).json()
    assert operation["accepted"] is True and operation["run"]["run_id"] == winner
    assert send_run not in run_ids(body)
    evidence["concurrent"] = {
        "statuses": [(status, kind) for status, kind, _ in outcomes],
        "run_id": winner,
        "session_id": session_id,
    }


def check_restart(flow: dict, evidence: dict) -> None:
    server = Server(app)
    with server, client(server.base_url) as http:
        expired = http.get(
            f"/api/sessions/{flow['session_id']}/operations/{flow['send_op']}"
        )
        assert expired.status_code == 409, expired.text
        assert expired.json()["detail"] == EXPIRED_DETAIL
        body = http.get(f"/api/sessions/{flow['session_id']}/history").json()
        assert flow["new_user"] in entry_ids(body)
        assert all(run["status"] != "running" for run in body["runs"])
        evidence["restart"] = {
            "expired": expired.json(),
            "entries": len(body["entries"]),
        }


def check() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = patch_default_database("edit-regenerate")
    evidence = {}
    server = Server(app)
    with server, client(server.base_url) as http:
        flow = check_flow(http, evidence)
        check_busy_and_cancel(http, evidence)
        check_rollback(path, http, evidence)
        check_concurrent(server, http, evidence)
        assert not active.locked()
    check_restart(flow, evidence)
    (EVIDENCE / "edit-regenerate.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "PASS: 编辑/重新生成删除语义、失效编号、幂等与冲突、操作查询、"
        "忙碌冲突、取消保持删除、受理事务回滚、并发同键与重启恢复"
    )


if __name__ == "__main__":
    check()
