# 模型入口的工具调用形态：与 text／structured 复用同一个 chat client，只经 bind_tools() 绑定 offered
# 工具，只读取标准 AIMessage.tool_calls；非 AIMessage 响应明确失败。lazy 入口每次调用重新 resolve
# Provider 配置，配置错误与 InvalidModelResponse 原样上抛，其余 Provider 失败包装为 ModelCallFailed。
# 全部用替身 Chat 客户端，不联网。

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables.base import RunnableBinding
from langchain_core.tools import BaseTool, tool
from langchain_openai import ChatOpenAI

from app.application.ports import (
    MODEL_CALL_FAILED_MESSAGE,
    InvalidModelResponse,
    ModelCallFailed,
    ModelConfigurationError,
    ModelGateway,
)
from app.infrastructure.llm import gateway as gateway_module
from app.infrastructure.llm.gateway import (
    build_chat_client,
    build_dynamic_model_gateway,
    build_model_gateway,
)
from app.infrastructure.llm.provider_settings import (
    API_ANTHROPIC_MESSAGES,
    API_OPENAI_COMPATIBLE,
    ProviderConfig,
)


@tool
def read_active_plan() -> str:
    """读取当前 active 计划。"""
    return "plan"


@tool
def read_training_calendar(year: int, month: int) -> str:
    """读取指定月份的日程与训练事实。"""
    return f"{year}-{month}"


_OFFERED = (read_active_plan, read_training_calendar)

_TOOL_CALLS = [
    {
        "name": "read_training_calendar",
        "args": {"year": 2026, "month": 3},
        "id": "call-1",
        "type": "tool_call",
    }
]


class RecordingChat:
    """替身 Chat 客户端：记录 bind_tools 收到的 offered tuple 与每次传入的消息序列。

    只有 text／tools 两条路径：没有 with_structured_output，工具路径若走结构化输出会 AttributeError。
    """

    def __init__(self, *, response: Any) -> None:
        self.response = response
        self.offered: list[tuple[BaseTool, ...]] = []
        self.messages: list[Any] = []

    async def ainvoke(self, messages: Any) -> Any:
        self.messages.append(messages)
        return self.response

    def bind_tools(self, tools: Sequence[BaseTool]) -> Any:
        self.offered.append(tuple(tools))
        return type("Runnable", (), {"ainvoke": staticmethod(self.ainvoke)})()


def _full_config(**overrides: str) -> ProviderConfig:
    base = {
        "api_key": "k",
        "base_url": "https://example.test/v1",
        "model": "m",
    }
    base.update(overrides)
    return ProviderConfig(**base)


async def _unavailable(*_args: Any) -> Any:
    raise AssertionError("该形态在本次测试中不被调用")


def _scripted_gateway(monkeypatch: pytest.MonkeyPatch, response: Any) -> RecordingChat:
    chat = RecordingChat(response=response)
    monkeypatch.setattr(gateway_module, "build_chat_client", lambda *a, **k: chat)
    return chat


async def test_tools_offers_the_exact_tuple_and_passes_messages_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bind_tools 收到精确 offered tuple；消息序列原样传入；AIMessage 的 tool_calls 原样返回。"""
    response = AIMessage(content="", tool_calls=_TOOL_CALLS)
    chat = _scripted_gateway(monkeypatch, response)
    gateway = build_model_gateway(Path("unused"), _full_config())
    messages: list[BaseMessage] = [
        HumanMessage(content="这个月有哪些训练"),
    ]

    result = await gateway.tools(messages, list(_OFFERED))

    assert chat.offered == [_OFFERED]
    assert chat.messages == [messages]
    assert result is response
    assert result.tool_calls == _TOOL_CALLS


async def test_plain_ai_message_is_returned_with_empty_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最终文本答复同样是一个 AIMessage：内容原样返回，tool_calls 为空表示循环结束。"""
    _scripted_gateway(monkeypatch, AIMessage(content="本周没有已计划的训练日"))
    gateway = build_model_gateway(Path("unused"), _full_config())

    result = await gateway.tools([HumanMessage(content="今天练什么")], _OFFERED)

    assert isinstance(result, AIMessage)
    assert result.content == "本周没有已计划的训练日"
    assert result.tool_calls == []


async def test_non_ai_message_response_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """响应不是 AIMessage 时明确失败：不得从其它消息类型里猜工具调用。"""
    _scripted_gateway(monkeypatch, HumanMessage(content="今天练胸"))
    gateway = build_model_gateway(Path("unused"), _full_config())

    with pytest.raises(InvalidModelResponse):
        await gateway.tools([HumanMessage(content="今天练什么")], _OFFERED)


