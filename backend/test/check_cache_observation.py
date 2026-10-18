import asyncio
import io
import json
import shutil
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from time import time_ns
from uuid import uuid4

import pytest

from app.agent.budget import ModelRequestLimitExceeded, RequestBudget
from app.agent.cache_observation import (
    APPEND_CALL,
    FIRST_CALL,
    HISTORY_SUMMARY_CALL,
    NORMAL_CALL,
    RESET_CALL,
    SUMMARY_CALL,
    TURN_PREFIX_SUMMARY_CALL,
    CacheObserver,
    ModelCallObservation,
    extract_projection,
    first_difference,
    main,
    observation_directory,
    read_observations,
    sanitize_endpoint,
    summarize_observations,
)
from app.agent.compaction import CompactionPreparation, generate_compaction_summary
from app.agent.config import AgentLoopConfig, CompactionHooks, LoopCompactionResult
from app.ai.api import anthropic_messages, openai_completions
from app.ai.messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    Tool,
    ToolCall,
    Usage,
    UserMessage,
)
from app.ai.stream import AssistantResponse, _observed, stream
from app.ai.types import ModelSpec
from app.application.session.credentials import CredentialFilter
from app.model_config import ModelConfig

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND / "temp" / "cache-observation"
RECORDS = ROOT / "records"
EVIDENCE = ROOT / "evidence.json"

TOOL = Tool(
    name="echo",
    description="回显文本",
    parameters={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
)
OTHER_TOOL = Tool(
    name="calc",
    description="计算",
    parameters={"type": "object", "properties": {"n": {"type": "integer"}}},
)

RESULTS: dict[str, object] = {}


def openai_spec(base_url: str = "https://example.invalid/v1") -> ModelSpec:
    return ModelSpec(
        api="openai-completions",
        provider="test",
        id="test-model",
        base_url=base_url,
        context_window=128000,
        max_tokens=16384,
    )


def anthropic_spec(base_url: str = "https://example.invalid") -> ModelSpec:
    return ModelSpec(
        api="anthropic-messages",
        provider="anthropic",
        id="claude-test",
        base_url=base_url,
        context_window=200000,
        max_tokens=64000,
    )


def usage(i=50, o=8, cr=40, cw=10) -> Usage:
    return Usage(
        input=i,
        output=o,
        cache_read=cr,
        cache_write=cw,
        total_tokens=i + o + cr + cw,
    )


def user(text: str, tick: int = 1) -> UserMessage:
    return UserMessage(role="user", content=text, timestamp=tick)


def assistant(text: str, tick: int = 2) -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=[TextContent(type="text", text=text)],
        api="openai-completions",
        provider="test",
        model="test-model",
        usage=usage(),
        stop_reason="stop",
        timestamp=tick,
    )


def system(text: str = "系统", tools=(TOOL,), tick: int = 0) -> SystemMessage:
    return SystemMessage(role="system", content=text, tools_added=list(tools), timestamp=tick)


def openai_request(messages, spec=None):
    return openai_completions.to_openai_request(
        messages, spec or openai_spec(), {"api_key": "k"}
    )


def anthropic_request(messages, spec=None):
    return anthropic_messages.to_anthropic_request(
        messages, spec or anthropic_spec(), {"api_key": "k", "max_tokens": 128}
    )


def fresh_observer(directory=RECORDS, session=None, contains=None):
    session = session or str(uuid4())
    return CacheObserver(
        session_id=session,
        run_id=str(uuid4()),
        directory=directory,
        contains=contains,
    )


def session_records(session: str, directory=RECORDS) -> list[ModelCallObservation]:
    return [
        record for record in read_observations(directory) if record.session_id == session
    ]


def call(observer, request, spec, *, kind=NORMAL_CALL, message_id=None, compaction_id=None,
         usage_value=usage(), status="stop"):
    handle = observer.start_call(kind, message_id=message_id, compaction_id=compaction_id)
    handle.observe(request, spec)
    handle.finish(usage_value, status)
    return handle


