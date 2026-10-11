import asyncio
import json
from concurrent.futures import CancelledError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import ceil
from pathlib import Path
from threading import Event, Lock, Thread

import pytest

from app.agent import compaction as C
from app.agent.budget import (
    CHARS_PER_TOKEN,
    SAFETY_MARGIN_TOKENS,
    ModelRequestLimitExceeded,
    RequestBudget,
)
from app.ai.messages import (
    AssistantMessage,
    ImageContent,
    TextContent,
    ToolCall,
    Usage,
    UserMessage,
)
from app.ai.types import ModelSpec
from test import check_compaction_core as T

ROOT = Path(__file__).resolve().parents[2] / "tmp" / "agent-compaction-task3"
EVIDENCE = ROOT / "budget-counting.json"

SUMMARY_TEXT = "## Goal\n完成四日循环计划\n\n## Critical Context\n- 左膝术后，保持每周四练"
RECORDS: dict[str, object] = {}

# 本地真实 OpenAI 兼容流式端点：返回真实 usage 的 SSE，不做任何客户端替换。
CHUNKS = [
    {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test",
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": SUMMARY_TEXT},
                "finish_reason": None,
            }
        ],
    },
    {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    },
    {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test",
        "choices": [],
        "usage": {
            "prompt_tokens": 15,
            "completion_tokens": 6,
            "total_tokens": 21,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    },
]


class SummaryServer:
    # 真实 HTTP 端点：成功返回 OpenAI SSE，失败返回 500；仅按请求计数，无伪造模型逻辑。
    def __init__(self) -> None:
        self._lock = Lock()
        self.requests = 0
        self.fail = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                with outer._lock:
                    outer.requests += 1
                    fail = outer.fail
                if fail:
                    body = b'{"error":{"message":"upstream failed","type":"server_error"}}'
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in CHUNKS:
                    self.wfile.write(
                        f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
                    )
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

            def log_message(self, *_args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def __enter__(self) -> "SummaryServer":
        self._thread.start()
        return self

    def __exit__(self, *_error) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


def model(
    context_window: int = 128000,
    max_tokens: int = 16384,
    base_url: str = "https://example.invalid/v1",
) -> ModelSpec:
    return ModelSpec(
        api="openai-completions",
        provider="test",
        id="test",
        base_url=base_url,
        context_window=context_window,
        max_tokens=max_tokens,
    )


def preparation(
    messages_to_summarize=(),
    turn_prefix_messages=(),
    *,
    previous_summary: str | None = None,
    is_split_turn: bool = False,
) -> C.CompactionPreparation:
    return C.CompactionPreparation(
        first_kept_entry_id="kept-1",
        messages_to_summarize=list(messages_to_summarize),
        turn_prefix_messages=list(turn_prefix_messages),
        is_split_turn=is_split_turn,
        tokens_before=0,
        previous_summary=previous_summary,
        attachments=[],
    )


def run_summary(prep, spec, budget, *, signal=None):
    return asyncio.run(
        C.generate_compaction_summary(
            prep, spec, api_key="test-key", budget=budget, signal=signal
        )
    )


def test_should_compact_safety_margin_boundary() -> None:
    window, reserve = 128000, 16384
    boundary = window - reserve - SAFETY_MARGIN_TOKENS
    assert C.should_compact(boundary, window, reserve) is False
    assert C.should_compact(boundary + 1, window, reserve) is True
    # 旧阈值（未含安全余量）现在必须触发压缩。
    assert C.should_compact(window - reserve, window, reserve) is True
    RECORDS["should_compact"] = {"boundary": boundary, "window": window, "reserve": reserve}


def test_chars_per_token_rounding() -> None:
    assert CHARS_PER_TOKEN == 3.5
    # 3.5 系数逐项向上取整：7→2、8→3、350→100、351→101、400→115（旧 /4 会得到 1/2/87/87/100）。
    assert C.estimate_tokens(T.user("中" * 7)) == 2
    assert C.estimate_tokens(T.user("中" * 8)) == 3
    assert C.estimate_tokens(T.user("中" * 350)) == 100
    assert C.estimate_tokens(T.user("中" * 351)) == 101
    assert C.estimate_tokens(T.user("中" * 400)) == 115
    prompt = "中文提示" * 10
    assert C._summary_input_tokens(prompt) == ceil(
        len(C.SUMMARIZATION_SYSTEM_PROMPT) / 3.5
    ) + ceil(len(prompt) / 3.5)
    RECORDS["chars_per_token"] = {
        "coefficient": CHARS_PER_TOKEN,
        "samples": {
            "7": C.estimate_tokens(T.user("中" * 7)),
            "8": C.estimate_tokens(T.user("中" * 8)),
            "350": C.estimate_tokens(T.user("中" * 350)),
            "351": C.estimate_tokens(T.user("中" * 351)),
            "400": C.estimate_tokens(T.user("中" * 400)),
        },
    }


def test_image_keeps_fixed_estimate() -> None:
    image = UserMessage(
        role="user",
        content=[ImageContent(type="image", data="x", mime_type="image/png")],
        timestamp=T.tick(),
    )
    assert C.ESTIMATED_IMAGE_CHARS == 4800
    assert C.ESTIMATED_IMAGE_TOKENS == 1200
    assert C.estimate_tokens(image) == 1200
    RECORDS["image_tokens"] = C.estimate_tokens(image)


def test_summary_budget_boundary_equal_and_over_one() -> None:
    messages = [T.user("历史" * 100)]
    prompt = C._summary_prompt(messages, previous_summary=None, turn_prefix=False)
    input_tokens = C._summary_input_tokens(prompt)
    assert input_tokens == C.estimate_summary_input_tokens(messages)
    output_tokens = C._summary_max_tokens(
        C.RESERVE_TOKENS, 16384, C.HISTORY_SUMMARY_RATIO
    )
    window = input_tokens + output_tokens + SAFETY_MARGIN_TOKENS
    C._check_summary_budget(prompt, model(window), output_tokens, "历史摘要")
    with pytest.raises(C.ContextBudgetExceeded):
        C._check_summary_budget(prompt, model(window - 1), output_tokens, "历史摘要")
    RECORDS["summary_budget"] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "safety_margin": SAFETY_MARGIN_TOKENS,
        "window_equal": window,
    }