async def test_tools_shape_shares_the_client_and_does_not_bind_on_the_text_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """text 形态与 tools 形态复用同一个 client；text 路径不绑定工具。"""
    chat = _scripted_gateway(
        monkeypatch, AIMessage(content="回答", tool_calls=_TOOL_CALLS)
    )
    gateway = build_model_gateway(Path("unused"), _full_config())

    assert await gateway.text("系统提示", "用户载荷") == "回答"
    assert chat.offered == []

    result = await gateway.tools([HumanMessage(content="今天练什么")], _OFFERED)

    assert chat.offered == [_OFFERED]
    assert result.content == "回答"


@pytest.mark.parametrize(
    "api, expected_client_cls",
    [
        (API_OPENAI_COMPATIBLE, ChatOpenAI),
        (API_ANTHROPIC_MESSAGES, ChatAnthropic),
    ],
)
def test_bind_tools_follows_api_and_returns_a_bound_runnable(
    api: str, expected_client_cls: type
) -> None:
    """两个 api 各走自己 client 的 bind_tools()：不构造第二套客户端，构造与绑定都不发网络请求。"""
    client = build_chat_client(_full_config(api=api))

    assert type(client) is expected_client_cls
    assert isinstance(client.bind_tools(_OFFERED), RunnableBinding)


async def test_lazy_tools_resolves_the_provider_configuration_on_every_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """lazy 工具闭包每次调用都重新 resolve：Provider 热更新语义与 text／structured 一致。"""
    chat = RecordingChat(response=AIMessage(content="答复"))
    monkeypatch.setattr(gateway_module, "build_chat_client", lambda *a, **k: chat)
    seen: list[Path] = []

    def build(data_dir: Path) -> ModelGateway:
        seen.append(data_dir)
        return build_model_gateway(data_dir, _full_config())

    monkeypatch.setattr(gateway_module, "build_model_gateway", build)
    gateway = build_dynamic_model_gateway(tmp_path)
    messages = [HumanMessage(content="今天练什么")]

    first = await gateway.tools(messages, _OFFERED)
    second = await gateway.tools(messages, _OFFERED)

    assert seen == [tmp_path, tmp_path]
    assert (first.content, second.content) == ("答复", "答复")
    assert chat.offered == [_OFFERED, _OFFERED]


async def test_lazy_tools_keeps_configuration_error_and_wraps_call_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """lazy 工具闭包：配置错误原样抛出，Provider／调用异常统一转 ModelCallFailed。"""
    monkeypatch.setattr(
        gateway_module,
        "build_model_gateway",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("客户端构造失败")),
    )
    with pytest.raises(ModelCallFailed) as raised:
        await build_dynamic_model_gateway(tmp_path).tools(
            [HumanMessage(content="x")], _OFFERED
        )
    assert str(raised.value) == MODEL_CALL_FAILED_MESSAGE

    async def failing(
        messages: Sequence[BaseMessage], offered_tools: Sequence[BaseTool]
    ) -> AIMessage:
        raise RuntimeError("Provider 调用失败")

    monkeypatch.setattr(
        gateway_module,
        "build_model_gateway",
        lambda *a, **k: ModelGateway(
            text=_unavailable, structured=_unavailable, tools=failing
        ),
    )
    with pytest.raises(ModelCallFailed) as raised:
        await build_dynamic_model_gateway(tmp_path).tools(
            [HumanMessage(content="x")], _OFFERED
        )
    assert str(raised.value) == MODEL_CALL_FAILED_MESSAGE

    monkeypatch.setattr(
        gateway_module,
        "build_model_gateway",
        lambda *a, **k: (_ for _ in ()).throw(
            ModelConfigurationError("缺少模型配置")
        ),
    )
    with pytest.raises(ModelConfigurationError):
        await build_dynamic_model_gateway(tmp_path).tools(
            [HumanMessage(content="x")], _OFFERED
        )


async def test_lazy_tools_re_raises_invalid_model_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """非 AIMessage 响应属模型响应错误，不是 Provider 调用失败：lazy 入口原样上抛。"""

    async def wrong_shape(
        messages: Sequence[BaseMessage], offered_tools: Sequence[BaseTool]
    ) -> AIMessage:
        raise InvalidModelResponse("模型响应不是 AIMessage：无法读取原生工具调用")

    monkeypatch.setattr(
        gateway_module,
        "build_model_gateway",
        lambda *a, **k: ModelGateway(
            text=_unavailable, structured=_unavailable, tools=wrong_shape
        ),
    )

    with pytest.raises(InvalidModelResponse):
        await build_dynamic_model_gateway(tmp_path).tools(
            [HumanMessage(content="x")], _OFFERED
        )


def test_gateway_construction_points_are_exhaustive() -> None:
    """三个形态都是 dataclass 字段：漏填任一形态在构造期即失败，不留运行期半成品入口。"""
    with pytest.raises(TypeError):
        ModelGateway(text=_unavailable, structured=_unavailable)

    gateway = ModelGateway(
        text=_unavailable, structured=_unavailable, tools=_unavailable
    )

    assert callable(gateway.tools)
