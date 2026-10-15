import asyncio
import json
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from math import isfinite
from pathlib import Path
from threading import current_thread
from time import perf_counter, time_ns
from uuid import UUID

import pytest

from app.agent.telemetry import (
    ModelAttributes,
    RunTrace,
    SpanName,
    SpanStatus,
    ToolAttributes,
    TraceSpan,
    trace_database_path,
    uuid4_canonical,
)
from app.interfaces.http import CredentialFilter

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND / "temp" / "trace" / "core"
EVIDENCE = ROOT / "evidence.json"

RESULTS: dict[str, object] = {}

MODEL_START = {"provider": "p", "model": "m", "call_kind": "normal"}
MODEL_SAMPLES = {
    "provider": "p",
    "model": "m",
    "call_kind": "normal",
    "stop_reason": "stop",
    "first_text_ms": 1.0,
    "input_tokens": 1,
    "output_tokens": 2,
    "cache_read_tokens": 3,
    "cache_write_tokens": 4,
}


def agent_trace(**kwargs) -> RunTrace:
    return RunTrace(
        session_id=uuid4_canonical(),
        operation_id=uuid4_canonical(),
        run_id=uuid4_canonical(),
        **kwargs,
    )


def confirmation_trace(**kwargs) -> RunTrace:
    return RunTrace(
        session_id=uuid4_canonical(),
        operation_id=uuid4_canonical(),
        run_id=None,
        **kwargs,
    )


def await_span(awaitable) -> object:
    async def drive():
        return await awaitable

    return asyncio.run(drive())


def test_callback_runs_exactly_once_and_value_preserved() -> None:
    trace = agent_trace()
    threads: list[str] = []
    sentinel = {"payload": 1}

    def sync_body(span: TraceSpan) -> object:
        threads.append(current_thread().name)
        return sentinel

    assert trace.start_span("agent_run", {}, sync_body) is sentinel
    assert threads == [current_thread().name]
    records = trace.records()
    assert [record.span_id for record in records] == [1]
    assert records[0].status is SpanStatus.completed and records[0].duration_ms >= 0

    async_trace = agent_trace()
    admitted: list[int] = []

    async def async_body(span: TraceSpan) -> object:
        admitted.append(1)
        await asyncio.sleep(0.02)
        return sentinel

    assert await_span(async_trace.start_span("agent_run", {}, async_body)) is sentinel
    assert admitted == [1]
    async_records = async_trace.records()
    assert async_records[0].status is SpanStatus.completed
    assert async_records[0].duration_ms >= 10

    nested = agent_trace()

    def outer(span: TraceSpan) -> str:
        def inner(child: TraceSpan) -> str:
            return "inner-value"

        return span.start_span("turn", {}, inner)

    assert nested.start_span("agent_run", {}, outer) == "inner-value"
    assert [(record.span_id, record.parent_span_id, record.name.value) for record in nested.records()] == [
        (1, None, "agent_run"),
        (2, 1, "turn"),
    ]
    RESULTS["callback_once"] = {
        "sync_thread": threads[0],
        "async_duration_ms": round(async_records[0].duration_ms, 3),
        "nested": [(record.span_id, record.parent_span_id) for record in nested.records()],
    }


def test_business_exception_object_propagates() -> None:
    sync_error = RuntimeError("同步业务失败正文")
    trace = agent_trace()

    def boom(span: TraceSpan) -> None:
        raise sync_error

    with pytest.raises(RuntimeError) as sync_caught:
        trace.start_span("agent_run", {}, boom)
    assert sync_caught.value is sync_error
    assert trace.records()[0].status is SpanStatus.failed
    assert trace.records()[0].error_code is None

    async_error = ValueError("异步业务失败正文")
    async_trace = agent_trace()

    async def async_boom(span: TraceSpan) -> None:
        await asyncio.sleep(0.001)
        raise async_error

    with pytest.raises(ValueError) as async_caught:
        await_span(async_trace.start_span("agent_run", {}, async_boom))
    assert async_caught.value is async_error
    assert async_trace.records()[0].status is SpanStatus.failed

    dumped = json.dumps([record.model_dump() for record in [*trace.records(), *async_trace.records()]])
    assert "业务失败正文" not in dumped
    RESULTS["exception_identity"] = {
        "sync_same_object": sync_caught.value is sync_error,
        "async_same_object": async_caught.value is async_error,
        "statuses": [trace.records()[0].status.value, async_trace.records()[0].status.value],
        "raw_detail_absent": "业务失败正文" not in dumped,
    }


