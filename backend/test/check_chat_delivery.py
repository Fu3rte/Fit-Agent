import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from uuid import uuid4

import httpx
import uvicorn
from pydantic import TypeAdapter

from test.check_http import events, wait_idle
from src.agent.tools.files import WORKSPACE
from src.interfaces.http import app

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / ".pi/delivery/react-chat/t5"


def check() -> None:
    package = TypeAdapter(dict).validate_json((ROOT / "package.json").read_text(encoding="utf-8"))
    backend_command = "cd backend && uv run python -m uvicorn src.interfaces.http:app --reload --host 127.0.0.1 --port 8000"
    assert package["scripts"]["dev:backend"] == backend_command
    assert '"npm run dev:backend"' in package["scripts"]["dev"]
    assert '"npm run dev:frontend"' in package["scripts"]["dev"]
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for command in (backend_command.removeprefix("cd backend && "), "npm run dev", "uv run python main.py"):
        assert command in readme
    assert "32,000" in readme and "手工浏览器产品体验验收未运行，由用户负责" in readme
    cli = subprocess.run(
        [sys.executable, "-X", "utf8", "main.py", "--help"],
        cwd=ROOT / "backend", capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert cli.returncode == 0 and "--prompt" in cli.stdout and "--max-steps" in cli.stdout

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
        ) as client, TemporaryDirectory(prefix="check-delivery-", dir=WORKSPACE) as directory:
            root = Path(directory)
            path = f"{root.name}/delivery.txt"
            token = uuid4().hex
            session = str(uuid4())

            def turn(prompt):
                with client.stream("POST", "/api/agent/run", json={"session_id": session, "request": prompt}) as response:
                    result = list(events(response))
                wait_idle()
                diagnostic = EVIDENCE / f"turn-{session}-{uuid4().hex}.json"
                diagnostic.write_text(
                    json.dumps({
                        "events": result,
                        "isolated_root": str(root.resolve()),
                        "resolved_paths": [
                            {
                                "tool_call_id": event["data"]["tool_call_id"],
                                "path": event["data"]["arguments"]["path"],
                                "resolved": str((WORKSPACE.resolve() / Path(event["data"]["arguments"]["path"])).resolve()),
                            }
                            for event in result if event["event"] == "tool_start"
                        ],
                        "files": {
                            item.relative_to(root).as_posix(): item.read_text(encoding="utf-8")
                            for item in root.rglob("*") if item.is_file()
                        },
                    }, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                assert result[-1] == {"event": "done", "data": {"status": "completed"}}
                assert result[-2]["event"] == "message" and result[-2]["data"]["text"]
                starts = {}
                results = {}
                for event in result[:-2]:
                    data = event["data"]
                    call = data["tool_call_id"]
                    if event["event"] == "tool_start":
                        assert call not in starts and isinstance(data["arguments"], dict)
                        relative = Path(data["arguments"]["path"])
                        assert not relative.anchor and ".." not in relative.parts, diagnostic.name
                        assert (WORKSPACE.resolve() / relative).resolve().is_relative_to(root.resolve()), diagnostic.name
                        starts[call] = data
                    else:
                        assert event["event"] == "tool_result" and call in starts and call not in results
                        results[call] = data["content"]
                assert starts.keys() == results.keys()
                return result, starts, results

            first, starts, results = turn(
                f"全部文件工具的 path 必须在 {root.name} 目录内。必须调用 write 将 {path} 内容精确写为 {token}，"
                "禁止附加空白或换行。随后必须调用 read 读取该文件，最后回答文件内容。"
            )
            assert (root / "delivery.txt").read_text(encoding="utf-8") == token
            assert {item["name"] for item in starts.values()} >= {"write", "read"}
            assert any(results[call] == f"1: {token}\n" for call, item in starts.items() if item["name"] == "read")
            assert token in first[-2]["data"]["text"]
            second, starts, results = turn(
                "沿用上一轮上下文中的目录、文件路径与原文本。全部文件工具的 path 限定于该目录内。"
                "必须调用 edit 将原文本精确替换为原文本拼接 -updated，禁止附加空白或换行；"
                "随后分别调用 read 读取文件、ls 列出父目录、find 在父目录查找 **/*.txt、"
                "grep 在父目录搜索 updated。五个工具全部实际调用，最后回答原文本与新文本。"
            )
            updated = token + "-updated"
            actual = (root / "delivery.txt").read_text(encoding="utf-8")
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
            assert token in second[-2]["data"]["text"] and updated in second[-2]["data"]["text"]
            print("PASS: dev entry and README commands; preserved CLI; real HTTP/SSE multi-turn memory; six paired file tools with exact disk-backed results")
    finally:
        server.should_exit = True
        thread.join(75)
        listener.close()
    assert not thread.is_alive()


if __name__ == "__main__":
    check()
