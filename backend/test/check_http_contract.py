import asyncio
import json
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Thread
from uuid import UUID, uuid4

import httpx
import uvicorn

from app.agent.agent_loop import run_agent_loop
from app.agent.config import AgentLoopConfig
from app.agent.tools.files import WORKSPACE
from app.ai.messages import AssistantMessage, UserMessage
from app.interfaces.http import active, app, runs
from app.model_config import load_model_config
from test.check_http import (
    create_session,
    events,
    final_text,
    last_run,
    stored_messages,
    validate_events,
    wait_idle,
)
from test.regression_support import patch_default_database

EVIDENCE = Path(__file__).resolve().parents[1] / "temp" / "http-contract"


def check_turn_limit() -> None:
    trace = []

    async def emit(event):
        trace.append(event)

    async def steering():
        return [UserMessage(role="user", content="追加输入", timestamp=0)]

    config = AgentLoopConfig(
        model=load_model_config(),
        max_turns=1,
        get_steering_messages=steering,
        get_steering_messages_or_close=steering,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        failure = executor.submit(asyncio.run, run_agent_loop(
            [UserMessage(role="user", content="只回复 FIRST。", timestamp=0)],
            {"messages": [], "tools": {}}, config, emit,
        )).exception()
    assert isinstance(failure, RuntimeError) and "模型调用上限" in str(failure)
    assert trace[-1]["type"] == "trace_end" and trace[-1]["status"] == "failed"


def check() -> None:
    check_turn_limit()
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    patch_default_database("http-contract")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
    thread = Thread(target=server.run, kwargs={"sockets": [listener]})
    thread.start()
    evidence = {}
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with httpx.Client(
            base_url=f"http://127.0.0.1:{listener.getsockname()[1]}",
            headers={"Host": "127.0.0.1:8000", "Origin": "http://localhost:5173"},
            timeout=600, trust_env=False,
        ) as client:
            session = str(uuid4())
            create_session(client, session)
            token1, token2 = uuid4().hex, uuid4().hex
            with client.stream("POST", "/api/agent/run", json={
                "session_id": session,
                "operation_id": str(uuid4()),
                "request": "只回复 FIRST，无需使用工具。",
            }) as response:
                run_id = response.headers["X-Run-ID"]
                UUID(run_id)
                endpoint = f"/api/agent/runs/{run_id}/steering"
                assert client.post(endpoint, json={"session_id": str(uuid4()), "operation_id": str(uuid4()), "message": "x"}).status_code == 409
                for invalid in ({}, {"session_id": session, "message": " "}, {"session_id": session, "message": 2}, {"session_id": session, "message": "x" * 32001}, {"session_id": session, "message": "x", "extra": True}):
                    rejected = client.post(endpoint, json=invalid)
                    assert rejected.status_code == 422
                    assert rejected.json() == {"detail": {"code": "invalid_request", "message": "请求字段不合法。"}}
                accepted = []
                steering_ops = []
                for text in (f"记住标记 {token1}，无需工具。", f"只回复两个标记 {token1} 和 {token2}，无需工具。"):
                    operation_id = str(uuid4())
                    reply = client.post(endpoint, json={
                        "session_id": session,
                        "operation_id": operation_id,
                        "message": text,
                    })
                    assert reply.status_code == 200, reply.text
                    body = reply.json()
                    assert body["run_id"] == run_id and body["status"] == "accepted"
                    UUID(body["steering_id"])
                    accepted.append(body["steering_id"])
                    steering_ops.append((operation_id, body["steering_id"]))
                received = list(events(response))
            wait_idle()
            for operation_id, steering_id in steering_ops:
                queried = client.get(f"/api/sessions/{session}/operations/{operation_id}")
                assert queried.status_code == 200
                body = queried.json()
                assert body["accepted"] is True and body["kind"] == "steering"
                assert body["run"]["run_id"] == run_id
                assert body["steering"]["steering_id"] == steering_id
                assert body["steering"]["status"] == "consumed"
                assert body["steering"]["entry_id"] is not None
            assert client.get(f"/api/sessions/{session}/operations/{uuid4()}").json()["accepted"] is False
            validate_events(received)
            assert all(item["data"]["run_id"] == run_id for item in received)
            statuses = [item["data"] for item in received if item["event"] == "steering_status"]
            assert [item["steering_id"] for item in statuses] == accepted
            assert all(item["status"] == "consumed" for item in statuses)
            assert sum(item["event"] == "message_end" for item in received) >= 2
            assert token1 in final_text(received) and token2 in final_text(received)
            history = stored_messages(session)
            user_content = [message.content for message in history if message.role == "user"]
            assert len(user_content) == 3 and token1 in user_content[1] and token2 in user_content[2]
            assert UUID(run_id) not in runs and last_run(session).status == "completed"
            assert client.post(endpoint, json={"session_id": session, "operation_id": str(uuid4()), "message": "late"}).status_code == 409
            assert client.post(f"/api/agent/runs/{uuid4()}/steering", json={"session_id": session, "operation_id": str(uuid4()), "message": "x"}).status_code == 404
            assert client.post("/api/agent/runs/invalid/steering", json={"session_id": session, "message": "x"}).status_code == 422
            evidence["steering"] = received

            tool_session = str(uuid4())
            create_session(client, tool_session)
            with client.stream("POST", "/api/agent/run", json={
                "session_id": tool_session,
                "operation_id": str(uuid4()),
                "request": "必须调用 bash，command 精确为 sleep 0.3，timeout 为 10；完成后报告。",
            }) as response:
                tool_run = response.headers["X-Run-ID"]
                tool_received = []
                tool_steering = None
                for item in events(response):
                    tool_received.append(item)
                    if item["event"] == "tool_start" and tool_steering is None:
                        reply = client.post(f"/api/agent/runs/{tool_run}/steering", json={"session_id": tool_session, "operation_id": str(uuid4()), "message": "只回复 STEERED。"})
                        assert reply.status_code == 200
                        tool_steering = reply.json()["steering_id"]
            wait_idle()
            validate_events(tool_received)
            consumed_index = next(index for index, item in enumerate(tool_received) if item["event"] == "steering_status" and item["data"]["steering_id"] == tool_steering)
            assert any(item["event"] == "tool_result" for item in tool_received[:consumed_index])
            assert tool_received[consumed_index]["data"]["status"] == "consumed"
            assert "STEERED" in final_text(tool_received)
            evidence["tool_steering"] = tool_received

            failure_session = str(uuid4())
            create_session(client, failure_session)
            with client.stream("POST", "/api/agent/run", json={
                "session_id": failure_session,
                "operation_id": str(uuid4()),
                "request": "必须直接调用 bash，command 精确为 sleep 2，timeout 为 0.2；不得提前回答，不得使用其他工具。",
            }) as response:
                failed_run = response.headers["X-Run-ID"]
                failure = []
                failure_steering = None
                for item in events(response):
                    failure.append(item)
                    if item["event"] == "tool_start":
                        reply = client.post(f"/api/agent/runs/{failed_run}/steering", json={"session_id": failure_session, "operation_id": str(uuid4()), "message": "只回复 LATE"})
                        assert reply.status_code == 200
                        failure_steering = reply.json()["steering_id"]
            wait_idle()
            validate_events(failure)
            assert failure_steering is not None and failure[-1]["event"] == "done"
            assert any(
                item["event"] == "tool_result"
                and item["data"]["is_error"] is True
                and "Command timed out after 0.2 seconds" in item["data"]["content"]
                for item in failure
            )
            assert any(
                item["event"] == "steering_status"
                and item["data"]["steering_id"] == failure_steering
                and item["data"]["status"] == "consumed"
                for item in failure
            )
            assert last_run(failure_session).status == "completed" and UUID(failed_run) not in runs
            evidence["tool_timeout"] = failure

            business_session = str(uuid4())
            create_session(client, business_session)
            with client.stream("POST", "/api/agent/run", json={
                "session_id": business_session,
                "operation_id": str(uuid4()),
                "request": "必须调用 bash，command 精确为 exit 7，然后说明真实结果。",
            }) as response:
                business = list(events(response))
            wait_idle()
            validate_events(business)
            assert business[-1]["event"] == "done"
            assert any(item["event"] == "tool_result" and item["data"]["is_error"] is True and "7" in item["data"]["content"] for item in business)
            evidence["tool_error"] = business

            cancelled_session = str(uuid4())
            create_session(client, cancelled_session)
            with client.stream("POST", "/api/agent/run", json={
                "session_id": cancelled_session,
                "operation_id": str(uuid4()),
                "request": "必须调用 bash，command 精确为 sleep 2，timeout 为 10；完成后报告。",
            }) as response:
                cancelled_run = response.headers["X-Run-ID"]
                disconnected = []
                for item in events(response):
                    disconnected.append(item)
                    if item["event"] == "tool_start":
                        reply = client.post(f"/api/agent/runs/{cancelled_run}/steering", json={"session_id": cancelled_session, "operation_id": str(uuid4()), "message": "只回复 LATE"})
                        assert reply.status_code == 200
                        assert active.locked()
                        break
            time.sleep(0.1)
            assert active.locked()
            assert client.post("/api/agent/run", json={"session_id": str(uuid4()), "operation_id": str(uuid4()), "request": "OK"}).status_code == 409
            wait_idle()
            assert last_run(cancelled_session).status == "cancelled" and UUID(cancelled_run) not in runs
            assert client.post(f"/api/agent/runs/{cancelled_run}/steering", json={"session_id": cancelled_session, "operation_id": str(uuid4()), "message": "late"}).status_code == 409
            evidence["disconnect"] = disconnected

            aborted_session = str(uuid4())
            create_session(client, aborted_session)
            with client.stream("POST", "/api/agent/run", json={
                "session_id": aborted_session,
                "operation_id": str(uuid4()),
                "request": "逐行输出从 1 到 100000 的整数，不要省略。无需使用工具。",
            }) as response:
                aborted_run = response.headers["X-Run-ID"]
                aborted = []
                stopped = False
                for item in events(response):
                    aborted.append(item)
                    if item["event"] == "message_update" and not stopped:
                        reply = client.post(f"/api/agent/runs/{aborted_run}/steering", json={"session_id": aborted_session, "operation_id": str(uuid4()), "message": "只回复 LATE"})
                        assert reply.status_code == 200
                        runs[UUID(aborted_run)].disconnect()
                        stopped = True
            wait_idle()
            validate_events(aborted)
            assert stopped and aborted[-1]["event"] == "error"
            assert aborted[-1]["data"]["status"] == "cancelled"
            assert any(item["event"] == "message_end" and item["data"]["stop_reason"] == "aborted" for item in aborted)
            assert aborted[-2]["event"] == "steering_status" and aborted[-2]["data"]["status"] == "discarded"
            assert last_run(aborted_session).status == "cancelled"
            evidence["aborted"] = aborted

            length_session = str(uuid4())
            create_session(client, length_session)
            large_text = "".join(uuid4().hex for _ in range(970))
            length_path = f"length-{uuid4().hex}.txt"
            with client.stream("POST", "/api/agent/run", json={
                "session_id": length_session,
                "operation_id": str(uuid4()),
                "request": f"仅调用 write，在 {length_path} 写入以下完整原文。直接将原文作为 content 参数，不要计算、改写、解释或省略：{large_text}",
            }) as response:
                limited = list(events(response))
            wait_idle()
            validate_events(limited)
            assert limited[-1]["event"] == "done" and limited[-1]["data"]["stop_reason"] == "length"
            assert any(item["event"] == "message_end" and item["data"]["stop_reason"] == "length" for item in limited)
            assert stored_messages(length_session)
            last_assistant = next(message for message in reversed(stored_messages(length_session)) if isinstance(message, AssistantMessage))
            assert last_assistant.usage is not None and last_assistant.usage.output > 0
            assert not any(item["event"] in {"tool_start", "tool_result"} for item in limited)
            assert not (WORKSPACE / length_path).exists()
            evidence["length"] = limited

            for _ in range(2):
                session_id = str(uuid4())
                create_session(client, session_id)
                with client.stream("POST", "/api/agent/run", json={"session_id": session_id, "operation_id": str(uuid4()), "request": "只回复 OK"}) as response:
                    pass
                wait_idle()
                assert last_run(session_id).status in {"cancelled", "failed"}
            assert not runs
            key = load_model_config().OPENAI_API_KEY
            encoded = json.dumps(evidence, ensure_ascii=False)
            assert key not in encoded
            assert all(term not in encoded for term in ("thinking_signature", "thought_signature", "text_signature", "Traceback", "Authorization"))
            (EVIDENCE / "real-events.json").write_text(encoded, encoding="utf-8")
            print("PASS: real HTTP identity, FIFO steering, single consumption, rejection, tool timeout is_error, true is_error, stop/length/aborted, disconnect, resource cleanup; 100 atomic end races")
    finally:
        server.should_exit = True
        thread.join(75)
        listener.close()
    assert not thread.is_alive()


if __name__ == "__main__":
    check()
