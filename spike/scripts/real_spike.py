# Fit-Agent PydanticAI spike：真实调用入口（唯一路径经 build_spike_agent + PersistentFeeGuard）。
# 三批次分进程串行执行，账本 spike/ledger.json 跨批次累计（并发启动会被锁拒绝）：
#   python scripts/real_spike.py flash-regression | pro-smoke | vision-smoke
# 前提：离线护栏全绿；价格核对 match；DEEPSEEK_API_KEY 只经环境变量传入（缺失即拒绝，不显示值）。
# 证据写 evidence/real/<phase>.json（脱敏：不含请求头、不含 Key）；任何异常（含 StopSpike）都落证据再退出。

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic_ai import RunContext  # noqa: E402

from spike_lib.fee_guard import StopSpike  # noqa: E402
from spike_lib.ledger import PersistentFeeGuard  # noqa: E402
from spike_lib.real_runner import build_spike_agent, read_api_key_from_env  # noqa: E402
from spike_lib.run_harness import RunHarness  # noqa: E402
from spike_lib.usage_norm import normalize_raw_usage  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
LEDGER_PATH = ROOT / "ledger.json"
EVIDENCE_DIR = ROOT / "evidence" / "real"
INSTRUCTIONS = "常驻层：你是 Fit-Agent 计划助手。" * 2


async def lookup_equipment(ctx: RunContext[None], equipment_name: str) -> str:
    """查询器械可用性。"""
    return f"{equipment_name}: available"


lookup_equipment.__name__ = "lookup_equipment"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _wire_request_view(req) -> dict:
    return {
        "url": req.url,
        "raw_bytes_len": len(req.raw),
        "body": req.body,
        "wire_messages_elements_bytes": [m.decode("utf-8") for m in req.wire_elements("messages")],
        "wire_tools_array_bytes": req.wire_array("tools").decode("utf-8"),
    }


def _usage_views(guard: PersistentFeeGuard) -> list[dict]:
    views = []
    for i, c in enumerate(guard.calls, 1):
        norm = normalize_raw_usage(c.usage_raw) if c.usage_raw is not None else None
        views.append(
            {
                "call_index": i,
                "model_response_identity": c.model,
                "note": c.note,
                "settled_usd": None if c.settled_usd is None else str(c.settled_usd),
                "reserved_usd": str(c.reserved_usd),
                "usage_raw": c.usage_raw,
                "usage_normalized": asdict(norm) if norm is not None else None,
            }
        )
    return views


def _fee_view(guard: PersistentFeeGuard) -> dict:
    return {
        "settled_usd": str(guard.settled_usd),
        "reserved_usd": str(guard.reserved_usd),
        "stopped": guard.stopped,
        "calls_count": len(guard.calls),
    }


def _write_evidence(phase: str, payload: dict) -> Path:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    out = EVIDENCE_DIR / f"{phase}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


class _Phase:
    """批次运行上下文：捕获 + 事件时间线 + 证据（异常也落盘）。"""

    def __init__(self, phase: str, model: str) -> None:
        self.phase = phase
        self.model = model
        self.captured: list = []
        self.timeline: list[str] = []
        self.chunk1_hook: Callable[[], None] | None = None  # 首 chunk 同步取消钩子
        self.payload: dict = {"phase": phase, "model_requested": model, "started_at": _now()}
        self.api_key = read_api_key_from_env()  # 缺失即拒绝（不显示值）
        self.guard = PersistentFeeGuard(LEDGER_PATH)

    def on_event(self, name: str) -> None:
        self.timeline.append(name)
        if name == "chunk:1" and self.chunk1_hook is not None:
            # 在 wire 首 chunk 送达的同一调用栈内同步取消（最小化竞态窗口）
            self.chunk1_hook()

    def build_agent(self, *, tools: list | None = None):
        return build_spike_agent(
            self.model,
            guard=self.guard,
            api_key=self.api_key,
            tools=tools,
            instructions=INSTRUCTIONS,
            captured=self.captured,
            on_event=self.on_event,
        )

    def finish(self, extra: dict | None = None) -> Path:
        self.payload["finished_at"] = _now()
        self.payload["events"] = self.timeline
        self.payload["wire_requests"] = [_wire_request_view(r) for r in self.captured]
        self.payload["fee_ledger"] = _fee_view(self.guard)
        self.payload["usage_views"] = _usage_views(self.guard)
        if extra:
            self.payload.update(extra)
        out = _write_evidence(self.phase, self.payload)
        print(f"[{self.phase}] evidence -> {out}")
        print(f"[{self.phase}] settled=${self.guard.settled_usd} reserved=${self.guard.reserved_usd} "
              f"calls={len(self.guard.calls)} stopped={self.guard.stopped}")
        return out