def test_previous_summary_and_full_tool_result_input() -> None:
    short = [T.user("短")]
    assert C.estimate_summary_input_tokens(
        short, previous_summary="旧摘要" * 500
    ) > C.estimate_summary_input_tokens(short)
    payload = "完整工具结果" * 40000
    messages = [T.tool_result("c1", payload)]
    serialized = C.serialize_conversation(messages)
    assert payload in serialized
    input_tokens = C.estimate_summary_input_tokens(messages)
    assert input_tokens > len(payload) // 4
    RECORDS["full_tool_result"] = {
        "payload_chars": len(payload),
        "input_tokens": input_tokens,
    }


def test_check_budget_includes_safety_margin() -> None:
    settings = C.CompactionSettings(reserve_tokens=16384, keep_recent_tokens=1)
    exact = 16384 + SAFETY_MARGIN_TOKENS
    C.check_budget(exact + 1, 16384, settings)
    with pytest.raises(C.ContextBudgetExceeded):
        C.check_budget(exact, 16384, settings)


def test_summary_validation() -> None:
    def result_message(**overrides) -> AssistantMessage:
        values = {
            "role": "assistant",
            "content": [TextContent(type="text", text="## Goal\n目标")],
            "api": "openai-completions",
            "provider": "test",
            "model": "test",
            "usage": T.USAGE,
            "stop_reason": "stop",
            "timestamp": T.tick(),
        }
        return AssistantMessage.model_validate({**values, **overrides})

    valid = C._validated_summary(result_message(), "历史摘要")
    assert valid.summary.startswith("## Goal") and valid.usage == T.USAGE
    with pytest.raises(C.CompactionSummaryError):
        C._validated_summary(result_message(stop_reason="length"), "历史摘要")
    with pytest.raises(C.CompactionSummaryError):
        C._validated_summary(result_message(content=[TextContent(type="text", text="  ")]), "历史摘要")
    with pytest.raises(C.CompactionSummaryError):
        C._validated_summary(
            result_message(
                content=[
                    TextContent(type="text", text="摘要"),
                    ToolCall(type="toolCall", id="c1", name="read", arguments={}),
                ]
            ),
            "历史摘要",
        )
    with pytest.raises(C.CompactionSummaryError):
        C._validated_summary(
            result_message(stop_reason="error", error_message="boom"), "历史摘要"
        )


