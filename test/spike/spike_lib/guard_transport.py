# Fit-Agent PydanticAI spike：wire 层费用护栏 transport。
# 所有真实模型调用的唯一 HTTP 通道：在每次 wire 请求发出前完成预算预留（逐次 wire 级，
# 不是逐 Run），在流结束后按原始 usage 结算；SDK/框架任何重试都会形成新的 wire 请求，
# 同样经过本层（因此逐次计费与串行约束天然覆盖所有重试路径）。
#
# 强制项：请求体必须显式携带 0 < max_tokens ≤ 256；模型必须已知；串行锁保证同一时刻
# 只有一个在途模型请求；取消/异常/HTTP 错误时预留全额保留不算 0。

from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
from collections.abc import Callable
from typing import Any

import httpx2 as httpx  # pyright: ignore[reportMissingImports]  # venv 内依赖：运行时+真 pyright 均可解析；仅扫描器 import 误报

from spike_lib.fee_guard import (
    MAX_TOKENS_LIMIT,
    FeeGuard,
    Reservation,
    StopSpike,
    _check_known,
)


def estimate_input_tokens_upper_bound(body: dict[str, Any]) -> int:
    """从实际 wire 请求体保守推导输入 token 上界（用于预留，宁高不低）。
    估算 = ceil(utf8 字节数 / 2) + 每消息 8 token 开销 + 64 全局余量。
    UTF-8 字节/2 对 ASCII（约 4 字节/token）高估 ~2x，对 CJK（3 字节/字，~1 token/字）高估 ~1.5x，
    加上结构开销后覆盖 DeepSeek 分词。不做精确分词承诺——只作为预留上界。"""
    messages = body.get("messages") or []
    tools = body.get("tools") or []
    rest = {k: v for k, v in body.items() if k not in ("messages", "tools")}
    payload_bytes = (
        len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
        + len(json.dumps(tools, ensure_ascii=False).encode("utf-8"))
        + len(json.dumps(rest, ensure_ascii=False).encode("utf-8"))
    )
    return math.ceil(payload_bytes / 2) + 8 * len(messages) + 64


class FeeGuardTransport(httpx.AsyncBaseTransport):
    """包装真实/桩 transport。每次 wire 请求：预留 → 发送 → 流结束后结算（或取消保留预留）。"""

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        guard: FeeGuard,
        *,
        captured: list[Any] | None = None,
        on_event: Callable[[str], None] | None = None,
        now_fn: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._inner = inner
        self._guard = guard
        self._captured = captured
        self._on_event = on_event or (lambda name: None)
        self._now_fn = now_fn or (lambda: dt.datetime.now(dt.timezone.utc))
        self._serial = asyncio.Lock()
        self._serial_released = True  # 当前持有者是否已释放（防双重 release）

    def _release_serial(self) -> None:
        if not self._serial_released:
            self._serial_released = True
            self._serial.release()

    def _event(self, name: str) -> None:
        self._on_event(name)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raw = request.content
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StopSpike(
                f"wire 请求体非法 JSON，停止：{type(exc).__name__}"
            ) from exc
        if self._captured is not None:
            self._captured.append(
                CapturedWireRequest(url=str(request.url), body=body, raw=raw)
            )
        self._event("model_request")

        # wire 约束（预留前强制）：输出上限必须显式且 ≤ 256；模型必须已知。
        # OpenAI SDK 3.x / pydantic-ai 发送新字段 max_completion_tokens；兼容旧字段 max_tokens。
        max_tokens = body.get("max_tokens")
        max_completion_tokens = body.get("max_completion_tokens")
        if (
            max_tokens is not None
            and max_completion_tokens is not None
            and max_tokens != max_completion_tokens
        ):
            raise StopSpike(
                f"wire 请求输出上限字段冲突 max_tokens={max_tokens!r} vs max_completion_tokens={max_completion_tokens!r}，拒绝发出"
            )
        output_limit = max_tokens if max_tokens is not None else max_completion_tokens
        if (
            isinstance(output_limit, bool)
            or not isinstance(output_limit, int)
            or output_limit <= 0
            or output_limit > MAX_TOKENS_LIMIT
        ):
            raise StopSpike(
                f"wire 请求输出上限（max_tokens/max_completion_tokens）={output_limit!r} 缺失或违反 ≤{MAX_TOKENS_LIMIT} 硬约束，拒绝发出"
            )
        req_model = body.get("model")
        _check_known(req_model if isinstance(req_model, str) else "")

        self._guard.require_active()
        await self._serial.acquire()
        self._serial_released = False
        try:
            bound = estimate_input_tokens_upper_bound(body)
            res = self._guard.reserve(
                req_model, input_tokens_reserve=bound, max_output=output_limit
            )
            self._event(f"reserved:{bound}")
            try:
                response = await self._inner.handle_async_request(request)
            except BaseException:
                self._guard.cancel(res, "transport_error")
                self._release_serial()
                raise
            if response.status_code >= 400:
                # HTTP 错误：无 usage 可结算 → 预留全额保留（不算 0），不做任何重试。
                self._event(f"http_error:{response.status_code}")
                self._guard.cancel(res, f"http_{response.status_code}")
                self._release_serial()
                return response
            billed = _BillingStream(
                response,
                guard=self._guard,
                res=res,
                req_model=req_model,
                on_event=self._event,
                release=self._release_serial,
                now_fn=self._now_fn,
            )
            # 流的收尾（finish/abort/aclose）负责释放锁；此处不再释放。
            return httpx.Response(
                response.status_code, headers=response.headers, stream=billed
            )
        except BaseException:
            # handle_async_request 自身异常（reserve 失败等）：确保锁不泄漏。
            self._release_serial()
            raise


