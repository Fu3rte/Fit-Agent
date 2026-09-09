# Harness 离线桩验证（2026-09-08 授权，PLAN.md「Harness 离线桩验证授权」小节）。
# 四项离线桩验证，全部基于实装框架（pydantic-ai 2.40.0 / openai 3.8.0 / httpx2 2.12.0）：
#   ① 事件流（run_stream_events）下可修正的副作用前校验失败是否触发纠错；每次模型尝试是否计入
#      RunUsage.requests 且与 UsageLimits.request_limit 共享同一预算；预算耗尽时纠错被拒绝。
#   ② AsyncOpenAI max_retries=0 + 假传输：一次逻辑调用只发生一次实际发送；对照 SDK 默认重试层。
#   ③ 纠错/门控退避期间取消：不再启动新的模型/工具尝试；进行中的工具任务被取消并排干。
#      区分框架级取消（AgentRun.cancel）与测试自有的重试门控等待策略（框架无内建退避延迟）。
#   ④ 工具写入后报错、模型再次发出同一工具调用：如实刻画框架原生重复写入风险，并用最小状态核对
#      桩（应用侧责任）证明可把一次合成写入收敛为一次；不宣称框架自带去重。
# 安全边界：合成非健康数据 + 占位密钥；写入仅限内存合成存储；无真实网络、无真实凭据、无业务库；
# 无任何生产重试/纠错参数决策（文中数字均为测试输入）。重试次数/费用不做真实计费验证。
#
# 运行方式（离线，进程级网络隔离 + socket 断网守卫）：
#   cd test/spike && unshare -rn .venv/bin/python -m pytest tests/test_harness_offline_stub_checks.py -q

from __future__ import annotations

import asyncio
import socket as _socket
from typing import Any, Literal