def test_projection_stability() -> None:
    messages = [system(), user("问"), assistant("答")]
    first = extract_projection(openai_request(messages), "openai-completions")
    again = extract_projection(openai_request(messages), "openai-completions")
    assert first == again and first[2]
    # 仅 timestamp 变化保持投影指纹稳定。
    shifted = [system(tick=100), user("问", tick=101), assistant("答", tick=102)]
    assert extract_projection(openai_request(shifted), "openai-completions") == first
    anthropic_first = extract_projection(
        anthropic_request(messages), "anthropic-messages"
    )
    anthropic_shifted = extract_projection(
        anthropic_request([system(tick=9), user("问", tick=10), assistant("答", tick=11)]),
        "anthropic-messages",
    )
    assert anthropic_first == anthropic_shifted
    assert anthropic_first[0] and anthropic_first[1]
    RESULTS["stability"] = {
        "openai": {"system": bool(first[0]), "tools": bool(first[1]), "count": len(first[2])},
        "anthropic": {"system": bool(anthropic_first[0]), "tools": bool(anthropic_first[1])},
    }


def test_append_prefix_and_difference() -> None:
    spec = openai_spec()
    session = str(uuid4())
    observer = fresh_observer(session=session)
    call(observer, openai_request([system(), user("一"), assistant("a1")]), spec, message_id="m1")
    # 普通追加：保留已有前缀，首个差异位置为空。
    call(
        observer,
        openai_request([system(), user("一"), assistant("a1"), user("二"), assistant("a2")]),
        spec,
        message_id="m2",
    )
    # 修改历史消息：差异位置落在被修改的下标。
    call(
        observer,
        openai_request([system(), user("一"), assistant("a1改了"), user("二"), assistant("a2")]),
        spec,
        message_id="m3",
    )
    records = sorted(session_records(session), key=lambda item: item.call_index)
    assert [record.category for record in records] == [FIRST_CALL, APPEND_CALL, APPEND_CALL]
    assert records[0].first_difference_index is None
    assert records[1].first_difference_index is None
    assert records[2].first_difference_index == 1
    # 差异定位边界：前缀保持、修改、截断与空基准。
    assert first_difference(["a", "b"], ["a", "b", "c"]) is None
    assert first_difference(["a", "b"], ["a", "x"]) == 1
    assert first_difference(["a", "b"], ["a"]) == 1
    assert first_difference([], ["a"]) is None
    RESULTS["append"] = {
        "differences": [record.first_difference_index for record in records],
        "categories": [record.category for record in records],
    }


def test_chain_bridges_runs_and_isolates_sessions() -> None:
    spec = openai_spec()
    session = str(uuid4())
    first_run = fresh_observer(session=session)
    call(first_run, openai_request([system(), user("一")]), spec, message_id="r1m1")
    # 新运行（新观测器）凭已持久化记录衔接比较链。
    second_run = fresh_observer(session=session)
    call(
        second_run,
        openai_request([system(), user("一"), assistant("a1"), user("二")]),
        spec,
        message_id="r2m1",
    )
    records = sorted(session_records(session), key=lambda item: item.call_index)
    assert records[0].category == FIRST_CALL
    assert records[1].category == APPEND_CALL and records[1].first_difference_index is None
    assert records[1].call_index == 2
    # 会话隔离：另一会话比较链互不影响。
    other = str(uuid4())
    other_observer = fresh_observer(session=other)
    call(other_observer, openai_request([system(), user("一")]), spec, message_id="o1")
    other_records = session_records(other)
    assert other_records[0].category == FIRST_CALL
    assert len(session_records(session)) == 2
    RESULTS["chain"] = {
        "bridged_call_index": records[1].call_index,
        "session_records": len(session_records(session)),
        "other_records": len(other_records),
    }


