# 模型入口的两种形态与 Provider 探针：客户端类只由 api 选、结构化 kwargs 只由 structured_output 选，
# 探针把「连接／鉴权失败」与「未通过所选结构化输出能力探针」区分成两句固定文案。全部用替身模型，不联网。

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from api import routes_provider
from api.app import _lazy_model_call
from graph import model as model_module
from graph.model import (
    MODEL_CALL_FAILED_MESSAGE,
    STRUCTURED_PROBE_SYSTEM_PROMPT,
    STRUCTURED_PROBE_USER_PAYLOAD,
    InvalidModelResponse,
    ModelCallFailed,
    StructuredProbeResult,
    build_chat_client,
    build_model_gateway,
    probe_structured_model_call,
)
from provider_settings import (
    API_ANTHROPIC_MESSAGES,
    API_OPENAI_COMPATIBLE,
    STRUCTURED_OUTPUT_FUNCTION_CALLING_STRICT,
    STRUCTURED_OUTPUT_JSON_SCHEMA,
    ModelConfigurationError,
    ProviderConfig,
)

_OPENAI_JSON_SCHEMA_KWARGS = {"method": "json_schema", "strict": True}
_OPENAI_FUNCTION_CALLING_KWARGS = {"method": "function_calling", "strict": True}
_ANTHROPIC_JSON_SCHEMA_KWARGS = {"method": "json_schema"}


#: 结构化替身未显式指定返回值时的哨兵：默认仍走目标 Schema 的校验结果。
_UNSET = object()


class RecordingChat:
    """替身 Chat 客户端：记录传入的消息与 with_structured_output 的关键字。"""

    def __init__(self, *, content: Any = "OK", structured_result: Any = _UNSET) -> None:
        self.content = content
        self.structured_result = structured_result
        self.messages: list[list[Any]] = []
        self.structured_calls: list[tuple[Any, Mapping[str, Any]]] = []

    async def ainvoke(self, messages: Sequence[Any]) -> Any:
        self.messages.append(list(messages))
        return type("Response", (), {"content": self.content})()

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:
        self.structured_calls.append((schema, kwargs))

        async def invoke(messages: Sequence[Any]) -> Any:
            self.messages.append(list(messages))
            if self.structured_result is not _UNSET:
                return self.structured_result
            return schema.model_validate(
                {"ok": True, "note": None, "detail": {"label": "probe"}}
            )

        return type("Runnable", (), {"ainvoke": staticmethod(invoke)})()


def _full_config(**overrides: str) -> ProviderConfig:
    base = {
        "api_key": "k",
        "base_url": "https://example.test/v1",
        "model": "m",
    }
    base.update(overrides)
    return ProviderConfig(**base)


@pytest.fixture
def recording_chat(monkeypatch: pytest.MonkeyPatch) -> RecordingChat:
    chat = RecordingChat()
    monkeypatch.setattr(model_module, "build_chat_client", lambda *a, **k: chat)
    return chat


@pytest.mark.parametrize(
    "api, structured_output, expected_kwargs",
    [
        (
            API_OPENAI_COMPATIBLE,
            STRUCTURED_OUTPUT_JSON_SCHEMA,
            _OPENAI_JSON_SCHEMA_KWARGS,
        ),
        (
            API_OPENAI_COMPATIBLE,
            STRUCTURED_OUTPUT_FUNCTION_CALLING_STRICT,
            _OPENAI_FUNCTION_CALLING_KWARGS,
        ),
        (
            API_ANTHROPIC_MESSAGES,
            STRUCTURED_OUTPUT_JSON_SCHEMA,
            _ANTHROPIC_JSON_SCHEMA_KWARGS,
        ),
    ],
)
async def test_structured_kwargs_follow_structured_output_only(
    recording_chat: RecordingChat,
    api: str,
    structured_output: str,
    expected_kwargs: dict[str, Any],
) -> None:
    """三个合法组合各自锁定唯一的 with_structured_output kwargs。"""
    gateway = build_model_gateway(
        Path("unused"),
        _full_config(api=api, structured_output=structured_output),
    )

    result = await gateway.structured("系统提示", "用户载荷", StructuredProbeResult)

    assert isinstance(result, StructuredProbeResult)
    assert recording_chat.structured_calls == [
        (StructuredProbeResult, expected_kwargs)
    ]
    system, human = recording_chat.messages[0]
    assert isinstance(system, SystemMessage) and system.content == "系统提示"
    assert isinstance(human, HumanMessage) and human.content == "用户载荷"


