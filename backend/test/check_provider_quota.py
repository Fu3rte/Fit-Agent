import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import anthropic
import httpx
import openai
import pytest

from app.agent.agent_loop import run_agent_loop
from app.agent.budget import RequestBudget
from app.agent.compaction import (
    CompactionPreparation,
    CompactionSummaryError,
    generate_compaction_summary,
)
from app.agent.config import AgentLoopConfig
from app.ai.messages import UserMessage
from app.ai.overflow import is_context_overflow_error
from app.ai.quota import (
    ProviderQuotaExceeded,
    is_provider_quota_error,
    raise_if_provider_quota_exceeded,
)
from app.ai.stream import complete
from app.ai.types import ModelSpec
from app.interfaces.http import PROVIDER_QUOTA_EXCEEDED_MESSAGE, terminal_decision
from app.model_config import ModelConfig

EVIDENCE = Path(__file__).resolve().parents[1] / "temp" / "provider-quota" / "evidence.json"
RECORDS: dict[str, object] = {}

OPENAI_URL = "https://example.invalid/v1/chat/completions"
ANTHROPIC_URL = "https://example.invalid/v1/messages"

# 明确账户配额耗尽：OpenAI 官方 429 结构化形态（type 与 code 均为 insufficient_quota）。
OPENAI_QUOTA_BODY = {
    "message": "You exceeded your current quota, please check your plan and billing details.",
    "type": "insufficient_quota",
    "param": None,
    "code": "insufficient_quota",
}
# 明确余额不足：OpenAI 兼容供应商 402，无结构化码，仅文本表达余额不足。
OPENAI_BALANCE_BODY = {
    "message": "Insufficient Balance",
    "type": "unknown_error",
    "param": None,
    "code": None,
}
# 普通临时限流：结构化码为 rate_limit_exceeded，type 为 requests，必须保持原分类。
OPENAI_RATE_LIMIT_BODY = {
    "message": "Rate limit reached for gpt-4 in organization org-x on requests per min.",
    "type": "requests",
    "param": None,
    "code": "rate_limit_exceeded",
}
# 明确余额不足：Anthropic 官方 400 文本（credit balance is too low），type 为 invalid_request_error。
ANTHROPIC_CREDIT_BODY = {
    "type": "error",
    "error": {
        "type": "invalid_request_error",
        "message": (
            "Your credit balance is too low to access the Anthropic API. "
            "Please go to Plans & Billing to upgrade or purchase credits."
        ),
    },
}


def sdk_error(cls, status: int, body: dict, *, url: str = OPENAI_URL):
    # 真实 SDK 异常类型：按 SDK 构造签名注入响应与已解析错误体，不伪造分类输入。
    response = httpx.Response(status, request=httpx.Request("POST", url))
    return cls(f"Error code: {status} - {body}", response=response, body=body)


def full_body(inner: dict) -> dict:
    return {"error": inner}


class QuotaServer:
    # 本地真实 HTTP 端点：按请求返回固定状态与 JSON 错误体，仅计数，无伪造模型逻辑。
    def __init__(self, status: int, body: dict):
        outer = self
        self.requests = 0
        payload = json.dumps(body, ensure_ascii=False).encode()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                outer.requests += 1
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def __enter__(self) -> "QuotaServer":
        self._thread.start()
        return self

    def __exit__(self, *_error) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


def spec(api: str, base_url: str) -> ModelSpec:
    return ModelSpec(
        api=api,
        provider="quota-test",
        id="quota-test",
        base_url=base_url,
        context_window=128000,
        max_tokens=16384,
    )


def context() -> dict:
    return {"messages": [UserMessage(role="user", content="hi", timestamp=0)]}


def loop_config(base_url: str, budget: RequestBudget) -> AgentLoopConfig:
    return AgentLoopConfig(
        model=ModelConfig(
            api="openai-completions",
            base_url=base_url,
            model="quota-test",
            provider="quota-test",
            api_key="test-key",
        ),
        max_turns=64,
        model_spec=spec("openai-completions", base_url),
        request_budget=budget,
    )


async def noop_emit(event) -> None:
    return None


def preparation() -> CompactionPreparation:
    return CompactionPreparation(
        first_kept_entry_id="kept-1",
        messages_to_summarize=[UserMessage(role="user", content="历史" * 100, timestamp=0)],
        turn_prefix_messages=[],
        is_split_turn=False,
        tokens_before=0,
        previous_summary=None,
        attachments=[],
    )


def run_stream(coro_factory):
    async def consume():
        return await coro_factory()

    return asyncio.run(consume())


def test_openai_quota_code_recognized() -> None:
    error = sdk_error(openai.RateLimitError, 429, OPENAI_QUOTA_BODY)
    assert error.status_code == 429
    assert error.code == "insufficient_quota" and error.type == "insufficient_quota"
    assert is_provider_quota_error(error) is True
    RECORDS["openai_insufficient_quota"] = {
        "type": type(error).__name__,
        "status_code": error.status_code,
        "code": error.code,
        "quota": True,
    }