def test_system_tools_and_model_change_detection() -> None:
    spec = openai_spec()
    session = str(uuid4())
    observer = fresh_observer(session=session)
    call(observer, openai_request([system(), user("问")]), spec)
    call(observer, openai_request([system("系统改了"), user("问")]), spec)
    call(observer, openai_request([system("系统改了", tools=(OTHER_TOOL,)), user("问")]), spec)
    call(observer, openai_request([system("系统改了", tools=(TOOL, OTHER_TOOL)), user("问")]), spec)
    call(
        observer,
        openai_request([system("系统改了", tools=(OTHER_TOOL, TOOL)), user("问")]),
        spec,
    )
    switched = ModelSpec(
        api="openai-completions",
        provider="test",
        id="other-model",
        base_url=spec.base_url,
        context_window=128000,
        max_tokens=16384,
    )
    call(
        observer,
        openai_request([system("系统改了", tools=(OTHER_TOOL, TOOL)), user("问")], switched),
        switched,
    )
    records = sorted(session_records(session), key=lambda item: item.call_index)
    assert "system_change" in records[1].known_events and records[1].system_hash != records[0].system_hash
    assert "tools_change" in records[2].known_events
    # 工具声明内容不变时工具哈希稳定；顺序变化改变工具哈希。
    assert records[1].tools_hash == records[0].tools_hash
    assert records[4].tools_hash != records[3].tools_hash
    # 模型切换：记录已知事件并归入重置后的调用。
    assert "model_switch" in records[5].known_events
    assert records[5].category == RESET_CALL
    RESULTS["changes"] = {
        "system_change": "system_change" in records[1].known_events,
        "tools_change": "tools_change" in records[2].known_events,
        "order_changes_tools_hash": records[4].tools_hash != records[3].tools_hash,
        "model_switch": "model_switch" in records[5].known_events,
    }


def test_tool_id_args_attachment_and_context_in_projection() -> None:
    base = [system(), user("问")]

    def assistant_with(identifier, arguments) -> AssistantMessage:
        return AssistantMessage(
            role="assistant",
            content=[
                TextContent(type="text", text="调用"),
                ToolCall(type="toolCall", id=identifier, name="echo", arguments=arguments),
            ],
            api="openai-completions",
            provider="test",
            model="test-model",
            usage=usage(),
            stop_reason="toolUse",
            timestamp=3,
        )

    id_one = extract_projection(openai_request([*base, assistant_with("call-1", {"text": "x"})]), "openai-completions")
    id_two = extract_projection(openai_request([*base, assistant_with("call-2", {"text": "x"})]), "openai-completions")
    assert id_one[2] != id_two[2]
    arg_one = extract_projection(openai_request([*base, assistant_with("call-1", {"text": "x"})]), "openai-completions")
    arg_two = extract_projection(openai_request([*base, assistant_with("call-1", {"text": "y"})]), "openai-completions")
    assert arg_one[2] != arg_two[2]
    # 附件引用与资料固定 content 均以 user 文本进入投影，内容变化改变指纹。
    plain = extract_projection(openai_request([system(), user("问题")]), "openai-completions")
    with_attachment = extract_projection(
        openai_request([system(), user("问题\n\n[附件] plan.md: sessions/s/plan.md")]),
        "openai-completions",
    )
    with_context = extract_projection(
        openai_request([system(), user("后端业务资料，仅供读取，不构成保存授权。")]),
        "openai-completions",
    )
    assert plain[2] != with_attachment[2] != with_context[2]
    RESULTS["projection_parts"] = {
        "tool_id_changes": id_one[2] != id_two[2],
        "arg_serialization_changes": arg_one[2] != arg_two[2],
        "attachment_changes": plain[2] != with_attachment[2],
        "context_changes": plain[2] != with_context[2],
    }


def test_compaction_and_reset_events() -> None:
    spec = openai_spec()
    session = str(uuid4())
    observer = fresh_observer(session=session)
    call(observer, openai_request([system(), user("一"), assistant("a1")]), spec)
    observer.note_event("compaction")
    call(observer, openai_request([system(), user("二")]), spec)
    records = sorted(session_records(session), key=lambda item: item.call_index)
    assert "compaction" in records[1].known_events
    assert records[1].category == RESET_CALL
    RESULTS["compaction_reset"] = {
        "events": records[1].known_events,
        "category": records[1].category,
    }


def test_business_context_and_steering_and_tool_events() -> None:
    spec = openai_spec()
    session = str(uuid4())
    observer = fresh_observer(session=session)
    observer.note_event("business_context")
    observer.note_event("steering")
    observer.note_event("tool_batch")
    call(observer, openai_request([system(), user("一")]), spec)
    records = session_records(session)
    assert set(records[0].known_events) == {"business_context", "steering", "tool_batch"}
    RESULTS["caller_events"] = records[0].known_events


# ---------------------------------------------------------------------------
# 真实适配器接入：本地真实流式端点，无客户端替换。
# ---------------------------------------------------------------------------