# venv 内依赖（httpx2/openai/pydantic_ai/pydantic_graph/pytest）：在本项目 venv
# 与 pyrightconfig.json（venvPath=.venv）下均可解析；仅外部扫描器的 import 误报，
# 与 tests/test_cancel.py 的既有注释约定一致。
import httpx2  # pyright: ignore[reportMissingImports]
import pytest  # pyright: ignore[reportMissingImports]
from openai import (  # pyright: ignore[reportMissingImports]
    APIConnectionError,
    APIError,
    AsyncOpenAI,
    BadRequestError,
    InternalServerError,
)
from pydantic_ai import (  # pyright: ignore[reportMissingImports]
    Agent,
    AgentRetries,
    ModelRetry,
)
from pydantic_ai.exceptions import (  # pyright: ignore[reportMissingImports]
    RunCancelled,
    UsageLimitExceeded,
    UserError,
)
from pydantic_ai.messages import (  # pyright: ignore[reportMissingImports]
    ModelMessage,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import (  # pyright: ignore[reportMissingImports]
    AgentInfo,
    DeltaToolCall,
    FunctionModel,
)
from pydantic_ai.usage import UsageLimits  # pyright: ignore[reportMissingImports]
from pydantic_graph import End  # pyright: ignore[reportMissingImports]

WATCHDOG_SECONDS = 15.0


@pytest.fixture(scope="module", autouse=True)
def _deny_outbound_network() -> Any:
    """Process-level tripwire: no DNS / outbound connect while these tests run.

    Real network is already impossible for these scenarios (pure FunctionModel +
    injected fake transports); this makes any accidental outbound attempt fail fast
    and visibly instead of silently leaking a request. 进程级再套 unshare -rn 双保险。
    """

    def _deny(*args: object, **kwargs: object) -> None:  # pragma: no cover - tripwire
        raise OSError("outbound network access disabled (offline harness stub checks)")

    real_getaddrinfo = _socket.getaddrinfo
    real_create_connection = _socket.create_connection
    _socket.getaddrinfo = _deny  # type: ignore[assignment]
    _socket.create_connection = _deny  # type: ignore[assignment]
    try:
        yield
    finally:
        _socket.getaddrinfo = real_getaddrinfo
        _socket.create_connection = real_create_connection


# --------------------------------------------------------------------------
# 检查 ①：事件流（run_stream_events）纠错 + 请求预算共享/拒绝
# --------------------------------------------------------------------------


def _check1_scenario(request_limit: int | None) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        invocations: list[list[str]] = []  # 每次模型请求“看到”的 part 类型（历史）
        writes: list[str] = []

        async def stream_model(messages: list[ModelMessage], info: AgentInfo):
            # run_stream_events 的后台 run() 对每次模型请求走流式，一个请求 = 一次本函数调用。
            invocations.append([type(p).__name__ for m in messages for p in m.parts])
            step = len(invocations) - 1
            if step == 0:
                # 参数不可校验通过 → 工具不执行（副作用前校验失败）
                yield {
                    0: DeltaToolCall(
                        name="record", json_args='{"topic": "bad"}', tool_call_id="t1"
                    )
                }
            elif step == 1:
                # 纠错请求：模型改正参数（预置脚本模拟“模型可修正”）
                yield {
                    0: DeltaToolCall(
                        name="record", json_args='{"topic": "good"}', tool_call_id="t2"
                    )
                }
            else:
                yield "finished"

        agent = Agent(model=FunctionModel(stream_function=stream_model), name="check1")

        @agent.tool_plain
        async def record(topic: Literal["good"]) -> str:
            writes.append(topic)
            return f"recorded {topic}"

        events: list[str] = []
        raised: str | None = None
        requests: int | None = None
        handle: Any = None
        try:
            async with agent.run_stream_events(
                "save the record",
                usage_limits=UsageLimits(request_limit=request_limit)
                if request_limit is not None
                else None,
            ) as events_handle:
                handle = events_handle
                async for event in events_handle:
                    events.append(type(event).__name__)
        except UsageLimitExceeded as exc:
            raised = type(exc).__name__
        if handle is not None:
            try:
                requests = handle.usage.requests
            except (
                UserError
            ):  # pragma: no cover - 仅在 run 从未启动时出现，这里总会启动
                requests = None
        return {
            "raised": raised,
            "model_invocations": len(invocations),
            "requests": requests,
            "writes": writes,
            "corrective_saw_retry_prompt": any(
                "RetryPromptPart" in seen for seen in invocations[1:]
            ),
            "events": events,
        }

    return asyncio.run(run())


def test_check1_event_stream_corrects_validation_failure_pre_side_effect() -> None:
    r = _check1_scenario(request_limit=None)
    assert r["raised"] is None
    # ① 事件流模式下发生纠错：坏参数请求 + 纠错请求 + 终稿请求，共 3 次模型尝试
    assert r["model_invocations"] == 3
    # 框架请求计数与模型调用一致（纠错请求计入 usage.requests，与正常请求同桶）
    assert r["requests"] == 3
    # 坏参数的那次尝试没有任何副作用（工具未执行）；纠错后只写入一次
    assert r["writes"] == ["good"]
    # 模型第 2 次尝试确实收到了纠错提示（RetryPromptPart 反馈）
    assert r["corrective_saw_retry_prompt"] is True


def test_check1_request_limit_denies_corrective_attempt() -> None:
    r = _check1_scenario(request_limit=1)
    # 共享预算拒绝：纠错请求在发送前被 UsageLimits 拦截
    assert r["raised"] == "UsageLimitExceeded"
    assert r["model_invocations"] == 1
    assert r["requests"] == 1
    assert r["writes"] == []  # 工具从未执行


def test_check1_corrective_attempt_shares_request_budget() -> None:
    r = _check1_scenario(request_limit=2)
    # 预算 2 = 初始请求 + 纠错请求：纠错执行成功（副作用一次），随后的终稿请求被拒绝
    assert r["raised"] == "UsageLimitExceeded"
    assert r["model_invocations"] == 2
    assert r["requests"] == 2
    assert r["writes"] == ["good"]
    # 说明：预算拒绝发生在工具已产生副作用之后，框架的 request_limit 是请求前检查，
    # 不是“副作用前预留”；真正的一次性成本预留语义不在本检查范围（费用护栏待另行验证）。
    assert r["corrective_saw_retry_prompt"] is True


# --------------------------------------------------------------------------
# 检查 ②：AsyncOpenAI max_retries=0 → 恰好一次实际发送；对照 SDK 默认隐式重试
# --------------------------------------------------------------------------


class _CountingTransport(httpx2.AsyncBaseTransport):
    """假传输：记录每次实际发送，返回脚本化失败响应或模拟传输层异常；绝不触网。"""

    def __init__(self, mode: str) -> None:
        self.mode = mode  # 'http500' | 'http400' | 'connect_error'
        self.sends: list[str] = []
        self.requests_seen: list[Any] = []

    async def handle_async_request(self, request: Any) -> httpx2.Response:
        self.sends.append(f"{request.method} {request.url}")
        self.requests_seen.append(request)
        if self.mode == "connect_error":
            raise httpx2.ConnectError("simulated connection failure (offline stub)")
        if self.mode == "http400":
            status, body = 400, b'{"error":{"message":"permanent bad request"}}'
        else:
            status, body = 500, b'{"error":{"message":"transient internal error"}}'
        headers = {"retry-after": "0.001"} if status == 500 else {}
        return httpx2.Response(status, headers=headers, content=body, request=request)


def _sdk_case(max_retries: int, mode: str) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        transport = _CountingTransport(mode)
        http_client = httpx2.AsyncClient(transport=transport)
        client = AsyncOpenAI(
            base_url="http://127.0.0.1:9/v1",  # 本地不可路由占位地址，仅用于构造
            api_key="sk-not-a-real-key-0000",  # 合成占位密钥，非凭据
            max_retries=max_retries,
            http_client=http_client,
        )
        raised: type[Exception] | None = None
        try:
            await client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": "hello"}],
            )
        except APIError as exc:
            raised = type(exc)
        finally:
            await http_client.aclose()
        return {"raised": raised, "sends": len(transport.sends)}

    return asyncio.run(run())


