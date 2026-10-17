import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from time import time_ns
from uuid import uuid4

import pytest
from pydantic import BaseModel

from app import model_config
from app.agent.agent_loop import ModelOutputLimitExceeded, run_agent_loop
from app.agent.budget import (
    MAX_MODEL_REQUESTS,
    ModelRequestLimitExceeded,
    RequestBudget,
)
from app.agent.compaction import (
    CompactionPreparation,
    ContextBudgetExceeded,
    NoSummarizableHistory,
    generate_compaction_summary,
)
from app.agent.config import AgentLoopConfig, CompactionHooks, LoopCompactionResult
from app.agent.tool import AgentTool, AgentToolResult
from app.ai.messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from app.ai.overflow import ContextOverflowError
from app.ai.quota import ProviderQuotaExceeded
from app.ai.stream import AssistantResponse
from app.ai.types import ModelSpec
from app.interfaces.http import (
    CONTEXT_BUDGET_EXCEEDED_MESSAGE,
    MODEL_OUTPUT_LIMIT_MESSAGE,
    PROVIDER_QUOTA_EXCEEDED_MESSAGE,
    terminal_decision,
)
from app.interfaces.http import app as http_app
from app.model_config import ModelConfig
from test.check_http import events as read_events
from test.check_http import wait_idle
from test.regression_support import Server, client, patch_default_database

# 本文件验证 Agent 调度的请求计数与压缩调度：真实 Agent-loop、真实 should_compact、
# 真实 RequestBudget 与终态裁决。模型侧与装配层的摘要入口通过 CompactionHooks 注入，
# 真实模型与真实摘要发送入口的联合验收见任务 3 完成后的联合验证。
EVIDENCE = Path(__file__).resolve().parents[1] / "temp" / "model-request-dispatch" / "evidence.json"
RECORDS: dict[str, object] = {}


def spec() -> ModelSpec:
    return ModelSpec(
        api="openai-completions",
        provider="test",
        id="test",
        base_url="https://example.invalid/v1",
        context_window=128000,
        max_tokens=16384,
    )


def dispatch_model_config() -> ModelConfig:
    return ModelConfig(
        api="openai-completions",
        base_url="https://example.invalid/v1",
        model="test",
        provider="test",
        api_key="secret",
    )


def usage() -> Usage:
    return Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=12)


def assistant(content, stop_reason="stop", **extra) -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=content,
        api="openai-completions",
        provider="test",
        model="test",
        usage=usage(),
        stop_reason=stop_reason,
        timestamp=time_ns() // 1_000_000,
        **extra,
    )


def text_message(text="done") -> AssistantMessage:
    return assistant([TextContent(type="text", text=text)])


def tool_call_message(call_id="call-1") -> AssistantMessage:
    return assistant(
        [ToolCall(type="toolCall", id=call_id, name="echo", arguments={"text": "x"})],
        stop_reason="toolUse",
    )


def overflow_message() -> AssistantMessage:
    return assistant(
        [], stop_reason="error", error_message="上下文容量溢出", context_overflow=True
    )