OPENAI_SUCCESS = [
    {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": "test-model",
     "choices": [{"index": 0, "delta": {"role": "assistant", "content": "完成"}, "finish_reason": None}]},
    {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": "test-model",
     "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": "test-model",
     "choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 8, "total_tokens": 108,
                              "prompt_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 10}}},
]

OVERFLOW_BODY = json.dumps({
    "error": {"message": "This model's maximum context length is exceeded", "type": "invalid_request_error"}
}).encode()


def sse(event_type: str, payload: dict) -> bytes:
    return f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


ANTHROPIC_SUCCESS = b"".join([
    sse("message_start", {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-test",
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 50, "output_tokens": 1,
                  "cache_read_input_tokens": 40, "cache_creation_input_tokens": 10}}}),
    sse("content_block_start", {"type": "content_block_start", "index": 0,
                                "content_block": {"type": "text", "text": ""}}),
    sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                                "delta": {"type": "text_delta", "text": "完成"}}),
    sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
    sse("message_delta", {"type": "message_delta",
                          "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                          "usage": {"output_tokens": 8}}),
    sse("message_stop", {"type": "message_stop"}),
])


class OpenAIServer:
    # 真实 OpenAI 兼容流式端点：按脚本返回容量拒绝或带真实 usage 的 SSE，无客户端替换。
    def __init__(self, behaviors):
        self.requests = 0
        self._behaviors = list(behaviors)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                index = outer.requests
                outer.requests += 1
                behavior = outer._behaviors[min(index, len(outer._behaviors) - 1)]
                if behavior == "overflow":
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(OVERFLOW_BODY)))
                    self.end_headers()
                    self.wfile.write(OVERFLOW_BODY)
                    return
                payload = bytearray()
                for chunk in OPENAI_SUCCESS:
                    payload += f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
                payload += b"data: [DONE]\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(bytes(payload))
                self.wfile.flush()

            def log_message(self, *_args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_error):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


class AnthropicServer:
    def __init__(self):
        self.requests = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                outer.requests += 1
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(ANTHROPIC_SUCCESS)))
                self.end_headers()
                self.wfile.write(ANTHROPIC_SUCCESS)
                self.wfile.flush()

            def log_message(self, *_args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_error):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


def consume(response):
    kinds = []

    async def run():
        async for event in response:
            kinds.append(event["type"])
        return await response.result()

    return kinds, asyncio.run(run())


def test_adapter_wiring_openai() -> None:
    with OpenAIServer(["ok"]) as server:
        spec = openai_spec(server.base_url)
        session = str(uuid4())
        observer = fresh_observer(session=session)
        context = {"messages": [system(), user("问")]}
        response = stream(spec, dict(context), {"api_key": "k", "observation": observer.start_call(NORMAL_CALL, message_id="m1")})
        kinds, message = consume(response)
        records = session_records(session)
        assert kinds[-1] == "done" and message.stop_reason == "stop"
        assert len(records) == 1 and server.requests == 1
        record = records[0]
        assert record.api == "openai-completions" and record.message_id == "m1"
        assert record.status == "stop" and record.input_tokens is not None
        assert (record.input_tokens, record.cache_read_tokens, record.cache_write_tokens, record.output_tokens) == (50, 40, 10, 8)
        # 观测不改动请求内容：同样输入产生同样的最终投影。
        assert extract_projection(openai_request(context["messages"], spec), "openai-completions") == (
            record.system_hash, record.tools_hash, record.message_fingerprints
        )
        RESULTS["openai_wiring"] = {
            "status": record.status,
            "usage": [record.input_tokens, record.cache_read_tokens, record.cache_write_tokens, record.output_tokens],
        }


def test_adapter_wiring_anthropic() -> None:
    with AnthropicServer() as server:
        spec = anthropic_spec(server.base_url)
        session = str(uuid4())
        observer = fresh_observer(session=session)
        context = {"messages": [system(), user("问")]}
        response = stream(spec, dict(context), {"api_key": "k", "max_tokens": 64, "observation": observer.start_call(NORMAL_CALL, message_id="a1")})
        kinds, message = consume(response)
        records = session_records(session)
        assert kinds[-1] == "done" and message.stop_reason == "stop"
        assert len(records) == 1 and server.requests == 1
        record = records[0]
        assert record.api == "anthropic-messages"
        assert record.system_hash and record.tools_hash
        assert record.status == "stop" and record.input_tokens is not None
        assert (record.input_tokens, record.cache_read_tokens, record.cache_write_tokens, record.output_tokens) == (50, 40, 10, 8)
        RESULTS["anthropic_wiring"] = {
            "status": record.status,
            "system_hash": bool(record.system_hash),
            "tools_hash": bool(record.tools_hash),
        }