def test_status_rules_and_late_updates_inert() -> None:
    trace = agent_trace()
    captured: dict[str, TraceSpan] = {}

    def body(span: TraceSpan) -> str:
        span.set_status("failed", "model_rate_limited")
        span.set_status("cancelled", "cancelled_by_user")
        captured["span"] = span
        return "value"

    assert trace.start_span("agent_run", {}, body) == "value"
    root = trace.records()[0]
    assert root.status is SpanStatus.cancelled and root.error_code == "cancelled_by_user"

    captured["span"].set_attributes({"first_text_ms": 9})
    captured["span"].set_status("completed")
    after = trace.records()[0]
    assert after.attributes == {} and after.status is SpanStatus.cancelled
    assert captured["span"].start_span("turn", {}, lambda child: "child-value") == "child-value"
    assert len(trace.records()) == 1

    explicit_then_throw = agent_trace()
    error = RuntimeError("显式状态后抛错")

    def keep_explicit(span: TraceSpan) -> None:
        span.set_status("completed")
        raise error

    with pytest.raises(RuntimeError):
        explicit_then_throw.start_span("agent_run", {}, keep_explicit)
    assert explicit_then_throw.records()[0].status is SpanStatus.completed

    uncontrolled = agent_trace()

    def bad_code(span: TraceSpan) -> None:
        span.set_status("failed", f"ValueError: {error} 原始异常详情")
        span.set_status("nonsense-status")
        span.set_status("failed", None)

    uncontrolled.start_span("agent_run", {}, bad_code)
    record = uncontrolled.records()[0]
    assert record.status is SpanStatus.failed and record.error_code is None
    RESULTS["status_rules"] = {
        "explicit_cancelled": root.status.value,
        "late_updates_inert": [after.attributes == {}, after.status.value, len(trace.records())],
        "explicit_before_throw": explicit_then_throw.records()[0].status.value,
        "uncontrolled_code_dropped": record.error_code,
    }


def test_nested_and_concurrent_span_relationships() -> None:
    trace = agent_trace()

    def work(index: int):
        async def turn_body(span: TraceSpan) -> str:
            async def model_body(child: TraceSpan) -> str:
                await asyncio.sleep(0.005)
                return "model-value"

            value = await span.start_span("model", MODEL_START, model_body)
            await asyncio.sleep(0.001 + index * 0.002)
            return f"{value}-{index}"

        return turn_body

    async def root_body(span: TraceSpan) -> list[str]:
        results = await asyncio.gather(*(span.start_span("turn", {}, work(index)) for index in range(8)))
        return list(results)

    assert await_span(trace.start_span("agent_run", {}, root_body)) == [f"model-value-{i}" for i in range(8)]
    records = trace.records()
    assert len(records) == 17
    assert {record.span_id for record in records} == set(range(1, 18))
    root = records[0]
    turns = [record for record in records if record.name is SpanName.turn]
    models = [record for record in records if record.name is SpanName.model]
    assert all(record.parent_span_id == 1 for record in turns)
    assert len(turns) == 8 and len(models) == 8
    assert {record.parent_span_id for record in models} == {record.span_id for record in turns}
    by_id = {record.span_id: record for record in records}
    assert all(record.parent_span_id < record.span_id for record in records[1:])
    assert all(by_id[record.parent_span_id].started_at <= record.started_at for record in records[1:])
    assert all(record.finished_at <= root.finished_at for record in records[1:])
    assert root.duration_ms >= max(record.duration_ms for record in records[1:])
    RESULTS["parentage"] = {
        "records": len(records),
        "turn_ids": sorted(record.span_id for record in turns),
        "model_parents": sorted(record.parent_span_id for record in models),
        "root_duration_ms": round(root.duration_ms, 3),
        "slowest_child_ms": round(max(record.duration_ms for record in records[1:]), 3),
    }