def user(text: str) -> UserMessage:
    return UserMessage(role="user", content=text, timestamp=time_ns() // 1_000_000)


class EchoArguments(BaseModel):
    text: str


def _echo(tool_call_id, arguments, signal, on_update) -> AgentToolResult:
    return AgentToolResult([TextContent(type="text", text=f"echo:{arguments.text}")])


ECHO = AgentTool(name="echo", description="回显文本", arguments=EchoArguments, execute=_echo)


def tool_context() -> dict:
    system = SystemMessage(
        role="system", content="", tools_added=[ECHO.definition()], timestamp=0
    )
    return {"messages": [system], "tools": {"echo": ECHO}}


def plain_context() -> dict:
    return {"messages": [], "tools": {}}


def loop_config(**overrides) -> AgentLoopConfig:
    base = {"model": dispatch_model_config(), "max_turns": 64, "model_spec": spec()}
    base.update(overrides)
    return AgentLoopConfig(**base)


async def noop_emit(event) -> None:
    return None


def collector():
    events: list[dict] = []

    async def emit(event) -> None:
        events.append(event)

    return events, emit


def scripted(messages):
    queue = list(messages)
    state = {"calls": 0}

    def stream_fn(_spec, _context, _options) -> AssistantResponse:
        # 模拟真实发送边界：生产扣除位于 app.ai.stream 的公共重试入口。
        budget = _options.get("budget")
        if budget is not None:
            budget.reserve(recovery=bool(_options.get("recovery")))
        message = queue[state["calls"]]
        state["calls"] += 1

        async def source():
            partial = message.model_copy(
                update={"content": [], "stop_reason": "pending", "usage": None}
            )
            yield {"type": "start", "partial": partial}
            yield {"type": "done", "reason": message.stop_reason, "message": message}

        return AssistantResponse(source())

    return stream_fn, state


def make_hooks(estimates, fail: BaseException | None = None):
    remaining = list(estimates)
    state = {"compactions": []}

    async def estimate_request() -> int:
        return remaining.pop(0)

    async def compact(compaction_id: str, signal, span) -> LoopCompactionResult:
        state["compactions"].append(compaction_id)
        if fail is not None:
            raise fail
        return LoopCompactionResult(entry_id=compaction_id, parent_id="parent", messages=[])

    hooks = CompactionHooks(
        estimate_request=estimate_request,
        compact=compact,
        context_window=128000,
        reserve_tokens=16384,
    )
    return hooks, state


def run_loop(prompts, context, config, stream_fn):
    events, emit = collector()
    messages = asyncio.run(run_agent_loop(prompts, context, config, emit, stream_fn=stream_fn))
    return messages, events


def test_normal_request_reserves_once() -> None:
    budget = RequestBudget()
    stream_fn, state = scripted([text_message()])
    run_loop([user("hi")], plain_context(), loop_config(request_budget=budget), stream_fn)
    assert budget.used == 1 and state["calls"] == 1
    RECORDS["normal_request"] = {"used": budget.used, "sends": state["calls"]}


def test_rejection_before_send_does_not_consume() -> None:
    budget = RequestBudget(limit=1)
    budget.reserve()
    stream_fn, state = scripted([text_message()])
    events, emit = collector()
    with pytest.raises(ModelRequestLimitExceeded, match="模型请求上限"):
        asyncio.run(
            run_agent_loop(
                [user("hi")],
                plain_context(),
                loop_config(request_budget=budget),
                emit,
                stream_fn=stream_fn,
            )
        )
    assert state["calls"] == 0 and budget.used == 1
    assert [event["type"] for event in events][-1] == "trace_end"
    RECORDS["rejected_before_send"] = {"used": budget.used, "sends": state["calls"]}


def test_missing_budget_fails_explicitly() -> None:
    stream_fn, state = scripted([text_message()])
    with pytest.raises(RuntimeError, match="共享模型请求预算"):
        asyncio.run(
            run_agent_loop(
                [user("hi")], plain_context(), loop_config(), noop_emit, stream_fn=stream_fn
            )
        )
    assert state["calls"] == 0
    RECORDS["missing_budget"] = {"sends": state["calls"]}


def test_steering_shares_budget() -> None:
    budget = RequestBudget()
    polls = {"n": 0}

    async def steering() -> list[UserMessage]:
        polls["n"] += 1
        return [user("second")] if polls["n"] == 2 else []

    stream_fn, state = scripted([text_message("first"), text_message("second")])
    config = loop_config(request_budget=budget, get_steering_messages=steering)
    run_loop([user("first")], plain_context(), config, stream_fn)
    assert budget.used == 2 and state["calls"] == 2
    RECORDS["steering"] = {"used": budget.used, "sends": state["calls"]}


def test_tool_continuation_shares_budget() -> None:
    budget = RequestBudget()
    stream_fn, state = scripted([tool_call_message(), text_message("done")])
    messages, events = run_loop(
        [user("go")], tool_context(), loop_config(request_budget=budget), stream_fn
    )
    assert budget.used == 2 and state["calls"] == 2
    assert sum(isinstance(message, ToolResultMessage) for message in messages) == 1
    assert any(event["type"] == "tool_result" for event in events)
    RECORDS["tool_continuation"] = {"used": budget.used, "sends": state["calls"]}


def test_recovery_retry_shares_budget() -> None:
    budget = RequestBudget()
    hooks, state = make_hooks([0, 0])
    stream_fn, calls = scripted([overflow_message(), text_message("recovered")])
    config = loop_config(request_budget=budget, compaction=hooks)
    _, events = run_loop([user("go")], plain_context(), config, stream_fn)
    assert budget.used == 2 and calls["calls"] == 2
    assert len(state["compactions"]) == 1
    assert [event["reason"] for event in events if event["type"] == "compaction_start"] == [
        "overflow"
    ]
    RECORDS["recovery_retry"] = {"used": budget.used, "sends": calls["calls"]}


def test_second_overflow_in_streak_terminates() -> None:
    budget = RequestBudget()
    hooks, state = make_hooks([0, 0])
    stream_fn, calls = scripted([overflow_message(), overflow_message()])
    with pytest.raises(ContextOverflowError):
        asyncio.run(
            run_agent_loop(
                [user("go")],
                plain_context(),
                loop_config(request_budget=budget, compaction=hooks),
                noop_emit,
                stream_fn=stream_fn,
            )
        )
    assert budget.used == 2 and len(state["compactions"]) == 1
    RECORDS["second_overflow"] = {"used": budget.used, "compactions": len(state["compactions"])}


def test_new_overflow_after_success_recovers_again() -> None:
    budget = RequestBudget()
    hooks, state = make_hooks([0, 0, 0, 0])
    polls = {"n": 0}

    async def steering() -> list[UserMessage]:
        polls["n"] += 1
        return [user("again")] if polls["n"] == 2 else []

    stream_fn, calls = scripted(
        [
            overflow_message(),
            text_message("first"),
            overflow_message(),
            text_message("second"),
        ]
    )
    config = loop_config(request_budget=budget, compaction=hooks, get_steering_messages=steering)
    _, events = run_loop([user("go")], plain_context(), config, stream_fn)
    assert budget.used == 4 and calls["calls"] == 4
    assert len(state["compactions"]) == 2
    assert [event["reason"] for event in events if event["type"] == "compaction_start"] == [
        "overflow",
        "overflow",
    ]
    RECORDS["new_overflow_after_success"] = {"used": budget.used, "compactions": 2}


def test_threshold_compaction_succeeds_on_fourth() -> None:
    budget = RequestBudget()
    hooks, state = make_hooks([120000, 118000, 116000, 114000, 1000])
    stream_fn, calls = scripted([text_message("ok")])
    _, events = run_loop(
        [user("go")], plain_context(), loop_config(request_budget=budget, compaction=hooks), stream_fn
    )
    assert len(state["compactions"]) == 4
    assert budget.used == 1 and calls["calls"] == 1
    assert sum(event["type"] == "compaction_start" for event in events) == 4
    RECORDS["threshold_fourth_success"] = {"compactions": 4, "used": budget.used}


def test_threshold_compaction_failure_cases() -> None:
    # 终止前均未发起模型请求，也不占用名额：四种失败码与压缩次数由同一表格驱动。
    cases = {
        "fourth_exceeded": ([120000, 118000, 116000, 114000, 112000], None, ContextBudgetExceeded, 4),
        "no_progress": ([120000, 120000], None, ContextBudgetExceeded, 1),
        "no_history": ([120000], NoSummarizableHistory("当前范围没有可总结消息"), NoSummarizableHistory, 1),
    }
    for name, (estimates, fail, expected, compactions) in cases.items():
        budget = RequestBudget()
        hooks, state = make_hooks(estimates, fail=fail)
        stream_fn, calls = scripted([text_message("never")])
        with pytest.raises(expected):
            asyncio.run(
                run_agent_loop(
                    [user("go")],
                    plain_context(),
                    loop_config(request_budget=budget, compaction=hooks),
                    noop_emit,
                    stream_fn=stream_fn,
                )
            )
        assert len(state["compactions"]) == compactions, name
        assert calls["calls"] == 0 and budget.used == 0, name
        RECORDS[f"compaction_failure_{name}"] = {"compactions": compactions, "used": budget.used}


def test_length_terminal_and_no_tool_execution() -> None:
    budget = RequestBudget()
    saved: list[AssistantMessage] = []

    async def save_message(node_id: str, message) -> None:
        saved.append(message)

    length = assistant([TextContent(type="text", text="partial answer")], stop_reason="length")
    stream_fn, calls = scripted([length])
    config = loop_config(request_budget=budget, save_message=save_message)
    events, emit = collector()
    with pytest.raises(ModelOutputLimitExceeded):
        asyncio.run(
            run_agent_loop([user("write")], tool_context(), config, emit, stream_fn=stream_fn)
        )
    assert budget.used == 1 and calls["calls"] == 1
    assert not any(event["type"] in {"tool_start", "tool_result"} for event in events)
    assert events[-1]["type"] == "trace_end" and events[-1]["status"] == "failed"
    assert len(saved) == 1 and saved[0].stop_reason == "length"
    assert "".join(block.text for block in saved[0].content) == "partial answer"
    assert terminal_decision(ModelOutputLimitExceeded("x"), False) == (
        "failed",
        "model_output_limit_exceeded",
        MODEL_OUTPUT_LIMIT_MESSAGE,
    )
    RECORDS["length"] = {"used": budget.used, "saved": len(saved)}


def test_provider_quota_propagates_and_maps() -> None:
    budget = RequestBudget()

    def stream_fn(_spec, _context, _options) -> AssistantResponse:
        budget = _options.get("budget")
        if budget is not None:
            budget.reserve(recovery=bool(_options.get("recovery")))
        raise ProviderQuotaExceeded("供应商账户余额不足")

    with pytest.raises(ProviderQuotaExceeded):
        asyncio.run(
            run_agent_loop(
                [user("go")],
                plain_context(),
                loop_config(request_budget=budget),
                noop_emit,
                stream_fn=stream_fn,
            )
        )
    assert budget.used == 1
    assert terminal_decision(ProviderQuotaExceeded("x"), False) == (
        "failed",
        "provider_quota_exceeded",
        PROVIDER_QUOTA_EXCEEDED_MESSAGE,
    )
    RECORDS["provider_quota"] = {"used": budget.used}


def test_shared_budget_exhaustion_returns_limit_error() -> None:
    budget = RequestBudget(limit=2)
    stream_fn, state = scripted(
        [tool_call_message("c1"), tool_call_message("c2"), text_message("never")]
    )
    with pytest.raises(ModelRequestLimitExceeded, match="模型请求上限"):
        asyncio.run(
            run_agent_loop(
                [user("go")],
                tool_context(),
                loop_config(request_budget=budget),
                noop_emit,
                stream_fn=stream_fn,
            )
        )
    assert budget.used == 2 and state["calls"] == 2
    RECORDS["shared_exhaustion"] = {"used": budget.used, "sends": state["calls"]}


def test_max_turns_bound_precedes_budget() -> None:
    # max_turns 是独立的显式调用约束，保持既有语义：先于共享预算触发时抛自身错误。
    budget = RequestBudget()
    stream_fn, state = scripted([tool_call_message("c1"), tool_call_message("c2")])
    with pytest.raises(RuntimeError, match="模型调用上限"):
        asyncio.run(
            run_agent_loop(
                [user("go")],
                tool_context(),
                loop_config(max_turns=2, request_budget=budget),
                noop_emit,
                stream_fn=stream_fn,
            )
        )
    assert budget.used == 2 and state["calls"] == 2
    RECORDS["max_turns_bound"] = {"used": budget.used, "sends": state["calls"]}


def test_shared_budget_across_normal_request_and_summary() -> None:
    # 联合验证：普通请求入口与任务 3 的真实摘要入口使用同一 RequestBudget 实例。
    budget = RequestBudget(limit=1)
    stream_fn, state = scripted([text_message()])
    run_loop([user("hi")], plain_context(), loop_config(request_budget=budget), stream_fn)
    assert budget.used == 1 and state["calls"] == 1
    preparation = CompactionPreparation(
        first_kept_entry_id="kept-1",
        messages_to_summarize=[user("历史" * 100)],
        turn_prefix_messages=[],
        is_split_turn=False,
        tokens_before=0,
        previous_summary=None,
        attachments=[],
    )
    with pytest.raises(ModelRequestLimitExceeded, match="模型请求上限"):
        asyncio.run(
            generate_compaction_summary(
                preparation, spec(), api_key="test-key", budget=budget
            )
        )
    assert budget.used == 1
    RECORDS["shared_with_summary"] = {"used": budget.used, "sends": state["calls"]}


def test_terminal_mappings() -> None:
    budget = RequestBudget()
    for _ in range(MAX_MODEL_REQUESTS):
        budget.reserve()
    with pytest.raises(ModelRequestLimitExceeded) as info:
        budget.reserve()
    assert terminal_decision(info.value, False) == (
        "failed",
        "model_request_limit_exceeded",
        "本次运行已达到 64 次模型请求上限",
    )
    assert terminal_decision(ContextBudgetExceeded("x"), False) == (
        "failed",
        "context_budget_exceeded",
        CONTEXT_BUDGET_EXCEEDED_MESSAGE,
    )
    assert terminal_decision(NoSummarizableHistory("x"), False) == (
        "failed",
        "context_budget_exceeded",
        CONTEXT_BUDGET_EXCEEDED_MESSAGE,
    )
    assert terminal_decision(None, True) == ("cancelled", "cancelled", "执行已取消。")
    RECORDS["terminal_mappings"] = {"request_limit": "本次运行已达到 64 次模型请求上限"}


def model_chunks(finish_reason: str, content: str) -> list[dict]:
    base = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": "test"}
    return [
        {**base, "choices": [
            {"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}
        ]},
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]},
        {**base, "choices": [], "usage": {
            "prompt_tokens": 15, "completion_tokens": 6, "total_tokens": 21,
            "prompt_tokens_details": {"cached_tokens": 0},
        }},
    ]