@pytest.mark.parametrize(
    "api, expected_client_cls",
    [
        (API_OPENAI_COMPATIBLE, ChatOpenAI),
        (API_ANTHROPIC_MESSAGES, ChatAnthropic),
    ],
)
def test_client_class_follows_api_only(
    api: str, expected_client_cls: type
) -> None:
    """客户端类只由 api 决定，自建兼容端点同样走 ChatOpenAI（构造不发网络请求）。"""
    client = build_chat_client(
        _full_config(api=api, base_url="https://self-hosted.test/v1")
    )

    assert isinstance(client, expected_client_cls)
    assert str(client.openai_api_base if api == API_OPENAI_COMPATIBLE else client.anthropic_api_url) == (
        "https://self-hosted.test/v1"
    )


def test_client_uses_openai_compatible_for_function_calling_strict() -> None:
    """组合里没有 DeepSeek 专属客户端：function_calling_strict 仍由 ChatOpenAI 承载。"""
    client = build_chat_client(
        _full_config(
            api=API_OPENAI_COMPATIBLE,
            structured_output=STRUCTURED_OUTPUT_FUNCTION_CALLING_STRICT,
        )
    )

    assert type(client) is ChatOpenAI
    assert client.openai_api_base == "https://example.test/v1"


def test_unknown_structured_output_fails_at_resolve(tmp_path: Path) -> None:
    """非法组合在 resolve 阶段就地抛 ModelConfigurationError，不构造客户端。"""
    with pytest.raises(ModelConfigurationError):
        build_model_gateway(
            tmp_path,
            _full_config(
                api=API_ANTHROPIC_MESSAGES,
                structured_output=STRUCTURED_OUTPUT_FUNCTION_CALLING_STRICT,
            ),
        )


async def test_structured_call_rejects_result_outside_the_schema(
    recording_chat: RecordingChat,
) -> None:
    """Provider 未返回目标 Schema 实例时明确失败：None 不得被当作合法结构化结果。"""
    recording_chat.structured_result = None
    gateway = build_model_gateway(Path("unused"), _full_config())

    with pytest.raises(InvalidModelResponse):
        await gateway.structured("系统提示", "用户载荷", StructuredProbeResult)


async def test_text_call_returns_text_and_rejects_other_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """文本调用返回模型文本；响应不是纯文本时明确失败。"""
    recording_chat = RecordingChat(content="你好")
    monkeypatch.setattr(
        model_module, "build_chat_client", lambda *a, **k: recording_chat
    )
    gateway = build_model_gateway(Path("unused"), _full_config())

    assert await gateway.text("系统提示", "用户载荷") == "你好"

    non_text = RecordingChat(content=[{"type": "text", "text": "你好"}])
    monkeypatch.setattr(
        model_module, "build_chat_client", lambda *a, **k: non_text
    )
    with pytest.raises(InvalidModelResponse):
        await build_model_gateway(Path("unused"), _full_config()).text("s", "u")


def test_probe_schema_has_required_nullable_and_nested_object() -> None:
    """探针 Schema 必须同时含必填字段、nullable 字段与嵌套 extra=forbid 对象。"""
    schema = StructuredProbeResult.model_json_schema()

    assert schema["required"] == ["ok", "detail"]
    assert schema["additionalProperties"] is False
    assert {"type": "string"} in schema["properties"]["note"]["anyOf"]
    assert schema["properties"]["detail"]["$ref"].endswith(
        "/StructuredProbeDetail"
    )
    nested = schema["$defs"]["StructuredProbeDetail"]
    assert nested["additionalProperties"] is False
    assert nested["required"] == ["label"]


async def test_structured_probe_uses_the_hardened_target_schema(
    recording_chat: RecordingChat,
) -> None:
    """探针是两次独立调用中的第二次：目标 Schema 为强化后的 StructuredProbeResult。"""
    await probe_structured_model_call(Path("unused"), _full_config())

    assert recording_chat.structured_calls == [
        (StructuredProbeResult, _OPENAI_JSON_SCHEMA_KWARGS)
    ]
    system, human = recording_chat.messages[0]
    assert system.content == STRUCTURED_PROBE_SYSTEM_PROMPT
    assert human.content == STRUCTURED_PROBE_USER_PAYLOAD


