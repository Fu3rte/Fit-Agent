"""S4-01（P1 修复）：生产依赖组合下 SDK 隐式重试确认为 0 —— 本地假服务端离线断言。

背景（08 8.6）：框架、SDK 与应用层禁止多层隐式重试叠加。openai SDK 默认
``max_retries=2``，会在应用层看不见的情况下重发同一请求；生产接缝
（``runtime.provider.build_openai_client``）显式 ``max_retries=0``。

证据形状（全部走回环地址、合成占位凭据、无真实端点）：

1. 生产客户端配置：HTTP 500 → **恰好一次**发送，异常上抛（SDK 层不再静默重发）。
2. 对照组：SDK 默认配置（``max_retries=2``）对同一假服务端发送 3 次 —— 证明第 1 条不是空断言，
   也说明为何必须在生产接缝关掉它。
3. 框架路径：用生产接缝的客户端 + DeepSeek provider 组装的模型跑一次 ``Agent.run``，
   HTTP 500 时同样只产生一次实际发送（框架不会替 SDK 补发模型请求）。

边界：本文件只证明「实际发送次数」这一事实；真实 Provider 的重试语义与错误分类
（S4-05）不在本文件范围。假服务端返回合成错误体，不含任何真实凭据或响应数据。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest
from openai import AsyncOpenAI, InternalServerError
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.deepseek import DeepSeekProvider

from runtime.models import ModelSpec
from runtime.provider import build_openai_client

#: 合成占位凭据：不是真实 Key，也不从环境读取（本文件绝不触碰 .env）。
_FAKE_KEY = "sk-synthetic-placeholder-for-offline-retry-check"

#: 假服务端声明的端点与模型规格（回环地址，只用于本文件）。
_FAKE_SPEC = ModelSpec(
    model_id="deepseek-flash",
    provider="deepseek",
    base_url="http://127.0.0.1:1/v1",  # 占位，由 fixture 用实际端口替换
    context_window=1_000_000,
    max_output_tokens=384_000,
)


@dataclass(frozen=True)
class _FakeServer:
    """回环假服务端句柄：端点 + 收到的请求路径（按顺序）。"""

    base_url: str
    requests: list[str]


class _CountingHandler(BaseHTTPRequestHandler):
    """对每个请求回 500 并计数；``Retry-After`` 仅用于压缩对照组的退避等待。"""

    def __init__(self, *args: object, requests: list[str], **kwargs: object) -> None:
        self._requests = requests
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 接口名
        self._requests.append(self.path)
        body = json.dumps(
            {"error": {"message": "synthetic server error", "type": "server_error"}}
        ).encode("utf-8")
        self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Retry-After", "0.001")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - 基类接口名
        return  # 静默：绝不打印请求内容或头


@pytest.fixture
def fake_server() -> Iterator[_FakeServer]:
    """回环假服务端；请求计数经处理器实例共享，不写模块级状态。"""
    requests: list[str] = []
    handler = partial(_CountingHandler, requests=requests)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = str(server.server_address[0]), int(server.server_address[1])
    try:
        yield _FakeServer(base_url=f"http://{host}:{port}/v1", requests=requests)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _spec_for(base_url: str) -> ModelSpec:
    return ModelSpec(
        model_id=_FAKE_SPEC.model_id,
        provider=_FAKE_SPEC.provider,
        base_url=base_url,
        context_window=_FAKE_SPEC.context_window,
        max_output_tokens=_FAKE_SPEC.max_output_tokens,
    )


def _assert_loopback(client: AsyncOpenAI) -> None:
    """守卫：确认客户端只指向回环，真实端点不可能被调用。"""
    hostname = urlsplit(str(client.base_url)).hostname
    assert hostname in {"127.0.0.1", "localhost", "::1"}, hostname


async def test_production_client_sends_exactly_once_on_http_500(
    fake_server: _FakeServer,
) -> None:
    client = build_openai_client(
        _FAKE_KEY, spec=_spec_for(fake_server.base_url), timeout_seconds=5.0
    )
    try:
        _assert_loopback(client)
        assert client.max_retries == 0
        with pytest.raises(InternalServerError):
            await client.chat.completions.create(
                model="deepseek-flash",
                messages=[{"role": "user", "content": "ping"}],
            )
    finally:
        await client.close()

    # 一次逻辑调用 = 一次实际发送：SDK 层不再静默重发
    assert fake_server.requests == ["/v1/chat/completions"]


async def test_sdk_default_retries_would_send_three_times_control(
    fake_server: _FakeServer,
) -> None:
    """对照组：默认 ``max_retries=2`` 会对同一故障发送 3 次（本接缝正是要关掉这个行为）。"""
    client = AsyncOpenAI(
        api_key=_FAKE_KEY, base_url=fake_server.base_url, timeout=5.0
    )  # 不传 max_retries：用 SDK 默认值
    try:
        assert client.max_retries == 2
        with pytest.raises(InternalServerError):
            await client.chat.completions.create(
                model="deepseek-flash",
                messages=[{"role": "user", "content": "ping"}],
            )
    finally:
        await client.close()

    assert len(fake_server.requests) == 3


async def test_framework_path_sends_single_request_on_http_500(
    fake_server: _FakeServer,
) -> None:
    """框架路径（生产接缝的客户端 + DeepSeek provider）同样只有一次实际发送。"""
    client = build_openai_client(
        _FAKE_KEY, spec=_spec_for(fake_server.base_url), timeout_seconds=5.0
    )
    try:
        _assert_loopback(client)
        model = OpenAIChatModel(
            "deepseek-flash",
            provider=DeepSeekProvider(openai_client=client),
        )
        agent = Agent(
            model=model, retries={"tools": 0, "output": 0}, name="s401-sdk-retry"
        )
        with pytest.raises(Exception) as excinfo:  # 模型 API 错误按终态失败上抛
            await agent.run("ping")
        assert "synthetic server error" in str(excinfo.value) or "500" in str(
            excinfo.value
        )
    finally:
        await client.close()

    assert len(fake_server.requests) == 1
