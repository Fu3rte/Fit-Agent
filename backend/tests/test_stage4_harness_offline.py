"""S4-01：生产依赖组合下的 Harness 四类离线断言（stage4.md S4-01；08 8.6/8.7）。

生产依赖组合（本文件实测）：Python 3.13.15、pydantic-ai-slim 2.41.0、httpx2 2.12.0、
openai 3.13.0（Provider SDK 已按用户授权加入并锁定）。本文件仍只用框架内的脚本桩
（``FunctionModel``）做确定性断言，配合进程内 socket 断网守卫保证零真实请求；
SDK 层隐式重试的离线断言见 ``tests/test_stage4_sdk_retries_offline.py``，
真实 Provider 连通性证据见 ``scripts/deepseek_smoke.py``。

四类断言对应 S4-01 验收：

1. 纠错请求计入同一 Run 的请求计数（适配层以 ``RunUsage.requests`` 为权威计数依据）。
2. 框架工具重试预算为 0 时框架**不会**自己补一次请求：纠错必须由适配层按 Run 级预算驱动。
3. 框架工具重试预算是 **per-tool** 而非 Run 级（两个工具各自获得预算，总数超过 Run 级一次）：
   所以框架预算不能当 Run 级计数权威，且它的每次尝试都可见于 ``RunUsage.requests``，不存在
   适配层看不见的隐式尝试。
4. 取消（在纠错请求开始前）不会启动新的模型尝试；写入后报错时，框架不保证不重复写入，
   必须由工具/适配层做状态核对（08 8.6「写入结果不确定先核对状态」）。

未在本文件覆盖（如实缺口）：真实流式断线、真实 Provider 的 ``finish_reason``/HTTP 分类、
单次请求 120 秒与 Run 300 秒时限（S4-05 实现）。SDK 层隐式重试已由
``tests/test_stage4_sdk_retries_offline.py`` 覆盖；真实模型调用不在本文件（默认套件必须离线）。
"""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator, Iterator
from typing import Any, Literal

import pytest
from pydantic_ai import Agent, ModelRequestNode, ModelRetry
from pydantic_ai.exceptions import RunCancelled, UnexpectedModelBehavior
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

# 每次模型“请求”的脚本：(工具名, 参数)；参数 None 表示直接给最终文本回答。
_ScriptStep = tuple[str, dict[str, Any]]


@pytest.fixture(scope="module", autouse=True)
def _deny_outbound_network() -> Iterator[None]:
    """进程内断网守卫：任何真实发出请求的尝试都立刻显式失败（不是静默泄漏）。"""

    def _deny(*args: object, **kwargs: object) -> None:
        raise OSError("offline: 离线桩检查期间禁止对外网络")

    getaddrinfo, create_connection = socket.getaddrinfo, socket.create_connection
    socket.getaddrinfo = _deny  # type: ignore[assignment]
    socket.create_connection = _deny  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.getaddrinfo = getaddrinfo
        socket.create_connection = create_connection


class _ScriptedModel:
    """按脚本产生模型响应并记录每次实际模型请求（一个请求 = 一次函数调用）。"""

    def __init__(self, script: list[_ScriptStep]) -> None:
        self.script = script
        self.requests: list[list[str]] = []

    def model(self) -> FunctionModel:
        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            self.requests.append(
                [type(part).__name__ for message in messages for part in message.parts]
            )
            step = len(self.requests) - 1
            if step < len(self.script):
                name, args = self.script[step]
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            tool_name=name, args=args, tool_call_id=f"call-{step}"
                        )
                    ]
                )
            return ModelResponse(parts=[TextPart(content="完成")])

        return FunctionModel(respond)

    @property
    def attempts(self) -> int:
        return len(self.requests)


# ---------- 断言 1–2：纠错计数与框架不替我们补请求 ----------


async def test_correction_request_counts_into_run_request_usage() -> None:
    model = _ScriptedModel(
        [("record", {"topic": "bad"}), ("record", {"topic": "good"})]
    )
    writes: list[str] = []
    agent = Agent(
        model=model.model(), retries={"tools": 1, "output": 0}, name="s401-count"
    )

    @agent.tool_plain
    async def record(topic: Literal["good"]) -> str:
        writes.append(topic)
        return "已记录"

    result = await agent.run("记一下")

    # 首次请求参数非法 → 工具零执行 → 纠错请求 → 终稿请求
    assert model.attempts == 3
    assert writes == ["good"]
    assert result.usage.requests == model.attempts
    # 纠错请求确实带上了可修正的反馈（RetryPromptPart），不是静默重发
    assert "RetryPromptPart" in model.requests[1]


