import json
import os
from collections.abc import Mapping
from uuid import uuid4

from app import model_config
from app.interfaces.http import ProviderWriteBody, app
from test.check_http import create_session
from test.regression_support import (
    Server,
    client,
    install_test_model_config,
    patch_default_database,
    temporary_root,
)

ROOT = temporary_root("provider-diagnostic") / uuid4().hex
ROOT.mkdir(parents=True)


def anthropic_body(environ: Mapping[str, str] | None = None) -> dict | None:
    source = os.environ if environ is None else environ
    base_url = source.get("FIT_AGENT_TEST_ANTHROPIC_BASE_URL")
    identifier = source.get("FIT_AGENT_TEST_ANTHROPIC_ID")
    api_key = source.get("FIT_AGENT_TEST_ANTHROPIC_API_KEY")
    if not (base_url and identifier and api_key):
        return None
    body = {
        "api": "anthropic-messages",
        "base_url": base_url,
        "model": identifier,
        "api_key": api_key,
    }
    # 服务标识可选：环境变量缺失时省略该字段，交由后端按协议计算（§1、§2）
    provider = source.get("FIT_AGENT_TEST_ANTHROPIC_PROVIDER")
    if provider:
        body["provider"] = provider
    return body


def check_request_body() -> dict:
    # 仅设置 Base URL、模型 ID 与 API Key 时，请求体省略 provider 并通过后端校验（§1、§2）。
    # 该检查不发起模型调用，不依赖真实凭据。
    body = anthropic_body(
        {
            "FIT_AGENT_TEST_ANTHROPIC_BASE_URL": "https://api.anthropic.com",
            "FIT_AGENT_TEST_ANTHROPIC_ID": "claude-3-5-sonnet-latest",
            "FIT_AGENT_TEST_ANTHROPIC_API_KEY": "sk-body-check",
        }
    )
    assert body is not None
    assert body == {
        "api": "anthropic-messages",
        "base_url": "https://api.anthropic.com",
        "model": "claude-3-5-sonnet-latest",
        "api_key": "sk-body-check",
    }
    assert ProviderWriteBody.model_validate(body).provider == ""
    return body


def expect_diagnostic(response, ok: bool, secret: str) -> dict:
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert set(result) == {"ok", "latency_ms", "message"}
    assert result["ok"] is ok, result
    assert type(result["latency_ms"]) is int and result["latency_ms"] >= 0, result
    assert isinstance(result["message"], str) and result["message"]
    assert secret not in json.dumps(result, ensure_ascii=False)
    return result


def check() -> None:
    original_root = model_config.DATA_ROOT
    patch_default_database("provider-diagnostic")
    evidence: dict = {"anthropic-request-body": check_request_body()}
    try:
        config = install_test_model_config(ROOT / "config")
        status_before = model_config.load_provider_status().model_dump()
        models_bytes = (model_config.DATA_ROOT / "models.json").read_bytes()
        auth_bytes = (model_config.DATA_ROOT / "auth.json").read_bytes()
        with Server(app) as server, client(server.base_url, timeout=180) as http:
            session = str(uuid4())
            create_session(http, session, "诊断会话")
            profile_before = http.get("/api/profile").json()
            plans_before = http.get("/api/plans").json()

            body = {
                "api": config.api,
                "base_url": config.base_url,
                "model": config.model,
                "api_key": config.api_key,
                "provider": config.provider,
            }
            # 15/16. 真实流式响应与原生工具调用诊断，报告耗时。
            success = expect_diagnostic(
                http.post("/api/provider/test", json=body), True, config.api_key
            )
            evidence["openai-completions"] = success

            # 16. 失败诊断同样报告非负耗时。
            failure = expect_diagnostic(
                http.post(
                    "/api/provider/test",
                    json={**body, "base_url": "http://127.0.0.1:9/v1"},
                ),
                False,
                config.api_key,
            )
            evidence["failure"] = failure

            # 17. 诊断不修改配置、会话与业务数据。
            assert model_config.load_provider_status().model_dump() == status_before
            assert (model_config.DATA_ROOT / "models.json").read_bytes() == models_bytes
            assert (model_config.DATA_ROOT / "auth.json").read_bytes() == auth_bytes
            assert http.get("/api/profile").json() == profile_before
            assert http.get("/api/plans").json() == plans_before
            history = http.get(f"/api/sessions/{session}/history").json()
            assert not any(
                item["message"]["role"] == "assistant" for item in history["entries"]
            )

        anthropic = anthropic_body()
        if anthropic is None:
            evidence["anthropic-messages"] = "blocked: 缺少测试环境变量"
            print("Anthropic Messages 真实诊断未执行：缺少测试环境变量（记录为阻断项）")
        else:
            with Server(app) as server, client(server.base_url, timeout=180) as http:
                result = expect_diagnostic(
                    http.post("/api/provider/test", json=anthropic),
                    True,
                    anthropic["api_key"],
                )
                evidence["anthropic-messages"] = result
    finally:
        model_config.DATA_ROOT = original_root
    (ROOT / "provider-diagnostic.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("真实协议适配器诊断：流式响应、终止状态、原生工具调用名称与参数、耗时与无副作用检查通过")


if __name__ == "__main__":
    check()