def test_check2_max_retries_zero_sends_exactly_once() -> None:
    # 可重试的 5xx：max_retries=0 时 SDK 不重试 → 恰好一次实际发送，然后抛 API 错误
    r = _sdk_case(max_retries=0, mode="http500")
    assert r["raised"] is InternalServerError
    assert r["sends"] == 1


def test_check2_max_retries_zero_transport_error_sends_exactly_once() -> None:
    # 传输层异常同样只发一次（不静默重发），包装为 APIConnectionError
    r = _sdk_case(max_retries=0, mode="connect_error")
    assert r["raised"] is APIConnectionError
    assert r["sends"] == 1


def test_check2_sdk_default_max_retries_hidden_resends() -> None:
    # 对照：SDK 默认 max_retries=2（openai 3.8.0 DEFAULT_MAX_RETRIES），同一 5xx 响应
    # 会被隐式重发两次（共 3 次实际发送）——应用层看不到、不进入框架 usage/cost 口径。
    # Retry-After: 0.001 仅用于把 SDK 内建退避压到 ~毫秒，保证测试有界且不发真实网络。
    r = _sdk_case(max_retries=2, mode="http500")
    assert r["raised"] is InternalServerError
    assert r["sends"] == 3


def test_check2_permanent_errors_not_retried_by_sdk_defaults() -> None:
    # 对照：4xx 永久错误即使默认重试也不重发 → 一次实际发送（分类重试原则的 SDK 侧证据）
    r = _sdk_case(max_retries=2, mode="http400")
    assert r["raised"] is BadRequestError
    assert r["sends"] == 1


# --------------------------------------------------------------------------
# 检查 ③：纠错/门控退避期间取消 —— 不再启动新尝试；进行中任务被排干
# --------------------------------------------------------------------------

_CORRECTIVE_RETRY_BUDGET: AgentRetries = {"tools": 3}  # 测试输入，非生产参数


def _is_corrective_request_node(node: Any) -> bool:
    """待执行的节点若为带 RetryPromptPart 的新模型请求 → 属纠错尝试。"""
    return type(node).__name__ == "ModelRequestNode" and any(
        isinstance(part, RetryPromptPart) for part in node.request.parts
    )