async def test_lazy_gateway_keeps_configuration_error_and_wraps_call_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """lazy 入口：配置错误原样抛出，客户端／调用异常统一转 ModelCallFailed。"""
    model = _lazy_model_call(tmp_path)

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("客户端构造失败")

    monkeypatch.setattr("api.app.build_model_gateway", _boom)
    with pytest.raises(ModelCallFailed) as raised:
        await model.text("s", "u")
    assert str(raised.value) == MODEL_CALL_FAILED_MESSAGE

    with pytest.raises(ModelCallFailed):
        await model.structured("s", "u", StructuredProbeResult)

    monkeypatch.setattr(
        "api.app.build_model_gateway",
        lambda *a, **k: (_ for _ in ()).throw(
            ModelConfigurationError("缺少模型配置")
        ),
    )
    with pytest.raises(ModelConfigurationError):
        await model.text("s", "u")
    with pytest.raises(ModelConfigurationError):
        await model.structured("s", "u", StructuredProbeResult)


def _client(data_dir: Path) -> TestClient:
    app = FastAPI()
    app.state.data_dir = data_dir
    app.include_router(routes_provider.router)
    return TestClient(app)


def _body() -> dict[str, str]:
    return {"api_key": "k", "base_url": "https://example.test/v1", "model": "m"}


@pytest.mark.parametrize(
    "text_probe_fails, structured_probe_fails, expected_message",
    [
        (False, False, routes_provider.PROBE_OK_MESSAGE),
        (False, True, routes_provider.PROBE_STRUCTURED_FAILED_MESSAGE),
        (True, False, routes_provider.PROBE_FAILED_MESSAGE),
    ],
)
def test_provider_test_separates_connectivity_from_structured_capability(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    text_probe_fails: bool,
    structured_probe_fails: bool,
    expected_message: str,
) -> None:
    """探针结果区分连接／鉴权失败与未通过结构化输出能力探针两类可见文案。"""
    called: list[str] = []

    async def text_probe(data_dir: Path, override: Any = None) -> None:
        called.append("text")
        if text_probe_fails:
            raise RuntimeError("连接失败")

    async def structured_probe(data_dir: Path, override: Any = None) -> None:
        called.append("structured")
        if structured_probe_fails:
            raise RuntimeError("未通过所选结构化输出能力探针")

    monkeypatch.setattr(routes_provider, "probe_model_call", text_probe)
    monkeypatch.setattr(routes_provider, "probe_structured_model_call", structured_probe)

    with _client(tmp_path) as client:
        response = client.post("/api/provider/test", json=_body())

    assert response.status_code == 200
    payload = response.json()
    assert payload["message"] == expected_message
    assert payload["ok"] is (expected_message == routes_provider.PROBE_OK_MESSAGE)
    assert payload["latency_ms"] is None or isinstance(payload["latency_ms"], int)
    if text_probe_fails:
        assert called == ["text"]
    else:
        assert called == ["text", "structured"]


def test_provider_test_does_not_write_the_saved_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """测试只探针，不落盘 provider.json。"""

    async def probe(data_dir: Path, override: Any = None) -> None:
        return None

    monkeypatch.setattr(routes_provider, "probe_model_call", probe)
    monkeypatch.setattr(routes_provider, "probe_structured_model_call", probe)

    with _client(tmp_path) as client:
        assert client.post("/api/provider/test", json=_body()).json()["ok"] is True

    assert not (tmp_path / "provider.json").exists()


def test_provider_get_put_delete_roundtrip(tmp_path: Path) -> None:
    """GET／PUT／DELETE 三个端点的五字段形状：PUT 整份写入，DELETE 回落默认且不回显 Key。"""
    with _client(tmp_path) as client:
        assert client.get("/api/provider").json() == {
            "has_api_key": False,
            "base_url": "",
            "model": "",
            "api": API_OPENAI_COMPATIBLE,
            "structured_output": STRUCTURED_OUTPUT_JSON_SCHEMA,
        }

        put = client.put(
            "/api/provider",
            json={
                **_body(),
                "api": API_OPENAI_COMPATIBLE,
                "structured_output": STRUCTURED_OUTPUT_FUNCTION_CALLING_STRICT,
            },
        )
        assert put.status_code == 200
        assert put.json() == {
            "has_api_key": True,
            "base_url": "https://example.test/v1",
            "model": "m",
            "api": API_OPENAI_COMPATIBLE,
            "structured_output": STRUCTURED_OUTPUT_FUNCTION_CALLING_STRICT,
        }
        assert client.get("/api/provider").json() == put.json()

        delete = client.delete("/api/provider")
        assert delete.status_code == 200
        assert delete.json() == {
            "has_api_key": False,
            "base_url": "",
            "model": "",
            "api": API_OPENAI_COMPATIBLE,
            "structured_output": STRUCTURED_OUTPUT_JSON_SCHEMA,
        }
        assert client.delete("/api/provider").json() == delete.json()