def test_adapter_wiring_connection_failure_keeps_missing_usage() -> None:
    # 已发起但连接失败：保留可核实状态，缺失用量保持缺失。
    spec = openai_spec("http://127.0.0.1:1/v1")
    session = str(uuid4())
    observer = fresh_observer(session=session)
    handle = observer.start_call(NORMAL_CALL, message_id="fail-1")
    with pytest.raises(Exception):
        consume(stream(spec, {"messages": [user("问")]}, {"api_key": "k", "observation": handle}))
    records = session_records(session)
    assert len(records) == 1
    record = records[0]
    assert record.status == "failed" and record.input_tokens is None
    RESULTS["failure"] = {"status": record.status, "input_tokens": record.input_tokens}


def test_summary_subcalls_recorded_separately() -> None:
    with OpenAIServer(["ok"]) as server:
        spec = openai_spec(server.base_url)
        session = str(uuid4())
        observer = fresh_observer(session=session)
        compaction_id = str(uuid4())
        preparation = CompactionPreparation(
            first_kept_entry_id="kept-1",
            messages_to_summarize=[user("历史" * 50)],
            turn_prefix_messages=[user("任务前缀" * 50)],
            is_split_turn=True,
            tokens_before=0,
            previous_summary=None,
            attachments=[],
        )
        budget = RequestBudget()
        asyncio.run(
            generate_compaction_summary(
                preparation, spec, api_key="k", budget=budget,
                observation=observer, compaction_id=compaction_id,
            )
        )
        records = sorted(session_records(session), key=lambda item: item.call_index)
        assert [record.call_kind for record in records] == [
            HISTORY_SUMMARY_CALL, TURN_PREFIX_SUMMARY_CALL
        ]
        assert all(record.compaction_id == compaction_id for record in records)
        assert all(record.category == SUMMARY_CALL for record in records)
        assert [record.call_index for record in records] == [1, 2]
        assert budget.used == server.requests == 2
        # 单摘要：只记录一次。
        single_session = str(uuid4())
        single_observer = fresh_observer(session=single_session)
        single = CompactionPreparation(
            first_kept_entry_id="kept-1",
            messages_to_summarize=[user("历史" * 50)],
            turn_prefix_messages=[],
            is_split_turn=False,
            tokens_before=0,
            previous_summary=None,
            attachments=[],
        )
        asyncio.run(
            generate_compaction_summary(
                single, spec, api_key="k", budget=RequestBudget(),
                observation=single_observer, compaction_id=str(uuid4()),
            )
        )
        single_records = session_records(single_session)
        assert len(single_records) == 1
        assert single_records[0].call_kind == HISTORY_SUMMARY_CALL
        RESULTS["summary"] = {
            "split_kinds": [record.call_kind for record in records],
            "single_kind": single_records[0].call_kind,
        }


def test_recovery_retry_recorded_separately() -> None:
    with OpenAIServer(["overflow", "ok"]) as server:
        spec = openai_spec(server.base_url)
        session = str(uuid4())
        observer = fresh_observer(session=session)
        model = ModelConfig(
            api="openai-completions", base_url=server.base_url, model="test-model",
            provider="test", api_key="k",
        )
        compactions = []

        async def estimate_request() -> int:
            return 0

        async def compact(compaction_id: str, signal, span) -> LoopCompactionResult:
            compactions.append(compaction_id)
            return LoopCompactionResult(entry_id=compaction_id, parent_id="parent", messages=[])

        hooks = CompactionHooks(
            estimate_request=estimate_request, compact=compact,
            context_window=128000, reserve_tokens=16384,
        )

        async def emit(_event) -> None:
            return None

        config = AgentLoopConfig(
            model=model, max_turns=8, model_spec=spec, request_budget=RequestBudget(),
            compaction=hooks, observation=observer,
        )
        from app.agent.agent_loop import run_agent_loop

        asyncio.run(run_agent_loop([user("go")], {"messages": [], "tools": {}}, config, emit, stream_fn=stream))
        records = sorted(session_records(session), key=lambda item: item.call_index)
        assert len(records) == 2 and server.requests == 2
        assert records[0].status == "error" and records[0].input_tokens is None
        assert records[1].status == "stop" and records[1].input_tokens is not None
        assert "overflow_recovery" in records[1].known_events
        assert records[0].message_id != records[1].message_id
        assert [record.call_index for record in records] == [1, 2]
        RESULTS["recovery"] = {
            "statuses": [record.status for record in records],
            "event": records[1].known_events,
            "requests": server.requests,
        }