def _check3_cancel_before_corrective_request() -> dict[str, Any]:
    """3A 框架级取消：纠错模型请求尚未执行即取消 → 该新尝试永不发生。"""

    async def run() -> dict[str, Any]:
        invocations: list[int] = []
        writes: list[int] = []

        def scripted(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            invocations.append(1)
            # 模型（无论第几次）都只会发出同一个工具调用；如果纠错请求真的执行，
            # 将出现第二次工具执行。
            return ModelResponse(
                parts=[ToolCallPart(tool_name="work", args={"x": 1}, tool_call_id="w1")]
            )

        agent = Agent(
            model=FunctionModel(function=scripted),
            name="check3a",
            retries=_CORRECTIVE_RETRY_BUDGET,
        )

        @agent.tool_plain
        async def work(x: int) -> str:
            writes.append(x)
            raise ModelRetry("recorded, then transient error")

        node_flow: list[str] = []
        requests_snapshot: int | None = None
        raised: str | None = None
        try:
            async with agent.iter("go") as run:
                node = run.next_node
                while not isinstance(node, End):
                    node_flow.append(type(node).__name__)
                    if _is_corrective_request_node(node):
                        run.cancel()  # 框架级取消（AgentRun.cancel），纠错请求不被执行
                        requests_snapshot = run.usage.requests
                        await asyncio.sleep(
                            0
                        )  # 交付取消信号后由 iter 上下文转 RunCancelled
                    node = await run.next(node)
        except RunCancelled as exc:
            raised = type(exc).__name__
        return {
            "raised": raised,
            "model_invocations": len(invocations),
            "requests_snapshot": requests_snapshot,
            "writes": writes,
            "node_flow": node_flow,
        }

    return asyncio.run(run())


def test_check3_cancel_before_corrective_request_stops_new_attempt() -> None:
    r = _check3_cancel_before_corrective_request()
    assert r["raised"] == "RunCancelled"
    # 取消后不再启动新的模型尝试：只有初始那一次
    assert r["model_invocations"] == 1
    assert r["requests_snapshot"] == 1
    # 工具只执行过一次（纠错尝试若发生会把 work 再执行一次并再写一次）
    assert r["writes"] == [1]
    # 节点流确实走到了“纠错请求”边界才取消
    assert "ModelRequestNode" in r["node_flow"] and "CallToolsNode" in r["node_flow"]


def _check3_gated_retry_delay(cancel_waiting: bool) -> dict[str, Any]:
    """3B 测试自有“重试门控延迟”：纠错请求前挂起在测试门控上（模拟未来适配器
    的有界退避窗口）。框架本身没有内建退避；取消时门控等待被打断，纠错请求不发生。"""

    async def run() -> dict[str, Any]:
        invocations: list[int] = []
        writes: list[int] = []
        gate = asyncio.Event()
        entered_gate = asyncio.Event()
        state: dict[str, Any] = {}

        def scripted(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            invocations.append(1)
            if len(invocations) == 1:
                return ModelResponse(
                    parts=[
                        ToolCallPart(tool_name="work", args={"x": 1}, tool_call_id="w1")
                    ]
                )
            return ModelResponse(parts=[TextPart(content="ok")])

        agent = Agent(
            model=FunctionModel(function=scripted),
            name="check3b",
            retries=_CORRECTIVE_RETRY_BUDGET,
        )

        @agent.tool_plain
        async def work(x: int) -> str:
            writes.append(x)
            raise ModelRetry("recorded, then transient error")

        async def driver() -> None:
            async with agent.iter("go") as run:
                state["run"] = run
                node = run.next_node
                while not isinstance(node, End):
                    if _is_corrective_request_node(node):
                        # 测试自有的“退避/门控”等待：放行前暂停纠错尝试
                        entered_gate.set()
                        await gate.wait()
                    node = await run.next(node)

        task = asyncio.create_task(driver())
        raised: str | None = None
        try:
            async with asyncio.timeout(WATCHDOG_SECONDS):
                await entered_gate.wait()
                if cancel_waiting:
                    state["run"].cancel()  # 门控等待期间取消
                else:
                    gate.set()  # 对照：放行门控 → 纠错请求照常发生
                await task
        except RunCancelled:
            raised = "RunCancelled"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return {
            "raised": raised,
            "model_invocations": len(invocations),
            "writes": writes,
        }

    return asyncio.run(run())


def test_check3_cancel_during_gated_retry_delay_stops_corrective_attempt() -> None:
    r = _check3_gated_retry_delay(cancel_waiting=True)
    assert r["raised"] == "RunCancelled"
    assert r["model_invocations"] == 1  # 纠错请求在门控期间被取消 → 从未发出
    assert r["writes"] == [1]


def test_check3_gate_release_allows_corrective_attempt_control() -> None:
    # 对照：同一门控策略，放行后纠错请求正常发出（证明 3B 是“取消”而非结构导致）
    r = _check3_gated_retry_delay(cancel_waiting=False)
    assert r["raised"] is None
    assert r["model_invocations"] == 2


def _check3_cancel_during_inflight_tool() -> dict[str, Any]:
    """3C 排干：取消时仍有工具任务在飞行 → 进行中任务被取消并排干，
    已完成的副作用保留，之后不再有新的模型/工具尝试。"""

    async def run() -> dict[str, Any]:
        invocations: list[int] = []
        writes: list[str] = []
        blocked_started = asyncio.Event()
        blocked_cleaned = asyncio.Event()
        state: dict[str, Any] = {}

        def scripted(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            invocations.append(1)
            if len(invocations) == 1:
                return ModelResponse(
                    parts=[
                        ToolCallPart(tool_name="blocked", args={}, tool_call_id="b1"),
                        ToolCallPart(
                            tool_name="fast_write", args={}, tool_call_id="f1"
                        ),
                    ]
                )
            return ModelResponse(parts=[TextPart(content="ok")])

        agent = Agent(model=FunctionModel(function=scripted), name="check3c")

        @agent.tool_plain
        async def blocked() -> str:
            blocked_started.set()
            try:
                await asyncio.Event().wait()  # 永不自行放行，只能被取消打断
                return "unreachable"  # pragma: no cover
            finally:
                blocked_cleaned.set()

        @agent.tool_plain
        async def fast_write() -> str:
            writes.append("fast")
            return "ok"

        async def driver() -> None:
            async with agent.iter("go") as run:
                state["run"] = run
                node = run.next_node
                while not isinstance(node, End):
                    node = await run.next(node)

        task = asyncio.create_task(driver())
        raised: str | None = None
        try:
            async with asyncio.timeout(WATCHDOG_SECONDS):
                await blocked_started.wait()
                state["run"].cancel()
                await task
        except RunCancelled:
            raised = "RunCancelled"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return {
            "raised": raised,
            "model_invocations": len(invocations),
            "writes": writes,
            "blocked_cleaned": blocked_cleaned.is_set(),
        }

    return asyncio.run(run())


def test_check3_cancel_drains_inflight_tool_and_stops_new_attempts() -> None:
    r = _check3_cancel_during_inflight_tool()
    assert r["raised"] == "RunCancelled"
    # 取消前已完成的副作用保留（fast_write 写入一次）
    assert r["writes"] == ["fast"]
    # 在飞行中的 blocked 工具被取消并排干（finally 执行、无悬挂任务）
    assert r["blocked_cleaned"] is True
    # 取消后不再有新的模型/工具尝试
    assert r["model_invocations"] == 1


# --------------------------------------------------------------------------
# 检查 ④：工具“写入后报错”模型重发同一工具 —— 原生重复写入风险 vs 状态核对桩
# --------------------------------------------------------------------------


def _check4_scenario(idempotent_stub: bool) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        invocations: list[list[str]] = []
        writes: list[tuple[str, int]] = []
        store: dict[str, int] = {}

        def scripted(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            invocations.append([type(p).__name__ for m in messages for p in m.parts])
            n = len(invocations)
            if n == 1:
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            tool_name="apply_payment",
                            args={"key": "K1", "amount": 10},
                            tool_call_id="p1",
                        )
                    ]
                )
            if n == 2:
                # 纠错请求：真实模型完全可能再次发出同一工具调用（本检查要刻画的场景）
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            tool_name="apply_payment",
                            args={"key": "K1", "amount": 10},
                            tool_call_id="p2",
                        )
                    ]
                )
            return ModelResponse(parts=[TextPart(content="done")])

        agent = Agent(
            model=FunctionModel(function=scripted),
            name="check4",
            retries=_CORRECTIVE_RETRY_BUDGET,
        )

        @agent.tool_plain
        async def apply_payment(key: str, amount: int) -> str:
            if idempotent_stub and key in store:
                # 最小状态核对桩（应用侧责任）：已写入则跳过，不再写第二次
                return f"already applied {store[key]}"
            store[key] = amount
            writes.append((key, amount))
            # 写入完成但确认丢失 → 工具报可纠错错误，触发纠错请求
            raise ModelRetry("write happened but commit confirmation was lost")

        result = await agent.run("apply payment")
        return {
            "model_invocations": len(invocations),
            "requests": result.usage.requests,
            "writes": writes,
            "store": store,
            "output": result.output,
            "corrective_saw_retry_prompt": any(
                "RetryPromptPart" in seen for seen in invocations[1:]
            ),
        }

    return asyncio.run(run())


def test_check4_native_duplicate_write_after_write_then_error() -> None:
    r = _check4_scenario(idempotent_stub=False)
    # 原生行为：工具先写入成功又报错；纠错请求中模型重发同一工具 → 第二次执行同样写入。
    # 框架对同一工具调用不做幂等/去重：两次工具执行都真实发生（2 次写入）。
    assert r["writes"] == [("K1", 10), ("K1", 10)]
    assert r["store"] == {"K1": 10}
    # 模型一共被调用 3 次（初始 + 纠错重发 + 终稿），框架请求计数一致
    assert r["model_invocations"] == 3
    assert r["requests"] == 3
    assert r["output"] == "done"
    assert r["corrective_saw_retry_prompt"] is True


def test_check4_state_check_stub_prevents_duplicate_write() -> None:
    r = _check4_scenario(idempotent_stub=True)
    # 同样 3 次模型尝试、纠错请求同样重发同一工具（工具仍被调用两次），
    # 但应用侧最小状态核对使合成业务写入只发生一次 —— 去重来自适配层桩，不是框架。
    assert r["model_invocations"] == 3
    assert r["requests"] == 3
    assert r["writes"] == [("K1", 10)]
    assert r["store"] == {"K1": 10}
    assert r["output"] == "done"
