import asyncio
import json
import socket
import time
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from uuid import UUID, uuid4

import httpx
import uvicorn
from openai._streaming import SSEDecoder
from pydantic import TypeAdapter

from src.agent.tools.files import WORKSPACE
from src.interfaces.http import ALLOWED_HOSTS, RunRequest, active, app, sessions
from src.model_config import load_model_config

EVIDENCE = Path(__file__).resolve().parents[2] / ".pi/delivery/react-chat/t2"


def wait_idle() -> None:
    deadline = time.monotonic() + 75
    while active.locked() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not active.locked(), "工作线程未释放运行占用"


def events(response):
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    for event in SSEDecoder().iter_bytes(response.iter_bytes()):
        yield {"event": event.event, "data": TypeAdapter(dict).validate_json(event.data)}


async def check_duplicate_host() -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000") as client:
        response = await client.post(
            "/api/agent/run",
            json={"session_id": str(uuid4()), "request": "请回答完成"},
            headers=[("Host", "localhost:8000"), ("Host", "evil.example")],
        )
        assert response.status_code == 403


def check() -> None:
    asyncio.run(check_duplicate_host())
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", log_level="warning", access_log=False))
    thread = Thread(target=server.run, kwargs={"sockets": [listener]})
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with httpx.Client(
            base_url=f"http://127.0.0.1:{listener.getsockname()[1]}",
            headers={"Host": "127.0.0.1:8000", "Origin": "http://localhost:5173"},
            timeout=180,
            trust_env=False,
        ) as client, TemporaryDirectory(dir=WORKSPACE) as directory:
            root = Path(directory)
            session = str(uuid4())
            token = uuid4().hex
            path = f"{root.name}/note.txt"
            payload = {"session_id": session, "request": "请回答完成"}
            before = deepcopy(sessions)
            for host in ("evil.example", "127.0.0.1:8000.evil.example", "localhost:9000"):
                assert client.post("/api/agent/run", json=payload, headers={"Host": host}).status_code == 403
            for origin in ("null", "https://evil.example", "http://localhost:5173.evil.example"):
                assert client.post("/api/agent/run", json=payload, headers={"Origin": origin}).status_code == 403
            assert client.post("/api/agent/run", json=payload, headers=[("Origin", "http://localhost:5173"), ("Origin", "http://evil.example")]).status_code == 403
            for host in ALLOWED_HOSTS:
                assert client.post("/api/agent/run", json={}, headers={"Host": host, "Origin": f"http://{host}"}).status_code == 422
            for invalid in (
                {}, {"session_id": "bad", "request": "x"},
                {"session_id": session, "request": ""},
                {"session_id": session, "request": " \n\t"},
                {"session_id": session, "request": "x" * 32001},
                {"session_id": session, "request": 123},
                {**payload, "extra": True},
            ):
                assert client.post("/api/agent/run", json=invalid).status_code == 422
            assert sessions == before and not active.locked()
            assert len(RunRequest.model_validate({"session_id": session, "request": "x" * 32000}).request) == 32000

            def turn(prompt, identifier=session):
                with client.stream("POST", "/api/agent/run", json={"session_id": identifier, "request": prompt}) as response:
                    result = list(events(response))
                assert result[-1] == {"event": "done", "data": {"status": "completed"}}
                assert result[-2]["event"] == "message" and result[-2]["data"]["text"]
                starts = {}
                results = {}
                for event in result[:-2]:
                    data = event["data"]
                    call = data["tool_call_id"]
                    if event["event"] == "tool_start":
                        assert call not in starts and isinstance(data["arguments"], dict)
                        starts[call] = data
                    else:
                        assert event["event"] == "tool_result" and call in starts and call not in results
                        results[call] = data["content"]
                assert starts.keys() == results.keys()
                wait_idle()
                return result, starts, results

            first, starts, results = turn(
                f"只操作 {root.name}/ 下文件。使用 write 在 {path} 写入精确文本 {token}，"
                "然后使用 read 读取同一文件，最后说明内容。禁止添加空白或换行。"
            )
            assert {data["name"] for data in starts.values()} >= {"write", "read"}
            assert (root / "note.txt").read_text(encoding="utf-8") == token
            history = sessions[UUID(session)]
            assert history[0]["role"] == "system" and history[1]["role"] == "user"
            for call, content in results.items():
                assert any(message.get("tool_call_id") == call and message.get("content") == content for message in history)
            count = len(history)
            second, starts, _ = turn("使用 read 读取上一轮上下文中的同一文件。回答上一轮原文本与本次真实读取的文本。")
            assert "read" in {data["name"] for data in starts.values()}
            assert token in second[-2]["data"]["text"]
            assert len(sessions[UUID(session)]) > count
            other = str(uuid4())
            turn("只回答完成，无需使用工具。", other)
            assert token not in json.dumps(sessions[UUID(other)])

            before = deepcopy(sessions)
            with client.stream("POST", "/api/agent/run", json={"session_id": session, "request": f"必须直接使用 read 读取 {root.name}/missing.txt，无需其他工具。"}) as response:
                failure = list(events(response))
            assert failure[-1]["event"] == "error"
            assert failure[-1]["data"]["tool_call_id"] == failure[-2]["data"]["tool_call_id"]
            assert failure[-2]["event"] == "tool_start"
            assert all(event["event"] != "done" for event in failure)
            wait_idle()
            assert sessions == before
            error_text = json.dumps(failure[-1], ensure_ascii=False)
            assert load_model_config().OPENAI_API_KEY not in error_text
            assert str(WORKSPACE.resolve()) not in error_text and "missing.txt" not in error_text

            cancelled = str(uuid4())
            cancelled_path = f"{root.name}/disconnect.txt"
            with client.stream("POST", "/api/agent/run", json={"session_id": cancelled, "request": f"全部文件操作仅允许在 {root.name}/ 内；如需 ls 仅可列出该目录。使用 write 在 {cancelled_path} 写入精确文本 {token}，然后使用 read 读取该文件，最后说明内容。"}) as response:
                assert response.status_code == 200
                assert active.locked()
                assert client.post("/api/agent/run", json=payload).status_code == 409
                assert client.post("/api/agent/run", json=payload, headers={"Origin": "http://evil.example"}).status_code == 403
                disconnected = []
                write_id = None
                for event in events(response):
                    disconnected.append(event)
                    if event["event"] == "tool_start" and event["data"]["name"] == "write" and event["data"]["arguments"]["path"] == cancelled_path:
                        write_id = event["data"]["tool_call_id"]
                    if event["event"] == "tool_result" and event["data"]["tool_call_id"] == write_id:
                        break
                (EVIDENCE / "disconnect-diagnostic.json").write_text(
                    json.dumps({
                        "events": disconnected,
                        "active": active.locked(),
                        "file_exists": (root / "disconnect.txt").exists(),
                        "file_content": (root / "disconnect.txt").read_text(encoding="utf-8") if (root / "disconnect.txt").exists() else None,
                        "context_committed": UUID(cancelled) in sessions,
                    }, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                assert write_id is not None
                assert disconnected[-1]["event"] == "tool_result"
                assert disconnected[-1]["data"]["tool_call_id"] == write_id
            assert active.locked()
            assert client.post("/api/agent/run", json=payload).status_code == 409
            wait_idle()
            assert sessions == before
            assert (root / "disconnect.txt").read_text(encoding="utf-8") == token
            recovered, _, _ = turn("只回答完成，无需使用工具。", cancelled)
            assert len(sessions[UUID(cancelled)]) == 3
            early = str(uuid4())
            with client.stream("POST", "/api/agent/run", json={"session_id": early, "request": f"全部文件操作仅允许在 {root.name}/ 内。使用 write 在 {root.name}/early.txt 写入 completed。"}) as response:
                assert response.status_code == 200
            wait_idle()
            assert UUID(early) not in sessions
            assert not (root / "early.txt").exists()
            EVIDENCE.mkdir(parents=True, exist_ok=True)
            (EVIDENCE / "real-http-events.json").write_text(
                json.dumps({"first": first, "second": second, "failure": failure, "disconnect": disconnected, "recovered": recovered}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print("Real HTTP/SSE, full multi-turn context, success-only commit, global run exclusion, disconnect cancellation, retained file edits, security boundaries, redacted errors and recovery passed")
    finally:
        server.should_exit = True
        thread.join(75)
        listener.close()
    assert not thread.is_alive()


if __name__ == "__main__":
    check()