def test_budget_rejection_makes_no_record() -> None:
    spec = openai_spec("http://127.0.0.1:1/v1")
    session = str(uuid4())
    observer = fresh_observer(session=session)
    budget = RequestBudget(limit=1)
    budget.reserve()
    model = ModelConfig(
        api="openai-completions", base_url=spec.base_url, model="test-model",
        provider="test", api_key="k",
    )

    async def emit(_event) -> None:
        return None

    config = AgentLoopConfig(
        model=model, max_turns=4, model_spec=spec, request_budget=budget, observation=observer
    )

    from app.agent.agent_loop import run_agent_loop

    with pytest.raises(ModelRequestLimitExceeded):
        asyncio.run(run_agent_loop([user("go")], {"messages": [], "tools": {}}, config, emit, stream_fn=stream))
    assert session_records(session) == []
    RESULTS["rejection"] = {"records": 0, "used": budget.used}


def make_record(**overrides) -> ModelCallObservation:
    values = {
        "observation_id": uuid4().hex,
        "recorded_at": time_ns() // 1_000_000,
        "api": "openai-completions",
        "provider": "test",
        "model": "test-model",
        "endpoint": "https://example.invalid/v1",
        "session_id": "session",
        "run_id": "run",
        "message_id": "message",
        "compaction_id": None,
        "call_kind": NORMAL_CALL,
        "call_index": 1,
        "category": FIRST_CALL,
        "system_hash": "s",
        "tools_hash": "t",
        "message_fingerprints": ["m"],
        "first_difference_index": None,
        "known_events": [],
        "status": "stop",
        "input_tokens": 50,
        "cache_read_tokens": 40,
        "cache_write_tokens": 10,
        "output_tokens": 8,
    }
    values.update(overrides)
    return ModelCallObservation.model_validate(values)


def test_aggregation_and_hit_rate() -> None:
    summary = summarize_observations([
        make_record(input_tokens=50, cache_read_tokens=40, cache_write_tokens=10, output_tokens=8),
        make_record(input_tokens=100, cache_read_tokens=0, cache_write_tokens=0, output_tokens=5, model="other-model"),
        make_record(input_tokens=None, cache_read_tokens=None, cache_write_tokens=None, output_tokens=None),
    ])
    totals = summary["totals"]
    assert totals["calls"] == 3
    assert totals["usage_available_calls"] == 2 and totals["usage_missing_calls"] == 1
    assert totals["input_tokens"] == 150 and totals["cache_read_tokens"] == 40 and totals["cache_write_tokens"] == 10
    assert totals["output_tokens"] == 13
    assert totals["hit_rate"] == 40 / 200
    groups = {group["model"]: group for group in summary["groups"]}
    assert set(groups) == {"test-model", "other-model"}
    assert groups["other-model"]["hit_rate"] == 0.0
    # 零分母：指标不可计算。
    zero = summarize_observations([
        make_record(input_tokens=0, cache_read_tokens=0, cache_write_tokens=0, output_tokens=0)
    ])["totals"]
    assert zero["hit_rate"] is None
    # 按真实调用去重：同一 observation_id 只计一次。
    duplicated = make_record(observation_id="dup")
    deduped = summarize_observations([duplicated, duplicated])["totals"]
    assert deduped["calls"] == 1
    # 分类独立汇总：本分类内先求用量总和再计算命中率，并保留有效用量数与缺失用量数。
    missing = {"input_tokens": None, "cache_read_tokens": None, "cache_write_tokens": None, "output_tokens": None}
    categorized = summarize_observations([
        make_record(category=FIRST_CALL, **missing),
        make_record(category=APPEND_CALL, input_tokens=0, cache_read_tokens=100, cache_write_tokens=0, output_tokens=4),
        make_record(category=SUMMARY_CALL, call_kind=HISTORY_SUMMARY_CALL, input_tokens=100, cache_read_tokens=0, cache_write_tokens=0, output_tokens=4),
        make_record(category=RESET_CALL, **missing),
    ])
    categories = categorized["totals"]["categories"]
    assert categories[APPEND_CALL]["hit_rate"] == 1.0
    assert categories[SUMMARY_CALL]["hit_rate"] == 0.0
    assert categories[FIRST_CALL]["hit_rate"] is None and categories[RESET_CALL]["hit_rate"] is None
    assert categories[FIRST_CALL]["usage_missing_calls"] == 1
    assert categories[APPEND_CALL]["usage_available_calls"] == 1
    assert [categories[name]["calls"] for name in (FIRST_CALL, APPEND_CALL, SUMMARY_CALL, RESET_CALL)] == [1, 1, 1, 1]
    assert categorized["totals"]["hit_rate"] == 0.5
    RESULTS["aggregation"] = {
        "hit_rate": totals["hit_rate"],
        "missing": totals["usage_missing_calls"],
        "zero_denominator": zero["hit_rate"],
        "deduped": deduped["calls"],
        "category_hit_rate": {name: categories[name]["hit_rate"] for name in (FIRST_CALL, APPEND_CALL, SUMMARY_CALL, RESET_CALL)},
    }


