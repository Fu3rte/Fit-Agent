import asyncio
import socket
import time
from pathlib import Path
from threading import Thread
from uuid import uuid4

import httpx
import uvicorn


def run_tool(tool, arguments, *, declared=None, signal=None, tool_call_id="call"):
    from app.agent.tool import run_tool_call
    from app.ai.messages import ToolCall

    tool_call = ToolCall(
        type="toolCall", id=tool_call_id, name=tool.name, arguments=arguments
    )
    declarations = {tool.name: tool.definition()} if declared is None else declared
    return asyncio.run(
        run_tool_call(
            tool_call, tools={tool.name: tool}, declared=declarations, signal=signal
        )
    )


def text(message) -> str:
    return "".join(block.text for block in message.content)


BACKEND = Path(__file__).resolve().parents[1]
TEMP_ROOT = BACKEND / "temp"
HOST_HEADER = "127.0.0.1:8000"
ORIGIN_HEADER = "http://localhost:5173"


def temporary_root(name: str) -> Path:
    root = TEMP_ROOT / name
    root.mkdir(parents=True, exist_ok=True)
    return root


def patch_default_database(name: str) -> Path:
    from app.infrastructure.persistence.sqlite import database as database_module

    path = temporary_root(name) / f"{uuid4().hex}.db"
    database_module.default_database_path = lambda: path
    return path


def client(base_url: str, timeout: float = 180) -> httpx.Client:
    return httpx.Client(
        base_url=base_url,
        headers={"Host": HOST_HEADER, "Origin": ORIGIN_HEADER},
        timeout=timeout,
        trust_env=False,
    )


class Server:
    def __init__(self, app):
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", log_level="warning", access_log=False)
        )
        self._thread = Thread(target=self._server.run, kwargs={"sockets": [self._listener]})

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._listener.getsockname()[1]}"

    def __enter__(self) -> "Server":
        self._thread.start()
        deadline = time.monotonic() + 10
        while (
            not self._server.started
            and self._thread.is_alive()
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        if not self._server.started:
            raise AssertionError("服务未启动")
        return self

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(75)
        self._listener.close()
        if self._thread.is_alive():
            raise AssertionError("服务未退出")

    def __exit__(self, *_error) -> None:
        self.stop()