def test_openai_balance_text_recognized() -> None:
    error = sdk_error(openai.APIStatusError, 402, OPENAI_BALANCE_BODY)
    assert error.status_code == 402 and error.code is None
    assert error.type == "unknown_error"
    assert is_provider_quota_error(error) is True
    RECORDS["openai_balance"] = {
        "type": type(error).__name__,
        "status_code": error.status_code,
        "code": error.code,
        "quota": True,
    }


def test_anthropic_credit_balance_recognized() -> None:
    error = sdk_error(
        anthropic.BadRequestError, 400, ANTHROPIC_CREDIT_BODY, url=ANTHROPIC_URL
    )
    assert error.status_code == 400
    assert error.type == "invalid_request_error"
    assert error.body["error"]["message"] in str(error.message)
    assert is_provider_quota_error(error) is True
    RECORDS["anthropic_credit_balance"] = {
        "type": type(error).__name__,
        "status_code": error.status_code,
        "type_field": error.type,
        "quota": True,
    }


def test_plain_rate_limit_stays_original() -> None:
    error = sdk_error(openai.RateLimitError, 429, OPENAI_RATE_LIMIT_BODY)
    assert error.code == "rate_limit_exceeded" and error.type == "requests"
    assert is_provider_quota_error(error) is False
    assert is_context_overflow_error(error) is False
    RECORDS["plain_rate_limit"] = {"code": error.code, "quota": False, "overflow": False}


def test_auth_permission_network_and_overflow_stay_original() -> None:
    auth = sdk_error(openai.AuthenticationError, 401, {"message": "invalid api key", "code": "invalid_api_key"})
    permission = sdk_error(
        anthropic.PermissionDeniedError,
        403,
        {"type": "error", "error": {"type": "permission_error", "message": "permission denied"}},
        url=ANTHROPIC_URL,
    )
    connection = openai.APIConnectionError(request=httpx.Request("POST", OPENAI_URL))
    timeout = anthropic.APITimeoutError(request=httpx.Request("POST", ANTHROPIC_URL))
    overflow = sdk_error(
        openai.BadRequestError,
        400,
        {"message": "This model's maximum context length is 128000 tokens.", "code": "context_length_exceeded"},
    )
    param = sdk_error(openai.BadRequestError, 400, {"message": "Invalid parameter: max_tokens must be positive", "code": "invalid_parameter"})
    for error in (auth, permission, connection, timeout, overflow, param):
        assert is_provider_quota_error(error) is False, repr(error)
    assert is_context_overflow_error(overflow) is True
    RECORDS["excluded"] = {
        "auth": type(auth).__name__,
        "permission": type(permission).__name__,
        "connection": type(connection).__name__,
        "timeout": type(timeout).__name__,
        "overflow": is_context_overflow_error(overflow),
        "param_quota": is_provider_quota_error(param),
    }


def test_non_sdk_errors_never_classified() -> None:
    # 请求转换、usage 校验与工具参数校验错误：非 SDK 供应商错误，即使文本含额度词也不分类。
    for text in ("insufficient_quota", "余额不足", "Insufficient Balance"):
        assert is_provider_quota_error(ValueError(text)) is False
        assert is_provider_quota_error(RuntimeError(text)) is False


def test_boundary_raises_shared_exception_with_chain() -> None:
    error = sdk_error(openai.RateLimitError, 429, OPENAI_QUOTA_BODY)
    with pytest.raises(ProviderQuotaExceeded) as info:
        raise_if_provider_quota_exceeded(error)
    assert info.value.__cause__ is error
    # 非额度错误原样返回，不转换。
    plain = sdk_error(openai.RateLimitError, 429, OPENAI_RATE_LIMIT_BODY)
    assert raise_if_provider_quota_exceeded(plain) is None


def test_openai_adapter_propagates_quota() -> None:
    with QuotaServer(429, full_body(OPENAI_QUOTA_BODY)) as server:
        model = spec("openai-completions", server.base_url)
        with pytest.raises(ProviderQuotaExceeded) as info:
            run_stream(lambda: complete(model, context(), {"api_key": "test-key", "max_tokens": 64}))
        assert isinstance(info.value.__cause__, openai.RateLimitError)
        assert server.requests == 1
    RECORDS["openai_adapter"] = {"requests": server.requests, "cause": "openai.RateLimitError"}


def test_anthropic_adapter_propagates_quota() -> None:
    with QuotaServer(400, ANTHROPIC_CREDIT_BODY) as server:
        model = spec("anthropic-messages", server.base_url)
        with pytest.raises(ProviderQuotaExceeded) as info:
            run_stream(lambda: complete(model, context(), {"api_key": "test-key", "max_tokens": 64}))
        assert isinstance(info.value.__cause__, anthropic.BadRequestError)
        assert server.requests == 1
    RECORDS["anthropic_adapter"] = {"requests": server.requests, "cause": "anthropic.BadRequestError"}