def test_security_boundary_and_guard() -> None:
    assert sanitize_endpoint("https://user:pw@host:443/v1?key=secret#frag") == "https://host:443/v1"
    assert sanitize_endpoint("http://host/v1/") == "http://host/v1"
    session = str(uuid4())
    observer = fresh_observer(session=session)
    secret_text = "SECRET-BUSINESS-CONTENT"
    call(observer, openai_request([system(), user(secret_text)], openai_spec(
        "https://user:pw@example.invalid/v1?api_key=leak"
    )), openai_spec("https://user:pw@example.invalid/v1?api_key=leak"))
    raw = (RECORDS / f"{session}.jsonl").read_text(encoding="utf-8")
    assert secret_text not in raw and "leak" not in raw and "?" not in raw
    # 写入边界：命中凭据时阻止落盘（复用生产凭据过滤器）。
    blocked_session = "secret-model"
    guard = CredentialFilter(("secret-model",))
    blocked = fresh_observer(session=blocked_session, contains=guard.contains)
    blocked_spec = ModelSpec(
        api="openai-completions", provider="test", id="secret-model",
        base_url="https://example.invalid/v1", context_window=128000, max_tokens=16384,
    )
    call(blocked, openai_request([system(), user("问")], blocked_spec), blocked_spec)
    assert not (RECORDS / f"{blocked_session}.jsonl").exists()
    RESULTS["security"] = {
        "endpoint": sanitize_endpoint("https://user:pw@host:443/v1?key=secret#frag"),
        "content_absent": secret_text not in raw,
        "guard_blocked": not (RECORDS / f"{blocked_session}.jsonl").exists(),
    }


def test_observe_does_not_mutate_request() -> None:
    spec = openai_spec()
    request = openai_request([system(), user("问"), assistant("答")])
    snapshot = json.dumps(request, ensure_ascii=False, sort_keys=True)
    observer = fresh_observer()
    handle = observer.start_call(NORMAL_CALL, message_id="m")
    handle.observe(request, spec)
    assert json.dumps(request, ensure_ascii=False, sort_keys=True) == snapshot
    RESULTS["immutable"] = True


