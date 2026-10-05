import asyncio
import json
import socket
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from uuid import UUID, uuid4

import httpx
import uvicorn
from openai._streaming import SSEDecoder
from pydantic import TypeAdapter

from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tools.files import WORKSPACE
from app.agent.usage import summarize_usage
from app.ai.messages import (
    AssistantMessage,
    SystemMessage,
    ToolResultMessage,
    UserMessage,
    serialize_message,
)
from app.domain.session.errors import SessionNotFound
from app.interfaces.http import ALLOWED_HOSTS, RunRequest, active, app, runs
from app.model_config import load_model_config
from test.regression_support import patch_default_database

EVIDENCE = Path(__file__).resolve().parents[1] / "temp" / "http"


def stored_messages(session_id: str) -> list:
    service = app.state.session_service
    try:
        session = asyncio.run_coroutine_threadsafe(
            service.get_session(session_id), app.state.loop
        ).result()
    except SessionNotFound:
        return []
    if session is None:
        return []
    entries = asyncio.run_coroutine_threadsafe(
        service.get_current_branch(session_id), app.state.loop
    ).result()
    return [entry.messages[0] for entry in entries]


def create_session(client, session_id: str, title: str = "只回答完成") -> None:
    response = client.post(
        "/api/sessions", json={"session_id": session_id, "title": title}
    )
    assert response.status_code in {200, 201}, response.text
    UUID(response.json()["session_id"])


def last_run(session_id: str):
    service = app.state.session_service
    runs_list = asyncio.run_coroutine_threadsafe(
        service.list_runs(session_id), app.state.loop
    ).result()
    return runs_list[-1] if runs_list else None


def wait_idle() -> None:
    deadline = time.monotonic() + 75
    while active.locked() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not active.locked(), "工作线程未释放运行占用"


def events(response):
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    for event in SSEDecoder().iter_bytes(response.iter_bytes()):
        yield {
            "event": event.event,
            "data": TypeAdapter(dict).validate_json(event.data),
        }


def final_text(result) -> str:
    final = next(event for event in reversed(result) if event["event"] == "message_end")
    return "".join(block["text"] for block in final["data"]["content"] if block["type"] == "text")


def validate_events(result):
    assert result and result[-1]["event"] in {"done", "error"}
    run_id = result[0]["data"]["run_id"]
    UUID(run_id)
    messages = {}
    starts = {}
    results = {}
    for position, event in enumerate(result):
        kind, data = event["event"], event["data"]
        assert data["run_id"] == run_id
        if kind in {"done", "error"}:
            assert position == len(result) - 1
            continue
        if kind == "steering_status":
            assert data["status"] in {"consumed", "discarded"}
            continue
        if kind.startswith("message_"):
            identifier = data["message_id"]
            UUID(identifier)
            if kind == "message_start":
                assert identifier not in messages
                messages[identifier] = False
            else:
                assert identifier in messages and not messages[identifier]
            indices = [block["content_index"] for block in data["content"]]
            assert indices == sorted(set(indices))
            assert all(set(block) <= {
                "content_index", "type", "text", "thinking", "tool_call_id", "name", "arguments",
            } for block in data["content"])
            if kind == "message_update":
                assert data["update_type"] in {
                    "text_start", "text_delta", "text_end", "thinking_start", "thinking_delta",
                    "thinking_end", "toolcall_start", "toolcall_delta", "toolcall_end",
                }
                assert data["content_index"] in indices
            elif kind == "message_end":
                assert data["stop_reason"] in {"stop", "length", "toolUse", "error", "aborted"}
                assert data["entry_id"] == identifier
                UUID(data["parent_id"])
                messages[identifier] = True
            continue
        call = data["tool_call_id"]
        if kind == "tool_start":
            assert call not in starts and isinstance(data["arguments"], dict)
            assert any(item["event"] == "message_end" and item["data"]["stop_reason"] == "toolUse" for item in result[:position])
            starts[call] = data
        else:
            assert kind == "tool_result" and call in starts and call not in results
            assert type(data["is_error"]) is bool
            UUID(data["entry_id"])
            UUID(data["parent_id"])
            results[call] = data["content"]
    if result[-1]["event"] == "done":
        assert result[-1]["data"]["status"] == "completed"
        assert all(messages.values())
        assert starts.keys() == results.keys()
        last = next(item for item in reversed(result) if item["event"] == "message_end")
        assert result[-1]["data"]["stop_reason"] == last["data"]["stop_reason"] in {"stop", "length"}
    return starts, results