def test_usage_basis_applicability_after_compaction() -> None:
    path = [
        T.entry("sys", None, T.system_message()),
        T.entry("u1", "sys", T.user("问题")),
        T.entry(
            "a1",
            "u1",
            T.assistant(
                "回答",
                usage=Usage(input=9, output=9, cache_read=0, cache_write=0, total_tokens=999),
            ),
        ),
    ]
    context = C.EffectiveContext(
        messages=[*path[1].messages, *path[2].messages],
        source_entry_ids=["u1", "a1"],
    )
    fresh = [*path, T.compaction_entry("c1", "a1", "u1")]
    estimate = C.estimate_projected_context_tokens(context, fresh)
    assert estimate.last_usage_index is None and estimate.usage_tokens == 0
    assert estimate.tokens == sum(C.estimate_tokens(m) for m in context.messages)


def test_cut_point_and_attachment_reference() -> None:
    path = [
        T.entry("sys", None, T.system_message()),
        T.entry("u1", "sys", T.user("问题一" * 20)),
        T.entry("a1", "u1", T.assistant("回答一" * 20)),
        T.entry("u2", "a1", T.user("问题二" * 400)),
        T.entry("a2", "u2", T.assistant("回答二")),
    ]
    cut = C.find_cut_point(path, 0, len(path), 1)
    assert path[cut.first_kept_entry_index].messages[0].role in {"user", "assistant"}

    attachment = T.attachment("44444444-4444-4444-8444-444444444444", "plan.md")
    with_attachment = [
        T.entry("sys", None, T.system_message()),
        T.entry("u1", "sys", T.user("问题一" * 20), attachments=[attachment]),
        T.entry("a1", "u1", T.assistant("回答一" * 20)),
        T.entry("u2", "a1", T.user("问题二" * 400)),
        T.entry("a2", "u2", T.assistant("回答二")),
    ]
    prep = C.prepare_compaction(
        with_attachment, 128000, 16384, C.CompactionSettings(reserve_tokens=16384, keep_recent_tokens=1)
    )
    assert [item.attachment_id for item in prep.attachments] == [
        "44444444-4444-4444-8444-444444444444"
    ]
    assert all(item.path.startswith("sessions/") for item in prep.attachments)


def test_single_summary_occupies_one_slot() -> None:
    with SummaryServer() as server:
        budget = RequestBudget()
        prep = preparation([T.user("历史" * 100)])
        result = run_summary(prep, model(base_url=server.base_url), budget)
        assert result.summary == SUMMARY_TEXT and result.usage.total_tokens == 21
        assert budget.used == 1
        assert server.requests == 1
    RECORDS["single_request"] = {"used": budget.used, "requests": server.requests}


def test_split_summary_occupies_two_slots() -> None:
    with SummaryServer() as server:
        budget = RequestBudget()
        prep = preparation(
            [T.user("历史" * 100)],
            [T.user("任务前缀" * 100)],
            is_split_turn=True,
        )
        result = run_summary(prep, model(base_url=server.base_url), budget)
        assert SUMMARY_TEXT in result.summary and "Turn Context" in result.summary
        assert budget.used == 2
        assert server.requests == 2
    RECORDS["split_requests"] = {"used": budget.used, "requests": server.requests}


