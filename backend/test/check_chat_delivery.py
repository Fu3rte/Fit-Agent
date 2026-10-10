import json
import socket
import time
from pathlib import Path
from threading import Thread
from uuid import UUID, uuid4

import httpx
import uvicorn
from pydantic import TypeAdapter

from app.domain.session.attachments import TMP_ROOT
from app.interfaces.http import app
from test.check_http import (
    create_session,
    events,
    final_text,
    validate_events,
    wait_idle,
)
from test.regression_support import install_test_model_config, patch_default_database

EVIDENCE = Path(__file__).resolve().parents[1] / "temp" / "chat-delivery"
ROOT = Path(__file__).resolve().parents[2]


def check() -> None:
    install_test_model_config()
    patch_default_database("chat-delivery")
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    package = TypeAdapter(dict).validate_json((ROOT / "package.json").read_text(encoding="utf-8"))
    backend_command = "cd backend && uv run python -m uvicorn app.interfaces.http:app --reload --host 127.0.0.1 --port 8000"
    assert package["scripts"]["dev:backend"] == backend_command
    assert package["scripts"]["dev"] == "node scripts/dev.mjs"
    launcher = (ROOT / "scripts" / "dev.mjs").read_text(encoding="utf-8")
    assert '"npm run dev:backend"' in launcher
    assert '"npm run dev:frontend"' in launcher
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for command in (backend_command.removeprefix("cd backend && "), "npm run dev"):
        assert command in readme

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
            timeout=180, trust_env=False,
        ) as client:
            session = str(uuid4())
            create_session(client, session)
            prefix = f"sessions/{session}/workspace"
            workspace = TMP_ROOT / "sessions" / session / "workspace"
            path = f"{prefix}/delivery.txt"
            token = uuid4().hex

            def turn(prompt):
                with client.stream("POST", "/api/agent/run", json={"session_id": session, "operation_id": str(uuid4()), "request": prompt}) as response:
                    assert response.status_code == 200, response.text
                    assert response.headers["content-type"].startswith("text/event-stream")
                    assert response.headers["X-Session-ID"] == session
                    UUID(response.headers["X-Run-ID"])
                    UUID(response.headers["X-Request-Entry-ID"])
                    result = list(events(response))
                wait_idle()
                diagnostic = EVIDENCE / f"turn-{session}-{uuid4().hex}.json"
                diagnostic.write_text(
                    json.dumps({
                        "events": result,
                        "session_workspace": str(workspace),
                        "resolved_paths": [
                            {
                                "tool_call_id": event["data"]["tool_call_id"],
                                "path": event["data"]["arguments"]["path"],
                                "resolved": str((TMP_ROOT / Path(event["data"]["arguments"]["path"])).resolve()),
                            }
                            for event in result if event["event"] == "tool_start"
                        ],
                        "files": {
                            item.relative_to(workspace).as_posix(): item.read_text(encoding="utf-8")
                            for item in workspace.rglob("*") if item.is_file()
                        } if workspace.exists() else {},
                    }, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                assert result[-1]["event"] == "done" and final_text(result)
                starts, results = validate_events(result)
                for data in starts.values():
                    relative = Path(data["arguments"]["path"])
                    assert not relative.anchor and ".." not in relative.parts, diagnostic.name
                    assert (TMP_ROOT / relative).resolve().is_relative_to(workspace.resolve()), diagnostic.name
                return result, starts, results

            first, starts, results = turn(
                "会话 workspace 已存在。仅允许 write 和 read 两种工具，禁止调用其他工具。"
                f"必须直接调用 write 将 {path} 内容精确写为 {token}，禁止附加空白或换行。"
                "随后必须调用 read 读取该文件，最后回答文件内容。"
            )
            assert (workspace / "delivery.txt").read_text(encoding="utf-8") == token
            assert {item["name"] for item in starts.values()} >= {"write", "read"}
            assert any(results[call] == f"1: {token}\n" for call, item in starts.items() if item["name"] == "read")
            assert token in final_text(first)
            second, starts, results = turn(
                "沿用上一轮上下文中的文件路径与原文本。全部文件工具的 path 限定于当前会话 workspace 内。"
                "必须调用 edit 将原文本精确替换为原文本拼接 -updated，禁止附加空白或换行；"
                "随后分别调用 read 读取文件、ls 列出父目录、find 在父目录查找 **/*.txt、"
                "grep 在父目录搜索 updated。五个工具全部实际调用，最后回答原文本与新文本。"
            )
            updated = token + "-updated"
            actual = (workspace / "delivery.txt").read_text(encoding="utf-8")
            (EVIDENCE / "integrated-events.json").write_text(
                json.dumps({"first": first, "second": second, "file_content": actual}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            assert actual == updated
            assert {item["name"] for item in starts.values()} >= {"edit", "read", "ls", "find", "grep"}
            expected = {
                "edit": f"已编辑 {path}", "read": f"1: {updated}\n", "ls": "delivery.txt",
                "find": path, "grep": f"{path}:1: {updated}",
            }
            for name, content in expected.items():
                assert any(results[call] == content for call, item in starts.items() if item["name"] == name), name
            assert token in final_text(second) and updated in final_text(second)
            print("PASS: dev entry and README commands; real HTTP/SSE multi-turn memory; six paired file tools with exact disk-backed results")
    finally:
        server.should_exit = True
        thread.join(75)
        listener.close()
    assert not thread.is_alive()


if __name__ == "__main__":
    check()