class CapturedWireRequest:
    """真实序列化 wire 请求的证据（含原始字节）。"""

    def __init__(self, *, url: str, body: dict[str, Any], raw: bytes) -> None:
        self.url = url
        self.body = body
        self.raw = raw

    # ---- 原始 wire 字节切片（非 sort_keys 再序列化）----
    def wire_elements(self, key: str) -> list[bytes]:
        """在原始请求字节中定位顶层数组 key（如 'messages'、'tools'），
        按顶层逗号切分元素，返回每个元素未经重排的原始字节切片。"""
        raw = self.raw
        marker = b'"' + key.encode("utf-8") + b'":'
        idx = raw.find(marker)
        if idx < 0:
            return []
        start_arr = raw.find(b"[", idx)
        if start_arr < 0:
            return []
        elements: list[bytes] = []
        depth = 0
        in_str = False
        esc = False
        elem_start: int | None = None
        i = start_arr
        while i < len(raw):
            ch = raw[i : i + 1]
            if in_str:
                if esc:
                    esc = False
                elif ch == b"\\":
                    esc = True
                elif ch == b'"':
                    in_str = False
            elif ch == b'"':
                in_str = True
            elif ch in (b"[", b"{"):
                depth += 1
                if depth == 1:
                    elem_start = i + 1
            elif ch in (b"]", b"}"):
                depth -= 1
                if depth == 0:
                    if elem_start is not None and i > elem_start:
                        elements.append(raw[elem_start:i])
                    break
            elif ch == b"," and depth == 1:
                if elem_start is not None:
                    elements.append(raw[elem_start:i])
                elem_start = i + 1
            i += 1
        return elements

    def wire_array(self, key: str) -> bytes:
        """顶层数组 key 的原始完整字节（含括号）。"""
        marker = b'"' + key.encode("utf-8") + b'":'
        idx = self.raw.find(marker)
        if idx < 0:
            return b""
        start = self.raw.find(b"[", idx)
        if start < 0:
            return b""
        depth = 0
        in_str = False
        esc = False
        i = start
        while i < len(self.raw):
            ch = self.raw[i : i + 1]
            if in_str:
                if esc:
                    esc = False
                elif ch == b"\\":
                    esc = True
                elif ch == b'"':
                    in_str = False
            elif ch == b'"':
                in_str = True
            elif ch == b"[":
                depth += 1
            elif ch == b"]":
                depth -= 1
                if depth == 0:
                    return self.raw[start : i + 1]
            i += 1
        return b""

    # ---- 规范化比较（仅作语义辅助证据，不能替代 wire 字节证据）----
    def canonical(self, *keys: str) -> bytes:
        payload = self.body
        for key in keys:
            payload = payload[key]
        return json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()

    def message_prefix(self, n: int) -> list[bytes]:
        """逐元素规范化前 n 条消息（辅助证据）。"""
        return [
            json.dumps(
                m, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode()
            for m in self.body["messages"][:n]
        ]


class _BillingStream(httpx.AsyncByteStream):
    """包装响应流：逐 chunk 透传；流正常结束时解析身份与 usage 并结算；
    取消/异常/提前关闭时预留全额保留。"""

    def __init__(
        self,
        response: httpx.Response,
        *,
        guard: FeeGuard,
        res: Reservation,
        req_model: str,
        on_event: Callable[[str], None],
        release: Callable[[], None],
        now_fn: Callable[[], dt.datetime],
    ) -> None:
        self._inner = response.stream
        self._content_type = response.headers.get("content-type", "")
        self._guard = guard
        self._res = res
        self._req_model = req_model
        self._on_event = on_event
        self._release = release
        self._now_fn = now_fn
        self._chunks = 0
        self._buf = b""
        self._finished = False

    async def __aiter__(self):  # type: ignore[override]
        try:
            async for chunk in self._inner:
                self._chunks += 1
                self._buf += chunk
                self._on_event(f"chunk:{self._chunks}")
                yield chunk
            self._finish()
        except BaseException as exc:  # CancelledError 等：预留保留，不算 0
            self._abort(exc)
            raise

    async def aclose(self) -> None:
        # 消费方读完内容后主动关闭流（pydantic-ai 文本流自然结束路径）≠ 取消：
        # 若缓冲已含完整身份 + usage（末尾 usage chunk 已被消费），按自然结束同等结算；
        # 否则（截断/中途关闭）走 abort，预留全额保留。
        try:
            aclose = getattr(self._inner, "aclose", None)
            if aclose is not None:
                await aclose()
        finally:
            if not self._finished:
                self._finish_or_abort()

    def _finish_or_abort(self) -> None:
        """aclose 收尾判定：缓冲可解析出身份+usage 即按完整结算；否则按截断 abort 保留预留。
        真正的取消在 __aiter__ 内以 CancelledError 先走 _abort（_finished 置位），
        后续 aclose 到此为 no-op，不会把已取消的流误结算。"""
        model, usage = _parse_response_identity_and_usage(self._buf, self._content_type)
        if usage is None or model is None:
            self._abort(RuntimeError("stream closed before completion"))
        else:
            self._finish()

    # ---- 收尾 ----
    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            model, usage = _parse_response_identity_and_usage(
                self._buf, self._content_type
            )
            if model is None:
                self._on_event("identity_missing")
                self._guard.stop("响应缺少 model 身份，停止；预留保留人工核查")
                self._guard.cancel(self._res, "identity_missing")
            elif model != self._req_model and model not in (
                alias for alias in _official_aliases()
            ):
                # 已知其他模型或未知字符串身份：不匹配即停，预留保留（保守，不明账目）。
                self._on_event(f"identity_mismatch:{model}")
                self._guard.stop(
                    f"响应模型身份 {model!r} 与请求 {self._req_model!r} 不匹配，停止；预留保留人工核查"
                )
                self._guard.cancel(self._res, f"identity_mismatch:{model}")
            elif model in _official_aliases() and model != self._req_model:
                # 服务端合法版本别名：映射未经验证，显式未验证并停止，不得静默放行。
                self._on_event(f"identity_alias_unverified:{model}")
                self._guard.stop(
                    f"响应返回官方版本别名 {model!r}，与请求 {self._req_model!r} 的映射未经验证，停止；预留保留"
                )
                self._guard.cancel(self._res, f"identity_alias_unverified:{model}")
            else:
                self._on_event("identity_ok")
                self._guard.settle(
                    self._res,
                    usage,
                    now_utc=self._now_fn(),
                    model=model,
                )
                self._on_event("settled")
        except StopSpike:
            raise
        finally:
            self._release()

    def _abort(self, exc: BaseException) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            self._on_event(f"stream_aborted:{type(exc).__name__}")
            self._guard.cancel(self._res, f"stream_aborted:{type(exc).__name__}")
        finally:
            self._release()


def _official_aliases() -> dict[str, str]:
    from spike_lib.fee_guard import OFFICIAL_VERSION_ALIASES

    return OFFICIAL_VERSION_ALIASES


def _parse_response_identity_and_usage(
    buf: bytes, content_type: str
) -> tuple[str | None, dict | None]:
    """从收尾缓冲解析响应 model 身份与原始 usage（wire 级，未经框架归一）。"""
    text = buf.decode("utf-8", errors="replace")
    if "text/event-stream" in content_type:
        model: str | None = None
        usage: dict | None = None
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if payload == "[DONE]":
                continue
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            chunk_model = chunk.get("model")
            if isinstance(chunk_model, str) and model is None:
                model = chunk_model
            chunk_usage = chunk.get("usage")
            if isinstance(chunk_usage, dict):
                usage = chunk_usage
        return model, usage
    # 非 SSE：整体 JSON。
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, None
    model = data.get("model") if isinstance(data, dict) else None
    usage = data.get("usage") if isinstance(data, dict) else None
    return (model if isinstance(model, str) else None), (
        usage if isinstance(usage, dict) else None
    )