def test_rejected_before_send_does_not_consume() -> None:
    with SummaryServer() as server:
        budget = RequestBudget(limit=1)
        assert budget.reserve() == 1
        prep = preparation([T.user("历史" * 100)])
        with pytest.raises(ModelRequestLimitExceeded):
            run_summary(prep, model(base_url=server.base_url), budget)
        assert budget.used == 1
        assert server.requests == 0
    RECORDS["rejected_before_send"] = {"used": budget.used, "requests": server.requests}


def test_initiated_failure_keeps_count() -> None:
    with SummaryServer() as server:
        server.fail = True
        budget = RequestBudget()
        prep = preparation([T.user("历史" * 100)])
        with pytest.raises(Exception) as error:
            run_summary(prep, model(base_url=server.base_url), budget)
        assert not isinstance(error.value, ModelRequestLimitExceeded)
        assert budget.used == 1
        assert server.requests == 1
    RECORDS["initiated_failure"] = {
        "used": budget.used,
        "requests": server.requests,
        "error": type(error.value).__name__,
    }


def test_second_summary_without_slot_returns_nothing() -> None:
    with SummaryServer() as server:
        budget = RequestBudget(limit=1)
        prep = preparation(
            [T.user("历史" * 100)],
            [T.user("任务前缀" * 100)],
            is_split_turn=True,
        )
        with pytest.raises(ModelRequestLimitExceeded):
            run_summary(prep, model(base_url=server.base_url), budget)
        assert budget.used == 1
        assert server.requests == 1
    RECORDS["second_request_no_slot"] = {"used": budget.used, "requests": server.requests}


def test_over_budget_and_cancellation_do_not_consume() -> None:
    with SummaryServer() as server:
        budget = RequestBudget()
        over = preparation([T.tool_result("c1", "完整工具结果" * 40000)])
        with pytest.raises(C.ContextBudgetExceeded):
            run_summary(over, model(40000, 1024, server.base_url), budget)
        assert budget.used == 0 and server.requests == 0

        signal = Event()
        signal.set()
        prep = preparation([T.user("历史" * 100)])
        with pytest.raises(CancelledError):
            run_summary(prep, model(base_url=server.base_url), budget, signal=signal)
        assert budget.used == 0 and server.requests == 0
    RECORDS["no_consume_paths"] = {"used": budget.used, "requests": server.requests}


def test_missing_budget_fails_before_send() -> None:
    with SummaryServer() as server:
        prep = preparation([T.user("历史" * 100)])
        with pytest.raises(ValueError, match="共享请求预算"):
            run_summary(prep, model(base_url=server.base_url), None)
        assert server.requests == 0


def check() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    test_should_compact_safety_margin_boundary()
    test_chars_per_token_rounding()
    test_image_keeps_fixed_estimate()
    test_summary_budget_boundary_equal_and_over_one()
    test_previous_summary_and_full_tool_result_input()
    test_check_budget_includes_safety_margin()
    test_summary_validation()
    test_usage_basis_applicability_after_compaction()
    test_cut_point_and_attachment_reference()
    test_single_summary_occupies_one_slot()
    test_split_summary_occupies_two_slots()
    test_rejected_before_send_does_not_consume()
    test_initiated_failure_keeps_count()
    test_second_summary_without_slot_returns_nothing()
    test_over_budget_and_cancellation_do_not_consume()
    test_missing_budget_fails_before_send()
    EVIDENCE.write_text(
        json.dumps(RECORDS, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "压缩预算计数：安全余量边界、3.5 系数与取整、图片固定估算、摘要预算边界、"
        "旧摘要与完整工具结果计入、能力关系含安全余量、usage 适用性、切点与附件、"
        "单/双请求占用、发送前拒绝不占名额、已发起失败保持计数、"
        "第二个摘要无名额不返回结果、超预算与取消不占名额、缺少预算明确失败通过"
    )


if __name__ == "__main__":
    check()