async def check_duplicate_host() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
    ) as client:
        response = await client.post(
            "/api/agent/run",
            json={
                "session_id": str(uuid4()),
                "operation_id": str(uuid4()),
                "request": "请回答完成",
            },
            headers=[("Host", "localhost:8000"), ("Host", "evil.example")],
        )
        assert response.status_code == 403


def check_credential_filter() -> None:
    from app.interfaces.http import CredentialFilter

    secret = "sk-" + "a" * 61
    guard = CredentialFilter((secret,))
    assert guard.contains(secret)
    assert guard.contains({"nested": [{"value": secret}]})
    assert guard.contains({secret: "value"})
    assert guard.contains(f"前缀 {secret} 后缀")
    assert not guard.contains("普通内容")
    assert guard.contains(secret[: len(secret) - 1]) is False
    for index in range(len(secret) + 1):
        prefix = secret[:index]
        assert secret not in guard.tail_safe(prefix)
        assert guard.contains(prefix) == (index == len(secret))


def check() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    patch_default_database("http")
    check_credential_filter()
    asyncio.run(check_duplicate_host())
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", log_level="warning", access_log=False)
    )
    thread = Thread(target=server.run, kwargs={"sockets": [listener]})
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with (
            httpx.Client(
                base_url=f"http://127.0.0.1:{listener.getsockname()[1]}",
                headers={"Host": "127.0.0.1:8000", "Origin": "http://localhost:5173"},
                timeout=180,
                trust_env=False,
            ) as client,
            TemporaryDirectory(dir=WORKSPACE) as directory,
        ):
            root = Path(directory)
            session = str(uuid4())
            create_session(client, session, "请回答完成")
            token = uuid4().hex
            path = f"{root.name}/note.txt"
            payload = {
                "session_id": session,
                "operation_id": str(uuid4()),
                "request": "请回答完成",
            }
            before = stored_messages(session)
            for host in (
                "evil.example",
                "127.0.0.1:8000.evil.example",
                "localhost:9000",
            ):
                assert (
                    client.post(
                        "/api/agent/run", json=payload, headers={"Host": host}
                    ).status_code
                    == 403
                )
            for origin in (
                "null",
                "https://evil.example",
                "http://localhost:5173.evil.example",
            ):
                assert (
                    client.post(
                        "/api/agent/run", json=payload, headers={"Origin": origin}
                    ).status_code
                    == 403
                )
            assert (
                client.post(
                    "/api/agent/run",
                    json=payload,
                    headers=[
                        ("Origin", "http://localhost:5173"),
                        ("Origin", "http://evil.example"),
                    ],
                ).status_code
                == 403
            )
            for host in ALLOWED_HOSTS:
                assert (
                    client.post(
                        "/api/agent/run",
                        json={},
                        headers={"Host": host, "Origin": f"http://{host}"},
                    ).status_code
                    == 422
                )
            for invalid in (
                {},
                {"session_id": "bad", "request": "x"},
                {"session_id": session, "request": ""},
                {"session_id": session, "request": " \n\t"},
                {"session_id": session, "request": "x" * 32001},
                {"session_id": session, "request": 123},
                {**payload, "extra": True},
            ):
                assert client.post("/api/agent/run", json=invalid).status_code == 422
            key = load_model_config().OPENAI_API_KEY
            blocked = client.post(
                "/api/agent/run",
                json={
                    "session_id": session,
                    "operation_id": str(uuid4()),
                    "request": f"记住这段凭据 {key}",
                },
            )
            assert blocked.status_code == 422
            assert blocked.json() == {
                "detail": {"code": "credential_detected", "message": "检测到受保护凭据，操作已拒绝"}
            }
            assert client.post(
                "/api/sessions", json={"session_id": str(uuid4()), "title": f"标题 {key}"}
            ).status_code == 422
            assert stored_messages(session) == before and not active.locked()
            assert (
                len(
                    RunRequest.model_validate(
                        {
                            "session_id": session,
                            "operation_id": str(uuid4()),
                            "request": "x" * 32000,
                        }
                    ).request
                )
                == 32000
            )

            def turn(prompt, identifier=session):
                operation_id = str(uuid4())
                with client.stream(
                    "POST",
                    "/api/agent/run",
                    json={
                        "session_id": identifier,
                        "operation_id": operation_id,
                        "request": prompt,
                    },
                ) as response:
                    assert response.status_code == 200, response.text
                    assert response.headers["content-type"].startswith(
                        "text/event-stream"
                    )
                    assert response.headers["X-Session-ID"] == identifier
                    assert response.headers["X-Operation-ID"] == operation_id
                    run_id = response.headers["X-Run-ID"]
                    UUID(run_id)
                    UUID(response.headers["X-Request-Entry-ID"])
                    result = list(events(response))
                assert result[-1]["event"] == "done" and final_text(result)
                starts, results = validate_events(result)
                wait_idle()
                return result, starts, results, operation_id, run_id

            received_after = time.time_ns() // 1_000_000
            first_prompt = (
                f"只操作 {root.name}/ 下文件。使用 write 在 {path} 写入精确文本 {token}，"
                "然后使用 read 读取同一文件，最后说明内容。禁止添加空白或换行。"
            )
            first, starts, results, first_op, first_run = turn(first_prompt)
            assert {data["name"] for data in starts.values()} >= {"write", "read"}
            assert (root / "note.txt").read_text(encoding="utf-8") == token
            history = stored_messages(session)
            assert history[0].role == "system" and history[1].role == "user"
            assert isinstance(history[0], SystemMessage) and isinstance(
                history[1], UserMessage
            )
            assert history[0].content == SYSTEM_PROMPT
            assert {tool.name for tool in history[0].tools_added} == {
                "read",
                "write",
                "edit",
                "ls",
                "find",
                "grep",
                "bash",
            }
            assert received_after <= history[1].timestamp <= time.time_ns() // 1_000_000
            assert history[0].timestamp == history[1].timestamp
            for call, content in results.items():
                assert any(
                    isinstance(message, ToolResultMessage)
                    and message.tool_call_id == call
                    and message.content[0].text == content
                    and message.is_error is False
                    for message in history
                )
            count = len(history)
            second, starts, _, _, _ = turn(
                "使用 read 读取上一轮上下文中的同一文件。回答上一轮原文本与本次真实读取的文本。"
            )
            assert "read" in {data["name"] for data in starts.values()}
            assert token in final_text(second)
            assert len(stored_messages(session)) > count

            repeated = client.post(
                "/api/agent/run",
                json={"session_id": session, "operation_id": first_op, "request": first_prompt},
            )
            assert repeated.status_code == 200
            assert repeated.headers["content-type"].startswith("application/json")
            repeat_body = repeated.json()
            assert repeat_body == {
                "operation_id": first_op,
                "session_id": session,
                "run_id": first_run,
                "request_entry_id": repeat_body["request_entry_id"],
                "status": "completed",
            }
            UUID(repeat_body["request_entry_id"])
            assert UUID(first_run) not in runs

            queried = client.get(f"/api/sessions/{session}/operations/{first_op}")
            assert queried.status_code == 200
            queried_body = queried.json()
            assert queried_body["accepted"] is True and queried_body["kind"] == "send"
            assert queried_body["run"]["run_id"] == first_run
            assert queried_body["run"]["status"] == "completed"
            assert queried_body["steering"] is None

            run_query = client.get(f"/api/sessions/{session}/runs/{first_run}")
            assert run_query.status_code == 200
            assert run_query.json() == queried_body["run"]

            unaccepted = client.get(f"/api/sessions/{session}/operations/{uuid4()}")
            assert unaccepted.status_code == 200
            assert unaccepted.json() == {
                "operation_id": unaccepted.json()["operation_id"],
                "session_id": session,
                "accepted": False,
                "kind": None,
                "run": None,
                "steering": None,
            }
            assert client.get(f"/api/sessions/{uuid4()}/operations/{uuid4()}").status_code == 404
            assert client.get(f"/api/sessions/{session}/runs/{uuid4()}").status_code == 404
            assert client.get(f"/api/sessions/{uuid4()}/runs/{uuid4()}").status_code == 404
            assert client.post("/api/sessions", json={"session_id": session, "title": "请回答完成"}).status_code == 200
            assert client.post("/api/sessions", json={"session_id": session, "title": "冲突标题"}).status_code == 409

            committed = stored_messages(session)
            snapshot = [serialize_message(message) for message in committed]
            totals = summarize_usage(committed)
            assert totals.total_tokens == sum(
                message.usage.total_tokens
                for message in committed
                if isinstance(message, AssistantMessage)
            )
            assert "cost" not in totals.model_dump(exclude_unset=True)
            assert [serialize_message(message) for message in committed] == snapshot
            other = str(uuid4())
            create_session(client, other)
            turn("只回答完成，无需使用工具。", other)
            assert token not in "".join(
                serialize_message(message) for message in stored_messages(other)
            )

            with client.stream(
                "POST",
                "/api/agent/run",
                json={
                    "session_id": session,
                    "operation_id": str(uuid4()),
                    "request": f"必须直接使用 read 读取 {root.name}/missing.txt，无需其他工具。",
                },
            ) as response:
                failure = list(events(response))
            assert failure[-1]["event"] == "error"
            assert (
                failure[-1]["data"]["tool_call_id"]
                == failure[-2]["data"]["tool_call_id"]
            )
            assert failure[-2]["event"] == "tool_start"
            assert all(event["event"] != "done" for event in failure)
            wait_idle()
            assert last_run(session).status == "failed"
            session_after_failure = stored_messages(session)
            error_text = json.dumps(failure[-1], ensure_ascii=False)
            assert load_model_config().OPENAI_API_KEY not in error_text
            assert (
                str(WORKSPACE.resolve()) not in error_text
                and "missing.txt" not in error_text
            )

            cancelled = str(uuid4())
            create_session(client, cancelled)
            cancelled_path = f"{root.name}/disconnect.txt"
            with client.stream(
                "POST",
                "/api/agent/run",
                json={
                    "session_id": cancelled,
                    "operation_id": str(uuid4()),
                    "request": f"全部文件操作仅允许在 {root.name}/ 内；如需 ls 仅可列出该目录。使用 write 在 {cancelled_path} 写入精确文本 {token}，然后使用 read 读取该文件，最后说明内容。",
                },
            ) as response:
                assert response.status_code == 200
                assert active.locked()
                assert client.post("/api/agent/run", json=payload).status_code == 409
                assert (
                    client.post(
                        "/api/agent/run",
                        json=payload,
                        headers={"Origin": "http://evil.example"},
                    ).status_code
                    == 403
                )
                disconnected = []
                write_id = None
                for event in events(response):
                    disconnected.append(event)
                    if (
                        event["event"] == "tool_start"
                        and event["data"]["name"] == "write"
                        and event["data"]["arguments"]["path"] == cancelled_path
                    ):
                        write_id = event["data"]["tool_call_id"]
                    if (
                        event["event"] == "tool_result"
                        and event["data"]["tool_call_id"] == write_id
                    ):
                        break
                (EVIDENCE / "disconnect-diagnostic.json").write_text(
                    json.dumps(
                        {
                            "events": disconnected,
                            "active": active.locked(),
                            "file_exists": (root / "disconnect.txt").exists(),
                            "file_content": (root / "disconnect.txt").read_text(
                                encoding="utf-8"
                            )
                            if (root / "disconnect.txt").exists()
                            else None,
                            "context_committed": bool(stored_messages(cancelled)),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                assert write_id is not None
                assert disconnected[-1]["event"] == "tool_result"
                assert disconnected[-1]["data"]["tool_call_id"] == write_id
            assert active.locked()
            assert client.post("/api/agent/run", json=payload).status_code == 409
            wait_idle()
            assert stored_messages(session) == session_after_failure
            assert (root / "disconnect.txt").read_text(encoding="utf-8") == token
            recovered, _, _, _, _ = turn("只回答完成，无需使用工具。", cancelled)
            assert len(stored_messages(cancelled)) >= 4
            early = str(uuid4())
            create_session(client, early)
            with client.stream(
                "POST",
                "/api/agent/run",
                json={
                    "session_id": early,
                    "operation_id": str(uuid4()),
                    "request": f"全部文件操作仅允许在 {root.name}/ 内。使用 write 在 {root.name}/early.txt 写入 completed。",
                },
            ) as response:
                assert response.status_code == 200
            wait_idle()
            assert last_run(early).status in {"cancelled", "failed"}
            assert not (root / "early.txt").exists()
            EVIDENCE.mkdir(parents=True, exist_ok=True)
            (EVIDENCE / "real-http-events.json").write_text(
                json.dumps(
                    {
                        "first": first,
                        "second": second,
                        "failure": failure,
                        "disconnect": disconnected,
                        "recovered": recovered,
                        "messages": [
                            json.loads(serialize_message(message))
                            for message in stored_messages(session)
                        ],
                        "usage": totals.model_dump(exclude_unset=True),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(
                "Real HTTP/SSE, full multi-turn context, success-only commit, global run exclusion, disconnect cancellation, retained file edits, security boundaries, redacted errors and recovery passed"
            )
    finally:
        server.should_exit = True
        thread.join(75)
        listener.close()
    assert not thread.is_alive()


if __name__ == "__main__":
    check()
