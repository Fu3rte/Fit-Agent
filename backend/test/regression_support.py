import asyncio
import os
import socket
import time
from pathlib import Path
from threading import Thread
from uuid import uuid4

import httpx
import uvicorn

from app.domain.session.attachments import TMP_ROOT


def install_test_model_config(root: Path | None = None):
    # 使用隔离目录的真实 JSON 配置服务，凭据来自专用测试环境变量。
    from app import model_config

    api = os.environ.get("FIT_AGENT_TEST_MODEL_API", "openai-completions")
    base_url = os.environ.get("FIT_AGENT_TEST_MODEL_BASE_URL")
    identifier = os.environ.get("FIT_AGENT_TEST_MODEL_ID")
    api_key = os.environ.get("FIT_AGENT_TEST_MODEL_API_KEY")
    provider = os.environ.get("FIT_AGENT_TEST_MODEL_PROVIDER")
    missing = [
        name
        for name, value in (
            ("FIT_AGENT_TEST_MODEL_BASE_URL", base_url),
            ("FIT_AGENT_TEST_MODEL_ID", identifier),
            ("FIT_AGENT_TEST_MODEL_API_KEY", api_key),
        )
        if not value
    ]
    if missing:
        raise RuntimeError("缺少测试模型环境变量: " + ", ".join(missing))
    if root is None:
        root = temporary_root("model-config") / uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    model_config.DATA_ROOT = root
    model_config.save_provider(
        api=api,
        base_url=base_url,
        model=identifier,
        provider=provider,
        api_key=api_key,
    )
    return model_config.load_model_config()


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
# 文件工具按可信会话绑定；测试使用固定会话，路径以注入的 tmp 根为基准。
TEST_SESSION = "00000000-0000-4000-8000-000000000001"


def session_workspace(
    tmp_root: Path, session_id: str = TEST_SESSION
) -> tuple[Path, str]:
    directory = Path(tmp_root) / "sessions" / session_id / "workspace"
    directory.mkdir(parents=True, exist_ok=True)
    return directory, f"sessions/{session_id}/workspace"


def session_directory(tmp_root: Path, session_id: str = TEST_SESSION) -> Path:
    return Path(tmp_root) / "sessions" / session_id


def seed_note(session_id: str = TEST_SESSION) -> str:
    # 文件工具按可信会话绑定；预置真实工作文件后返回 read 可用的相对路径。
    directory, prefix = session_workspace(TMP_ROOT, session_id)
    (directory / "note.txt").write_text("工具期间输入", encoding="utf-8")
    return f"{prefix}/note.txt"


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