def test_provider_put_empty_api_key_keeps_stored_key(tmp_path: Path) -> None:
    """空串 api_key 沿用已存值：重开弹层未重贴 Key 的保存不得抹掉凭据。"""
    with _client(tmp_path) as client:
        client.put("/api/provider", json=_body())
        put = client.put(
            "/api/provider",
            json={"api_key": "", "base_url": "https://other.test/v1", "model": "m2"},
        )
        stored = client.get("/api/provider").json()

    assert put.json() == stored
    assert stored["has_api_key"] is True
    assert (stored["base_url"], stored["model"]) == ("https://other.test/v1", "m2")


def test_provider_put_defaults_protocol_pair_when_absent(tmp_path: Path) -> None:
    """PUT 省略两个协议字段时写默认组合并回显。"""
    with _client(tmp_path) as client:
        put = client.put("/api/provider", json=_body())

        assert put.status_code == 200
        assert (put.json()["api"], put.json()["structured_output"]) == (
            API_OPENAI_COMPATIBLE,
            STRUCTURED_OUTPUT_JSON_SCHEMA,
        )
        assert client.get("/api/provider").json() == put.json()


@pytest.mark.parametrize(
    "protocol_fields",
    [
        {"api": "bogus_api"},
        {"structured_output": "bogus_output"},
        {"api": "openai", "structured_output": "json_schema"},
        {"structured_protocol": "openai_chat_json_schema"},
    ],
)
def test_provider_put_rejects_unknown_protocol_values(
    tmp_path: Path, protocol_fields: dict[str, str]
) -> None:
    """body 边界用 Literal 拒绝未知值与旧字段名，不落盘。"""
    with _client(tmp_path) as client:
        response = client.put("/api/provider", json={**_body(), **protocol_fields})

    assert response.status_code == 422
    assert not (tmp_path / "provider.json").exists()


def test_provider_put_rejects_illegal_combination(tmp_path: Path) -> None:
    """非法组合在写边界被拒（400），磁盘上不留坏配置。"""
    app = FastAPI()
    app.state.data_dir = tmp_path
    app.include_router(routes_provider.router)
    from api import dto

    dto.install_error_handlers(app)

    with TestClient(app) as client:
        response = client.put(
            "/api/provider",
            json={
                **_body(),
                "api": API_ANTHROPIC_MESSAGES,
                "structured_output": STRUCTURED_OUTPUT_FUNCTION_CALLING_STRICT,
            },
        )

    assert response.status_code == 400
    assert not (tmp_path / "provider.json").exists()


def test_provider_test_follows_saved_protocol_when_body_omits_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """探针 body 省略协议字段时，resolve 沿用 provider.json 已存组合，不强制回落默认。"""
    from provider_settings import resolve_provider_config

    seen: list[Any] = []

    def _resolved_pair(data_dir: Path, override: Any = None) -> tuple[str, str]:
        config = resolve_provider_config(data_dir, override)
        return (config.api, config.structured_output)

    async def text_probe(data_dir: Path, override: Any = None) -> None:
        seen.append(("text", _resolved_pair(data_dir, override)))

    async def structured_probe(data_dir: Path, override: Any = None) -> None:
        seen.append(("structured", _resolved_pair(data_dir, override)))

    monkeypatch.setattr(routes_provider, "probe_model_call", text_probe)
    monkeypatch.setattr(routes_provider, "probe_structured_model_call", structured_probe)

    with _client(tmp_path) as client:
        client.put(
            "/api/provider",
            json={
                **_body(),
                "api": API_ANTHROPIC_MESSAGES,
                "structured_output": STRUCTURED_OUTPUT_JSON_SCHEMA,
            },
        )
        response = client.post("/api/provider/test", json=_body())

    assert response.json()["ok"] is True
    assert seen == [
        ("text", (API_ANTHROPIC_MESSAGES, STRUCTURED_OUTPUT_JSON_SCHEMA)),
        ("structured", (API_ANTHROPIC_MESSAGES, STRUCTURED_OUTPUT_JSON_SCHEMA)),
    ]
