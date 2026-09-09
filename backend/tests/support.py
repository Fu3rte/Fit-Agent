"""Stage 0 测试共用设施：临时文件库、假框架消息与序列化往返助手。

约束（stage0.md 第 6 节）：每个用例只操作 pytest tmp_path 下的独立临时文件库，
不触碰真实用户数据目录、不读取真实 API Key、不访问模型或其他外部网络。

框架消息序列化在此以 PydanticAI ``ModelMessagesTypeAdapter`` 做 runtime 层
（Stage 4）的序列化替身：storage 层本身不 import PydanticAI（README 硬规则）。
"""

import http.client
import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from pydantic_ai import ModelMessagesTypeAdapter
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from storage.db import Database

# 固定时间戳：让假框架消息的 JSON 往返可精确比较（不受测试运行时刻影响）。
FIXED_TS = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)

BACKEND_ROOT = Path(__file__).resolve().parents[1]


@asynccontextmanager
async def open_database(
    path: Path, *, migrate: bool = True, migrations_dir: str | Path | None = None
) -> AsyncIterator[Database]:
    """打开一个隔离的临时文件库；退出时关闭，不遗留连接。"""
    db = Database(path, migrations_dir=migrations_dir)
    await db.open()
    try:
        if migrate:
            await db.migrate()
        yield db
    finally:
        await db.close()


def dump_framework_message(message: ModelMessage) -> str:
    """runtime 层序列化替身：单条框架消息 → 单元素数组 JSON 字符串。"""
    return ModelMessagesTypeAdapter.dump_json([message]).decode("utf-8")


def load_framework_message(payload: str) -> ModelMessage:
    """runtime 层反序列化替身：单元素数组 JSON → 单条框架消息。"""
    messages = ModelMessagesTypeAdapter.validate_json(payload)
    assert len(messages) == 1
    return messages[0]


def user_request(text: str) -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(content=text, timestamp=FIXED_TS)])


def text_response(text: str) -> ModelResponse:
    return ModelResponse(parts=[TextPart(content=text)], timestamp=FIXED_TS)


def tool_call_response(tool_name: str, args: dict, tool_call_id: str) -> ModelResponse:
    return ModelResponse(
        parts=[ToolCallPart(tool_name=tool_name, args=args, tool_call_id=tool_call_id)],
        timestamp=FIXED_TS,
    )


def tool_return_request(
    tool_name: str, content: str, tool_call_id: str
) -> ModelRequest:
    return ModelRequest(
        parts=[
            ToolReturnPart(
                tool_name=tool_name,
                content=content,
                tool_call_id=tool_call_id,
                timestamp=FIXED_TS,
            )
        ]
    )


# ---------- 真实入口子进程助手（api.app + uvicorn，仅回环） ----------


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_app(
    data_dir: Path, port: int, *, log_level: str = "warning"
) -> subprocess.Popen[bytes]:
    """以真实入口启动最小服务：``python -m uvicorn api.app:create_app --factory``。

    ``log_level`` 可覆盖以便取最坏情况（``debug``）验证服务日志不带出凭据。
    """
    env = {
        **os.environ,
        "FIT_AGENT_DATA_DIR": str(data_dir),
        "PYTHONPATH": str(BACKEND_ROOT),
    }
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "api.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            log_level,
        ],
        cwd=BACKEND_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def stop_app(process: subprocess.Popen[bytes]) -> str:
    """停服并回收子进程，返回合并捕获的 stdout/stderr 文本。

    只用 ``Popen.wait()`` 回收：``poll()``/``wait()`` 已 reap 子进程后再调
    ``os.waitpid`` 会抛 ChildProcessError（进程对象自己管理子进程状态）。
    """
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
    stream = process.stdout
    return stream.read().decode("utf-8", "replace") if stream is not None else ""


def http_get(
    port: int, path: str = "/healthz", headers: dict[str, str] | None = None
) -> tuple[int, str]:
    """经 http.client 直连回环端口；可覆盖 Host/Origin 头以验证校验行为。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3.0)
    try:
        conn.request("GET", path, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read().decode("utf-8")
    finally:
        conn.close()


def wait_for_healthz(port: int, *, deadline_seconds: float = 15.0) -> tuple[int, str]:
    """轮询直到 /healthz 可达（真实进程启动证据）；超时抛 AssertionError。"""
    deadline = time.monotonic() + deadline_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return http_get(port)
        except (OSError, http.client.HTTPException) as error:
            last_error = error
            time.sleep(0.05)
    raise AssertionError(f"服务未在期限内就绪: {last_error}")