def test_thread_concurrent_registration_and_merge() -> None:
    trace = agent_trace()
    created: list[str] = []

    def tool_body(span: TraceSpan) -> str:
        span.set_attributes({"tool_name": "echo"})
        return "done"

    def root_body(span: TraceSpan) -> None:
        with ThreadPoolExecutor(max_workers=8) as pool:
            created.extend(
                pool.map(
                    lambda index: span.start_span("tool", {"tool_call_id": f"call-{index}"}, tool_body),
                    range(8),
                )
            )

    trace.start_span("agent_run", {}, root_body)
    records = trace.records()
    assert len(created) == 8 and set(created) == {"done"}
    assert [record.span_id for record in records] == list(range(1, 10))
    assert all(record.parent_span_id == 1 for record in records[1:])
    assert all(record.status is SpanStatus.completed for record in records)
    assert all(record.attributes["tool_name"] == "echo" for record in records[1:])

    merge_trace = agent_trace()

    def merge_body(span: TraceSpan) -> None:
        with ThreadPoolExecutor(max_workers=9) as pool:
            list(pool.map(lambda item: span.set_attributes({item[0]: item[1]}), MODEL_SAMPLES.items()))

    merge_trace.start_span("agent_run", {}, lambda root: root.start_span("model", MODEL_START, merge_body))
    model = next(record for record in merge_trace.records() if record.name is SpanName.model)
    assert model.attributes == MODEL_SAMPLES
    RESULTS["threaded"] = {
        "span_ids": [record.span_id for record in records],
        "tool_parents": sorted({record.parent_span_id for record in records[1:]}),
        "merged_attributes": model.attributes,
    }


def test_attribute_whitelist_and_unknown_usage() -> None:
    trace = agent_trace()

    def body(span: TraceSpan) -> None:
        span.set_attributes({"prompt": "对话正文与伤病信息"})
        span.set_attributes({"model": {"nested": 1}})
        span.set_attributes({"input_tokens": -1})
        span.set_attributes({"first_text_ms": float("inf")})
        span.set_attributes({"call_kind": "unknown_kind"})
        span.set_attributes({"output_tokens": None})
        span.set_attributes({"input_tokens": 0})
        span.set_attributes({"cache_read_tokens": 12})
        span.set_attributes({"first_text_ms": 0})

    trace.start_span("agent_run", {}, lambda root: root.start_span("model", MODEL_START, body))
    model = next(record for record in trace.records() if record.name is SpanName.model)
    assert model.attributes == {
        "provider": "p",
        "model": "m",
        "call_kind": "normal",
        "input_tokens": 0,
        "cache_read_tokens": 12,
        "first_text_ms": 0.0,
    }
    parsed = ModelAttributes.model_validate(model.attributes)
    assert parsed.output_tokens is None and parsed.cache_write_tokens is None and parsed.stop_reason is None
    assert parsed.input_tokens == 0

    turn = agent_trace()
    turn.start_span("agent_run", {}, lambda root: root.start_span("turn", {}, lambda child: "empty"))
    assert [record.attributes for record in turn.records()] == [{}, {}]
    RESULTS["attributes"] = {
        "kept": model.attributes,
        "unknown_read_as_null": [parsed.output_tokens, parsed.cache_write_tokens, parsed.stop_reason],
        "turn_empty_object": turn.records()[1].attributes,
    }


def test_required_start_attributes_reject_record_only() -> None:
    trace = agent_trace()
    values: list[str] = []

    def body(root: TraceSpan) -> str:
        values.append(root.start_span("model", {"provider": None, "model": "m", "call_kind": "normal"}, lambda s: "no-provider"))
        values.append(root.start_span("model", {}, lambda s: "missing-all"))
        values.append(root.start_span("tool", {"tool_call_id": "call-1"}, lambda s: "tool-name-null"))
        values.append(root.start_span("tool", {"tool_name": "echo"}, lambda s: "missing-id"))
        values.append(root.start_span("agent_run", {}, lambda s: "root-as-child"))
        values.append(root.start_span("confirmation", {}, lambda s: "confirmation-as-child"))
        values.append(root.start_span("ghost", {}, lambda s: "bad-name"))
        values.append(root.start_span("turn", {"unexpected": 1}, lambda s: "bad-attrs"))
        return "root-value"

    assert trace.start_span("agent_run", {}, body) == "root-value"
    assert values == [
        "no-provider",
        "missing-all",
        "tool-name-null",
        "missing-id",
        "root-as-child",
        "confirmation-as-child",
        "bad-name",
        "bad-attrs",
    ]
    records = trace.records()
    assert [(record.span_id, record.name.value, record.attributes) for record in records] == [
        (1, "agent_run", {}),
        (2, "tool", {"tool_call_id": "call-1"}),
    ]
    assert ToolAttributes.model_validate(records[1].attributes).tool_name is None

    confirmation = confirmation_trace()
    assert confirmation.start_span("confirmation", {"business_kind": "plan"}, lambda s: "saved") == "saved"
    assert confirmation.start_span("confirmation", {}, lambda s: "missing-kind") == "missing-kind"
    assert confirmation.start_span("agent_run", {}, lambda s: "wrong-root") == "wrong-root"
    kept = confirmation.records()
    assert [record.attributes for record in kept] == [{"business_kind": "plan"}]
    assert kept[0].run_id is None and kept[0].name is SpanName.confirmation

    # agent_run 根必须携带 run_id，缺失时整条链路拒绝。
    broken = RunTrace(session_id=uuid4_canonical(), operation_id=uuid4_canonical(), run_id=None)
    broken.start_span("agent_run", {}, lambda root: root.start_span("turn", {}, lambda s: "child"))
    assert broken.records() == []
    RESULTS["required_start"] = {
        "kept": [(record.span_id, record.name.value) for record in records],
        "confirmation_root": kept[0].model_dump(),
        "agent_root_without_run_id": len(broken.records()),
    }


