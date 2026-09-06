# Fit-Agent PydanticAI spike：离线桩 transport。
# 通过自定义 AsyncBaseTransport 捕获 pydantic-ai/openai-sdk 实际序列化的 wire 请求体（原始字节），
# 并按脚本返回：普通 JSON 响应、真正的异步分块 SSE 流（逐 chunk 可取消）、可控 HTTP 错误状态。

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx2 as httpx


@dataclass
class CapturedRequest:
    method: str
    url: str
    body: dict[str, Any]
    raw: bytes  # 未经解析的 wire 原始字节（真实序列化形态）

    # ---- 原始字节切片（首选证据）----
    def wire_elements(self, key: str) -> list[bytes]:
        """顶层数组 key 的逐元素原始字节切片（未重排、未再序列化）。"""
        from spike_lib.guard_transport import CapturedWireRequest

        return CapturedWireRequest(url=self.url, body=self.body, raw=self.raw).wire_elements(key)

    def wire_array(self, key: str) -> bytes:
        from spike_lib.guard_transport import CapturedWireRequest

        return CapturedWireRequest(url=self.url, body=self.body, raw=self.raw).wire_array(key)

    # ---- 规范化比较（仅作语义辅助证据，不能替代 wire 字节证据）----
    def canonical(self, *keys: str) -> bytes:
        payload = self.body
        for key in keys:
            payload = payload[key]
        return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()

    def message_prefix(self, n: int) -> list[bytes]:
        """逐元素规范化前 n 条消息（辅助证据）。"""
        return [
            json.dumps(m, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
            for m in self.body["messages"][:n]
        ]


def _completion_response(model: str | None, content: str, usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "chatcmpl-spike",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": usage,
    }


def _tool_call_response(model: str | None, call_id: str, name: str, arguments: str, usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "chatcmpl-spike",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": usage,
    }


def deepseek_usage(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int | None = None,
    hit_tokens: int | None = None,
    miss_tokens: int | None = None,
) -> dict[str, Any]:
    """构造 DeepSeek OpenAI 兼容 usage；字段默认省略（缺 ≠ 0）。
    注意：显式提供 hit/miss 时必须满足 hit+miss==prompt，否则会被费用护栏正确拒绝。"""
    usage: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    if hit_tokens is not None:
        usage["prompt_cache_hit_tokens"] = hit_tokens
    if miss_tokens is not None:
        usage["prompt_cache_miss_tokens"] = miss_tokens
    if cached_tokens is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
    return usage


class _ScriptedStream(httpx.AsyncByteStream):
    """真正的异步分块流：逐 chunk yield，每 chunk 后可选 delay（可被取消打断了已消费进度）。"""

    def __init__(self, chunks: list[bytes], delay: float) -> None:
        self._chunks = chunks
        self._delay = delay

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk
            if self._delay:
                await asyncio.sleep(self._delay)


@dataclass
class ScriptedTransport:
    """按脚本依次返回响应；记录每个请求的原始字节与解析体。
    支持普通 JSON、真正分块 SSE（chunk_delay 控制节奏）、HTTP 错误状态（status 字段）。"""

    script: list[dict[str, Any] | Exception] = field(default_factory=list)
    captured: list[CapturedRequest] = field(default_factory=list)
    on_event: Callable[[str], None] | None = None
    chunk_delay: float = 0.05

    def _event(self, name: str) -> None:
        if self.on_event is not None:
            self.on_event(name)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raw = request.content
        body = json.loads(raw.decode("utf-8"))
        self.captured.append(CapturedRequest(method=request.method, url=str(request.url), body=body, raw=raw))
        self._event("model_request")
        if not self.script:
            raise AssertionError("ScriptedTransport: 脚本已耗尽，仍有请求到达（可能是取消后继续调用）")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        status = item.get("status", 200)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": item.get("error_message", "spike scripted error"), "type": "spike_error"}})
        # 响应身份：默认回显请求 model；测试可用 item["model"] 覆盖（含缺失：item["omit_model"]=True）。
        model = body["model"]
        if item.get("model") is not None:
            model = item["model"]
        if item.get("omit_model"):
            model = None
        usage = item["usage"]
        if body.get("stream") is True:
            return self._sse_response(model, item, usage)
        if "tool_call" in item:
            tc = item["tool_call"]
            response = _tool_call_response(model, tc["id"], tc["name"], tc["arguments"], usage)
        else:
            response = _completion_response(model, item.get("content", "ok"), usage)
        if model is None:
            response.pop("model", None)
        return httpx.Response(200, json=response)

    def _sse_response(self, model: str | None, item: dict[str, Any], usage: dict[str, Any]) -> httpx.Response:
        content = item.get("content", "ok")
        tool_call = item.get("tool_call")
        chunk_count = item.get("sse_chunks", 3)  # 内容拆成多 chunk，保证"首 chunk 后取消"可测
        chunks: list[dict[str, Any]] = []
        base: dict[str, Any] = {"id": "chatcmpl-spike", "object": "chat.completion.chunk", "created": 0}
        if model is not None:
            base["model"] = model

        def _add(delta: dict[str, Any], finish: str | None = None, with_usage: bool = False) -> None:
            chunk = {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            if with_usage:
                chunk["usage"] = usage
            chunks.append(chunk)

        _add({"role": "assistant"})
        if tool_call is not None:
            _add({"tool_calls": [{"index": 0, "id": tool_call["id"], "type": "function", "function": {"name": tool_call["name"], "arguments": ""}}]})
            _add({"tool_calls": [{"index": 0, "function": {"arguments": tool_call["arguments"]}}]})
            finish = "tool_calls"
        else:
            # 内容均分为 chunk_count 个 delta chunk
            piece = max(1, len(content) // chunk_count) if content else 1
            for i in range(0, len(content), piece):
                _add({"content": content[i : i + piece]})
            if not content:
                _add({"content": ""})
            finish = "stop"
        _add({}, finish=finish, with_usage=True)
        lines = [f"data: {json.dumps(c)}\n\n" for c in chunks]
        lines.append("data: [DONE]\n\n")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_ScriptedStream([line.encode("utf-8") for line in lines], item.get("chunk_delay", self.chunk_delay)),
        )