async def test_framework_tool_budget_zero_starts_no_correction_request() -> None:
    model = _ScriptedModel(
        [("record", {"topic": "bad"}), ("record", {"topic": "good"})]
    )
    writes: list[str] = []
    agent = Agent(
        model=model.model(), retries={"tools": 0, "output": 0}, name="s401-zero"
    )

    @agent.tool_plain
    async def record(topic: Literal["good"]) -> str:
        writes.append(topic)
        return "已记录"

    with pytest.raises(UnexpectedModelBehavior):
        await agent.run("记一下")

    # 预算为 0 时框架不自行补发请求：纠错只能由适配层按 Run 级预算驱动
    assert model.attempts == 1
    assert writes == []


# ---------- 断言 3：框架预算是 per-tool，不能充当 Run 级计数权威 ----------


async def test_framework_tool_retry_budget_is_per_tool_not_run_scoped() -> None:
    model = _ScriptedModel(
        [
            ("first", {"kind": "bad"}),
            ("second", {"kind": "bad"}),
            ("first", {"kind": "good"}),
            ("second", {"kind": "good"}),
        ]
    )
    agent = Agent(
        model=model.model(), retries={"tools": 1, "output": 0}, name="s401-scope"
    )

    @agent.tool_plain
    async def first(kind: Literal["good"]) -> str:
        return "一"

    @agent.tool_plain
    async def second(kind: Literal["good"]) -> str:
        return "二"

    result = await agent.run("两个动作都要")

    # 两个工具各自获得 1 次纠错预算 → 纠错请求多于 Run 级「1 次」；且每一步都计入 usage
    assert model.attempts > 3
    assert result.usage.requests == model.attempts
    # 两个工具各触发了一次纠错请求（框架 per-tool 预算，不是共享的一次）
    assert sum(1 for parts in model.requests if "RetryPromptPart" in parts) >= 2


# ---------- 断言 4：取消不启动新尝试；写后报错需状态核对 ----------


async def test_cancel_before_correction_request_starts_no_new_attempt() -> None:
    model = _ScriptedModel(
        [("record", {"topic": "bad"}), ("record", {"topic": "good"})]
    )
    writes: list[str] = []
    agent = Agent(
        model=model.model(), retries={"tools": 1, "output": 0}, name="s401-cancel"
    )

    @agent.tool_plain
    async def record(topic: Literal["good"]) -> str:
        writes.append(topic)
        return "已记录"

    model_request_nodes = 0
    with pytest.raises(RunCancelled):
        async with agent.iter("记一下") as agent_run:
            async for node in agent_run:
                if isinstance(node, ModelRequestNode):
                    model_request_nodes += 1
                    if model_request_nodes == 2:  # 纠错请求即将开始
                        agent_run.cancel()

    # 取消生效在纠错请求之前：没有第二次模型尝试，也没有工具副作用
    assert model.attempts == 1
    assert writes == []


async def test_write_then_error_needs_state_check_to_avoid_duplicate_write() -> None:
    """工具“写入后报错被要求重发”时：框架原生会重复写入，状态核对桩收敛为一次。"""

    async def run(stub) -> tuple[list[str], int]:
        model = _ScriptedModel([("apply", {"key": "K1"}), ("apply", {"key": "K1"})])
        store: list[str] = []
        agent = Agent(
            model=model.model(), retries={"tools": 2, "output": 0}, name="s401-write"
        )

        @agent.tool_plain
        async def apply(key: str) -> str:
            stub(store, key)
            raise ModelRetry("写入确认丢失，请核对状态")

        await agent.run("提交 K1")
        return store, model.attempts

    def naive(store: list[str], key: str) -> None:
        store.append(key)

    def state_checked(store: list[str], key: str) -> None:
        if key not in store:  # 幂等桩：先核对已存在状态再写
            store.append(key)

    naive_store, naive_attempts = await run(naive)
    checked_store, checked_attempts = await run(state_checked)

    # 框架不拦重复工具调用：模型重发时两次都真的写了
    assert naive_store == ["K1", "K1"]
    assert checked_store == ["K1"]
    # 两者尝试次数相同（核对桩只改变写入结果，不改变框架重发行为）
    assert naive_attempts == checked_attempts


async def _unused_async_iterator_typing() -> AsyncIterator[None]:  # pragma: no cover
    yield None