def test_identity_and_timing_boundary() -> None:
    trace = agent_trace()
    assert re.fullmatch(r"[0-9a-f]{32}", trace.trace_id)
    assert UUID(trace.trace_id).version == 4

    accepted_at = time_ns() // 1_000_000 - 40
    accepted_perf = perf_counter() - 0.03
    timed = agent_trace(accepted_at=accepted_at, accepted_perf=accepted_perf)

    def body(root: TraceSpan) -> str:
        def child_body(child: TraceSpan) -> None:
            time.sleep(0.01)

        root.start_span("turn", {}, child_body)
        time.sleep(0.02)
        return "value"

    assert timed.start_span("agent_run", {}, body) == "value"
    root, child = timed.records()
    assert root.span_id == 1 and root.parent_span_id is None
    assert root.started_at == accepted_at
    assert root.duration_ms >= 40 and isfinite(root.duration_ms)
    assert child.started_at >= accepted_at and child.duration_ms >= 5
    assert root.finished_at >= child.finished_at
    assert abs(root.finished_at - time_ns() // 1_000_000) < 2_000
    assert all(record.trace_id == timed.trace_id for record in timed.records())

    reused = agent_trace(trace_id="abc")
    assert reused.trace_id == "abc"
    RESULTS["identity"] = {
        "trace_id": trace.trace_id,
        "root_started_at": root.started_at,
        "root_duration_ms": round(root.duration_ms, 3),
        "child_duration_ms": round(child.duration_ms, 3),
        "identity_shared": {record.session_id for record in timed.records()},
    }


def test_credential_boundary_rejects_record_and_subtree() -> None:
    secret = "sk-trace-secret-value"
    trace = agent_trace(contains=CredentialFilter((secret,)).contains)

    def body(root: TraceSpan) -> str:
        def leaky_turn(turn: TraceSpan) -> str:
            turn.start_span("model", {"provider": secret, "model": "m", "call_kind": "normal"}, lambda s: "leaky")
            return "leaky-turn"

        def clean_turn(turn: TraceSpan) -> str:
            def deep(child: TraceSpan) -> str:
                return "deep"

            return turn.start_span("tool", {"tool_call_id": "call-1"}, deep)

        root.start_span("turn", {}, leaky_turn)
        root.start_span("turn", {}, clean_turn)
        return "ok"

    assert trace.start_span("agent_run", {}, body) == "ok"
    records = trace.records()
    assert [(record.span_id, record.name.value, record.parent_span_id) for record in records] == [
        (1, "agent_run", None),
        (2, "turn", 1),
        (4, "turn", 1),
        (5, "tool", 4),
    ]
    dumped = json.dumps([record.model_dump() for record in records])
    assert secret not in dumped

    session = uuid4_canonical()
    root_hit = RunTrace(
        session_id=session,
        operation_id=uuid4_canonical(),
        run_id=uuid4_canonical(),
        contains=CredentialFilter((session,)).contains,
    )
    assert root_hit.start_span("agent_run", {}, lambda root: root.start_span("turn", {}, lambda s: "child")) == "child"
    assert root_hit.records() == []
    RESULTS["credentials"] = {
        "kept": [(record.span_id, record.name.value) for record in records],
        "secret_absent": secret not in dumped,
        "root_rejected_trace_records": len(root_hit.records()),
    }


def test_settlement_fault_and_double_failure() -> None:
    # 非法计时边界属纯观测输入错误：结算故障被隔离，业务结局保持原样。
    trace = agent_trace(accepted_perf="非法计时边界")

    def body(root: TraceSpan) -> str:
        return root.start_span("turn", {}, lambda child: "child-value")

    assert trace.start_span("agent_run", {}, body) == "child-value"
    assert trace.records() == []

    error = RuntimeError("业务与观测同时失败")
    double = agent_trace(accepted_perf="非法计时边界")

    def boom(root: TraceSpan) -> None:
        root.start_span("turn", {}, lambda child: None)
        raise error

    with pytest.raises(RuntimeError) as caught:
        double.start_span("agent_run", {}, boom)
    assert caught.value is error
    assert double.records() == []
    RESULTS["settlement_fault"] = {
        "value_preserved": "child-value",
        "records": len(trace.records()),
        "double_failure_exception": type(caught.value).__name__,
    }


def test_cooperative_cancellation_control_flow() -> None:
    trace = agent_trace()

    async def child_scenario() -> str:
        async def body(root: TraceSpan) -> str:
            async def slow(span: TraceSpan) -> None:
                await asyncio.sleep(5)

            child = asyncio.ensure_future(root.start_span("model", MODEL_START, slow))
            await asyncio.sleep(0.01)
            child.cancel()
            with pytest.raises(asyncio.CancelledError):
                await child
            return "cancelled-child"

        return await trace.start_span("agent_run", {}, body)

    assert await_span(child_scenario()) == "cancelled-child"
    records = trace.records()
    assert [(record.name.value, record.status.value) for record in records] == [
        ("agent_run", "completed"),
        ("model", "cancelled"),
    ]

    outer = agent_trace()

    async def outer_scenario() -> None:
        async def body(root: TraceSpan) -> None:
            async def slow(span: TraceSpan) -> None:
                await asyncio.sleep(5)

            await root.start_span("model", MODEL_START, slow)

        task = asyncio.ensure_future(outer.start_span("agent_run", {}, body))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    await_span(outer_scenario())
    assert [record.status.value for record in outer.records()] == ["cancelled", "cancelled"]
    RESULTS["cancellation"] = {
        "first": [(record.name.value, record.status.value) for record in records],
        "outer": [record.status.value for record in outer.records()],
    }


def test_process_exit_control_flow_preserved() -> None:
    trace = agent_trace()
    signal = SystemExit(3)

    def body(span: TraceSpan) -> None:
        raise signal

    with pytest.raises(SystemExit) as caught:
        trace.start_span("agent_run", {}, body)
    assert caught.value is signal
    assert trace.records()[0].status is SpanStatus.failed
    RESULTS["process_exit"] = {"same_object": caught.value is signal, "status": trace.records()[0].status.value}


def test_release_blocks_later_records_and_keeps_callback_once() -> None:
    trace = agent_trace()
    trace.start_span("agent_run", {}, lambda span: "value")
    trace.release()
    assert trace.released is True
    assert trace.records() == []
    assert trace.start_span("turn", {}, lambda span: "late") == "late"
    assert trace.records() == []

    captured: list[TraceSpan] = []

    def body(root: TraceSpan) -> str:
        def deep(span: TraceSpan) -> str:
            def inner(child: TraceSpan) -> str:
                captured.append(child)
                child.set_attributes({"unexpected": 1})
                child.set_status("nonsense")
                return "deep-value"

            return span.start_span("turn", {}, inner)

        return root.start_span("ghost", {}, deep)

    assert trace.start_span("agent_run", {}, body) == "deep-value"
    assert trace.records() == []
    assert len(captured) == 1
    assert captured[0].start_span("turn", {}, lambda s: "still-once") == "still-once"
    RESULTS["release"] = {
        "records_after_release": len(trace.records()),
        "unrecorded_nesting_depth": len(captured),
    }


def check() -> None:
    if ROOT.exists():
        shutil.rmtree(ROOT)
    ROOT.mkdir(parents=True, exist_ok=True)
    for name, function in list(globals().items()):
        if name.startswith("test_") and callable(function):
            function()
    assert trace_database_path() == BACKEND / "data" / "trace.db"
    EVIDENCE.write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(
        "Trace 采集核心：同步与异步 callback 恰好一次、返回值与异常对象身份保持、自动与显式状态覆盖及迟到更新无效、"
        "嵌套与并发父子编号、线程并发登记与属性合并、属性白名单与未知用量读取为 null、开始时必填字段、"
        "Trace 身份与受理起点计时边界、凭据命中拒绝相应记录及子树、结算故障隔离与双重失败、"
        "协作取消与进程退出控制流、释放后 callback 仍恰好一次且不记录通过"
    )


if __name__ == "__main__":
    check()