def test_sdk_retries_disabled_quota_terminates_immediately() -> None:
    # 429 为可重试状态：max_retries=0 时只发起一次请求；额度错误立即终止，不重试、不换服务。
    openai_body = full_body(OPENAI_QUOTA_BODY)
    anthropic_body = {
        "type": "error",
        "error": {"type": "rate_limit_error", "message": "Your credit balance is too low to access the Anthropic API."},
    }
    with QuotaServer(429, openai_body) as openai_server:
        model = spec("openai-completions", openai_server.base_url)
        with pytest.raises(ProviderQuotaExceeded):
            run_stream(lambda: complete(model, context(), {"api_key": "test-key", "max_tokens": 64}))
        assert openai_server.requests == 1
    with QuotaServer(429, anthropic_body) as anthropic_server:
        model = spec("anthropic-messages", anthropic_server.base_url)
        with pytest.raises(ProviderQuotaExceeded):
            run_stream(lambda: complete(model, context(), {"api_key": "test-key", "max_tokens": 64}))
        assert anthropic_server.requests == 1
    RECORDS["retries_disabled"] = {
        "openai_requests": openai_server.requests,
        "anthropic_requests": anthropic_server.requests,
    }


def test_normal_run_and_summary_share_same_exception() -> None:
    with QuotaServer(429, full_body(OPENAI_QUOTA_BODY)) as server:
        budget = RequestBudget()
        with pytest.raises(ProviderQuotaExceeded) as normal:
            asyncio.run(
                run_agent_loop(
                    [UserMessage(role="user", content="go", timestamp=0)],
                    {"messages": [], "tools": {}},
                    loop_config(server.base_url, budget),
                    noop_emit,
                )
            )
        assert budget.used == 1
        summary_budget = RequestBudget()
        with pytest.raises(ProviderQuotaExceeded) as summary:
            asyncio.run(
                generate_compaction_summary(
                    preparation(),
                    spec("openai-completions", server.base_url),
                    api_key="test-key",
                    budget=summary_budget,
                )
            )
        assert summary_budget.used == 1
        # 摘要不得把额度错误转成普通摘要失败。
        assert not isinstance(summary.value, CompactionSummaryError)
        assert normal.value.__class__ is summary.value.__class__ is ProviderQuotaExceeded
        assert server.requests == 2
    RECORDS["normal_and_summary"] = {
        "normal_used": budget.used,
        "summary_used": summary_budget.used,
        "requests": server.requests,
    }


def test_quota_maps_to_public_terminal_decision() -> None:
    failure = ProviderQuotaExceeded("模型供应商账户额度不足或配额耗尽")
    status, code, message = terminal_decision(failure, False)
    assert (status, code, message) == ("failed", "provider_quota_exceeded", PROVIDER_QUOTA_EXCEEDED_MESSAGE)


def test_public_output_is_credential_safe() -> None:
    api_key = "sk-secret-quota-key-0001"
    error = sdk_error(openai.RateLimitError, 429, OPENAI_QUOTA_BODY)
    with pytest.raises(ProviderQuotaExceeded) as info:
        raise_if_provider_quota_exceeded(error)
    _, _, message = terminal_decision(info.value, False)
    assert message == PROVIDER_QUOTA_EXCEEDED_MESSAGE
    for secret in (api_key, "sk-secret", OPENAI_QUOTA_BODY["message"], "example.invalid"):
        assert secret not in message
        assert secret not in str(info.value)
    RECORDS["credential_safe"] = {
        "message": message,
        "leaks_api_key": api_key in message,
        "leaks_body": OPENAI_QUOTA_BODY["message"] in message,
    }


def check() -> None:
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    test_openai_quota_code_recognized()
    test_openai_balance_text_recognized()
    test_anthropic_credit_balance_recognized()
    test_plain_rate_limit_stays_original()
    test_auth_permission_network_and_overflow_stay_original()
    test_non_sdk_errors_never_classified()
    test_boundary_raises_shared_exception_with_chain()
    test_openai_adapter_propagates_quota()
    test_anthropic_adapter_propagates_quota()
    test_sdk_retries_disabled_quota_terminates_immediately()
    test_normal_run_and_summary_share_same_exception()
    test_quota_maps_to_public_terminal_decision()
    test_public_output_is_credential_safe()
    EVIDENCE.write_text(json.dumps(RECORDS, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "供应商额度：OpenAI 结构化 insufficient_quota、402 余额不足、"
        "Anthropic 400 credit balance 明确识别；普通 429 限流、"
        "鉴权、权限、网络、上下文溢出与参数错误保持原分类；"
        "非 SDK 错误不分类；边界抛出共享异常并保留原链；"
        "两协议 adapter 传播同一异常；429 只请求一次证明重试关闭；"
        "普通与摘要调用共享同一异常；公开终态文案凭据安全"
    )


if __name__ == "__main__":
    check()