def test_early_close_cancel_and_error_release_source() -> None:
    # 转发层持有底层流的关闭责任：提前关闭、取消与异常退出都必须在关闭返回前释放底层资源，
    # 且观测结局只写一次。
    def probe(state, *, block=False, error=None):
        async def generator():
            try:
                yield {"type": "start", "message": assistant("开始")}
                if block:
                    await state["gate"].wait()
                if error is not None:
                    raise error
            finally:
                state["cleanup"].append("source_closed")
        return generator()

    def observed(state, session, message_id, *, block=False, error=None):
        observer = fresh_observer(session=session)
        handle = observer.start_call(NORMAL_CALL, message_id=message_id)
        handle.observe(openai_request([system(), user("问")]), openai_spec())
        return AssistantResponse(_observed(probe(state, block=block, error=error), handle, None)), handle

    async def scenario_close():
        state = {"cleanup": [], "gate": asyncio.Event()}
        session = str(uuid4())
        response, handle = observed(state, session, "close-1")
        iterator = response.__aiter__()
        await iterator.__anext__()
        assert state["cleanup"] == []
        await response.aclose()
        assert state["cleanup"] == ["source_closed"]
        return session, handle

    async def scenario_error():
        state = {"cleanup": [], "gate": asyncio.Event()}
        session = str(uuid4())
        response, handle = observed(state, session, "error-1", error=RuntimeError("boom"))
        raised = False
        try:
            async for _ in response:
                pass
        except RuntimeError:
            raised = True
        assert raised and state["cleanup"] == ["source_closed"]
        return session, handle

    async def scenario_cancel():
        state = {"cleanup": [], "gate": asyncio.Event()}
        session = str(uuid4())
        response, handle = observed(state, session, "cancel-1", block=True)

        async def consume_all():
            async for _ in response:
                pass

        task = asyncio.create_task(consume_all())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        cancelled = False
        try:
            await task
        except asyncio.CancelledError:
            cancelled = True
        assert cancelled and state["cleanup"] == ["source_closed"]
        return session, handle

    outcomes = {}
    # 取消按真实控制流结算为 aborted；提前关闭与普通异常仍为 failed。
    expected_status = {"early_close": "failed", "error": "failed", "cancel": "aborted"}
    for name, scenario in (("early_close", scenario_close), ("error", scenario_error), ("cancel", scenario_cancel)):
        session, handle = asyncio.run(scenario())
        records = session_records(session)
        # 结局只写一次：提前关闭后再 aclose 不产生第二条记录。
        assert len(records) == 1 and records[0].status == expected_status[name]
        assert handle.finished
        outcomes[name] = {"records": len(records), "status": records[0].status}
    RESULTS["release"] = outcomes


def test_reader_and_cli() -> None:
    assert observation_directory().name == "cache-observation"
    session = str(uuid4())
    observer = fresh_observer(session=session)
    call(observer, openai_request([system(), user("问")]), openai_spec())
    assert len(read_observations(RECORDS)) >= 1
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        main(["--directory", str(RECORDS), "--session", session])
    payload = json.loads(buffer.getvalue())
    assert payload["calls"] == 1 and payload["totals"]["input_tokens"] == 50
    RESULTS["reader"] = {"calls": payload["calls"]}


def check() -> None:
    if RECORDS.exists():
        shutil.rmtree(RECORDS)
    RECORDS.mkdir(parents=True, exist_ok=True)
    test_projection_stability()
    test_append_prefix_and_difference()
    test_chain_bridges_runs_and_isolates_sessions()
    test_system_tools_and_model_change_detection()
    test_tool_id_args_attachment_and_context_in_projection()
    test_compaction_and_reset_events()
    test_business_context_and_steering_and_tool_events()
    test_adapter_wiring_openai()
    test_adapter_wiring_anthropic()
    test_adapter_wiring_connection_failure_keeps_missing_usage()
    test_summary_subcalls_recorded_separately()
    test_recovery_retry_recorded_separately()
    test_budget_rejection_makes_no_record()
    test_aggregation_and_hit_rate()
    test_security_boundary_and_guard()
    test_observe_does_not_mutate_request()
    test_early_close_cancel_and_error_release_source()
    test_reader_and_cli()
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "缓存观测：投影指纹稳定（含 timestamp 稳定性）、普通追加前缀与差异定位、"
        "跨运行与会话隔离、系统/工具/顺序/模型切换检测、"
        "工具 ID 与参数序列化及附件资料纳入投影、两条适配器接入与失败缺失用量、"
        "两个摘要子调用分别记录、恢复重试分别记录、发送前拒绝零记录、"
        "用量汇总与去重与零分母、各分类独立用量与命中率、安全边界与写入阻止、请求不变、"
        "提前关闭与取消与异常退出的底层资源释放、读取与汇总入口通过"
    )


if __name__ == "__main__":
    check()