class LocalModelServer:
    # 本地真实 OpenAI 兼容流式端点：按请求返回指定 finish_reason 的 SSE，无客户端替换。
    def __init__(self, finish_reason: str = "stop", content: str = "完成。"):
        outer = self
        self.requests = 0
        payload = bytearray()
        for chunk in model_chunks(finish_reason, content):
            payload += f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
        payload += b"data: [DONE]\n\n"
        self._payload = bytes(payload)

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                outer.requests += 1
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(outer._payload)))
                self.end_headers()
                self.wfile.write(outer._payload)
                self.wfile.flush()

            def log_message(self, *_args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def __enter__(self) -> "LocalModelServer":
        self._thread.start()
        return self

    def __exit__(self, *_error) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


def install_local_model(base_url: str) -> None:
    model_config.DATA_ROOT = (
        Path(__file__).resolve().parents[1] / "temp" / "model-request-dispatch" / "config"
    )
    model_config.DATA_ROOT.mkdir(parents=True, exist_ok=True)
    model_config.save_provider(
        api="openai-completions",
        base_url=base_url,
        model="test",
        provider="test",
        api_key="local-test-key",
    )


def http_run(finish_reason: str) -> dict:
    with LocalModelServer(finish_reason) as model_server:
        install_local_model(model_server.base_url)
        patch_default_database("model-request-dispatch")
        with Server(http_app) as server, client(server.base_url, timeout=120) as http:
            session_id = str(uuid4())
            created = http.post(
                "/api/sessions", json={"session_id": session_id, "title": "dispatch"}
            )
            assert created.status_code in {200, 201}, created.text
            with http.stream(
                "POST",
                "/api/agent/run",
                json={
                    "session_id": session_id,
                    "operation_id": str(uuid4()),
                    "request": "只回复完成。",
                },
            ) as response:
                received = list(read_events(response))
            wait_idle()
            history = http.get(f"/api/sessions/{session_id}/history").json()
            requests = model_server.requests
        return {
            "session_id": session_id,
            "events": received,
            "runs": history["runs"],
            "entries": history["entries"],
            "model_requests": requests,
        }


def test_http_normal_run_completes() -> None:
    outcome = http_run("stop")
    kinds = [event["event"] for event in outcome["events"]]
    assert kinds[-1] == "done", kinds
    assert outcome["events"][-1]["data"]["status"] == "completed"
    assert any(
        event["event"] == "message_end" and event["data"]["stop_reason"] == "stop"
        for event in outcome["events"]
    )
    assert len(outcome["runs"]) == 1
    run = outcome["runs"][0]
    assert run["status"] == "completed" and run["error_code"] is None
    assert any(entry["type"] == "message" for entry in outcome["entries"])
    assert outcome["model_requests"] == 1
    RECORDS["http_normal"] = {
        "status": run["status"],
        "model_requests": outcome["model_requests"],
    }


def test_http_length_maps_to_output_limit() -> None:
    outcome = http_run("length")
    kinds = [event["event"] for event in outcome["events"]]
    assert kinds[-1] == "error", kinds
    final = outcome["events"][-1]["data"]
    assert (final["status"], final["code"], final["message"]) == (
        "failed",
        "model_output_limit_exceeded",
        MODEL_OUTPUT_LIMIT_MESSAGE,
    )
    assert any(
        event["event"] == "message_end" and event["data"]["stop_reason"] == "length"
        for event in outcome["events"]
    )
    assert not any(
        event["event"] in {"tool_start", "tool_result"} for event in outcome["events"]
    )
    run = outcome["runs"][0]
    assert (run["status"], run["error_code"], run["error_message"]) == (
        "failed",
        "model_output_limit_exceeded",
        MODEL_OUTPUT_LIMIT_MESSAGE,
    )
    assistants = [
        entry["message"]
        for entry in outcome["entries"]
        if entry.get("message", {}).get("role") == "assistant"
    ]
    assert assistants and assistants[-1]["stop_reason"] == "length"
    RECORDS["http_length"] = {"status": run["status"], "code": run["error_code"]}


def check() -> None:
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    test_normal_request_reserves_once()
    test_rejection_before_send_does_not_consume()
    test_missing_budget_fails_explicitly()
    test_steering_shares_budget()
    test_tool_continuation_shares_budget()
    test_recovery_retry_shares_budget()
    test_second_overflow_in_streak_terminates()
    test_new_overflow_after_success_recovers_again()
    test_threshold_compaction_succeeds_on_fourth()
    test_threshold_compaction_failure_cases()
    test_length_terminal_and_no_tool_execution()
    test_provider_quota_propagates_and_maps()
    test_shared_budget_exhaustion_returns_limit_error()
    test_max_turns_bound_precedes_budget()
    test_shared_budget_across_normal_request_and_summary()
    test_terminal_mappings()
    test_http_normal_run_completes()
    test_http_length_maps_to_output_limit()
    EVIDENCE.write_text(json.dumps(RECORDS, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "调度：普通请求发送前计数；发送前拒绝不占名额；缺预算明确失败；"
        "Steering、工具续接与恢复重试共享同一预算；第 4 次压缩后成功或明确终止；"
        "压缩无进展与无可总结历史终止；一次连续溢出只恢复一次；成功后可再次恢复；"
        "length 截断终态且不执行工具；请求额度与供应商额度映射各自终态"
    )


if __name__ == "__main__":
    check()