def _prefix_checks(r1a, r1b, r2a) -> dict:
    """字节级前缀稳定检查（真实 wire 字节，非再序列化）：
    r1a=run1 请求1，r1b=run1 工具往返请求2，r2a=run2 追加请求1。"""
    m1a, m1b, m2a = r1a.wire_elements("messages"), r1b.wire_elements("messages"), r2a.wire_elements("messages")
    return {
        "resident_layer_byte_stable": m1a[0] == m1b[0] == m2a[0],
        "tool_roundtrip_prefix_byte_stable": m1a[:2] == m1b[:2],
        "append_prefix_byte_stable": m1b[:4] == m2a[:4],
        "tools_schema_byte_stable": r1a.wire_array("tools") == r1b.wire_array("tools") == r2a.wire_array("tools"),
        "message_counts": {"r1a": len(m1a), "r1b": len(m1b), "r2a": len(m2a)},
    }


async def flash_regression() -> None:
    phase = _Phase("flash-regression", "deepseek-v4-flash")
    error: str | None = None
    try:
        agent = phase.build_agent(tools=[lookup_equipment])
        res1 = await agent.run("必须调用工具 lookup_equipment 查询 barbell 的可用性，然后用一句话回答。")
        res2 = await agent.run("继续：把上一条结果压缩成不超过二十字的结论。", message_history=res1.all_messages())
        n_before_cancel = len(phase.captured)
        if len(phase.captured) < 3:
            raise RuntimeError(f"预期至少 3 个 wire 请求（工具往返+追加），实际 {len(phase.captured)}")
        checks = _prefix_checks(phase.captured[0], phase.captured[1], phase.captured[2])
        for name in (
            "resident_layer_byte_stable",
            "tool_roundtrip_prefix_byte_stable",
            "append_prefix_byte_stable",
            "tools_schema_byte_stable",
        ):
            if not checks[name]:
                raise AssertionError(f"字节级前缀稳定检查失败：{name}")

        # 真实流式取消：首 wire chunk 同步触发取消（禁止后续模型调用、不产生成功提交、预留全额保留）。
        harness_holder: dict = {}

        def _cancel_on_first_chunk() -> None:
            h = harness_holder.get("h")
            if h is not None:
                h.cancel()

        phase.chunk1_hook = _cancel_on_first_chunk
        harness_holder["h"] = RunHarness(phase.build_agent())
        outcome = await harness_holder["h"].run(
            "请用大约一百字说明深蹲的常见注意事项。", timeline=phase.timeline, stream=True
        )
        no_subsequent = len(phase.captured) == n_before_cancel + 1
        last = phase.guard.calls[-1]
        cancel_checks = {
            "cancelled_status": outcome.status == "cancelled",
            "no_subsequent_wire_request": no_subsequent,
            "reservation_retained_not_zero": last.settled_usd is None,
            "slot_released_in_timeline": "slot_released" in phase.timeline,
        }
        cancel_check = {
            "status": outcome.status,
            "events_tail": outcome.events,
            "last_record_note": last.note,
            "wire_request_count_before_cancel": n_before_cancel,
            "wire_request_count_final": len(phase.captured),
            **cancel_checks,
        }
        for name, ok in cancel_checks.items():
            if not ok:
                raise AssertionError(f"真实流式取消语义检查失败：{name}；check={cancel_check}")
        phase.payload["prefix_checks"] = checks
        phase.payload["cancel_check"] = cancel_check
        phase.payload["run2_output_len"] = len(res2.output or "")
        phase.finish()
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if error is not None:
            try:
                phase.finish({"error": error})  # 异常也落证据（保留现场）
            except Exception as exc2:
                print(f"[flash-regression] 证据落盘失败: {exc2}", file=sys.stderr)
        phase.guard.close()


async def smoke(model: str, phase_name: str) -> None:
    phase = _Phase(phase_name, model)
    error: str | None = None
    try:
        agent = phase.build_agent()
        result = await agent.run("文本冒烟：请用一句话说明什么是器械训练。")
        last = phase.guard.calls[-1]
        if last.settled_usd is None:
            raise AssertionError(f"{phase_name}: 最终调用未结算（usage 缺失或身份异常）：note={last.note}")
        phase.finish({"output_preview": (result.output or "")[:200], "final_note": last.note})
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if error is not None:
            try:
                phase.finish({"error": error})  # 异常也落证据（保留现场）
            except Exception as exc2:
                print(f"[{phase_name}] 证据落盘失败: {exc2}", file=sys.stderr)
        phase.guard.close()


def main() -> None:
    phase = sys.argv[1] if len(sys.argv) > 1 else ""
    phases = {
        "flash-regression": lambda: asyncio.run(flash_regression()),
        "pro-smoke": lambda: asyncio.run(smoke("deepseek-v4-pro", "pro-smoke")),
        "vision-smoke": lambda: asyncio.run(smoke("deepseek-v4-flash-vision-exp", "vision-smoke")),
    }
    if phase not in phases:
        raise SystemExit(f"用法: real_spike.py {' | '.join(phases)}")
    try:
        phases[phase]()
    except StopSpike as exc:
        # 护栏停止（如身份别名未验证、预算线、落盘失败）：证据已落盘，保留现场退出。
        print(f"STOP_SPIKE: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
